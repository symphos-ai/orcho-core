"""replan-path regression.

Round-1 PLAN injects TEXT attachments via prompt_prefix; round-2+
replan path used to bypass that injection (it builds replan_prompt
directly and sends to agent.run_prompt). This test pins the contract:
attachments must persist across replan rounds so the architect doesn't
lose spec / mockup context on retry.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline.lifecycle import default_lifecycle_context
from pipeline.phases.builtin import _phase_plan
from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    Attachment,
    AttachmentKind,
    PhaseRegistry,
    PipelineState,
)


def _seed_plan_step(state: PipelineState) -> PipelineState:
    """Seed the FSM-provided active step these handler tests bypass.

    ADR 0113 (declarative continuity): the plan handler resolves continuity off
    ``lifecycle_ctx.active_step.execution_policy``. Production seeds it via the
    FSM; these handler-level tests call ``_phase_plan`` directly, so seed plan's
    real declared continuity (``loop_continue``) — without it the resolver
    raises rather than silently defaulting to fresh. These round-2 cases run no
    round 1, so there is no prior session to resume and the render falls back to
    full; the attachment / critique contracts they pin are unaffected.
    """
    ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
    ctx.active_step = SimpleNamespace(
        prompt=None,
        execution_policy=SimpleNamespace(
            session_split=None, session_continuity="loop_continue",
        ),
    )
    state.lifecycle_ctx = ctx
    return state


class _CapturingAgent:
    """Architect double that records every prompt sent to ``invoke``."""
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.model = "test-architect"
        self.session_id: str | None = None

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        del cwd, mutates_artifacts, continue_session, attachments
        self.prompts.append(prompt)
        return "## Task A\n```json\n{}\n```"

    def reset_session(self) -> None:
        self.session_id = None


@dataclass
class _StubPhaseConfig:
    plan_agent: Any = None
    validate_plan_agent: Any = None
    implement_agent: Any = None
    repair_changes_agent: Any = None
    repair_escalation_agent: Any = None
    review_changes_agent: Any = None
    final_acceptance_agent: Any = None


def _state_with_attachments(tmp_path: Path) -> tuple[PipelineState, _CapturingAgent]:
    spec = tmp_path / "spec.md"
    spec.write_text("# Product spec\n\nMust support OAuth2.\n", encoding="utf-8")
    att = Attachment(
        kind=AttachmentKind.TEXT,
        name="spec.md",
        content_path=str(spec),
    )
    agent = _CapturingAgent()
    pc = _StubPhaseConfig(plan_agent=agent)
    state = PipelineState(
        task="implement login",
        project_dir=str(tmp_path),
        plugin=PluginConfig(),
        phase_config=pc,
    )
    state.attachments = (att,)
    _seed_plan_step(state)
    return state, agent


def test_round1_plan_includes_attachments(tmp_path: Path) -> None:
    """Sanity check — the round-1 path was correct already; the test
 locks the contract so the replan path can be compared against it."""
    state, agent = _state_with_attachments(tmp_path)
    state.extras["plan_round"] = 1
    _phase_plan(state)
    assert len(agent.prompts) == 1
    assert "ATTACHMENTS:" in agent.prompts[0]
    assert "Must support OAuth2" in agent.prompts[0]


def test_replan_round_includes_attachments(tmp_path: Path) -> None:
    """regression: round-2 replan must prepend the same
 TEXT attachment block. Without the fix in builtin_phases.py the
 replan prompt never sees the spec."""
    state, agent = _state_with_attachments(tmp_path)
    state.extras["plan_round"] = 2
    state.last_critique = "missing OAuth flow"
    _phase_plan(state)
    assert len(agent.prompts) == 1
    prompt = agent.prompts[0]
    assert "ATTACHMENTS:" in prompt
    assert "spec.md" in prompt
    assert "Must support OAuth2" in prompt
    # Replan critique still flows through too.
    assert "missing OAuth flow" in prompt


def test_replan_without_attachments_unchanged(tmp_path: Path) -> None:
    """No attachments → no prompt prefix. Pre-Phase-4.5 behaviour
 preserved when state.attachments is empty."""
    agent = _CapturingAgent()
    pc = _StubPhaseConfig(plan_agent=agent)
    state = PipelineState(
        task="x",
        project_dir=str(tmp_path),
        plugin=PluginConfig(),
        phase_config=pc,
    )
    _seed_plan_step(state)
    state.extras["plan_round"] = 2
    state.last_critique = "fix tests"
    _phase_plan(state)
    assert "ATTACHMENTS:" not in agent.prompts[0]


def test_replan_operator_only_fires_without_reviewer_critique(
    tmp_path: Path,
) -> None:
    """Operator-driven retry: only ``state.human_feedback`` is set,
    no prior reviewer critique. The replan branch must still fire and
    the prompt must carry the operator text without any "Reviewer
    found these issues" framing."""
    agent = _CapturingAgent()
    pc = _StubPhaseConfig(plan_agent=agent)
    state = PipelineState(
        task="implement login",
        project_dir=str(tmp_path),
        plugin=PluginConfig(),
        phase_config=pc,
    )
    _seed_plan_step(state)
    state.extras["plan_round"] = 2
    state.last_critique = ""
    state.human_feedback = "Stay inside the API; do not touch the SPA."
    _phase_plan(state)
    assert agent.prompts, "replan branch must fire on operator-only retry"
    prompt = agent.prompts[0]
    assert "Stay inside the API" in prompt
    assert "Reviewer found these issues" not in prompt


def test_replan_persists_both_fields_on_success_then_clears(
    tmp_path: Path,
) -> None:
    """Success exit: phase_log['plan'] carries both replan_critique
    and human_feedback; state.human_feedback is cleared afterwards so
    a subsequent retry does not re-inject stale operator text."""
    state, agent = _state_with_attachments(tmp_path)
    state.extras["plan_round"] = 2
    state.last_critique = "missing rollback"
    state.human_feedback = "Scope to migrations only."
    _phase_plan(state)
    log = state.phase_log["plan"]
    assert log["replan_critique"] == "missing rollback"
    assert log["human_feedback"] == "Scope to migrations only."
    assert log["meta"]["human_directed"] is True
    assert state.human_feedback == ""
    # Reviewer critique is reset on validate_plan approval, not here.
    assert state.last_critique == "missing rollback"


def test_replan_dry_run_persists_both_fields_then_clears(
    tmp_path: Path,
) -> None:
    """Dry-run exit must observe the same invariant as success."""
    state, agent = _state_with_attachments(tmp_path)
    state.dry_run = True
    state.extras["plan_round"] = 2
    state.last_critique = "missing rollback"
    state.human_feedback = "Scope to migrations only."
    _phase_plan(state)
    log = state.phase_log["plan"]
    assert log["replan_critique"] == "missing rollback"
    assert log["human_feedback"] == "Scope to migrations only."
    assert log["meta"]["human_directed"] is True
    assert state.human_feedback == ""


class _ParseFailingAgent(_CapturingAgent):
    def invoke(self, *args: Any, **kwargs: Any) -> str:  # type: ignore[override]
        prompt = args[0] if args else kwargs.get("prompt", "")
        self.prompts.append(prompt)
        return "not valid plan json"


def test_replan_parse_error_persists_both_fields_then_clears(
    tmp_path: Path,
) -> None:
    """Parse-error exit must observe the same invariant. Without this,
    a malformed replan output would silently lose the operator feedback
    from run evidence."""
    agent = _ParseFailingAgent()
    pc = _StubPhaseConfig(plan_agent=agent)
    state = PipelineState(
        task="x",
        project_dir=str(tmp_path),
        plugin=PluginConfig(),
        phase_config=pc,
    )
    _seed_plan_step(state)
    state.extras["plan_round"] = 2
    state.last_critique = "missing rollback"
    state.human_feedback = "Scope to migrations only."
    _phase_plan(state)
    log = state.phase_log["plan"]
    assert log["replan_critique"] == "missing rollback"
    assert log["human_feedback"] == "Scope to migrations only."
    assert log["meta"]["human_directed"] is True
    assert "parse_error" in log
    assert state.human_feedback == ""
    assert state.halt is True
