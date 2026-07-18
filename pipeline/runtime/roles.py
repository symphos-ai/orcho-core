"""
pipeline/runtime/roles.py — agent role, execution mode, and policy enums.

All ``StrEnum`` types the runtime / profile loader / dispatcher need.
They split into three groups:

- **Dispatch.** ``ExecutionMode`` picks the executor strategy for a phase.
  ``AgentRole`` remains only for older cross-project planning types and the
  typed agent protocol wrappers; runtime selection is per phase.
- **Policy.** ``EffortLevel``, ``GateKind``, ``FailStrategy``,
  ``ReviewTiming``, ``HumanAction``, ``PhaseHandoffType``,
  ``PhaseHandoffAction`` — quality-gate cost / failure strategy,
  human-review timing and verdicts, phase-level human handoff regime
  and decision actions.
- **Profile typology.** ``ProfileKind``, ``FullCycleDepth``,
  ``ScopedTarget``, ``AttachmentKind``, ``ChangeHandoffMode`` —
  classifications used by ``Profile`` / ``Attachment`` / authoring
  handoff strategy.

Pure data: no I/O, no runtime/state imports.
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionMode(StrEnum):
    """Per-phase execution strategy. Open string at PhaseStep level — plugin
    extension via ``orcho.execution_modes`` entry_points (Phase 2).
    """
    LINEAR = "linear"


class ImplementationExecution(StrEnum):
    """How the built-in implement phase consumes a parsed plan.

    Policy-owned implement delivery, selected via
    ``pipeline.implementation_execution`` — independent of the per-phase
    ``ExecutionMode`` (which only ships ``linear`` plus plugin-registered modes).
    """
    WHOLE_PLAN = "whole_plan"
    SUBTASK_DAG = "subtask_dag"


class AgentRole(StrEnum):
    """Legacy behaviour-intent slot.

    Runtime selection no longer flows through this enum; use
    ``AppConfig.phases[phase].provider`` for runtime selection and
    ``PromptSpec.role`` for prompt persona selection.
    """
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"


class SessionInvocationRole(StrEnum):
    """The *kind of work* an agent invocation performs, for session policy.

    This is the single, explicit source of invocation-role identity that the
    session-disposition policy keys on (see
    :mod:`pipeline.runtime.session_disposition`). It is a *new, separate*
    taxonomy from the legacy :class:`AgentRole`: ``AgentRole`` is the historic
    behaviour-intent slot used by cross-project planning types and the typed
    agent protocol wrappers, and it is deliberately **not** reused for session
    continuation decisions — collapsing the two would re-introduce the
    ambiguity this enum removes (e.g. a ``reviewer`` invocation vs a developer
    repair invocation need different session dispositions).

    The member set covers every invocation shape the pipeline drives, so the
    disposition projection can stay total over a closed vocabulary:

    - ``implement`` / ``repair`` — the *edit-shaped* writers. These are the
      only roles the policy may continue (and only for a same-write-zone
      follow-on).
    - ``plan`` / ``validate_plan`` / ``review`` — planning and reviewing
      invocations. Always fresh; round 2+ carries a compact handoff instead
      of resuming.
    - ``companion`` — a companion subtask invocation. Always fresh.
    - ``format_repair`` — the one-shot contract re-emit
      (:mod:`pipeline.phases.review_contract_recovery`). Always fresh: the
      prior output is embedded in the repair prompt, so resume is unnecessary.
    - ``audit`` / ``verification`` / ``boundary`` — auxiliary read-shaped
      invocations included so the policy table is exhaustive over the role
      vocabulary. Always fresh.

    Pure data: no I/O, no runtime/state imports.
    """
    IMPLEMENT = "implement"
    REPAIR = "repair"
    PLAN = "plan"
    VALIDATE_PLAN = "validate_plan"
    REVIEW = "review"
    COMPANION = "companion"
    FORMAT_REPAIR = "format_repair"
    AUDIT = "audit"
    VERIFICATION = "verification"
    BOUNDARY = "boundary"


class SessionContinuity(StrEnum):
    """Declarative per-phase session-continuity policy.

    A profile step declares one of these on its execution policy; the
    session-disposition projection (:mod:`pipeline.runtime.session_disposition`)
    consumes the resolved member and maps it onto whether an invocation
    continues the prior provider session or starts fresh. This replaces the
    old hard-coded role-partition tables: continuity is now data on the
    profile, not a frozenset in code.

    - ``fresh_only`` — always start a new provider session, regardless of any
      follow-on signal. Round 2+ carries a compact handoff instead of
      resuming (e.g. ``review``).
    - ``loop_continue`` — continue the prior session on a loop follow-on
      (round 2+ of the same planning/reviewing loop), else fresh (e.g.
      ``plan`` / ``validate_plan``).
    - ``same_zone_continue`` — continue the prior session only for a
      same-write-zone follow-on, else fresh (e.g. ``implement`` /
      ``repair_changes``).

    Pure data: no I/O, no runtime/state imports.
    """
    FRESH_ONLY = "fresh_only"
    LOOP_CONTINUE = "loop_continue"
    SAME_ZONE_CONTINUE = "same_zone_continue"


class EffortLevel(StrEnum):
    """Reasoning-sandwich override knob (per harness engineering)."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class GateKind(StrEnum):
    """Quality-gate cost / scheduling distinction (per Fowler's harness analysis)."""
    COMPUTATIONAL = "computational"  # exit-code gates (tests, lint, compile)
    INFERENTIAL = "inferential"      # LLM judges (security review, spec compat)


class FailStrategy(StrEnum):
    """Quality-gate failure policy."""
    HALT = "halt"
    FEED_INTO_NEXT = "feed_into_next"
    TRIGGER_REPLAN = "trigger_replan"
    INFORMATIONAL = "informational"


class ReviewTiming(StrEnum):
    """When a human review checkpoint fires relative to phase handler."""
    BEFORE = "before"
    AFTER = "after"


class HumanAction(StrEnum):
    """Human-in-the-loop verdict at a review checkpoint (Phase 8)."""
    APPROVE = "approve"
    HALT = "halt"
    RETRY = "retry"
    REPROMPT = "reprompt"
    EDIT = "edit"
    SKIP = "skip"


class AttachmentKind(StrEnum):
    """Prompt-context attachment routing kind (Phase 4.5)."""
    TEXT = "text"      # inline in prompt as XML-block
    IMAGE = "image"    # multimodal API
    BINARY = "binary"  # path passthrough


class ProfileKind(StrEnum):
    """Two-axis profile typology (kind × variant)."""
    FULL_CYCLE = "full_cycle"
    SCOPED = "scoped"
    CUSTOM = "custom"


class FullCycleDepth(StrEnum):
    """Variant for kind=FULL_CYCLE: depth of dev cycle."""
    LITE = "lite"
    ADVANCED = "advanced"
    ENTERPRISE = "enterprise"


class ScopedTarget(StrEnum):
    """Variant for kind=SCOPED: target of partial workflow."""
    PLAN = "plan"
    REVIEW = "review"
    TASK = "task"


class ChangeHandoffMode(StrEnum):
    """How authoring phases hand code changes to review_changes / repair_changes / final_acceptance."""
    UNCOMMITTED = "uncommitted"
    COMMIT = "commit"
    COMMIT_SET = "commit_set"


class PhaseHandoffType(StrEnum):
    """Phase-level human handoff policy.

    Declared per-``PhaseStep`` via ``PhaseStep.handoff``. The loader accepts
    any value defined here on any phase; concrete runner support may be
    narrower than the schema. Executors that do not yet support a policy
    on a given phase reject it at execution time, not at load time.

    HUMAN_BYPASS — never paused for human input (default; equivalent to
        omitting the field).
    HUMAN_FEEDBACK_ON_REJECT — pause for human only when a verdict is
        rejected on the final automatic loop round.
    HUMAN_FEEDBACK_ALWAYS — pause for human on every verdict (approved or
        rejected); available actions depend on the verdict.
    """
    HUMAN_BYPASS = "human_bypass"
    HUMAN_FEEDBACK_ON_REJECT = "human_feedback_on_reject"
    HUMAN_FEEDBACK_ALWAYS = "human_feedback_always"


class PhaseHandoffAction(StrEnum):
    """Action a human can take on an active phase handoff.

    Action availability is runtime-produced (varies by ``PhaseHandoffType``
    and verdict) and is **not** part of the profile schema. The decision
    API validates the chosen action against the active handoff's
    ``available_actions`` payload.

    CONTINUE — proceed past the paused phase as a manual override. The
        machine verdict is preserved (not rewritten to approved); a
        ``phase_handoff_override`` marker records the override.
    RETRY_FEEDBACK — inject feedback and run exactly one extra
        human-directed loop round; ``LoopStep.max_rounds`` is not mutated.
    HALT — finalise the run as halted.
    CONTINUE_WITH_WAIVER — proceed past the paused phase like CONTINUE
        (machine verdict stays REJECTED, no extra reviewer round), but
        require a non-empty operator verdict and durably record a waiver.
        The waiver is authoritatively injected into all downstream review
        gates so the waived findings are not reopened as blocking.
    """
    CONTINUE = "continue"
    RETRY_FEEDBACK = "retry_feedback"
    HALT = "halt"
    CONTINUE_WITH_WAIVER = "continue_with_waiver"


class ScopeExpansionSanction(StrEnum):
    """Routing outcome for an out-of-plan scope expansion / participant-add.

    The closed set of sanctions the scope-expansion sanction projection
    (:mod:`pipeline.runtime.scope_expansion_sanction`) can choose. It encodes
    the §5 routing matrix of ADR 0112 — *what to do* about a classified
    out-of-plan change — and is kept strictly separate from the ADR 0110
    classifier's *what happened* status vocabulary
    (:class:`~pipeline.engine.scope_expansion.ScopeExpansionStatus`): the
    classifier stays a pure fact (``notice`` / ``risk`` / ``blocker``); this
    enum is the verdict-route, and the route is *computed* per mode, never
    baked into the classifier.

    - ``AUTO_CONTINUE`` — record the expansion, re-setup, and continue without
      a pause. The ``fast``-mode outcome for every scope classification.
    - ``AUTO_ALERT`` — continue, but raise an operator-visible alert. The
      ``pro``-mode outcome for every scope classification.
    - ``HANDOFF`` — route through the phase-handoff lifecycle (ADR 0038) for
      operator sanction rather than silently rejecting. The ``governed``-mode
      outcome for every participant-add / scope expansion.

    Pure data: no I/O, no runtime/state imports.
    """
    AUTO_CONTINUE = "auto_continue"
    AUTO_ALERT = "auto_alert"
    HANDOFF = "handoff"


class CommitDecisionAction(StrEnum):
    """Action an operator can take on an active commit-decision gate.

    Action availability is published by the orchestrator in the gate's
    ``available_actions`` payload (today: always the full set). The
    decision SDK validates the chosen action against that payload.

    FIX — keep the retained run worktree for a correction follow-up.
    APPROVE — execute ``git add`` + ``git commit`` using the chosen
        :class:`CommitMessageStrategy` and any operator overrides.
    APPLY — deliver the run-owned diff to the project checkout but leave
        it uncommitted so the operator can batch it with other changes.
    SKIP — finalise the run without delivery; the diff remains in the
        run artifacts / retained worktree for manual recovery.
    HALT — terminate the run as halted; no commit; ``meta.status``
        flips to ``halted`` with ``halt_reason='commit_decision_halt'``.
    """
    FIX = "fix"
    APPROVE = "approve"
    APPLY = "apply"
    SKIP = "skip"
    HALT = "halt"


class CommitMessageStrategy(StrEnum):
    """How the commit-decision gate produces the ``git commit -m`` text.

    RELEASE_SUMMARY — reuse the release-gate ``short_summary`` as the
        subject; operators may override the message before approve.
    LLM_GENERATE — synchronously invoke the runtime with the
        ``commit_message_json_contract`` and render Conventional Commits
        text from the parsed JSON.
    OPERATOR_TYPED — no suggested message; the operator supplies the
        full text at decision time.
    """
    RELEASE_SUMMARY = "release_summary"
    LLM_GENERATE = "llm_generate"
    OPERATOR_TYPED = "operator_typed"


__all__ = [
    "ExecutionMode",
    "AgentRole",
    "SessionInvocationRole",
    "SessionContinuity",
    "EffortLevel",
    "GateKind",
    "FailStrategy",
    "ReviewTiming",
    "HumanAction",
    "AttachmentKind",
    "ProfileKind",
    "FullCycleDepth",
    "ScopedTarget",
    "ChangeHandoffMode",
    "PhaseHandoffType",
    "PhaseHandoffAction",
    "ScopeExpansionSanction",
    "CommitDecisionAction",
    "CommitMessageStrategy",
]
