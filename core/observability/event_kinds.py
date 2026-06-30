"""core.observability.event_kinds — REA-2 typed event vocabulary.

The single source of truth for what kinds of events the orcho pipeline
emits and what payload fields are required for each kind. Replaces the
ad-hoc string literals scattered across phase handlers, runtimes, and
gates.

REA-2 invariants:

* **Stable vocabulary.** New event kinds land here first; one-off kinds
  scattered across modules drift by definition. ``EventKind`` is a
  ``StrEnum`` so emitters can keep passing strings (no signature
  churn) and the value column doubles as the on-disk wire shape.
* **Typed payloads.** Each kind declares its required-fields contract
  via :data:`REQUIRED_PAYLOAD_KEYS`. :func:`validate_payload` checks an
  emitted payload against the contract; the schema test in
  ``tests/unit/test_event_kinds.py`` enforces full coverage so a
  regression that drops a required field fails loudly.
* **Naming**: ``<domain>.<event>`` snake_case. Lifecycle pairs use
  ``.start`` / ``.end`` (matching the existing ``phase.start`` /
  ``phase.end`` convention rather than the past-participle alternative
  the REA roadmap floated — verb-tense rename has zero functional
  value and a 20+ file blast radius across orcho-core / orcho-mcp /
  orcho-web).

Out of scope for REA-2 v1:

* ``review.finding`` — emitted as a structured record per finding;
  blocked on the review_changes handler emitting structured findings (REA-3).
* ``file.changed`` — needs the diff-capture path REA-3 builds for
  the evidence bundle.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    """Every event kind emitted by orcho.

    The string value IS the on-disk ``kind`` column; never rename a
    value without bumping schema_version on the evidence bundle (REA-3).
    """

    # ── Run lifecycle ───────────────────────────────────────────────
    RUN_START = "run.start"
    RUN_END = "run.end"

    # ── Phase lifecycle ────────────────────────────────────────────
    PHASE_START = "phase.start"
    PHASE_END = "phase.end"

    # ── Agent invocations ──────────────────────────────────────────
    AGENT_START = "agent.start"
    AGENT_END = "agent.end"
    AGENT_TEXT = "agent.text"           # streamed stdout chunk (claude jsonl)
    AGENT_SKILL_USE = "agent.skill_use" # detected registered skill selection
    AGENT_CONTRACT_READY = "agent.contract_ready"  # JSON contract output ready
    AGENT_TOOL_USE = "agent.tool_use"   # built-in tool invocation
    AGENT_MCP_TOOL_CALL = "agent.mcp_tool_call"  # MCP tool invocation
    AGENT_SUMMARY = "agent.summary"     # claude run summary
    AGENT_GUARDRAIL = "agent.guardrail" # runtime safety guard fired
    AGENT_COMMAND_STALLED = "agent.command_stalled"
    """A command an agent is running stopped making progress. The
    ``terminal`` flag discriminates the two paths that emit it: a
    *terminal* hang (idle-timeout escalated to
    ``AgentCommandStalledError`` and a failed run) versus a *non-terminal*
    risk signal (e.g. unsafe free-text process polling) recorded live
    while the phase is still running — the latter never fails the run.
    ``reason`` is one of the ``StallReason`` values; ``elapsed_s`` is the
    stall duration. Optional ``command_preview`` / ``output_tail`` /
    ``process_group`` carry the bounded diagnostic detail."""

    # ── Hypothesis loop ────────────────────────────────────────────
    HYPOTHESIS_PROPOSED = "hypothesis.proposed"
    HYPOTHESIS_VERDICT = "hypothesis.verdict"
    HYPOTHESIS_EXHAUSTED = "hypothesis.exhausted"

    # ── Plan validation gate ───────────────────────────────────────
    VALIDATE_PLAN_VERDICT = "validate_plan.verdict"

    # ── Generic phase handoff ──────────────────────────────────────
    PHASE_HANDOFF_REQUESTED = "phase.handoff_requested"
    """Generic phase-level handoff pause request. Emitted by the
    orchestrator when a phase declares a non-bypass handoff and the
    runtime trigger condition fires. Payload identifies the phase,
    handoff type, trigger, round, and the persisted handoff id."""

    # ── Cross-project gates (ADR 0024 / 0025) ──────────────────────
    CROSS_VALIDATE_PLAN_VERDICT = "cross_validate_plan.verdict"
    """Per-round verdict emitted by the cross runner's plan QA loop
    (``_validate_cross_plan``). Mirrors ``validate_plan.verdict`` for
    the cross-level plan."""

    CROSS_FINAL_ACCEPTANCE_VERDICT = "cross_final_acceptance.verdict"
    """ADR 0025 Phase 3: the cross runner's system release gate emits
    its verdict before ``run.end``. ``source`` discriminates the path
    that produced the verdict (``agent`` / ``precondition`` /
    ``parse_error``)."""

    # ── Cross-level commit delivery (ADR cross-delivery + CFA pause) ─
    CROSS_DELIVERY_STARTED = "cross.delivery.started"
    """Phase B: the cross runner begins delivering per-alias worktree
    diffs into the project checkouts (after CFA approved / overridden).
    ``project_count`` is the number of aliases in the delivery loop.
    Emitted strictly before ``run.end``."""

    CROSS_DELIVERY_ALIAS_COMMITTED = "cross.delivery.alias_committed"
    """Phase B: one alias reached a success-like delivery status
    (``committed`` / ``applied_uncommitted`` / ``no_diff`` /
    ``skipped`` / ``skipped_already_delivered``). ``commit_sha`` is
    present only for the ``committed`` status."""

    CROSS_DELIVERY_ALIAS_FAILED = "cross.delivery.alias_failed"
    """Phase B: one alias reached a failure-like delivery status
    (``target_dirty`` / ``commit_failed`` / ``apply_failed`` /
    ``not_applicable``) or the operator ``halted`` the loop. ``error``
    carries the human-readable reason."""

    CROSS_DELIVERY_COMPLETED = "cross.delivery.completed"
    """Phase B: the per-alias delivery loop finished. ``overall`` is
    the aggregate verdict (``ok`` / ``partial`` / ``failed`` /
    ``halted`` / ``disabled``) the finalizer maps to a terminal
    status. Emitted strictly before ``run.end``."""

    # ── Subtask DAG ────────────────────────────────────────────────
    SUBTASK_START = "subtask.start"
    SUBTASK_END = "subtask.end"
    SUBTASK_RECEIPT = "subtask.receipt"

    # ── REA-2 additions ────────────────────────────────────────────
    PLAN_PARSED = "plan.parsed"
    """Emitted by the PLAN handler after a successful ``parse_plan``.
    Payload reports the plan-contract surface so REA-3 can render
    "what the architect committed to" without re-reading the markdown."""

    GATE_START = "gate.start"
    GATE_END = "gate.end"
    """Quality-gate execution boundary. ``gate.start`` carries
    ``name`` + ``kind`` (computational/behavioral); ``gate.end``
    carries ``name`` + ``outcome`` (passed/failed/skipped) + ``duration_s``.
    Allows evidence to reconstruct the gate timeline without parsing
    handler-internal state."""

    COMMAND_START = "command.start"
    COMMAND_END = "command.end"
    """Shell-command lifecycle (test runners, linters, etc.).
    ``command.start`` carries ``argv_summary`` + ``cwd``;
    ``command.end`` carries ``exit_code`` + ``duration_s`` +
    ``outcome``. Distinct from ``agent.*`` events because commands
    are orchestrator-driven, not agent-driven."""

    ARTIFACT_CREATED = "artifact.created"
    """Emitted when orcho writes a durable artifact under the run
    dir (plan_*.md, evidence.json, etc.). Lets REA-3 enumerate the
    artifacts manifest without scanning the run dir."""


#: Per-kind required payload fields. The schema test in
#: ``tests/unit/test_event_kinds.py`` asserts every kind in
#: :class:`EventKind` has a row here so additions don't quietly slip
#: through without a contract.
REQUIRED_PAYLOAD_KEYS: dict[EventKind, frozenset[str]] = {
    # Lifecycle ─────────────────────────────────────────────────────
    EventKind.RUN_START: frozenset({"task", "run_kind"}),
    EventKind.RUN_END: frozenset({"status"}),

    EventKind.PHASE_START: frozenset({"title"}),
    EventKind.PHASE_END: frozenset({"title", "outcome"}),

    EventKind.AGENT_START: frozenset({"agent", "model"}),
    EventKind.AGENT_END: frozenset({"agent"}),
    # Streaming events are best-effort; payload shape varies by parser
    # (claude jsonl) so no required fields beyond the kind itself.
    EventKind.AGENT_TEXT: frozenset(),
    EventKind.AGENT_SKILL_USE: frozenset({"skill_name", "text"}),
    EventKind.AGENT_CONTRACT_READY: frozenset({"agent", "format"}),
    EventKind.AGENT_TOOL_USE: frozenset(),
    EventKind.AGENT_MCP_TOOL_CALL: frozenset({"server", "tool_name", "status"}),
    EventKind.AGENT_SUMMARY: frozenset(),
    EventKind.AGENT_GUARDRAIL: frozenset({"agent", "guardrail", "action"}),
    # Stalled command — ``terminal`` (bool) discriminates the idle-timeout
    # escalation path from the live non-terminal risk-flag path. Optional
    # payload keys: ``command_preview`` / ``output_tail`` / ``process_group``.
    EventKind.AGENT_COMMAND_STALLED: frozenset(
        {"phase", "reason", "elapsed_s", "terminal"}
    ),

    # Hypothesis ────────────────────────────────────────────────────
    EventKind.HYPOTHESIS_PROPOSED: frozenset({"attempt", "max", "text"}),
    EventKind.HYPOTHESIS_VERDICT: frozenset({"attempt", "approved"}),
    EventKind.HYPOTHESIS_EXHAUSTED: frozenset({"attempts", "max"}),

    # Plan validation gate ─────────────────────────────────────────
    EventKind.VALIDATE_PLAN_VERDICT: frozenset({"attempt", "approved"}),

    # Generic phase handoff ─────────────────────────────────────────
    EventKind.PHASE_HANDOFF_REQUESTED: frozenset(
        {"phase", "handoff_type", "trigger", "round", "handoff_id"}
    ),

    # Cross-project gates (ADR 0024 / 0025) ────────────────────────
    EventKind.CROSS_VALIDATE_PLAN_VERDICT: frozenset({"attempt", "approved"}),
    EventKind.CROSS_FINAL_ACCEPTANCE_VERDICT: frozenset({
        "approved",
        "verdict",
        "ship_ready",
        "source",
        "short_summary",
    }),

    # Cross-level commit delivery (ADR cross-delivery + CFA pause) ──
    EventKind.CROSS_DELIVERY_STARTED: frozenset({"project_count"}),
    EventKind.CROSS_DELIVERY_ALIAS_COMMITTED: frozenset({"alias", "status"}),
    EventKind.CROSS_DELIVERY_ALIAS_FAILED: frozenset({"alias", "status"}),
    EventKind.CROSS_DELIVERY_COMPLETED: frozenset({"overall"}),

    # Subtask DAG ───────────────────────────────────────────────────
    EventKind.SUBTASK_START: frozenset({"subtask_id", "runtime", "model"}),
    EventKind.SUBTASK_END: frozenset({"subtask_id"}),
    EventKind.SUBTASK_RECEIPT: frozenset({"subtask_id", "state"}),

    # REA-2 additions ───────────────────────────────────────────────
    EventKind.PLAN_PARSED: frozenset({
        "source",
        "short_summary",
        "planning_context",
        "subtask_count",
        "has_contract",
    }),
    # Payload field naming: ``gate_kind`` / ``command_kind`` /
    # ``artifact_kind`` instead of ``kind`` because :func:`events.emit`
    # already takes ``kind`` as its first positional argument — keyword
    # collisions raise TypeError at runtime.
    EventKind.GATE_START: frozenset({"name", "gate_kind"}),
    EventKind.GATE_END: frozenset({"name", "outcome", "duration_s"}),
    EventKind.COMMAND_START: frozenset({"argv_summary", "cwd"}),
    EventKind.COMMAND_END: frozenset({"exit_code", "duration_s", "outcome"}),
    EventKind.ARTIFACT_CREATED: frozenset({"path", "artifact_kind"}),
}


class EventSchemaError(ValueError):
    """Raised when an emitted event payload is missing required fields.

    Surfaced by :func:`validate_payload`; the schema test in
    ``tests/unit/test_event_kinds.py`` uses this to fail-fast on
    contract regressions.
    """


def _validate_run_start_payload(payload: dict[str, Any]) -> None:
    """Validate the discriminated ``run.start`` payload.

    ``run.start`` is shared by two first-class run shapes:

    * ``single_project`` runs are driven by a v2 profile and require one
      concrete ``project`` path plus the resolved ``profile`` name.
    * ``cross_project`` runs are macro-orchestrations across a project
      manifest. They require structured ``projects`` entries and record
      the cross mode plus the per-project profile used for child runs.
    """
    run_kind = payload.get("run_kind")
    if run_kind == "single_project":
        missing = [
            key for key in ("project", "profile")
            if not isinstance(payload.get(key), str) or not payload.get(key)
        ]
        if missing:
            raise EventSchemaError(
                "event 'run.start' payload for run_kind='single_project' "
                f"missing required string keys: {missing}; "
                f"got {sorted(payload.keys())}"
            )
        # REA-3.6: optional child-run linkage. When the run is spawned by a
        # cross-project orchestrator, both fields land together — having one
        # without the other is a coding bug, not partial data.
        parent = payload.get("parent_run_id")
        alias = payload.get("project_alias")
        if (parent is None) != (alias is None):
            raise EventSchemaError(
                "event 'run.start' payload for run_kind='single_project' "
                "requires parent_run_id and project_alias to be set together "
                "(or both omitted); "
                f"got parent_run_id={parent!r}, project_alias={alias!r}"
            )
        for field, value in (("parent_run_id", parent), ("project_alias", alias)):
            if value is not None and (not isinstance(value, str) or not value):
                raise EventSchemaError(
                    f"event 'run.start' payload field {field!r} must be a "
                    f"non-empty string when present; got {value!r}"
                )
        return

    if run_kind == "cross_project":
        missing = [
            key for key in ("cross_mode", "profile", "plan_source")
            if not isinstance(payload.get(key), str) or not payload.get(key)
        ]
        plan_source = payload.get("plan_source")
        if isinstance(plan_source, str) and plan_source not in {
            "local", "cross", "none",
        }:
            raise EventSchemaError(
                "event 'run.start' payload for run_kind='cross_project' "
                "requires plan_source to be one of {'local','cross','none'}; "
                f"got {plan_source!r}"
            )
        projected_profile = payload.get("projected_profile")
        if projected_profile is not None and (
            not isinstance(projected_profile, str) or not projected_profile
        ):
            raise EventSchemaError(
                "event 'run.start' payload for run_kind='cross_project' "
                "requires projected_profile to be a non-empty string when "
                f"present; got {projected_profile!r}"
            )
        cross_mode = payload.get("cross_mode")
        if isinstance(cross_mode, str) and cross_mode not in {"full", "plan"}:
            raise EventSchemaError(
                "event 'run.start' payload for run_kind='cross_project' "
                "requires cross_mode to be one of {'full', 'plan'}; "
                f"got {cross_mode!r}"
            )
        projects = payload.get("projects")
        if not isinstance(projects, list) or not projects:
            missing.append("projects")
        else:
            bad_indexes: list[int] = []
            for idx, project in enumerate(projects):
                if not isinstance(project, dict):
                    bad_indexes.append(idx)
                    continue
                alias = project.get("alias")
                path = project.get("path")
                if not isinstance(alias, str) or not alias:
                    bad_indexes.append(idx)
                    continue
                if not isinstance(path, str) or not path:
                    bad_indexes.append(idx)
            if bad_indexes:
                raise EventSchemaError(
                    "event 'run.start' payload for run_kind='cross_project' "
                    "requires projects entries shaped as "
                    "{'alias': str, 'path': str}; "
                    f"bad indexes: {bad_indexes}"
                )
        if missing:
            raise EventSchemaError(
                "event 'run.start' payload for run_kind='cross_project' "
                f"missing required keys: {sorted(set(missing))}; "
                f"got {sorted(payload.keys())}"
            )
        return

    raise EventSchemaError(
        "event 'run.start' payload requires run_kind to be one of "
        "{'single_project', 'cross_project'}; "
        f"got {run_kind!r}"
    )


def validate_payload(kind: str, payload: dict[str, Any]) -> None:
    """Validate ``payload`` against the contract for ``kind``.

    No-op for kinds outside :class:`EventKind` — orcho deliberately
    allows ad-hoc kinds for plugin-emitted events that haven't (yet)
    landed in the canonical vocabulary. The schema test enforces the
    canonical kinds; plugin extensions are advisory.

    Raises:
        :class:`EventSchemaError` when a required field is missing
        from ``payload``.
    """
    try:
        required = REQUIRED_PAYLOAD_KEYS[EventKind(kind)]
    except (KeyError, ValueError):
        return
    missing = sorted(required - payload.keys())
    if missing:
        raise EventSchemaError(
            f"event {kind!r} payload missing required keys: {missing}; "
            f"got {sorted(payload.keys())}"
        )
    if EventKind(kind) is EventKind.RUN_START:
        _validate_run_start_payload(payload)
