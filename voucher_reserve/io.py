"""Offline loading and validation for the voucher reserve packet."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _int(value: str) -> int | None:
    value = value.strip()
    if value in {"-", "?"}:
        return None
    return int(value)


def _str(value: str) -> str | None:
    value = value.strip()
    return None if value == "-" else value


def _sites(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Packet:
    root: Path
    vouchers: list[dict[str, Any]]
    terms: dict[str, dict[str, Any]]
    actions: list[dict[str, Any]]
    rectifications: list[dict[str, Any]]
    pauses: list[dict[str, Any]]
    slots: dict[str, dict[str, Any]]
    closures: list[dict[str, Any]]
    approvals: list[dict[str, Any]]

    @property
    def approval_by_voucher(self) -> dict[str, dict[str, Any]]:
        return {row["voucher"]: row for row in self.approvals}


def _read_tsv(path: Path, fields: list[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t", strict=True)
        header = next(reader, None)
        if header != fields:
            raise ValueError(f"{path.name}: 表头不符")
        rows: list[dict[str, str]] = []
        for line_number, values in enumerate(reader, start=2):
            if len(values) != len(fields) or any(not value.strip() for value in values):
                raise ValueError(f"{path.name}:{line_number}: 列数不符或有未标记的空值")
            rows.append(dict(zip(fields, values, strict=True)))
        return rows


def load_packet(folder: str | Path = "data") -> Packet:
    root = Path(folder)
    with (root / "layout.json").open(encoding="utf-8") as handle:
        layout = json.load(handle)

    raw = {
        name: _read_tsv(root / name, fields)
        for name, fields in layout.items()
        if name.endswith(".tsv")
    }

    vouchers: list[dict[str, Any]] = []
    for row in raw["vouchers.tsv"]:
        vouchers.append(
            {
                "voucher": row["voucher"],
                "batch": row["batch"],
                "issue": int(row["issue"]),
                "expiry": int(row["expiry"]),
                "original_site": row["original_site"],
                "terms": row["terms"],
            }
        )

    terms: dict[str, dict[str, Any]] = {}
    for row in raw["terms.tsv"]:
        terms[row["terms"]] = {
            "terms": row["terms"],
            "sites": _sites(row["sites"]),
            "hold_days": int(row["hold_days"]),
            "refund_day": int(row["refund_day"]),
            "refundable": int(row["refundable"]),
            "fulfil_cost": int(row["fulfil_cost"]),
        }

    actions: list[dict[str, Any]] = []
    for row in raw["actions.tsv"]:
        actions.append(
            {
                "event": row["event"],
                "voucher": row["voucher"],
                "happened": int(row["happened"]),
                "received": int(row["received"]),
                "kind": row["kind"],
                "party": row["party"],
                "site": row["site"],
                "slot": _str(row["slot"]),
            }
        )

    rectifications = [
        {
            "record": row["record"],
            "event": row["event"],
            "received": int(row["received"]),
            "kind": row["kind"],
            "competing_event": _str(row["competing_event"]),
        }
        for row in raw["rectifications.tsv"]
    ]

    pauses = [
        {
            "site": row["site"],
            "start": int(row["start"]),
            "end": int(row["end"]),
            "extend": int(row["extend"]),
            "segment": row["segment"],
        }
        for row in raw["pause.tsv"]
    ]

    slots: dict[str, dict[str, Any]] = {}
    for row in raw["slots.tsv"]:
        slots[row["slot"]] = {
            "slot": row["slot"],
            "site": row["site"],
            "day": int(row["day"]),
            "capacity": int(row["capacity"]),
            "sealed": int(row["sealed"]),
        }

    closures = [
        {
            "date": int(row["date"]),
            "site": row["site"],
            "as_of": int(row["as_of"]),
            "coverage": float(row["coverage"]),
            "week_start": int(row["week_start"]),
        }
        for row in raw["closures.tsv"]
    ]

    approvals = [
        {
            "approval": row["approval"],
            "voucher": row["voucher"],
            "signed": int(row["signed"]),
            "operation": row["operation"],
            "destination": _str(row["destination"]),
            "amount": int(row["amount"]),
            "settlement_week": int(row["settlement_week"]),
        }
        for row in raw["approved.tsv"]
    ]

    return Packet(
        root=root,
        vouchers=vouchers,
        terms=terms,
        actions=actions,
        rectifications=rectifications,
        pauses=pauses,
        slots=slots,
        closures=closures,
        approvals=approvals,
    )
