"""``retry_verification`` resume: proof before re-execution, re-park otherwise.

Every case drives the *public* seam (``handoff.apply_phase_handoff_resume``
with a persisted decision), because the routing position of the env-retry arm
— before the ledger is read — is part of what is under test.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.control.handoff_routing import GateIdentity
from pipeline.plugins import PluginConfig
from pipeline.project import gate_handoff_actions, verification_env_retry
from pipeline.project.verification_env_retry import (
    ENV_RETRY_LEDGER_BLOCKED_KEY,
    EnvRetryLedgerBlocked,
)
from pipeline.project.verification_ledger_runtime import (
    initialize,
    record_execution,
    select_epoch,
)
from pipeline.verification_contract import VerificationContract
from pipeline.verification_ledger_store import load_ledger
from pipeline.verification_selection import SelectionContext

_HEAD = "a" * 40
_TREE = "b" * 40
_COMMANDS = ("lint", "pytest-unit", "typecheck")
_HOOK = "after_phase"
_PHASE = "implement"
_DECIDED_AT = "2026-09-17T10:00:00Z"


# ── fixtures / harness ──────────────────────────────────────────────────────


def _contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {name: {"run": f"{name} --check"} for name in _COMMANDS},
        "gate_sets": {"required": {"commands": list(_COMMANDS)}},
        "selection": [{"always": ["required"]}],
        "schedule": [
            {"after_phase": _PHASE, "gate_sets": ["required"], "policy": "require"},
            # A second *declared* identity for the same commands. It gives the
            # ledger a row the decided set could be re-pointed at without the
            # record looking malformed — the substitution the retry has to
            # catch by identity rather than by command name.
            {"before_delivery": True, "gate_sets": ["required"], "policy": "require"},
            # A gate that *guards* a loop member rather than reporting on one:
            # its pause leaves that member still owed.
            {"before_phase": "validate_plan", "gate_sets": ["required"],
             "policy": "require"},
            # Loop members the boundary tests pause on. A pause names the phase
            # its own gates were scheduled at, so a test that wants to stop
            # inside a loop has to schedule the gates there.
            *(
                {"after_phase": member, "gate_sets": ["required"],
                 "policy": "require"}
                for member in (
                    "plan", "validate_plan", "review_changes", "repair_changes",
                )
            ),
        ],
    }))
    assert contract is not None
    return contract


def _receipt_body(command: str, *, head: str = _HEAD) -> dict[str, Any]:
    """A v4 command receipt that classifies as a failed ``env_failure``.

    No exit code plus a non-empty ``detail`` is exactly what the executor
    writes when the command could not run at all — the shape an operator
    repairs outside the run and asks the engine to re-measure.
    """
    return {
        "schema_version": 4,
        "kind": "verification_command",
        "command": command,
        "env": "",
        "cwd": "/wt",
        "placeholders": {"checkout": "/wt", "project": "/wt"},
        "argv": [command, "--check"],
        "env_overrides": {},
        "assertions": [],
        "exit_code": None,
        "duration_s": 0.0,
        "stdout_tail": "",
        "stderr_tail": "",
        "log_path": None,
        "parity": "absolute",
        "detail": f"{command}: command not found",
        "outcome": "completed",
        "git": {"checkout_head": head, "baseline_head": None},
        "subject": {"status": "available", "identity": {
            "version": 1, "object_format": "sha1", "tree_oid": _TREE,
            "observed_head_oid": head, "baseline_oid": None,
        }},
        "dependencies": [],
    }


def _write_receipt(
    run_dir: Path,
    command: str,
    *,
    head: str = _HEAD,
    body: Any = None,
    hook: str = _HOOK,
    gate_phase: str = _PHASE,
) -> str:
    relative = (
        f"verification_command_receipts/executions/"
        f"{command}--{hook}--{gate_phase or 'none'}--0001.json"
    )
    payload = _receipt_body(command, head=head) if body is None else body
    path = run_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return relative


def _active(
    evidence: dict[str, str], *, hook: str = _HOOK, gate_phase: str = _PHASE,
) -> dict[str, Any]:
    commands = list(evidence)
    primary = commands[0]
    return {
        "id": f"gate:{primary}:1",
        "round": 1,
        "loop_max_rounds": 1,
        "round_extras_key": "repair_round",
        "phase": gate_phase or "final_acceptance",
        "trigger": "verification_gate_failed",
        "requested_at": "2026-09-17T09:00:00+00:00",
        "available_actions": [
            "retry_verification", "continue_with_waiver", "halt",
        ],
        "artifacts": {
            "gate_command": primary,
            "gate_set": "required",
            "gate_identity": {
                "command": primary, "hook": hook, "phase": gate_phase,
            },
            "gate_commands": commands,
            "gate_identities": [
                {
                    "command": command, "hook": hook, "phase": gate_phase,
                    "receipt_evidence": evidence[command],
                }
                for command in commands
            ],
            "findings": [
                {
                    "id": "verification_gate_env_failure",
                    "severity": "P3",
                    "title": "Verification gate env_failure",
                    "body": f"class=env_failure; exit_code=None ({command})",
                    "required_fix": "Fix the verification environment.",
                    "failure_kind": "env_failure",
                    "command": command,
                }
                for command in commands
            ],
            "short_summary": "\n".join(
                f"{command}: class=env_failure" for command in commands
            ),
        },
        "last_output": "3 required verification gates failed: " + ", ".join(commands),
    }


def _env_retry_run(
    tmp_path: Path,
    *,
    commands: tuple[str, ...] = _COMMANDS,
    head: str = _HEAD,
    hook: str = _HOOK,
    gate_phase: str = _PHASE,
) -> tuple[SimpleNamespace, Path, dict[str, str]]:
    """A paused env-failure gate handoff with a complete evidence chain.

    The ledger is built through the production runtime (``initialize`` /
    ``select_epoch`` / ``record_execution``) rather than hand-written, so the
    rows/trail this retry validates are the ones a real run would leave.
    """
    checkout = tmp_path / "wt"
    checkout.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    contract = _contract()
    state = SimpleNamespace(
        extras={"verification_contract": contract, "git_cwd": str(checkout)},
        output_dir=run_dir,
        project_dir=str(checkout),
        human_feedback="",
        halt=False,
        halt_reason="",
        last_critique="",
        last_test_output="",
        repair_feedback=None,
        phase_handoff_request=None,
        phase_log={},
    )
    state.stop = lambda reason: (
        setattr(state, "halt", True), setattr(state, "halt_reason", reason),
    )
    run = SimpleNamespace(
        session={"status": "awaiting_phase_handoff"},
        state=state,
        output_dir=run_dir,
        _ckpt=None,
        _metrics=SimpleNamespace(save=lambda _path: None),
        checkpoint_resume=False,
        project_alias=None,
    )
    initialize(state)
    plan = select_epoch(
        run, contract, epoch=f"{hook}:{gate_phase}", context=SelectionContext(),
    )
    # Keyed by command *within this epoch's identity*: the contract declares
    # the same commands under a second hook, and picking the wrong entry would
    # record the executions against an identity the handoff never blocked on.
    entries = {
        entry.command: entry
        for entry in plan.entries
        if (entry.hook, entry.phase) == (hook, gate_phase)
    }
    evidence: dict[str, str] = {}
    for command in commands:
        evidence[command] = _write_receipt(
            run_dir, command, head=head, hook=hook, gate_phase=gate_phase,
        )
        record_execution(
            run, entries[command], passed=False,
            receipt_evidence=evidence[command],
        )
    run.session["phase_handoff"] = _active(
        evidence, hook=hook, gate_phase=gate_phase,
    )
    run.session["worktree"] = {"isolation": "per_run", "path": str(checkout)}
    return run, checkout, evidence


def _decide(
    monkeypatch: pytest.MonkeyPatch, *, action: str = "retry_verification",
    feedback: str = "",
) -> None:
    from pipeline.project import handoff

    monkeypatch.setattr(
        handoff, "load_handoff_decision_validated",
        lambda *_args: SimpleNamespace(
            action=action, feedback=feedback, note="operator note",
            decided_at=_DECIDED_AT,
        ),
    )


def _forbid_subject_probes(monkeypatch: pytest.MonkeyPatch, *, head: str = _HEAD) -> None:
    """Pin the two git reads the retained-subject guard performs."""
    monkeypatch.setattr(
        "pipeline.project.retry_subject.git_head", lambda _cwd: head,
    )
    monkeypatch.setattr(
        "pipeline.project.retry_subject.is_worktree_reclaimed",
        lambda block: bool(block.get("reclaimed")),
    )


def _forbid_agent_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env retry runs no agent round and never re-reads the route ledger."""
    from pipeline.project import handoff

    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_a, **_k: pytest.fail("env retry must not dispatch a repair"),
    )
    monkeypatch.setattr(
        "pipeline.runtime.runner._dispatch_via_fsm",
        lambda *_a, **_k: pytest.fail("env retry must not enter the FSM"),
    )
    monkeypatch.setattr(
        handoff, "_scheduled_gate_identities",
        lambda _run: pytest.fail("env retry must route before the route ledger read"),
    )


def _resume(run: SimpleNamespace) -> Any:
    from pipeline.project import handoff

    return handoff.apply_phase_handoff_resume(run, profile=object(), ctx=object())


# ── (a) the happy path: one re-execution of the whole proven set ────────────


def test_env_retry_reruns_the_whole_proven_set_without_any_agent_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    reruns: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **kwargs: reruns.append(kwargs) or True,
    )

    outcome = _resume(run)

    assert len(reruns) == 1
    ctx = reruns[0]["retry_context"]
    assert set(ctx.identities) == {
        GateIdentity(command, _HOOK, _PHASE) for command in _COMMANDS
    }
    assert ctx.identity == GateIdentity("lint", _HOOK, _PHASE)
    assert outcome.paused is False
    assert outcome.completed_phases == frozenset({_PHASE})
    override = run.state.extras["phase_handoff_override"]
    assert override["action"] == "retry_verification"
    assert override["feedback"] is None
    # No agent round means no operator feedback and no waiver were written.
    assert "human_feedback" not in run.state.extras
    assert "phase_handoff_waiver" not in run.session
    assert run.session["status"] == "running"
    assert "phase_handoff" not in run.session


def _feature_like_profile(*, plan_rounds: int = 1):
    """A profile with the two loops the continuation has to reason about."""
    from pipeline.runtime import LoopStep, PhaseStep, Profile, ProfileKind

    return Profile(
        name="feature",
        kind=ProfileKind.FULL_CYCLE,
        variant="advanced",
        steps=(
            LoopStep(
                steps=(PhaseStep(phase="plan"), PhaseStep(phase="validate_plan")),
                until="validate_plan.approved",
                round_extras_key="plan_round",
                max_rounds=plan_rounds,
            ),
            PhaseStep(phase=_PHASE),
            LoopStep(
                steps=(
                    PhaseStep(phase="review_changes"),
                    PhaseStep(phase="repair_changes"),
                ),
                until="review_changes.approved",
                round_extras_key="repair_round",
                max_rounds=1,
            ),
            PhaseStep(phase="final_acceptance"),
        ),
    )


def test_a_passing_retry_continues_after_the_phase_that_raised_the_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The continuation starts after the raising phase — silently.

    Everything up to and including that phase is already done, so none of it
    may run again; and because no agent round is entitled to happen there, none
    of it may even emit a start/end trace pair. The plan loop is dropped from
    the walked profile (a persisted round cursor could otherwise re-enter it)
    and its parsed plan is rehydrated for the phases ahead.
    """
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )
    rehydrated: list[Any] = []
    monkeypatch.setattr(
        "pipeline.project.handoff.rehydrate_parsed_plan",
        lambda run_: rehydrated.append(run_) or True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is False
    expected = frozenset({"plan", "validate_plan", _PHASE})
    assert outcome.completed_phases == expected
    # The same set, silently: the runner skips them without callbacks.
    assert outcome.silent_completed_phases == expected
    # The review loop and the terminal gate are untouched — they are the
    # ordinary continuation this action promises, not something to skip.
    assert "review_changes" not in outcome.completed_phases
    assert "final_acceptance" not in outcome.completed_phases
    walked = [
        getattr(step, "round_extras_key", getattr(step, "phase", None))
        for step in outcome.profile.steps
    ]
    assert walked == [_PHASE, "repair_round", "final_acceptance"]
    assert rehydrated == [run]
    from pipeline.project.resume_artifacts import RESUME_PLAN_REQUIRED_KEY

    assert run.state.extras[RESUME_PLAN_REQUIRED_KEY] is True


def test_the_whole_granted_budget_survives_the_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rounds an operator granted are not retired by the round that paused.

    ``max_rounds=1`` with two extra rounds granted is a budget of three. A gate
    pausing in round 2 leaves round 3 still promised, so the continuation must
    restore the whole extension — deriving it from the round it stopped in
    would silently drop the operator's final configured round.

    Proved against a real ``run_profile``: round 3 has to execute, and round 2
    must not be replayed.
    """
    from pipeline.runtime import PhaseRegistry, run_profile
    from pipeline.runtime.handoff import HUMAN_DIRECTED_ROUNDS_KEY

    run, _checkout, _evidence = _env_retry_run(
        tmp_path, gate_phase="plan",
    )
    active = run.session["phase_handoff"]
    # Paused mid-round, on the round's first member: the continuation resumes
    # round 2 at ``validate_plan`` and the loop then has round 3 left.
    active["artifacts"][gate_handoff_actions.LOOP_POSITION_KEY] = _loop_position(
        "plan", loop_key="plan_round",
        members=("plan", "validate_plan"),
        round_n=2, budget=3, until_satisfied=False,
    )
    profile = _feature_like_profile()
    assert profile.steps[0].max_rounds == 1
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    # Two extra rounds on top of the declared one, not "enough for this round".
    assert run.state.extras[HUMAN_DIRECTED_ROUNDS_KEY] == {"plan_round": 2}
    cursor = outcome.loop_resume_cursors["plan_round"]
    assert cursor.round_n == 2
    assert cursor.next_phase == "validate_plan"

    rounds: list[tuple[str, int]] = []
    registry = PhaseRegistry()
    for name in ("plan", "validate_plan", _PHASE, "review_changes",
                 "repair_changes", "final_acceptance"):
        def _handler(state, _n=name):
            round_n = state.extras.get("plan_round", 0)
            rounds.append((_n, round_n))
            if _n == "validate_plan":
                # Round 2 still rejects, so the loop owes round 3 — the round
                # a truncated budget would have retired.
                state.phase_log.setdefault(_n, {})["approved"] = round_n >= 3
            return state
        registry.register(name, _handler)
    state = _pipeline_state(run)
    state.extras[HUMAN_DIRECTED_ROUNDS_KEY] = dict(
        run.state.extras[HUMAN_DIRECTED_ROUNDS_KEY],
    )

    run_profile(
        outcome.profile, state, registry,
        completed_phases=set(outcome.completed_phases),
        silent_completed_phases=set(outcome.silent_completed_phases),
        quiet_loop_phases=set(outcome.quiet_loop_phases),
        loop_resume_cursors=dict(outcome.loop_resume_cursors),
    )

    plan_loop_rounds = [item for item in rounds
                        if item[0] in ("plan", "validate_plan")]
    # Round 2 resumes at the member it owed — its ``plan`` is not replayed —
    # and round 3 then runs in full.
    assert plan_loop_rounds == [
        ("validate_plan", 2), ("plan", 3), ("validate_plan", 3),
    ]
    # Round 3 ran once and the phases ahead of the loop followed it — the
    # continuation resumed the loop, it did not shorten the pipeline.
    assert _PHASE in [name for name, _round in rounds]


def test_a_gate_guarding_a_top_level_phase_leaves_that_phase_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``before_delivery`` pause sits *before* the phase it guards.

    The gates never reported on ``final_acceptance`` — they ran to decide
    whether it may start. Reporting it completed after a green rerun would end
    the run without the terminal phase ever executing, which is the opposite of
    what the gate was protecting. The whole evidence chain here is the
    before-delivery one: its own ledger epoch, its own receipts.
    """
    from pipeline.runtime import PhaseRegistry, run_profile

    run, _checkout, _evidence = _env_retry_run(
        tmp_path, hook="before_delivery", gate_phase="",
    )
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    # The repair seam stays forbidden; the FSM is not, because the phases ahead
    # of the resume point dispatch through it for real below.
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_a, **_k: pytest.fail("env retry must not dispatch a repair"),
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is False
    assert "final_acceptance" not in outcome.completed_phases
    assert outcome.completed_phases == frozenset({
        "plan", "validate_plan", _PHASE, "review_changes", "repair_changes",
    })

    dispatched: list[str] = []
    registry = PhaseRegistry()
    for name in ("plan", "validate_plan", _PHASE, "review_changes",
                 "repair_changes", "final_acceptance"):
        registry.register(
            name, lambda state, _n=name: (dispatched.append(_n), state)[1],
        )
    run_profile(
        outcome.profile, _pipeline_state(run), registry,
        completed_phases=set(outcome.completed_phases),
        silent_completed_phases=set(outcome.silent_completed_phases),
        quiet_loop_phases=set(outcome.quiet_loop_phases),
        loop_resume_cursors=dict(outcome.loop_resume_cursors),
    )

    assert dispatched == ["final_acceptance"]


def test_a_gate_guarding_a_loop_member_resumes_at_that_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``before_phase`` pause inside a loop is locatable, and owes its member.

    The member has not run, so the position records the round without it and
    the continuation resumes *at* it — rather than re-parking as unlocatable
    (the action would then never execute a gate) or skipping the member (the
    round would lose the work the gate was guarding).
    """
    from pipeline.project.gate_repair import _active_loop_position
    from pipeline.runtime import PhaseRegistry, run_profile
    from pipeline.runtime.runner import (
        LOOP_DISPATCH_DECLARED,
        mark_loop_member_executed,
        stamp_active_loop,
    )

    run, _checkout, _evidence = _env_retry_run(
        tmp_path, hook="before_phase", gate_phase="validate_plan",
    )
    profile = _feature_like_profile()
    plan_loop = profile.steps[0]

    # ── producer: the runner is mid-round and about to enter validate_plan
    run.state.extras["plan_round"] = 1
    stamp_active_loop(
        run.state,
        loop_key=plan_loop.round_extras_key,
        phases=tuple(inner.phase for inner in plan_loop.steps),
        budget=plan_loop.max_rounds,
        until=plan_loop.until,
        mode=LOOP_DISPATCH_DECLARED,
    )
    mark_loop_member_executed(run.state, "plan")
    position = _active_loop_position(
        run, "validate_plan", hook="before_phase",
    )
    assert position is not None
    # The guarded member is *not* recorded as executed.
    assert position["executed"] == ["plan"]
    assert position["hook"] == "before_phase"

    run.session["phase_handoff"]["artifacts"][
        gate_handoff_actions.LOOP_POSITION_KEY
    ] = position
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_a, **_k: pytest.fail("env retry must not dispatch a repair"),
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is False
    cursor = outcome.loop_resume_cursors["plan_round"]
    assert cursor.round_n == 1
    assert cursor.completed_phases == ("plan",)
    assert cursor.next_phase == "validate_plan"

    dispatched: list[str] = []
    registry = PhaseRegistry()
    for name in ("plan", "validate_plan", _PHASE, "review_changes",
                 "repair_changes", "final_acceptance"):
        def _handler(state, _n=name):
            dispatched.append(_n)
            if _n == "validate_plan":
                state.phase_log.setdefault(_n, {})["approved"] = True
            return state
        registry.register(name, _handler)

    run_profile(
        outcome.profile, _pipeline_state(run), registry,
        completed_phases=set(outcome.completed_phases),
        silent_completed_phases=set(outcome.silent_completed_phases),
        quiet_loop_phases=set(outcome.quiet_loop_phases),
        loop_resume_cursors=dict(outcome.loop_resume_cursors),
    )

    # The guarded member runs exactly once, and the member behind it does not.
    assert dispatched.count("validate_plan") == 1
    assert "plan" not in dispatched
    assert dispatched[0] == "validate_plan"


def _loop_position(
    phase: str,
    *,
    loop_key: str,
    members: tuple[str, ...],
    round_n: int = 1,
    budget: int = 1,
    until_satisfied: bool = False,
    executed: tuple[str, ...] | None = None,
    mode: str | None = None,
    hook: str = "after_phase",
) -> dict:
    """What routing records when a gate pauses inside a loop round.

    ``executed`` defaults to the declared order through ``phase`` — what a
    round driven by the runner leaves — and ``mode`` to the dispatcher that
    produces that order. A caller passes both explicitly for a round that ran
    its members in its own sequence.
    """
    from pipeline.runtime.runner import LOOP_DISPATCH_DECLARED

    return {
        "loop_key": loop_key,
        "loop_phases": list(members),
        "round": round_n,
        "phase": phase,
        "hook": hook,
        "budget": budget,
        "until_satisfied": until_satisfied,
        "executed": list(
            executed
            if executed is not None
            else members[: members.index(phase) + 1]
        ),
        "mode": mode if mode is not None else LOOP_DISPATCH_DECLARED,
    }


def _raise_inside_plan_loop(run: SimpleNamespace, *, position: dict | None) -> None:
    """Attach ``position`` to a pause already raised inside the plan loop."""
    active = run.session["phase_handoff"]
    assert active["phase"] == "plan", "build the run with gate_phase='plan'"
    if position is not None:
        active["artifacts"][gate_handoff_actions.LOOP_POSITION_KEY] = position


def test_a_gate_raised_inside_a_loop_continues_at_the_next_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The round picks up after the raising phase, not at the loop's head.

    A gate scheduled inside a loop pauses mid-round. Re-entering that loop from
    its first member would re-run the very phase whose gates were just
    re-measured — an agent round the operator never asked for — so the
    continuation carries the validated cursor the runner needs to skip exactly
    the members that already ran, callback-free.
    """
    run, _checkout, _evidence = _env_retry_run(tmp_path, gate_phase="plan")
    _raise_inside_plan_loop(run, position=_loop_position(
        "plan", loop_key="plan_round", members=("plan", "validate_plan"),
    ))
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is False
    cursor = outcome.loop_resume_cursors["plan_round"]
    assert cursor.round_n == 1
    assert cursor.completed_phases == ("plan",)
    assert cursor.next_phase == "validate_plan"
    assert cursor.loop_phases == ("plan", "validate_plan")
    # The loop stays in the walked profile — the cursor positions it — and no
    # phase of it is reported completed, which would skip the member still owed.
    assert outcome.profile is profile
    assert outcome.completed_phases == frozenset()


def test_a_gate_raised_on_a_loops_last_member_reports_the_round_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The round closed the loop, so the whole loop is behind us.

    The boundary answer comes from the pause, which recorded it while the
    round's verdict was live. Budget was left over here — it is the satisfied
    ``until`` that ends the loop, exactly as it would have at the round end the
    gate interrupted.
    """
    run, _checkout, _evidence = _env_retry_run(
        tmp_path, gate_phase="validate_plan",
    )
    run.session["phase_handoff"]["artifacts"][
        gate_handoff_actions.LOOP_POSITION_KEY
    ] = _loop_position(
        "validate_plan", loop_key="plan_round",
        members=("plan", "validate_plan"),
        budget=2, until_satisfied=True,
    )
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is False
    assert outcome.loop_resume_cursors == {}
    assert outcome.completed_phases == frozenset({"plan", "validate_plan"})
    assert outcome.silent_completed_phases == outcome.completed_phases


def test_an_unfinished_loop_resumes_at_its_next_round_not_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last member is not the end of the loop — the ``until`` clause is.

    A gate after the round's final member pauses exactly where the runner would
    have asked "is this loop done?". If the answer was no and the budget still
    has rounds in it, the profile still owes those rounds: skipping the loop
    would walk on to ``implement`` with a plan nothing approved.
    """
    run, _checkout, _evidence = _env_retry_run(
        tmp_path, gate_phase="validate_plan",
    )
    active = run.session["phase_handoff"]
    active["artifacts"][gate_handoff_actions.LOOP_POSITION_KEY] = _loop_position(
        "validate_plan", loop_key="plan_round",
        members=("plan", "validate_plan"),
        round_n=1, budget=2, until_satisfied=False,
    )
    profile = _feature_like_profile(plan_rounds=2)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is False
    cursor = outcome.loop_resume_cursors["plan_round"]
    assert cursor.round_n == 2
    assert cursor.completed_phases == ()
    assert cursor.next_phase == "plan"
    # The finished round is not replayed and the loop is not reported done.
    assert outcome.completed_phases == frozenset()
    assert outcome.profile is profile


def test_only_loops_ahead_of_the_resume_point_are_quieted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop the resume lands *inside* keeps its ordinary trace.

    Its round is mid-flight: whatever its remaining members do is work this run
    is really doing now, and it announces itself like any other. Only the loops
    the continuation reaches fresh can hold a member that the round no longer
    needs.
    """
    run, _checkout, _evidence = _env_retry_run(
        tmp_path, gate_phase="plan",
    )
    active = run.session["phase_handoff"]
    active["artifacts"][gate_handoff_actions.LOOP_POSITION_KEY] = _loop_position(
        "plan", loop_key="plan_round", members=("plan", "validate_plan"),
    )
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.quiet_loop_phases == frozenset({"repair_changes"})
    assert "validate_plan" not in outcome.quiet_loop_phases


def test_an_exhausted_round_budget_closes_the_loop_at_its_last_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rounds left to owe: the loop is behind the continuation point."""
    run, _checkout, _evidence = _env_retry_run(
        tmp_path, gate_phase="validate_plan",
    )
    active = run.session["phase_handoff"]
    active["artifacts"][gate_handoff_actions.LOOP_POSITION_KEY] = _loop_position(
        "validate_plan", loop_key="plan_round",
        members=("plan", "validate_plan"),
        round_n=2, budget=2, until_satisfied=False,
    )
    profile = _feature_like_profile(plan_rounds=2)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is False
    assert outcome.loop_resume_cursors == {}
    assert outcome.completed_phases == frozenset({"plan", "validate_plan"})


def test_the_next_round_really_runs_and_the_finished_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary continuation, proved against a real ``run_profile``.

    Round 1 ran both members and did not satisfy the loop. The continuation
    must therefore execute round 2 in full — and must not replay round 1: the
    handlers count their dispatches, so either mistake is visible.
    """
    from pipeline.runtime import PhaseRegistry, run_profile

    run, _checkout, _evidence = _env_retry_run(
        tmp_path, gate_phase="validate_plan",
    )
    active = run.session["phase_handoff"]
    active["artifacts"][gate_handoff_actions.LOOP_POSITION_KEY] = _loop_position(
        "validate_plan", loop_key="plan_round",
        members=("plan", "validate_plan"),
        round_n=1, budget=2, until_satisfied=False,
    )
    profile = _feature_like_profile(plan_rounds=2)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    rounds: list[tuple[str, int]] = []
    registry = PhaseRegistry()
    for name in ("plan", "validate_plan", _PHASE, "review_changes",
                 "repair_changes", "final_acceptance"):
        def _handler(state, _n=name):
            rounds.append((_n, state.extras.get("plan_round", 0)))
            if _n == "validate_plan":
                # Round 2 approves, so the loop closes the ordinary way.
                state.phase_log.setdefault(_n, {})["approved"] = True
            return state
        registry.register(name, _handler)
    state = _pipeline_state(run)

    run_profile(
        outcome.profile, state, registry,
        completed_phases=set(outcome.completed_phases),
        silent_completed_phases=set(outcome.silent_completed_phases),
        quiet_loop_phases=set(outcome.quiet_loop_phases),
        loop_resume_cursors=dict(outcome.loop_resume_cursors),
    )

    plan_loop_dispatches = [item for item in rounds if item[0] in
                            ("plan", "validate_plan")]
    assert plan_loop_dispatches == [("plan", 2), ("validate_plan", 2)]
    assert [name for name, _round in rounds if name == _PHASE] == [_PHASE]


def _plan_handoff(round_n: int = 1) -> dict[str, Any]:
    """A paused plan loop awaiting an operator retry."""
    return {
        "id": f"validate_plan:plan:{round_n}",
        "phase": "validate_plan",
        "round": round_n,
        "loop_max_rounds": 1,
        "round_extras_key": "plan_round",
        "trigger": "rejected",
        "available_actions": ["retry_feedback", "continue", "halt"],
        "artifacts": {},
        "last_output": "plan rejected",
    }


@pytest.mark.parametrize(
    ("arm", "expected"),
    [
        ("plan", ("plan_round", ["plan", "validate_plan"],
                  [["plan"], ["plan", "validate_plan"]])),
        # The repair arm runs its members in the reverse of the declared order.
        ("repair", ("repair_round", ["review_changes", "repair_changes"],
                    [["repair_changes"],
                     ["repair_changes", "review_changes"]])),
    ],
    ids=["plan_retry", "repair_retry"],
)
def test_both_direct_retry_arms_publish_the_whole_loop_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, expected: Any,
) -> None:
    """Each human-directed arm dispatches loop members outside the runner.

    Whoever drives a member owes the same record, or a gate failing in that
    round publishes ``retry_verification`` and then has nowhere to resume from.
    The execution *order* is part of that record: the repair arm reviews after
    it repairs, so a pause on the repair must leave the review still owed —
    reading the declared order instead would call the repair the round's last
    member and drop the review.
    """
    from pipeline.project import handoff as _handoff
    from pipeline.project.gate_repair import _active_loop_position

    loop_key, declared, executions = expected
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    profile = _feature_like_profile()
    run.session["phase_handoff"] = (
        _plan_handoff() if arm == "plan" else _review_handoff()
    )
    run.state.last_critique = "blockers remain"
    run._on_phase_start = lambda _name, _state: None
    run._on_phase_end = lambda _name, _state: None
    run._metrics = SimpleNamespace(
        save=lambda _path: None, add_round=lambda: None,
    )
    seen: list[dict | None] = []

    def _dispatch(step, state, _ctx, **_kw):
        # What an ``after_phase`` gate on this member would read.
        seen.append(_active_loop_position(run, step.phase))
        return state

    monkeypatch.setattr(_handoff, "_dispatch_via_fsm", _dispatch)
    monkeypatch.setattr(
        _handoff, "load_handoff_decision_validated",
        lambda *_args: SimpleNamespace(
            action="retry_feedback", feedback="Почините это",
            note=None, decided_at=_DECIDED_AT,
        ),
    )
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject",
        lambda _run: None,
    )
    ctx = SimpleNamespace(session_adapter_registry=None)

    _handoff.apply_phase_handoff_resume(run, profile=profile, ctx=ctx)

    assert [entry["phase"] for entry in seen] == [
        order[-1] for order in executions
    ]
    for entry, order in zip(seen, executions, strict=True):
        assert entry["loop_key"] == loop_key
        assert entry["loop_phases"] == declared
        # Round 2 of a loop declared with max_rounds=1: the budget has to say
        # so, or the resume that reads this record cannot accept the round.
        assert entry["round"] == 2
        assert entry["budget"] == 2
        assert entry["until_satisfied"] is False
        assert entry["executed"] == order


def _review_handoff(round_n: int = 1) -> dict[str, Any]:
    """A paused review loop awaiting an operator retry."""
    return {
        "id": f"review_changes:review:{round_n}",
        "phase": "review_changes",
        "round": round_n,
        "loop_max_rounds": 1,
        "round_extras_key": "repair_round",
        "trigger": "rejected",
        "available_actions": ["retry_feedback", "continue", "halt"],
        "artifacts": {},
        "last_output": "blockers remain",
    }


def test_a_gate_on_the_direct_retrys_repair_leaves_its_review_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Producer to consumer, across the arm that runs its members out of order.

    A ``retry_feedback`` round repairs first. A verification gate failing on
    that repair pauses with the round's review still owed — and the whole point
    of ``retry_verification`` is that the operator repairs the environment and
    the run picks up exactly there. So the continuation must run that review
    once, and must not open a second repair the operator never asked for.
    """
    from pipeline.project import handoff as _handoff
    from pipeline.project.gate_repair import _active_loop_position
    from pipeline.runtime import PhaseRegistry, run_profile
    from pipeline.runtime.handoff import HUMAN_DIRECTED_ROUNDS_KEY

    run, _checkout, _evidence = _env_retry_run(
        tmp_path, gate_phase="repair_changes",
    )
    profile = _feature_like_profile()
    repair_loop = profile.steps[2]
    assert repair_loop.max_rounds == 1  # the human round below is round 2
    gate_payload = run.session["phase_handoff"]
    run.session["phase_handoff"] = _review_handoff()
    run.state.last_critique = "blockers remain"
    run._on_phase_start = lambda _name, _state: None
    run._on_phase_end = lambda _name, _state: None
    run._metrics = SimpleNamespace(
        save=lambda _path: None, add_round=lambda: None,
    )
    captured: dict[str, Any] = {}

    def _dispatch_with_a_failing_gate(step, state, _ctx, **_kw):
        """The gate hook fires from inside the repair's phase-end."""
        if step.phase == "repair_changes":
            captured["position"] = _active_loop_position(
                run, step.phase, hook="after_phase",
            )
            state.halt = True
        return state

    monkeypatch.setattr(
        _handoff, "_dispatch_via_fsm", _dispatch_with_a_failing_gate,
    )
    monkeypatch.setattr(
        _handoff, "load_handoff_decision_validated",
        lambda *_args: SimpleNamespace(
            action="retry_feedback", feedback="Почините это",
            note=None, decided_at=_DECIDED_AT,
        ),
    )
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject",
        lambda _run: None,
    )
    _handoff.apply_phase_handoff_resume(
        run, profile=profile, ctx=SimpleNamespace(session_adapter_registry=None),
    )

    position = captured["position"]
    assert position["executed"] == ["repair_changes"]

    # ── consumer: the env gate pause carries that position
    run.state.halt = False
    gate_payload["artifacts"][gate_handoff_actions.LOOP_POSITION_KEY] = position
    run.session["phase_handoff"] = gate_payload
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    outcome = _handoff.apply_phase_handoff_resume(
        run, profile=profile, ctx=object(),
    )

    assert outcome.paused is False
    cursor = outcome.loop_resume_cursors["repair_round"]
    assert cursor.round_n == 2
    assert cursor.next_phase == "review_changes"
    # A prefix cannot say "the second member ran, the first is owed"; the
    # out-of-order member is named instead.
    assert cursor.completed_phases == ()
    assert cursor.done_phases == frozenset({"repair_changes"})

    dispatched: list[str] = []
    registry = PhaseRegistry()
    for name in ("plan", "validate_plan", _PHASE, "review_changes",
                 "repair_changes", "final_acceptance"):
        def _handler(state, _n=name):
            dispatched.append(_n)
            if _n == "review_changes":
                state.phase_log.setdefault(_n, {})["approved"] = True
            return state
        registry.register(name, _handler)
    state = _pipeline_state(run)
    state.extras[HUMAN_DIRECTED_ROUNDS_KEY] = dict(
        run.state.extras[HUMAN_DIRECTED_ROUNDS_KEY],
    )

    run_profile(
        outcome.profile, state, registry,
        completed_phases=set(outcome.completed_phases),
        silent_completed_phases=set(outcome.silent_completed_phases),
        quiet_loop_phases=set(outcome.quiet_loop_phases),
        loop_resume_cursors=dict(outcome.loop_resume_cursors),
    )

    # Exactly one review, no second repair, and nothing behind the loop.
    assert dispatched.count("review_changes") == 1
    assert "repair_changes" not in dispatched
    assert not [name for name in dispatched if name in ("plan", _PHASE)]
    assert "final_acceptance" in dispatched


_UNLOCATABLE_LOOP_POSITIONS: tuple[tuple[str, Any], ...] = (
    ("absent", None),
    ("another_loop", {
        "loop_key": "repair_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 1, "until_satisfied": False,
    }),
    ("another_member_order", {
        "loop_key": "plan_round", "loop_phases": ["plan"],
        "round": 1, "phase": "plan", "budget": 1, "until_satisfied": False,
    }),
    ("another_phase", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "validate_plan", "budget": 1,
        "until_satisfied": False,
    }),
    ("round_out_of_range", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 9, "phase": "plan", "budget": 1, "until_satisfied": False,
    }),
    ("round_is_not_an_int", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": "1", "phase": "plan", "budget": 1, "until_satisfied": False,
    }),
    ("budget_is_absent", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "until_satisfied": False,
    }),
    ("round_exceeds_the_recorded_budget", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 3, "phase": "plan", "budget": 2, "until_satisfied": False,
    }),
    ("the_boundary_verdict_is_absent", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 2,
    }),
    ("the_boundary_verdict_is_not_a_bool", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 2, "until_satisfied": "yes",
    }),
    # ── the execution order has to be one the named dispatcher can produce ──
    ("the_dispatch_mode_is_absent", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 1, "until_satisfied": False,
        "executed": ["plan"],
    }),
    ("the_dispatch_mode_is_unknown", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 1, "until_satisfied": False,
        "executed": ["plan"], "mode": "whatever_order",
    }),
    ("an_executed_member_is_not_a_member", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 1, "until_satisfied": False,
        "executed": ["plan", "implement"], "mode": "declared_order",
    }),
    ("an_executed_member_repeats", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 1, "until_satisfied": False,
        "executed": ["plan", "plan"], "mode": "declared_order",
    }),
    ("the_raising_phase_is_not_where_that_mode_would_be", {
        "loop_key": "plan_round", "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 1, "until_satisfied": False,
        "executed": ["plan", "validate_plan"], "mode": "declared_order",
    }),
    ("the_review_retry_order_is_claimed_for_a_longer_loop", {
        "loop_key": "plan_round",
        "loop_phases": ["plan", "validate_plan"],
        "round": 1, "phase": "plan", "budget": 1, "until_satisfied": False,
        "executed": ["validate_plan"], "mode": "review_retry_order",
    }),
)


def _swap_active_phase(run: SimpleNamespace, _c: Any, _e: Any) -> None:
    """Move only the pause's phase, leaving its gates where they are.

    The probe shape: the ledger, the receipts and the identities still prove
    ``after_phase(implement)``, so the rerun would run the right commands — and
    the continuation would then report the review loop and the terminal gate
    complete, ending a run that never verified them.
    """
    run.session["phase_handoff"]["phase"] = "final_acceptance"


def _swap_primary_hook(run: SimpleNamespace, _c: Any, _e: Any) -> None:
    artifacts = run.session["phase_handoff"]["artifacts"]
    artifacts["gate_identity"] = {
        **artifacts["gate_identity"], "hook": "before_delivery",
    }


def _mix_identity_phases(run: SimpleNamespace, _c: Any, _e: Any) -> None:
    artifacts = run.session["phase_handoff"]["artifacts"]
    artifacts["gate_identities"][1] = {
        **artifacts["gate_identities"][1], "phase": "review_changes",
    }


def _swap_position_hook(run: SimpleNamespace, _c: Any, _e: Any) -> None:
    artifacts = run.session["phase_handoff"]["artifacts"]
    artifacts[gate_handoff_actions.LOOP_POSITION_KEY] = _loop_position(
        "plan", loop_key="plan_round", members=("plan", "validate_plan"),
        hook="before_phase",
    )


_INCOHERENT_SCOPES: tuple[tuple[str, str, Any, str], ...] = (
    (
        "the_active_phase_was_moved_off_its_gates",
        "implement",
        _swap_active_phase,
        "do not describe the same position",
    ),
    (
        "the_primary_identity_names_another_hook",
        "implement",
        _swap_primary_hook,
        "does not prove an env-only retryable set",
    ),
    (
        "the_identities_span_two_phases",
        "implement",
        _mix_identity_phases,
        "one pause is raised by one hook evaluation",
    ),
    (
        "the_loop_position_names_another_hook",
        "plan",
        _swap_position_hook,
        "names hook",
    ),
)


@pytest.mark.parametrize(
    ("label", "gate_phase", "tamper", "reason"),
    _INCOHERENT_SCOPES,
    ids=[label for label, _p, _t, _r in _INCOHERENT_SCOPES],
)
def test_a_record_whose_scope_disagrees_with_itself_blocks_before_any_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    gate_phase: str,
    tamper: Any,
    reason: str,
) -> None:
    """The pause's phase and its gates have to describe one position.

    Everything after admission reads the *identities*; the continuation reads
    the *phase*. Left unbound the two can disagree, and a green rerun of the
    right commands would then report phases nothing verified as complete. The
    scope is checked before the transition, so a disagreement costs no gate.
    """
    del label
    run, _checkout, _evidence = _env_retry_run(tmp_path, gate_phase=gate_phase)
    if gate_phase == "plan":
        _raise_inside_plan_loop(run, position=_loop_position(
            "plan", loop_key="plan_round", members=("plan", "validate_plan"),
        ))
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )
    tamper(run, _checkout, _evidence)
    profile = _feature_like_profile()

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is True
    signal = _blocked_signal(run)
    assert reason in signal.artifacts["retry_blocked_reason"]
    assert not any(event.rerun for event in _executions(run))


_FORGED_EXECUTION_ORDERS: tuple[tuple[str, list[str], str], ...] = (
    # The shape a read-only probe found acceptable before the order was bound
    # to a dispatcher: a *complete* declared order claimed for the dispatcher
    # that reverses it. Taken at face value it closes a round whose review has
    # not run — the run would walk on without its review.
    (
        "declared_order_claimed_for_the_review_retry",
        ["review_changes", "repair_changes"],
        "review_retry_order",
    ),
    # And the mirror: the review-retry's own order claimed for the runner,
    # which would make a repair look like the round's opening member.
    ("reverse_order_claimed_for_the_runner", ["repair_changes"], "declared_order"),
    (
        "an_extra_member_appended_to_a_real_order",
        ["repair_changes", "review_changes", "repair_changes"],
        "review_retry_order",
    ),
)


@pytest.mark.parametrize(
    ("label", "executed", "mode"),
    _FORGED_EXECUTION_ORDERS,
    ids=[label for label, _executed, _mode in _FORGED_EXECUTION_ORDERS],
)
def test_an_order_its_dispatcher_cannot_produce_blocks_before_any_gate_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    executed: list[str],
    mode: str,
) -> None:
    """An execution order is only evidence together with who produced it.

    "review then repair" is what the runner leaves *and* what a record would
    claim to make a half-finished human-directed round look complete. Each mode
    admits exactly one family of orders, so a record outside its own family is
    refused — before the transition, before a gate runs.
    """
    del label
    run, _checkout, _evidence = _env_retry_run(
        tmp_path, gate_phase="repair_changes",
    )
    active = run.session["phase_handoff"]
    active["artifacts"][gate_handoff_actions.LOOP_POSITION_KEY] = {
        "loop_key": "repair_round",
        "loop_phases": ["review_changes", "repair_changes"],
        "round": 2,
        "phase": "repair_changes",
        "hook": "after_phase",
        "budget": 2,
        "until_satisfied": False,
        "executed": executed,
        "mode": mode,
    }
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is True
    signal = _blocked_signal(run)
    assert "cannot resume a loop round it cannot locate" in (
        signal.artifacts["retry_blocked_reason"]
    )
    assert not any(event.rerun for event in _executions(run))


@pytest.mark.parametrize(
    ("label", "position"),
    _UNLOCATABLE_LOOP_POSITIONS,
    ids=[label for label, _position in _UNLOCATABLE_LOOP_POSITIONS],
)
def test_an_unlocatable_loop_round_blocks_before_any_gate_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, position: Any,
) -> None:
    """No provable position, no re-execution — and no gate spent finding out.

    The check sits with the other evidence proofs, before the transition: a
    green rerun the engine cannot position would leave the operator with passing
    receipts and nowhere to continue from.
    """
    del label
    run, _checkout, _evidence = _env_retry_run(tmp_path, gate_phase="plan")
    _raise_inside_plan_loop(run, position=position)
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    assert outcome.paused is True
    signal = _blocked_signal(run)
    assert "cannot resume a loop round it cannot locate" in (
        signal.artifacts["retry_blocked_reason"]
    )
    assert not any(event.rerun for event in _executions(run))


def test_the_continuation_cursor_keeps_the_raising_phase_from_running_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proof that matters: feed the outcome to a real ``run_profile``.

    The plan handler fails the test if it is ever entered, so a continuation
    that re-opened the round would not merely look wrong — it would stop here.
    """
    from pipeline.runtime import PhaseRegistry, run_profile

    run, _checkout, _evidence = _env_retry_run(tmp_path, gate_phase="plan")
    _raise_inside_plan_loop(run, position=_loop_position(
        "plan", loop_key="plan_round", members=("plan", "validate_plan"),
    ))
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    # The FSM seam is deliberately *not* forbidden here: the phases ahead of the
    # continuation point dispatch through it for real. The repair seam still is
    # — nothing about this resume may open a repair round.
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_a, **_k: pytest.fail("env retry must not dispatch a repair"),
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    from pipeline.project import handoff

    outcome = handoff.apply_phase_handoff_resume(run, profile=profile, ctx=object())

    dispatched: list[str] = []
    started: list[str] = []
    registry = PhaseRegistry()
    for name in ("plan", "validate_plan", _PHASE, "review_changes",
                 "repair_changes", "final_acceptance"):
        if name == "plan":
            registry.register(
                name,
                lambda _state, _n=name: pytest.fail(
                    "the raising phase must never be dispatched again",
                ),
            )
            continue
        registry.register(
            name,
            lambda state, _n=name: (dispatched.append(_n), state)[1],
        )
    state = _pipeline_state(run)

    run_profile(
        outcome.profile, state, registry,
        on_phase_start=lambda name, _s: started.append(name),
        completed_phases=set(outcome.completed_phases),
        silent_completed_phases=set(outcome.silent_completed_phases),
        quiet_loop_phases=set(outcome.quiet_loop_phases),
        loop_resume_cursors=dict(outcome.loop_resume_cursors),
    )

    assert "plan" not in dispatched
    assert "plan" not in started
    assert dispatched[0] == "validate_plan"
    assert _PHASE in dispatched and "final_acceptance" in dispatched


def _pipeline_state(run: SimpleNamespace) -> Any:
    """A real ``PipelineState`` seeded from the harness run."""
    from pipeline.runtime import PipelineState

    return PipelineState(
        task="env retry continuation",
        project_dir=str(run.state.project_dir),
        plugin=PluginConfig(),
        output_dir=run.output_dir,
    )


def test_the_interactive_decision_continues_exactly_like_the_sdk_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TTY decision must leave the same trace as the same decision via the SDK.

    The interactive loop re-dispatches the profile in the *same* process,
    through its own ``run_profile`` call. If that call forwarded only
    ``completed_phases``, an operator deciding at the prompt would see
    ``implement`` start and a repair round announced, while the identical
    decision taken through ``phase_handoff_decide`` + resume would not — the
    same run, two different accounts of what happened.
    """
    from pipeline.project import handoff as _handoff
    from pipeline.runtime import PhaseRegistry, run_profile
    from pipeline.runtime.roles import PhaseHandoffAction

    run, _checkout, _evidence = _env_retry_run(tmp_path)
    profile = _feature_like_profile()
    continuation = verification_env_retry._prove_continuation_position(
        profile, run.session["phase_handoff"],
    )
    outcome = verification_env_retry._continuation_outcome(
        run, profile, continuation,
    )

    dispatched: list[str] = []
    started: list[str] = []
    ended: list[str] = []
    registry = PhaseRegistry()
    for name in ("plan", "validate_plan", _PHASE, "review_changes",
                 "repair_changes", "final_acceptance"):
        if name in (_PHASE, "plan", "validate_plan"):
            registry.register(
                name,
                lambda _state, _n=name: pytest.fail(
                    f"{_n} is behind the continuation point and must not run",
                ),
            )
            continue
        def _handler(state, _n=name):
            dispatched.append(_n)
            if _n == "review_changes":
                # A clean review: the loop's ``until`` is satisfied, so the
                # repair member that follows has nothing to do.
                state.phase_log.setdefault(_n, {})["approved"] = True
            if _n == "repair_changes":
                state.phase_log[_n] = {"skipped": "review clean"}
            return state
        registry.register(name, _handler)

    state = _pipeline_state(run)
    prompted: list[Any] = []
    monkeypatch.setattr(_handoff, "apply_phase_handoff_pause", lambda _run: None)
    monkeypatch.setattr(
        _handoff, "should_prompt_for_phase_handoff", lambda **_kw: True,
    )
    monkeypatch.setattr(
        _handoff, "prompt_phase_handoff_action",
        lambda signal, **_kw: prompted.append(signal) or SimpleNamespace(
            action=PhaseHandoffAction.RETRY_VERIFICATION.value,
            feedback="", note=None,
        ),
    )
    monkeypatch.setattr(
        "sdk.phase_handoff.phase_handoff_decide", lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        _handoff, "apply_phase_handoff_resume_with_banners",
        lambda *_a, **_kw: outcome,
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair.arm_gate_context", lambda *_a, **_kw: None,
    )
    monkeypatch.setattr("pipeline.runtime.run_profile", run_profile)

    state.phase_handoff_request = SimpleNamespace(handoff_id="gate:lint:1")
    interactive = SimpleNamespace(
        no_interactive=False,
        output_dir=run.output_dir,
        session_ts=run.output_dir.name,
        session={"phases": {}},
        state=state,
        _ckpt=None,
        registry=registry,
        _on_phase_start=lambda name, _s: started.append(name),
        _on_phase_end=lambda name, _s: ended.append(name),
        _dispatch_active=True,
        unattended=False,
    )

    result = _handoff.process_pending_phase_handoffs(
        interactive, profile, ctx=None,
    )

    assert result.paused is False
    assert prompted, "the interactive loop never reached the prompt"
    # Behind the continuation point: no handler, no callbacks, no trace.
    assert _PHASE not in dispatched and _PHASE not in started
    assert "plan" not in started and "validate_plan" not in started
    # Ahead of it: the review loop and terminal gate run for real, and the
    # repair member that had nothing to do stays off the trace.
    assert "review_changes" in dispatched and "final_acceptance" in dispatched
    assert "repair_changes" in dispatched
    assert "repair_changes" not in started and "repair_changes" not in ended


# ── (b) the real executor: rerun executions, then an honest re-pause ────────


def test_env_retry_executes_every_identity_and_reparks_when_still_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production rerun owner re-executes each identity and re-parks.

    Only ``run_command`` (the true external boundary) is replaced; the ledger
    append, the receipt write, and the fresh handoff artifacts are real, so the
    re-published menu is the one an operator would actually be offered.
    """
    from pipeline.project.handoff import apply_phase_handoff_pause

    run, _checkout, _evidence = _env_retry_run(tmp_path)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    executed: list[str] = []

    def _run_command(command, _spec, _contract, _placeholders, **_kwargs):
        executed.append(command)
        return _receipt_body(command)

    monkeypatch.setattr("pipeline.verification_command.run_command", _run_command)

    outcome = _resume(run)

    assert executed == list(_COMMANDS)
    assert outcome.paused is True
    assert outcome.completed_phases == frozenset()

    reruns = [
        event for event in load_ledger(run.output_dir).trail
        if event.kind == "execution" and event.rerun
    ]
    assert [(event.identity, event.outcome) for event in reruns] == [
        ((command, _HOOK, _PHASE), "fail") for command in _COMMANDS
    ]
    assert all(event.receipt_evidence for event in reruns)

    signal = run.state.phase_handoff_request
    assert signal is not None
    assert signal.handoff_id == "gate:lint:2"
    assert signal.round == 2
    assert "retry_verification" in signal.available_actions
    assert all(
        entry.get("receipt_evidence")
        for entry in signal.artifacts["gate_identities"]
    )

    apply_phase_handoff_pause(run)
    meta = json.loads((run.output_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "awaiting_phase_handoff"
    assert meta["phase_handoff"]["id"] == "gate:lint:2"


def test_a_second_failure_inside_a_loop_stays_retryable_to_the_exact_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole chain: loop pause → retry fails → decide again → green → resume.

    A rerun runs *outside* the loop that owned the round, so the fresh pause it
    publishes has no live loop to read a position from. If the proven position
    were dropped there, the operator would be offered ``retry_verification``
    again and the next resume could only refuse it as unlocatable — a menu that
    cannot be acted on. The position the first pause proved therefore travels
    into the fresh one, and the second decision continues at the same member.
    """
    from pipeline.project.handoff import apply_phase_handoff_pause
    from pipeline.runtime import PhaseRegistry, run_profile

    run, _checkout, _evidence = _env_retry_run(tmp_path, gate_phase="plan")
    position = _loop_position(
        "plan", loop_key="plan_round", members=("plan", "validate_plan"),
    )
    _raise_inside_plan_loop(run, position=position)
    profile = _feature_like_profile()
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    # The repair seam stays forbidden; the FSM is not, because the members
    # ahead of the resume point dispatch through it in the walk below.
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_a, **_k: pytest.fail("env retry must not dispatch a repair"),
    )
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda command, *_a, **_k: _receipt_body(command),
    )

    # ── first decision: the environment is still broken
    first = handoff_module().apply_phase_handoff_resume(
        run, profile=profile, ctx=object(),
    )
    assert first.paused is True
    signal = run.state.phase_handoff_request
    assert signal is not None
    assert "retry_verification" in signal.available_actions
    # The boundary the first pause proved rode into the fresh one.
    assert signal.artifacts[gate_handoff_actions.LOOP_POSITION_KEY] == position

    apply_phase_handoff_pause(run)
    fresh = run.session["phase_handoff"]
    assert fresh["id"] == signal.handoff_id

    # ── second decision: the operator repaired the environment
    _decide(monkeypatch)
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda command, *_a, **_k: {
            **_receipt_body(command), "exit_code": 0, "detail": "",
        },
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair._classify_gate_receipt",
        lambda *_a: SimpleNamespace(
            status="present", failure_kind=None, exit_code=0,
            assertions_passed=0, assertions_total=0, failed_assertions=(),
            reason="",
        ),
    )
    run.session["status"] = "running"

    second = handoff_module().apply_phase_handoff_resume(
        run, profile=profile, ctx=object(),
    )

    assert second.paused is False
    cursor = second.loop_resume_cursors["plan_round"]
    assert cursor.round_n == 1
    assert cursor.completed_phases == ("plan",)
    assert cursor.next_phase == "validate_plan"

    dispatched: list[str] = []
    registry = PhaseRegistry()
    for name in ("plan", "validate_plan", _PHASE, "review_changes",
                 "repair_changes", "final_acceptance"):
        def _handler(state, _n=name):
            dispatched.append(_n)
            if _n == "validate_plan":
                state.phase_log.setdefault(_n, {})["approved"] = True
            return state
        registry.register(name, _handler)

    run_profile(
        second.profile, _pipeline_state(run), registry,
        completed_phases=set(second.completed_phases),
        silent_completed_phases=set(second.silent_completed_phases),
        quiet_loop_phases=set(second.quiet_loop_phases),
        loop_resume_cursors=dict(second.loop_resume_cursors),
    )

    # The member the round owed runs once; the one behind it never re-runs.
    assert dispatched.count("validate_plan") == 1
    assert "plan" not in dispatched
    assert dispatched[0] == "validate_plan"


def handoff_module():
    """The resume router, imported late (it imports this owner lazily)."""
    from pipeline.project import handoff

    return handoff


def test_env_retry_continues_the_run_when_the_reexecution_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda command, *_a, **_k: {**_receipt_body(command), "exit_code": 0, "detail": ""},
    )
    # The rerun's own freshness comparison is not what this asserts; the
    # executed/passed classification is.
    monkeypatch.setattr(
        "pipeline.project.gate_repair._classify_gate_receipt",
        lambda *_a: SimpleNamespace(
            status="present", failure_kind=None, exit_code=0,
            assertions_passed=0, assertions_total=0, failed_assertions=(),
            reason="",
        ),
    )

    outcome = _resume(run)

    assert outcome.paused is False
    assert outcome.completed_phases == frozenset({_PHASE})
    assert run.state.phase_handoff_request is None
    passes = [
        event for event in load_ledger(run.output_dir).trail
        if event.kind == "execution" and event.outcome == "pass"
    ]
    assert [event.identity[0] for event in passes] == list(_COMMANDS)


# ── (c) fail-closed: every blocker re-parks with zero gates executed ───────


def _blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    *,
    head: str = _HEAD,
    subject_head: str = _HEAD,
) -> SimpleNamespace:
    """Apply ``mutate`` to a proven run, resume, and assert nothing executed."""
    run, checkout, evidence = _env_retry_run(tmp_path, head=subject_head)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch, head=head)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )
    mutate(run, checkout, evidence)
    # Counted after the mutation: some blockers (a stale passing execution)
    # legitimately add a trail event of their own, and an unreadable/absent
    # ledger has no count at all. The hard guarantee is the ``run_command``
    # fail above plus "no rerun execution landed".
    before = _executions(run)

    outcome = _resume(run)

    assert outcome.paused is True
    assert outcome.completed_phases == frozenset()
    after = _executions(run)
    assert len(after) == len(before)
    assert not any(event.rerun for event in after)
    return run


def _executions(run: SimpleNamespace) -> list[Any]:
    """Recorded gate executions, or ``[]`` when the ledger is unreadable."""
    from pipeline.verification_ledger_store import LedgerStoreError

    try:
        ledger = load_ledger(run.output_dir)
    except LedgerStoreError:
        return []
    return [event for event in ledger.trail if event.kind == "execution"]


def _blocked_signal(run: SimpleNamespace) -> Any:
    signal = run.state.phase_handoff_request
    assert signal is not None
    assert signal.handoff_id.endswith(":retry_blocked")
    return signal


def _assert_awaiting(run: SimpleNamespace) -> None:
    from pipeline.project.handoff import apply_phase_handoff_pause

    apply_phase_handoff_pause(run)
    meta = json.loads((run.output_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "awaiting_phase_handoff"


def _drop_receipt_key(run: SimpleNamespace, command: str, key: str) -> None:
    artifacts = run.session["phase_handoff"]["artifacts"]
    relative = next(
        entry["receipt_evidence"]
        for entry in artifacts["gate_identities"]
        if entry["command"] == command
    )
    path = run.output_dir / relative
    body = json.loads(path.read_text(encoding="utf-8"))
    body.pop(key, None)
    path.write_text(json.dumps(body), encoding="utf-8")


def _replace_receipt(run: SimpleNamespace, command: str, body: Any) -> None:
    artifacts = run.session["phase_handoff"]["artifacts"]
    relative = next(
        entry["receipt_evidence"]
        for entry in artifacts["gate_identities"]
        if entry["command"] == command
    )
    (run.output_dir / relative).write_text(
        json.dumps(body), encoding="utf-8",
    )


def _patch_receipt(run: SimpleNamespace, gate: str, **fields: Any) -> None:
    artifacts = run.session["phase_handoff"]["artifacts"]
    relative = next(
        entry["receipt_evidence"]
        for entry in artifacts["gate_identities"]
        if entry["command"] == gate
    )
    path = run.output_dir / relative
    body = json.loads(path.read_text(encoding="utf-8"))
    body.update(fields)
    path.write_text(json.dumps(body), encoding="utf-8")


def _repoint_evidence(run: SimpleNamespace, command: str, pointer: str) -> None:
    """Point the persisted record *and* the ledger at ``pointer``.

    Both halves deliberately: the ledger cross-check runs before the receipt is
    read, so a record edited alone would block on the pointer mismatch and
    never reach the path guard under test. The corruption worth defending
    against is the *consistent* one — a record and a ledger that agree with
    each other about a file this run never wrote.
    """
    for entry in run.session["phase_handoff"]["artifacts"]["gate_identities"]:
        if entry["command"] == command:
            entry["receipt_evidence"] = pointer
    path = run.output_dir / "scheduled_gate_ledger.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    for event in ledger["trail"]:
        if event.get("kind") == "execution" and event.get("command") == command:
            event["receipt_evidence"] = pointer
    path.write_text(json.dumps(ledger), encoding="utf-8")


def _foreign_receipt(run: SimpleNamespace, command: str, relative: str) -> Path:
    """A perfectly valid env-failure receipt, written outside this run."""
    path = run.output_dir.parent / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_receipt_body(command)), encoding="utf-8")
    return path


def _evidence_escapes_by_absolute_path(run: SimpleNamespace, _c: Any, _e: Any) -> None:
    foreign = _foreign_receipt(run, "typecheck", "foreign/typecheck.json")
    _repoint_evidence(run, "typecheck", str(foreign))


def _evidence_escapes_by_traversal(run: SimpleNamespace, _c: Any, _e: Any) -> None:
    _foreign_receipt(run, "typecheck", "foreign/typecheck.json")
    _repoint_evidence(run, "typecheck", "../foreign/typecheck.json")


def _evidence_escapes_through_a_symlink(
    run: SimpleNamespace, _c: Any, _e: Any,
) -> None:
    foreign = _foreign_receipt(run, "typecheck", "foreign/typecheck.json")
    link = (
        run.output_dir / "verification_command_receipts" / "executions"
        / "escape.json"
    )
    link.symlink_to(foreign)
    _repoint_evidence(
        run, "typecheck",
        "verification_command_receipts/executions/escape.json",
    )


def _evidence_is_a_symlink_loop(run: SimpleNamespace, _c: Any, _e: Any) -> None:
    """A pointer the filesystem cannot resolve at all.

    Resolution raises rather than returning a path here, so this is the case
    that distinguishes "refused because it resolved somewhere wrong" from
    "refused because it could not be resolved" — both have to land as the same
    decidable pause, not as an exception out of resume.
    """
    executions = run.output_dir / "verification_command_receipts" / "executions"
    (executions / "loop-a.json").symlink_to("loop-b.json")
    (executions / "loop-b.json").symlink_to("loop-a.json")
    _repoint_evidence(
        run, "typecheck",
        "verification_command_receipts/executions/loop-a.json",
    )


def _evidence_leaves_the_executions_dir(
    run: SimpleNamespace, _c: Any, _e: Any,
) -> None:
    """A receipt of this run, but not an immutable execution copy.

    The flat ``<command>.json`` receipt is overwritten by every later run of
    the same command, so it cannot prove *which* execution the operator decided
    on — which is the entire reason the pointer names an execution copy.
    """
    path = (
        run.output_dir / "verification_command_receipts" / "typecheck.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_receipt_body("typecheck")), encoding="utf-8")
    _repoint_evidence(
        run, "typecheck", "verification_command_receipts/typecheck.json",
    )


_BLOCKERS: tuple[tuple[str, str, Any], ...] = (
    (
        "untrusted_ledger_marker",
        "ledger was rejected by resume setup",
        lambda run, _c, _e: run.state.extras.update({
            ENV_RETRY_LEDGER_BLOCKED_KEY: EnvRetryLedgerBlocked(
                "ledger was rejected by resume setup",
            ),
        }),
    ),
    (
        "untrusted_ledger_marker_as_text",
        "ledger snapshot is unreadable",
        lambda run, _c, _e: run.state.extras.update({
            ENV_RETRY_LEDGER_BLOCKED_KEY: "ledger snapshot is unreadable",
        }),
    ),
    (
        "malformed_primary_identity",
        "does not prove an env-only retryable set",
        lambda run, _c, _e: run.session["phase_handoff"]["artifacts"].update(
            {"gate_identity": {"command": "lint", "hook": "after_phase"}},
        ),
    ),
    (
        "malformed_secondary_identity",
        "does not prove an env-only retryable set",
        lambda run, _c, _e: run.session["phase_handoff"]["artifacts"][
            "gate_identities"
        ].__setitem__(1, {"command": "pytest-unit", "phase": _PHASE}),
    ),
    (
        "secondary_without_receipt_evidence",
        "does not prove an env-only retryable set",
        lambda run, _c, _e: run.session["phase_handoff"]["artifacts"][
            "gate_identities"
        ][1].pop("receipt_evidence"),
    ),
    (
        "corrupt_ledger_json",
        "ledger is unreadable",
        lambda run, _c, _e: (run.output_dir / "scheduled_gate_ledger.json")
        .write_text("{not json", encoding="utf-8"),
    ),
    (
        "absent_ledger",
        "no scheduled-gate ledger",
        lambda run, _c, _e: (run.output_dir / "scheduled_gate_ledger.json").unlink(),
    ),
    (
        # Same command, a real ledger row, but an identity this run never
        # selected or executed: matching on the command alone would have
        # re-run a gate whose failure nothing recorded.
        "secondary_rehooked_to_another_scheduled_identity",
        # Refused one step earlier than it used to be: a set whose members sit
        # at two different hooks describes no single position at all, so the
        # substitution never reaches the ledger check it used to fail.
        "one pause is raised by one hook evaluation",
        lambda run, _c, _e: run.session["phase_handoff"]["artifacts"][
            "gate_identities"
        ][1].update({"hook": "before_delivery", "phase": ""}),
    ),
    (
        "stale_evidence_after_a_passing_execution",
        "the decided failure evidence is stale",
        lambda run, _c, _e: _record_pass(run, "pytest-unit"),
    ),
    (
        "receipt_evidence_pointer_mismatch",
        "points at receipt evidence",
        lambda run, _c, evidence: run.session["phase_handoff"]["artifacts"][
            "gate_identities"
        ][1].update({"receipt_evidence": evidence["lint"]}),
    ),
    (
        "receipt_is_a_test_failure",
        "not a failed env_failure",
        lambda run, _c, _e: _patch_receipt(run, "typecheck", exit_code=1, detail=""),
    ),
    (
        "receipt_is_an_empty_object",
        "has schema_version None",
        lambda run, _c, _e: _replace_receipt(run, "typecheck", {}),
    ),
    (
        "receipt_is_a_json_list",
        "is not a receipt object",
        lambda run, _c, _e: _replace_receipt(run, "typecheck", [_receipt_body("typecheck")]),
    ),
    (
        "receipt_has_no_command",
        "records command None",
        lambda run, _c, _e: _drop_receipt_key(run, "typecheck", "command"),
    ),
    (
        "receipt_records_a_foreign_command",
        "records command 'lint'",
        lambda run, _c, _e: _patch_receipt(run, "typecheck", command="lint"),
    ),
    (
        "receipt_has_no_exit_code_key",
        "has no int-or-null exit_code",
        lambda run, _c, _e: _drop_receipt_key(run, "typecheck", "exit_code"),
    ),
    (
        "receipt_has_no_assertions",
        "has no assertions list",
        lambda run, _c, _e: _drop_receipt_key(run, "typecheck", "assertions"),
    ),
    (
        "receipt_has_no_detail",
        "has no detail string",
        lambda run, _c, _e: _drop_receipt_key(run, "typecheck", "detail"),
    ),
    (
        "receipt_schema_predates_the_subject_block",
        "a subject-carrying receipt is v3 or newer",
        lambda run, _c, _e: _patch_receipt(run, "typecheck", schema_version=2),
    ),
    (
        "receipt_has_no_subject",
        "has no subject block",
        lambda run, _c, _e: _drop_receipt_key(run, "typecheck", "subject"),
    ),
    (
        "subject_has_no_status",
        "records subject status None",
        lambda run, _c, _e: _patch_receipt(
            run, "typecheck", subject={"identity": {}},
        ),
    ),
    (
        "subject_has_an_unknown_status",
        "records subject status 'partial'",
        lambda run, _c, _e: _patch_receipt(
            run, "typecheck", subject={"status": "partial"},
        ),
    ),
    (
        "available_subject_has_an_empty_head",
        "identity is unusable",
        lambda run, _c, _e: _patch_receipt(run, "typecheck", subject={
            "status": "available", "identity": {
                "version": 1, "object_format": "sha1", "tree_oid": _TREE,
                "observed_head_oid": "", "baseline_oid": None,
            },
        }),
    ),
    (
        "unavailable_subject_has_no_reason",
        "unavailable subject with no reason",
        lambda run, _c, _e: _patch_receipt(
            run, "typecheck", subject={"status": "unavailable"},
        ),
    ),
    (
        "the_receipts_disagree_about_the_subject",
        "no single tree to re-measure",
        lambda run, _c, _e: _patch_receipt(run, "typecheck", subject={
            "status": "available", "identity": {
                "version": 1, "object_format": "sha1", "tree_oid": _TREE,
                "observed_head_oid": "c" * 40, "baseline_oid": None,
            },
        }),
    ),
    (
        "receipt_evidence_is_an_absolute_path",
        "points outside the run directory",
        _evidence_escapes_by_absolute_path,
    ),
    (
        "receipt_evidence_traverses_out_of_the_run_dir",
        "points outside the run directory",
        _evidence_escapes_by_traversal,
    ),
    (
        "receipt_evidence_symlinks_out_of_the_run_dir",
        "resolves outside this run's execution evidence directory",
        _evidence_escapes_through_a_symlink,
    ),
    (
        "receipt_evidence_is_an_unresolvable_symlink_loop",
        "cannot be resolved",
        _evidence_is_a_symlink_loop,
    ),
    (
        "receipt_evidence_is_not_an_immutable_execution_copy",
        "resolves outside this run's execution evidence directory",
        _evidence_leaves_the_executions_dir,
    ),
    (
        "retained_worktree_was_reclaimed",
        "was reclaimed by workspace cleanup",
        lambda run, _c, _e: run.session["worktree"].update(
            {"reclaimed": {"at": "2026-09-17T09:30:00Z"}},
        ),
    ),
    (
        "retained_worktree_is_gone",
        "no longer exists",
        lambda run, checkout, _e: run.session["worktree"].update(
            {"path": str(checkout.parent / "vanished")},
        ),
    ),
    (
        "gate_cwd_is_not_the_retained_worktree",
        "does not match the retained verification subject",
        lambda run, checkout, _e: run.state.extras.update(
            {"git_cwd": str(checkout.parent)},
        ),
    ),
)


def _record_pass(run: SimpleNamespace, command: str) -> None:
    """Append a *passing* execution after the decided failure."""
    from pipeline.verification_ledger import GateTrailEvent
    from pipeline.verification_ledger_store import update_ledger

    update_ledger(run.output_dir, GateTrailEvent(
        command, _HOOK, _PHASE, "execution", "pass",
        receipt_evidence="verification_command_receipts/later.json",
    ))


@pytest.mark.parametrize(
    ("label", "reason", "mutate"),
    _BLOCKERS,
    ids=[label for label, _reason, _mutate in _BLOCKERS],
)
def test_env_retry_blocker_reparks_a_decidable_pause_without_executing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, reason: str, mutate,
) -> None:
    del label
    run = _blocked(tmp_path, monkeypatch, mutate)

    signal = _blocked_signal(run)
    assert reason in signal.artifacts["retry_blocked_reason"]
    assert reason in signal.last_output
    _assert_awaiting(run)


def test_a_replaced_active_payload_blocks_before_any_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decided payload must still be the one on disk.

    Driven at the owner rather than through ``apply_phase_handoff_resume``,
    which reads both the payload and the id from the same session entry and so
    cannot express the divergence this guard exists for.
    """
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    decided = run.session["phase_handoff"]
    run.session["phase_handoff"] = {**decided, "id": "gate:lint:99"}
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )

    outcome = verification_env_retry.apply_verification_env_retry_resume(
        run=run, profile=object(), active=decided,
        handoff_id=decided["id"], note=None, decided_at=_DECIDED_AT,
    )

    assert outcome.paused is True
    signal = _blocked_signal(run)
    assert "no longer matches decision" in signal.artifacts["retry_blocked_reason"]


def test_head_drift_from_the_decided_subject_blocks_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worktree is intact but has moved: re-measuring it proves nothing."""
    run = _blocked(tmp_path, monkeypatch, lambda *_a: None, head="d" * 40)

    signal = _blocked_signal(run)
    assert "the failing receipts observed" in signal.artifacts["retry_blocked_reason"]


def test_explicitly_unavailable_subjects_still_admit_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An honest ``unavailable`` subject weakens only the HEAD comparison.

    The retained-worktree checks stay mandatory, so the retry is still tied to
    the same checkout — but a receipt that never observed a subject must not be
    read as one that observed a *different* one.
    """
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    for command in _COMMANDS:
        _patch_receipt(run, command, subject={
            "status": "unavailable", "reason": "git_repository_unavailable",
        })
    _decide(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.retry_subject.is_worktree_reclaimed", lambda _b: False,
    )
    monkeypatch.setattr(
        "pipeline.project.retry_subject.git_head",
        lambda _cwd: pytest.fail("no recorded HEAD means no HEAD comparison"),
    )
    reruns: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **kwargs: reruns.append(kwargs) or True,
    )

    outcome = _resume(run)

    assert len(reruns) == 1
    assert outcome.paused is False
    assert run.state.phase_handoff_request is None


def test_isolation_off_needs_no_retained_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    run.session["worktree"] = {"isolation": "off"}
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: True,
    )

    assert _resume(run).paused is False


def test_menu_without_retry_verification_is_not_an_executable_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed record re-parks with a menu that no longer offers the retry.

    This is the loop the fail-closed policy has to terminate: the operator's
    next decision surface must be honest about what the engine can still do.
    """
    run = _blocked(
        tmp_path, monkeypatch,
        lambda run_, _c, _e: run_.session["phase_handoff"]["artifacts"].update(
            {"gate_identity": {"command": "lint", "hook": _HOOK}},
        ),
    )

    signal = _blocked_signal(run)
    assert "retry_verification" not in signal.available_actions
    assert signal.available_actions == ("continue_with_waiver", "halt")


def test_rerun_control_failure_restores_the_decidable_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rerun-side control error re-exposes the subject instead of eating it."""
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("verification retry gate identity is missing or ambiguous"),
        ),
    )

    outcome = _resume(run)

    assert outcome.paused is True
    signal = _blocked_signal(run)
    assert "missing or ambiguous" in signal.artifacts["retry_blocked_reason"]
    _assert_awaiting(run)


def test_provider_crash_is_not_downgraded_to_a_control_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.io.retry import AgentProcessKilledError

    run, _checkout, _evidence = _env_retry_run(tmp_path)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda *_a, **_k: (_ for _ in ()).throw(AgentProcessKilledError("killed")),
    )

    with pytest.raises(AgentProcessKilledError, match="killed"):
        _resume(run)


def test_env_retry_arm_rejects_a_non_verification_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing defect, not operator state: it must not become a re-park."""
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    run.session["phase_handoff"]["trigger"] = "incomplete"
    monkeypatch.setattr(
        "pipeline.project.gate_repair.repark_verification_handoff_retry_blocked",
        lambda *_a, **_k: pytest.fail("a routing defect must not be re-parked"),
    )

    with pytest.raises(RuntimeError, match="expected 'verification_gate_failed'"):
        verification_env_retry.apply_verification_env_retry_resume(
            run=run, profile=object(), active=run.session["phase_handoff"],
            handoff_id=run.session["phase_handoff"]["id"], note=None,
            decided_at=_DECIDED_AT,
        )


# ── (d) the retry_feedback path keeps its route, ledger read, and repair ────


def test_retry_feedback_still_routes_through_the_ledger_and_repairs_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.project import handoff

    run, _checkout, _evidence = _env_retry_run(tmp_path)
    run.session["phase_handoff"]["available_actions"] = [
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ]
    _decide(monkeypatch, action="retry_feedback", feedback="Починил окружение")
    order: list[str] = []
    original = handoff._scheduled_gate_identities
    monkeypatch.setattr(
        handoff, "_scheduled_gate_identities",
        lambda run_: order.append("route_ledger") or original(run_),
    )
    monkeypatch.setattr(
        "pipeline.project.retry_subject.guard_review_retry_subject", lambda _run: None,
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair._repair_step", lambda _profile: object(),
    )
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_a, **_k: order.append("repair"),
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **_kwargs: order.append("rerun") or True,
    )

    assert _resume(run).paused is False
    assert order == ["route_ledger", "repair", "rerun"]


def test_unreadable_ledger_under_retry_feedback_keeps_its_hard_resume_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the env retry owns the ledger as evidence; the other arm must not
    quietly inherit its recoverable re-park."""
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    run.session["phase_handoff"]["available_actions"] = [
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ]
    (run.output_dir / "scheduled_gate_ledger.json").write_text(
        "{not json", encoding="utf-8",
    )
    _decide(monkeypatch, action="retry_feedback", feedback="Починил окружение")
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_a, **_k: pytest.fail("must not repair on an unreadable ledger"),
    )

    with pytest.raises(RuntimeError, match="ledger is unreadable"):
        _resume(run)


def test_retry_feedback_without_feedback_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    run.session["phase_handoff"]["available_actions"] = [
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ]
    _decide(monkeypatch, action="retry_feedback", feedback="   ")
    monkeypatch.setattr(
        "pipeline.project.verification_handoff_retry._dispatch_one_repair",
        lambda *_a, **_k: pytest.fail("must not repair without feedback"),
    )

    outcome = _resume(run)

    assert outcome.paused is True
    signal = _blocked_signal(run)
    assert "requires retry_feedback" in signal.artifacts["retry_blocked_reason"]


# ── (e) idempotence ─────────────────────────────────────────────────────────


def test_a_second_resume_after_the_payload_was_consumed_executes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _checkout, _evidence = _env_retry_run(tmp_path)
    _decide(monkeypatch)
    _forbid_subject_probes(monkeypatch)
    _forbid_agent_seams(monkeypatch)
    reruns: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "pipeline.project.gate_repair.rerun_verification_handoff_gate",
        lambda _run, **kwargs: reruns.append(kwargs) or True,
    )

    assert _resume(run).paused is False
    assert "phase_handoff" not in run.session

    second = _resume(run)

    assert len(reruns) == 1
    assert second.paused is False
    assert second.completed_phases == frozenset()


# ── pure detection helpers (consumed by pre-router resume setup) ────────────


def test_is_env_retry_decision_needs_both_the_pause_and_its_decision() -> None:
    active = {"id": "gate:lint:1", "trigger": "verification_gate_failed"}
    decided = [{"action": "retry_verification", "handoff_id": "gate:lint:1"}]

    assert verification_env_retry.is_env_retry_decision(active, decided) is True
    assert verification_env_retry.is_env_retry_decision(active, []) is False
    assert verification_env_retry.is_env_retry_decision(
        active, [{"action": "continue", "handoff_id": "gate:lint:1"}],
    ) is False
    assert verification_env_retry.is_env_retry_decision(
        active, [{"action": "retry_verification", "handoff_id": "gate:lint:9"}],
    ) is False
    assert verification_env_retry.is_env_retry_decision(
        {"id": "review_changes:x:1", "trigger": "rejected"}, decided,
    ) is False


def test_a_corrupted_decision_id_is_still_an_env_retry_candidate() -> None:
    """The guard predicate must cover the record the strict reader will reject.

    An artifact addressed to this handoff but persisting a different id is
    exactly what a hand-edit or a torn write leaves. It is unusable as a
    decision — and that verdict is reached long after the pre-router guards
    that keep the retained subject alive, so those guards have to recognise the
    claim, not the record's validity.
    """
    from sdk.phase_handoff import safe_handoff_id

    active = {"id": "gate:lint:1", "trigger": "verification_gate_failed"}
    damaged = [{
        "action": "retry_verification",
        "handoff_id": "gate:lint:1-tampered",
        "artifact_stem": safe_handoff_id("gate:lint:1"),
    }]

    assert verification_env_retry.is_env_retry_decision(active, damaged) is False
    assert verification_env_retry.is_env_retry_decision_candidate(
        active, damaged,
    ) is True
    # A decision that was never addressed to this handoff stays out of scope:
    # widening to "any retry_verification in the run" would make every later
    # pause of a retried run claim the retained subject.
    assert verification_env_retry.is_env_retry_decision_candidate(
        active,
        [{
            "action": "retry_verification", "handoff_id": "gate:lint:9",
            "artifact_stem": safe_handoff_id("gate:lint:9"),
        }],
    ) is False
    assert verification_env_retry.is_env_retry_decision_candidate(
        active,
        [{
            "action": "continue_with_waiver", "handoff_id": "gate:lint:1",
            "artifact_stem": safe_handoff_id("gate:lint:1"),
        }],
    ) is False


def test_detect_env_retry_resume_sees_a_decision_with_a_corrupted_id(
    tmp_path: Path,
) -> None:
    from sdk.phase_handoff import safe_handoff_id

    decisions = tmp_path / "phase_handoff_decisions"
    decisions.mkdir()
    (decisions / f"{safe_handoff_id('gate:lint:1')}.json").write_text(
        json.dumps({
            "action": "retry_verification", "handoff_id": "gate:lint:1-tampered",
        }),
        encoding="utf-8",
    )

    assert verification_env_retry.detect_env_retry_resume(
        {"phase_handoff": {
            "id": "gate:lint:1", "trigger": "verification_gate_failed",
        }},
        tmp_path,
    ) is True


def test_an_unreadable_env_retry_decision_reparks_instead_of_failing_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict validation of the decision is fail-closed, not fail-hard.

    The pre-router guards already held this run's retained subject for the
    claimed retry, so the run is intact and decidable — a hard resume error
    would leave the operator with a pause and no way to move it.
    """
    from sdk.phase_handoff import safe_handoff_id

    run, _checkout, _evidence = _env_retry_run(tmp_path)
    handoff_id = run.session["phase_handoff"]["id"]
    decisions = run.output_dir / "phase_handoff_decisions"
    decisions.mkdir()
    (decisions / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "action": "retry_verification",
            "handoff_id": f"{handoff_id}-tampered",
            "run_id": run.output_dir.name,
            "phase": _PHASE,
            "decided_at": _DECIDED_AT,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pipeline.verification_command.run_command",
        lambda *_a, **_k: pytest.fail("a blocked env retry must execute no gate"),
    )

    outcome = _resume(run)

    assert outcome.paused is True
    signal = _blocked_signal(run)
    assert "failed strict validation" in signal.artifacts["retry_blocked_reason"]
    # The record itself is intact, so the retry is still on the menu once the
    # audit artifact is repaired.
    assert "retry_verification" in signal.available_actions
    assert not any(event.rerun for event in _executions(run))
    _assert_awaiting(run)


def test_a_corrupt_decision_for_another_action_keeps_its_hard_resume_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the env retry re-parks: no other action retained a subject for it."""
    from sdk.phase_handoff import safe_handoff_id

    run, _checkout, _evidence = _env_retry_run(tmp_path)
    handoff_id = run.session["phase_handoff"]["id"]
    decisions = run.output_dir / "phase_handoff_decisions"
    decisions.mkdir()
    (decisions / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "action": "continue_with_waiver",
            "handoff_id": f"{handoff_id}-tampered",
            "run_id": run.output_dir.name,
            "phase": _PHASE,
            "decided_at": _DECIDED_AT,
            "feedback": "waived",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pipeline.project.gate_repair.repark_verification_handoff_retry_blocked",
        lambda *_a, **_k: pytest.fail("only a decided env retry may re-park"),
    )

    with pytest.raises(RuntimeError, match="failed strict validation"):
        _resume(run)


def test_detect_env_retry_resume_reads_prior_meta_and_session_alike(
    tmp_path: Path,
) -> None:
    decisions = tmp_path / "phase_handoff_decisions"
    decisions.mkdir()
    (decisions / "gate.json").write_text(
        json.dumps({"action": "retry_verification", "handoff_id": "gate:lint:1"}),
        encoding="utf-8",
    )
    (decisions / "broken.json").write_text("{not json", encoding="utf-8")
    payload = {"phase_handoff": {
        "id": "gate:lint:1", "trigger": "verification_gate_failed",
    }}

    assert verification_env_retry.detect_env_retry_resume(payload, tmp_path) is True
    # Same shape whether it came from meta.json or the live session dict.
    assert verification_env_retry.detect_env_retry_resume(
        {"status": "awaiting_phase_handoff", **payload}, tmp_path,
    ) is True
    assert verification_env_retry.detect_env_retry_resume(payload, None) is False
    assert verification_env_retry.detect_env_retry_resume({}, tmp_path) is False
    assert verification_env_retry.detect_env_retry_resume(
        payload, tmp_path / "missing",
    ) is False


def test_the_env_retry_owner_never_imports_an_agent_dispatch_seam() -> None:
    """Structural: an env retry has no agent round to spend."""
    source = Path(verification_env_retry.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "_dispatch_via_fsm", "_dispatch_one_repair", "_repair_step",
        "_execute_gate_with_boundary", "_run_gate_command",
    ):
        assert forbidden not in source
