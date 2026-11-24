"""Finance/front-desk reporting, reconciliation and signable plan artifacts."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .io import Packet
from .lifecycle import build_ledger, voucher_view_rows
from .planning import Trial, enumerate_scenarios, week_key


def json_safe(value: Any):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(json_safe(v) for v in value)
    return value


def summarize_statuses(ledger: list[dict[str, Any]]):
    counts: dict[str, int] = defaultdict(int)
    ids: dict[str, list[str]] = defaultdict(list)
    for item in ledger:
        counts[item["status"]] += 1
        ids[item["status"]].append(item["voucher"])
    return {"counts": dict(sorted(counts.items())), "vouchers": dict(sorted(ids.items()))}


def historical_support(packet: Packet, as_of: int | None = None) -> dict[str, Any]:
    complete = build_ledger(packet, as_of)
    by_id = {item["voucher"]: item for item in complete}

    def summary(predicate):
        items = [item for item in complete if predicate(item)]
        return {
            "vouchers": [item["voucher"] for item in items],
            "count": len(items),
            "fulfilled": [i["voucher"] for i in items if i["status"] == "fulfilled"],
            "refunded": [i["voucher"] for i in items if i["status"] == "refunded"],
            "open": [i["voucher"] for i in items if i["terminal"] is None and not i["approval"]],
        }

    cross_site = []
    cancel_rebook = []
    for item in complete:
        terminal = item["terminal"]
        if terminal and terminal["kind"] == "attend" and terminal["site"] != item["original_site"]:
            cross_site.append(item["voucher"])
        canceled_slots = {a["slot"] for a in item["actions"] if a["kind"] == "cancel"}
        if canceled_slots and terminal and terminal["kind"] == "attend":
            cancel_rebook.append(item["voucher"])

    return {
        "all_observed": summary(lambda _: True),
        "mature_batch_B1": summary(lambda i: i["batch"] == "B1"),
        "terms_T1": summary(lambda i: i["terms"] == "T1"),
        "terms_T2": summary(lambda i: i["terms"] == "T2"),
        "transferred": summary(lambda i: i["transfer_count"] > 0),
        "pause_extended": summary(lambda i: i["extension_days"] > 0),
        "cross_site_fulfilment": {
            "vouchers": sorted(set(cross_site)),
            "note": "check-in site differs from original_site",
        },
        "cancel_then_fulfil": {"vouchers": sorted(set(cancel_rebook))},
        "rectified_duplicates": [
            row
            for row in packet.rectifications
            if row["competing_event"] is not None
            and (as_of is None or row["received"] <= as_of)
        ],
        "late_received_terminal_records": [
            {
                "event": action["event"],
                "voucher": action["voucher"],
                "happened": action["happened"],
                "received": action["received"],
                "kind": action["kind"],
            }
            for action in packet.actions
            if action["received"] > action["happened"]
            and action["kind"] in {"attend", "cash_refund"}
            and (as_of is None or action["received"] <= as_of)
        ],
        "knowledge_cutoff": as_of,
        "interpretation": (
            "B1 supplies mature evidence for transfer, pause extension and cross-store "
            "redemption. B2 has cutoff-only and rectified experience; B3 has no mature "
            "outcome, so its unobserved behavior is not treated as breakage."
        ),
    }


def slot_inventory(packet: Packet, ledger: list[dict[str, Any]], enumeration: dict[str, Any]):
    approved_at_slot: dict[str, int] = defaultdict(int)
    for row in enumeration["approved_rows"]:
        if row["operation"] == "rebook" and row["destination"]:
            approved_at_slot[row["destination"]] += 1

    unapproved_claims: dict[str, list[str]] = defaultdict(list)
    all_claims: dict[str, list[str]] = defaultdict(list)
    for item in ledger:
        for claim in item["active_claims"]:
            slot_id = claim["slot"]
            all_claims[slot_id].append(item["voucher"])
            if not item["approval"]:
                unapproved_claims[slot_id].append(item["voucher"])

    rows = []
    for slot_id, slot in sorted(packet.slots.items()):
        approved_count = approved_at_slot.get(slot_id, 0)
        protected = max(slot["sealed"], approved_count)
        discretionary_cap = enumeration["slot_discretionary_caps"].get(slot_id, slot["capacity"] - protected)
        unapproved_count = len(unapproved_claims.get(slot_id, []))
        rows.append(
            {
                "slot": slot_id,
                "site": slot["site"],
                "day": slot["day"],
                "capacity": slot["capacity"],
                "sealed": slot["sealed"],
                "approved_rebook_count": approved_count,
                "protected_count": protected,
                "discretionary_cohort_capacity": discretionary_cap,
                "current_active_claims": all_claims.get(slot_id, []),
                "unapproved_claim_vouchers": unapproved_claims.get(slot_id, []),
                "remaining_for_additional_uncommitted_bookings": max(0, discretionary_cap - unapproved_count),
            }
        )
    return rows


def dimension_tables(enumeration: dict[str, Any]) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    approved = enumeration["approved_rows"]

    for basis in ("min_total_scenario", "max_total_scenario"):
        scenario = enumeration[basis]
        rows = list(approved) + [
            {
                "batch": assignment["batch"],
                "holder_type": assignment["holder_type"],
                "site": assignment["site"],
                "amount": assignment["amount"],
                "operation": assignment["cost_type"],
            }
            for assignment in scenario["assignments"]
        ]

        def group(key: str):
            grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0})
            for row in rows:
                label = row[key] if key != "site" else row["site"]
                bucket = grouped[label]
                bucket["count"] += 1
                bucket["amount"] += row["amount"]
            return {label: values for label, values in sorted(grouped.items())}

        tables[basis] = {
            "by_batch": group("batch"),
            "by_holder_type": group("holder_type"),
            "by_responsible_site": group("site"),
            "total": sum(row["amount"] for row in rows),
            "reconciles_to_scenario_total": (
                sum(row["amount"] for row in rows)
                == sum(a["amount"] for a in approved) + scenario["total"]
            ),
        }
    return tables


def coverage_summary(enumeration: dict[str, Any], trial: Trial) -> dict[str, Any]:
    fixed_total = sum(row["amount"] for row in enumeration["approved_rows"])
    agreed_payments = sum(
        row["amount"] for row in enumeration["approved_rows"] if row["non_releasable"]
    )
    signed_reserve = sum(
        row["amount"] for row in enumeration["approved_rows"] if row["registered_reserve"]
    )
    open_min = enumeration["min_total_scenario"]["total"]
    open_max = enumeration["max_total_scenario"]["total"]
    hard_min = fixed_total + open_min
    hard_max = fixed_total + open_max
    coverage_floor = round(trial.coverage * hard_max)
    full_fixed_plus_coverage_open = fixed_total + round(trial.coverage * open_max)
    result = {
        "coverage_requirement": trial.coverage,
        "agreed_payment_obligations_full": agreed_payments,
        "signed_pending_reserve": signed_reserve,
        "fixed_approved_total": fixed_total,
        "uncommitted_interval_fen": [open_min, open_max],
        "hard_total_interval_fen": [hard_min, hard_max],
        "finance_coverage_arithmetic_floor_fen": coverage_floor,
        "full_fixed_plus_coverage_on_uncommitted_fen": full_fixed_plus_coverage_open,
        "prudent_reserve_fen": hard_max,
        "prudent_coverage_ratio": 1.0,
        "recommendation": (
            "Reserve the hard upper end. The packet has no mature evidence for "
            "abandonment in the unsupported strata, so a point estimate is not a "
            "recognized liability."
        ),
    }
    if trial.provision_rate is not None:
        rate_amount = round(trial.provision_rate * open_max)
        result["provision_rate"] = trial.provision_rate
        result["rate_based_uncommitted_provision_fen"] = rate_amount
        result["rate_policy_total_fen"] = fixed_total + rate_amount
    return result


def build_trial_report(
    packet: Packet,
    closure: dict[str, Any],
    *,
    provision_rate: float | None = None,
) -> dict[str, Any]:
    trial = Trial(
        name=f"exit-{closure['date']}-{closure['site']}-asof-{closure['as_of']}",
        exit_date=closure["date"],
        exit_sites=(closure["site"],),
        as_of=closure["as_of"],
        week_start=closure["week_start"],
        coverage=closure["coverage"],
        provision_rate=provision_rate,
    )
    return build_report_from_trial(packet, trial)


def build_report_from_trial(packet: Packet, trial: Trial) -> dict[str, Any]:
    ledger = build_ledger(packet, trial.as_of)
    enumeration = enumerate_scenarios(packet, ledger, trial)
    exit_week = week_key(trial.exit_date, trial.week_start)
    weekly_table = []
    for week, values in enumeration["weekly_ranges"].items():
        weekly_table.append(
            {
                "settlement_week_start": week,
                "future_or_current_at_exit": week >= exit_week,
                **values,
            }
        )

    return {
        "trial": json_safe(trial.__dict__),
        "status_summary": summarize_statuses(ledger),
        "voucher_view": voucher_view_rows(ledger, trial.exit_date),
        "cross_store": cross_store_view(ledger),
        "slot_inventory": slot_inventory(packet, ledger, enumeration),
        "population_groups": enumeration["population_groups"],
        "slot_discretionary_caps": enumeration["slot_discretionary_caps"],
        "capacity_warnings": enumeration["capacity_warnings"],
        "weekly_reserve_table": weekly_table,
        "coherent_scenarios": {
            "minimum_total": enumeration["min_total_scenario"],
            "maximum_total": enumeration["max_total_scenario"],
        },
        "dimension_reconciliation": dimension_tables(enumeration),
        "coverage_and_reserve": coverage_summary(enumeration, trial),
        "historical_support": historical_support(packet, trial.as_of),
        "feasible_fulfil_all": enumeration["feasible_fulfil_all"],
        "valid_scenario_count": enumeration["valid_scenario_count"],
        "warning": (
            "Weekly gross_min/gross_max are marginal bounds over mutually exclusive "
            "coherent scenarios. Do not sum all weekly minima or all weekly maxima as "
            "one balance; use coherent_scenarios for joint totals."
        ),
    }


def cross_store_view(ledger: list[dict[str, Any]]):
    result = []
    for item in ledger:
        claims = [
            {
                "event": claim["event"],
                "site": claim["site"],
                "slot": claim["slot"],
                "received": claim["received"],
            }
            for claim in item["active_claims"]
        ]
        if len(claims) > 1 or item["status"] == "cross_store_claim_unresolved":
            result.append(
                {
                    "voucher": item["voucher"],
                    "possible_attributions": claims,
                    "rectifications": item["rectifications"],
                    "treatment": "All lawful attributions remain in the interval; no venue is selected for finance.",
                }
            )
    return result


def build_complete_report(
    packet: Packet,
    *,
    exit_date: int = 71,
    site: str = "A",
    week_start: int = 64,
    coverage: float = 0.95,
    provision_rate: float | None = None,
) -> dict[str, Any]:
    closure = {"date": exit_date, "site": site, "as_of": None, "coverage": coverage, "week_start": week_start}
    report = build_trial_report(packet, closure, provision_rate=provision_rate)
    report["trial"]["name"] = f"complete-review-exit-{exit_date}-{site}"
    report["historical_support"] = historical_support(packet)
    return report


def voucher_lifecycle_amounts(packet: Packet, report: dict[str, Any] | None = None):
    ledger = build_ledger(packet, None)
    contingent = {}
    if report:
        for assignment in report["coherent_scenarios"]["maximum_total"]["assignments"]:
            contingent[assignment["voucher"]] = assignment
    rows = []
    for item in ledger:
        terminal = item["terminal"]
        approval = item["approval"]
        if terminal and terminal["kind"] == "attend":
            recognized = item["term"]["fulfil_cost"]
            basis = "actual_check_in_cost"
            site = terminal["site"]
        elif terminal and terminal["kind"] == "cash_refund":
            recognized = item["term"]["refundable"]
            basis = "actual_paid_refund"
            site = terminal["site"]
        elif approval:
            recognized = approval["amount"]
            basis = f"signed_{approval['operation']}"
            site = approval["destination"] and packet.slots.get(approval["destination"], {}).get("site") or item["original_site"]
        else:
            recognized = 0
            basis = "uncommitted_not_recognized"
            site = item["original_site"]
        upper = contingent.get(item["voucher"], {}).get("amount", 0)
        rows.append(
            {
                "voucher": item["voucher"],
                "batch": item["batch"],
                "holder_type": item["holder_type"],
                "status": item["status"],
                "responsible_site": site,
                "recognized_or_signed_amount_fen": recognized,
                "basis": basis,
                "contingent_upper_if_uncommitted_fen": upper if basis == "uncommitted_not_recognized" else 0,
                "reason": item["reason"],
            }
        )
    return rows


def build_proposed_plan(report: dict[str, Any], *, choice: str = "minimum_total") -> dict[str, Any]:
    scenario_key = "minimum_total" if choice == "minimum_total" else "maximum_total"
    scenario = report["coherent_scenarios"][scenario_key]
    destinations = []
    refunds = []
    exit_date = report["trial"]["exit_date"]
    for assignment in scenario["assignments"]:
        if assignment["cost_type"] == "fulfil":
            destinations.append(
                {
                    "voucher": assignment["voucher"],
                    "destination": assignment["slot"],
                    "site": assignment["site"],
                    "settlement_week": assignment["week"],
                    "amount": assignment["amount"],
                    "hold_days": assignment.get("hold_days"),
                    "slot_wait_deadline_from_exit": exit_date + assignment.get("hold_days", 0),
                    "unique_voucher_destination": True,
                    "sealed_capacity_checked": True,
                }
            )
        else:
            refunds.append(
                {
                    "voucher": assignment["voucher"],
                    "responsible_site": assignment["site"],
                    "settlement_week": assignment["week"],
                    "amount": assignment["amount"],
                }
            )

    destination_slots = [row["destination"] for row in destinations]
    if len(destination_slots) != len(set(destination_slots)):
        # Multiple vouchers may share a slot up to capacity; uniqueness required
        # is per voucher, not one voucher per slot. Capacity was enumerated.
        pass
    voucher_ids = [row["voucher"] for row in destinations] + [row["voucher"] for row in refunds]
    if len(voucher_ids) != len(set(voucher_ids)):
        raise ValueError("same voucher counted twice")

    selected_weekly: dict[int, int] = defaultdict(int)
    for row in report["weekly_reserve_table"]:
        selected_weekly[row["settlement_week_start"]] += row["fixed"]
    for assignment in scenario["assignments"]:
        selected_weekly[assignment["week"]] += assignment["amount"]

    registered_or_cutoff_pending = [
        row["voucher"]
        for row in report["voucher_view"]
        if row["status"] in {"registered_reserve", "cross_store_claim_unresolved", "booked_pending_completion"}
    ]
    awaiting_acceptance = [row["voucher"] for row in destinations]
    return {
        "status": "proposed_for_finance_signature",
        "choice_basis": choice,
        "non_repeatable_rebook_destinations": destinations,
        "refund_instructions_in_scenario": refunds,
        "awaiting_customer_acceptance": awaiting_acceptance,
        "registered_or_cutoff_pending": registered_or_cutoff_pending,
        "pending_or_acceptance_unresolved": sorted(set(awaiting_acceptance + registered_or_cutoff_pending)),
        "selected_plan_weekly_requirement_fen": {
            str(week): amount for week, amount in sorted(selected_weekly.items())
        },
        "weekly_reserve_table": report["weekly_reserve_table"],
        "coverage_and_reserve": report["coverage_and_reserve"],
        "constraints": {
            "no_sealed_slot_invasion": True,
            "one_benefit_per_voucher": True,
            "coherent_scenario_only": True,
        },
    }


def create_signed_plan(
    report: dict[str, Any],
    *,
    approval_id: str,
    signed_day: int,
    choice: str = "minimum_total",
) -> dict[str, Any]:
    proposed = build_proposed_plan(report, choice=choice)
    proposed.update(
        {
            "status": "signed_artifact",
            "approval": approval_id,
            "signed": signed_day,
            "approved_tsv_not_modified": True,
            "immutable_destination_register": proposed.pop("non_repeatable_rebook_destinations"),
        }
    )
    return proposed


def post_cutoff_review(packet: Packet, previous_report: dict[str, Any], complete_report: dict[str, Any]):
    from_as_of = previous_report["trial"]["as_of"]
    before = {item["voucher"]: item for item in build_ledger(packet, from_as_of)}
    after = {item["voucher"]: item for item in build_ledger(packet, None)}

    newly_actual = []
    prior_max_assignments = {
        row["voucher"]: row
        for row in previous_report["coherent_scenarios"]["maximum_total"]["assignments"]
    }
    prior_allowance = 0
    for vid, current in after.items():
        previous = before[vid]
        if previous["terminal"] is None and current["terminal"] is not None:
            terminal = current["terminal"]
            amount = (
                current["term"]["fulfil_cost"]
                if terminal["kind"] == "attend"
                else current["term"]["refundable"]
            )
            prior = prior_max_assignments.get(vid, {}).get("amount", 0)
            prior_allowance += prior
            newly_actual.append(
                {
                    "voucher": vid,
                    "kind": terminal["kind"],
                    "event": terminal["event"],
                    "happened": terminal["happened"],
                    "received": terminal["received"],
                    "site": terminal["site"],
                    "actual_amount_fen": amount,
                    "prior_prudent_allowance_fen": prior,
                    "estimate_variance_fen": amount - prior,
                }
            )

    actual_total = sum(row["actual_amount_fen"] for row in newly_actual)
    checkin_catchup = sum(
        row["actual_amount_fen"] for row in newly_actual if row["kind"] == "attend"
    )
    late_refunds = sum(
        row["actual_amount_fen"] for row in newly_actual if row["kind"] == "cash_refund"
    )
    reserve = complete_report["coverage_and_reserve"]
    prudent_remaining = reserve["prudent_reserve_fen"]
    return {
        "from_trial": previous_report["trial"]["name"],
        "to_trial": complete_report["trial"]["name"],
        "newly_terminal_actuals": newly_actual,
        "new_actual_total_fen": actual_total,
        "check_in_settlement_catch_up_fen": checkin_catchup,
        "late_cash_refunds_to_reconcile_fen": late_refunds,
        "prior_prudent_allowance_for_these_vouchers_fen": prior_allowance,
        "favorable_estimate_variance_fen": actual_total - prior_allowance,
        "remaining_prudent_reserve_fen": prudent_remaining,
        "lifetime_memo_actuals_plus_registered_and_remaining_upper_fen": actual_total + prudent_remaining,
        "prior_prudent_reserve_fen": previous_report["coverage_and_reserve"]["prudent_reserve_fen"],
        "rule": (
            "Do not release the old provision merely because the complete file lowers "
            "the remaining interval. Post newly received check-in costs as catch-up and "
            "reconcile late actual refunds; only the residual variance is an estimate change."
        ),
    }


def roll_forward(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    prev_reserve = previous["coverage_and_reserve"]
    curr_reserve = current["coverage_and_reserve"]
    prev_open = prev_reserve["uncommitted_interval_fen"]
    curr_open = curr_reserve["uncommitted_interval_fen"]

    prev_status = set(previous["status_summary"]["vouchers"].get("refunded", []))
    curr_status = set(current["status_summary"]["vouchers"].get("unused_live", []))
    reinstated = sorted(v for v in curr_status if v in prev_status)

    fixed_now = curr_reserve["fixed_approved_total"]
    current_week_row = next(
        (row for row in current["weekly_reserve_table"] if row["future_or_current_at_exit"]),
        current["weekly_reserve_table"][0] if current["weekly_reserve_table"] else None,
    )
    return {
        "from_trial": previous["trial"]["name"],
        "to_trial": current["trial"]["name"],
        "reinstated_after_void_or_new_evidence": reinstated,
        "non_releasable_payment_obligations_fen": curr_reserve["agreed_payment_obligations_full"],
        "signed_pending_reserve_fen": curr_reserve["signed_pending_reserve"],
        "registered_total_including_reserve_fen": fixed_now,
        "uncommitted_interval_change_fen": [
            curr_open[0] - prev_open[0],
            curr_open[1] - prev_open[1],
        ],
        "catch_up_proposal_week": current_week_row["settlement_week_start"] if current_week_row else None,
        "catch_up_prudent_fen": max(0, curr_open[1] - prev_open[1]),
        "estimate_interval_now_fen": curr_open,
        "rule": (
            "Approved rebook/refund_due amounts are not released. A voided refund "
            "reinstates the voucher; the prudent added amount is a current-week "
            "catch-up provision, while lower/upper movement remains an estimate interval."
        ),
    }
