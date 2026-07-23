"""Suggested follow-up actions surfaced in SDK return shapes (MCP UX A1).

Every workflow-decision response carries a ``next_actions`` field — a
list of :class:`Action` records that name the tool, args, and intent
for likely follow-up calls. The LLM consuming MCP responses does not
need to remember workflow patterns from documentation; the patterns
ride in the payload.

Rules covered by :func:`compute_next_actions`:

* Run finished with a persisted ``parsed_plan.json`` and status
  ``awaiting_phase_handoff`` (the ``plan`` profile's normal stop):
  suggest ``orcho_run_start`` with ``from_run_plan=<run_id>`` so the
  caller can spawn an implementation run from this plan.
* Run paused on a phase handoff with an active payload: surface one
  action per ``available_actions`` from the handoff payload
  (``continue`` / ``retry_feedback`` / ``halt`` /
  ``continue_with_waiver``), all targeting
  ``orcho_phase_handoff_decide``. ``retry_feedback`` and
  ``continue_with_waiver`` intentionally omit ``feedback`` from ``args``
  because the caller must collect real operator text before invoking the
  decision tool.
* Run terminated with a checkpoint-resumable state (``halted``,
  ``failed``, ``interrupted``): suggest ``orcho_run_resume`` so the
  caller can pick the run back up.
* Run in a terminal-success state (``done``, ``success``): no
  suggestions — the workflow is complete.
* Anything else (``running``, missing status, unknown shapes):
  empty list. We do not invent suggestions when state is unclear.

The helper is pure: it takes a meta dict (the parsed ``meta.json``)
and a ``run_id`` and returns a tuple of :class:`Action`. State-derived,
no drift risk — callers recompute on every request rather than
persisting next_actions.

Intent text is English-only by design. Workflow semantics are global;
localization belongs to the consuming client (Claude Code's UI) if
needed. See ``orcho-mcp/docs/ux_vision.md`` §11 OQ3.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pipeline.control.continuation import ContinuationDecision
from pipeline.run_state.status_vocab import (
    RESUMABLE_TERMINAL_STATUSES,
    TERMINAL_SUCCESS_STATUSES,
)


@dataclass(frozen=True, slots=True)
class Action:
    """One suggested follow-up the caller could invoke next.

    The shape is deliberately small so it round-trips cleanly through
    MCP wire payloads:

    * ``intent`` — one-sentence human-readable description. Surfaces
      in MCP client UIs (Claude Code, Cursor) as a quick "why" so
      the operator can scan suggestions.
    * ``tool`` — the MCP tool name (``orcho_run_start``,
      ``orcho_phase_handoff_decide``, ``orcho_run_resume``). Must match
      a tool actually registered with the MCP server; the surface
      treats unknown names as soft errors (logged + dropped) rather
      than hard validation failures so a future tool rename does not
      crash existing clients holding stale ``next_actions`` from
      a polled status.
    * ``args`` — the args dict to pass to the tool. The keys must
      match the tool's input schema; values are whatever the workflow
      pattern requires (run_id, profile, action verb, etc.).
    * ``optional`` — ``True`` (default) when this action is one of
      several valid follow-ups. ``False`` is reserved for cases where
      the workflow has a single deterministic next step (e.g. a
      paused handoff with a single available action).
    """

    intent: str
    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    optional: bool = True
    kind: str = "ready_call"
    requires_operator_input: bool = False
    choices: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] | None = None
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for wire transmission.

        MCP Pydantic models accept the dict directly via Action(**dict).
        Defensive copies of ``args`` so caller mutations cannot reach
        the in-memory Action.
        """
        payload: dict[str, Any] = {
            "intent": self.intent,
            "tool": self.tool,
            "args": dict(self.args),
            "optional": self.optional,
        }
        # Preserve the established compact wire shape for ready calls that do
        # not require input. The readiness extension is additive: records that
        # carry a non-default readiness fact serialize it explicitly.
        if self.kind != "ready_call":
            payload["kind"] = self.kind
        if self.requires_operator_input:
            payload["requires_operator_input"] = True
        if self.choices:
            payload["choices"] = list(self.choices)
        if self.input_schema is not None:
            payload["input_schema"] = dict(self.input_schema)
        if self.context:
            payload["context"] = dict(self.context)
        return payload


# ── compute_next_actions ────────────────────────────────────────────────────


def compute_next_actions(
    meta: Mapping[str, Any] | None,
    *,
    run_id: str,
    live_stall_diagnostics: Sequence[Any] | None = None,
    has_parsed_plan_artifact: bool | None = None,
    status: str | None = None,
    continuation_decision: ContinuationDecision | None = None,
) -> tuple[Action, ...]:
    """Project a run's meta.json into a tuple of suggested follow-ups.

    Pure function. Reads only ``meta`` + ``run_id`` + the already-projected
    ``live_stall_diagnostics`` / ``has_parsed_plan_artifact`` / ``status`` facts
    the caller passes in; no filesystem, no clock, no env. Returns a tuple
    (immutable, hashable) so SDK return types can carry it under
    ``next_actions`` without aliasing concerns.

    ``status`` is an optional pre-resolved status override. When provided (not
    ``None``) it is used verbatim in place of ``meta['status']`` for every
    status-driven branch below — ``load_status`` passes the ADR 0104
    ``merged_status`` so a run whose terminal status lives only in the launcher
    state (empty/``running`` ``meta.status`` + an abnormal launcher exit) still
    projects the correct resumable recovery, and so the suggestions agree with
    the ``status`` ``get_errors_halt`` reports. The failure-record branches
    (provider-access, stalled-command) still read ``meta['failure']`` and are
    untouched: for those runs ``meta.status`` is already terminal, so
    ``merged_status`` returns the same value and the override is a no-op.

    Two stall sources feed recovery actions:

    * **Terminal** — a stalled-command failure persisted in
      ``meta['failure']`` (``failure_kind == 'stalled_command'``) projects a
      ``resume_from_checkpoint`` resume Action, like the provider-access path.
    * **Live non-terminal** — ``live_stall_diagnostics`` (the caller passes the
      output of ``sdk.evidence_slices.active_stall_diagnostics``, read from the
      event-store while the phase is still running) projects an ``interrupt``
      Action against the run's own subprocess group. This is bounded and
      non-empty whenever an active diagnostic exists, and never makes the run
      terminal.

    See module docstring for the full rule set. Missing or
    shape-mismatched ``meta`` returns an empty tuple — we do not
    invent suggestions when state is unclear.
    """
    if not isinstance(meta, Mapping):
        return ()

    if status is None:
        status = meta.get("status")
    if not isinstance(status, str):
        return ()

    if continuation_decision is not None and continuation_decision.continuation_subject == "retained_change":
        return (_correction_followup_action(continuation_decision),)

    # Terminal success — workflow complete, nothing to suggest.
    if status in TERMINAL_SUCCESS_STATUSES:
        return ()

    # Build the action list incrementally so multiple rules can
    # contribute (e.g. a paused handoff with a persisted plan
    # surfaces BOTH decide actions AND from-run-plan suggestion).
    out: list[Action] = []

    # Paused on a phase handoff: surface one action per available
    # decide verb. The payload's ``available_actions`` is the source
    # of truth — both ``human_feedback_always`` and
    # ``human_feedback_on_reject`` policies publish it.
    if status == "awaiting_phase_handoff":
        out.extend(_handoff_actions(meta, run_id=run_id))

    # Resumable terminal states. A terminal *provider-access* failure
    # (ADR 0101) projects a richer recovery set — retry + one runtime/model
    # replace per configured candidate — instead of the flat resume Action;
    # ``_provider_access_recovery_actions`` returns ``None`` for every other
    # resumable terminal so the flat resume Action is used unchanged. The two
    # branches are mutually exclusive, so a provider-access run never emits a
    # duplicate plain ``orcho_run_resume {run_id}``.
    if status in RESUMABLE_TERMINAL_STATUSES:
        recovery = _provider_access_recovery_actions(meta, run_id=run_id)
        if recovery is None:
            # A terminal stalled-command failure projects its own resume
            # recovery (resume_from_checkpoint). Mutually exclusive with the
            # provider-access branch, so a stalled run never also emits a
            # duplicate flat ``orcho_run_resume {run_id}``.
            recovery = _terminal_stall_recovery_actions(meta, run_id=run_id)
        if recovery is not None:
            out.extend(recovery)
        else:
            out.append(_resume_action(run_id))

    # Live event-backed non-terminal stall diagnostics. The run is still
    # ``running`` (write-through emission during a stream event), so the
    # actionable recovery is to interrupt the run's own subprocess group —
    # resume / halt are not actionable on a live run. Independent of the
    # terminal branches above: a still-running run reaches none of them.
    if status == "running":
        out.extend(
            _live_stall_recovery_actions(live_stall_diagnostics, run_id=run_id),
        )

    # If a persisted machine-readable plan exists, the operator can
    # spawn a NEW run that inherits it — independent of whether THIS
    # run is paused, halted, or otherwise non-success. ``load_status``
    # passes the physical ``parsed_plan.json`` probe so a child that
    # stamped ``plan_source='run'`` before failing in setup does not
    # advertise a false ``from_run_plan`` action.
    has_plan = (
        _has_persisted_plan(meta)
        if has_parsed_plan_artifact is None
        else has_parsed_plan_artifact
    )
    if status != "running" and has_plan:
        task = meta.get("task")
        out.append(_from_run_plan_action(run_id, task=task if isinstance(task, str) else None))

    return tuple(out)


# ── builders ────────────────────────────────────────────────────────────────


def _from_run_plan_action(run_id: str, *, task: str | None) -> Action:
    """Suggest a plan-artifact continuation run from this run's plan.

    This is the durable plan-artifact continuation path: the new run loads
    this run's persisted ``parsed_plan.json``, strips the leading
    plan/validate_plan block, and starts a fresh worktree at ``implement`` —
    it does NOT depend on this run's worktree still being present. The
    suggested ``profile`` is a semantic work kind with phases downstream of
    planning (``feature``); never a legacy profile name.
    """
    args = {"from_run_plan": run_id, "profile": "feature"}
    if task and task.strip():
        args["task"] = task
        return Action(
            intent=(
                "Start a plan artifact continuation run: inherit this run's "
                "persisted parsed plan, skip the plan/validate_plan block, and "
                "begin at implement on a fresh worktree."
            ),
            tool="orcho_run_start",
            args=args,
            optional=True,
        )
    return Action(
        intent="Provide the parent task before starting a plan artifact continuation.",
        tool="orcho_run_start",
        args={"from_run_plan": run_id, "profile": "feature"},
        optional=False,
        kind="operator_input_required",
        requires_operator_input=True,
        input_schema={"type": "object", "required": ["task"], "properties": {"task": {"type": "string", "minLength": 1}}},
    )


def _correction_followup_action(decision: ContinuationDecision) -> Action:
    """Project the sole correction interaction without a plan fallback."""
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["operator_intent"],
        "properties": {
            "operator_intent": {"type": "string", "enum": ["followup", "exit"]},
            "operator_comment": {"type": "string", "minLength": 1},
        },
        "allOf": [{"if": {"properties": {"operator_intent": {"const": "followup"}}}, "then": {"required": ["operator_comment"]}}],
    }
    return Action(
        intent=decision.reason,
        tool="orcho_run_resume",
        args={"run_id": decision.run_id},
        optional=False,
        kind="operator_input_required",
        requires_operator_input=True,
        choices=("followup", "exit"),
        input_schema=schema,
        context={"blocked": decision.blocked, "retained_worktree": decision.retained_worktree, "diff_source": decision.diff_source},
    )


def _resume_action(run_id: str, *, intent: str | None = None) -> Action:
    """Suggest resuming this run from its checkpoint.

    ``intent`` overrides the default prose — used by the provider-access
    retry projection (ADR 0101) to clarify that retry only helps after the
    operator restores provider access. The tool + ``{run_id}`` args are
    identical, so the retry projection reuses this single resume contract.
    """
    return Action(
        intent=intent or "Resume this run from its checkpoint.",
        tool="orcho_run_resume",
        args={"run_id": run_id},
        optional=True,
    )


# ── provider-access recovery projection (ADR 0101) ──────────────────────────

_PROVIDER_ACCESS_FAILURE_KIND = "provider_access"
_PROVIDER_ACCESS_RETRY_INTENT = (
    "Resume this run unchanged once provider access is restored (same "
    "runtime/model). Provider-access failures are not fixed by blind retry — "
    "retry only after access is back."
)


def _provider_access_recovery_actions(
    meta: Mapping[str, Any], *, run_id: str,
) -> tuple[Action, ...] | None:
    """Project a terminal provider-access failure into resume Actions.

    Returns ``None`` when the run did not die on a provider-access failure, so
    the caller falls back to the flat resume Action (no behavioural change for
    ordinary halted/failed/interrupted runs). For a provider-access failure it
    returns:

    * one **retry** Action — ``orcho_run_resume`` with args ``{run_id}`` (the
      same resume contract, retry-after-access-restore intent);
    * one **replace** Action per configured replacement candidate —
      ``orcho_run_resume`` with args exactly
      ``{run_id, runtime_override: {phase, runtime, model}}``, the shape
      :meth:`sdk.run_control.service.RunService.resume` accepts (T2).

    ``halt`` is intentionally NOT projected: it has no executable tool and the
    run is already terminal, so it stays a durable recovery option in
    ``meta.failure.recovery_actions`` (ADR 0101) and never reaches
    ``next_actions``. When no replacement candidate exists, only the retry
    Action is returned.

    Pure + SDK-layer-only: reads ``meta`` exclusively (the candidates were
    persisted into ``meta.failure.recovery_actions`` by the run, T1); it does
    not import the agent registry or recompute candidates.
    """
    failure = meta.get("failure")
    if not isinstance(failure, Mapping):
        return None
    if failure.get("failure_kind") != _PROVIDER_ACCESS_FAILURE_KIND:
        return None

    actions: list[Action] = [
        _resume_action(run_id, intent=_PROVIDER_ACCESS_RETRY_INTENT),
    ]

    failed_phase = failure.get("failed_phase")
    recovery_actions = failure.get("recovery_actions")
    if (
        isinstance(failed_phase, str)
        and failed_phase
        and isinstance(recovery_actions, list)
    ):
        for entry in recovery_actions:
            if not isinstance(entry, Mapping) or entry.get("action") != "replace":
                continue
            runtime = entry.get("runtime")
            model = entry.get("model")
            if not (
                isinstance(runtime, str) and runtime
                and isinstance(model, str) and model
            ):
                continue
            actions.append(
                _replace_action(
                    run_id, phase=failed_phase, runtime=runtime, model=model,
                ),
            )
    return tuple(actions)


def _replace_action(
    run_id: str, *, phase: str, runtime: str, model: str,
) -> Action:
    """Suggest resuming with a per-phase runtime/model override.

    ``args`` is exactly ``{run_id, runtime_override: {phase, runtime, model}}``
    — the contract :meth:`RunService.resume` validates + persists before
    building the run request (ADR 0101 / T2). ``run_id`` is mandatory so the
    override addresses this specific run, never a default-latest one.
    """
    return Action(
        intent=(
            f"Switch the {phase!r} phase to runtime {runtime!r} (model "
            f"{model!r}) and resume — use when provider access cannot be "
            "restored on the original runtime."
        ),
        tool="orcho_run_resume",
        args={
            "run_id": run_id,
            "runtime_override": {
                "phase": phase,
                "runtime": runtime,
                "model": model,
            },
        },
        optional=True,
    )


# ── stalled-command recovery projection (dual source) ───────────────────────

# Local literal (mirrors pipeline.run_state.stalled_command — actions.py stays
# dependency-light and pure, exactly as ``_PROVIDER_ACCESS_FAILURE_KIND`` does).
_STALLED_COMMAND_FAILURE_KIND = "stalled_command"
_STALL_RESUME_INTENT = (
    "Resume this run from its checkpoint after a command stalled (idle "
    "timeout). The hung child process group was already stopped; resume "
    "re-enters the failed phase."
)
_STALL_INTERRUPT_INTENT = (
    "Interrupt this run's own subprocess group — a command is stalled (still "
    "running, flagged as a risk). This stops only the run's own child group; "
    "it does not touch unrelated processes."
)


def _terminal_stall_recovery_actions(
    meta: Mapping[str, Any], *, run_id: str,
) -> tuple[Action, ...] | None:
    """Project a terminal stalled-command failure into resume Actions.

    Returns ``None`` when the run did not die on a stalled command (so the
    caller falls back to the flat resume Action). For a stalled-command failure
    it projects the ``resume_from_checkpoint`` verb into a single
    ``orcho_run_resume`` Action; ``interrupt`` and ``halt`` are meta-only on a
    terminal run (the child group is already gone and there is no halt tool).
    Pure: reads ``meta`` only.
    """
    failure = meta.get("failure")
    if not isinstance(failure, Mapping):
        return None
    if failure.get("failure_kind") != _STALLED_COMMAND_FAILURE_KIND:
        return None
    verbs = {
        entry.get("action")
        for entry in (failure.get("recovery_actions") or [])
        if isinstance(entry, Mapping)
    }
    # ``resume_from_checkpoint`` is the actionable verb on a terminal stall;
    # fall back to a plain resume so a stalled (and therefore resumable) run is
    # never left without a follow-up.
    intent = _STALL_RESUME_INTENT if "resume_from_checkpoint" in verbs else None
    return (_resume_action(run_id, intent=intent),)


def _live_stall_recovery_actions(
    diagnostics: Sequence[Any] | None, *, run_id: str,
) -> tuple[Action, ...]:
    """Project live non-terminal stall diagnostics into Actions.

    Each diagnostic carries the bounded recovery verb set; on a still-running
    run only ``interrupt`` is actionable, so the projection collapses to a
    single ``orcho_run_cancel`` Action whenever any active diagnostic offers
    it. Bounded and non-empty in the presence of a diagnostic; an empty /
    ``None`` input yields an empty tuple (no diagnostics → nothing to suggest).
    """
    if not diagnostics:
        return ()
    verbs: set[str] = set()
    for diag in diagnostics:
        verbs.update(getattr(diag, "recovery_actions", ()) or ())
    if "interrupt" in verbs:
        return (
            Action(
                intent=_STALL_INTERRUPT_INTENT,
                tool="orcho_run_cancel",
                args={"run_id": run_id},
                optional=True,
            ),
        )
    return ()


def _handoff_actions(
    meta: Mapping[str, Any], *, run_id: str,
) -> tuple[Action, ...]:
    """Project the active phase-handoff payload into Action records.

    Reads ``meta["phase_handoff"]`` (the persisted active payload).
    Each entry in its ``available_actions`` list becomes an Action
    targeting ``orcho_phase_handoff_decide``. Required handoff_id
    and the action verb are baked into the args. ``retry_feedback`` and
    ``continue_with_waiver`` deliberately do not include a ``feedback``
    key: callers must collect real operator text and add it before
    invoking the decision tool.
    """
    payload = meta.get("phase_handoff")
    if not isinstance(payload, Mapping):
        return ()

    handoff_id = payload.get("id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return ()

    raw_actions = payload.get("available_actions") or []
    if not isinstance(raw_actions, list):
        return ()

    out: list[Action] = []
    for verb in raw_actions:
        if not isinstance(verb, str):
            continue
        intent = _handoff_intent(verb)
        if intent is None:
            # Unknown verb — skip silently. A future verb that the SDK
            # has not learned about yet should not crash the client.
            continue
        args: dict[str, Any] = {
            "run_id": run_id,
            "handoff_id": handoff_id,
            "action": verb,
        }
        out.append(
            Action(
                intent=intent,
                tool="orcho_phase_handoff_decide",
                args=args,
                optional=True,
            ),
        )
    return tuple(out)


def _handoff_intent(verb: str) -> str | None:
    """Map a handoff action verb to its human-readable intent."""
    return {
        "continue": (
            "Accept the current verdict and continue the run as-is."
        ),
        "retry_feedback": (
            "Reject the verdict and trigger one extra plan round with "
            "operator-supplied feedback."
        ),
        "halt": (
            "Terminate the run; no further phases will execute."
        ),
        "continue_with_waiver": (
            "Continue past the rejected verdict with a durable operator "
            "waiver; the waived findings are injected into downstream "
            "review gates so they are not reopened as blocking. Requires "
            "an operator verdict."
        ),
    }.get(verb)


def _has_persisted_plan(meta: Mapping[str, Any]) -> bool:
    """True when the run produced a durable ``parsed_plan.json``.

    The plan-phase writer stamps ``meta["plan_source"]`` whenever it
    persists the artefact (see ``pipeline.project_orchestrator``).
    Default ``"local"`` means the run produced its own plan;
    ``"run"`` means the plan was inherited via ``--from-run-plan``;
    ``"cross"`` means it came from a cross-project bundle. All three
    carry a persisted artefact suitable for follow-up reuse.
    ``"none"`` (explicit no-plan profiles) is the negative case.
    """
    plan_source = meta.get("plan_source")
    if not isinstance(plan_source, str):
        return False
    return plan_source in {"local", "run", "cross"}


__all__ = [
    "Action",
    "compute_next_actions",
]
