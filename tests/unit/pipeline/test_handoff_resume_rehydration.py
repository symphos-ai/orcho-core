"""Tests for reviewer-critique rehydration on phase-handoff resume.

``_apply_phase_handoff_resume`` rehydrates ``state.last_critique`` from
the persisted active payload (or the round-matching session
``validate_plan`` entry) and sets ``state.human_feedback`` separately.

These tests pin the round-matching fallback helper in isolation. The
full resume flow is integration-tested elsewhere; the round-matching
edge cases live here because they were the persistence-layer fix flagged
by P2 review.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.observability import events as _events
from pipeline.control import render_round_label
from pipeline.plugins import PluginConfig
from pipeline.project.handoff import (
    apply_phase_handoff_pause,
    last_validate_plan_critique as _last_validate_plan_critique,
    load_handoff_decision_validated,
    process_pending_phase_handoffs,
)
from pipeline.runtime import (
    LoopStep,
    PhaseRegistry,
    PhaseStep,
    PipelineState,
    Profile,
    run_profile,
)
from pipeline.runtime.handoff import PhaseHandoffRequested
from pipeline.runtime.roles import PhaseHandoffAction, PhaseHandoffType


def _init_dirty_repo(repo: Path) -> None:
    """Create a git repo with one commit plus an uncommitted change.

    Used where the review-retry subject guard (T2) requires the repair cwd to
    carry the rejected diff; a dirty working tree satisfies the proof.
    """
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    (repo / "f.txt").write_text("base\nrejected diff\n", encoding="utf-8")


class TestLastValidatePlanCritique:
    def test_returns_round_matching_entry(self) -> None:
        session = {
            "phases": {
                "validate_plan": [
                    {"attempt": 1, "critique": "round-1 critique"},
                    {"attempt": 2, "critique": "round-2 critique"},
                ],
            },
        }
        assert (
            _last_validate_plan_critique(session, round_n=2)
            == "round-2 critique"
        )

    def test_no_match_returns_empty_when_multiple_entries(self) -> None:
        # Multiple chained handoffs in session and none matches the
        # active round — safer to return "" than to pick [-1] and
        # mix critique across handoffs.
        session = {
            "phases": {
                "validate_plan": [
                    {"attempt": 1, "critique": "first"},
                    {"attempt": 2, "critique": "second"},
                ],
            },
        }
        assert _last_validate_plan_critique(session, round_n=5) == ""

    def test_round_miss_returns_empty_even_with_single_entry(self) -> None:
        # Strict round match: when ``round_n`` is supplied, a session
        # entry that doesn't match by round is treated as unrelated.
        # No guessing from a lone entry — active handoff knows its
        # round, so a mismatch is not an invitation to surface
        # whatever critique is lying around.
        session = {
            "phases": {
                "validate_plan": [
                    {"attempt": 1, "critique": "round-1 critique"},
                ],
            },
        }
        assert _last_validate_plan_critique(session, round_n=99) == ""

    def test_single_entry_fallback_when_round_unknown(self) -> None:
        session = {
            "phases": {
                "validate_plan": [
                    {"attempt": 1, "critique": "lone critique"},
                ],
            },
        }
        assert (
            _last_validate_plan_critique(session, round_n=None)
            == "lone critique"
        )

    def test_no_validate_plan_returns_empty(self) -> None:
        assert _last_validate_plan_critique({}, round_n=1) == ""
        assert (
            _last_validate_plan_critique({"phases": {}}, round_n=1) == ""
        )
        assert (
            _last_validate_plan_critique(
                {"phases": {"validate_plan": []}}, round_n=1,
            )
            == ""
        )

    def test_round_n_none_with_multiple_entries_returns_empty(self) -> None:
        # Ambiguous: caller did not supply a round and there are
        # multiple chained-handoff entries. Refuse to guess.
        session = {
            "phases": {
                "validate_plan": [
                    {"attempt": 1, "critique": "first"},
                    {"attempt": 2, "critique": "second"},
                ],
            },
        }
        assert _last_validate_plan_critique(session, round_n=None) == ""

    def test_non_string_critique_returns_empty(self) -> None:
        session = {
            "phases": {
                "validate_plan": [
                    {"attempt": 1, "critique": None},
                ],
            },
        }
        assert _last_validate_plan_critique(session, round_n=1) == ""


def _seed_waiver_decision(
    run_dir,
    *,
    handoff_id: str,
    phase: str,
    feedback: str,
    note: str | None = None,
    decided_at: str = "2026-06-03T12:00:00+00:00",
) -> None:
    """Write a continue_with_waiver decision artifact in the SDK shape."""
    from sdk.phase_handoff import safe_handoff_id

    decisions = run_dir / "phase_handoff_decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "run_id":     run_dir.name,
            "handoff_id": handoff_id,
            "phase":      phase,
            "action":     "continue_with_waiver",
            "feedback":   feedback,
            "note":       note,
            "decided_at": decided_at,
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class TestContinueWithWaiverResume:
    """``continue_with_waiver`` strips the paused loop like ``continue``
    but durably records a waiver into the session (so a fresh-process
    resume rehydrates it) and into ``state.extras`` (so the gates
    dispatched in THIS process inject it)."""

    def _profile(self) -> Profile:
        plan_loop = LoopStep(
            steps=(
                PhaseStep(phase="plan"),
                PhaseStep(phase="validate_plan"),
            ),
            until="validate_plan.approved",
            max_rounds=2,
            round_extras_key="plan_round",
        )
        return Profile(
            name="advanced",
            kind="advanced",
            description="plan loop only",
            steps=(plan_loop,),
        )

    def _run(self, run_dir, state):
        class _Run:
            output_dir = run_dir
            session = {
                "status": "awaiting_phase_handoff",
                "phases": {},
                "phase_handoff": {
                    "id": "validate_plan:plan_round:2",
                    "phase": "validate_plan",
                    "round": 2,
                    "artifacts": {
                        "findings": [{"id": "F1", "title": "legacy shim"}],
                    },
                    "last_output": "round-2 critique",
                },
            }
            _ckpt = None
            _metrics = None
        r = _Run()
        r.state = state
        return r

    def test_resume_strips_loop_and_persists_waiver(self, tmp_path) -> None:
        from pipeline.project.handoff import apply_phase_handoff_resume

        run_dir = tmp_path / "20260603_120000_waiver"
        run_dir.mkdir()
        _seed_waiver_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
            feedback="accepted risk: legacy shim stays this release",
            note="operator waiver",
        )
        state = PipelineState(
            task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
        )
        run = self._run(run_dir, state)

        outcome = apply_phase_handoff_resume(run, self._profile(), None)

        # Loop stripped, no extra round, not paused.
        assert outcome.paused is False
        assert outcome.completed_phases == frozenset({"plan", "validate_plan"})
        # Active payload cleared; status back to running.
        assert "phase_handoff" not in run.session
        assert run.session["status"] == "running"

        # Durable waiver in session (→ meta.json).
        waiver = run.session["phase_handoff_waiver"]
        assert waiver["handoff_id"] == "validate_plan:plan_round:2"
        assert waiver["phase"] == "validate_plan"
        assert waiver["waiver_text"] == (
            "accepted risk: legacy shim stays this release"
        )
        assert waiver["findings"] == [{"id": "F1", "title": "legacy shim"}]
        assert waiver["critique"] == "round-2 critique"

        # Runtime copy for in-process gates.
        assert state.extras["phase_handoff_waiver"] == waiver
        override = state.extras["phase_handoff_override"]
        assert override["action"] == "continue_with_waiver"
        assert override["feedback"] == (
            "accepted risk: legacy shim stays this release"
        )

    def test_persisted_waiver_rehydrates_in_fresh_process(self, tmp_path) -> None:
        """A fresh-process resume (MCP/Web) reads the waiver back from
        meta.json into ``state.extras`` via the state_setup hydration
        seam — no in-memory carry-over required."""
        from pipeline.engine.session import save_session
        from pipeline.project.handoff import apply_phase_handoff_resume
        from pipeline.project.state_setup import (
            hydrate_state_extras_from_session,
        )

        run_dir = tmp_path / "20260603_130000_waiver"
        run_dir.mkdir()
        _seed_waiver_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
            feedback="accepted risk: documented gap",
        )
        state = PipelineState(
            task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
        )
        run = self._run(run_dir, state)
        apply_phase_handoff_resume(run, self._profile(), None)
        # Persist session → meta.json the way _persist_handoff_running_state
        # does, then simulate a brand-new process reading it back.
        save_session(run_dir, run.session)

        reloaded = json.loads((run_dir / "meta.json").read_text())
        assert "phase_handoff_waiver" in reloaded

        fresh_state = PipelineState(
            task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
        )
        hydrate_state_extras_from_session(fresh_state, reloaded)
        assert fresh_state.extras["phase_handoff_waiver"]["waiver_text"] == (
            "accepted risk: documented gap"
        )

    def test_resume_without_feedback_raises(self, tmp_path) -> None:
        from pipeline.project.handoff import apply_phase_handoff_resume

        run_dir = tmp_path / "20260603_140000_waiver"
        run_dir.mkdir()
        _seed_waiver_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
            feedback="   ",
        )
        state = PipelineState(
            task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
        )
        run = self._run(run_dir, state)
        import pytest

        with pytest.raises(RuntimeError, match="operator verdict"):
            apply_phase_handoff_resume(run, self._profile(), None)


def _seed_continue_decision(
    run_dir,
    *,
    handoff_id: str,
    phase: str,
    decided_at: str = "2026-06-03T12:00:00+00:00",
) -> None:
    """Write a plain ``continue`` decision artifact in the SDK shape."""
    from sdk.phase_handoff import safe_handoff_id

    decisions = run_dir / "phase_handoff_decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "run_id":     run_dir.name,
            "handoff_id": handoff_id,
            "phase":      phase,
            "action":     "continue",
            "feedback":   None,
            "note":       None,
            "decided_at": decided_at,
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class TestParsedPlanRehydrationOnPlanLoopStrip:
    """An operator override (``continue`` / ``continue_with_waiver``) past a
    *rejected* validate_plan strips the plan loop, so ``implement`` becomes
    the first phase the runner dispatches. On a fresh-process resume the
    in-memory ``state.parsed_plan`` is gone; the rejected plan must be
    rehydrated from the persisted ``parsed_plan.json`` so subtask_dag
    ``implement`` runs the plan instead of halting with "requires a parsed
    plan". Regression: this halt made ``continue_with_waiver`` functionally
    hollow for plan-loop rejections."""

    def _plan_with_subtasks(self):
        from agents.entities import SubTask
        from pipeline.plan_parser import ParsedPlan

        return ParsedPlan(
            short_summary="Rejected plan, accepted by operator.",
            planning_context="ctx",
            subtasks=(
                SubTask(id="t1", goal="inspect"),
                SubTask(id="t2", goal="apply", depends_on=("t1",)),
            ),
            source="json",
        )

    def _profile(self) -> Profile:
        plan_loop = LoopStep(
            steps=(
                PhaseStep(phase="plan"),
                PhaseStep(phase="validate_plan"),
            ),
            until="validate_plan.approved",
            max_rounds=2,
            round_extras_key="plan_round",
        )
        return Profile(
            name="advanced",
            kind="advanced",
            description="plan loop only",
            steps=(plan_loop,),
        )

    def _run(self, run_dir, state):
        class _Run:
            output_dir = run_dir
            session = {
                "status": "awaiting_phase_handoff",
                "phases": {},
                "phase_handoff": {
                    "id": "validate_plan:plan_round:2",
                    "phase": "validate_plan",
                    "round": 2,
                    "artifacts": {"findings": [{"id": "F1"}]},
                    "last_output": "round-2 critique",
                },
            }
            _ckpt = None
            _metrics = None
        r = _Run()
        r.state = state
        return r

    def _fresh_state(self, tmp_path):
        return PipelineState(
            task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
        )

    def test_continue_rehydrates_rejected_plan_from_disk(self, tmp_path) -> None:
        from pipeline.plan_artifacts import write_parsed_plan_artifact
        from pipeline.project.handoff import apply_phase_handoff_resume

        run_dir = tmp_path / "20260603_120000_continue"
        run_dir.mkdir()
        write_parsed_plan_artifact(
            run_dir, self._plan_with_subtasks(), attempt=2,
        )
        _seed_continue_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
        )
        state = self._fresh_state(tmp_path)
        assert state.parsed_plan is None  # fresh-process resume

        apply_phase_handoff_resume(self._run(run_dir, state), self._profile(), None)

        # The rejected plan is now carried into implement.
        assert state.parsed_plan is not None
        assert [s.id for s in state.parsed_plan.subtasks] == ["t1", "t2"]
        assert state.plan_markdown  # rendered for presentation/evidence

    def test_continue_with_waiver_rehydrates_rejected_plan(self, tmp_path) -> None:
        from pipeline.plan_artifacts import write_parsed_plan_artifact
        from pipeline.project.handoff import apply_phase_handoff_resume

        run_dir = tmp_path / "20260603_130000_waiver"
        run_dir.mkdir()
        write_parsed_plan_artifact(
            run_dir, self._plan_with_subtasks(), attempt=2,
        )
        _seed_waiver_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
            feedback="accepted: findings are known false positives",
        )
        state = self._fresh_state(tmp_path)

        apply_phase_handoff_resume(self._run(run_dir, state), self._profile(), None)

        assert state.parsed_plan is not None
        assert [s.id for s in state.parsed_plan.subtasks] == ["t1", "t2"]
        # Waiver still recorded alongside the rehydrated plan.
        assert state.extras["phase_handoff_waiver"]["waiver_text"] == (
            "accepted: findings are known false positives"
        )

    def test_does_not_overwrite_in_memory_plan(self, tmp_path) -> None:
        """Same-process resume already has ``state.parsed_plan`` populated by
        the plan phase that ran in this process — do not clobber it with the
        disk snapshot."""
        from pipeline.plan_artifacts import write_parsed_plan_artifact
        from pipeline.project.handoff import apply_phase_handoff_resume

        run_dir = tmp_path / "20260603_140000_inproc"
        run_dir.mkdir()
        write_parsed_plan_artifact(
            run_dir, self._plan_with_subtasks(), attempt=2,
        )
        _seed_continue_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
        )
        state = self._fresh_state(tmp_path)
        live_plan = self._plan_with_subtasks()
        state.parsed_plan = live_plan  # in-memory copy from this process

        apply_phase_handoff_resume(self._run(run_dir, state), self._profile(), None)

        assert state.parsed_plan is live_plan  # identity preserved, no reload

    def test_missing_artifact_is_noop(self, tmp_path) -> None:
        """No persisted plan → leave ``state.parsed_plan`` None so the
        downstream subtask_dag guard surfaces the missing plan to the
        operator rather than this helper masking it.

        The continue plan-strip site also stamps the owned
        ``RESUME_PLAN_REQUIRED_KEY`` marker so the subtask_dag guard fires on
        this in-process resume form, where ``checkpoint.completed`` does not
        yet carry ``plan``."""
        from pipeline.project.handoff import apply_phase_handoff_resume
        from pipeline.project.resume_artifacts import RESUME_PLAN_REQUIRED_KEY

        run_dir = tmp_path / "20260603_150000_noplan"
        run_dir.mkdir()
        _seed_continue_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
        )
        state = self._fresh_state(tmp_path)

        apply_phase_handoff_resume(self._run(run_dir, state), self._profile(), None)

        assert state.parsed_plan is None
        # Marker set so the subtask_dag guard fires (see below).
        assert state.extras[RESUME_PLAN_REQUIRED_KEY] is True

    def test_missing_artifact_resume_drives_instructive_subtask_dag_error(
        self, tmp_path,
    ) -> None:
        """End-to-end of the handoff-resume falsifier: a continue-resume that
        leaves the plan behind with no recoverable artifact must drive the
        ``subtask_dag`` implement guard to an INSTRUCTIVE error (naming the run
        dir + ``parsed_plan.json``), not the generic empty-plan line."""
        from types import SimpleNamespace

        from pipeline.phases.builtin.subtask_dag import _run_subtask_dag_implement
        from pipeline.project.handoff import apply_phase_handoff_resume

        run_dir = tmp_path / "20260603_160000_noplan"
        run_dir.mkdir()
        _seed_continue_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
        )
        state = self._fresh_state(tmp_path)
        state.output_dir = run_dir  # the guard reads state.output_dir

        apply_phase_handoff_resume(self._run(run_dir, state), self._profile(), None)
        assert state.parsed_plan is None

        _run_subtask_dag_implement(state, SimpleNamespace(), None)

        assert state.halt is True
        generic = (
            "implementation_execution=subtask_dag requires a parsed plan "
            "with at least one required subtask"
        )
        assert state.halt_reason != generic
        assert "parsed_plan.json" in state.halt_reason
        assert run_dir.name in state.halt_reason


class TestInteractivePhaseHandoffRetryObservability:
    def test_retry_feedback_round_has_phase_context_and_metrics_snapshot(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Interactive retry_feedback runs a real phase attempt, not an
        anonymous side invocation.

        Regression: the post-handoff prompt loop called
        ``apply_phase_handoff_resume`` after the main v2 dispatch had
        already reset ``run._dispatch_active``. The extra plan and
        validate_plan invocations then emitted runtime events with
        ``phase=null`` and did not snapshot the retry attempts into
        ``metrics.json`` until much later.
        """
        run_dir = tmp_path / "run-retry"
        run_dir.mkdir()
        _events.init_event_store(run_dir)

        reg = PhaseRegistry()

        def plan(state: PipelineState) -> PipelineState:
            _events.emit("agent.start", agent="claude", model="m")
            state.phase_log["plan"] = {"output": "updated plan"}
            return state

        def validate_plan(state: PipelineState) -> PipelineState:
            _events.emit("agent.start", agent="codex", model="m")
            state.phase_log["validate_plan"] = {
                "approved": True,
                "critique": "{\"verdict\":\"APPROVED\",\"findings\":[]}",
            }
            return state

        reg.register("plan", plan)
        reg.register("validate_plan", validate_plan)

        plan_loop = LoopStep(
            steps=(
                PhaseStep(phase="plan"),
                PhaseStep(phase="validate_plan"),
            ),
            until="validate_plan.approved",
            max_rounds=2,
            round_extras_key="plan_round",
        )
        profile = Profile(
            name="advanced",
            kind="advanced",
            description="plan loop only",
            steps=(plan_loop,),
        )

        state = PipelineState(
            task="t",
            project_dir=str(tmp_path),
            plugin=PluginConfig(),
        )
        state.phase_handoff_request = PhaseHandoffRequested(
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
            type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
            trigger="rejected",
            verdict="REJECTED",
            approved=False,
            round_extras_key="plan_round",
            round=2,
            loop_max_rounds=2,
            available_actions=(
                PhaseHandoffAction.CONTINUE.value,
                PhaseHandoffAction.RETRY_FEEDBACK.value,
                PhaseHandoffAction.HALT.value,
            ),
            last_output="round-2 critique",
        )

        class _Metrics:
            def __init__(self) -> None:
                self.records: list[str] = []

            def record_phase(self, phase: str, **_kwargs) -> None:
                self.records.append(phase)

            def add_round(self) -> None:
                pass

            def save(self, output_dir) -> None:
                (output_dir / "metrics.json").write_text(
                    json.dumps({"phase_attempts": list(self.records)}),
                    encoding="utf-8",
                )

        class _Run:
            def __init__(self) -> None:
                self.output_dir = run_dir
                self.session_ts = run_dir.name
                self.session = {"status": "running", "phases": {}}
                self.registry = reg
                self.state = state
                self.no_interactive = False
                self._dispatch_active = False
                self._presentation = None
                self._ckpt = None
                self._metrics = _Metrics()

            def _on_phase_start(self, name, st):
                st.extras[f"_phase_t0_{name}"] = 0.0
                st.extras["_current_phase"] = name
                if not self._dispatch_active:
                    return
                phase = name.upper()
                round_n = int(st.extras.get("plan_round") or 1)
                _events.set_phase_context(
                    phase=phase,
                    phase_key=name,
                    round=round_n,
                    title=phase,
                )
                _events.emit("phase.start")

            def _on_phase_end(self, name, _st):
                if self._dispatch_active:
                    _events.emit("phase.end")
                    _events.clear_phase_context()

            def _fsm_metrics(self, name, _st):
                self._metrics.record_phase(name)

            def _fsm_checkpoint(self, *_args):
                pass

            def _record_phase_failure(self, exc, fallback_phase):
                raise AssertionError((exc, fallback_phase))

        from pipeline.lifecycle import default_lifecycle_context
        from pipeline.project import handoff as _handoff

        run = _Run()
        ctx = default_lifecycle_context(phase_registry=reg)
        ctx.on_metrics = run._fsm_metrics
        ctx.on_checkpoint = run._fsm_checkpoint

        monkeypatch.setattr(
            _handoff,
            "should_prompt_for_phase_handoff",
            lambda *, no_interactive: True,
        )
        monkeypatch.setattr(
            _handoff,
            "prompt_phase_handoff_action",
            lambda _signal, **_kw: SimpleNamespace(
                action=PhaseHandoffAction.RETRY_FEEDBACK.value,
                feedback="fix both findings",
                note=None,
            ),
        )

        rounds: list[int] = []
        result = process_pending_phase_handoffs(
            run,
            profile,
            ctx,
            on_round_end=lambda _loop, round_n, _state: rounds.append(round_n),
        )

        assert result.continue_dispatch is True
        assert result.paused is False
        assert run.session["status"] == "running"
        assert "phase_handoff" not in run.session
        assert rounds == [3]

        event_rows = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(
                encoding="utf-8",
            ).splitlines()
        ]
        agent_events = [e for e in event_rows if e["kind"] == "agent.start"]
        assert [e["phase"] for e in agent_events] == ["PLAN", "VALIDATE_PLAN"]
        assert all(
            e["payload"].get("phase_key") in {"plan", "validate_plan"}
            for e in agent_events
        )

        metrics = json.loads((run_dir / "metrics.json").read_text())
        assert metrics["phase_attempts"] == ["plan", "validate_plan"]
        assert run._ckpt is None  # checkpoint status is not needed for this test


def _signal(
    *,
    handoff_id: str,
    round_n: int,
    loop_max_rounds: int = 2,
    approved: bool = False,
) -> PhaseHandoffRequested:
    return PhaseHandoffRequested(
        handoff_id=handoff_id,
        phase="validate_plan",
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger="rejected",
        verdict="REJECTED",
        approved=approved,
        round_extras_key="plan_round",
        round=round_n,
        loop_max_rounds=loop_max_rounds,
        available_actions=(
            PhaseHandoffAction.CONTINUE.value,
            PhaseHandoffAction.RETRY_FEEDBACK.value,
            PhaseHandoffAction.HALT.value,
        ),
        artifacts={"findings": [{"id": "F1"}]},
        last_output="round critique",
    )


def _pause_run(signal: PhaseHandoffRequested):
    """Minimal duck-typed run for ``apply_phase_handoff_pause``.

    ``output_dir=None`` skips the save_session / metrics IO tail and
    ``_presentation=None`` suppresses the terminal banner — leaving only the
    in-memory pause-snapshot mutation under test.
    """
    class _Run:
        output_dir = None
        _ckpt = None
        _presentation = None
        session: dict = {"status": "running", "phases": {}}
    run = _Run()
    run.state = SimpleNamespace(phase_handoff_request=signal)
    return run


class TestPhaseHandoffPauseSnapshot:
    """The refactored ``apply_phase_handoff_pause`` (now routed through the
    run_state helper) writes a byte-equivalent active payload + status."""

    def test_pause_snapshot_is_byte_equivalent(self) -> None:
        signal = _signal(handoff_id="validate_plan:plan_round:2", round_n=2)
        run = _pause_run(signal)

        apply_phase_handoff_pause(run)

        assert run.session["status"] == "awaiting_phase_handoff"
        # Exact legacy shape AND key order (load-bearing for meta.json).
        assert list(run.session["phase_handoff"].items()) == [
            ("id", "validate_plan:plan_round:2"),
            ("phase", "validate_plan"),
            ("type", signal.type.value),
            ("trigger", "rejected"),
            ("verdict", "REJECTED"),
            ("approved", False),
            ("round_extras_key", "plan_round"),
            ("round", 2),
            ("loop_max_rounds", 2),
            ("available_actions", list(signal.available_actions)),
            ("artifacts", {"findings": [{"id": "F1"}]}),
            ("last_output", "round critique"),
        ]
        # available_actions / artifacts are copies, not aliases of the signal.
        assert run.session["phase_handoff"]["available_actions"] is not (
            signal.available_actions
        )
        assert run.session["phase_handoff"]["artifacts"] is not signal.artifacts


class TestRepeatRejectCoherence:
    """A human-directed retry that is rejected again re-pauses on a NEW
    handoff id (per-round id from ``build_phase_handoff_signal``), so the
    active payload never desyncs with the prior round's decision artifact,
    and round labels stay coherent for ``round > loop_max_rounds``."""

    def test_repause_carries_new_handoff_id_and_no_stale_decision(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / "20260603_160000_repause"
        run_dir.mkdir()
        # Prior round-2 decision artifact (the retry the operator already
        # decided). The re-pause must NOT point back at this id.
        _seed_continue_decision(
            run_dir,
            handoff_id="validate_plan:plan_round:2",
            phase="validate_plan",
        )
        # The retry round (3) was rejected again → a fresh pause at round 3.
        signal = _signal(handoff_id="validate_plan:plan_round:3", round_n=3)
        run = _pause_run(signal)

        apply_phase_handoff_pause(run)

        active = run.session["phase_handoff"]
        # NEW current payload with the new per-round id.
        assert active["id"] == "validate_plan:plan_round:3"
        assert active["round"] == 3
        # The new active id awaits a fresh decision — no stale artifact.
        with pytest.raises(RuntimeError):
            load_handoff_decision_validated(run_dir, active["id"])
        # The prior round-2 decision is distinct and not re-bound.
        assert active["id"] != "validate_plan:plan_round:2"

    def test_round_label_coherent_above_loop_max(self) -> None:
        # round=3 > loop_max_rounds=2 must never render the '3/2' fraction.
        label = render_round_label(
            phase="validate_plan",
            round=3,
            loop_max_rounds=2,
            human_directed=True,
            rejected_again=True,
        )
        assert "3/2" not in label
        assert "human retry 1" in label


class TestReviewRepairRetryResume:
    def _profile(self) -> Profile:
        return Profile(
            name="advanced",
            kind="advanced",
            description="advanced profile",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(phase="plan"),
                        PhaseStep(phase="validate_plan"),
                    ),
                    until="validate_plan.approved",
                    max_rounds=2,
                    round_extras_key="plan_round",
                ),
                PhaseStep(phase="implement"),
                LoopStep(
                    steps=(
                        PhaseStep(phase="review_changes"),
                        PhaseStep(phase="repair_changes"),
                    ),
                    until="review_changes.approved",
                    max_rounds=2,
                    round_extras_key="repair_round",
                ),
                PhaseStep(phase="final_acceptance"),
            ),
        )

    @pytest.mark.git_worktree
    def test_approved_repair_retry_skips_upstream_phases(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        """After a review retry is approved, resume continues at the tail.

        Regression: the successful retry outcome only marked
        review_changes/repair_changes completed. If checkpoint.completed was
        empty or incomplete, the stripped profile still began with the plan
        loop, so dispatch went back to PLAN after an approved review.
        """
        import pipeline.project.handoff as handoff_mod
        from sdk.phase_handoff import safe_handoff_id

        run_dir = tmp_path / "20260610_173948"
        run_dir.mkdir()
        handoff_id = "review_changes:repair_round:1"
        decisions_dir = run_dir / "phase_handoff_decisions"
        decisions_dir.mkdir()
        (decisions_dir / f"{safe_handoff_id(handoff_id)}.json").write_text(
            json.dumps(
                {
                    "run_id": run_dir.name,
                    "handoff_id": handoff_id,
                    "phase": "review_changes",
                    "action": "retry_feedback",
                    "feedback": "fix review blocker",
                    "note": None,
                    "decided_at": "2026-06-10T12:00:00+00:00",
                },
            ),
            encoding="utf-8",
        )
        # The review-retry guard (T2) proves the rejected diff subject is
        # present before dispatching repair. Give the run a dirty git repo as
        # its cwd so the guard passes — a real review-retry resume always
        # carries the rejected diff in its (retained) worktree.
        repo = tmp_path / "checkout"
        _init_dirty_repo(repo)
        state = PipelineState(
            task="t",
            project_dir=str(repo),
            plugin=PluginConfig(),
        )

        class _Metrics:
            def add_round(self) -> None:
                pass

            def save(self, _output_dir) -> None:
                pass

        class _Run:
            output_dir = run_dir
            session_ts = run_dir.name
            session = {
                "status": "awaiting_phase_handoff",
                "phases": {},
                "worktree": {"isolation": "off", "path": str(repo)},
                "phase_handoff": {
                    "id": handoff_id,
                    "phase": "review_changes",
                    "round": 1,
                    "loop_max_rounds": 1,
                    "last_output": "review rejected",
                },
            }
            _ckpt = None
            _metrics = _Metrics()

            def __init__(self) -> None:
                self.state = state

            def _on_phase_start(self, _name, _state) -> None:
                pass

            def _on_phase_end(self, _name, _state) -> None:
                pass

        def _dispatch_stub(step, current_state, _ctx, **_kwargs):
            if step.phase == "review_changes":
                current_state.phase_log["review_changes"] = {
                    "approved": True,
                    "clean": True,
                    "critique": "approved",
                }
            else:
                current_state.phase_log[step.phase] = {"ok": True}
            return current_state

        monkeypatch.setattr(
            handoff_mod,
            "_dispatch_via_fsm",
            _dispatch_stub,
        )
        run = _Run()
        outcome = handoff_mod.apply_phase_handoff_resume(
            run,
            self._profile(),
            SimpleNamespace(session_adapter_registry=None),
        )

        assert outcome.paused is False
        assert outcome.completed_phases == frozenset(
            {
                "plan",
                "validate_plan",
                "implement",
                "review_changes",
                "repair_changes",
            },
        )

        seen: list[str] = []
        reg = PhaseRegistry()
        for phase in (
            "plan",
            "validate_plan",
            "implement",
            "final_acceptance",
        ):
            reg.register(phase, lambda s, phase=phase: seen.append(phase) or s)

        run_profile(
            outcome.profile,
            state,
            reg,
            completed_phases=outcome.completed_phases,
        )

        assert seen == ["final_acceptance"]
