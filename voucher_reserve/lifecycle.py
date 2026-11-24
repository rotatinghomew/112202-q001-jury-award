"""Voucher lifecycle reconstruction under as-of and complete views."""
from __future__ import annotations

from typing import Any

from .io import Packet


def visible_rows(rows: list[dict[str, Any]], as_of: int | None, *, date_key: str = "received"):
    if as_of is None:
        return list(rows)
    return [row for row in rows if row[date_key] <= as_of]


def voided_event_ids(packet: Packet, as_of: int | None) -> set[str]:
    voided: set[str] = set()
    for row in visible_rows(packet.rectifications, as_of):
        if row["kind"] == "void":
            # The event column is the record pointed to and revoked by R*.
            voided.add(row["event"])
    return voided


def expiry_extension(packet: Packet, voucher: dict[str, Any]) -> int:
    """Sum all pause extensions registered for this issuance segment.

    The packet explicitly says one segment can occur in several site rows; each
    row carries its own granted extension.
    """
    return sum(row["extend"] for row in packet.pauses if row["segment"] == voucher["batch"])


def _chronological(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(actions, key=lambda row: (row["happened"], row["received"], row["event"]))


def build_ledger(packet: Packet, as_of: int | None = None) -> list[dict[str, Any]]:
    voided = voided_event_ids(packet, as_of)
    actions = [
        row
        for row in visible_rows(packet.actions, as_of)
        if row["event"] not in voided
    ]
    by_voucher: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        by_voucher.setdefault(action["voucher"], []).append(action)

    approvals = {
        row["voucher"]: row
        for row in visible_rows(packet.approvals, as_of, date_key="signed")
    }

    ledger: list[dict[str, Any]] = []
    for voucher in packet.vouchers:
        vid = voucher["voucher"]
        term = packet.terms[voucher["terms"]]
        extension = expiry_extension(packet, voucher)
        adjusted_expiry = voucher["expiry"] + extension
        refund_start = voucher["issue"] + term["refund_day"]

        rows = _chronological(by_voucher.get(vid, []))
        holder_type = "buyer"
        holder_evidence = "issue_default"
        active_claims: dict[str, dict[str, Any]] = {}
        terminal: dict[str, Any] | None = None
        terminal_conflicts: list[dict[str, Any]] = []
        warnings: list[str] = []
        transfer_count = 0
        latest_event_party: str | None = None

        for event in rows:
            latest_event_party = event["party"]
            if event["kind"] == "transfer":
                transfer_count += 1
                holder_type = event["party"]
                holder_evidence = event["event"]
            elif event["party"] in {"buyer", "gift"} and transfer_count == 0:
                holder_type = event["party"]
                holder_evidence = event["event"]

            if event["kind"] == "book" and event["slot"]:
                active_claims[event["slot"]] = event
            elif event["kind"] == "cancel" and event["slot"]:
                active_claims.pop(event["slot"], None)
            elif event["kind"] in {"attend", "cash_refund"}:
                if terminal is None:
                    terminal = event
                elif terminal["kind"] != event["kind"] or terminal["event"] != event["event"]:
                    terminal_conflicts.append(event)

        # A terminal refund/attendance consumes the whole benefit. Remaining
        # booking rows are history, not live claims.
        effective_claims = list(active_claims.values()) if terminal is None else []

        # A non-transfer event claiming a different holder after a transfer is
        # retained as a data-quality warning, never used to restart age.
        if transfer_count and latest_event_party and latest_event_party != holder_type:
            warnings.append(
                f"party_mismatch:transfer_holder={holder_type},latest_party={latest_event_party}"
            )

        relevant_voids = []
        voucher_event_ids = {a["event"] for a in packet.actions if a["voucher"] == vid}
        for row in visible_rows(packet.rectifications, as_of):
            if row["event"] in voucher_event_ids:
                relevant_voids.append(row["record"])
                if row["kind"] == "void" and row["event"] == "E16":
                    warnings.append("refund_record_voided:benefit_reinstated")

        approval = approvals.get(vid)
        status, reason = classify_status(
            terminal=terminal,
            claims=effective_claims,
            approval=approval,
            conflicts=terminal_conflicts,
        )

        if terminal and terminal["happened"] < refund_start and terminal["kind"] == "cash_refund":
            warnings.append("refund_before_contractual_refund_day")
        if terminal_conflicts:
            warnings.append("conflicting_terminal_records_preserved")
        if len(effective_claims) > 1:
            warnings.append("cross_store_duplicate_claim_unresolved")

        ledger.append(
            {
                **voucher,
                "term": term,
                "extension_days": extension,
                "adjusted_expiry": adjusted_expiry,
                "refund_start": refund_start,
                "holder_type": holder_type,
                "holder_evidence": holder_evidence,
                "transfer_count": transfer_count,
                "actions": rows,
                "active_claims": effective_claims,
                "terminal": terminal,
                "terminal_conflicts": terminal_conflicts,
                "approval": approval,
                "status": status,
                "reason": reason,
                "rectifications": relevant_voids,
                "warnings": warnings,
            }
        )
    return ledger


def classify_status(
    *,
    terminal: dict[str, Any] | None,
    claims: list[dict[str, Any]],
    approval: dict[str, Any] | None,
    conflicts: list[dict[str, Any]],
) -> tuple[str, str]:
    if conflicts:
        return "terminal_conflict_unresolved", "preserve_competing_checkin_refund_records"
    if terminal and terminal["kind"] == "attend":
        return "fulfilled", f"check_in:{terminal['event']}"
    if terminal and terminal["kind"] == "cash_refund":
        return "refunded", f"actual_refund:{terminal['event']}"
    if approval and approval["operation"] == "refund_due":
        return "registered_refund_due", f"approval:{approval['approval']}"
    if approval and approval["operation"] == "rebook":
        return "committed_rebook", f"approval:{approval['approval']}:{approval['destination']}"
    if approval and approval["operation"] == "reserve":
        return "registered_reserve", f"approval:{approval['approval']}"
    if len(claims) > 1:
        return "cross_store_claim_unresolved", "possible_lawful_attributions_kept"
    if len(claims) == 1:
        return "booked_pending_completion", f"booking:{claims[0]['event']}:{claims[0]['slot']}"
    return "unused_live", "no_terminal_or_commitment_observed"


def voucher_view_rows(ledger: list[dict[str, Any]], on_date: int | None = None):
    rows = []
    for item in ledger:
        age = None if on_date is None else on_date - item["issue"]
        remaining = None if on_date is None else item["adjusted_expiry"] - on_date
        rows.append(
            {
                "voucher": item["voucher"],
                "batch": item["batch"],
                "status": item["status"],
                "holder_type": item["holder_type"],
                "holder_evidence": item["holder_evidence"],
                "original_site": item["original_site"],
                "terms": item["terms"],
                "age_on_date": age,
                "adjusted_expiry": item["adjusted_expiry"],
                "remaining_days": remaining,
                "active_slots": ",".join(claim["slot"] for claim in item["active_claims"]) or "-",
                "approval": item["approval"]["approval"] if item["approval"] else "-",
                "reason": item["reason"],
                "warnings": ";".join(item["warnings"]) or "-",
            }
        )
    return rows
