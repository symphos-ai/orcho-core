# SPDX-License-Identifier: Apache-2.0
"""verification_env_retry.py — the ``retry_verification`` resume owner.

An ``env_failure`` gate set is the one blocking verdict no agent can move: the
command never produced a verdict because its *environment* was wrong. The
operator fixes that outside the run and asks the engine to re-measure. So this
resume dispatches no agent, writes no feedback, and touches nothing in the
checkout — it re-executes exactly the persisted blocking set, on exactly the
subject those receipts observed, and then either walks on or re-parks honestly.

Because nothing is repaired, the *only* thing standing between the operator's
decision and a re-execution is evidence. Every precondition therefore runs
before any mutation, and each one is a distinct, named block reason:

1. **Marker** — a pre-router setup step already found the ledger untrustworthy
   (:data:`ENV_RETRY_LEDGER_BLOCKED_KEY`). Nothing here reads it; the marker's
   own reason is the block.
2. **Decision** — the active payload still matches the decided id, the menu
   really offered ``retry_verification``, and the persisted record admits it
   (:func:`~pipeline.project.gate_handoff_actions.env_retry_admissible`).
3. **Ledger** — every identity is a durably *selected* row whose latest
   execution event both **failed** and points at the same ``receipt_evidence``
   the handoff recorded. A ``pass`` after the recorded failure means the
   evidence is stale; a missing execution means the record was altered.
4. **Receipt** — the pointer resolves inside *this* run's immutable execution
   evidence, and the file it names parses in strict form and classifies as
   ``failed`` / ``env_failure``. A pointer that escapes the run directory, or a
   malformed, absent, or differently-classified receipt, is not permission to
   re-run.
5. **Subject** — the retained worktree still exists and still sits at the HEAD
   the failing receipts agreed on (:func:`ensure_verification_subject_retained`).

A chain of four independent facts (identity ↔ ledger ↔ receipt ↔ retained
subject) that must all name the same thing is deliberate: any one of them alone
could be reconstructed from a record an operator or a crash had edited, and a
re-execution taken on a guess would publish a *pass* the run never earned.
Every block re-parks the pause through
:func:`~pipeline.project.gate_repair.repark_verification_handoff_retry_blocked`
with its reason, so the run stays decidable and zero gates run.

Execution itself is not re-implemented here: it delegates to the existing
:func:`~pipeline.project.gate_repair.rerun_verification_handoff_gate`, the same
owner the ``retry_feedback`` path uses after its repair round. This module
must never reach for the repair/FSM dispatch seams — there is no agent round in
an env retry, and borrowing one would spend a repair budget on work no agent
performed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.io.retry import AgentCallError
from pipeline.control.handoff_routing import GateIdentity
from pipeline.project import gate_handoff_actions
from pipeline.project.verification_handoff_retry import (
    VerificationHandoffRetryBlocked,
    VerificationHandoffRetryContext,
)
from pipeline.run_state import retry_verification_handoff

#: ``state.extras`` key a pre-router resume step sets when it could not trust
#: the scheduled-gate ledger. The env retry is the one resume that *depends*
#: on the ledger as evidence, so it must fail closed on the marker instead of
#: reading the artifact the setup step already rejected.
ENV_RETRY_LEDGER_BLOCKED_KEY = "verification_env_retry_ledger_blocked"

#: The trigger an env retry may resolve. Any other trigger reaching this owner
#: is a routing defect, not an operator-recoverable state.
VERIFICATION_TRIGGER = "verification_gate_failed"

#: The wire action this module owns.
RETRY_VERIFICATION = "retry_verification"

#: Lowest command-receipt schema whose ``subject`` block carries the typed
#: identity this retry compares against. Anything older cannot prove the
#: subject at all.
_MIN_RECEIPT_SCHEMA_VERSION = 3

_DECISIONS_DIRNAME = "phase_handoff_decisions"


@dataclass(frozen=True, slots=True)
class EnvRetryLedgerBlocked:
    """Why a pre-router setup step declared the ledger untrustworthy."""

    reason: str

    def __str__(self) -> str:
        return self.reason


# ── pure detection (consumed by pre-router resume setup) ────────────────────


def is_env_retry_decision(
    active: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]],
) -> bool:
    """Whether ``active`` is a gate pause the operator decided to re-execute.

    Pure over the two facts a pre-router step can read without loading the
    engine: the active payload and the tolerantly-parsed decision artifacts.
    Both halves are required — a verification pause with no recorded
    ``retry_verification`` is an ordinary pause, and a ``retry_verification``
    decision for some *other* handoff id says nothing about this one.

    This is the *exact* predicate. Pre-router guards use
    :func:`is_env_retry_decision_candidate` instead: they must also cover the
    decision the strict reader will later reject.
    """
    handoff_id = _decided_verification_id(active)
    if handoff_id is None:
        return False
    return any(
        _decision_action(decision) == RETRY_VERIFICATION
        and decision.get("handoff_id") == handoff_id
        for decision in decisions
    )


def is_env_retry_decision_candidate(
    active: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]],
) -> bool:
    """Whether this resume must be *treated* as an env retry, damage included.

    Widens :func:`is_env_retry_decision` by one case: the decision artifact
    addressed to the active handoff records ``retry_verification`` while its
    own persisted ids disagree with the path it sits at. The strict reader
    refuses that artifact — correctly; it is corrupted audit evidence — but it
    does so *after* the pre-router guards that keep the retained subject alive.
    A predicate matching only intact records would hand a damaged env retry to
    the ordinary resume path, which may legitimately mint a fresh checkout, and
    the subject those receipts observed would be gone before anyone read the
    decision at all.

    So the fail-closed question is not "is this a valid env retry?" but "does
    anything on disk claim this pause was decided ``retry_verification``?".
    Answering yes costs an ordinary resume nothing — it only retains a subject
    and re-parks a pause — while answering no can destroy the evidence.
    """
    handoff_id = _decided_verification_id(active)
    if handoff_id is None:
        return False
    if is_env_retry_decision(active, decisions):
        return True
    stem = _artifact_stem(handoff_id)
    if stem is None:
        return False
    return any(
        _decision_action(decision) == RETRY_VERIFICATION
        and decision.get("artifact_stem") == stem
        for decision in decisions
    )


def _decided_verification_id(active: Mapping[str, Any]) -> str | None:
    """The active verification pause's id, or ``None`` when it is not one."""
    if not isinstance(active, Mapping):
        return None
    if active.get("trigger") != VERIFICATION_TRIGGER:
        return None
    handoff_id = active.get("id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return None
    return handoff_id


def _decision_action(decision: Any) -> str | None:
    return decision.get("action") if isinstance(decision, Mapping) else None


def _artifact_stem(handoff_id: str) -> str | None:
    """The decision-artifact filename stem the strict reader would address."""
    from sdk.phase_handoff import safe_handoff_id

    try:
        return safe_handoff_id(handoff_id)
    except ValueError:
        return None


def detect_env_retry_resume(
    meta_or_session: Mapping[str, Any], output_dir: Path | None,
) -> bool:
    """Whether a resume of ``output_dir`` is an env retry, read tolerantly.

    ``meta_or_session`` may be either the prior ``meta.json`` (read before
    session-init overwrites it) or the live session dict: both carry
    ``phase_handoff`` in the same shape, and a pre-router caller legitimately
    holds one or the other. Decision artifacts are read as
    ``resume_worktree._read_decisions`` does — a missing directory or one
    unparseable file never breaks the scan, because this predicate only
    *widens* a guard and a false negative degrades to existing behaviour.

    Uses the *candidate* predicate: a pre-router step has to hold the env-retry
    boundary open for a decision whose record is damaged, since the component
    that would notice the damage runs later than the guards this feeds.
    """
    if not isinstance(meta_or_session, Mapping):
        return False
    active = meta_or_session.get("phase_handoff")
    if not isinstance(active, Mapping):
        return False
    if output_dir is None:
        return False
    return is_env_retry_decision_candidate(
        active, _read_decisions(Path(output_dir)),
    )


def _read_decisions(run_dir: Path) -> list[dict[str, Any]]:
    """``{action, handoff_id, artifact_stem}`` per decision artifact, tolerantly.

    ``artifact_stem`` is the filename the strict reader addresses by handoff
    id, carried so a caller can tell "a decision for some other handoff" from
    "*this* handoff's decision, with a corrupted id inside".
    """
    decisions_dir = run_dir / _DECISIONS_DIRNAME
    if not decisions_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(decisions_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not (entry.is_file() and entry.suffix == ".json"):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            out.append({
                "action": raw.get("action"),
                "handoff_id": raw.get("handoff_id"),
                "artifact_stem": entry.stem,
            })
    return out


# ── the resume arm ──────────────────────────────────────────────────────────


def apply_verification_env_retry_resume(
    *,
    run: Any,
    profile: Any,
    active: dict[str, Any],
    handoff_id: str,
    note: str | None,
    decided_at: str | None,
) -> Any:
    """Re-execute the decided gate set, or re-park the pause with the reason.

    The thin arm the resume router enters. It exists to make every control-plane
    block land in exactly one place: a fresh, decidable pause carrying the block
    reason. The old decision stays immutable audit evidence — its id cannot be
    reused, because the re-parked handoff must offer the actions the record can
    still prove.
    """
    if active.get("trigger") != VERIFICATION_TRIGGER:
        raise RuntimeError(
            f"env retry resume reached with trigger {active.get('trigger')!r}, "
            f"expected {VERIFICATION_TRIGGER!r}",
        )
    try:
        return apply_verification_env_retry(
            run=run, profile=profile, active=active, handoff_id=handoff_id,
            note=note, decided_at=decided_at,
        )
    except VerificationHandoffRetryBlocked as exc:
        from pipeline.project.gate_repair import (
            repark_verification_handoff_retry_blocked,
        )

        repark_verification_handoff_retry_blocked(
            run, profile=profile, active=active, reason=str(exc),
        )
        return _outcome(profile, completed=frozenset(), paused=True)


def repark_unreadable_env_retry_decision(
    *,
    run: Any,
    profile: Any,
    active: dict[str, Any],
    handoff_id: str,
    error: Exception,
) -> Any | None:
    """Re-park a decided env retry whose decision artifact failed strict reading.

    Returns ``None`` when nothing on disk claims this pause was decided
    ``retry_verification`` — the caller then keeps its existing hard failure,
    which is the right answer for every other action: a corrupted decision is
    not something resume may guess around.

    For an env retry it is the wrong answer. The pre-router guards already held
    the retained subject for this decision (see
    :func:`is_env_retry_decision_candidate`), so the run still has everything it
    needs to be decided again — and the one thing the operator must be told is
    that the audit record, not the gate set, is what blocked. A hard resume
    failure would say neither, and would leave the pause undecidable.
    """
    output_dir = getattr(run, "output_dir", None)
    decisions = _read_decisions(Path(output_dir)) if output_dir is not None else []
    if not is_env_retry_decision_candidate(active, decisions):
        return None
    from pipeline.project.gate_repair import (
        repark_verification_handoff_retry_blocked,
    )

    repark_verification_handoff_retry_blocked(
        run, profile=profile, active=active,
        reason=(
            f"the recorded retry_verification decision for {handoff_id!r} "
            f"failed strict validation: {error}"
        ),
    )
    return _outcome(profile, completed=frozenset(), paused=True)


def apply_verification_env_retry(
    *,
    run: Any,
    profile: Any,
    active: dict[str, Any],
    handoff_id: str,
    note: str | None,
    decided_at: str | None,
) -> Any:
    """Prove the decision, then re-execute the persisted blocking set.

    Every check precedes the transition, so a block leaves the active handoff
    and its decision exactly as they were and zero gate commands have run.
    ``AgentCallError`` is deliberately re-raised: the env retry dispatches no
    agent, so one surfacing from the shared executor keeps its established
    interrupted/failed lifecycle rather than becoming a re-park.
    """
    _reject_untrusted_ledger(run)
    identities, evidence = _decided_gate_set(run, active, handoff_id)
    run_dir = _prove_ledger_evidence(run, identities, evidence)
    expected_head = _prove_receipt_evidence(run_dir, identities, evidence)
    _prove_retained_subject(run, expected_head)
    ctx = _retry_context(active, identities)
    # Proven before anything runs: a passing rerun must have a continuation
    # position to hand back, and "the gates are green but the engine cannot say
    # where to resume" is not a state worth creating.
    continuation = _prove_continuation_position(profile, active)

    transition = retry_verification_handoff(
        run.session, handoff_id=handoff_id, note=note, decided_at=decided_at,
    )
    run.state.extras["phase_handoff_override"] = transition.override
    from pipeline.project.handoff import _persist_handoff_running_state

    _persist_handoff_running_state(run)

    from pipeline.project import gate_repair

    try:
        passed = gate_repair.rerun_verification_handoff_gate(
            run, retry_context=ctx, profile=profile,
            # If these gates fail again the fresh pause is published from
            # outside the loop that owned the round, where no live stamp
            # exists. Hand over the boundary this resume just proved: nothing
            # has executed since, so it is still the run's real position, and
            # the next operator decision stays executable.
            carry_loop_position=_recorded_loop_position(active),
        )
    except AgentCallError:
        # Provider/process failures keep their established interrupted/failed
        # lifecycle. An env retry dispatches no agent, so one surfacing here
        # came from the shared executor and is not an operator-recoverable
        # control-plane state to re-park.
        raise
    except (RuntimeError, ValueError) as exc:
        # Identity/ledger/dispatch configuration errors are control-plane
        # blockers. Re-expose the original subject rather than consuming it.
        _restore_recovery_subject(run, active)
        raise VerificationHandoffRetryBlocked(str(exc)) from exc
    if not passed:
        # The rerun published a fresh signal/id; keep it active for the normal
        # pause persistence tail rather than clearing it as consumed.
        return _outcome(profile, completed=frozenset(), paused=True)
    return _continuation_outcome(run, profile, continuation)


@dataclass(frozen=True, slots=True)
class _Continuation:
    """Where the pipeline picks up once the re-measured gates are green.

    ``completed`` are the phases the walk must treat as done *and* leave
    unannounced; ``cursor`` positions the loop that contains the raising phase
    at the member after it; ``quiet_loop`` names the loop members ahead that
    must stay silent when the round no longer needs them.
    """

    completed: frozenset[str]
    cursor: Any | None
    quiet_loop: frozenset[str]
    strip_plan_loop: bool
    #: ``(loop_key, extra_rounds)`` when the position lives in a round the
    #: loop's *declared* budget does not cover — a human-directed retry
    #: extended it. The runner re-derives that budget from state, which a
    #: fresh process no longer carries, so the continuation restores it.
    human_directed_rounds: tuple[str, int] | None = None


def _prove_continuation_position(profile: Any, active: Mapping[str, Any]) -> _Continuation:
    """Locate the exact resume point, or block before a single gate runs.

    Everything before the raising phase is finished work: re-entering any of it
    would spend an agent round the operator never asked for. Whether the raising
    phase itself is behind the resume point depends on the gate's hook — an
    ``after_phase`` gate reported on a phase that ran, while a ``before_phase``
    / ``before_delivery`` gate *guards* one that has not — and getting that
    backwards would let a green rerun walk past the phase the gate protected.

    On top of that, two shapes have to be told apart:

    * a **top-level** raising phase — everything through it is complete, and a
      loop before it is complete as a whole;
    * a raising phase **inside a loop** — the round stopped between members, a
      position only a validated cursor can express. The evidence for it is the
      loop position routing recorded at pause time
      (:data:`~pipeline.project.gate_handoff_actions.LOOP_POSITION_KEY`); it is
      checked against the live profile, because a profile edited between pause
      and resume would otherwise silently re-run or skip members.

    Which member is still owed is read from what the round *executed*, never
    from where the raising phase sits in the declaration: a ``retry_feedback``
    round repairs before it reviews, so its repair is the later member and yet
    the review is the one outstanding.

    A round that ran every member is the loop's own boundary question, and "the
    round ended" is not "the loop ended". Only a satisfied ``until`` or an
    exhausted round budget closes it; otherwise the profile still owes rounds,
    and walking past the loop would carry unapproved work forward.

    A loop-internal pause whose position is missing or disagrees with the
    profile is blocked here — before the transition, before any gate executes.
    The engine has no round-level resume it can invent, and continuing from a
    guessed member is exactly the re-execution this action exists to avoid.
    """
    phase = active.get("phase")
    if not (isinstance(phase, str) and phase):
        raise VerificationHandoffRetryBlocked(
            "the paused handoff does not name the phase its gates ran for",
        )
    protected = _pause_guards_its_phase(active)
    steps = getattr(profile, "steps", None)
    if not steps:
        # A profile shape with no walkable steps (direct-dispatch test doubles):
        # nothing to position, and the phase name alone is the whole answer.
        completed = frozenset() if protected else frozenset({phase})
        return _Continuation(completed, None, frozenset(), False)

    enclosing, members = _enclosing_loop(steps, phase)
    quiet_loop = _quiet_loop_members(steps, phase, enclosing)
    if enclosing is None:
        target = next(
            (step for step in steps if getattr(step, "phase", None) == phase),
            None,
        )
        if target is None:
            if protected:
                raise VerificationHandoffRetryBlocked(
                    f"the gates guard phase {phase!r}, which this profile does "
                    "not declare; the engine cannot say what the retry would "
                    "let run next",
                )
            return _Continuation(frozenset({phase}), None, quiet_loop, False)
        before = _phases_before(steps, target)
        # A gate that guards a phase pauses *before* it: the phase is still
        # owed, and reporting it completed would let a green rerun walk past
        # the very phase the gate was protecting.
        completed = before if protected else before | frozenset({phase})
        return _Continuation(
            completed, None, quiet_loop, "plan" in completed,
        )

    position = _validated_loop_position(
        active, enclosing, members, phase, protected=protected,
    )
    before_loop = _phases_before(steps, enclosing)
    pending = [member for member in members if member not in position.executed]
    if pending:
        # The round is unfinished: resume at the first member it still owes.
        # Whatever ran out of order — the repair of a human-directed review
        # retry, which runs before its review — is carried as ``done_phases``
        # so the round skips it without running it twice.
        return _Continuation(
            before_loop,
            _cursor(enclosing, members, position.round, pending[0],
                    executed=position.executed),
            quiet_loop,
            False,
            _human_directed_rounds(enclosing, position.budget),
        )
    # The round ran every member, so what remains is the loop's own boundary
    # question — the one the runner asks at every round end. It was answered at
    # pause time, while the verdict was still live: a satisfied ``until`` closes
    # the loop, and so does an exhausted round budget.
    if position.until_satisfied or position.round >= position.budget:
        return _Continuation(
            before_loop | frozenset(members), None, quiet_loop, False,
        )
    # Otherwise the loop owes another round. Resume at its first member in the
    # *next* round: the completed round is not replayed, and the rounds the
    # profile still promises are not silently dropped.
    return _Continuation(
        before_loop,
        _cursor(enclosing, members, position.round + 1, members[0]),
        quiet_loop,
        False,
        _human_directed_rounds(enclosing, position.budget),
    )


def _cursor(
    loop: Any,
    members: list[str],
    round_n: int,
    next_phase: str,
    *,
    executed: tuple[str, ...] = (),
) -> Any:
    """A cursor resuming ``round_n`` at ``next_phase``.

    Members that ran in declared order become the ordered prefix the runner
    already understands; anything executed out of that order is named
    separately, because a prefix cannot describe "the second member ran and the
    first is still owed".
    """
    from pipeline.runtime.resume import LoopResumeCursor

    prefix: list[str] = []
    for member in members:
        if member not in executed or member == next_phase:
            break
        prefix.append(member)
    return LoopResumeCursor(
        loop_key=loop.round_extras_key,
        loop_phases=tuple(members),
        round_n=round_n,
        completed_phases=tuple(prefix),
        next_phase=next_phase,
        source="phase_handoff",
        done_phases=frozenset(set(executed) - set(prefix)),
    )


def _human_directed_rounds(loop: Any, budget: int) -> tuple[str, int] | None:
    """Extra rounds the loop needs on top of its declared budget, if any.

    Read from the *budget* the pause recorded, never from the round it stopped
    in: an operator who granted two extra rounds and paused in the first still
    has the second coming, and deriving the extension from the current round
    would quietly retire it.
    """
    declared = getattr(loop, "max_rounds", 0)
    if not isinstance(declared, int) or budget <= declared:
        return None
    return (loop.round_extras_key, budget - declared)


#: Gate hooks that fire *before* the phase they guard, leaving it still owed.
_PRE_PHASE_HOOKS = frozenset({"before_phase", "before_delivery"})


def _recorded_loop_position(active: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The loop position this pause persisted, or ``None`` for a top-level one."""
    artifacts = active.get("artifacts")
    recorded = (
        artifacts.get(gate_handoff_actions.LOOP_POSITION_KEY)
        if isinstance(artifacts, Mapping)
        else None
    )
    return recorded if isinstance(recorded, Mapping) else None


def _primary_hook(active: Mapping[str, Any]) -> Any:
    """The hook of the pause's primary gate identity, unvalidated."""
    artifacts = active.get("artifacts")
    primary = artifacts.get("gate_identity") if isinstance(artifacts, Mapping) else None
    return primary.get("hook") if isinstance(primary, Mapping) else None


def _pause_guards_its_phase(active: Mapping[str, Any]) -> bool:
    """Whether the gates ran *before* the phase the pause names.

    ``before_phase`` / ``before_delivery`` gates guard a phase that has not run
    yet; ``after_phase`` gates report on one that has. The continuation point
    sits on opposite sides of the phase in the two cases, so a retry that read
    the phase name alone would let a green rerun walk straight past the very
    phase the gate was protecting.
    """
    return _primary_hook(active) in _PRE_PHASE_HOOKS


def _enclosing_loop(steps: Any, phase: str) -> tuple[Any, list[str]]:
    """The loop step containing ``phase`` and its member order, or ``(None, [])``."""
    for step in steps:
        members = [
            inner.phase
            for inner in getattr(step, "steps", ()) or ()
            if isinstance(getattr(inner, "phase", None), str)
        ]
        if phase in members:
            return step, members
    return None, []


def _phases_before(steps: Any, target: Any) -> frozenset[str]:
    """Every phase name declared before ``target`` in the walked profile."""
    names: set[str] = set()
    for step in steps:
        if step is target:
            break
        inner = [
            member.phase
            for member in getattr(step, "steps", ()) or ()
            if isinstance(getattr(member, "phase", None), str)
        ]
        if inner:
            names.update(inner)
        elif isinstance(getattr(step, "phase", None), str):
            names.add(step.phase)
    return frozenset(names)


def _quiet_loop_members(
    steps: Any, phase: str, enclosing: Any,
) -> frozenset[str]:
    """Loop members that may be reached with the loop already satisfied.

    Scoped to loops the continuation walks into *fresh*, ahead of the resume
    point. Only members after the first: reaching member 0 with ``until``
    already true says nothing about this round (the predicate still reflects
    the previous one), while reaching a later member does — its predecessors
    just ran. The loop the resume point itself sits in is excluded: its round
    is mid-flight, so its members answer for work this run really is doing.
    """
    names: set[str] = set()
    ahead = False
    for step in steps:
        members = [
            inner.phase
            for inner in getattr(step, "steps", ()) or ()
            if isinstance(getattr(inner, "phase", None), str)
        ]
        if ahead and members:
            names.update(members[1:])
        if step is enclosing or getattr(step, "phase", None) == phase:
            ahead = True
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class _LoopPosition:
    """The loop boundary the pause recorded, as the resume may rely on it."""

    round: int
    budget: int
    until_satisfied: bool
    #: Members this round ran, in the order it ran them. The declared order is
    #: not a substitute: a human-directed review retry repairs before it
    #: reviews, so which member is still owed is only readable from here.
    executed: tuple[str, ...]


def _validated_loop_position(
    active: Mapping[str, Any],
    loop: Any,
    members: list[str],
    phase: str,
    *,
    protected: bool,
) -> _LoopPosition:
    """The recorded loop boundary the pause stopped at, or a typed block."""
    artifacts = active.get("artifacts")
    recorded = (
        artifacts.get(gate_handoff_actions.LOOP_POSITION_KEY)
        if isinstance(artifacts, Mapping)
        else None
    )
    def _block(detail: str) -> VerificationHandoffRetryBlocked:
        return VerificationHandoffRetryBlocked(
            f"the gates ran inside loop {loop.round_extras_key!r}, and the "
            f"persisted record {detail}; the engine cannot resume a loop round "
            "it cannot locate",
        )

    if not isinstance(recorded, Mapping):
        raise _block("carries no loop position")
    if recorded.get("loop_key") != loop.round_extras_key:
        raise _block(f"names loop {recorded.get('loop_key')!r}")
    if list(recorded.get("loop_phases") or ()) != members:
        raise _block("describes a different member order")
    if recorded.get("phase") != phase:
        raise _block(f"names phase {recorded.get('phase')!r}")
    # The position and the identities describe the same pause, so they describe
    # the same hook. A position that claims another one would flip which side
    # of the member the round resumes on while the gates say otherwise.
    if recorded.get("hook") != _primary_hook(active):
        raise _block(
            f"names hook {recorded.get('hook')!r} while the gates ran at "
            f"{_primary_hook(active)!r}",
        )
    round_n = recorded.get("round")
    # Validated against the budget the pause recorded, not the loop's declared
    # ``max_rounds``: a human-directed retry legitimately runs past the
    # declaration, and a pause raised in such a round is still a real position.
    budget = recorded.get("budget")
    if not _positive_int(round_n):
        raise _block(f"records round {round_n!r}")
    if not _positive_int(budget) or round_n > budget:
        raise _block(f"records round budget {budget!r} for round {round_n!r}")
    satisfied = recorded.get("until_satisfied")
    if not isinstance(satisfied, bool):
        raise _block(f"records until_satisfied {satisfied!r}")
    executed = recorded.get("executed")
    if (
        not isinstance(executed, list)
        or len(set(executed)) != len(executed)
        or not set(executed) <= set(members)
    ):
        raise _block(f"records executed members {executed!r}")
    _reject_unprovable_order(
        recorded.get("mode"), members, tuple(executed), phase,
        protected=protected, block=_block,
    )
    return _LoopPosition(
        round=round_n,
        budget=budget,
        until_satisfied=satisfied,
        executed=tuple(executed),
    )


def _reject_unprovable_order(
    mode: Any,
    members: list[str],
    executed: tuple[str, ...],
    phase: str,
    *,
    protected: bool,
    block: Callable[[str], VerificationHandoffRetryBlocked],
) -> None:
    """Refuse any execution order the named dispatcher could not have produced.

    An order on its own is only a claim: "review then repair" is what the
    runner leaves *and* what a forged record would say to make a half-finished
    human-directed round look complete. So the record names the dispatcher, and
    each dispatcher admits exactly one family of orders — the runner and the
    plan retry walk the declared order, the human-directed review retry repairs
    then reviews. Anything outside the family its own mode allows, or a raising
    phase that is not the member that mode would be at, is refused before a
    gate runs.
    """
    from pipeline.runtime.runner import (
        LOOP_DISPATCH_DECLARED,
        LOOP_DISPATCH_REVIEW_RETRY,
    )

    if mode == LOOP_DISPATCH_DECLARED:
        order = tuple(members)
    elif mode == LOOP_DISPATCH_REVIEW_RETRY:
        if len(members) != 2:
            raise block(
                f"claims the review-retry order for a {len(members)}-member "
                "loop, which that dispatcher cannot drive",
            )
        order = (members[1], members[0])
    else:
        raise block(f"records dispatch mode {mode!r}")
    if executed != order[: len(executed)]:
        raise block(
            f"records executed members {list(executed)!r}, which "
            f"{mode!r} cannot produce",
        )
    # Where that dispatcher stands after this record, and therefore which
    # member the pause must name: the one it just ran, or — for a gate that
    # guards its member — the one it is about to run.
    expected = (
        order[len(executed)] if len(executed) < len(order) else None
    ) if protected else (executed[-1] if executed else None)
    if phase != expected:
        raise block(
            f"names phase {phase!r} where {mode!r} would be at "
            f"{expected!r} after {list(executed)!r}",
        )


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _continuation_outcome(
    run: Any, profile: Any, continuation: _Continuation,
) -> Any:
    """Resume the ordinary pipeline *after* the phase whose gates just passed.

    The gates measured work that is already done; nothing behind them may run
    again. So every phase up to and including the one that raised the pause is
    reported completed — and reported *silently*: an ordinary resume-skip fires
    the trace callbacks so an operator sees why a phase did not re-run, but here
    a ``phase.start`` for ``implement`` would announce an agent round this
    resume is forbidden to take, to every events.jsonl consumer. The phase log
    still records the skip, so the run's own account stays complete.

    A fully covered plan loop is dropped from the walked profile rather than
    merely marked completed — the same treatment the gate ``continue`` /
    ``continue_with_waiver`` arms give it — because a loop that is still in the
    profile can be re-entered by a persisted round cursor. Its parsed plan is
    rehydrated for the phases ahead, which no longer have a plan handler to
    produce it.

    Everything after the resume point is untouched: the rest of the interrupted
    round, the review loop and the terminal gate run exactly as they would have
    had the gates passed the first time. That is the continuation this action
    promises — not a shortcut past the work that was never done.
    """
    from pipeline.project.handoff import rehydrate_parsed_plan, strip_plan_loop
    from pipeline.project.resume_artifacts import RESUME_PLAN_REQUIRED_KEY

    if continuation.human_directed_rounds is not None:
        # The runner derives a loop's effective budget from state, and a
        # fresh-process resume no longer carries what the retry that extended
        # it wrote. Restore exactly the extension the recorded position proves,
        # so the cursor lands inside the budget instead of being refused.
        from pipeline.runtime.handoff import HUMAN_DIRECTED_ROUNDS_KEY

        loop_key, extra = continuation.human_directed_rounds
        bag = run.state.extras.get(HUMAN_DIRECTED_ROUNDS_KEY)
        bag = dict(bag) if isinstance(bag, dict) else {}
        bag[loop_key] = max(int(bag.get(loop_key, 0) or 0), extra)
        run.state.extras[HUMAN_DIRECTED_ROUNDS_KEY] = bag
    if continuation.strip_plan_loop:
        # Marked required first: the plan phases are reported completed only by
        # this outcome, so the state-build bootstrap did not see them and would
        # not have surfaced a missing/corrupt artifact on its own.
        run.state.extras[RESUME_PLAN_REQUIRED_KEY] = True
        rehydrate_parsed_plan(run)
        profile = strip_plan_loop(profile)
    return _outcome(
        profile,
        completed=continuation.completed,
        paused=False,
        silent=continuation.completed,
        quiet_loop=continuation.quiet_loop,
        cursor=continuation.cursor,
    )


# ── 1. the untrusted-ledger marker ──────────────────────────────────────────


def _reject_untrusted_ledger(run: Any) -> None:
    """Block on a marker a pre-router step left, without reading the ledger.

    The marker means an earlier setup step already looked at the artifact this
    retry would use as evidence and refused it. Re-reading it here to "check
    for ourselves" would be the whole failure mode: two readers disagreeing
    about the same file, with the laxer one winning.
    """
    extras = getattr(getattr(run, "state", None), "extras", None)
    if not isinstance(extras, Mapping):
        return
    marker = extras.get(ENV_RETRY_LEDGER_BLOCKED_KEY)
    if marker is None:
        return
    reason = getattr(marker, "reason", None)
    if not isinstance(reason, str) or not reason.strip():
        reason = str(marker)
    raise VerificationHandoffRetryBlocked(
        f"scheduled-gate ledger is not trusted for this resume: {reason}",
    )


# ── 2. the decided gate set ─────────────────────────────────────────────────


def _decided_gate_set(
    run: Any, active: Mapping[str, Any], handoff_id: str,
) -> tuple[tuple[GateIdentity, ...], dict[GateIdentity, str]]:
    """The persisted blocking set + its evidence pointers, or a typed block."""
    persisted = run.session.get("phase_handoff")
    if not isinstance(persisted, dict) or persisted.get("id") != handoff_id:
        raise VerificationHandoffRetryBlocked(
            "active recovery subject no longer matches decision",
        )
    available = active.get("available_actions")
    if not isinstance(available, list | tuple) or RETRY_VERIFICATION not in available:
        raise VerificationHandoffRetryBlocked(
            "the paused handoff did not offer retry_verification",
        )
    artifacts = active.get("artifacts")
    if not gate_handoff_actions.env_retry_admissible(artifacts):
        raise VerificationHandoffRetryBlocked(
            "persisted gate record does not prove an env-only retryable set",
        )
    # Admissibility already proved both are complete; the parsers lead with the
    # primary read from ``artifacts['gate_identity']``.
    identities = gate_handoff_actions.persisted_gate_identities(artifacts)
    evidence = gate_handoff_actions.persisted_gate_evidence(artifacts)
    assert identities is not None and evidence is not None
    _reject_incoherent_gate_scope(active, identities)
    return identities, evidence


def _reject_incoherent_gate_scope(
    active: Mapping[str, Any], identities: tuple[GateIdentity, ...],
) -> None:
    """Tie the pause's own phase to the gates it says it blocked on.

    Every check after this one reads the *identities*: the ledger rows, the
    receipts, the subject. The continuation, by contrast, reads the pause's
    ``phase`` — which is what decides how much of the pipeline is behind the
    resume point. Left unbound, those two halves can disagree: a record whose
    gates still prove ``after_phase(implement)`` but whose phase was moved to
    ``final_acceptance`` re-runs the right commands and then reports the review
    loop and the terminal gate complete, ending a run that never verified them.

    So the scope has to be one scope. Every identity names the same hook and
    the same phase — one hook evaluation raised this pause, and a set spanning
    two of them describes no single position — and the pause's phase is the one
    routing derives from that hook (:func:`~pipeline.project.gate_repair._handoff_phase`,
    the same rule on both sides rather than a second opinion about it).
    """
    from pipeline.project.gate_repair import _handoff_phase

    hooks = {identity.hook for identity in identities}
    phases = {identity.phase for identity in identities}
    if len(hooks) != 1 or len(phases) != 1:
        raise VerificationHandoffRetryBlocked(
            f"the decided gate set spans hooks {sorted(hooks)} and phases "
            f"{sorted(phases)}: one pause is raised by one hook evaluation, so "
            "this record names no single position to re-measure",
        )
    hook, gate_phase = next(iter(hooks)), next(iter(phases))
    expected = _handoff_phase(hook, gate_phase)
    if active.get("phase") != expected:
        raise VerificationHandoffRetryBlocked(
            f"the paused handoff names phase {active.get('phase')!r}, but its "
            f"gates were scheduled at {hook!r}/{gate_phase or '-'!r}, which "
            f"raises a pause at {expected!r}; the record's phase and its gates "
            "do not describe the same position",
        )
    primary_hook = _primary_hook(active)
    if primary_hook != hook:
        raise VerificationHandoffRetryBlocked(
            f"the primary gate identity names hook {primary_hook!r} while the "
            f"decided set ran at {hook!r}",
        )


# ── 3. the ledger must agree with the record ────────────────────────────────


def _prove_ledger_evidence(
    run: Any,
    identities: tuple[GateIdentity, ...],
    evidence: Mapping[GateIdentity, str],
) -> Path:
    """Prove the ledger agrees, and return the run dir the evidence is rooted at.

    Every identity must be a durably *selected* row whose latest execution both
    failed and points at the receipt the handoff recorded.
    """
    from pipeline.verification_ledger_store import (
        LedgerStoreError,
        ledger_path,
        load_ledger,
    )

    output_dir = getattr(run, "output_dir", None)
    if output_dir is None:
        raise VerificationHandoffRetryBlocked(
            "verification retry has no run directory to read gate evidence from",
        )
    run_dir = Path(output_dir)
    if not ledger_path(run_dir).exists():
        raise VerificationHandoffRetryBlocked(
            "verification retry has no scheduled-gate ledger to prove its gate set",
        )
    try:
        ledger = load_ledger(run_dir)
    except (LedgerStoreError, OSError) as exc:
        raise VerificationHandoffRetryBlocked(
            f"scheduled-gate ledger is unreadable: {exc}",
        ) from exc

    rows = {row.identity: row for row in ledger.rows}
    for identity in identities:
        key = (identity.command, identity.hook, identity.phase)
        row = rows.get(key)
        if row is None:
            raise VerificationHandoffRetryBlocked(
                f"scheduled-gate ledger has no row for decided identity {key!r}",
            )
        if row.selected is not True:
            raise VerificationHandoffRetryBlocked(
                f"scheduled-gate identity {key!r} was never durably selected",
            )
        executions = [
            event
            for event in ledger.trail
            if event.kind == "execution" and event.identity == key
        ]
        if not executions:
            raise VerificationHandoffRetryBlocked(
                f"scheduled-gate identity {key!r} has no recorded execution to retry",
            )
        last = executions[-1]
        if last.outcome != "fail":
            raise VerificationHandoffRetryBlocked(
                f"scheduled-gate identity {key!r} last executed "
                f"{last.outcome!r}: the decided failure evidence is stale",
            )
        if last.receipt_evidence != evidence[identity]:
            raise VerificationHandoffRetryBlocked(
                f"scheduled-gate identity {key!r} points at receipt evidence "
                f"{last.receipt_evidence!r}, but the decision recorded "
                f"{evidence[identity]!r}",
            )
    return run_dir


# ── 4. the receipts must still be the failures that were decided ────────────


def _prove_receipt_evidence(
    run_dir: Path,
    identities: tuple[GateIdentity, ...],
    evidence: Mapping[GateIdentity, str],
) -> str | None:
    """Validate every receipt strictly; return the HEAD they agree on.

    Returns ``None`` only when *every* receipt in the set recorded an
    unavailable subject — an honest "the subject was never observable", which
    weakens the HEAD comparison but leaves the retained-worktree checks in
    place. A mix of available HEADs that disagree is a block: the set would
    have been measured across two different trees.
    """
    from pipeline.verification_failure import classify_receipt

    heads: set[str] = set()
    for identity in identities:
        relative = evidence[identity]
        receipt, head = _validated_receipt(run_dir, identity, relative)
        classification = classify_receipt(receipt)
        if classification.status != "failed" or (
            classification.failure_kind != gate_handoff_actions.ENV_FAILURE_KIND
        ):
            raise VerificationHandoffRetryBlocked(
                f"receipt {relative!r} for {identity.command!r} classifies as "
                f"{classification.status}/{classification.failure_kind}, not a "
                "failed env_failure the engine may re-execute",
            )
        if head is not None:
            heads.add(head)
    if len(heads) > 1:
        raise VerificationHandoffRetryBlocked(
            "the decided receipts observed different subjects "
            f"({sorted(heads)}): there is no single tree to re-measure",
        )
    return next(iter(heads), None)


def _validated_receipt(
    run_dir: Path, identity: GateIdentity, relative: str,
) -> tuple[dict[str, Any], str | None]:
    """Parse one receipt in strict form, before any classification.

    Strict form first, deliberately: :func:`classify_receipt` is tolerant by
    design (it must classify historical and hand-built receipts), so it would
    happily read ``{}`` as a missing-exit-code ``env_failure`` — the exact
    shape this retry is looking for. Only a receipt whose fields are all
    actually present and typed is evidence of anything.

    Returns ``(receipt, observed_head_oid)``, with ``None`` for a receipt that
    explicitly recorded an unavailable subject.
    """
    def _block(detail: str) -> VerificationHandoffRetryBlocked:
        return VerificationHandoffRetryBlocked(
            f"receipt {relative!r} for {identity.command!r} {detail}",
        )

    evidence_file = _contained_evidence_path(run_dir, relative, _block)
    try:
        raw = json.loads(evidence_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _block(f"is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise _block("is not a receipt object")
    schema_version = raw.get("schema_version")
    if not _is_int(schema_version) or schema_version < _MIN_RECEIPT_SCHEMA_VERSION:
        raise _block(
            f"has schema_version {schema_version!r}; a subject-carrying "
            f"receipt is v{_MIN_RECEIPT_SCHEMA_VERSION} or newer",
        )
    command = raw.get("command")
    if not isinstance(command, str) or command != identity.command:
        raise _block(f"records command {command!r}")
    if "exit_code" not in raw or not (
        raw["exit_code"] is None or _is_int(raw["exit_code"])
    ):
        raise _block("has no int-or-null exit_code")
    if not isinstance(raw.get("assertions"), list):
        raise _block("has no assertions list")
    if not isinstance(raw.get("detail"), str):
        raise _block("has no detail string")
    return raw, _validated_subject_head(raw.get("subject"), _block)


def _contained_evidence_path(
    run_dir: Path,
    relative: str,
    block: Callable[[str], VerificationHandoffRetryBlocked],
) -> Path:
    """Resolve ``relative`` inside this run's immutable execution evidence.

    ``receipt_evidence`` is written as a run-dir-relative pointer into
    ``verification_command_receipts/executions/``, and the retry's whole claim
    is that it re-executes what *this* run recorded. A pointer is a string in a
    record that a crash or a hand-edit can rewrite, so it is resolved rather
    than joined: an absolute path, a ``..`` segment, or a symlink leading out
    of the evidence directory would let a handoff and a ledger that agree with
    each other name a perfectly valid receipt belonging to some other run, and
    the retry would then "prove" its gate set against evidence this run never
    produced.

    Resolution itself can fail — a symlink loop raises ``RuntimeError``, a
    broken traversal ``OSError`` — and a pointer the filesystem cannot even
    resolve is exactly as unproven as one that resolves elsewhere. Both become
    the same typed block, because an unresolvable pointer must leave the
    operator a decidable pause, not a hard resume failure.
    """
    from pipeline.evidence.verification_receipt import (
        COMMAND_RECEIPT_EXECUTIONS_DIRNAME,
        COMMAND_RECEIPTS_DIRNAME,
    )

    pointer = Path(relative)
    if pointer.is_absolute() or pointer.drive or ".." in pointer.parts:
        raise block(
            f"points outside the run directory via {relative!r}",
        )
    executions_dir = (
        run_dir / COMMAND_RECEIPTS_DIRNAME / COMMAND_RECEIPT_EXECUTIONS_DIRNAME
    )
    try:
        resolved = (run_dir / pointer).resolve()
        root = executions_dir.resolve()
    except (OSError, RuntimeError) as exc:
        raise block(f"cannot be resolved: {exc}") from exc
    if not resolved.is_relative_to(root):
        raise block(
            "resolves outside this run's execution evidence directory",
        )
    if not resolved.is_file():
        raise block("is not a regular evidence file")
    return resolved


def _validated_subject_head(
    subject: Any, block: Callable[[str], VerificationHandoffRetryBlocked],
) -> str | None:
    """The receipt subject's observed HEAD, or ``None`` when explicitly unavailable."""
    from pipeline.evidence.verification_receipt import subject_identity

    if not isinstance(subject, dict):
        raise block("has no subject block")
    status = subject.get("status")
    if status == "unavailable":
        if not isinstance(subject.get("reason"), str):
            raise block("records an unavailable subject with no reason")
        return None
    if status != "available":
        raise block(f"records subject status {status!r}")
    if not isinstance(subject.get("identity"), dict):
        raise block("records an available subject with no identity object")
    parsed = subject_identity(subject)
    if parsed is None:
        raise block("records an available subject whose identity is unusable")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (parsed.object_format, parsed.tree_oid, parsed.observed_head_oid)
    ):
        raise block("records an available subject with an incomplete identity")
    return parsed.observed_head_oid


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ── 5. the retained subject ─────────────────────────────────────────────────


def _prove_retained_subject(run: Any, expected_head: str | None) -> None:
    """The gate will re-measure the same checkout the receipts observed."""
    from pipeline.project.retry_subject import (
        RepairSubjectUnproven,
        ensure_verification_subject_retained,
    )

    cwd = run.state.extras.get("git_cwd") or run.state.project_dir
    try:
        ensure_verification_subject_retained(
            cwd=str(cwd),
            worktree_block=run.session.get("worktree"),
            expected_head=expected_head,
        )
    except RepairSubjectUnproven as exc:
        raise VerificationHandoffRetryBlocked(str(exc)) from exc


# ── the retry context ───────────────────────────────────────────────────────


def _retry_context(
    active: Mapping[str, Any], identities: tuple[GateIdentity, ...],
) -> VerificationHandoffRetryContext:
    """Build the shared retry context and pin it to the proven set.

    ``from_active`` re-reads ``gate_identities`` through its own fallback, so
    the result is re-checked against the set this module proved: if the two
    readers ever disagreed, the rerun would execute a set no evidence covers.
    """
    ctx = VerificationHandoffRetryContext.from_active(active, identities[0])
    if set(ctx.identities) != set(identities):
        raise VerificationHandoffRetryBlocked(
            "retry context resolved a different gate set than the proven one",
        )
    return ctx


def _restore_recovery_subject(run: Any, active: Mapping[str, Any]) -> None:
    """Durably re-expose a consumed subject after a control-plane failure."""
    from pipeline.project.handoff import _persist_decidable_after_guard_abort

    run.session["phase_handoff"] = dict(active)
    _persist_decidable_after_guard_abort(run)


def _outcome(
    profile: Any,
    *,
    completed: frozenset[str],
    paused: bool,
    silent: frozenset[str] = frozenset(),
    quiet_loop: frozenset[str] = frozenset(),
    cursor: Any | None = None,
) -> Any:
    # Lazy to avoid a circular import at module load; the existing outcome DTO
    # is still the public handoff contract.
    from pipeline.project.handoff import PhaseHandoffResumeOutcome

    return PhaseHandoffResumeOutcome(
        profile,
        completed,
        paused,
        silent_completed_phases=silent,
        quiet_loop_phases=quiet_loop,
        loop_resume_cursors=(
            {cursor.loop_key: cursor} if cursor is not None else {}
        ),
    )


__all__ = [
    "ENV_RETRY_LEDGER_BLOCKED_KEY",
    "EnvRetryLedgerBlocked",
    "apply_verification_env_retry",
    "apply_verification_env_retry_resume",
    "detect_env_retry_resume",
    "is_env_retry_decision",
    "is_env_retry_decision_candidate",
    "repark_unreadable_env_retry_decision",
]
