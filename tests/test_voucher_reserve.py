import tempfile
import unittest
from pathlib import Path

from voucher_reserve import (
    Trial,
    build_complete_report,
    build_ledger,
    build_proposed_plan,
    build_trial_report,
    load_packet,
)
from voucher_reserve.cli import generate
from voucher_reserve.report import build_report_from_trial, post_cutoff_review, roll_forward

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.packet = load_packet(DATA)

    def by_voucher(self, as_of):
        return {row["voucher"]: row for row in build_ledger(self.packet, as_of)}

    def test_transfer_preserves_age_and_marks_gift_holder(self):
        view = self.by_voucher(67)
        v7 = view["V7"]
        self.assertEqual(v7["holder_type"], "gift")
        self.assertEqual(v7["transfer_count"], 1)
        self.assertEqual(67 - v7["issue"], 29)
        self.assertEqual(v7["adjusted_expiry"], 72)

    def test_unresolved_cross_store_claim_is_kept_at_first_cutoff(self):
        view = self.by_voucher(60)
        v7 = view["V7"]
        self.assertEqual(v7["status"], "cross_store_claim_unresolved")
        self.assertEqual({c["slot"] for c in v7["active_claims"]}, {"S1", "S2"})

    def test_rectification_resolves_to_b_but_does_not_rewrite_received_date(self):
        at_65 = self.by_voucher(65)["V7"]
        self.assertEqual(at_65["status"], "booked_pending_completion")
        self.assertEqual([c["slot"] for c in at_65["active_claims"]], ["S2"])
        event = next(e for e in at_65["actions"] if e["event"] == "E13")
        self.assertEqual(event["received"], 60)

    def test_voided_refund_reinstates_v10_only_after_rectification_cutoff(self):
        self.assertEqual(self.by_voucher(60)["V10"]["status"], "refunded")
        at_67 = self.by_voucher(67)["V10"]
        self.assertEqual(at_67["status"], "unused_live")
        self.assertIn("refund_record_voided:benefit_reinstated", at_67["warnings"])

    def test_complete_review_uses_late_records(self):
        view = self.by_voucher(None)
        self.assertEqual(view["V7"]["status"], "fulfilled")
        self.assertEqual(view["V8"]["status"], "refunded")
        self.assertEqual(view["V10"]["status"], "unused_live")

    def test_bookings_and_approvals_are_not_unused_reissues(self):
        view = self.by_voucher(67)
        for voucher in ("V7", "V8", "V9", "V11"):
            self.assertNotEqual(view[voucher]["status"], "unused_live")


class ReserveReportTests(unittest.TestCase):
    def setUp(self):
        self.packet = load_packet(DATA)
        self.reports = [build_trial_report(self.packet, row) for row in self.packet.closures]
        self.complete = build_complete_report(self.packet)

    def test_first_trial_coherent_hard_interval(self):
        report = self.reports[0]
        reserve = report["coverage_and_reserve"]
        self.assertEqual(reserve["uncommitted_interval_fen"], [13600, 20700])
        self.assertEqual(reserve["hard_total_interval_fen"], [26500, 33600])
        weekly = {row["settlement_week_start"]: row for row in report["weekly_reserve_table"]}
        self.assertEqual((weekly[64]["gross_min"], weekly[64]["gross_max"]), (4800, 21200))
        self.assertEqual((weekly[71]["gross_min"], weekly[71]["gross_max"]), (8100, 22100))
        self.assertEqual((weekly[78]["gross_min"], weekly[78]["gross_max"]), (0, 3900))

    def test_third_trial_includes_reinstated_v10(self):
        report = self.reports[2]
        assigned = [a["voucher"] for a in report["coherent_scenarios"]["minimum_total"]["assignments"]]
        self.assertIn("V10", assigned)
        reserve = report["coverage_and_reserve"]
        self.assertEqual(reserve["uncommitted_interval_fen"], [16400, 24900])
        self.assertEqual(reserve["hard_total_interval_fen"], [29300, 37800])

    def test_complete_review_narrows_after_late_terminal_records(self):
        reserve = self.complete["coverage_and_reserve"]
        self.assertEqual(reserve["uncommitted_interval_fen"], [10800, 16500])
        self.assertEqual(reserve["hard_total_interval_fen"], [23700, 29400])

    def test_weekly_endpoints_must_not_be_summed_as_one_scenario(self):
        report = self.reports[0]
        summed_weekly_max = sum(row["gross_max"] for row in report["weekly_reserve_table"])
        coherent_max = report["coverage_and_reserve"]["hard_total_interval_fen"][1]
        self.assertGreater(summed_weekly_max, coherent_max)
        self.assertIn("Do not sum", report["warning"])

    def test_sealed_slot_has_no_discretionary_capacity_and_plan_avoids_it(self):
        self.assertEqual(self.complete["slot_discretionary_caps"]["S5"], 0)
        at_67 = self.reports[2]
        inventory = {row["slot"]: row for row in at_67["slot_inventory"]}
        self.assertEqual(inventory["S1"]["remaining_for_additional_uncommitted_bookings"], 0)
        self.assertEqual(inventory["S5"]["discretionary_cohort_capacity"], 0)
        plan = build_proposed_plan(self.complete)
        destinations = [row["destination"] for row in plan["non_repeatable_rebook_destinations"]]
        self.assertNotIn("S5", destinations)
        vouchers = [row["voucher"] for row in plan["non_repeatable_rebook_destinations"]]
        vouchers += [row["voucher"] for row in plan["refund_instructions_in_scenario"]]
        self.assertEqual(len(vouchers), len(set(vouchers)))

    def test_dimension_tables_reconcile(self):
        for report in self.reports + [self.complete]:
            for tables in report["dimension_reconciliation"].values():
                self.assertTrue(tables["reconciles_to_scenario_total"])
                for dimension in ("by_batch", "by_holder_type", "by_responsible_site"):
                    self.assertGreater(sum(v["amount"] for v in tables[dimension].values()), 0)

    def test_rollforward_flags_v10_and_current_week_catchup(self):
        movement = roll_forward(self.reports[1], self.reports[2])
        self.assertEqual(movement["reinstated_after_void_or_new_evidence"], ["V10"])
        self.assertEqual(movement["uncommitted_interval_change_fen"], [2800, 4200])
        self.assertEqual(movement["catch_up_prudent_fen"], 4200)
        self.assertEqual(movement["catch_up_proposal_week"], 71)
        self.assertEqual(movement["non_releasable_payment_obligations_fen"], 8700)
        self.assertEqual(movement["signed_pending_reserve_fen"], 4200)

    def test_complete_review_separates_late_actuals_from_remaining_reserve(self):
        review = post_cutoff_review(self.packet, self.reports[2], self.complete)
        self.assertEqual(
            {row["voucher"]: row["actual_amount_fen"] for row in review["newly_terminal_actuals"]},
            {"V7": 2800, "V8": 4200},
        )
        self.assertEqual(review["new_actual_total_fen"], 7000)
        self.assertEqual(review["check_in_settlement_catch_up_fen"], 2800)
        self.assertEqual(review["late_cash_refunds_to_reconcile_fen"], 4200)
        self.assertEqual(review["favorable_estimate_variance_fen"], -1400)
        self.assertEqual(review["remaining_prudent_reserve_fen"], 29400)
        self.assertEqual(
            review["lifetime_memo_actuals_plus_registered_and_remaining_upper_fen"],
            36400,
        )

    def test_no_feasible_slot_forces_refund_only_interval(self):
        trial = Trial(
            name="sealed-only",
            exit_date=71,
            exit_sites=("A",),
            as_of=None,
            week_start=64,
            candidate_slot_ids=("S5",),
        )
        report = build_report_from_trial(self.packet, trial)
        self.assertEqual(report["coverage_and_reserve"]["uncommitted_interval_fen"], [16500, 16500])
        self.assertEqual(
            [v["voucher"] for v in report["population_groups"]["refund_only"]],
            ["V10", "V12", "V15", "V16"],
        )

    def test_generate_cli_outputs_reconciliations(self):
        with tempfile.TemporaryDirectory() as out:
            summary = generate(DATA, Path(out))
            self.assertTrue(Path(out, "summary.json").exists())
            self.assertTrue(Path(out, "complete_review.json").exists())
            self.assertEqual(summary["complete_review"]["prudent_reserve_fen"], 29400)


if __name__ == "__main__":
    unittest.main()
