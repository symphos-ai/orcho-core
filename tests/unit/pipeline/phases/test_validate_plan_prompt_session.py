"""M7 wiring tests: per_phase delta rendering for validate_plan.

The :func:`pipeline.phases.builtin._session_aware_invoke` helper
sits between the validate_plan prompt builder and the agent
runtime. It takes a :class:`~pipeline.prompts.turn.PromptTurn`,
runs the M6 selector under :data:`PromptSessionSplit.PER_PHASE`,
and either sends the full prompt or a delta wire prompt depending
on whether the per-run :class:`PromptSessionState` has prior sent
parts.

Tests are scoped to the helper itself: they construct minimal
:class:`PipelineState` instances and a recording mock agent so the
M7 contract can be pinned without spinning up the full pipeline.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.phases.builtin import _session_aware_invoke
from pipeline.plugins import PluginConfig
from pipeline.prompts.session import (
    PromptSessionSplit,
)
from pipeline.prompts.turn import PromptTurn, PromptTurnEditor
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)
from pipeline.runtime import PipelineState

# ---------------------------------------------------------------------------
# Mock runtime + turn fixtures.
# ---------------------------------------------------------------------------


class _RecordingAgent:
    """Minimal agent stub that records every prompt it received.

    Mirrors the surface ``_session_aware_invoke`` pokes at:
    ``invoke()``, ``model``, ``session_id``. Every invocation pushes
    onto ``calls`` so tests can assert wire prompts and counts.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        responses: list[str] | None = None,
        session_id: str | None = "sess-1",
        raise_on_invoke: BaseException | None = None,
    ) -> None:
        self.model = model
        self.session_id = session_id
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or ["{}"])
        self._raise = raise_on_invoke

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        self.calls.append({
            "prompt": prompt,
            "cwd": cwd,
            "continue_session": continue_session,
            "attachments": attachments,
        })
        if self._raise is not None:
            raise self._raise
        if not self._responses:
            return "{}"
        head, *rest = self._responses
        self._responses = rest or [head]
        return head


def _state(*, run_id: str = "run-1") -> PipelineState:
    """Minimal PipelineState for helper-level tests."""
    return PipelineState(
        task="Fix calc.add",
        project_dir="/proj",
        plugin=PluginConfig(),
        extras={"run_id": run_id},
    )


def _role_part(name: str = "plan_reviewer") -> PromptPart:
    return PromptPart(
        kind="role",
        name=name,
        source="core",
        body=f"role:{name}",
        layer=PromptLayer.ROLE,
    )


def _phase_part(name: str = "validate_plan") -> PromptPart:
    return PromptPart(
        kind="task",
        name=name,
        source="core",
        body=f"phase:{name}",
        layer=PromptLayer.PHASE,
    )


def _system_tail_part(name: str = "review_json") -> PromptPart:
    return PromptPart(
        kind="system_tail",
        name=name,
        source="code-owned",
        body=f"<orcho:system-block>{name}</orcho:system-block>",
        layer=PromptLayer.CONTRACT,
    )


def _artifact_part(name: str = "artifact:validate_plan") -> PromptPart:
    return PromptPart(
        kind="artifact",
        name="validate_plan",
        source="code-owned",
        body="plan body",
        artifact_path="/tmp/plan.md",
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="reviewed file",
        id=name,
    )


def _make_turn(*parts: PromptPart) -> PromptTurn:
    """Build a PromptTurn from parts in order."""
    editor = PromptTurnEditor()
    for p in parts:
        editor.append(p)
    return editor.build()


# ---------------------------------------------------------------------------
# Branch coverage: round 1 (full) and round 2 (delta) under PER_PHASE.
# ---------------------------------------------------------------------------


class TestRoundOneAndTwoUnderPerPhase:
    def test_round1_full_prompt_in_per_phase_mode(self) -> None:
        agent = _RecordingAgent()
        state = _state()
        turn = _make_turn(
            _role_part(), _phase_part(), _system_tail_part(), _artifact_part(),
        )
        text = turn.text

        raw = _session_aware_invoke(
            agent, state,
            phase="validate_plan",
            turn=turn,
            cwd="/proj",
            continue_session=False,
        )
        assert raw == "{}"
        assert len(agent.calls) == 1
        # Round 1 sends the full envelope text on the wire.
        assert agent.calls[0]["prompt"] == text
        # The state_update committed for the next round.
        assert state.prompt_sessions, "state must record session for round 2"
        # Trace metadata reflects full render.
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["render_mode"] == "full"
        assert meta["wire_chars"] == len(text)
        assert meta["selected_part_keys"], "round 1 lists all selected keys"

    def test_round2_delta_prompt_in_per_phase_mode(self) -> None:
        # Round 1: prime the state with the full part set.
        agent = _RecordingAgent(responses=["round1", "round2"])
        state = _state()
        role = _role_part()
        phase_p = _phase_part()
        contract = _system_tail_part()
        artifact_r1 = _artifact_part()
        turn_r1 = _make_turn(role, phase_p, contract, artifact_r1)

        _session_aware_invoke(
            agent, state,
            phase="validate_plan",
            turn=turn_r1,
            cwd="/proj",
            continue_session=False,
        )
        # Capture state after round 1 to drive the round-2 selector.
        assert state.prompt_sessions

        # Round 2: same role + phase + contract, fresh artifact body.
        artifact_r2 = PromptPart(
            kind="artifact", name="validate_plan", source="code-owned",
            body="plan body v2",
            artifact_path="/tmp/plan.md",
            layer=PromptLayer.TURN,
            stability=PromptStability.TURN,
            cache_scope=PromptCacheScope.NONE,
            volatile_reason="reviewed file",
            id="artifact:validate_plan",
        )
        turn_r2 = _make_turn(role, phase_p, contract, artifact_r2)

        _session_aware_invoke(
            agent, state,
            phase="validate_plan",
            turn=turn_r2,
            cwd="/proj",
            continue_session=True,
        )

        round2_call = agent.calls[1]
        # Delta wire prompt: should contain the artifact body, must not
        # carry the cacheable role/phase/contract bodies on the wire.
        assert "plan body v2" in round2_call["prompt"]
        # Stable parts are omitted, so their bodies disappear from the
        # wire on the resumed turn.
        assert "role:plan_reviewer" not in round2_call["prompt"]
        # Trace metadata advances to delta and lists omissions.
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["render_mode"] == "delta"
        assert meta["omitted_part_keys"], "round 2 must omit something"
        # continue_session was True for the resumed call.
        assert round2_call["continue_session"] is True

    def test_round2_prompt_size_strictly_less_than_round1(self) -> None:
        agent = _RecordingAgent(responses=["r1", "r2"])
        state = _state()
        role = _role_part()
        phase_p = _phase_part()
        contract = _system_tail_part()

        turn_r1 = _make_turn(role, phase_p, contract, _artifact_part())
        _session_aware_invoke(
            agent, state, phase="validate_plan", turn=turn_r1,
            cwd="/proj", continue_session=False,
        )
        size_r1 = len(agent.calls[0]["prompt"])

        turn_r2 = _make_turn(role, phase_p, contract, _artifact_part())
        _session_aware_invoke(
            agent, state, phase="validate_plan", turn=turn_r2,
            cwd="/proj", continue_session=True,
        )
        size_r2 = len(agent.calls[1]["prompt"])

        # Threshold-based: round 2 wire prompt is strictly smaller
        # than round 1 because stable parts were omitted. No exact
        # byte assertion — that would be M10's prose-rewrite turf.
        assert size_r2 < size_r1

    def test_prior_state_without_continue_session_forces_full_render(
        self,
    ) -> None:
        agent = _RecordingAgent(responses=["r1", "r2"])
        state = _state()
        role = _role_part()
        phase_p = _phase_part()
        contract = _system_tail_part()

        turn_r1 = _make_turn(role, phase_p, contract, _artifact_part())
        _session_aware_invoke(
            agent, state, phase="validate_plan", turn=turn_r1,
            cwd="/proj", continue_session=False,
        )
        assert state.prompt_sessions

        turn_r2 = _make_turn(role, phase_p, contract, _artifact_part())
        _session_aware_invoke(
            agent, state, phase="validate_plan", turn=turn_r2,
            cwd="/proj", continue_session=False,
        )

        round2_call = agent.calls[1]
        assert round2_call["prompt"] == turn_r2.text
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["render_mode"] == "full"
        assert meta["omitted_part_keys"] == []


# ---------------------------------------------------------------------------
# Stateless and fallback branches.
# ---------------------------------------------------------------------------


class TestStatelessAndFallback:
    def test_stateless_helper_keeps_full_prompts_every_round(self) -> None:
        agent = _RecordingAgent(responses=["r1", "r2"])
        state = _state()
        turn = _make_turn(
            _role_part(), _phase_part(), _system_tail_part(), _artifact_part(),
        )
        text = turn.text
        for _ in range(2):
            _session_aware_invoke(
                agent, state,
                phase="validate_plan",
                turn=_make_turn(
                    _role_part(), _phase_part(), _system_tail_part(),
                    _artifact_part(),
                ),
                cwd="/proj",
                continue_session=False,
                split=PromptSessionSplit.STATELESS,
            )
        # Both calls saw the full prompt (same parts → same text).
        for call in agent.calls:
            assert call["prompt"] == text
        # Stateless leaves prompt_sessions empty — no reusable state.
        assert state.prompt_sessions == {}
        # Trace metadata reports full render both times.
        assert (
            state.phase_log["validate_plan"]["prompt_render"]["render_mode"]
            == "full"
        )

    def test_missing_envelope_falls_back_to_full_prompt(self) -> None:
        # When the turn has no stable-prefix parts the selector still
        # operates — the turn's text is sent in full.
        agent = _RecordingAgent()
        state = _state()
        # Build a minimal turn with only a TURN/NONE part (no
        # prefix-eligible parts) so the selector has nothing to omit.
        part = PromptPart(
            kind="task", name="test_prompt", source="core",
            body="raw prompt body",
            stability=PromptStability.TURN,
            cache_scope=PromptCacheScope.NONE,
            volatile_reason="test prompt",
        )
        editor = PromptTurnEditor()
        editor.append(part)
        turn = editor.build()
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=turn, cwd="/proj", continue_session=False,
        )
        assert agent.calls[0]["prompt"] == "raw prompt body"
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["render_mode"] == "full"


# ---------------------------------------------------------------------------
# Runtime / model boundary regressions.
# ---------------------------------------------------------------------------


class TestRuntimeModelBoundary:
    def _seed_round1(self, agent, state) -> None:
        turn = _make_turn(
            _role_part(), _phase_part(), _system_tail_part(), _artifact_part(),
        )
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=turn,
            cwd="/proj", continue_session=False,
        )

    def test_runtime_change_forces_full_render(self) -> None:
        state = _state()
        agent_a = _RecordingAgent(model="m")
        self._seed_round1(agent_a, state)
        # A different runtime instance with a different class should
        # produce a different PhysicalSessionKey, so the second call
        # misses cache and falls back to a full render.

        class _OtherRuntime(_RecordingAgent):
            pass

        agent_b = _OtherRuntime(model="m")
        turn = _make_turn(
            _role_part(), _phase_part(), _system_tail_part(), _artifact_part(),
        )
        _session_aware_invoke(
            agent_b, state, phase="validate_plan",
            turn=turn,
            cwd="/proj", continue_session=True,
        )
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["render_mode"] == "full"
        # The two runtime keys coexist in state.prompt_sessions.
        assert len(state.prompt_sessions) == 2

    def test_model_change_forces_full_render(self) -> None:
        state = _state()
        agent_a = _RecordingAgent(model="opus")
        self._seed_round1(agent_a, state)
        agent_b = _RecordingAgent(model="sonnet")
        turn = _make_turn(
            _role_part(), _phase_part(), _system_tail_part(), _artifact_part(),
        )
        _session_aware_invoke(
            agent_b, state, phase="validate_plan",
            turn=turn,
            cwd="/proj", continue_session=True,
        )
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["render_mode"] == "full"


# ---------------------------------------------------------------------------
# Trace metadata + commit-on-success rules.
# ---------------------------------------------------------------------------


class TestTraceAndCommitRules:
    def test_trace_records_selected_and_omitted_part_keys(self) -> None:
        agent = _RecordingAgent(responses=["r1", "r2"])
        state = _state()
        role = _role_part()
        phase_p = _phase_part()
        contract = _system_tail_part()
        turn1 = _make_turn(role, phase_p, contract, _artifact_part())
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=turn1, cwd="/proj", continue_session=False,
        )
        turn2 = _make_turn(role, phase_p, contract, _artifact_part())
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=turn2, cwd="/proj", continue_session=True,
        )
        meta = state.phase_log["validate_plan"]["prompt_render"]
        # M5/M6 composite key shape: id@version. The role / phase /
        # contract anchors stay stable, so they appear in the
        # omitted set on the resumed turn.
        for omitted in meta["omitted_part_keys"]:
            assert "@" in omitted
        # session_key dict view — stable shape for M12 persistence.
        assert meta["session_key"]["scope"] == "per_phase:validate_plan"

    def test_agent_exception_does_not_commit_prompt_session_state(
        self,
    ) -> None:
        boom = RuntimeError("provider failure")
        agent = _RecordingAgent(raise_on_invoke=boom)
        state = _state()
        turn = _make_turn(
            _role_part(), _phase_part(), _system_tail_part(), _artifact_part(),
        )
        with pytest.raises(RuntimeError, match="provider failure"):
            _session_aware_invoke(
                agent, state, phase="validate_plan",
                turn=turn, cwd="/proj", continue_session=False,
            )
        # Commit-on-success rule: a failed invoke leaves the cache
        # untouched so a retry sees the same "no prior sends" state.
        assert state.prompt_sessions == {}
        assert "validate_plan" not in state.phase_log

    def test_parse_failure_still_commits_after_successful_invoke(
        self,
    ) -> None:
        # Parse errors happen AFTER the provider returned text — the
        # cache view should still record what the agent received,
        # so a retry-after-parse-error doesn't double-send the
        # already-cached parts.
        agent = _RecordingAgent(responses=["this is not json"])
        state = _state()
        turn = _make_turn(
            _role_part(), _phase_part(), _system_tail_part(), _artifact_part(),
        )
        raw = _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=turn, cwd="/proj", continue_session=False,
        )
        # The helper itself does not parse; it returns whatever the
        # provider produced. Parsing happens later in
        # _phase_validate_plan. The state commit and trace happen
        # purely on the provider returning text — that is the M7
        # contract.
        assert raw == "this is not json"
        assert state.prompt_sessions, "successful invoke must commit"
        assert "prompt_render" in state.phase_log["validate_plan"]


# ---------------------------------------------------------------------------
# Fix 7 — plan_contract envelope fidelity.
# ---------------------------------------------------------------------------


class TestValidatePlanTypedParts:
    """``_review_plan_artifact`` routes via ``state.parsed_plan`` and
    emits the typed ``plan_contract:typed_plan`` + ``plan_tasks:
    validate_plan`` parts — the *what* the reviewer reads. The
    monolithic ``artifact:validate_plan`` part is gone.
    """

    def test_validate_plan_wire_emits_typed_plan_parts(
        self, tmp_path,
    ) -> None:
        """Round 1 validate_plan renders full and surfaces the typed
        plan parts in the selected part keys. PR3 cutover:
        ``_review_plan_artifact`` routes via ``state.parsed_plan`` (the
        canonical contract), not via ``state.extras["plan_artifact_path"]``
        + markdown re-read.
        """
        from agents.entities import SubTask
        from pipeline.phases.builtin import _review_plan_artifact
        from pipeline.plan_parser import ParsedPlan
        from pipeline.runtime import PipelineState

        parsed_plan = ParsedPlan(
            short_summary="Stub plan body.",
            planning_context="Stub planning context.",
            subtasks=(
                SubTask(id="t1", goal="Demo subtask."),
            ),
            source="json",
            goal="Demo goal",
            acceptance_criteria=("X",),
        )
        state = PipelineState(
            task="Demo task",
            project_dir=str(tmp_path),
            plugin=PluginConfig(),
            extras={"run_id": "run-typed-parts"},
        )
        state.parsed_plan = parsed_plan
        state.plan_markdown = "# Plan\n\nStub plan body.\n"
        agent = _RecordingAgent()

        _review_plan_artifact(
            agent, state, focus="(unused)", cwd=str(tmp_path),
        )

        wire = agent.calls[0]["prompt"]
        meta = state.phase_log["validate_plan"]["prompt_render"]
        # Round 1 is a full render — wire_chars equals the wire length.
        assert meta["render_mode"] == "full"
        assert meta["wire_chars"] == len(wire)
        # PR3: validate_plan emits a typed plan_contract part
        # (rendered from state.parsed_plan) AND a plan_tasks part.
        keys = list(meta["selected_part_keys"])
        assert any("plan_contract:typed_plan" in key for key in keys)
        assert any("plan_tasks:execution_plan" in key for key in keys)
        assert not any("artifact:validate_plan" in key for key in keys)

    def test_replan_validate_receives_receipt_and_current_plan_subject(
        self, tmp_path,
    ) -> None:
        from agents.entities import SubTask
        from pipeline.phases.builtin import _review_plan_artifact
        from pipeline.plan_parser import ParsedPlan
        from pipeline.repair_protocol import (
            build_repair_receipt,
            repair_receipt_to_dict,
        )
        from pipeline.runtime import PipelineState

        parsed_plan = ParsedPlan(
            short_summary="Updated plan body.",
            planning_context="Updated planning context.",
            subtasks=(SubTask(id="t1", goal="Updated subtask."),),
            source="json",
            goal="Updated goal",
            acceptance_criteria=("Fresh AC",),
        )
        state = PipelineState(
            task="Demo task",
            project_dir=str(tmp_path),
            plugin=PluginConfig(),
            extras={
                "run_id": "run-replan-receipt",
                "plan_round": 2,
                "_last_repair_receipt": repair_receipt_to_dict(
                    build_repair_receipt(
                        source_phase="validate_plan",
                        source_round=1,
                        repair_phase="plan",
                        repair_round=2,
                        critique="F1: missing AC.",
                        repair_output="Added Fresh AC.",
                        changed_refs=("parsed_plan",),
                    )
                ),
            },
        )
        state.parsed_plan = parsed_plan
        agent = _RecordingAgent()

        _review_plan_artifact(
            agent, state, focus="(unused)", cwd=str(tmp_path),
        )

        prompt = agent.calls[0]["prompt"]
        assert "## Repair Receipt" in prompt
        assert "## Current Plan Subject" in prompt
        assert "Fresh AC" in prompt
        current_subject = prompt.split("## Current Plan Subject", 1)[1]
        assert "`plan_contract:typed_plan`" in current_subject
        assert "`plan_tasks:execution_plan`" in current_subject
        assert "Subject hash: sha256:" in current_subject
        assert "Fresh AC" not in current_subject
        assert "Updated subtask." not in current_subject
        meta = state.phase_log["validate_plan"]["prompt_render"]
        keys = set(meta["selected_part_keys"])
        assert "repair_receipt:latest@0" in keys
        assert "current_review_subject:latest@0" in keys

    def test_validate_plan_hard_fails_without_parsed_plan(
        self, tmp_path,
    ) -> None:
        """PR3 hard-fail invariant: if a plan markdown is persisted
        but ``state.parsed_plan`` is None, ``_review_plan_artifact``
        refuses to silently re-parse the markdown — it raises
        instead. Markdown is a projection, not a round-trip-safe
        source; reconstructing from prose would lose typed contract
        fields. The durable machine source lives in
        ``pipeline.plan_artifacts`` (parsed_plan.json)."""
        from pipeline.phases.builtin import _review_plan_artifact
        from pipeline.runtime import PipelineState

        plan_path = tmp_path / "plan.md"
        plan_path.write_text("# Plan\n\nLegacy markdown body.\n")
        state = PipelineState(
            task="Demo task",
            project_dir=str(tmp_path),
            plugin=PluginConfig(),
            extras={
                "run_id": "run-hard-fail",
                "plan_artifact_path": str(plan_path),
            },
        )
        # parsed_plan intentionally not set — torn runtime state.
        agent = _RecordingAgent()
        with pytest.raises(RuntimeError) as exc:
            _review_plan_artifact(
                agent, state, focus="(unused)", cwd=str(tmp_path),
            )
        # Message must name the offending state so an operator can
        # diagnose the upstream phase that produced markdown without
        # threading the parsed object.
        msg = str(exc.value)
        assert "parsed_plan is None" in msg
        assert str(plan_path) in msg
        # Agent must NOT have been invoked — the failure is detected
        # before any wire bytes leave the process.
        assert agent.calls == []


class TestPlanContractEnvelopeFidelityM11_5:
    """PR3 cutover: ``plan_file_review_prompt`` now emits typed views
    rendered from :class:`ParsedPlan` instead of a monolithic
    plan-markdown artifact. The plan_contract:typed_plan part IS
    expected on this surface (rendered from the parsed plan's typed
    contract fields), and the plan_tasks:execution_plan part carries
    the decomposition. The legacy ``artifact:validate_plan`` part
    is gone on this surface.
    """

    def test_plan_file_review_prompt_models_typed_plan_views(
        self,
    ) -> None:
        from agents.entities import SubTask
        from pipeline.plan_parser import ParsedPlan
        from pipeline.prompts.builders import plan_file_review_prompt

        parsed_plan = ParsedPlan(
            short_summary="Stub plan.",
            planning_context="Stub.",
            subtasks=(SubTask(id="t1", goal="Demo task body."),),
            source="json",
            goal="Demo goal",
            acceptance_criteria=("Stub criterion",),
        )
        turn = plan_file_review_prompt(
            parsed_plan, "Demo task", PluginConfig(),
            project_dir="/proj",
        )
        # Builder now returns a PromptTurn; get envelope from it.
        env = turn.envelope()
        wire = turn.text
        assert env.text == wire

        kinds = {p.kind for p in env.parts}
        # PR3: typed contract + decomposition views are present.
        assert "plan_contract" in kinds
        assert "plan_tasks" in kinds
        # The monolithic artifact part on this surface is gone.
        plan_artifact_parts = [
            p for p in env.parts
            if p.kind == "artifact" and p.name == "validate_plan"
        ]
        assert plan_artifact_parts == []

    def test_review_focus_models_plan_contract_and_handoff(self) -> None:
        from pipeline.prompts.builders import review_focus

        turn = review_focus(
            "Demo task",
            PluginConfig(),
            project_dir="/proj",
            plan_contract="CONTRACT BODY",
            plan_tasks="TASKS BODY",
            handoff_contract="HANDOFF BODY",
        )
        # Builder now returns a PromptTurn; get envelope from it.
        env = turn.envelope()
        wire = turn.text
        assert env.text == wire
        assert "HANDOFF BODY" in wire
        assert "CONTRACT BODY" in wire
        assert "TASKS BODY" in wire
        kinds = [p.kind for p in env.parts]
        assert "handoff_contract" in kinds
        assert "plan_contract" in kinds
        assert "plan_tasks" in kinds
        # Handoff comes before plan_contract in render order (the
        # legacy ``_prepend_block`` wrapper ordering).
        assert kinds.index("handoff_contract") < kinds.index(
            "plan_contract",
        )
        assert kinds.index("plan_contract") < kinds.index("plan_tasks")


# ---------------------------------------------------------------------------
# ADR 0026 resume-delta: the original task is dropped from the wire on a
# resumed validate retry (already in history), but always sent on round 1
# and in stateless mode.
# ---------------------------------------------------------------------------


def _task_part() -> PromptPart:
    return PromptPart(
        kind="turn_input", name="validate_plan_task", source="code-owned",
        body="TASK:\n<full original task>",
        layer=PromptLayer.TURN, stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE, volatile_reason="per-turn",
        id="turn_input:validate_plan_task",
    )


_DROP = ("turn_input:validate_plan_task",)


class TestResumeDeltaTaskDrop:
    def test_round1_sends_task_round2_resumed_drops_it(self) -> None:
        agent = _RecordingAgent(responses=["r1", "r2"])
        state = _state()
        role, phase_p, contract = _role_part(), _phase_part(), _system_tail_part()
        task = _task_part()

        # Round 1 (fresh) — full render, task on the wire.
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=_make_turn(role, phase_p, contract, task, _artifact_part()),
            cwd="/proj", continue_session=False,
            delta_droppable_part_ids=_DROP,
        )
        assert "<full original task>" in agent.calls[0]["prompt"]
        assert (
            state.phase_log["validate_plan"]["prompt_render"]["render_mode"]
            == "full"
        )

        # Round 2 (resumed) — delta render, task dropped, new plan kept.
        artifact_r2 = PromptPart(
            kind="artifact", name="validate_plan", source="code-owned",
            body="plan body v2", artifact_path="/tmp/plan.md",
            layer=PromptLayer.TURN, stability=PromptStability.TURN,
            cache_scope=PromptCacheScope.NONE, volatile_reason="reviewed file",
            id="artifact:validate_plan",
        )
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=_make_turn(role, phase_p, contract, task, artifact_r2),
            cwd="/proj", continue_session=True,
            delta_droppable_part_ids=_DROP,
        )
        r2 = agent.calls[1]
        assert "plan body v2" in r2["prompt"]
        # Task is gone from the round-2 wire.
        assert "<full original task>" not in r2["prompt"]
        meta = state.phase_log["validate_plan"]["prompt_render"]
        assert meta["render_mode"] == "delta"
        assert "turn_input:validate_plan_task@0" in meta["delta_dropped_part_keys"]
        assert (
            "turn_input:validate_plan_task@0" not in meta["selected_part_keys"]
        )

    def test_stateless_keeps_task_every_round(self) -> None:
        agent = _RecordingAgent(responses=["r1", "r2"])
        state = _state()
        for _ in range(2):
            _session_aware_invoke(
                agent, state, phase="validate_plan",
                turn=_make_turn(_role_part(), _task_part(), _artifact_part()),
                cwd="/proj", continue_session=False,
                split=PromptSessionSplit.STATELESS,
                delta_droppable_part_ids=_DROP,
            )
        for call in agent.calls:
            assert "<full original task>" in call["prompt"]

    def test_round1_no_prior_session_keeps_task_even_with_continue_flag(
        self,
    ) -> None:
        # continue_session=True but no committed session yet → fresh state →
        # full render → task stays (fail-safe).
        agent = _RecordingAgent(responses=["r1"])
        state = _state()
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=_make_turn(_role_part(), _task_part(), _artifact_part()),
            cwd="/proj", continue_session=True,
            delta_droppable_part_ids=_DROP,
        )
        assert "<full original task>" in agent.calls[0]["prompt"]
        assert (
            state.phase_log["validate_plan"]["prompt_render"]["render_mode"]
            == "full"
        )

    def test_model_change_between_rounds_resends_task(self) -> None:
        # Round 1 seeds; round 2 with a different model → fresh key →
        # full render → task re-sent.
        agent = _RecordingAgent(responses=["r1", "r2"])
        state = _state()
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=_make_turn(_role_part(), _task_part(), _artifact_part()),
            cwd="/proj", continue_session=False,
            delta_droppable_part_ids=_DROP,
        )
        agent.model = "claude-sonnet-4-6"  # model swap invalidates the key
        _session_aware_invoke(
            agent, state, phase="validate_plan",
            turn=_make_turn(_role_part(), _task_part(), _artifact_part()),
            cwd="/proj", continue_session=True,
            delta_droppable_part_ids=_DROP,
        )
        assert "<full original task>" in agent.calls[1]["prompt"]
        assert (
            state.phase_log["validate_plan"]["prompt_render"]["render_mode"]
            == "full"
        )
