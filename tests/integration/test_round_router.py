"""
tests/integration/test_round_router.py

Integration tests for the round-based model escalation logic.

Rule: round 1 repair_changes → phase_model("implement") (fast)
      round 2+ repair_changes → phase_model("repair_escalation") (more capable)
"""

import json
from unittest.mock import MagicMock, patch

from agents.runtimes._strategy import MockAgentProvider
from core.infra import config
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline

IMPLEMENT_MODEL = config.phase_model("implement", "claude-opus-4-8[1m]")
PLAN_MODEL = config.phase_model("plan", "claude-opus-4-8[1m]")
REPAIR_ESCALATION_MODEL = config.phase_model(
    "repair_escalation", "claude-opus-4-8[1m]",
)


def _claude_factory(output: str = "built"):
    """Returns a fresh ClaudeAgent mock instance each time it's called."""
    inst = MagicMock()
    inst.invoke.return_value = output
    return inst


def _make_provider(critique: str) -> MockAgentProvider:
    """MockAgentProvider with a codex stub that always returns given critique."""
    provider = MockAgentProvider()

    class _FixedCodex:
        model = "mock"
        session_id: str | None = None

        def invoke(
            self, prompt: str, cwd: str, *,
            mutates_artifacts: bool = False,
            continue_session: bool = False,
            attachments: tuple = (),
        ) -> str:
            # ADR 0025: project final_acceptance uses release_json.
            if _prompt_requests_release(prompt):
                return _approved_release_json()
            # Plan validation prompts include the plan body; pass them.
            # Change-review prompts go through critique.
            if "Review the implementation plan document below" in prompt:
                return _approved_review_json("Plan review approved by JSON contract.")
            return critique

        def reset_session(self) -> None:
            self.session_id = None

    provider.codex = lambda model, **_kw: _FixedCodex()  # type: ignore[method-assign]
    return provider


def _approved_review_json(summary: str = "Approved by JSON contract.") -> str:
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": summary,
        "findings":      [],
    })


def _approved_release_json(summary: str = "Ship-ready.") -> str:
    """ADR 0025: release-gate APPROVED payload."""
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      summary,
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "sufficient",
        },
    })


def _prompt_requests_release(prompt: str) -> bool:
    return (
        'kind="contract"' in prompt
        and 'name="release_json"' in prompt
        and "<orcho:system-block " in prompt
    )


def _rejected_review_json(summary: str = "Issue found by JSON contract.") -> str:
    return json.dumps({
        "verdict":       "REJECTED",
        "short_summary": summary,
        "findings":      [{
            "id": "F1",
            "severity": "P2",
            "title": "Review finding",
            "body": summary,
            "required_fix": "Address the review finding.",
        }],
    })


class TestRoundRouter:
    @patch("core.io.git_helpers.has_uncommitted", return_value=True)
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_round1_fix_uses_code_model(
        self, _, __, project_dir: str
    ) -> None:
        """First repair round must use the implement-phase model."""
        provider = _make_provider(_rejected_review_json("one issue found"))
        claude_calls: list[str] = []

        real_claude = provider.claude
        def _track_claude(model: str, *, effort: str | None = None):
            claude_calls.append(model)
            return real_claude(model, effort=effort)
        provider.claude = _track_claude  # type: ignore[method-assign]

        run_pipeline(
            task="Task", project_dir=project_dir,
            max_rounds=1, profile_name="task",
            model=IMPLEMENT_MODEL,
            provider=provider,
        )

        assert any(IMPLEMENT_MODEL in m for m in claude_calls), (
            f"Expected {IMPLEMENT_MODEL} in first FIX round. Got: {claude_calls}"
        )

    @patch("core.io.git_helpers.has_uncommitted", return_value=True)
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_round2_fix_escalates_to_opus(
        self, _, __, project_dir: str
    ) -> None:
        """Second repair round must escalate to the repair_escalation-phase model.

        After the data-driven runtime migration, escalation no longer
        instantiates ``agent_module.ClaudeAgent`` directly; instead the
        orchestrator pre-builds ``phase_config.repair_escalation_agent`` via
        ``provider.claude(repair_escalation_model)``. We assert on the provider
        call tracker, which sees that construction.
        """
        provider = _make_provider(_rejected_review_json("still has issues"))
        claude_calls: list[str] = []

        real_claude = provider.claude
        def _track_claude(model: str, *, effort: str | None = None):
            claude_calls.append(model)
            return real_claude(model, effort=effort)
        provider.claude = _track_claude  # type: ignore[method-assign]

        run_pipeline(
            task="Hard task", project_dir=project_dir,
            max_rounds=2, profile_name="task",
            model=IMPLEMENT_MODEL,
            provider=provider,
        )

        assert any(REPAIR_ESCALATION_MODEL in m for m in claude_calls), (
            f"Expected {REPAIR_ESCALATION_MODEL} in provider.claude calls "
            f"(escalation slot). Got: {claude_calls}"
        )

    @patch("core.io.git_helpers.has_uncommitted", return_value=True)
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_round2_session_records_escalate_model(
        self, _, __, project_dir: str
    ) -> None:
        """Session JSON must record which model was used per FIX round."""
        provider = _make_provider(_rejected_review_json("still has issues"))

        session = run_pipeline(
            task="Task", project_dir=project_dir,
            max_rounds=2, profile_name="task",
            model=IMPLEMENT_MODEL,
            provider=provider,
        )

        rounds = session["phases"]["rounds"]
        assert len(rounds) == 2
        r2 = rounds[1]
        assert "repair_model" in r2
        assert r2["repair_model"] == REPAIR_ESCALATION_MODEL

    @patch("core.io.git_helpers.has_uncommitted", return_value=True)
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_clean_critique_prevents_escalation(
        self, _, __, project_dir: str
    ) -> None:
        """If round 1 critique is clean, round 2 never runs — no escalation."""
        provider = _make_provider(_approved_review_json())

        session = run_pipeline(
            task="Simple task", project_dir=project_dir,
            max_rounds=2, profile_name="task",
            model=IMPLEMENT_MODEL,
            provider=provider,
        )

        rounds = session["phases"]["rounds"]
        assert len(rounds) == 1
        assert "repair_output" not in rounds[0]

    @patch("core.io.git_helpers.has_uncommitted", return_value=True)
    @patch("pipeline.project.session_run.load_plugin", return_value=PluginConfig())
    def test_round1_chain_routes_fix_through_build_agent(
        self, _, __, project_dir: str
    ) -> None:
        """When the caller passes a PhaseAgentConfig with distinct implement_agent
        and repair_changes_agent instances and round 1 resolves to CHAIN, the FIX must
        run on implement_agent so the session_id captured during BUILD's run()
        actually carries over. Otherwise ``session_mode: chain`` is recorded
        but the run is effectively stateless.
        """
        from agents.registry import PhaseAgentConfig

        provider = _make_provider(_rejected_review_json("still has issues"))

        # Two distinct agent instances bound to the same model. Each tracks
        # whether it was used to run a FIX prompt.
        implement_agent = provider.claude(IMPLEMENT_MODEL)
        implement_agent.label = "build-instance"
        repair_changes_agent = provider.claude(IMPLEMENT_MODEL)
        repair_changes_agent.label = "fix-instance"
        assert implement_agent is not repair_changes_agent

        fix_calls_on_build: list[tuple] = []
        fix_calls_on_fix: list[tuple] = []
        original_build_run = implement_agent.run
        original_fix_run = repair_changes_agent.run

        def _track_build(prompt, cwd, **kwargs):
            fix_calls_on_build.append((prompt[:30], kwargs))
            return original_build_run(prompt, cwd, **kwargs)

        def _track_fix(prompt, cwd, **kwargs):
            fix_calls_on_fix.append((prompt[:30], kwargs))
            return original_fix_run(prompt, cwd, **kwargs)

        implement_agent.run = _track_build  # type: ignore[method-assign]
        repair_changes_agent.run = _track_fix  # type: ignore[method-assign]

        codex_agent = provider.codex(config.CODEX_MODEL)
        phase_cfg = PhaseAgentConfig(
            plan_agent=provider.claude(PLAN_MODEL),
            validate_plan_agent=codex_agent,
            implement_agent=implement_agent,
            review_changes_agent=codex_agent,
            repair_changes_agent=repair_changes_agent,
            repair_escalation_agent=provider.claude(REPAIR_ESCALATION_MODEL),
            final_acceptance_agent=codex_agent,
        )

        from agents.protocols import SessionMode
        session = run_pipeline(
            task="Task", project_dir=project_dir,
            max_rounds=1, profile_name="task",
            model=IMPLEMENT_MODEL,
            phase_config=phase_cfg,
            session_mode=SessionMode.CHAIN,
            provider=provider,
        )

        rounds = session["phases"]["rounds"]
        assert len(rounds) == 1, f"Expected 1 round, got {rounds}"
        assert rounds[0]["session_mode"] == "chain"

        # The FIX run() must have landed on implement_agent (which carries the
        # build session_id from its earlier BUILD run), NOT on the distinct
        # repair_changes_agent. If round 1 CHAIN forgot to swap, fix_calls_on_fix would
        # be the one with continue_session=True instead.
        assert fix_calls_on_fix == [], (
            f"Round 1 CHAIN must NOT route through repair_changes_agent — its session "
            f"is empty. repair_changes_agent.run calls: {fix_calls_on_fix}"
        )
        chain_calls_on_build = [
            (p, kw) for p, kw in fix_calls_on_build
            if kw.get("continue_session") is True
        ]
        assert len(chain_calls_on_build) == 1, (
            f"Expected exactly 1 continue_session=True call on implement_agent "
            f"(the round-1 CHAIN FIX). Got: {chain_calls_on_build}"
        )
