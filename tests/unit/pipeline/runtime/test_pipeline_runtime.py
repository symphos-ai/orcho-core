"""
Data-driven pipeline runtime.

Covers PipelineState, PhaseRegistry, the legacy ``PipelineProfile`` shape
(retained as a thin profile dataclass for direct dispatcher tests), and
the runner's halt + callback semantics.

 removed coverage for the deleted legacy ``load_profiles``
JSON loader + ``resolve_profile`` env/plugin priority resolver. v2 profile
resolution lives in ``project_orchestrator._resolve_v2_profile`` and is
covered by ``test_runtime_v2_dispatch.py`` + ``test_profile_loader.py``.
"""

from __future__ import annotations

import pytest

from pipeline.plugins import PluginConfig
from pipeline.runtime import (
    PhaseRegistry,
    PipelineProfile,
    PipelineState,
    run_profile,
)

# ── Registry ─────────────────────────────────────────────────────────────────

class TestPhaseRegistry:
    def test_register_and_get(self) -> None:
        reg = PhaseRegistry()
        def sentinel(s):
            return s
        reg.register("plan", sentinel)
        assert reg.has("plan")
        assert reg.get("plan") is sentinel

    def test_unknown_phase_lists_registered(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        with pytest.raises(KeyError, match=r"Unknown phase 'implement'.*Registered.*plan"):
            reg.get("implement")

    def test_empty_name_rejected(self) -> None:
        reg = PhaseRegistry()
        with pytest.raises(ValueError):
            reg.register("", lambda s: s)
        with pytest.raises(ValueError):
            reg.register("   ", lambda s: s)

    def test_re_registration_overwrites(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        def replacement(s):
            return s
        reg.register("plan", replacement)
        assert reg.get("plan") is replacement

    def test_names_sorted(self) -> None:
        reg = PhaseRegistry()
        reg.register("review_changes", lambda s: s)
        reg.register("plan", lambda s: s)
        reg.register("implement", lambda s: s)
        assert reg.names() == ["implement", "plan", "review_changes"]


# ── Runner ───────────────────────────────────────────────────────────────────

def _state(**kw) -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig(), **kw)


class TestRunProfile:
    def test_walks_phases_in_order(self) -> None:
        seen: list[str] = []
        reg = PhaseRegistry()

        def make(name: str):
            def handler(state: PipelineState) -> PipelineState:
                seen.append(name)
                return state
            return handler

        for name in ("plan", "implement", "review_changes"):
            reg.register(name, make(name))

        run_profile(
            PipelineProfile("p", ("plan", "implement", "review_changes")),
            _state(),
            reg,
        )
        assert seen == ["plan", "implement", "review_changes"]

    def test_handler_returning_none_is_treated_as_in_place_mutation(self) -> None:
        reg = PhaseRegistry()

        def mark(state: PipelineState) -> None:
            state.phase_log["mark"] = "ran"
            return None

        reg.register("mark", mark)
        result = run_profile(PipelineProfile("p", ("mark",)), _state(), reg)
        assert result.phase_log["mark"] == "ran"

    def test_halt_stops_subsequent_phases(self) -> None:
        seen: list[str] = []
        reg = PhaseRegistry()

        def stopper(state: PipelineState) -> PipelineState:
            seen.append("stopper")
            state.stop("manual halt for testing")
            return state

        def follow_up(state: PipelineState) -> PipelineState:
            seen.append("follow_up")
            return state

        reg.register("stopper",   stopper)
        reg.register("follow_up", follow_up)

        result = run_profile(
            PipelineProfile("p", ("stopper", "follow_up")),
            _state(),
            reg,
        )
        assert seen == ["stopper"]
        assert result.halt
        assert result.halt_reason == "manual halt for testing"

    def test_validate_rejects_unknown_phase_in_profile(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan", lambda s: s)
        with pytest.raises(ValueError, match="unknown phases.*implement"):
            run_profile(PipelineProfile("p", ("plan", "implement")), _state(), reg)

    def test_callbacks_fire_around_each_phase(self) -> None:
        reg = PhaseRegistry()
        reg.register("plan",  lambda s: s)
        reg.register("implement", lambda s: s)

        order: list[tuple[str, str]] = []
        run_profile(
            PipelineProfile("p", ("plan", "implement")),
            _state(),
            reg,
            on_phase_start=lambda name, _s: order.append(("start", name)),
            on_phase_end  =lambda name, _s: order.append(("end",   name)),
        )
        assert order == [
            ("start", "plan"), ("end", "plan"),
            ("start", "implement"), ("end", "implement"),
        ]

    def test_callbacks_skipped_after_halt(self) -> None:
        reg = PhaseRegistry()

        def stopper(state: PipelineState) -> PipelineState:
            state.stop("halt")
            return state

        reg.register("stopper", stopper)
        reg.register("never",   lambda s: s)
        starts: list[str] = []
        run_profile(
            PipelineProfile("p", ("stopper", "never")),
            _state(),
            reg,
            on_phase_start=lambda name, _s: starts.append(name),
        )
        assert starts == ["stopper"]


# ════════════════════════════════════════════════════════════════════════════
#  Claude stream-json usage extraction (Variant 1 cost foundation)
# ════════════════════════════════════════════════════════════════════════════

class TestClaudeUsageCapture:
    """``ClaudeAgent.last_cost_usd`` / ``last_tokens_in`` / ``last_tokens_out``
 must hold the values from the final ``{"type":"result",…}`` line of
 Claude Code's stream-json. This is the foundation that powers the
 API-equivalent cost banner ("$0.42 saved by your Pro subscription")
 without faithful per-call capture every aggregate downstream is
 estimated, not measured.
 """

    def test_extract_last_result_picks_final_result_line(self):
        from agents.runtimes.claude import _extract_last_result
        stdout = (
            '{"type":"system","subtype":"init","session_id":"x"}\n'
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
            '{"type":"result","subtype":"success","total_cost_usd":0.42,'
            '"usage":{"input_tokens":1234,"output_tokens":567}}\n'
        )
        d = _extract_last_result(stdout)
        assert d is not None
        assert d["total_cost_usd"] == 0.42
        assert d["usage"]["input_tokens"] == 1234
        assert d["usage"]["output_tokens"] == 567

    def test_extract_last_result_returns_none_when_no_result(self):
        from agents.runtimes.claude import _extract_last_result
        stdout = (
            '{"type":"system","subtype":"init"}\n'
            '{"type":"assistant","message":{"content":[]}}\n'
            'random non-JSON tail\n'
        )
        assert _extract_last_result(stdout) is None
        assert _extract_last_result("") is None

    def test_capture_usage_populates_agent_attrs(self):
        from agents.runtimes.claude import ClaudeAgent, _capture_usage
        agent = ClaudeAgent(model="claude-opus-4-7")
        assert agent.last_cost_usd is None
        _capture_usage(
            agent,
            '{"type":"result","total_cost_usd":1.84,'
            '"usage":{"input_tokens":11200,"output_tokens":3323}}\n',
        )
        assert agent.last_cost_usd == 1.84
        assert agent.last_tokens_in == 11200
        assert agent.last_tokens_out == 3323

    def test_capture_usage_resets_when_no_result_line(self):
        from agents.runtimes.claude import ClaudeAgent, _capture_usage
        agent = ClaudeAgent(model="claude-opus-4-7")
        # Seed prior values — call without a result line must clear them
        # so a stale cost from an earlier phase doesn't leak forward.
        agent.last_cost_usd = 0.99
        agent.last_tokens_in = 100
        agent.last_tokens_out = 200
        _capture_usage(agent, "no-result-here\n")
        assert agent.last_cost_usd is None
        assert agent.last_tokens_in is None
        assert agent.last_tokens_out is None


# ════════════════════════════════════════════════════════════════════════════
#  MetricsCollector cost_usd_equivalent aggregation
# ════════════════════════════════════════════════════════════════════════════

class TestMetricsCostAggregation:
    """``cost_usd_equivalent`` aggregates native provider cost and local
 pricing estimates. Unpriced phases contribute 0 and must not render as
 ``$0.00``, because that's indistinguishable from "the call was free".
 """

    @pytest.fixture(autouse=True)
    def _enable_accounting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core.infra import config
        monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
        config._reset_config()
        yield
        config._reset_config()

    def test_total_cost_sums_reported_phases(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector(default_model="claude-opus-4-7")
        m.record_phase("plan",   model="claude-opus-4-7", duration_s=1.0,
                       tokens_in=100, tokens_out=50, cost_usd=0.42)
        m.record_phase("implement",  model="claude-sonnet-4-6", duration_s=2.0,
                       tokens_in=200, tokens_out=80, cost_usd=0.10)
        m.record_phase("review_changes", model="unpriced-local-model", duration_s=1.5,
                       tokens_in=50,  tokens_out=20)  # no cost available
        assert abs(m.total_cost_usd_equivalent - 0.52) < 1e-9
        line = m.summary_line()
        assert "API-equiv: $0.52" in line, line

    def test_summary_omits_cost_when_none_reported(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector(default_model="unpriced-local-model")
        m.record_phase("plan", model="unpriced-local-model",
                       duration_s=1.0, tokens_in=10, tokens_out=5)
        line = m.summary_line()
        # No phase has native cost or known pricing → must NOT show
        # ``API-equiv: $0.00`` which would mislead users into thinking the
        # call was free.
        assert "API-equiv" not in line, line

    def test_metrics_dict_exposes_cost_when_reported(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector(default_model="claude-opus-4-7")
        m.record_phase("plan", model="claude-opus-4-7",
                       duration_s=1.0, tokens_in=10, tokens_out=5,
                       cost_usd=0.07)
        d = m.as_dict()
        assert d.get("total_cost_usd_equivalent") == 0.07
        assert d["phases"]["plan"]["cost_usd_equivalent"] == 0.07


# ════════════════════════════════════════════════════════════════════════════
#  _infer_workspace_from_project — walk-up sibling-scan
# ════════════════════════════════════════════════════════════════════════════

class TestInferWorkspaceFromProject:
    """``--project`` should determine where runs land. Without auto-derive,
 ``orcho run --project ~/www/atas/bot_1`` writes into the global
 ``$ORCHO_WORKSPACE`` (often a sibling project's workspace), which
 surprises every multi-project user. Walk-up from the project path
 looking for a sibling ``workspace-orchestrator/`` is the fix.
 """

    def test_finds_sibling_workspace(self, tmp_path):
        from pipeline.project.bootstrap import (
            infer_workspace_from_project as _infer_workspace_from_project,
        )
        # Layout: <root>/project_a/ + <root>/workspace-orchestrator/
        root = tmp_path / "atas"
        proj = root / "bot_1"
        ws = root / "workspace-orchestrator"
        proj.mkdir(parents=True)
        ws.mkdir(parents=True)
        assert _infer_workspace_from_project(str(proj)) == ws.resolve()

    def test_finds_workspace_two_levels_up(self, tmp_path):
        from pipeline.project.bootstrap import (
            infer_workspace_from_project as _infer_workspace_from_project,
        )
        # Nested: <root>/sub/proj/ + <root>/workspace-orchestrator/
        root = tmp_path / "monorepo"
        proj = root / "sub" / "lib"
        ws = root / "workspace-orchestrator"
        proj.mkdir(parents=True)
        ws.mkdir(parents=True)
        assert _infer_workspace_from_project(str(proj)) == ws.resolve()

    def test_returns_none_when_no_workspace_in_ancestry(self, tmp_path):
        from pipeline.project.bootstrap import (
            infer_workspace_from_project as _infer_workspace_from_project,
        )
        proj = tmp_path / "lonely_project"
        proj.mkdir()
        assert _infer_workspace_from_project(str(proj)) is None

    def test_returns_none_for_nonexistent_project(self):
        from pipeline.project.bootstrap import (
            infer_workspace_from_project as _infer_workspace_from_project,
        )
        assert _infer_workspace_from_project("/no/such/path/anywhere") is None

    def test_returns_none_for_empty_path(self):
        from pipeline.project.bootstrap import (
            infer_workspace_from_project as _infer_workspace_from_project,
        )
        assert _infer_workspace_from_project(None) is None
        assert _infer_workspace_from_project("") is None


# ════════════════════════════════════════════════════════════════════════════
#  detect_workspace_drift — env-vs-cwd mismatch guard
# ════════════════════════════════════════════════════════════════════════════

class TestDetectWorkspaceDrift:
    """Catches the 'forgot to re-source orcho-env.sh' foot-gun: the operator's
    shell still carries a stale ``ORCHO_WORKSPACE`` (often pointing into
    ``/tmp`` from a disposable demo) but they're cd'ed into a freshly
    bootstrapped workspace with a different prefix. Without the warning the
    run silently lands in the stale workspace.
    """

    def test_reports_drift_when_env_and_cwd_diverge(
        self, tmp_path, monkeypatch,
    ):
        from pipeline.project.bootstrap import detect_workspace_drift
        # Stale env workspace (disposable demo).
        stale_ws = tmp_path / "stale" / "workspace-orchestrator"
        stale_ws.mkdir(parents=True)
        # Fresh workspace next to the project tree.
        fresh_root = tmp_path / "fresh"
        fresh_ws = fresh_root / "workspace-orchestrator"
        fresh_proj = fresh_root / "api"
        fresh_ws.mkdir(parents=True)
        fresh_proj.mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(stale_ws))
        drift = detect_workspace_drift(cwd=str(fresh_proj))
        assert drift is not None
        assert drift.env_workspace == stale_ws.resolve()
        assert drift.cwd_workspace == fresh_ws.resolve()

    def test_returns_none_when_env_matches_cwd(
        self, tmp_path, monkeypatch,
    ):
        from pipeline.project.bootstrap import detect_workspace_drift
        root = tmp_path / "ws_root"
        ws = root / "workspace-orchestrator"
        proj = root / "api"
        ws.mkdir(parents=True)
        proj.mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
        assert detect_workspace_drift(cwd=str(proj)) is None

    def test_returns_none_when_env_unset(self, tmp_path, monkeypatch):
        from pipeline.project.bootstrap import detect_workspace_drift
        root = tmp_path / "ws_root"
        (root / "workspace-orchestrator").mkdir(parents=True)
        proj = root / "api"
        proj.mkdir(parents=True)
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        assert detect_workspace_drift(cwd=str(proj)) is None

    def test_returns_none_when_cwd_has_no_walkup_workspace(
        self, tmp_path, monkeypatch,
    ):
        # Operator running orcho from somewhere unrelated to any
        # workspace tree — can't infer intent, stay silent.
        from pipeline.project.bootstrap import detect_workspace_drift
        ws = tmp_path / "ws_root" / "workspace-orchestrator"
        ws.mkdir(parents=True)
        lonely = tmp_path / "elsewhere"
        lonely.mkdir()
        monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
        assert detect_workspace_drift(cwd=str(lonely)) is None

    def test_handles_symlinked_paths(self, tmp_path, monkeypatch):
        # Both sides must canonicalise via resolve() so a symlinked
        # ORCHO_WORKSPACE pointing at the same underlying dir as the
        # cwd walk-up doesn't show as drift.
        from pipeline.project.bootstrap import detect_workspace_drift
        root = tmp_path / "ws_root"
        ws = root / "workspace-orchestrator"
        proj = root / "api"
        ws.mkdir(parents=True)
        proj.mkdir(parents=True)
        ws_link = tmp_path / "ws_via_symlink"
        ws_link.symlink_to(ws)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(ws_link))
        assert detect_workspace_drift(cwd=str(proj)) is None


# ════════════════════════════════════════════════════════════════════════════
#  Cross-orchestrator workspace inference (smoke-level)
# ════════════════════════════════════════════════════════════════════════════

class TestCrossOrchestratorWorkspaceInference:
    """Cross-orchestrator must auto-derive workspace from the first
 project's location (mirror of project_orchestrator's walk-up). Without
 this, ``orcho cross --projects unity:~/www/qcg/unity api:~/www/qcg/api``
 would write into ``$ORCHO_WORKSPACE`` even when both projects clearly
 belong to qcg.
 """

    def test_first_project_walk_up_finds_shared_workspace(self, tmp_path):
        """Two sibling projects + sibling workspace-orchestrator → first
 project's walk-up returns it."""
        from pipeline.project.bootstrap import (
            infer_workspace_from_project as _infer_workspace_from_project,
        )

        root = tmp_path / "qcg"
        proj_a = root / "unity"
        proj_b = root / "api"
        ws = root / "workspace-orchestrator"
        for d in (proj_a, proj_b, ws):
            d.mkdir(parents=True)

        # Both projects find the same workspace via walk-up.
        a_ws = _infer_workspace_from_project(str(proj_a))
        b_ws = _infer_workspace_from_project(str(proj_b))
        assert a_ws == ws.resolve()
        assert b_ws == ws.resolve()
        # No mismatch — cross-orchestrator's warning branch wouldn't fire.
        assert a_ws == b_ws

    def test_disjoint_workspaces_detectable(self, tmp_path):
        """Projects in different roots → walk-up returns different
 workspaces. Cross-orchestrator's mismatch warning relies on this
 distinguishability."""
        from pipeline.project.bootstrap import (
            infer_workspace_from_project as _infer_workspace_from_project,
        )

        root_a = tmp_path / "qcg"
        proj_a = root_a / "unity"
        ws_a = root_a / "workspace-orchestrator"
        root_b = tmp_path / "atas"
        proj_b = root_b / "bot_1"
        ws_b = root_b / "workspace-orchestrator"
        for d in (proj_a, ws_a, proj_b, ws_b):
            d.mkdir(parents=True)

        a_ws = _infer_workspace_from_project(str(proj_a))
        b_ws = _infer_workspace_from_project(str(proj_b))
        assert a_ws == ws_a.resolve()
        assert b_ws == ws_b.resolve()
        assert a_ws != b_ws  # cross-orchestrator must warn in this case


# ════════════════════════════════════════════════════════════════════════════
#  Claude stream-json text extraction — readable previews
# ════════════════════════════════════════════════════════════════════════════

class TestClaudeAssistantTextExtraction:
    """Without this extractor, ``preview("Plan output", stdout, …)`` would
 print the first 300 chars of ``{"type":"system","subtype":"init",
 "tools":[…]}`` — the init banner — and the actual plan text would
 stay buried in the JSONL tail.
 """

    def test_extracts_single_text_block(self):
        from agents.runtimes.claude import _extract_assistant_text
        stdout = (
            '{"type":"system","subtype":"init","session_id":"x"}\n'
            '{"type":"assistant","message":{"content":[{"type":"text","text":"Hello, world."}]}}\n'
            '{"type":"result","total_cost_usd":0.01}\n'
        )
        assert _extract_assistant_text(stdout) == "Hello, world."

    def test_returns_last_assistant_message(self):
        from agents.runtimes.claude import _extract_assistant_text
        stdout = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"part 1"}]}}\n'
            '{"type":"assistant","message":{"content":[{"type":"text","text":"part 2"}]}}\n'
        )
        assert _extract_assistant_text(stdout) == "part 2"

    def test_joins_text_blocks_within_final_message(self):
        from agents.runtimes.claude import _extract_assistant_text
        stdout = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"progress"}]}}\n'
            '{"type":"assistant","message":{"content":['
                '{"type":"text","text":"part 1"},'
                '{"type":"text","text":"part 2"}'
            ']}}\n'
        )
        assert _extract_assistant_text(stdout) == "part 1\npart 2"

    def test_skips_tool_use_and_system_events(self):
        """Only ``assistant`` events with ``type=text`` content count.
 ``tool_use`` blocks and system/result events must not pollute
 the rendered text."""
        from agents.runtimes.claude import _extract_assistant_text
        stdout = (
            '{"type":"system","subtype":"init"}\n'
            '{"type":"assistant","message":{"content":['
                '{"type":"tool_use","name":"Bash","input":{"command":"ls"}},'
                '{"type":"text","text":"the actual reply"}'
            ']}}\n'
            '{"type":"result","total_cost_usd":0.01}\n'
        )
        assert _extract_assistant_text(stdout) == "the actual reply"

    def test_returns_empty_for_no_text_or_invalid_input(self):
        from agents.runtimes.claude import _extract_assistant_text
        assert _extract_assistant_text("") == ""
        assert _extract_assistant_text("not json at all\nstill not\n") == ""
        # Stream with only init and result, no assistant text:
        stdout = (
            '{"type":"system","subtype":"init"}\n'
            '{"type":"result","total_cost_usd":0.01}\n'
        )
        assert _extract_assistant_text(stdout) == ""


# ════════════════════════════════════════════════════════════════════════════
#  preview() verbose mode
# ════════════════════════════════════════════════════════════════════════════

class TestPreviewVerboseMode:
    """``preview()`` truncates at 700 chars by default. With ``-v``, the
 operator wants the full text — ``set_verbose(True)`` flips a module
 flag that overrides the cap globally."""

    def test_truncates_by_default(self, capsys):
        from core.observability.logging import preview, set_verbose
        set_verbose(False)
        preview("test", "x" * 1000)
        out = capsys.readouterr().out
        assert "…" in out
        assert "x" * 1000 not in out

    def test_full_text_when_verbose(self, capsys):
        from core.observability.logging import preview, set_verbose
        set_verbose(True)
        try:
            preview("test", "x" * 1000)
            out = capsys.readouterr().out
            assert "…" not in out
            assert "x" * 1000 in out
        finally:
            set_verbose(False)  # leave global state clean for other tests

    def test_short_text_never_truncates(self, capsys):
        from core.observability.logging import preview, set_verbose
        set_verbose(False)
        preview("test", "short")
        out = capsys.readouterr().out
        assert "…" not in out
        assert "short" in out


class TestOutputMode:
    def test_apply_summary_mode(self, capsys):
        import agents.stream as stream
        from core.observability import trace
        from core.observability.logging import apply_output_mode, preview

        apply_output_mode("summary")
        assert trace.is_enabled() is False
        assert stream._stdout_echo is False

        preview("test", "x" * 1000)
        out = capsys.readouterr().out
        assert "…" in out

    def test_apply_live_mode_enables_agent_echo_only(self):
        import agents.stream as stream
        from core.observability import trace
        from core.observability.logging import apply_output_mode

        apply_output_mode("live")
        try:
            assert trace.is_enabled() is False
            assert stream._stdout_echo is True
        finally:
            apply_output_mode("summary")

    def test_apply_debug_mode_is_live_plus_trace_and_full_previews(self, capsys):
        import agents.stream as stream
        from core.observability import trace
        from core.observability.logging import apply_output_mode, preview

        apply_output_mode("debug")
        try:
            assert trace.is_enabled() is True
            assert stream._stdout_echo is True
            preview("test", "x" * 1000)
            out = capsys.readouterr().out
            assert "…" not in out
            assert "x" * 1000 in out
        finally:
            apply_output_mode("summary")


# ════════════════════════════════════════════════════════════════════════════
#  Provider symmetry — every CLI fills every role
# ════════════════════════════════════════════════════════════════════════════

class TestProviderRoleSymmetry:
    """Codex was the asymmetry: registered as architect+reviewer but not
 developer because the original CodexAgent had no ``run()``. Now
 ``codex exec`` powers the developer slot — every active provider
 fills all three roles. Gemini stays guarded behind a CLI probe so
 a config typo doesn't get a stub that fails at first method call."""

    def test_codex_implements_runtime_protocol(self):
        from agents.protocols import IAgentRuntime
        from agents.runtimes.codex import CodexAgent

        agent = CodexAgent(model="gpt-5.4")
        assert isinstance(agent, IAgentRuntime), (
            "CodexAgent must satisfy IAgentRuntime — has invoke() + reset_session()"
        )

    def test_codex_invoke_method_exists_and_takes_continue_session(self):
        """``invoke(prompt, cwd, *, mutates_artifacts, continue_session, attachments)``
 is the unified runtime contract. Codex silently ignores
 ``continue_session`` (parity only), but the kwarg must be accepted."""
        import inspect

        from agents.runtimes.codex import CodexAgent

        sig = inspect.signature(CodexAgent.invoke)
        assert "prompt" in sig.parameters
        assert "cwd" in sig.parameters
        assert "continue_session" in sig.parameters
        assert "mutates_artifacts" in sig.parameters

    def test_codex_exec_cmd_includes_model_and_sandbox_flags(
        self, mock_codex_bin: None,
    ):
        from agents.runtimes.codex import CodexAgent

        cmd = CodexAgent(model="gpt-5.4", effort="medium")._exec_cmd(
            mutates_artifacts=True,
        )
        assert any("model=\"gpt-5.4\"" in c for c in cmd), cmd
        assert any("model_reasoning_effort=\"medium\"" in c for c in cmd), cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--json" in cmd
        # Subcommand must be ``exec``, not ``review``.
        assert cmd[1] == "exec", cmd

    def test_codex_exec_wires_runtime_command_guard(
        self, monkeypatch, tmp_path, mock_codex_bin: None,
    ):
        import agents
        from agents.command_guard import ORCHO_GUARDRAIL_BLOCKED
        from agents.runtimes.codex import CodexAgent
        from agents.stream import StreamAbort

        def fake_stream_run(cmd, **kwargs):
            on_line = kwargs.get("on_line")
            assert on_line is not None
            try:
                on_line("tool Bash: git checkout -- test_calc.py\n")
            except StreamAbort as exc:
                return "", 1, f"[ABORTED by stream guard: {exc}]", 0.01
            raise AssertionError("expected StreamAbort from command guard")

        monkeypatch.setattr(agents, "_stream_run", fake_stream_run)

        out = CodexAgent(model="gpt-5.4").invoke(
            "fix it", str(tmp_path), mutates_artifacts=True,
        )

        assert ORCHO_GUARDRAIL_BLOCKED in out
        assert "git checkout -- test_calc.py" in out

    def test_default_registry_has_codex_as_developer(self):
        """Hard-line symmetry assertion: every registered provider is
 in every role. This is the single test that breaks if someone
 re-introduces an asymmetric registration."""
        from agents.registry import AgentRegistry

        r = AgentRegistry.default()
        for provider in ("claude", "codex"):
            assert provider in r._runtimes, f"{provider} missing as architect"
            assert provider in r._runtimes, f"{provider} missing as developer"
            assert provider in r._runtimes,  f"{provider} missing as reviewer"

    def test_gemini_always_registered(self):
        """The PATH-based availability probe was removed once the real
 GeminiAgent landed. Registration is governed by entry-point
 discovery alone — a missing ``gemini`` binary surfaces lazily at
 the first ``invoke()`` via the same ``lazy_cli_binary`` lookup
 the other adapters use, not by hiding the runtime id."""
        from agents.registry import AgentRegistry

        r = AgentRegistry.default()
        assert "gemini" in r._runtimes
        # Resolution succeeds (construction is side-effect-free); the
        # missing-CLI error would surface inside the first ``invoke``.
        agent = r.resolve("gemini-2.5-flash", "gemini")
        assert agent.runtime == "gemini"

    def test_gemini_satisfies_iagentruntime(self):
        """The real GeminiAgent must satisfy the structural Protocol
 every phase adapter depends on."""
        from agents.protocols import IAgentRuntime
        from agents.runtimes.gemini import GeminiAgent

        agent = GeminiAgent(model="gemini-2.5-pro")
        assert isinstance(agent, IAgentRuntime)


# ════════════════════════════════════════════════════════════════════════════
#  Codex usage parser — ``tokens used\nN`` trailer
# ════════════════════════════════════════════════════════════════════════════

class TestCodexUsageParser:
    """Codex CLI prints a ``tokens used\\nN`` trailer at the end of every
 invocation. Without this parser, codex token counts in
 ``metrics.json`` were estimate_tokens(prompt+output) heuristics; now
 they're the CLI's own number, with ``tokens_exact=True``.
 """

    def test_extracts_total_with_thousands_separator(self):
        from agents.runtimes.codex import _extract_codex_tokens
        out = "...some output...\ntokens used\n9,498\n"
        assert _extract_codex_tokens(out) == 9498

    def test_extracts_total_without_separator(self):
        from agents.runtimes.codex import _extract_codex_tokens
        out = "tokens used\n453\n"
        assert _extract_codex_tokens(out) == 453

    def test_returns_none_when_trailer_missing(self):
        from agents.runtimes.codex import _extract_codex_tokens
        assert _extract_codex_tokens("just some review prose") is None
        assert _extract_codex_tokens("") is None

    def test_capture_writes_last_tokens_total(self):
        from agents.runtimes.codex import CodexAgent
        agent = CodexAgent(model="gpt-5.4")
        assert agent.last_tokens_total is None
        agent._capture_tokens("tokens used\n1,234\n", stderr="")
        assert agent.last_tokens_total == 1234

    def test_capture_handles_stderr_only(self):
        """Trailer can land on stderr depending on codex version + how
 the parent shell merged streams. Parser must scan both."""
        from agents.runtimes.codex import CodexAgent
        agent = CodexAgent(model="gpt-5.4")
        agent._capture_tokens(stdout="review body", stderr="tokens used\n42\n")
        assert agent.last_tokens_total == 42


class TestMetricsTokensExactFlag:
    """``PhaseMetrics.tokens_exact`` lets ``orcho cost`` distinguish
 measured from estimated counts without re-deriving the rule.
 """

    def test_explicit_in_out_counts_as_exact(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            tokens_in=100, tokens_out=50, duration_s=1.0)
        assert pm.tokens_exact is True

    def test_tokens_total_only_counts_as_exact(self):
        """Codex path: total provided, in/out absent — still exact."""
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("validate_plan", model="gpt-5.4",
                            tokens_total=1234, duration_s=1.0)
        assert pm.tokens_exact is True
        assert pm.total_tokens == 1234
        assert pm.tokens_in == 0
        assert pm.tokens_out == 0
        assert pm.tokens_unknown == 1234

    def test_no_token_args_falls_back_to_estimate_and_marks_inexact(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            prompt="hello", output="world",
                            duration_s=1.0)
        assert pm.tokens_exact is False

    def test_exact_override_none_leaves_total_only_branch_unchanged(self):
        """``tokens_exact=None`` (default) must not perturb any branch —
        the total-only path still derives ``True`` exactly as before."""
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            tokens_total=1234, duration_s=1.0,
                            tokens_exact=None)
        assert pm.tokens_exact is True
        assert pm.tokens_unknown == 1234

    def test_exact_override_false_overrides_total_only_branch(self):
        """An explicit ``False`` override marks the record inexact even on
        the total-only branch that otherwise derives ``True``."""
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            tokens_total=1234, duration_s=1.0,
                            tokens_exact=False)
        assert pm.tokens_exact is False
        assert pm.tokens_unknown == 1234  # value unchanged, only flag flipped

    def test_exact_override_true_marks_estimate_branch_exact(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            prompt="hello", output="world",
                            duration_s=1.0, tokens_exact=True)
        assert pm.tokens_exact is True


class TestMetricsReconcileTotal:
    """``reconcile_total`` lets a caller mark ``tokens_total`` authoritative
    so a provider total that exceeds the in/out split is preserved. Default
    (``False``) keeps the historical behavior: total ignored when a split
    is present."""

    def test_default_ignores_total_when_split_present(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            tokens_in=100, tokens_out=50, tokens_total=200,
                            duration_s=1.0)
        # historical behavior: provider total dropped, total == in + out
        assert pm.total_tokens == 150
        assert pm.tokens_unknown == 0

    def test_reconcile_total_preserves_provider_total(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            tokens_in=100, tokens_out=50, tokens_total=200,
                            duration_s=1.0, reconcile_total=True)
        assert pm.tokens_in == 100
        assert pm.tokens_out == 50
        assert pm.tokens_unknown == 50  # remainder folded into unknown
        assert pm.total_tokens == 200

    def test_reconcile_total_no_negative_unknown_when_total_below_split(self):
        """Defensive: a provider total below in+out must never yield a
        negative ``tokens_unknown`` — clamp to zero, keep the split."""
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            tokens_in=100, tokens_out=50, tokens_total=120,
                            duration_s=1.0, reconcile_total=True)
        assert pm.tokens_unknown == 0
        assert pm.total_tokens == 150

    def test_reconcile_total_noop_when_total_absent(self):
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            tokens_in=100, tokens_out=50,
                            duration_s=1.0, reconcile_total=True)
        assert pm.tokens_unknown == 0
        assert pm.total_tokens == 150

    def test_reconcile_total_missing_side_not_estimated(self):
        """Partial provider split: one side is ``None`` but a provider total
        is present. The missing side must be taken as ``0`` (NOT estimated
        from ``prompt``/``output``), and the remainder up to the provider
        total goes to ``tokens_unknown`` so the authoritative total survives."""
        from core.observability.metrics import MetricsCollector
        m = MetricsCollector()
        # A long output would estimate to many tokens — prove it is ignored.
        pm = m.record_phase("plan", model="claude-opus-4-7",
                            prompt="p" * 4_000, output="o" * 4_000,
                            tokens_in=100, tokens_out=None, tokens_total=120,
                            duration_s=1.0, reconcile_total=True)
        assert pm.tokens_in == 100
        assert pm.tokens_out == 0  # missing side NOT estimated from output
        assert pm.tokens_unknown == 20
        assert pm.total_tokens == 120


# ════════════════════════════════════════════════════════════════════════════
#  _fsm_metrics ← agent.last_invocation_outcome bridge
# ════════════════════════════════════════════════════════════════════════════

def _make_outcome(**overrides):
    """Build an ``AgentInvocationOutcome`` with all-``None`` defaults so a
    test only specifies the fields it cares about. Mirrors the transient
    shape ``build_invocation_outcome`` would stamp on an agent."""
    from pipeline.observability.invocation_outcome import AgentInvocationOutcome
    base = dict(
        runtime="claude",
        model="claude-opus-4-7",
        tokens_in=None,
        tokens_in_fresh=None,
        tokens_in_cache_read=None,
        tokens_in_cache_create=None,
        tokens_out=None,
        tokens_out_reasoning=None,
        tokens_total=None,
        tool_calls=0,
        cost_usd_equivalent=None,
        tokens_exact=False,
        usage_source="estimate",
        wire_tokens_estimate=None,
        runtime_overhead_tokens=None,
    )
    base.update(overrides)
    return AgentInvocationOutcome(**base)


def _call_fsm_metrics(agent, *, phase="plan", log_entry=None, model="claude-opus-4-7"):
    """Invoke ``_PipelineRun._fsm_metrics`` against a lightweight stand-in
    self, without constructing the full dataclass. Returns the recorded
    ``PhaseMetrics``."""
    from types import SimpleNamespace

    from core.observability.metrics import MetricsCollector
    from pipeline.project.run import _PipelineRun

    metrics = MetricsCollector(default_model=model)
    fake = SimpleNamespace(
        _agent_for_phase=lambda name: agent,
        _model_for_phase=lambda name: model,
        _metrics=metrics,
    )
    st = SimpleNamespace(
        extras={},
        phase_log={phase: (log_entry or {})},
    )
    _PipelineRun._fsm_metrics(fake, phase, st)
    return metrics


class TestFsmMetricsOutcomeBridge:
    """``_fsm_metrics`` prefers ``agent.last_invocation_outcome`` when set,
    falls back to the ``last_*`` attributes otherwise, and never persists
    the outcome object itself into the metrics dict."""

    def test_provider_reported_outcome_records_exact_tokens(self, monkeypatch):
        from types import SimpleNamespace

        from core.infra import config
        # Enable accounting so the cost path is exercised and the outcome's
        # cost is provably threaded into the recorded metric (not gated off).
        monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
        config._reset_config()
        try:
            agent = SimpleNamespace(
                last_invocation_outcome=_make_outcome(
                    tokens_in=10_000, tokens_out=620, tokens_total=10_620,
                    cost_usd_equivalent=0.012, tokens_exact=True,
                    usage_source="runtime_reported",
                ),
            )
            metrics = _call_fsm_metrics(
                agent,
                log_entry={"output": "out",
                           "context_growth": {"tool_use_count": 4}},
            )
            pm = metrics.phases[-1]
            assert pm.tokens_in == 10_000
            assert pm.tokens_out == 620
            assert pm.tokens_exact is True
            assert pm.cost_usd_equivalent == 0.012  # threaded from outcome
            assert pm.tool_calls == 4  # still from context_growth, not outcome
        finally:
            config._reset_config()

    def test_provider_reported_tokens_without_cost_get_estimated_cost(
        self,
        monkeypatch,
    ):
        from types import SimpleNamespace

        from core.infra import config
        from core.observability import pricing

        monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
        seen: dict[str, int | str] = {}

        def fake_estimate(model, *, tokens_in, tokens_out, cached_tokens_in=0):
            seen.update({
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cached_tokens_in": cached_tokens_in,
            })
            return 0.123

        monkeypatch.setattr(pricing, "estimate_cost_usd", fake_estimate)
        config._reset_config()
        try:
            agent = SimpleNamespace(
                last_invocation_outcome=_make_outcome(
                    model="gpt-5.5",
                    tokens_in=10_000,
                    tokens_out=620,
                    tokens_in_cache_read=9_000,
                    tokens_total=10_620,
                    cost_usd_equivalent=None,
                    tokens_exact=True,
                    usage_source="runtime_reported",
                ),
            )
            metrics = _call_fsm_metrics(
                agent,
                phase="review_changes",
                log_entry={"output": "out"},
            )
            pm = metrics.phases[-1]
            assert pm.cost_usd_equivalent == pytest.approx(0.123)
            assert pm.cost_estimated is True
            assert pm.tokens_in_cache_read == 9_000
            assert seen == {
                "model": "claude-opus-4-7",
                "tokens_in": 10_000,
                "tokens_out": 620,
                "cached_tokens_in": 9_000,
            }
            assert "API-equiv: ~$0.12" in metrics.summary_line()
        finally:
            config._reset_config()

    def test_codex_cached_input_uses_discounted_pricing(
        self,
        tmp_path,
        monkeypatch,
    ):
        from types import SimpleNamespace

        from core.infra import config

        pricing_file = tmp_path / "pricing.local.toml"
        pricing_file.write_text(
            '[models."gpt-5.5"]\n'
            'input_per_1m_usd = 5.00\n'
            'cached_input_per_1m_usd = 0.50\n'
            'output_per_1m_usd = 30.00\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
        monkeypatch.setenv("ORCHO_PRICING_FILE", str(pricing_file))
        config._reset_config()
        try:
            agent = SimpleNamespace(
                last_invocation_outcome=_make_outcome(
                    runtime="codex",
                    model="gpt-5.5",
                    tokens_in=1_695_643,
                    tokens_in_cache_read=1_536_384,
                    tokens_out=4_198,
                    tokens_total=1_699_841,
                    cost_usd_equivalent=None,
                    tokens_exact=True,
                    usage_source="runtime_reported",
                ),
            )
            metrics = _call_fsm_metrics(
                agent,
                phase="final_acceptance",
                model="gpt-5.5",
                log_entry={"output": "ok"},
            )
            pm = metrics.phases[-1]
            assert pm.tokens_in_cache_read == 1_536_384
            assert pm.cost_usd_equivalent == pytest.approx(1.690427)
            assert pm.cost_estimated is True
        finally:
            config._reset_config()

    def test_provider_total_preserved_when_exceeds_split(self):
        """Codex/Gemini report a normalized total that can exceed the in/out
        split (e.g. reasoning tokens). The recorded ``total_tokens`` must
        reflect ``outcome.tokens_total``, not a bare ``in + out`` sum."""
        from types import SimpleNamespace
        agent = SimpleNamespace(
            last_invocation_outcome=_make_outcome(
                tokens_in=100, tokens_out=50, tokens_total=200,
                tokens_out_reasoning=50, tokens_exact=True,
                usage_source="runtime_reported",
            ),
        )
        metrics = _call_fsm_metrics(
            agent,
            log_entry={"output": "out",
                       "context_growth": {"tool_use_count": 1}},
        )
        pm = metrics.phases[-1]
        assert pm.tokens_in == 100
        assert pm.tokens_out == 50
        # remainder beyond the split is folded into unknown so the provider
        # total survives — total_tokens == outcome.tokens_total (200), NOT 150.
        assert pm.tokens_unknown == 50
        assert pm.total_tokens == 200
        assert pm.tokens_exact is True

    def test_composite_phase_usage_beats_output_estimation(self):
        """Composite handlers such as subtask_dag report N provider calls via
        phase_log. Metrics must trust that rollup instead of estimating the
        integrated markdown output as one huge output-only call."""
        from types import SimpleNamespace

        agent = SimpleNamespace(
            last_invocation_outcome=None,
            last_prompt="",
        )
        metrics = _call_fsm_metrics(
            agent,
            phase="implement",
            log_entry={
                "output": "x" * 20_000,
                "_metrics_usage": {
                    "source": "subtask_dag",
                    "invocations": 2,
                    "tokens_in": 1_000,
                    "tokens_out": 200,
                    "tokens_total": 1_250,
                    "tool_calls": 3,
                    "tokens_exact": True,
                },
            },
        )
        pm = metrics.phases[-1]
        assert pm.tokens_in == 1_000
        assert pm.tokens_out == 200
        assert pm.tokens_unknown == 50
        assert pm.total_tokens == 1_250
        assert pm.tool_calls == 3
        assert pm.tokens_exact is True

    def test_partial_provider_outcome_preserves_total_without_estimate(self):
        """A partial split (one side missing) with an authoritative provider
        total must keep the known side verbatim, NOT estimate the missing one
        from the phase output, and fold the remainder into ``tokens_unknown``
        so ``total_tokens == outcome.tokens_total``."""
        from types import SimpleNamespace
        agent = SimpleNamespace(
            last_invocation_outcome=_make_outcome(
                tokens_in=100, tokens_out=None, tokens_total=120,
                tokens_exact=True, usage_source="runtime_partial",
            ),
        )
        # A long output would otherwise estimate to many tokens; the bridge
        # must ignore it and trust the provider total.
        metrics = _call_fsm_metrics(
            agent,
            log_entry={"output": "x" * 4_000,
                       "context_growth": {"tool_use_count": 0}},
        )
        pm = metrics.phases[-1]
        assert pm.tokens_in == 100
        assert pm.tokens_out == 0  # missing side NOT estimated
        assert pm.tokens_unknown == 20
        assert pm.total_tokens == 120
        assert pm.tokens_exact is True

    def test_no_outcome_falls_back_to_last_attributes(self):
        from types import SimpleNamespace
        agent = SimpleNamespace(
            last_tokens_in=200,
            last_tokens_out=80,
            last_tokens_total=None,
            last_cost_usd=0.05,
            last_duration_s=1.5,
            last_prompt="p",
            # deliberately no last_invocation_outcome
        )
        metrics = _call_fsm_metrics(
            agent,
            log_entry={"output": "out",
                       "context_growth": {"tool_use_count": 2}},
        )
        pm = metrics.phases[-1]
        assert pm.tokens_in == 200
        assert pm.tokens_out == 80
        assert pm.tokens_exact is True  # in/out present → exact, as before
        assert pm.tool_calls == 2

    def test_estimate_only_outcome_is_not_marked_exact(self):
        from types import SimpleNamespace
        agent = SimpleNamespace(
            last_invocation_outcome=_make_outcome(
                tokens_in=None, tokens_out=None, tokens_total=None,
                tokens_exact=False, usage_source="estimate",
                wire_tokens_estimate=777,
            ),
        )
        metrics = _call_fsm_metrics(
            agent,
            log_entry={"output": "out",
                       "context_growth": {"tool_use_count": 0}},
        )
        pm = metrics.phases[-1]
        assert pm.tokens_exact is False
        # wire estimate fed as total to avoid the misleading in=0/out=N tail
        assert pm.total_tokens == 777
        assert pm.tokens_in == 0
        assert pm.tokens_out == 0

    def test_outcome_object_not_persisted_in_metrics_dict(self):
        import json
        from types import SimpleNamespace
        agent = SimpleNamespace(
            last_invocation_outcome=_make_outcome(
                tokens_in=100, tokens_out=50, tokens_total=150,
                tokens_exact=True, usage_source="runtime_reported",
            ),
        )
        metrics = _call_fsm_metrics(
            agent,
            log_entry={"output": "out",
                       "context_growth": {"tool_use_count": 1}},
        )
        d = metrics.as_dict()
        serialized = json.dumps(d)
        assert "invocation_outcome" not in serialized
        # schema fields stay exactly as before
        plan = d["phases"]["plan"]
        assert set(["tokens_in", "tokens_out", "total_tokens",
                    "tokens_exact", "tool_calls"]).issubset(plan.keys())


# ════════════════════════════════════════════════════════════════════════════
#  OpenAI pricing snapshot + user override
# ════════════════════════════════════════════════════════════════════════════

class TestPricingLoader:
    """``load_pricing()`` merges the bundled snapshot (intentionally empty)
 with user overrides at ``~/.orcho/pricing.local.toml``. User wins.
 The bundled snapshot ships empty by design — no speculative rates.
 """

    def test_bundled_snapshot_is_empty_by_default(self):
        """orcho commits no hardcoded prices. The empty snapshot is the
 contract — if someone slips numbers in, this test catches it."""
        from core.observability.pricing import _load_snapshot
        snapshot = _load_snapshot()
        assert snapshot.get("models") == {}, (
            "Bundled snapshot must stay empty — "
            "no shipped speculation about OpenAI rates."
        )

    def test_user_override_wins_over_snapshot(self, tmp_path, monkeypatch):
        """Loader merges: user TOML on top of snapshot. When both have a
 model, user's rate wins."""
        from core.observability import pricing as p

        user_file = tmp_path / "pricing.local.toml"
        user_file.write_text(
            '[meta]\nfetched_at = "2026-05-06T12:00:00+00:00"\n'
            'source = "https://test"\n'
            '\n[models."gpt-5.4"]\n'
            'input_per_1m_usd = 1.50\n'
            'output_per_1m_usd = 6.00\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ORCHO_PRICING_FILE", str(user_file))

        table = p.load_pricing()
        assert "gpt-5.4" in table
        entry = table["gpt-5.4"]
        assert entry.input_per_1m_usd == 1.50
        assert entry.output_per_1m_usd == 6.00
        assert entry.cached_input_per_1m_usd is None
        assert entry.source == "user"

    def test_unknown_model_returns_none_from_estimator(self, tmp_path, monkeypatch):
        from core.observability import pricing as p
        monkeypatch.setenv("ORCHO_PRICING_FILE", str(tmp_path / "absent.toml"))
        assert p.estimate_cost_usd("gpt-not-a-model",
                                   tokens_in=1000, tokens_out=500) is None
        assert p.estimate_cost_from_total("gpt-not-a-model", 1000) is None

    def test_estimate_cost_from_split(self, tmp_path, monkeypatch):
        """Exact in/out split — direct multiplication."""
        from core.observability import pricing as p
        user_file = tmp_path / "pricing.local.toml"
        user_file.write_text(
            '[meta]\nfetched_at = "2026-05-06T12:00:00+00:00"\n'
            '[models."test-model"]\n'
            'input_per_1m_usd = 2.00\n'
            'output_per_1m_usd = 8.00\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ORCHO_PRICING_FILE", str(user_file))
        # 1M in × $2 + 1M out × $8 = $10
        assert p.estimate_cost_usd(
            "test-model", tokens_in=1_000_000, tokens_out=1_000_000,
        ) == 10.0

    def test_estimate_cost_uses_cached_input_rate(self, tmp_path, monkeypatch):
        """Codex/OpenAI usage reports cached_input_tokens as an input subset;
        API-equivalent estimates must not bill that subset at full price."""
        from core.observability import pricing as p

        user_file = tmp_path / "pricing.local.toml"
        user_file.write_text(
            '[models."gpt-5.5"]\n'
            'input_per_1m_usd = 5.00\n'
            'cached_input_per_1m_usd = 0.50\n'
            'output_per_1m_usd = 30.00\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ORCHO_PRICING_FILE", str(user_file))

        assert p.estimate_cost_usd(
            "gpt-5.5",
            tokens_in=1_695_643,
            cached_tokens_in=1_536_384,
            tokens_out=4_198,
        ) == pytest.approx(1.690427)

    def test_estimate_cost_falls_back_to_ten_percent_cached_rate(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Older pricing.local.toml files predate cached-input rates. Keep
        their estimates cache-aware instead of charging cached input at full
        rate."""
        from core.observability import pricing as p

        user_file = tmp_path / "pricing.local.toml"
        user_file.write_text(
            '[models."gpt-5.5"]\n'
            'input_per_1m_usd = 5.00\n'
            'output_per_1m_usd = 30.00\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ORCHO_PRICING_FILE", str(user_file))

        assert p.estimate_cost_usd(
            "gpt-5.5",
            tokens_in=1_695_643,
            cached_tokens_in=1_536_384,
            tokens_out=4_198,
        ) == pytest.approx(1.690427)

    def test_estimate_from_total_assumes_50_50(self, tmp_path, monkeypatch):
        """Codex case: total only. Documented as 50/50 split, so 1M
 total at $2 input + $8 output = 500k×$2 + 500k×$8 = $5."""
        from core.observability import pricing as p
        user_file = tmp_path / "pricing.local.toml"
        user_file.write_text(
            '[models."test-model"]\n'
            'input_per_1m_usd = 2.00\n'
            'output_per_1m_usd = 8.00\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("ORCHO_PRICING_FILE", str(user_file))
        assert p.estimate_cost_from_total("test-model", 1_000_000) == 5.0


class TestPricingScrape:
    """The HTML scraper is best-effort: we walk the ``__NEXT_DATA__``
 blob and pluck nodes that look like model→rate rows. Crucial that
 a structural change → ``_PricingScrapeError``, never a half-baked
 write. These tests pin the failure modes."""

    def test_scrape_succeeds_on_synthetic_next_data(self):
        from core.observability.pricing_scrapers import (
            scrape_openai_pricing as _scrape_openai_pricing,
        )
        # Synthetic ``__NEXT_DATA__`` payload that mimics a typical
        # docs-site shape: nested dict with model entries.
        html = (
            '<html><body>'
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"pricing":['
            '{"name":"gpt-5.4","input":2.5,"output":10.0},'
            '{"name":"gpt-5.5","input":5.0,"output":20.0}'
            ']}}}</script></body></html>'
        )
        models = _scrape_openai_pricing(html)
        assert "gpt-5.4" in models
        assert models["gpt-5.4"]["input_per_1m_usd"] == 2.5
        assert models["gpt-5.4"]["output_per_1m_usd"] == 10.0
        assert "gpt-5.5" in models

    def test_scrape_fails_when_no_next_data(self):
        import pytest

        from core.observability.pricing_scrapers import (
            PricingScrapeError as _PricingScrapeError,
            scrape_openai_pricing as _scrape_openai_pricing,
        )
        html = "<html><body><h1>no script here</h1></body></html>"
        with pytest.raises(_PricingScrapeError, match="__NEXT_DATA__"):
            _scrape_openai_pricing(html)

    def test_scrape_fails_when_next_data_has_no_models(self):
        import pytest

        from core.observability.pricing_scrapers import (
            PricingScrapeError as _PricingScrapeError,
            scrape_openai_pricing as _scrape_openai_pricing,
        )
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"unrelated":"data"}}</script>'
        )
        with pytest.raises(_PricingScrapeError, match=r"model.*rate map"):
            _scrape_openai_pricing(html)

    def test_coerce_dollar_sign_strings(self):
        """Real OpenAI page sometimes has prices as ``"$2.50"`` strings,
 not raw floats — scraper must handle both."""
        from core.observability.pricing_scrapers import (
            scrape_openai_pricing as _scrape_openai_pricing,
        )
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"data":[{"model":"foo","input":"$2.50","output":"$10.00"}]}'
            '</script>'
        )
        models = _scrape_openai_pricing(html)
        assert models["foo"]["input_per_1m_usd"] == 2.50
        assert models["foo"]["output_per_1m_usd"] == 10.00


class TestMockProviderSymmetry:
    """``MockAgentProvider`` exposes claude / codex / gemini with the
 same ``(model, *, effort=None)`` signature the real provider uses.
 Without these tests, a regression that drops one of the methods —
 or stops accepting ``effort`` — would only surface when an
 integration test happens to exercise that path. Pin the contract
 here so it fails at unit-test speed.
 """

    def test_all_three_methods_accept_effort_kwarg(self):
        """Every role-method must accept ``effort=`` without TypeError —
 that's the AgentProvider Protocol contract."""
        from agents.runtimes._strategy import MockAgentProvider

        p = MockAgentProvider(latency=0.0)
        # No assertion errors / TypeError — just exercising the call shape.
        p.claude("any-model", effort="medium")
        p.claude("any-model", effort=None)
        p.claude("any-model")              # default effort
        p.codex("any-model", effort="low")
        p.codex("any-model")
        p.gemini("any-model", effort="high")
        p.gemini("any-model")

    def test_gemini_returns_developer_agent_compatible_stub(self):
        """``provider.gemini`` returning None / non-callable would silently
 break a phase that routes through gemini. The mock reuses the
 claude stub (it satisfies all three protocols)."""
        from agents.protocols import IAgentRuntime
        from agents.runtimes._strategy import MockAgentProvider

        agent = MockAgentProvider(latency=0.0).gemini("gemini-pro", effort="medium")
        assert isinstance(agent, IAgentRuntime), (
            "gemini stub must satisfy IAgentRuntime — has invoke() + reset_session()"
        )

    def test_codex_singleton_per_provider_instance(self):
        """Mock has a singleton-codex contract: every ``provider.codex(...)``
 on the same provider instance returns the same stub. The
 validate_plan reject counter relies on this so the count survives
 across validate_plan rounds (effort kwarg must NOT break the
 singleton identity check)."""
        from agents.runtimes._strategy import MockAgentProvider

        p = MockAgentProvider(latency=0.0, validate_plan_reject_rounds=2)
        first = p.codex("gpt-5.4")
        second = p.codex("gpt-5.4", effort="low")
        third = p.codex("anything-else", effort="high")
        assert first is second is third, (
            "codex stub must be a singleton per MockAgentProvider — "
            "the reject-counter state lives on the singleton"
        )

    def test_mock_developer_records_prompt_and_materializes_build_file(self, tmp_path):
        """Mock BUILD should be fast, but not physically impossible.

 It records synthetic prompt/duration telemetry and creates the
 missing file it claims to modify so full mock smoke runs exercise
 the dirty-tree review path.
 """
        from agents.runtimes._strategy import MockAgentProvider

        project = tmp_path / "proj"
        project.mkdir()
        agent = MockAgentProvider(latency=0.0).claude("mock")

        output = agent.run(
            "TASK: add smoke file\n\nImplement the task end-to-end. Work this way:",
            str(project),
        )

        assert "src/proj/implementation.txt" in output
        assert (project / "src" / "proj" / "implementation.txt").exists()
        assert agent.last_prompt.startswith("TASK: add smoke file")
        assert agent.last_duration_s > 0
        assert agent.last_estimated_tokens_in > 0
        assert agent.last_estimated_tokens_out > 0

    def test_mock_build_task_with_fix_keyword_still_materializes_file(self, tmp_path):
        """A BUILD task often says "Fix bug..."; that must not be
 misclassified as a FIX prompt before the mock dirties the tree.
 """
        from agents.runtimes._strategy import MockAgentProvider

        project = tmp_path / "proj"
        project.mkdir()
        agent = MockAgentProvider(latency=0.0).claude("mock")

        output = agent.run(
            "TASK: Fix bug in calc.add\n\nImplement the task end-to-end. Work this way:",
            str(project),
        )

        assert output.startswith("## Build output")
        assert "Applied review feedback" not in output
        assert (project / "src" / "proj" / "implementation.txt").exists()

    def test_mock_subtask_prompt_materializes_build_file(self, tmp_path):
        """subtask_dag prompts are build prompts in mock mode too."""
        from agents.runtimes._strategy import MockAgentProvider

        project = tmp_path / "proj"
        project.mkdir()
        agent = MockAgentProvider(latency=0.0).claude("mock")

        output = agent.run(
            "## Current Executable Subtask `apply-fix`\n\n"
            "**Goal:** Implement the requested change.\n\n"
            "**Done criteria (the work is not finished until each is true):**\n"
            "- Bug is fixed\n",
            str(project),
        )

        assert output.startswith("## Build output")
        assert (project / "src" / "proj" / "implementation.txt").exists()

    def test_mock_developer_touches_existing_claimed_file(self, tmp_path):
        """Golden demos need a tracked diff, not only an untracked marker."""
        from agents.runtimes._strategy import MockAgentProvider

        project = tmp_path / "proj"
        target = project / "src" / "proj" / "implementation.txt"
        target.parent.mkdir(parents=True)
        target.write_text("before = True\n", encoding="utf-8")
        agent = MockAgentProvider(latency=0.0).claude("mock")

        agent.run(
            "TASK: add smoke file\n\n"
            "Implement the task end-to-end. Work this way:\n\n"
            "## Plan Contract",
            str(project),
        )

        assert "before = True" in target.read_text(encoding="utf-8")
        assert "orcho mock implementation touched" in target.read_text(encoding="utf-8")
        assert not (project / ".orcho" / "mock_changes" / "last_build.md").exists()

    def test_mock_plan_prompt_returns_plan_not_build_or_fix(self, tmp_path):
        """PLAN mock should look like a plan even when the task says "Fix"."""
        from agents.runtimes._strategy import MockAgentProvider
        from pipeline.plan_parser import parse_plan

        project = tmp_path / "proj"
        project.mkdir()
        (project / "calc.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
        agent = MockAgentProvider(latency=0.0).claude("mock")

        output = agent.run(
            # The mock detector keys on the PLAN task header, the
            # opening directive (ADR 0028 / M10.5 Step 2 wording:
            # "implementation plan for the task"), and the code-owned
            # plan artifact boundary block.
            'TASK TO PLAN: Fix bug in calc.add\n\n'
            'Produce an implementation plan for the task before any code lands.\n\n'
            '<orcho:system-block kind="contract" '
            'name="plan_artifact_boundary" version="1">',
            str(project),
        )

        assert output.lstrip().startswith("{")
        assert '"short_summary"' in output
        assert '"planning_context"' in output
        assert "## Build output" not in output
        assert "Applied review feedback" not in output
        assert "calc.py" in output
        parsed = parse_plan(output)
        assert [s.id for s in parsed.subtasks] == [
            "inspect-target", "apply-fix", "verify",
        ]

    def test_mock_replan_prompt_returns_revised_plan(self, tmp_path):
        from agents.runtimes._strategy import MockAgentProvider

        project = tmp_path / "proj"
        project.mkdir()
        agent = MockAgentProvider(latency=0.0).claude("mock")

        output = agent.run(
            # ``_is_replan_prompt`` anchors on the retry-frame opening
            # and the human-feedback authority directive emitted by
            # ``tasks/replan.md``.
            "TASK: Fix bug in calc.add\n\n"
            "You are revising the plan for another attempt.\n\n"
            "Apply human feedback as authoritative operator guidance.\n\n"
            "Reviewer findings:\nMissing edge-case coverage.\n",
            str(project),
        )

        assert output.lstrip().startswith("{")
        assert '"Revised mock plan' in output
        assert "## Build output" not in output
        assert "Applied review feedback" not in output

    def test_mock_architect_plan_protocol_uses_plan_content(self, tmp_path):
        """REA-1 finalizer: mock ``.plan()`` returns a ``ParsedPlan`` whose
 ``short_summary`` / ``planning_context`` come from the architect contract and whose
 ``subtasks`` carry the planned files. The legacy
 ``ImplementationPlan.tasks`` shape is gone alongside
 ``parse_markdown``."""
        from agents.runtimes._strategy import MockAgentProvider

        project = tmp_path / "proj"
        project.mkdir()
        (project / "calc.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
        agent = MockAgentProvider(latency=0.0).claude("mock")

        plan = agent.plan("Fix bug in calc.add", str(project))

        assert plan.short_summary.startswith("Mock plan for")
        assert plan.subtasks, "mock plan emitted no subtasks"
        assert any("calc.py" in s.files for s in plan.subtasks)
        assert "calc.py" in plan.file_paths

    def test_mock_hypothesis_reads_project_hints_not_engine_cwd(self, tmp_path):
        from agents.runtimes._strategy import MockAgentProvider

        project = tmp_path / "proj"
        project.mkdir()
        (project / "calc.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
        agent = MockAgentProvider(latency=0.0).claude("mock")

        output = agent.hypothesize("Fix bug in calc.add", str(project))

        assert "calc.py" in output
        assert "dashboard/" not in output
        assert "pipeline/" not in output

    def test_mock_review_records_prompt_and_duration(self, tmp_path):
        """Mock reviewer telemetry gives MetricsCollector non-zero input
 tokens without pretending those estimates are provider-measured."""
        from agents.runtimes._strategy import MockAgentProvider

        project = tmp_path / "proj"
        project.mkdir()
        agent = MockAgentProvider(latency=0.0).codex("mock")

        output = agent.review_uncommitted(str(project), focus="check build diff")

        assert output
        assert "check build diff" in agent.last_prompt
        assert agent.last_duration_s > 0
        assert agent.last_estimated_tokens_in > 0
        assert agent.last_estimated_tokens_out > 0


class TestPricingWriteUser:
    """``write_user_pricing`` produces a TOML file that ``load_pricing``
 can read back. Round-trip must preserve every model + rate."""

    def test_round_trip(self, tmp_path, monkeypatch):
        from core.observability import pricing as p
        target = tmp_path / "pricing.local.toml"
        monkeypatch.setenv("ORCHO_PRICING_FILE", str(target))

        p.write_user_pricing(
            {
                "gpt-5.4": {
                    "input_per_1m_usd": 2.5,
                    "cached_input_per_1m_usd": 0.25,
                    "output_per_1m_usd": 10.0,
                },
                "o4-mini": {"input_per_1m_usd": 0.25, "output_per_1m_usd": 1.0},
            },
            source_url="https://example/pricing",
        )
        table = p.load_pricing()
        assert table["gpt-5.4"].input_per_1m_usd == 2.5
        assert table["gpt-5.4"].cached_input_per_1m_usd == 0.25
        assert table["gpt-5.4"].output_per_1m_usd == 10.0
        assert table["o4-mini"].source == "user"
