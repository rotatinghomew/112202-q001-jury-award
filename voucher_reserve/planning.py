"""Trial construction, coherent scenario enumeration and feasibility checks."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from .io import Packet
from .lifecycle import build_ledger


@dataclass(frozen=True)
class Trial:
    name: str
    exit_date: int
    exit_sites: tuple[str, ...]
    as_of: int | None
    week_start: int
    coverage: float = 1.0
    candidate_slot_ids: tuple[str, ...] | None = None
    use_hold_window: bool = True
    provision_rate: float | None = None


def week_key(day: int, week_start: int) -> int:
    return week_start + ((day - week_start) // 7) * 7


def slot_discretionary_capacity(packet: Packet, slot: dict[str, Any], trial: Trial) -> int:
    approved_count = sum(
        1
        for approval in packet.approvals
        if approval["operation"] == "rebook"
        and approval["destination"] == slot["slot"]
        and (trial.as_of is None or approval["signed"] <= trial.as_of)
    )
    return slot["capacity"] - max(slot["sealed"], approved_count)


def approved_placements(packet: Packet, ledger: list[dict[str, Any]], as_of: int | None):
    counts: dict[str, int] = {}
    rows = []
    for item in ledger:
        approval = item.get("approval")
        if not approval:
            continue
        destination = approval["destination"]
        site = None
        if destination and destination in packet.slots:
            site = packet.slots[destination]["site"]
            if approval["operation"] == "rebook":
                counts[destination] = counts.get(destination, 0) + 1
        rows.append(
            {
                "approval": approval["approval"],
                "voucher": item["voucher"],
                "batch": item["batch"],
                "holder_type": item["holder_type"],
                "operation": approval["operation"],
                "destination": destination,
                "site": site or item["original_site"],
                "amount": approval["amount"],
                "settlement_week": approval["settlement_week"],
                "non_releasable": approval["operation"] in {"rebook", "refund_due"},
                "registered_reserve": approval["operation"] == "reserve",
            }
        )
    return rows, counts


def fixed_weekly(approved_rows: list[dict[str, Any]]) -> dict[int, dict[str, int]]:
    weeks: dict[int, dict[str, int]] = {}
    for row in approved_rows:
        week = row["settlement_week"]
        bucket = weeks.setdefault(week, {"payment_obligation": 0, "registered_reserve": 0, "total": 0})
        if row["registered_reserve"]:
            bucket["registered_reserve"] += row["amount"]
        else:
            bucket["payment_obligation"] += row["amount"]
        bucket["total"] += row["amount"]
    return dict(sorted(weeks.items()))


def _fulfil_option(
    *,
    item: dict[str, Any],
    slot: dict[str, Any],
    kind: str,
    trial: Trial,
    week_start: int,
) -> dict[str, Any] | None:
    term = item["term"]
    if slot["site"] not in term["sites"]:
        return None
    if slot["day"] > item["adjusted_expiry"]:
        return None
    if trial.use_hold_window and slot["day"] > trial.exit_date + term["hold_days"]:
        return None
    service_week = week_key(slot["day"], week_start)
    funding_week = max(week_key(trial.exit_date, week_start), service_week)
    return {
        "kind": kind,
        "slot": slot["slot"],
        "site": slot["site"],
        "day": slot["day"],
        "service_week": service_week,
        "week": funding_week,
        "cost_type": "fulfil",
        "amount": term["fulfil_cost"],
    }


def voucher_options(
    packet: Packet,
    item: dict[str, Any],
    trial: Trial,
) -> list[dict[str, Any]]:
    """All mutually exclusive terminal choices preserved for one voucher."""
    term = item["term"]
    ws = trial.week_start
    refund_week = week_key(max(trial.exit_date, item["refund_start"]), ws)
    options: list[dict[str, Any]] = [
        {
            "kind": "refund",
            "slot": None,
            "site": item["original_site"],
            "day": None,
            "service_week": None,
            "week": refund_week,
            "cost_type": "refund",
            "amount": term["refundable"],
        }
    ]

    seen_slots: set[str] = set()

    # Existing bookings/claims are retained as one possible fulfilment route.
    # A post-exit slot at an exiting site cannot be retained under the trial.
    for claim in item["active_claims"]:
        slot_id = claim["slot"]
        if not slot_id or slot_id not in packet.slots or slot_id in seen_slots:
            continue
        slot = packet.slots[slot_id]
        if slot["site"] in trial.exit_sites and slot["day"] >= trial.exit_date:
            continue
        option = _fulfil_option(item=item, slot=slot, kind="retain_booking", trial=trial, week_start=ws)
        if option and slot_discretionary_capacity(packet, slot, trial) <= 0:
            option = None
        if option:
            # Existing claim can be older than the conservative wait window?
            # It is already held, so hold_days governs new waits, not forfeiture
            # of an existing reservation. Keep it even if the window is short.
            if trial.use_hold_window and slot["day"] > trial.exit_date + term["hold_days"]:
                option["hold_window_overridden_by_existing_claim"] = True
            options.append(option)
            seen_slots.add(slot_id)

    if trial.candidate_slot_ids is not None:
        candidate_ids = trial.candidate_slot_ids
    else:
        candidate_ids = tuple(
            slot_id
            for slot_id, slot in packet.slots.items()
            if slot["day"] >= trial.exit_date
            and slot["site"] not in trial.exit_sites
        )

    for slot_id in candidate_ids:
        if slot_id in seen_slots or slot_id not in packet.slots:
            continue
        slot = packet.slots[slot_id]
        if slot["site"] in trial.exit_sites or slot["day"] < trial.exit_date:
            continue
        if slot_discretionary_capacity(packet, slot, trial) <= 0:
            continue
        option = _fulfil_option(item=item, slot=slot, kind="new_rebook", trial=trial, week_start=ws)
        if option:
            options.append(option)
            seen_slots.add(slot_id)

    # De-duplicate exact options while retaining stable order.
    unique: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str | None]] = set()
    for option in options:
        key = (option["kind"], option["slot"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(option)
    return unique


def _dimension_row(item: dict[str, Any], option: dict[str, Any] | None, scenario_name: str):
    site = option["site"] if option else item["original_site"]
    return {
        "scenario": scenario_name,
        "voucher": item["voucher"],
        "batch": item["batch"],
        "holder_type": item["holder_type"],
        "responsible_site": site,
        "amount": option["amount"] if option else 0,
        "cost_type": option["cost_type"] if option else "none",
        "week": option["week"] if option else None,
        "slot": option.get("slot") if option else None,
    }


def enumerate_scenarios(
    packet: Packet,
    ledger: list[dict[str, Any]],
    trial: Trial,
) -> dict[str, Any]:
    approved_rows, approved_at_slot = approved_placements(packet, ledger, trial.as_of)
    fixed = fixed_weekly(approved_rows)

    open_items = [
        item
        for item in ledger
        if item["terminal"] is None and item["approval"] is None
        and item["status"] != "terminal_conflict_unresolved"
    ]
    conflict_items = [
        item for item in ledger if item["status"] == "terminal_conflict_unresolved"
    ]
    # Conflicts are not silently optimized away. Treat them as open too, with
    # both terminal routes represented by their recorded amounts.
    open_items.extend(conflict_items)

    slot_caps: dict[str, int] = {}
    capacity_warnings: list[str] = []
    for slot_id, slot in packet.slots.items():
        protected = max(slot["sealed"], approved_at_slot.get(slot_id, 0))
        cap = slot["capacity"] - protected
        slot_caps[slot_id] = cap
        if cap < 0:
            capacity_warnings.append(f"{slot_id}:approved_or_sealed_count_exceeds_capacity")

    options_by_voucher = {
        item["voucher"]: voucher_options(packet, item, trial)
        for item in open_items
    }

    valid_scenarios: list[dict[str, Any]] = []
    voucher_ids = [item["voucher"] for item in open_items]
    option_lists = [options_by_voucher[vid] for vid in voucher_ids]

    for combination in itertools.product(*option_lists):
        used: dict[str, int] = {}
        capacity_ok = True
        for option in combination:
            slot_id = option.get("slot")
            if slot_id:
                used[slot_id] = used.get(slot_id, 0) + 1
                if used[slot_id] > max(0, slot_caps.get(slot_id, 0)):
                    capacity_ok = False
                    break
        if not capacity_ok:
            continue

        weekly: dict[int, dict[str, int]] = {}
        assignments = []
        dimensions = []
        total = 0
        for item, option in zip(open_items, combination, strict=True):
            week = option["week"]
            bucket = weekly.setdefault(week, {"refund": 0, "fulfil": 0, "total": 0})
            bucket[option["cost_type"]] += option["amount"]
            bucket["total"] += option["amount"]
            total += option["amount"]
            assignments.append(
                {
                    "voucher": item["voucher"],
                    "batch": item["batch"],
                    "holder_type": item["holder_type"],
                    "choice": option["kind"],
                    "cost_type": option["cost_type"],
                    "amount": option["amount"],
                    "week": week,
                    "slot": option.get("slot"),
                    "site": option["site"],
                    "hold_days": item["term"]["hold_days"],
                }
            )
            dimensions.append(_dimension_row(item, option, "coherent_scenario"))

        valid_scenarios.append(
            {"total": total, "weekly": dict(sorted(weekly.items())), "assignments": assignments, "dimensions": dimensions}
        )

    if not valid_scenarios:
        raise RuntimeError("no coherent scenarios; refund option should always be available")

    min_total = min(valid_scenarios, key=lambda s: s["total"])
    max_total = max(valid_scenarios, key=lambda s: s["total"])

    all_weeks = sorted(
        set(fixed)
        | {week for scenario in valid_scenarios for week in scenario["weekly"]}
    )
    weekly_ranges = {}
    for week in all_weeks:
        totals = [scenario["weekly"].get(week, {}).get("total", 0) for scenario in valid_scenarios]
        refunds = [scenario["weekly"].get(week, {}).get("refund", 0) for scenario in valid_scenarios]
        fulfils = [scenario["weekly"].get(week, {}).get("fulfil", 0) for scenario in valid_scenarios]
        fixed_total = fixed.get(week, {}).get("total", 0)
        weekly_ranges[week] = {
            "open_min": min(totals),
            "open_max": max(totals),
            "fixed": fixed_total,
            "gross_min": fixed_total + min(totals),
            "gross_max": fixed_total + max(totals),
            "refund_min": min(refunds),
            "refund_max": max(refunds),
            "fulfil_min": min(fulfils),
            "fulfil_max": max(fulfils),
        }

    groups = population_groups(packet, open_items, options_by_voucher, trial)
    feasible_fulfil_all = any(
        all(assignment["cost_type"] == "fulfil" for assignment in scenario["assignments"])
        for scenario in valid_scenarios
    )

    return {
        "trial": trial.__dict__,
        "open_vouchers": voucher_ids,
        "approved_rows": approved_rows,
        "fixed_weekly": fixed,
        "slot_discretionary_caps": dict(sorted(slot_caps.items())),
        "capacity_warnings": capacity_warnings,
        "weekly_ranges": weekly_ranges,
        "min_total_scenario": min_total,
        "max_total_scenario": max_total,
        "valid_scenario_count": len(valid_scenarios),
        "feasible_fulfil_all": feasible_fulfil_all,
        "population_groups": groups,
        "options": options_by_voucher,
    }


def population_groups(
    packet: Packet,
    open_items: list[dict[str, Any]],
    options_by_voucher: dict[str, list[dict[str, Any]]],
    trial: Trial,
):
    groups = {
        "transferable_from_exit": [],
        "refund_only": [],
        "still_redeem_in_place": [],
    }
    for item in open_items:
        options = options_by_voucher[item["voucher"]]
        fulfil_options = [option for option in options if option["cost_type"] == "fulfil"]
        exit_claim = any(claim["site"] in trial.exit_sites for claim in item["active_claims"])
        exit_linked = item["original_site"] in trial.exit_sites or exit_claim
        record = {
            "voucher": item["voucher"],
            "batch": item["batch"],
            "holder_type": item["holder_type"],
            "original_site": item["original_site"],
            "exit_claim": exit_claim,
            "fulfil_option_count": len(fulfil_options),
            "fulfil_slots": [option["slot"] for option in fulfil_options if option.get("slot")],
            "constraints": constraint_reasons(packet, item, fulfil_options, trial),
        }
        if not fulfil_options:
            groups["refund_only"].append(record)
        elif exit_linked:
            groups["transferable_from_exit"].append(record)
        else:
            groups["still_redeem_in_place"].append(record)
    return groups


def constraint_reasons(
    packet: Packet,
    item: dict[str, Any],
    fulfil_options: list[dict[str, Any]],
    trial: Trial,
) -> list[str]:
    if fulfil_options:
        return []
    reasons = [f"no_fulfilment_route_within_{item['term']['hold_days']}_day_hold_window_and_expiry"]
    blocked_sealed = []
    expired = []
    for slot_id, slot in packet.slots.items():
        if slot["site"] not in item["term"]["sites"] or slot["site"] in trial.exit_sites:
            continue
        if slot["day"] > item["adjusted_expiry"]:
            expired.append(slot_id)
        elif trial.use_hold_window and slot["day"] > trial.exit_date + item["term"]["hold_days"]:
            continue
        elif slot["capacity"] <= slot["sealed"]:
            blocked_sealed.append(slot_id)
    if blocked_sealed:
        reasons.append("sealed_or_no_discretionary_capacity:" + ",".join(blocked_sealed))
    if expired:
        reasons.append("past_adjusted_expiry:" + ",".join(expired))
    reasons.append("remaining_liability_must_be_refund_or_pending_reserve")
    return reasons
