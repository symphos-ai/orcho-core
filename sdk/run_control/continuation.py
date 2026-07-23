"""Durable preflight for the canonical continuation reducer.

The control reducer selects *what* an operator asked to do; this module owns
the disk facts needed before a launcher can act on that selection.  Keeping it
here prevents CLI, SDK wrappers, and transport adapters from growing subtly
different ledger or parent-artifact checks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.control.continuation import (
    ContinuationRequest,
    ContinuationResolution,
    resolve_continuation,
)
from pipeline.verification_ledger_store import LedgerStoreError, load_ledger


@dataclass(frozen=True, slots=True)
class ContinuationPreflight:
    """Resolved parent facts and the single permitted continuation operation."""

    resolution: ContinuationResolution
    parent_run_dir: Path
    parent_meta: dict[str, Any] | None


def read_continuation_meta(run_dir: Path) -> dict[str, Any] | None:
    """Read parent metadata without leaking JSON/IO exceptions to operators."""
    try:
        value = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def preflight_continuation(
    request: ContinuationRequest,
    *,
    parent_run_dir: Path,
    meta: dict[str, Any] | None = None,
) -> ContinuationPreflight:
    """Resolve one operation and enforce finalized-ledger safety before spawn.

    A malformed ledger is a durable-state blocker, never a reason to guess.
    A finalized ledger only prevents a same-run checkpoint resume: child
    follow-up and from-run-plan launches use fresh run directories and must not
    inherit the parent's finalized execution record.
    """
    resolved_meta = (
        meta if isinstance(meta, dict) else read_continuation_meta(parent_run_dir)
    )
    resolution = resolve_continuation(
        request,
        meta=resolved_meta,
        parent_run_dir=parent_run_dir,
        allow_paused_checkpoint=_paused_handoff_has_decision(
            request.run_id, parent_run_dir, resolved_meta,
        ),
    )
    if resolution.operation == "resume_checkpoint":
        ledger_file = parent_run_dir / "scheduled_gate_ledger.json"
        if ledger_file.exists():
            try:
                ledger = load_ledger(parent_run_dir)
            except LedgerStoreError as exc:
                resolution = ContinuationResolution(
                    request, resolution.decision, "blocked",
                    f"scheduled-gate ledger is unreadable: {exc}",
                )
            else:
                if ledger.finalized:
                    resolution = ContinuationResolution(
                        request, resolution.decision, "blocked",
                        "same-run resume is blocked: parent has a finalized scheduled-gate ledger",
                    )
    return ContinuationPreflight(resolution, parent_run_dir, resolved_meta)


def _paused_handoff_has_decision(
    run_id: str, run_dir: Path, meta: dict[str, Any] | None,
) -> bool:
    """Permit a paused checkpoint only after its matching decision exists."""
    if not isinstance(meta, dict) or meta.get("status") != "awaiting_phase_handoff":
        return False
    active = meta.get("phase_handoff")
    handoff_id = active.get("id") if isinstance(active, dict) else None
    if not isinstance(handoff_id, str) or not handoff_id:
        return False
    from sdk.errors import InvalidPhaseHandoffState
    from sdk.phase_handoff import load_phase_handoff_decision

    try:
        return load_phase_handoff_decision(
            run_id, handoff_id, runs_dir=run_dir.parent, cwd=None,
        ) is not None
    except (InvalidPhaseHandoffState, OSError, ValueError):
        return False


__all__ = [
    "ContinuationPreflight", "preflight_continuation", "read_continuation_meta",
]
