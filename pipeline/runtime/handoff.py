"""Phase-handoff runtime signal, resolver protocol, and trigger helpers.

The loop runner publishes a :class:`PhaseHandoffRequested` signal when a
phase declares a non-bypass ``handoff`` policy and the trigger condition
fires. The signal is consumed by:

* a transport-agnostic :data:`PhaseHandoffResolver` that decides how to
  respond (default: :func:`pause_resolver`, returning
  :class:`PhaseHandoffResolution.PAUSE`);
* the project orchestrator, which picks the ``PAUSE`` resolution off
  ``PipelineState.phase_handoff_request``, persists
  ``meta.phase_handoff`` + ``meta.status="awaiting_phase_handoff"``,
  emits ``phase.handoff_requested``, and exits rc=4. The runner does
  **not** write meta itself.

This module is data + protocol only. Trigger discipline (the
3-condition gate for ``human_feedback_on_reject``, the always-fire
behaviour for ``human_feedback_always``, the available-actions matrix
for each verdict) lives in :func:`build_phase_handoff_signal` so the
loop driver has a single, testable seam.

Ownership invariant: the runner returns this signal; the orchestrator
owns lifecycle artifacts (meta mutation, event emission, rc=4). Keeping
the signal data-only makes it trivial to ship across that boundary.

Runtime support is intentionally narrow: ``validate_plan`` inside the
plan loop and ``review_changes`` inside the review/repair loop are
honoured, plus the bare top-level ``implement`` (ADR 0073) and
``final_acceptance`` (ADR 0112 §5 scope-expansion seam) phases. The
phase-name guard lives in two places (the support check at
``run_profile`` time and this trigger helper) so widening support must
be a conscious change to both.

ADR 0112 §5 scope-expansion handoff: when the final_acceptance sanction
routing (:mod:`pipeline.phases.builtin.scope_expansion_support`) returns
a ``HANDOFF`` disposition for an out-of-plan change, a phase handoff is
raised at runtime via :func:`build_scope_expansion_handoff_signal` rather
than from a verdict loop. It rides the same ADR 0038 lifecycle as the
loop-driven handoffs (same signal, same decide/advice path) but carries a
``scope_expansion:*`` trigger: ``scope_expansion:participant_add:<repo>``
for a participant-add promotion and ``scope_expansion:out_of_plan`` for a
generic out-of-plan blocker. The trigger string is opaque to the decide /
advice machinery — only the action typing is fixed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pipeline.runtime.roles import PhaseHandoffAction, PhaseHandoffType

if TYPE_CHECKING:
    from pipeline.runtime.profile import LoopStep
    from pipeline.runtime.state import PipelineState
    from pipeline.runtime.steps import PhaseStep


_SUPPORTED_HANDOFF_PHASES = frozenset(
    {"validate_plan", "review_changes", "implement", "final_acceptance"}
)

# ADR 0112 §5: stable opaque trigger strings for the scope-expansion handoff.
# The participant-add variant appends the discovered repo; the out-of-plan
# variant is a fixed descriptor. Both share the ``scope_expansion:`` family
# prefix so a consumer can recognise the family without parsing the tail.
_SCOPE_EXPANSION_TRIGGER_PREFIX = "scope_expansion:"
SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER = "scope_expansion:out_of_plan"
# The participant-add variant appends the discovered repo to this prefix. Exposed
# so the resume path can tell it apart from the out-of-plan variant: they re-enter
# the pipeline at different points (see ``_apply_scope_expansion_handoff_resume``).
SCOPE_EXPANSION_PARTICIPANT_ADD_PREFIX = "scope_expansion:participant_add:"

# The phase seam a scope-expansion sanction routes its handoff through. The
# final_acceptance handler is where the T2 routing fires and the durable
# ``scope_expansion_sanction`` evidence is recorded; the participant-add variant
# is discovered earlier (increment C) but surfaces its handoff through this same
# seam, so a single phase widening at both support sites covers both triggers.
SCOPE_EXPANSION_HANDOFF_PHASE = "final_acceptance"

# A scope-expansion handoff is raised at runtime (not from a verdict loop), so
# it carries its own stable round-bucket key rather than a loop's.
_SCOPE_EXPANSION_ROUND_EXTRAS_KEY = "scope_expansion"


def scope_expansion_participant_add_trigger(repo: str) -> str:
    """Return the stable ``scope_expansion:participant_add:<repo>`` trigger.

    ``repo`` is the discovered out-of-set repository identity (an alias or a
    path); it is preserved verbatim in the trigger tail so the persisted
    ``meta.phase_handoff`` records exactly which repo prompted the promotion.
    """
    if not isinstance(repo, str) or not repo:
        raise ValueError(
            "scope_expansion_participant_add_trigger: repo must be non-empty"
        )
    return f"{SCOPE_EXPANSION_PARTICIPANT_ADD_PREFIX}{repo}"


class PhaseHandoffResolution(StrEnum):
    """How a resolver wants the runner to react to a handoff signal.

    PAUSE — the orchestrator must persist the signal and exit rc=4. This
        is the default for non-interactive execution (CLI without TTY,
        MCP-driven runs, CI).

    Future resolvers (interactive TTY, Web prompt) may extend this enum
    with inline directives that apply ``continue`` / ``retry_feedback``
    / ``halt`` actions without exiting the process. Slice 2 only ships
    ``PAUSE``; inline directives land with the CLI TTY resolver wiring.
    """

    PAUSE = "pause"


@dataclass(frozen=True, slots=True)
class PhaseHandoffRequested:
    """Runtime signal: a phase handoff trigger fired and the runner needs
    a resolution.

    The fields mirror the persisted ``meta.phase_handoff`` payload one
    layer up — the project orchestrator (later slice) copies these into
    ``meta`` verbatim, then emits ``phase.handoff_requested`` with the
    same ``handoff_id`` / ``phase`` / ``handoff_type`` / ``trigger`` /
    ``round`` fields.

    Built by :func:`build_phase_handoff_signal`; consumers should treat
    the dataclass as opaque audit-grade data — no derived fields.
    """

    handoff_id: str
    phase: str
    type: PhaseHandoffType
    trigger: str
    verdict: str
    approved: bool
    round_extras_key: str
    round: int
    loop_max_rounds: int
    available_actions: tuple[str, ...]
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    last_output: str = ""

    def __post_init__(self) -> None:
        if not self.handoff_id:
            raise ValueError("PhaseHandoffRequested.handoff_id must be non-empty")
        if not self.phase:
            raise ValueError("PhaseHandoffRequested.phase must be non-empty")
        if not isinstance(self.type, PhaseHandoffType):
            raise TypeError(
                "PhaseHandoffRequested.type must be PhaseHandoffType, "
                f"got {type(self.type).__name__}"
            )
        if not self.available_actions:
            raise ValueError(
                "PhaseHandoffRequested.available_actions must be non-empty"
            )
        if self.round < 1:
            raise ValueError(
                f"PhaseHandoffRequested.round must be >= 1, got {self.round}"
            )
        if self.loop_max_rounds < 1:
            raise ValueError(
                "PhaseHandoffRequested.loop_max_rounds must be >= 1, got "
                f"{self.loop_max_rounds}"
            )


PhaseHandoffResolver = Callable[
    ["PhaseHandoffRequested", "PipelineState"], PhaseHandoffResolution,
]
"""Callable contract for resolving a handoff signal.

The runner calls the active resolver after every fired trigger. The
default :func:`pause_resolver` returns ``PAUSE``, which the orchestrator
picks up via ``PipelineState.phase_handoff_request`` after the run loop
exits. Embedders supplying interactive transports (CLI TTY, Web prompt)
should mirror this contract: read the signal, ask the human, return a
resolution. The decision **artifact** is still written through
``sdk.phase_handoff.phase_handoff_decide`` — resolvers never hand-roll
artifact I/O (audit-trail invariant).
"""


def pause_resolver(
    signal: PhaseHandoffRequested,  # noqa: ARG001 — protocol takes signal
    state: PipelineState,            # noqa: ARG001 — protocol takes state
) -> PhaseHandoffResolution:
    """Default no-op resolver: always return ``PAUSE``.

    The orchestrator (one slice up) reads
    ``PipelineState.phase_handoff_request`` after the run loop exits,
    persists ``meta.phase_handoff``, emits the
    ``phase.handoff_requested`` event, and exits rc=4. Suitable for
    non-interactive runs (CI, MCP, CLI without TTY).
    """
    return PhaseHandoffResolution.PAUSE


def build_phase_handoff_signal(
    step: PhaseStep,
    loop_step: LoopStep,
    state: PipelineState,
    round_n: int,
) -> PhaseHandoffRequested | None:
    """Return a signal when ``step.handoff`` trigger fires for this round.

    Trigger discipline:

    * ``human_bypass`` — never fires.
    * ``human_feedback_on_reject`` — fires only when **all three** of:

        1. the phase verdict is ``approved=False``;
        2. the loop's ``until`` predicate is not satisfied (guaranteed
           by the support matrix — the runner only accepts canonical
           predicates whose bool verdict field matches the triggering
           phase);
        3. this is the final automatic round (``round_n >=
           loop_step.max_rounds``); earlier rounds still have auto
           budget and the loop must retry, not pause.

    * ``human_feedback_always`` — fires on every round irrespective of
      verdict and budget; available actions are
      ``[continue, retry_feedback, halt]`` (plus ``continue_with_waiver``
      when the verdict is REJECTED). The human keeps feedback
      authority even when the reviewer agent already approved — the
      one-shot human-directed round budget (see
      :data:`HUMAN_DIRECTED_ROUNDS_KEY`) sits on top of
      ``LoopStep.max_rounds`` precisely so this is legal at any round.

    Defense-in-depth: even though
    :func:`pipeline.runtime.runner._validate_handoff_support` rejects
    non-bypass handoff on unsupported phases at ``run_profile`` time,
    this trigger refuses to fire for other phases too. The runner-time
    check and this trigger-time check intentionally duplicate the same
    supported-phase constant — widening support must be a conscious
    change in both modules, not a single import that one site silently
    inherits.

    Returns ``None`` when the trigger does not fire. The phase log is
    consulted via ``state.phase_log[step.phase]``; missing or shape-mismatched
    entries (no verdict recorded) also yield ``None`` — the runner
    treats those as "no opinion" and keeps iterating.
    """
    policy = step.handoff
    if policy is None or policy.type is PhaseHandoffType.HUMAN_BYPASS:
        return None
    # Defense-in-depth — the runner's support matrix should have
    # rejected this at ``run_profile`` time, but a future code path
    # adding handoff support to a new phase must consciously update
    # both the support check and this trigger gate together.
    if step.phase not in _SUPPORTED_HANDOFF_PHASES:
        return None

    approved, verdict_label_from_log = _read_verdict(state, step.phase)
    if approved is None:
        # No verdict (or shape-mismatched) — runner treats as "no
        # opinion" and proceeds. ``describe_handoff_outcome`` surfaces
        # this as ``NO_VERDICT`` so the operator still sees the
        # policy was active.
        return None
    # Re-fetch the verdict entry for the artifact / critique fields that
    # ``_read_verdict`` does not surface (it only normalises the
    # approved / verdict-label pair shared with
    # ``describe_handoff_outcome``).
    log_entry = state.phase_log.get(step.phase)
    if isinstance(log_entry, list) and log_entry and isinstance(
        log_entry[-1], dict,
    ):
        verdict_entry: dict[str, Any] = log_entry[-1]
    elif isinstance(log_entry, dict):
        verdict_entry = log_entry
    else:  # pragma: no cover — _read_verdict already gated on this
        return None

    if policy.type is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT:
        if approved:
            return None
        if round_n < loop_step.max_rounds:
            return None
        trigger = "rejected"
        available_actions = (
            PhaseHandoffAction.CONTINUE.value,
            PhaseHandoffAction.RETRY_FEEDBACK.value,
            PhaseHandoffAction.HALT.value,
            PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
        )
    elif policy.type is PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS:
        # The whole point of ``human_feedback_always`` is that the human
        # always has feedback authority — including the right to disagree
        # with an APPROVED verdict from the reviewer agent. Offer the
        # same full action set on either verdict; the bonus human-directed
        # round (see ``HUMAN_DIRECTED_ROUNDS_KEY``) is reserved on top of
        # ``LoopStep.max_rounds`` so retry stays legal regardless of how
        # much auto budget has been consumed.
        trigger = "approved" if approved else "rejected"
        # ``continue_with_waiver`` only makes sense against a REJECTED
        # verdict — there is nothing to waive when the reviewer approved.
        if approved:
            available_actions = (
                PhaseHandoffAction.CONTINUE.value,
                PhaseHandoffAction.RETRY_FEEDBACK.value,
                PhaseHandoffAction.HALT.value,
            )
        else:
            available_actions = (
                PhaseHandoffAction.CONTINUE.value,
                PhaseHandoffAction.RETRY_FEEDBACK.value,
                PhaseHandoffAction.HALT.value,
                PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
            )
    else:
        return None

    handoff_id = f"{step.phase}:{loop_step.round_extras_key}:{round_n}"
    verdict_label = verdict_label_from_log or (
        "APPROVED" if approved else "REJECTED"
    )
    raw_artifacts = verdict_entry.get("artifacts")
    artifacts: dict[str, Any] = (
        dict(raw_artifacts) if isinstance(raw_artifacts, Mapping) else {}
    )
    # validate_plan handlers write ``plan_file`` directly into the
    # phase-log entry, not under a nested ``artifacts`` dict. The
    # persisted handoff payload promises an ``artifacts`` view of the
    # review surface (the UI needs the plan file to render the review /
    # edit screen), so we surface that field here. Any explicit
    # ``artifacts["plan_file"]`` wins over the top-level fallback.
    if "plan_file" not in artifacts:
        raw_plan_file = verdict_entry.get("plan_file")
        if isinstance(raw_plan_file, str) and raw_plan_file:
            artifacts["plan_file"] = raw_plan_file
    for key in ("short_summary", "findings", "risks", "checks", "parse_warnings"):
        if key in artifacts:
            continue
        raw = verdict_entry.get(key)
        if raw is not None:
            artifacts[key] = raw
    raw_last = (
        verdict_entry.get("critique")
        or verdict_entry.get("last_output")
        or verdict_entry.get("output")
        or ""
    )
    last_output = raw_last if isinstance(raw_last, str) else ""

    return PhaseHandoffRequested(
        handoff_id=handoff_id,
        phase=step.phase,
        type=policy.type,
        trigger=trigger,
        verdict=verdict_label,
        approved=approved,
        round_extras_key=loop_step.round_extras_key,
        round=round_n,
        loop_max_rounds=loop_step.max_rounds,
        available_actions=available_actions,
        artifacts=artifacts,
        last_output=last_output,
    )


def build_scope_expansion_handoff_signal(
    *,
    trigger: str,
    round_n: int = 1,
    phase: str = SCOPE_EXPANSION_HANDOFF_PHASE,
    handoff_id: str | None = None,
    artifacts: Mapping[str, Any] | None = None,
    last_output: str = "",
) -> PhaseHandoffRequested | None:
    """Build a phase-handoff signal for a scope-expansion ``HANDOFF`` sanction.

    ADR 0112 §5: the final_acceptance sanction routing returns ``HANDOFF`` for an
    out-of-plan change that must not silently reject under ``pro`` / ``governed``
    — it needs operator sanction. This builds the :class:`PhaseHandoffRequested`
    that rides the ADR 0038 lifecycle for that pause, for either trigger family:

    * ``scope_expansion:participant_add:<repo>`` (build with
      :func:`scope_expansion_participant_add_trigger`) — a promotion of a
      newly-discovered out-of-set repository;
    * ``scope_expansion:out_of_plan`` (:data:`SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER`)
      — a generic out-of-plan blocker not tied to a repo add.

    The operator action set is ``continue`` / ``halt`` / ``continue_with_waiver``
    so the ``continue_with_waiver`` escape hatch stays available. Unlike the
    loop-driven REJECTED handoff, ``retry_feedback`` is intentionally omitted: this
    sanction is raised at the terminal ``final_acceptance`` seam (a bare top-level
    phase), so there is no plan/repair loop to retry into. ``continue_with_waiver``
    is the durable escape hatch; routing a retry here would mis-resume into the
    plan loop (see ``_apply_scope_expansion_handoff_resume`` in
    :mod:`pipeline.project.handoff`).

    Support guard (the handoff.py site): returns ``None`` when ``phase`` is not a
    supported handoff phase, mirroring :func:`build_phase_handoff_signal`'s
    line-205 guard so the two phase-name gates stay duplicated on purpose. A
    ``trigger`` outside the ``scope_expansion:`` family is a programming error and
    raises — this builder is only for scope-expansion handoffs.
    """
    if not isinstance(trigger, str) or not trigger.startswith(
        _SCOPE_EXPANSION_TRIGGER_PREFIX
    ):
        raise ValueError(
            "build_scope_expansion_handoff_signal: trigger must be a "
            f"'{_SCOPE_EXPANSION_TRIGGER_PREFIX}*' string, got {trigger!r}"
        )
    # Phase-name support guard — the conscious twin of build_phase_handoff_signal's
    # check. Widening support means editing both this set and the runner's.
    if phase not in _SUPPORTED_HANDOFF_PHASES:
        return None
    if round_n < 1:
        raise ValueError(
            f"build_scope_expansion_handoff_signal: round_n must be >= 1, got "
            f"{round_n}"
        )

    resolved_handoff_id = handoff_id or f"{phase}:{trigger}:{round_n}"
    # A scope-expansion handoff always pauses for operator sanction. The action
    # set is continue / halt / continue_with_waiver: continue_with_waiver disarms
    # it (the durable escape hatch), and retry_feedback is omitted because the
    # terminal final_acceptance seam has no plan/repair loop to retry into — see
    # the builder docstring and ``_apply_scope_expansion_handoff_resume``.
    available_actions = (
        PhaseHandoffAction.CONTINUE.value,
        PhaseHandoffAction.HALT.value,
        PhaseHandoffAction.CONTINUE_WITH_WAIVER.value,
    )
    return PhaseHandoffRequested(
        handoff_id=resolved_handoff_id,
        phase=phase,
        type=PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS,
        trigger=trigger,
        # The scope expansion needs operator sanction: not an auto-approval. A
        # rejected-equivalent verdict keeps the advisory pass eligible.
        verdict="REJECTED",
        approved=False,
        round_extras_key=_SCOPE_EXPANSION_ROUND_EXTRAS_KEY,
        round=round_n,
        loop_max_rounds=max(round_n, 1),
        available_actions=available_actions,
        artifacts=dict(artifacts or {}),
        last_output=last_output,
    )


# Convention key for the "extra human-directed rounds" budget that a
# future resume path (slice 4) injects into ``state.extras`` so the
# loop runner can add exactly one ``plan → validate_plan`` round on top
# of ``LoopStep.max_rounds`` without mutating the profile.
HUMAN_DIRECTED_ROUNDS_KEY = "_phase_handoff_human_directed_rounds"

# Per-round flag the loop driver writes into ``state.extras`` so phase
# handlers and observability can distinguish a human-directed retry from
# an automatic auto-budget round.
HUMAN_DIRECTED_FLAG_KEY = "_phase_handoff_human_directed_round"


def extra_human_directed_rounds(state: PipelineState, loop_step: LoopStep) -> int:
    """Return the number of human-directed extra rounds queued for ``loop_step``.

    The slice 4 resume path will write
    ``state.extras[HUMAN_DIRECTED_ROUNDS_KEY][round_extras_key] = N``
    before re-entering ``_run_loop_step``. Slice 2 leaves the value at
    ``0`` by default, so the loop bounds stay identical to the
    pre-handoff behaviour.
    """
    bag = state.extras.get(HUMAN_DIRECTED_ROUNDS_KEY)
    if not isinstance(bag, dict):
        return 0
    raw = bag.get(loop_step.round_extras_key, 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


class HandoffOutcomeKind(StrEnum):
    """Classification of what a non-bypass handoff policy decided this round.

    The runner publishes one of these for every round of a phase whose
    ``handoff`` policy is non-bypass — even when no pause is requested —
    so the orchestrator (and any other observer) can surface what the
    policy did. ``FIRED`` is the case that also yields a
    :class:`PhaseHandoffRequested` signal; the others are
    observability-only.
    """

    FIRED = "fired"
    """A pause was requested. The runner has set ``state.phase_handoff_request``;
    the orchestrator will persist + resolve."""

    DEFERRED = "deferred"
    """Policy is active but did not fire this round because automatic
    retry budget remains (``human_feedback_on_reject`` with a rejected
    verdict before the final auto round)."""

    BYPASSED = "bypassed"
    """Policy is active but did not fire because the verdict is approved
    and the policy only intercepts rejected verdicts
    (``human_feedback_on_reject``)."""

    NO_VERDICT = "no_verdict"
    """Policy is active but the phase did not publish a strict-bool
    ``approved`` field — defensive classification. The runner treats this
    as "no opinion" and keeps iterating; surfacing it makes the absence
    visible to operators rather than silently falling back to a retry."""


@dataclass(frozen=True, slots=True)
class HandoffOutcome:
    """One-round description of what the active handoff policy decided.

    The runner emits this through an optional ``on_handoff_outcome``
    callback for **every** round of a phase whose policy is non-bypass.
    Bypass-only policies (``human_bypass`` or no policy at all) yield
    ``None`` from :func:`describe_handoff_outcome` so observers never
    see "noise" lines for unconfigured phases.

    ``message`` is a short, operator-readable sentence; transports format
    it however they like (CLI prefixes a checkmark, Web renders a chip).
    """

    phase: str
    policy_type: PhaseHandoffType
    kind: HandoffOutcomeKind
    round: int
    loop_max_rounds: int
    verdict: str | None
    approved: bool | None
    message: str


def describe_handoff_outcome(
    step: PhaseStep,
    loop_step: LoopStep,
    state: PipelineState,
    round_n: int,
) -> HandoffOutcome | None:
    """Classify what ``step.handoff`` did this round, even when no pause fires.

    Returns ``None`` for ``human_bypass`` and for steps without a policy
    — those have no observable handoff outcome.

    For non-bypass policies returns a :class:`HandoffOutcome` whose
    ``kind`` is one of :class:`HandoffOutcomeKind`. The classification
    mirrors :func:`build_phase_handoff_signal`'s trigger discipline so
    ``FIRED`` outcomes always correspond to a signal being returned by
    that function for the same inputs.

    Shape-mismatched / missing verdicts map to ``NO_VERDICT``; the runner
    treats them as "no opinion" but the operator should still know the
    policy was active and the phase log was empty/malformed.
    """
    policy = step.handoff
    if policy is None or policy.type is PhaseHandoffType.HUMAN_BYPASS:
        return None

    approved, verdict_label = _read_verdict(state, step.phase)

    if approved is None:
        return HandoffOutcome(
            phase=step.phase,
            policy_type=policy.type,
            kind=HandoffOutcomeKind.NO_VERDICT,
            round=round_n,
            loop_max_rounds=loop_step.max_rounds,
            verdict=verdict_label,
            approved=None,
            message=(
                f"policy={policy.type.value}: no verdict on phase "
                f"{step.phase!r} this round — runtime keeps iterating"
            ),
        )

    if policy.type is PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT:
        if approved:
            kind = HandoffOutcomeKind.BYPASSED
            message = (
                f"policy={policy.type.value}: verdict approved on round "
                f"{round_n}/{loop_step.max_rounds} — no human input required"
            )
        elif round_n < loop_step.max_rounds:
            kind = HandoffOutcomeKind.DEFERRED
            message = (
                f"policy={policy.type.value}: verdict rejected on round "
                f"{round_n}/{loop_step.max_rounds} — auto-retry budget "
                f"remains, no pause"
            )
        else:
            kind = HandoffOutcomeKind.FIRED
            message = (
                f"policy={policy.type.value}: verdict rejected on final "
                f"automatic round {round_n}/{loop_step.max_rounds} — "
                f"pausing for human decision"
            )
    elif policy.type is PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS:
        # ``human_feedback_always`` fires every round; the action set
        # varies by verdict but the outcome kind is uniformly FIRED.
        kind = HandoffOutcomeKind.FIRED
        verdict_phrase = "approved" if approved else "rejected"
        message = (
            f"policy={policy.type.value}: verdict {verdict_phrase} on round "
            f"{round_n}/{loop_step.max_rounds} — pausing for human decision"
        )
    else:
        # Unknown policy type — return None so the runner doesn't surface
        # a line we don't know how to describe. Loader-level validation
        # should have rejected this upstream.
        return None

    return HandoffOutcome(
        phase=step.phase,
        policy_type=policy.type,
        kind=kind,
        round=round_n,
        loop_max_rounds=loop_step.max_rounds,
        verdict=verdict_label,
        approved=approved,
        message=message,
    )


def _read_verdict(
    state: PipelineState, phase: str,
) -> tuple[bool | None, str | None]:
    """Read the ``approved`` bool + ``verdict`` label for ``phase``.

    Returns ``(None, label_or_None)`` when the phase log is missing,
    malformed, or has no strict-bool ``approved`` field. Single source
    of truth shared by :func:`build_phase_handoff_signal` and
    :func:`describe_handoff_outcome` so both see the same picture.
    """
    log_entry = state.phase_log.get(phase)
    verdict_entry: dict[str, Any] | None
    if isinstance(log_entry, list) and log_entry and isinstance(
        log_entry[-1], dict,
    ):
        verdict_entry = log_entry[-1]
    elif isinstance(log_entry, dict):
        verdict_entry = log_entry
    else:
        return None, None
    if "approved" not in verdict_entry:
        return None, None
    approved_raw = verdict_entry.get("approved")
    if not isinstance(approved_raw, bool):
        return None, None
    verdict_label = verdict_entry.get("verdict")
    if not isinstance(verdict_label, str) or not verdict_label:
        verdict_label = "APPROVED" if approved_raw else "REJECTED"
    return approved_raw, verdict_label


PhaseHandoffOutcomeCallback = Callable[["HandoffOutcome"], None]
"""Optional observer for per-round handoff outcomes.

The runner invokes this once per inner step per round when
:func:`describe_handoff_outcome` returns a non-``None`` outcome — i.e.
the step has a non-bypass policy attached. Wiring the callback is the
transport's job: orchestrator prints to stdout, web pushes to the
review surface, MCP folds into the event spine.
"""


__all__ = [
    "HUMAN_DIRECTED_FLAG_KEY",
    "HUMAN_DIRECTED_ROUNDS_KEY",
    "SCOPE_EXPANSION_HANDOFF_PHASE",
    "SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER",
    "SCOPE_EXPANSION_PARTICIPANT_ADD_PREFIX",
    "HandoffOutcome",
    "HandoffOutcomeKind",
    "PhaseHandoffOutcomeCallback",
    "PhaseHandoffRequested",
    "PhaseHandoffResolution",
    "PhaseHandoffResolver",
    "build_phase_handoff_signal",
    "build_scope_expansion_handoff_signal",
    "describe_handoff_outcome",
    "extra_human_directed_rounds",
    "pause_resolver",
    "scope_expansion_participant_add_trigger",
]
