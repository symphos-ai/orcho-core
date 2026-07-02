"""Client-neutral run-control value types (Stage 4 read/command model).

These shapes are the typed surface a headless client (CLI, MCP adapter,
web projection, terminal UI) reads to observe a run and to express an
operator decision. They are pure data: ``frozen=True, slots=True`` and
round-trip cleanly through :func:`sdk._jsonable.to_jsonable`.

Discipline:

- This package reads durable artifacts; it never prints, renders, or
  imports a terminal layer.
- ``RunEvent`` is re-exported from :mod:`sdk.types`; we do not define a
  parallel event type.
- ``available_actions`` on :class:`PendingOperatorAction` is the single
  sanctioned source of handoff verbs (verbatim from the runtime-produced
  payload, same contract as :mod:`sdk.phase_handoff`). ``kind`` and
  ``handoff_kind`` come from explicit fields, never inferred from an id
  prefix.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sdk.phase_handoff import PhaseHandoffActionValue
from sdk.runs import _CWD_DEFAULT
from sdk.types import PhaseStatus, RunEvent

__all__ = [
    "CancelCommand",
    "DeliveryDecisionActionValue",
    "DeliveryDecisionCommand",
    "DeliveryDecisionResult",
    "DeliveryDecisionState",
    "DeliveryPrIntent",
    "PendingOperatorAction",
    "PhaseHandoffActionValue",
    "PhaseHandoffDecisionCommand",
    "RecoveryLineage",
    "ResumeCommand",
    "RunControlUnsupported",
    "RunDiagnosis",
    "RuntimeOverride",
    "RunEvent",
    "RunSnapshot",
]

DeliveryDecisionActionValue = Literal["approve", "apply", "fix", "skip", "halt"]


@dataclass(frozen=True, slots=True)
class PendingOperatorAction:
    """A single point where a run is waiting on an operator.

    Covers two forms, discriminated by ``kind``:

    - ``"phase_handoff"`` — the run is paused on a phase handoff. For
      single-project runs this is the ``meta.phase_handoff`` payload;
      for cross runs the cross checkpoint discriminates further via
      ``handoff_kind`` (``"plan"`` / ``"project"`` / ``"cfa"``).
      ``available_actions`` carries the runtime-produced handoff verbs
      verbatim — the only sanctioned source of allowed actions.
    - ``"gate"`` — the run is paused on a gate. Its ``choices`` /
      ``on_skip`` semantics live in ``raw`` (gate decisions go through
      ``core.resolve_gate_decision``, not the phase-handoff decide API),
      so ``available_actions`` is an empty tuple for gates. A typed
      gate command is intentionally out of scope for the first half of
      Stage 4; the pending gate stays observable here.

    ``kind`` and ``handoff_kind`` are plain ``str`` for forward
    compatibility and are taken from explicit payload fields (e.g. the
    cross checkpoint's ``phase_handoff_kind``), never inferred from the
    shape or prefix of ``handoff_id``. ``raw`` preserves the originating
    payload so no field is dropped.
    """

    run_id: str
    kind: str  # 'phase_handoff' | 'gate' (plain str for forward-compat)
    handoff_kind: str | None = None  # 'plan' | 'project' | 'cfa' | None
    handoff_id: str | None = None
    phase: str | None = None
    project_alias: str | None = None  # cross kind='project'
    available_actions: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Focused, client-neutral control projection of one run.

    This is deliberately *not* a full :class:`sdk.types.RunStatus`
    re-implementation: it carries only what a run-control client needs to
    observe lifecycle and pending operator state, reusing
    :class:`sdk.types.PhaseStatus` for sub-run rows. ``raw_meta`` is a
    full-fidelity escape hatch over ``meta.json`` so clients can reach
    fields the snapshot has not promoted.
    """

    run_id: str
    run_dir: Path
    status: str | None
    task: str
    project: str | None = None
    profile: str | None = None
    phases: tuple[str, ...] = ()
    sub_runs: tuple[PhaseStatus, ...] = ()
    worktree: dict[str, Any] | None = None
    pending_action: PendingOperatorAction | None = None
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PhaseHandoffDecisionCommand:
    """Pure command DTO for an operator phase-handoff decision.

    This DTO only *describes* a decision; it does not execute it and never
    writes to disk. :meth:`to_decide_kwargs` adapts it to the keyword
    arguments of :func:`sdk.phase_handoff.phase_handoff_decide`, which is
    the sole executor. A gate command is intentionally out of scope here:
    gate decisions resolve through ``core.resolve_gate_decision`` (run /
    skip choices) and do not reduce to ``phase_handoff_decide``.
    """

    run_id: str
    handoff_id: str
    action: PhaseHandoffActionValue
    feedback: str | None = None
    note: str | None = None

    def to_decide_kwargs(self) -> dict[str, Any]:
        """Return kwargs for :func:`sdk.phase_handoff.phase_handoff_decide`."""
        return {
            "run_id": self.run_id,
            "handoff_id": self.handoff_id,
            "action": self.action,
            "feedback": self.feedback,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class RuntimeOverride:
    """Operator-chosen per-phase runtime/model replacement (ADR 0101).

    Names the single failed ``phase`` and the configured replacement
    ``(runtime, model)`` pair the operator selected from the durable recovery
    record. Carried on :class:`ResumeCommand`; :class:`RunService` persists it
    into ``meta.json`` before resume and the pipeline re-applies it to exactly
    that phase. The validated payload shape is the resume arg contract:
    ``orcho_run_resume(run_id, runtime_override={phase, runtime, model})``.
    """

    phase: str
    runtime: str
    model: str


@dataclass(frozen=True, slots=True)
class ResumeCommand:
    """Pure command DTO describing a single-project resume request.

    ``run_id`` is the parent run id to resume; ``None`` and the literal
    ``"latest"`` are equivalent and select the newest project run before
    any filesystem access. ``task`` upgrades the resume to a follow-up
    (a new run that carries the parent as context); when omitted the
    resume is a checkpoint continuation. ``project`` is an explicit
    project path / alias — ``None`` restores the project from the
    parent's ``meta.json``.

    ``workspace`` / ``runs_dir`` / ``cwd`` are the run-discovery context
    forwarded verbatim to :func:`sdk.runs.find_runs_dir`. ``cwd`` shares
    the :data:`sdk.runs._CWD_DEFAULT` sentinel: the default enables
    walk-up, an explicit ``None`` disables it, so an embedder can resolve
    strictly from ``workspace`` / ``runs_dir`` with no ambient leak.

    This DTO only describes the request; :class:`RunService` executes it
    by reusing the resume-context helpers and delegating to
    ``run_project_pipeline``. Cross-run resume is out of this slice.
    """

    run_id: str | None = None
    task: str | None = None
    project: str | None = None
    max_rounds: int | None = None
    model: str | None = None
    profile_name: str | None = None
    output_dir: Path | None = None
    output_run_id: str | None = None
    workspace: Path | str | None = None
    runs_dir: Path | str | None = None
    cwd: Path | str | None | object = _CWD_DEFAULT
    # ADR 0101 / T2: optional operator runtime/model override. When set,
    # :meth:`RunService.resume` validates + persists it into the run's
    # ``meta.json`` BEFORE building the ``ProjectRunRequest``, and the
    # pipeline re-applies it to the named phase on resume. ``None`` keeps a
    # plain resume byte-identical. Additive frozen-DTO field (default None) —
    # existing constructors are unaffected.
    runtime_override: RuntimeOverride | None = None


@dataclass(frozen=True, slots=True)
class DeliveryDecisionCommand:
    """Pure command DTO for an operator post-release delivery decision.

    Describes one decision on a parked delivery / correction gate; it does
    not execute it. :meth:`RunService.decide_delivery` is the sole executor,
    delegating to :func:`sdk.run_control.delivery.decide_delivery`. ``action``
    is one of ``approve`` / ``apply`` / ``fix`` / ``skip`` / ``halt``; ``note``
    is an optional free-text operator annotation.
    """

    run_id: str
    action: DeliveryDecisionActionValue
    note: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryPrIntent:
    """Provider-neutral pull-request intent for a published delivery branch (ADR 0119).

    Emitted when delivery publishes a branch instead of committing onto the
    target checkout: ``branch`` is the published/publishable delivery branch,
    ``base`` the repository's default branch, ``title`` is lifted from the
    release summary, and ``suggested_command`` is a plain-``git`` command. Core
    records the intent only — it never pushes or opens a pull request; a
    git-provider plugin owns that step.
    """

    branch: str
    base: str
    title: str
    suggested_command: str


@dataclass(frozen=True, slots=True)
class DeliveryDecisionResult:
    """Typed outcome of a post-release delivery decision (ADR 0100).

    Field names are the core→client contract. ``accepted`` is ``True`` only
    when the requested ``action`` was valid for the gate and executed;
    otherwise ``blocker`` names the typed refusal reason and the run is left
    untouched. ``status`` is the resulting :data:`CommitDeliveryStatus`.

    ``terminal_outcome`` is STRICTLY the run's resulting terminal status —
    ``'done'`` (delivered / skipped) or ``'halted'`` (halt / fix / blocked /
    still parked) — and nothing else. The 'correction marked' state (an
    accepted ``fix`` that did not start a follow-up) is NOT encoded here: it is
    expressed by the combination ``status='fix_requested'`` +
    ``halt_reason='commit_decision_fix'`` + ``followup_run_id=None``.

    ``followup_run_id`` is the id of a correction follow-up run started by an
    accepted ``fix`` — ``None`` when ``fix`` only marked the run
    correction-ready (the SDK never starts the follow-up synchronously).
    """

    run_id: str
    action: str
    accepted: bool
    status: str
    terminal_outcome: Literal["done", "halted"]
    halt_reason: str | None = None
    artifact_paths: tuple[str, ...] = ()
    commit_sha: str | None = None
    # ADR 0119 — additive delivery-branch projection. ``delivery_branch`` is the
    # published/publishable branch and ``pr_intent`` its provider-neutral PR
    # record. Fill rule mirrors the core decision: ``commit_sha`` stays populated
    # for a commit that landed on the target checkout (``protect_default`` /
    # ``named`` / ``bypass``) and is ``None`` for a pure ``worktree_branch``
    # publish, where ``delivery_branch`` + ``pr_intent`` carry the outcome
    # instead. Both are ``None`` on a commit-onto-checkout decision.
    delivery_branch: str | None = None
    pr_intent: DeliveryPrIntent | None = None
    blocker: str | None = None
    followup_run_id: str | None = None
    # Per-alias companion files (``[alias]/rel``) implicated by a delivery-scope
    # decision. Stage C (ADR 0102) populated this for a strict-mono violation
    # (``blocker='delivery_scope_violation'``); ADR 0107 / T3 extends the
    # *semantics* — backward-compatibly, same string format — to also disclose
    # every declared companion repo's changed paths (dirty and observably
    # committed) derived from the durable plan scope. The per-repo typed state
    # (dirty / committed / planned_requirement) lives in the core-durable
    # ``multi_project_delivery`` evidence block, NOT in these strings, so the
    # MCP-visible shape (``list[str]``) is unchanged. Empty for a non-companion
    # decision.
    scope_disclosure: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliveryDecisionState:
    """Read-only projection of a parked delivery gate (ADR 0100).

    The single authoritative source a gate-aware client (MCP, UI) reads to
    learn which delivery actions are safe to offer. ``decidable`` is ``True``
    only when the run is parked on a pending delivery / correction gate.
    ``kind`` discriminates ``'delivery'`` (release approved — ship or skip) from
    ``'correction'`` (release rejected or already fix-marked) and ``'none'``
    (no gate). ``available_actions`` lists exactly the actions core considers
    safe right now; ``blocked_actions`` lists the actions a hard guard
    (rejected release, required verification) currently refuses.
    """

    run_id: str
    decidable: bool
    kind: Literal["delivery", "correction", "none"]
    available_actions: tuple[str, ...] = ()
    blocked_actions: tuple[str, ...] = ()
    default_action: str | None = None
    reason: str | None = None
    # Per-alias companion files (``[alias]/rel``) disclosed on a parked gate.
    # Stage C (ADR 0102) disclosed only the strict-mono violation siblings;
    # ADR 0107 / T3 extends the *semantics* — backward-compatibly, same string
    # format — to also surface every declared companion repo's changed paths
    # (dirty and observably committed) from the durable plan scope, so a client
    # sees the full companion file set. The per-repo typed state lives in the
    # core-durable ``multi_project_delivery`` evidence block, NOT in these
    # strings, so the MCP-visible shape (``list[str]``) is unchanged. Empty for
    # every non-companion gate.
    scope_disclosure: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryLineage:
    """Core-owned recovery-lineage read-model (ADR 0114).

    The typed mirror of MCP's ``RecoveryLineageProjection``
    (``orcho_mcp.services.run_lineage``): a single deterministic
    *composition* of the existing P0 lifecycle predicates that re-derives
    no terminal / resume / gate logic of its own. It answers, for one run,
    *what to continue and how* by walking the same five-branch priority
    ladder MCP's ``project_recovery_lineage`` walks:

    1. an active follow-up child → ``active_child_run`` /
       ``resume_active_child``;
    2. a parked delivery / correction gate → ``delivery_gate`` /
       ``delivery_decision``;
    3. a terminal-or-rejected run → ``source_run_checkpoint`` /
       ``resume_source_run``, ``plan_artifact`` /
       ``plan_artifact_continuation``, ``none`` / ``start_followup``
       (clean terminal success), or ``unknown`` / ``stop_unknown``;
    4. / 5. a non-terminal stop → ``none`` / ``None`` with the source and
       plan facts still enriched.

    ``continuation_subject`` and ``recommended_next_action`` are drawn from
    a closed vocabulary (the ``SUBJECT_*`` / ``ACTION_*`` constants), never
    free-form. ``missing_facts`` is non-empty only on the
    ``unknown`` / ``stop_unknown`` dead-end and names the durable facts that
    were absent. ``reason`` is one line assembled from persisted facts,
    never parsed from log prose.

    The remaining fields carry the resolved lineage context:
    ``recommended_run_id`` for a redirect, the ``source_*`` quartet
    (``source_run_id`` / ``source_status`` / ``source_resumable`` /
    ``source_worktree_preserved``) describing the originating run,
    ``plan_subject_available`` for a continuable plan artifact, and
    ``active_child_run_id`` for an in-flight follow-up child.
    """

    run_id: str
    is_terminal_or_rejected: bool
    continuation_subject: str
    recommended_next_action: str | None
    recommended_run_id: str | None = None
    source_run_id: str | None = None
    source_status: str | None = None
    source_resumable: bool = False
    source_worktree_preserved: bool = False
    plan_subject_available: bool = False
    active_child_run_id: str | None = None
    missing_facts: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RunDiagnosis:
    """Core-owned read-model of a run's resume situation (ADR 0114).

    A single, deterministic *composition* of the existing P0 lifecycle
    predicates — it re-derives no terminal / resumable logic of its own.
    ``condition`` is the first matching branch in the fixed priority order
    (the same order MCP's ``project_run_diagnosis`` uses):

    ``needs_decision`` → ``superseded_by_child`` → ``blocked_worktree`` →
    ``correction_followup_required`` → ``needs_delivery_decision`` →
    ``recover_via_source_run`` → ``resume_inert_terminal`` /
    ``closed_by_followup`` → ``active`` → a resumable non-terminal stop
    (the status string itself: ``halted`` / ``failed`` / ``interrupted``).

    ``continuation_subject`` is the recovery-lineage role — one of
    ``active_child_run`` / ``delivery_gate`` / ``source_run_checkpoint`` /
    ``plan_artifact`` / ``none`` / ``unknown`` — and
    ``recommended_next_action`` the typed next step a captain should take.
    A dead-end with no resolvable continuation reports
    ``continuation_subject='unknown'`` + ``recommended_next_action=
    'stop_unknown'`` and fills ``missing_facts`` with the durable facts that
    are absent, rather than blindly recommending a resume.

    The remaining fields carry branch-specific context: ``handoff_id`` /
    ``available_actions`` for a pending decision, ``delivery_gate_kind`` for a
    parked delivery / correction gate, ``recommended_run_id`` / ``source_run_id``
    for a lineage redirect, and ``blocked`` / ``block_message`` for a blocked
    follow-up worktree. ``reason`` is one line assembled from persisted facts,
    never parsed from log prose.

    ``recovery`` additively attaches the full :class:`RecoveryLineage`
    read-model (ADR 0114): the same composition, projected as the typed
    recovery-lineage parity surface; it never alters the diagnosis fields
    above.
    """

    run_id: str
    condition: str
    reason: str
    status: str | None = None
    halt_reason: str | None = None
    continuation_subject: str | None = None
    recommended_next_action: str | None = None
    recommended_run_id: str | None = None
    source_run_id: str | None = None
    missing_facts: tuple[str, ...] = ()
    handoff_id: str | None = None
    available_actions: tuple[str, ...] = ()
    delivery_gate_kind: str | None = None
    blocked: bool = False
    block_message: str | None = None
    recovery: RecoveryLineage | None = None


@dataclass(frozen=True, slots=True)
class CancelCommand:
    """Pure command DTO addressing a run to cancel.

    Core has no run supervisor, so :meth:`RunService.cancel` returns a
    typed :class:`RunControlUnsupported` rather than acting; the DTO
    exists so a future supervisor-backed driver has a stable shape.
    """

    run_id: str


@dataclass(frozen=True, slots=True)
class RunControlUnsupported:
    """Typed result for a run-control operation core cannot perform.

    Returned (never raised) for operations that are intentionally out of
    scope for the core service — ``cancel`` (no supervisor), cross-run
    resume, and checkpoint-resume of a terminal parent — so callers get a
    structured, inspectable outcome instead of a silent delegation or an
    irrelevant exception.
    """

    operation: str
    reason: str
