"""Command line entry point for generating reserve and front-desk reports."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .io import load_packet
from .planning import Trial
from .report import (
    build_complete_report,
    build_proposed_plan,
    build_report_from_trial,
    build_trial_report,
    create_signed_plan,
    json_safe,
    post_cutoff_review,
    roll_forward,
    voucher_lifecycle_amounts,
)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_safe(value), ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_dimension_csv(path: Path, report: dict) -> None:
    rows = []
    for basis, tables in report["dimension_reconciliation"].items():
        for dimension in ("by_batch", "by_holder_type", "by_responsible_site"):
            for label, values in tables[dimension].items():
                rows.append(
                    {
                        "basis": basis,
                        "dimension": dimension,
                        "label": label,
                        "count": values["count"],
                        "amount_fen": values["amount"],
                    }
                )
    write_csv(path, rows)


def generate(data_dir: Path, out_dir: Path, *, provision_rate: float | None = None) -> dict:
    packet = load_packet(data_dir)
    closure_reports = [
        build_trial_report(packet, closure, provision_rate=provision_rate)
        for closure in packet.closures
    ]
    complete = build_complete_report(packet, provision_rate=provision_rate)
    proposed = build_proposed_plan(complete)

    write_json(out_dir / "trials.json", closure_reports)
    write_json(out_dir / "complete_review.json", complete)
    write_json(out_dir / "proposed_plan.json", proposed)
    write_json(
        out_dir / "rollforward.json",
        [
            roll_forward(closure_reports[i - 1], closure_reports[i])
            for i in range(1, len(closure_reports))
        ],
    )
    write_json(
        out_dir / "post_cutoff_review.json",
        post_cutoff_review(packet, closure_reports[-1], complete),
    )

    for report in closure_reports:
        stem = report["trial"]["name"].replace("-", "_")
        write_csv(out_dir / f"vouchers_{stem}.tsv", report["voucher_view"])
        write_dimension_csv(out_dir / f"reconciliation_{stem}.tsv", report)

    write_csv(out_dir / "vouchers_complete.tsv", complete["voucher_view"])
    write_csv(out_dir / "lifecycle_amounts_complete.tsv", voucher_lifecycle_amounts(packet, complete))
    write_dimension_csv(out_dir / "reconciliation_complete.tsv", complete)

    summary = {
        "closure_trials": [
            {
                "trial": report["trial"]["name"],
                "coverage": report["trial"]["coverage"],
                "open_vouchers": report["coherent_scenarios"]["minimum_total"]["assignments"]
                and [a["voucher"] for a in report["coherent_scenarios"]["minimum_total"]["assignments"]],
                "hard_total_interval_fen": report["coverage_and_reserve"]["hard_total_interval_fen"],
                "prudent_reserve_fen": report["coverage_and_reserve"]["prudent_reserve_fen"],
                "weekly": report["weekly_reserve_table"],
            }
            for report in closure_reports
        ],
        "complete_review": {
            "hard_total_interval_fen": complete["coverage_and_reserve"]["hard_total_interval_fen"],
            "prudent_reserve_fen": complete["coverage_and_reserve"]["prudent_reserve_fen"],
            "weekly": complete["weekly_reserve_table"],
        },
        "proposed_plan": proposed["status"],
    }
    write_json(out_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    report_parser = sub.add_parser("report", help="generate JSON/TSV reports")
    report_parser.add_argument("--data", type=Path, default=Path("data"))
    report_parser.add_argument("--out", type=Path, default=Path("reports"))
    report_parser.add_argument("--provision-rate", type=float, default=None)

    trial_parser = sub.add_parser("trial", help="run one handler-submitted trial")
    trial_parser.add_argument("--data", type=Path, default=Path("data"))
    trial_parser.add_argument("--out", type=Path, default=None, help="optional JSON output file")
    trial_parser.add_argument("--name", default="custom-trial")
    trial_parser.add_argument("--exit-date", type=int, required=True)
    trial_parser.add_argument("--site", action="append", required=True, help="exiting site; repeat for several sites")
    trial_parser.add_argument("--as-of", type=int, default=None)
    trial_parser.add_argument("--week-start", type=int, default=64)
    trial_parser.add_argument("--coverage", type=float, default=0.95)
    trial_parser.add_argument("--slots", default=None, help="comma-separated acceptable rebook slot IDs")
    trial_parser.add_argument("--ignore-hold-window", action="store_true")
    trial_parser.add_argument("--provision-rate", type=float, default=None)

    approve_parser = sub.add_parser("approve-complete", help="write a signed plan artifact from the complete review")
    approve_parser.add_argument("--data", type=Path, default=Path("data"))
    approve_parser.add_argument("--out", type=Path, default=Path("reports/signed_plan.json"))
    approve_parser.add_argument("--approval-id", required=True)
    approve_parser.add_argument("--signed-day", type=int, required=True)
    approve_parser.add_argument("--choice", choices=("minimum_total", "maximum_total"), default="minimum_total")
    approve_parser.add_argument("--provision-rate", type=float, default=None)

    args = parser.parse_args(argv)
    if args.command == "report":
        summary = generate(args.data, args.out, provision_rate=args.provision_rate)
        print(json.dumps(json_safe(summary), ensure_ascii=False, indent=2))
        return 0
    if args.command == "trial":
        packet = load_packet(args.data)
        slot_ids = tuple(s.strip() for s in args.slots.split(",") if s.strip()) if args.slots else None
        trial = Trial(
            name=args.name,
            exit_date=args.exit_date,
            exit_sites=tuple(args.site),
            as_of=args.as_of,
            week_start=args.week_start,
            coverage=args.coverage,
            candidate_slot_ids=slot_ids,
            use_hold_window=not args.ignore_hold_window,
            provision_rate=args.provision_rate,
        )
        report = build_report_from_trial(packet, trial)
        encoded = json.dumps(json_safe(report), ensure_ascii=False, indent=2)
        if args.out:
            write_json(args.out, report)
            print(json.dumps({"written": str(args.out)}, ensure_ascii=False))
        else:
            print(encoded)
        return 0
    if args.command == "approve-complete":
        packet = load_packet(args.data)
        report = build_complete_report(packet, provision_rate=args.provision_rate)
        plan = create_signed_plan(
            report,
            approval_id=args.approval_id,
            signed_day=args.signed_day,
            choice=args.choice,
        )
        write_json(args.out, plan)
        print(json.dumps(json_safe({"written": str(args.out), "approval": args.approval_id}), ensure_ascii=False))
        return 0
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
