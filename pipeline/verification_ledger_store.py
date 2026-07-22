# SPDX-License-Identifier: Apache-2.0
"""Crash-safe persistence for the scheduled-gate ledger.

The file is a snapshot plus append-only identity-scoped trail.  It has no
knowledge of contracts or plugins, which keeps historical reads independent of
the project configuration that happens to be installed later.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from pipeline.verification_ledger import (
    TERMINAL_DISPOSITIONS,
    GateLedgerRow,
    GateTrailEvent,
    reduce_disposition,
)

FILENAME = "scheduled_gate_ledger.json"
SCHEMA_VERSION = "1"


class LedgerStoreError(ValueError):
    """Raised for malformed artifacts or forbidden semantic mutation."""


@dataclass(frozen=True)
class ScheduledGateLedger:
    """Versioned durable ledger; rows retain declaration order exactly."""

    rows: tuple[GateLedgerRow, ...]
    trail: tuple[GateTrailEvent, ...] = ()
    finalized: bool = False

    def __post_init__(self) -> None:
        """Reject invalid snapshots before they can be persisted.

        The store is deliberately the only place that turns the immutable model
        into an artifact.  Keeping these checks here prevents ``write_ledger``
        from creating a file that its strict loader would subsequently reject.
        """
        identities = [row.identity for row in self.rows]
        if len(identities) != len(set(identities)):
            raise LedgerStoreError("scheduled-gate ledger has duplicate identities")
        known = set(identities)
        if any(event.identity not in known for event in self.trail):
            raise LedgerStoreError("scheduled-gate trail references an unknown identity")
        if self.finalized and any(
            row.disposition not in TERMINAL_DISPOSITIONS for row in self.rows
        ):
            raise LedgerStoreError("finalized scheduled-gate ledger has open or invalid rows")

    def append(self, event: GateTrailEvent) -> ScheduledGateLedger:
        if event.identity not in {row.identity for row in self.rows}:
            raise LedgerStoreError(f"unknown scheduled-gate identity {event.identity!r}")
        # Event identity + semantic payload form its idempotency key.  Replays do
        # not grow the trail, while a distinct observation remains explainable.
        if event in self.trail:
            return self
        if self.finalized:
            raise LedgerStoreError("finalized scheduled-gate ledger cannot be updated")
        return replace(self, trail=(*self.trail, event))

    def finalize(self) -> ScheduledGateLedger:
        if self.finalized:
            return self
        rows = tuple(
            replace(
                row,
                disposition=reduce_disposition(row, self.trail),
                receipt_evidence=_last_receipt_evidence(row, self.trail),
            )
            for row in self.rows
        )
        return replace(self, rows=rows, finalized=True)


def ledger_path(run_dir: Path) -> Path:
    return run_dir / FILENAME


def write_ledger(run_dir: Path, ledger: ScheduledGateLedger) -> Path:
    """Atomically replace the artifact only after a complete deterministic write."""
    run_dir.mkdir(parents=True, exist_ok=True)
    target = ledger_path(run_dir)
    encoded = _serialize(ledger)
    fd, temporary = tempfile.mkstemp(prefix=f".{FILENAME}.", suffix=".tmp", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return target


def load_ledger(run_dir: Path) -> ScheduledGateLedger:
    """Strictly load a ledger; partial/unknown shapes never silently coerce."""
    path = ledger_path(run_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerStoreError(f"invalid scheduled-gate ledger at {path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "finalized", "rows", "trail"}:
        raise LedgerStoreError("scheduled-gate ledger has an invalid top-level shape")
    if raw["schema_version"] != SCHEMA_VERSION or not isinstance(raw["finalized"], bool):
        raise LedgerStoreError("unsupported scheduled-gate ledger schema")
    rows = tuple(_row_from_wire(item) for item in _list(raw["rows"], "rows"))
    identities = [row.identity for row in rows]
    if len(identities) != len(set(identities)):
        raise LedgerStoreError("scheduled-gate ledger has duplicate identities")
    trail = tuple(_event_from_wire(item) for item in _list(raw["trail"], "trail"))
    if any(event.identity not in set(identities) for event in trail):
        raise LedgerStoreError("scheduled-gate trail references an unknown identity")
    ledger = ScheduledGateLedger(rows, trail, raw["finalized"])
    if ledger.finalized and any(
        row.disposition not in TERMINAL_DISPOSITIONS for row in rows
    ):
        raise LedgerStoreError("finalized scheduled-gate ledger has open or invalid rows")
    return ledger


def update_ledger(run_dir: Path, event: GateTrailEvent | None = None, *, finalize: bool = False) -> ScheduledGateLedger:
    """Load, idempotently append/finalize, and atomically persist one update."""
    ledger = load_ledger(run_dir)
    if event is not None:
        ledger = ledger.append(event)
    if finalize:
        ledger = ledger.finalize()
    write_ledger(run_dir, ledger)
    return ledger


def _serialize(ledger: ScheduledGateLedger) -> str:
    raw = {
        "schema_version": SCHEMA_VERSION,
        "finalized": ledger.finalized,
        "rows": [asdict(row) for row in ledger.rows],
        "trail": [asdict(event) for event in ledger.trail],
    }
    return json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise LedgerStoreError(f"scheduled-gate ledger {label} must be a list")
    return value


def _row_from_wire(value: Any) -> GateLedgerRow:
    if not isinstance(value, dict):
        raise LedgerStoreError("scheduled-gate ledger row must be an object")
    expected = set(GateLedgerRow.__dataclass_fields__)
    if set(value) != expected:
        raise LedgerStoreError("scheduled-gate ledger row has an invalid shape")
    for key in ("gate_sets", "condition_paths", "selection_task_kinds"):
        if not isinstance(value[key], list) or not all(isinstance(item, str) for item in value[key]):
            raise LedgerStoreError(f"scheduled-gate ledger row {key} must be string list")
        value = {**value, key: tuple(value[key])}
    for key in (
        "gate", "hook", "phase", "timing", "run_mode", "condition",
        "activation_binding", "policy", "kind", "when", "execution_policy",
        "consequence",
    ):
        if not isinstance(value[key], str):
            raise LedgerStoreError(f"scheduled-gate ledger row {key} must be a string")
    for key in ("declared", "selectable"):
        if not isinstance(value[key], bool):
            raise LedgerStoreError(f"scheduled-gate ledger row {key} must be bool")
    if value["disposition"] is not None and value["disposition"] not in TERMINAL_DISPOSITIONS:
        raise LedgerStoreError("scheduled-gate ledger row has invalid disposition")
    if value["selected"] is not None and not isinstance(value["selected"], bool):
        raise LedgerStoreError("scheduled-gate ledger row selected must be bool or null")
    for key in ("resolved", "selection_reason", "executor", "trigger", "receipt_evidence"):
        if value[key] is not None and not isinstance(value[key], str):
            raise LedgerStoreError(
                f"scheduled-gate ledger row {key} must be a string or null",
            )
    try:
        return GateLedgerRow(**value)
    except (TypeError, ValueError) as exc:
        raise LedgerStoreError("invalid scheduled-gate ledger row") from exc


def _event_from_wire(value: Any) -> GateTrailEvent:
    if not isinstance(value, dict) or set(value) != set(GateTrailEvent.__dataclass_fields__):
        raise LedgerStoreError("scheduled-gate trail event has an invalid shape")
    if value.get("kind") not in {"selection", "execution", "reuse", "receipt"}:
        raise LedgerStoreError("scheduled-gate trail event has invalid kind")
    if not all(isinstance(value[key], str) for key in ("command", "hook", "phase", "kind", "outcome", "reason")):
        raise LedgerStoreError("scheduled-gate trail event fields must be strings")
    if value["receipt_evidence"] is not None and not isinstance(
        value["receipt_evidence"], str,
    ):
        raise LedgerStoreError(
            "scheduled-gate trail event receipt_evidence must be a string or null",
        )
    if not isinstance(value["rerun"], bool):
        raise LedgerStoreError("scheduled-gate trail event rerun must be bool")
    try:
        return GateTrailEvent(**value)
    except (TypeError, ValueError) as exc:
        raise LedgerStoreError("invalid scheduled-gate trail event") from exc


def _last_receipt_evidence(row: GateLedgerRow, trail: tuple[GateTrailEvent, ...]) -> str | None:
    for event in reversed(trail):
        if event.identity == row.identity and event.receipt_evidence is not None:
            return event.receipt_evidence
    return row.receipt_evidence


__all__ = [
    "FILENAME", "SCHEMA_VERSION", "LedgerStoreError", "ScheduledGateLedger",
    "ledger_path", "load_ledger", "update_ledger", "write_ledger",
]
