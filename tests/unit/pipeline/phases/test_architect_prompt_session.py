"""M8 wiring tests: per_phase delta rendering for plan and replan.

The architect side of ``_phase_plan`` opts into the M2/M5/M6
session-aware stack the same way ``_review_plan_artifact`` does in
M7. Both the plan and replan branches use ``phase="plan"`` so the
M5 :class:`PhysicalSessionKey` is shared — replan is round 2+ of
the plan loop, not a separate phase, and the M6 selector must see
the parts seeded by round 1.

The brief calls out two M8-specific risks the tests must pin:

1. Out-of-builder text (``prompt_prefix``, codemap, hypothesis
   suffix) is appended after the M2 builder gateway publishes its
   envelope. The handler must rebuild the envelope with those
   additions as :class:`PromptPart` instances, otherwise full
   render would silently drop them or the M6 selector would
   classify them stale.
2. ``prompt_render`` trace metadata stashed by
   :func:`_session_aware_invoke` must survive the success and
   parse-error overwrites of ``state.phase_log["plan"]``.

Tests stay scoped to the helper interaction at the handler layer
— they construct minimal pipeline state and a recording mock
agent, then exercise ``_phase_plan`` end-to-end.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from core.observability import prompt_trace
from pipeline.lifecycle import default_lifecycle_context
from pipeline.phases.builtin import _phase_plan
from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    Attachment,
    AttachmentKind,
    PhaseRegistry,
    PipelineState,
)

# ---------------------------------------------------------------------------
# Minimal mock surface.
# ---------------------------------------------------------------------------


def _approved_plan_json() -> str:
    """Smallest valid plan JSON — keeps parse_plan happy in tests."""
    return json.dumps({
        "short_summary":      "Stub plan for M8 wiring tests.",
        "planning_context":   "Test fixture only.",
        "goal":               "Exercise plan/replan session wiring.",
        "acceptance_criteria": ["pipeline returns"],
        "risks":              [],
        "review_focus":       [],
        "tasks": [{
            "id":             "T1",
            "spec":           "Stub task body for M8 wiring tests.",
            "goal":           "Exercise.",
            "files":          [],
            "depends_on":     [],
            "done_criteria":  ["pipeline returns"],
        }],
    })


class _RecordingAgent:
    """Mock plan agent. Records every wire prompt + invoke kwargs."""

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        model: str = "claude-opus-4-7",
        session_id: str | None = "sess-arch-1",
    ) -> None:
        self.model = model
        self.session_id = session_id
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or [_approved_plan_json()])

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
        head, *rest = self._responses
        self._responses = rest or [head]
        return head


def _make_state(
    *,
    plan_round: int = 1,
    last_critique: str = "",
    codemap: str = "",
    validated_hypothesis: str = "",
    attachments: tuple[Attachment, ...] = (),
    plugin: PluginConfig | None = None,
) -> PipelineState:
    """Build a PipelineState wired for plan/replan invocation."""
    plugin = plugin or PluginConfig()
    extras: dict[str, Any] = {
        "run_id": "run-arch-1",
        "plan_round": plan_round,
    }
    if codemap:
        extras["codemap"] = codemap
    if validated_hypothesis:
        extras["validated_hypothesis"] = validated_hypothesis
    state = PipelineState(
        task="Add structured logging",
        project_dir="/proj",
        plugin=plugin,
        extras=extras,
        last_critique=last_critique,
    )
    state.attachments = attachments
    # ADR 0113 (declarative continuity): the plan handler resolves continuity
    # off the active step's execution policy. These handler-level tests bypass
    # the FSM (which seeds active_step from the v2 profile in production), so
    # seed a real lifecycle context (it carries ``plan_helpers`` the handler
    # needs) and set its active step to plan's real declared continuity —
    # ``loop_continue`` — with the per_phase default split. Round 1 is fresh
    # under loop_continue; round 2+ resumes the prior plan-loop session (the
    # restored pre-0113 behaviour this change ships).
    ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
    ctx.active_step = SimpleNamespace(
        prompt=None,
        execution_policy=SimpleNamespace(
            session_split=None, session_continuity="loop_continue",
        ),
    )
    state.lifecycle_ctx = ctx
    return state


def _install_agent(state: PipelineState, agent: _RecordingAgent) -> None:
    """Wire the recording agent into state.phase_config the way
    ``_require_agent`` reads it.

    PhaseAgentConfig only requires an attribute lookup that returns
    a non-None object on each ``*_agent`` slot, so a tiny stand-in
    namespace works for handler-level tests without instantiating
    the full registry.
    """
    from types import SimpleNamespace

    state.phase_config = SimpleNamespace(
        plan_agent=agent,
        validate_plan_agent=agent,
        implement_agent=agent,
        review_agent=agent,
        repair_agent=agent,
        final_acceptance_agent=agent,
    )


# ---------------------------------------------------------------------------
# Plan branch: round 1 full render under PER_PHASE.
# ---------------------------------------------------------------------------


class TestPlanRoundOne:
    def test_plan_round1_full_prompt_in_per_phase_mode(self) -> None:
        agent = _RecordingAgent()
        state = _make_state()
        _install_agent(state, agent)

        _phase_plan(state)

        assert len(agent.calls) == 1
        # Round 1 sends the full prompt. The session record commits
        # for round 2 to find it.
        assert state.prompt_sessions, "round 1 must seed prompt_sessions"
        meta = state.phase_log["plan"]["prompt_render"]
        assert meta["render_mode"] == "full"
        assert meta["session_key"]["scope"] == "per_phase:plan"


# ---------------------------------------------------------------------------
# Replan branch: round 2 must hit the same PER_PHASE:plan key.
# ---------------------------------------------------------------------------


class TestReplanShareSessionKey:
    def test_replan_round2_resume_uses_same_plan_session_key(self) -> None:
        # Round 1: drive a full plan to seed state.prompt_sessions.
        agent = _RecordingAgent(
            responses=[_approved_plan_json(), _approved_plan_json()],
        )
        state = _make_state(plan_round=1)
        _install_agent(state, agent)
        _phase_plan(state)

        # Capture the PhysicalSessionKey from round 1.
        keys_round1 = list(state.prompt_sessions.keys())
        assert len(keys_round1) == 1
        round1_key = keys_round1[0]
        assert round1_key.scope == "per_phase:plan"

        # Round 2: replan with a critique. Same state, plan_round=2,
        # last_critique non-empty triggers replan branch.
        state.extras["plan_round"] = 2
        state.last_critique = "Reviewer says: missing rollback plan."
        _phase_plan(state)

        # The replan branch must NOT create a second key under
        # per_phase:replan — it shares the plan key.
        keys_round2 = list(state.prompt_sessions.keys())
        assert len(keys_round2) == 1, (
            "replan must reuse plan's PhysicalSessionKey, not create a new one"
        )
        assert keys_round2[0] == round1_key

        # ADR 0113 (declarative continuity): plan declares ``loop_continue``,
        # so round 2 RESUMES the prior plan-loop session and renders delta
        # while reusing the same per_phase:plan session key. The new critique
        # rides the resumed wire; the original task is dropped (already in the
        # architect's history). This is the restored pre-0113 behaviour.
        meta = state.phase_log["plan"]["prompt_render"]
        assert meta["render_mode"] == "delta"

    def test_replan_round2_sends_critique_payload(self) -> None:
        agent = _RecordingAgent(
            responses=[_approved_plan_json(), _approved_plan_json()],
        )
        state = _make_state(plan_round=1)
        _install_agent(state, agent)
        _phase_plan(state)

        # Round 2 with critique.
        critique = "Reviewer flag: rollback plan missing."
        state.extras["plan_round"] = 2
        state.last_critique = critique
        _phase_plan(state)

        round2_prompt = agent.calls[1]["prompt"]
        # Critique text must be in the wire prompt — the replan
        # task body substitutes it as a turn variable.
        assert critique in round2_prompt

    def test_replan_round2_reuses_plan_session_under_resumed_render(
        self,
    ) -> None:
        agent = _RecordingAgent(
            responses=[_approved_plan_json(), _approved_plan_json()],
        )
        state = _make_state(plan_round=1)
        _install_agent(state, agent)
        _phase_plan(state)

        round1_prompt = agent.calls[0]["prompt"]
        # Round 1 includes the systems_architect role body anchor.
        assert "You are the solution architect" in round1_prompt

        state.extras["plan_round"] = 2
        state.last_critique = "Some critique."
        _phase_plan(state)

        # ADR 0113 (declarative continuity): plan declares loop_continue, so
        # round 2 RESUMES and renders delta. The contract pinned here is that
        # round 2 reuses the per_phase:plan session key (it does not fork a
        # per_phase:replan key) — the key plumbing is observable on the trace.
        meta = state.phase_log["plan"]["prompt_render"]
        assert meta["render_mode"] == "delta"
        assert meta["session_key"]["scope"] == "per_phase:plan"


# ---------------------------------------------------------------------------
# Out-of-builder text guard — the load-bearing M8 risk.
# ---------------------------------------------------------------------------


class TestOutOfBuilderTextSurvivesFullRender:
    def test_prompt_prefix_codemap_and_hypothesis_suffix_are_not_dropped(
        self, tmp_path,
    ) -> None:
        agent = _RecordingAgent()
        spec = tmp_path / "spec.md"
        spec.write_text("Project rules: do not break the build.", encoding="utf-8")
        att = Attachment(
            kind=AttachmentKind.TEXT, name="spec.md", content_path=str(spec),
        )
        codemap = "src/core.py\nsrc/api.py\ntests/test_core.py"
        hypothesis = "Hypothesis: payload key was renamed in v3 schema."
        state = _make_state(
            codemap=codemap,
            validated_hypothesis=hypothesis,
            attachments=(att,),
        )
        _install_agent(state, agent)
        _phase_plan(state)

        wire = agent.calls[0]["prompt"]
        # The TEXT-attachment prefix must survive — the helper's full
        # render must use the assembled full_prompt, not envelope.text
        # alone.
        assert "ATTACHMENTS:" in wire
        assert "Project rules: do not break the build" in wire
        # Codemap block must survive in its REPO MAP envelope.
        assert "--- REPO MAP ---" in wire
        assert "src/core.py" in wire
        # Hypothesis suffix must survive.
        assert "payload key was renamed in v3 schema" in wire

    def test_envelope_includes_prefix_codemap_and_suffix_as_parts(
        self, tmp_path,
    ) -> None:
        agent = _RecordingAgent()
        spec = tmp_path / "spec.md"
        spec.write_text("attachment body", encoding="utf-8")
        att = Attachment(
            kind=AttachmentKind.TEXT, name="spec.md", content_path=str(spec),
        )
        state = _make_state(
            codemap="src/x.py",
            validated_hypothesis="Hypothesis text.",
            attachments=(att,),
        )
        _install_agent(state, agent)
        _phase_plan(state)

        meta = state.phase_log["plan"]["prompt_render"]
        # The trace records selected part keys; out-of-builder
        # additions must appear among them so M6 can drive delta on
        # the cacheable prefix part (the TEXT-attachment text_prefix).
        keys = meta["selected_part_keys"]
        assert any("text_prefix:attachments" in k for k in keys)
        assert any("codemap:repo_map" in k for k in keys)
        assert any("hypothesis_suffix:hypothesis_context" in k for k in keys)


# ---------------------------------------------------------------------------
# Plan artifact boundary contract still arrives.
# ---------------------------------------------------------------------------


class TestProtectedContractsPresent:
    def test_plan_artifact_boundary_present_on_plan_round(self) -> None:
        agent = _RecordingAgent()
        state = _make_state()
        _install_agent(state, agent)
        _phase_plan(state)

        wire = agent.calls[0]["prompt"]
        assert 'name="plan_artifact_boundary"' in wire

    def test_contract_change_resends_contract_parts(self) -> None:
        # Round 1 with default change_handoff = "uncommitted".
        agent = _RecordingAgent(
            responses=[_approved_plan_json(), _approved_plan_json()],
        )
        state = _make_state(plan_round=1)
        _install_agent(state, agent)
        _phase_plan(state)

        # Round 2: replan with a change_handoff override stashed in
        # extras (the helper reads it through _change_handoff_for).
        state.extras["plan_round"] = 2
        state.last_critique = "Critique."
        state.extras["change_handoff"] = "commit"
        _phase_plan(state)

        round2_prompt = agent.calls[1]["prompt"]
        # The change_handoff contract body has mode-specific text;
        # a mode flip must put the new contract on the wire.
        assert 'name="change_handoff"' in round2_prompt


# ---------------------------------------------------------------------------
# prompt_render preservation across phase_log overwrites.
# ---------------------------------------------------------------------------


class TestPromptRenderPreservation:
    def test_plan_phase_log_preserves_prompt_render_on_success(self) -> None:
        agent = _RecordingAgent()
        state = _make_state()
        _install_agent(state, agent)
        _phase_plan(state)

        # Successful plan path overwrites state.phase_log["plan"]
        # with parsed_plan summary; prompt_render must survive that
        # rewrite so M12 trace persistence sees the render metadata.
        plan_log = state.phase_log["plan"]
        assert "prompt_render" in plan_log
        assert plan_log["prompt_render"]["session_key"]["scope"] == \
            "per_phase:plan"

    def test_plan_phase_log_preserves_prompt_render_on_parse_failure(
        self,
    ) -> None:
        # Provider returns non-JSON garbage; parse_plan raises and the
        # parse-error branch overwrites state.phase_log["plan"].
        agent = _RecordingAgent(responses=["this is not json"])
        state = _make_state()
        _install_agent(state, agent)
        with patch(
            "pipeline.phases.builtin.handlers.plan._render_parse_failure",
            return_value="",
        ):
            _phase_plan(state)

        plan_log = state.phase_log["plan"]
        assert "parse_error" in plan_log, "parse-error branch must run"
        assert "prompt_render" in plan_log, (
            "prompt_render must survive parse-error overwrite of "
            "state.phase_log['plan']"
        )


# ---------------------------------------------------------------------------
# Smoke: dry-run still works (no helper invocation).
# ---------------------------------------------------------------------------


class TestDryRunBypass:
    def test_dry_run_skips_session_aware_invocation(self) -> None:
        agent = _RecordingAgent()
        state = _make_state()
        state.dry_run = True
        _install_agent(state, agent)
        _phase_plan(state)

        # Dry-run path bypasses agent invocation entirely; nothing
        # was sent on the wire and no prompt session committed.
        assert agent.calls == []
        assert state.prompt_sessions == {}
        # The plan log is still written for orchestrator consumption.
        assert state.phase_log["plan"]["output"] == "[DRY RUN]"


# ---------------------------------------------------------------------------
# Pytest hooks
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _drain_render_envelope():
    """Ensure each test starts with a clean prompt_trace slot."""
    prompt_trace.take_last_upper()
    yield
    prompt_trace.take_last_upper()


class TestReplanResumeDropsTask:
    """ADR 0113 (declarative continuity): plan declares ``loop_continue``, so a
    round-2 replan RESUMES the prior plan-loop session (the restored pre-0113
    behaviour). On a resumed delta render the ADR 0026 drop-on-resume
    optimization applies: the original task is already in the architect's
    conversation history, so it is dropped from the round-2 wire while the new
    critique (a distinct volatile part) still rides it.
    """

    def test_task_dropped_on_resumed_replan(self) -> None:
        agent = _RecordingAgent(
            responses=[_approved_plan_json(), _approved_plan_json()],
        )
        state = _make_state(plan_round=1)
        _install_agent(state, agent)
        _phase_plan(state)

        # Round 1 carries the full task on the wire.
        assert "Add structured logging" in agent.calls[0]["prompt"]

        # Round 2 replan — loop_continue RESUMES, so a delta render.
        state.extras["plan_round"] = 2
        state.last_critique = "Reviewer: tie logging to request id."
        _phase_plan(state)

        meta = state.phase_log["plan"]["prompt_render"]
        assert meta["render_mode"] == "delta"
        # The task part is dropped on the resumed wire (ADR 0026).
        assert meta["delta_dropped_part_keys"]
        # The critique is still delivered (distinct volatile part).
        assert "tie logging to request id" in agent.calls[1]["prompt"]
        # The original task is NOT re-sent — it is already in the resumed
        # session's history.
        assert "Add structured logging" not in agent.calls[1]["prompt"]
