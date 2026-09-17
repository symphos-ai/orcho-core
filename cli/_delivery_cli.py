# SPDX-License-Identifier: Apache-2.0
"""Pure formatters for ``orcho delivery gate`` / ``orcho delivery decide``.

Every function here takes the typed SDK read-state / result (plus the
persisted gate facts the facade already loaded) and returns text, a JSON
dict, or an exit code. They never print, never touch the filesystem, and
never decide anything: which actions are available, whether a release is
blocked, and whether a decision is accepted is computed by
``sdk.run_control.delivery`` and merely displayed here. The ``cmd_delivery_*``
facades in ``cli/orcho.py`` own all I/O and error mapping. Output style
mirrors ``cli/_reconcile_delivery.py`` (2-space indent, a 60-char box rule).
"""
from __future__ import annotations

import dataclasses
from typing import Any

_SEP = "─" * 60


def _joined(values: Any) -> str:
    """Render a sequence as a comma list, ``-`` when empty."""
    items = [str(v) for v in (values or ())]
    return ", ".join(items) if items else "-"


def format_delivery_gate(
    state: Any, *, gate_facts: dict[str, Any], current_status: str | None,
) -> str:
    """Render the read-only gate report for ``orcho delivery gate``."""
    out: list[str] = ["", _SEP, f"  Delivery gate:   {state.run_id}", _SEP]
    out.append(f"  Run status:      {current_status or '?'}")
    out.append(f"  Gate kind:       {state.kind}")
    out.append(f"  Decidable:       {'yes' if state.decidable else 'no'}")
    out.append(f"  Available:       {_joined(state.available_actions)}")
    out.append(f"  Blocked:         {_joined(state.blocked_actions)}")
    out.append(f"  Default action:  {state.default_action or '-'}")
    if state.reason:
        out.append(f"  Reason:          {state.reason}")
    if state.verification_contract_declared is False:
        from pipeline.project.verification_disclosure import delivery_gate_line

        out.append(f"  Verification:    {delivery_gate_line()}")
    out.append(f"  Release verdict: {gate_facts.get('release_verdict') or '-'}")
    if state.requested_at:
        out.append(f"  Requested at:    {state.requested_at}")
    for path in state.scope_disclosure:
        out.append(f"  Companion file:  {path}")
    if gate_facts:
        out.append("")
        out.append(f"  Gate status:     {gate_facts.get('status') or '-'}")
        out.append(f"  Gate action:     {gate_facts.get('action') or '-'}")
        commit_target = gate_facts.get("commit_target") or gate_facts.get("project_path")
        if commit_target:
            out.append(f"  Checkout:        {commit_target}")
        if gate_facts.get("source_path"):
            out.append(f"  Source:          {gate_facts['source_path']}")
        if gate_facts.get("baseline_ref"):
            out.append(f"  Baseline ref:    {gate_facts['baseline_ref']}")
        for path in gate_facts.get("changed_paths") or ():
            out.append(f"  Changed:         {path}")
        for path in gate_facts.get("untracked_paths") or ():
            out.append(f"  Untracked:       {path}")
        if gate_facts.get("scope_blocker"):
            out.append(f"  Scope blocker:   {gate_facts['scope_blocker']}")
    out.append("")
    if state.decidable and state.available_actions:
        out.append(
            f"  Decide with: orcho delivery decide {state.run_id} "
            f"<{'|'.join(state.available_actions)}> [--note ...]"
        )
    elif state.kind == "none":
        out.append("  This run has no parked delivery gate; nothing to decide.")
    else:
        out.append("  The gate exists but cannot be decided right now (see Reason).")
    out.append(_SEP)
    return "\n".join(out)


def gate_exit_code(state: Any) -> int:
    """0 when the gate is decidable, 1 when there is no gate, 3 otherwise."""
    if state.decidable and state.available_actions:
        return 0
    if state.kind == "none":
        return 1
    return 3


def delivery_gate_to_json(state: Any, gate_facts: dict[str, Any]) -> dict[str, Any]:
    """The ``--json`` payload: the typed state plus the persisted gate facts."""
    payload = dataclasses.asdict(state)
    payload["gate"] = dict(gate_facts)
    return payload


def format_delivery_decision(result: Any) -> str:
    """Render the outcome of ``orcho delivery decide``."""
    out: list[str] = ["", _SEP, f"  Delivery decision: {result.run_id}", _SEP]
    out.append(f"  Accepted:        {'yes' if result.accepted else 'no'}")
    out.append(f"  Action:          {result.action}")
    out.append(f"  Gate status:     {result.status}")
    out.append(f"  Run status:      {result.terminal_outcome}")
    if result.halt_reason:
        out.append(f"  Halt reason:     {result.halt_reason}")
    if result.commit_sha:
        out.append(f"  Git commit:      {result.commit_sha}")
    if result.published_commit_sha:
        out.append(f"  Published:       {result.published_commit_sha}")
    if result.delivery_branch:
        out.append(f"  Branch:          {result.delivery_branch}")
    if result.pr_url:
        out.append(f"  Pull request:    {result.pr_url}")
    intent = result.pr_intent
    if intent is not None and getattr(intent, "suggested_command", None):
        out.append(f"  Open a PR with:  {intent.suggested_command}")
    if result.followup_run_id:
        out.append(f"  Follow-up run:   {result.followup_run_id}")
    if result.blocker:
        out.append(f"  Blocker:         {result.blocker}")
    if result.reason:
        out.append(f"  Reason:          {result.reason}")
    for path in result.artifact_paths:
        out.append(f"  Artifact:        {path}")
    for path in result.scope_disclosure:
        out.append(f"  Companion file:  {path}")
    out.append(_SEP)
    return "\n".join(out)


def delivery_decision_to_json(result: Any) -> dict[str, Any]:
    """The ``--json`` payload for a decision result."""
    return dataclasses.asdict(result)
