# SPDX-License-Identifier: Apache-2.0
"""Pure formatters for ``orcho reconcile-delivery`` (ADR 0191).

Both functions take the typed SDK read-state / result and return text. They
never print, never write files, and never raise on the normal paths — the
``cmd_reconcile_delivery`` facade owns all I/O and error mapping. Output style
mirrors ``cli/_repair_state.py`` (2-space indent, a 60-char box rule).
"""
from __future__ import annotations

from typing import Any

_SEP = "─" * 60


def format_reconcile_state(state: Any, *, current_status: str | None) -> str:
    """Render the read-only reconciliation report (the dry-run default)."""
    out: list[str] = ["", _SEP, f"  Delivery reconciliation:  {state.run_id}", _SEP]
    out.append(f"  Run status:      {current_status or '?'}")
    out.append(f"  Reconciliation:  {state.state}")
    out.append(f"  Ledger stage:    {state.ledger_stage or '-'}")
    out.append(
        f"  Recorded:        {state.recorded_status or '-'}"
        + (f" ({state.recorded_sha[:12]})" if state.recorded_sha else "")
    )
    if state.commit_sha:
        out.append(f"  Git commit:      {state.commit_sha}")
    if state.commit_target:
        out.append(f"  Checkout:        {state.commit_target}")
    commit = state.commit or {}
    if commit:
        out.append(f"  Subject:         {commit.get('subject') or ''}")
        out.append(f"  Author:          {commit.get('author') or ''}")
        out.append(f"  Committed at:    {commit.get('committed_at') or ''}")
        parents = commit.get("parents") or []
        out.append(f"  Parents:         {', '.join(p[:12] for p in parents) or '-'}")
        out.append(f"  Files:           {commit.get('files', 0)}")
    if state.detail:
        out.append(f"  Detail:          {state.detail}")
    out.append("")
    if state.consistent:
        out.append("  The run's delivery record already agrees with Git; nothing to record.")
    else:
        out.append(
            "  Git carries a delivery commit this run does not record. Verify the"
        )
        out.append(
            "  commit above, then record it with --apply --commit <sha>"
            " (add --note to explain)."
        )
    out.append(_SEP)
    return "\n".join(out)


def format_reconcile_result(result: Any) -> str:
    """Render the outcome of an ``--apply`` run."""
    out: list[str] = ["", _SEP, f"  Delivery reconciliation:  {result.run_id}", _SEP]
    out.append(f"  Accepted:        {'yes' if result.accepted else 'no'}")
    out.append(f"  Reconciliation:  {result.state}")
    if result.commit_sha:
        out.append(f"  Git commit:      {result.commit_sha}")
    if result.blocker:
        out.append(f"  Blocker:         {result.blocker}")
    if result.reason:
        out.append(f"  Reason:          {result.reason}")
    if result.artifact_path:
        out.append(f"  Audit artifact:  {result.artifact_path}")
    if result.terminal_outcome:
        out.append(f"  Run status:      {result.terminal_outcome}")
    if result.release_verdict:
        out.append(f"  Release verdict: {result.release_verdict}")
    for note in result.notes:
        out.append(f"  Note:            {note}")
    out.append(_SEP)
    return "\n".join(out)
