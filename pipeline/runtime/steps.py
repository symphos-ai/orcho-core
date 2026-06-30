"""
pipeline/runtime/steps.py — per-step declared dataclasses.

A ``PhaseStep`` is one declared entry in a ``Profile``: it names a
phase, an execution mode, an optional skill, optional quality gates,
and an optional human review hook. ``QualityGate``, ``HumanReview``,
``Attachment`` are the supporting dataclasses that populate a step.

``PromptSpec`` is re-exported here for the long-standing
``pipeline.runtime.PromptSpec`` import path. Its canonical home is
``pipeline.prompts.spec`` (prompt-config data, not runtime-execution
data).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pipeline.prompts.spec import PromptSpec
from pipeline.runtime.roles import (
    AttachmentKind,
    EffortLevel,
    ExecutionMode,
    FailStrategy,
    GateKind,
    HumanAction,
    PhaseHandoffType,
    ReviewTiming,
)


class CrossScope(StrEnum):
    """Where a PhaseStep runs when its profile is projected for ``orcho cross``.

    GLOBAL  — runs once at the cross level (e.g. shared plan / validate_plan).
    PROJECT — runs once inside each child project's sub-pipeline.
    BOTH    — runs at both levels (rare; reserved for steps that genuinely
              fan out, e.g. a shared compliance check that also runs locally).
    SKIP    — omitted entirely in cross mode (still runs in mono mode).
    """
    GLOBAL = "global"
    PROJECT = "project"
    BOTH = "both"
    SKIP = "skip"


@dataclass(frozen=True)
class HypothesisPrelude:
    """Plan-step prelude configuration.

    ``attempts=0`` disables the pre-plan hypothesis pass. ``format`` is the
    optional format preset for both the hypothesis and its QA review; when
    omitted, callers inherit the owning plan step's prompt format.
    """

    attempts: int = 0
    format: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or self.attempts < 0
        ):
            raise TypeError(
                "HypothesisPrelude.attempts must be a non-negative int, "
                f"got {type(self.attempts).__name__}"
            )
        if self.format is not None and (
            not isinstance(self.format, str) or not self.format.strip()
        ):
            raise ValueError("HypothesisPrelude.format must be non-empty or None")
        if isinstance(self.format, str):
            object.__setattr__(self, "format", self.format.strip())


@dataclass(frozen=True)
class CrossStepPolicy:
    """Per-step cross-projection policy.

    ``scope``   declares where the step runs in cross mode.
    ``handler`` is dispatch metadata only — it does NOT rename
    ``PhaseStep.phase``. The cross runner uses ``handler`` to look up a
    cross-level function (e.g. ``cross_plan``, ``cross_validate_plan``)
    while keeping the semantic phase name intact so loop predicates like
    ``until: validate_plan.approved`` continue to evaluate correctly.

    A ``cross`` annotation has no effect in mono-project runs: the
    single-project runner ignores this field. A profile without ``cross``
    on a step is still valid for mono runs and is rejected only when the
    cross projector encounters it.
    """
    scope: CrossScope
    handler: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CrossScope):
            raise TypeError(
                f"CrossStepPolicy.scope must be CrossScope, got "
                f"{type(self.scope).__name__}"
            )
        if self.handler is not None and (
            not isinstance(self.handler, str) or not self.handler.strip()
        ):
            raise ValueError(
                "CrossStepPolicy.handler must be a non-empty string or None"
            )


@dataclass(frozen=True)
class QualityGate:
    """Registered post-phase check + fail policy. See docs/architecture/
    quality_gates.md (Phase 4)."""
    name: str
    on_fail: FailStrategy
    kind: GateKind = GateKind.COMPUTATIONAL
    feed_target: str | None = None
    config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("QualityGate.name is empty")
        if self.on_fail is FailStrategy.FEED_INTO_NEXT and not self.feed_target:
            raise ValueError(
                f"QualityGate {self.name!r}: feed_target required when "
                "on_fail=FEED_INTO_NEXT"
            )


@dataclass(frozen=True)
class HumanReview:
    """Opt-in blocking review point. Backend selection is global runtime
    concern (Phase 8), not declared on the dataclass.

    Invariants:
      - At least one terminal action {APPROVE, HALT, SKIP}
      - EDIT only valid when timing=AFTER (nothing to edit BEFORE handler ran)
    """
    timing: ReviewTiming = ReviewTiming.AFTER
    actions: tuple[HumanAction, ...] = (
        HumanAction.APPROVE, HumanAction.HALT,
        HumanAction.RETRY, HumanAction.REPROMPT,
    )
    prompt: str | None = None
    retry_budget: int = 5

    def __post_init__(self) -> None:
        if not self.actions:
            raise ValueError("HumanReview.actions cannot be empty")
        terminal = {HumanAction.APPROVE, HumanAction.HALT, HumanAction.SKIP}
        if not (set(self.actions) & terminal):
            raise ValueError(
                f"HumanReview.actions must include at least one terminal "
                f"action {sorted(t.value for t in terminal)} — otherwise run hangs"
            )
        if HumanAction.EDIT in self.actions and self.timing is ReviewTiming.BEFORE:
            raise ValueError(
                "HumanReview.EDIT incompatible with timing=BEFORE "
                "(nothing to edit before handler executes)"
            )
        if self.retry_budget < 0:
            raise ValueError(
                f"HumanReview.retry_budget must be ≥0, got {self.retry_budget}"
            )


@dataclass(frozen=True)
class PhaseHandoffPolicy:
    """Per-step human handoff policy attached to a ``PhaseStep``.

    ``type`` selects the handoff regime (see ``PhaseHandoffType``). Action
    availability is runtime-produced from the verdict and the active loop
    round, not declared here. The profile loader validates ``type`` only;
    decision actions are validated by the decision API against the active
    handoff's ``available_actions``.

    The loader accepts any policy on any phase; concrete runner support may
    be narrower than the schema. Unsupported policies are rejected by the
    executor at run time, not by the loader.

    ``repair_attempts`` and ``on_exhausted`` configure the implement-phase
    substance-repair fallback. ``repair_attempts`` (≥0) bounds automatic
    repair rounds before the handoff fires; ``on_exhausted`` selects what
    happens once they are spent (``halt`` to pause for an operator,
    ``auto_waiver`` to record a synthetic waiver and continue). Either
    non-default value requires an interactive ``type`` — ``HUMAN_BYPASS``
    never pauses, so a repair/exhaustion policy on it is meaningless.
    """

    type: PhaseHandoffType = PhaseHandoffType.HUMAN_BYPASS
    repair_attempts: int = 0
    on_exhausted: str = "halt"

    def __post_init__(self) -> None:
        if not isinstance(self.type, PhaseHandoffType):
            raise TypeError(
                f"PhaseHandoffPolicy.type must be PhaseHandoffType, got "
                f"{type(self.type).__name__}"
            )
        if self.repair_attempts < 0:
            raise ValueError(
                f"PhaseHandoffPolicy.repair_attempts must be ≥0, got "
                f"{self.repair_attempts}"
            )
        if self.on_exhausted not in {"halt", "auto_waiver"}:
            raise ValueError(
                f"PhaseHandoffPolicy.on_exhausted must be one of "
                f"{{'halt', 'auto_waiver'}}, got {self.on_exhausted!r}"
            )
        non_default = self.repair_attempts != 0 or self.on_exhausted != "halt"
        if non_default and self.type is PhaseHandoffType.HUMAN_BYPASS:
            raise ValueError(
                "PhaseHandoffPolicy.repair_attempts/on_exhausted require an "
                "interactive type; HUMAN_BYPASS never pauses"
            )


_ATTACHMENT_DEFAULT_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB


@dataclass(frozen=True)
class Attachment:
    """Prompt-context resource: file (TEXT/IMAGE/BINARY) or inline payload.

    ``content_path`` (filesystem) and ``content_b64`` (inline) are mutually
    exclusive — exactly one must be set. ``mime_type`` is required for
    IMAGE/BINARY (vision routing); optional for TEXT.
    """
    kind: AttachmentKind
    name: str
    content_path: str | None = None
    content_b64: str | None = None
    mime_type: str | None = None
    description: str | None = None
    size_bytes: int | None = None       # populated by loader
    content_hash: str | None = None     # sha256 hex, populated by loader

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Attachment.name is empty")
        has_path = bool(self.content_path)
        has_b64 = bool(self.content_b64)
        if has_path == has_b64:  # both or neither
            raise ValueError(
                f"Attachment {self.name!r}: exactly one of content_path / "
                "content_b64 required (xor)"
            )
        if self.kind in (AttachmentKind.IMAGE, AttachmentKind.BINARY) \
                and not self.mime_type:
            raise ValueError(
                f"Attachment {self.name!r}: mime_type required for "
                f"kind={self.kind.value}"
            )
        if self.size_bytes is not None and \
                self.size_bytes > _ATTACHMENT_DEFAULT_SIZE_LIMIT:
            raise ValueError(
                f"Attachment {self.name!r}: size {self.size_bytes} bytes "
                f"exceeds limit {_ATTACHMENT_DEFAULT_SIZE_LIMIT}"
            )


@dataclass(frozen=True)
class PhaseStep:
    """Declared step in a Profile. Combines what (phase name), how
    (execution mode), what skill grounds the prompt, what gates run
    after, what review hooks fire.

    ADR 0027 / M11: ``execution`` stays as the mode string for
    backward compatibility with the many callers that read it as
    a plain string (lifecycle resolver, runner validator, etc.).
    ``execution_policy`` is the richer profile-visible policy
    object that carries ``session_split`` and the reserved
    fanout-review shape. The loader keeps the two consistent by
    construction and the post-init below enforces
    ``execution_policy.mode == execution``.
    """
    phase: str
    execution: str = ExecutionMode.LINEAR.value     # open string (R4) — plugin
                                                     # extension via registry
    skill: str | None = None
    effort: EffortLevel | None = None
    overrides: dict[str, Any] | None = None
    prompt: PromptSpec | None = None
    hypothesis: HypothesisPrelude | None = None
    quality_gates: tuple[QualityGate, ...] = ()
    human_review: HumanReview | None = None
    handoff: PhaseHandoffPolicy | None = None
    cross: CrossStepPolicy | None = None
    # ADR 0027 / M11: profile-visible execution policy. Defaults
    # to ``ExecutionPolicy(mode="linear")`` so legacy string-only
    # profiles get a deterministic policy object without an
    # explicit JSON edit. The loader synthesises a policy whose
    # ``mode`` matches the string form when authors use
    # ``"execution": "linear"``.
    execution_policy: Any = None  # ExecutionPolicy — typed loosely
                                    # to break a runtime/profile cycle.

    def __post_init__(self) -> None:
        if not self.phase or not self.phase.strip():
            raise ValueError("PhaseStep.phase is empty")
        if not self.execution or not self.execution.strip():
            raise ValueError("PhaseStep.execution is empty")
        # Synthesise the default execution policy when the loader did
        # not provide one (handler-direct construction in tests, or
        # legacy callers that pre-date ADR 0027). The synthesised
        # policy mirrors the string-form mode so downstream readers
        # see a consistent view.
        if self.execution_policy is None:
            from pipeline.runtime.profile import ExecutionPolicy
            object.__setattr__(
                self,
                "execution_policy",
                ExecutionPolicy(mode=self.execution),
            )
        else:
            from pipeline.runtime.profile import ExecutionPolicy
            if not isinstance(self.execution_policy, ExecutionPolicy):
                raise TypeError(
                    f"PhaseStep {self.phase!r}: execution_policy must be "
                    f"ExecutionPolicy or None, "
                    f"got {type(self.execution_policy).__name__}"
                )
            if self.execution_policy.mode != self.execution:
                raise ValueError(
                    f"PhaseStep {self.phase!r}: execution_policy.mode="
                    f"{self.execution_policy.mode!r} does not match "
                    f"execution={self.execution!r}"
                )
        # ADR 0027 hardening: ``session_split=per_role`` keys the
        # prompt-session by ``prompt.role``. Without an explicit
        # ``prompt.role``, the wiring helper falls back to the phase
        # name, which silently degrades ``per_role`` to ``per_phase``
        # under a different label. Reject the inconsistent profile
        # at construction time so the trace can be trusted.
        if (
            getattr(self.execution_policy, "session_split", None) == "per_role"
            and (self.prompt is None or not getattr(self.prompt, "role", None))
        ):
            raise ValueError(
                f"PhaseStep {self.phase!r}: execution_policy.session_split="
                "'per_role' requires prompt.role to be set on the same step; "
                "without an explicit role the per_role split silently "
                "degrades to per_phase under a different label"
            )
        if self.hypothesis is not None and not isinstance(
            self.hypothesis, HypothesisPrelude
        ):
            raise TypeError(
                f"PhaseStep {self.phase!r}: hypothesis must be "
                f"HypothesisPrelude or None, "
                f"got {type(self.hypothesis).__name__}"
            )
        gate_names = [g.name for g in self.quality_gates]
        if len(set(gate_names)) != len(gate_names):
            duplicates = {n for n in gate_names if gate_names.count(n) > 1}
            raise ValueError(
                f"PhaseStep {self.phase!r}: duplicate quality_gate names {duplicates}"
            )
        if self.cross is not None and not isinstance(self.cross, CrossStepPolicy):
            raise TypeError(
                f"PhaseStep {self.phase!r}: cross must be CrossStepPolicy or None, "
                f"got {type(self.cross).__name__}"
            )
        if self.handoff is not None and not isinstance(self.handoff, PhaseHandoffPolicy):
            raise TypeError(
                f"PhaseStep {self.phase!r}: handoff must be PhaseHandoffPolicy or None, "
                f"got {type(self.handoff).__name__}"
            )
        if self.handoff is not None and self.human_review is not None:
            raise ValueError(
                f"PhaseStep {self.phase!r}: handoff and human_review are mutually "
                "exclusive — both declare human-control surface on the same step"
            )


__all__ = [
    "PromptSpec",
    "HypothesisPrelude",
    "QualityGate",
    "HumanReview",
    "PhaseHandoffPolicy",
    "Attachment",
    "PhaseStep",
    "CrossScope",
    "CrossStepPolicy",
]
