"""Cross-project checkpoint I/O.

The cross orchestrator persists a small JSON checkpoint per cross run that
records ``phase0_done``, per-alias ``sub_status``, and phase-handoff pause
state. Resume logic and UI surfaces consult it to decide what to skip /
re-run / proxy.

Schema (all keys optional, defaulted on read):

```
{
    "phase0_done": bool,
    "sub_status": {alias: "done" | "failed" | "running" | "awaiting_phase_handoff"},
    "phase_handoff_pending": bool,
    "phase_handoff_id": str,
    "phase_handoff_kind": "plan" | "project" | "cfa",
    "phase_handoff_project_alias": str,    # when kind == "project"
    "phase_handoff_child_id": str,         # when kind == "project"
    "cfa_paused_state": dict,              # when kind == "cfa" (verdict, findings_count, summary, source)
    "pending_gate": dict,
    ...
}
```

``phase_handoff_kind`` discriminates which resume routine handles the
decision when the cross runner re-enters with ``phase_handoff_pending``:

* ``"plan"`` (or omitted on legacy entries) — cross_plan rejection
  pause; resume routes into the planning loop.
* ``"project"`` — child sub-pipeline pause; the decision is off-band
  (child run + SDK / MCP), so the cross CLI prompt loop breaks out
  to exit 4 rather than prompting the parent operator.
* ``"cfa"`` — cross_final_acceptance REJECTED pause; resume routes
  into the new CFA gate helper (added in ADR cross-delivery+CFA-pause
  Phase A2). Cross-owned, in-process, prompted by the cross CLI.

The kind field is the dispatch authority; the id prefix
(``cross_plan:`` / ``project:`` / ``cfa:``) is informational and
must agree with the kind. Resume code MUST NOT route on the id
prefix alone (a stale checkpoint with a mis-prefixed id would then
mis-dispatch).

This is a schema docstring, not a runtime validator — readers and
writers are best-effort: a missing or corrupt file yields the empty
default and a failing write is silently dropped. The cross
orchestrator never relies on checkpoint integrity for correctness —
it is a resume-hint surface, not a source of truth.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

CROSS_CHECKPOINT_FILE = "cross_checkpoint.json"


def read_cross_checkpoint(run_dir: Path | None) -> dict:
    """Load ``cross_checkpoint.json`` from ``run_dir`` or return default.

    Returns ``{"phase0_done": False, "sub_status": {}}`` when the file is
    missing, the path is ``None``, or the payload is unreadable.
    """
    if run_dir is None:
        return {"phase0_done": False, "sub_status": {}}
    path = run_dir / CROSS_CHECKPOINT_FILE
    if not path.exists():
        return {"phase0_done": False, "sub_status": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("phase0_done", False)
            data.setdefault("sub_status", {})
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"phase0_done": False, "sub_status": {}}


def write_cross_checkpoint(run_dir: Path | None, ckpt: dict) -> None:
    """Persist ``ckpt`` to ``cross_checkpoint.json``. Best-effort."""
    if run_dir is None:
        return
    with contextlib.suppress(OSError):
        (run_dir / CROSS_CHECKPOINT_FILE).write_text(
            json.dumps(ckpt, ensure_ascii=False, indent=2), encoding="utf-8",
        )
