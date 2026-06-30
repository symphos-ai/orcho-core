"""
SessionMode auto-resolution + ClaudeAgent
session capture / --resume integration.

Three layers:

1. ``_resolve_session_mode`` — pure function, no IO. Verifies the matrix from
 plan §9: AUTO + same model → CHAIN; AUTO + different models → HYBRID;
 round 0 → STATELESS; explicit modes pass through.

2. ``_extract_session_id`` — parses the session id out of stream-json output.
 Tolerates JSON-per-line, ANSI noise, and missing ids.

3. ``ClaudeAgent.run(continue_session=...)`` — adds ``--resume <session_id>``
 to the CLI command on the second call, but only after a session id has
 been captured.

The actual orchestrator wiring (CHAIN → continue_session=True in FIX) is
covered by tests/integration/test_pipeline.py via stub agents — we don't
duplicate that here. Instead this file targets the small, exact units.
"""

from unittest.mock import MagicMock

import pytest

import agents as agents_module
from agents.protocols import SessionMode
from agents.runtimes.claude import _extract_session_id
from pipeline.project.runtime_setup import _resolve_session_mode


# ────────────────────────────────────────────────────────────────────────────
#  _resolve_session_mode — auto-selection matrix
# ────────────────────────────────────────────────────────────────────────────
class TestResolveSessionMode:
    def test_explicit_stateless_passes_through(self) -> None:
        out = _resolve_session_mode(
            SessionMode.STATELESS,
            repair_round=1, implement_model="x", repair_model="x",
            chain_same_model_only=True,
        )
        assert out is SessionMode.STATELESS

    def test_explicit_chain_passes_through(self) -> None:
        out = _resolve_session_mode(
            SessionMode.CHAIN,
            repair_round=1, implement_model="a", repair_model="b",
            chain_same_model_only=True,
        )
        assert out is SessionMode.CHAIN

    def test_explicit_hybrid_passes_through(self) -> None:
        out = _resolve_session_mode(
            SessionMode.HYBRID,
            repair_round=1, implement_model="x", repair_model="x",
            chain_same_model_only=True,
        )
        assert out is SessionMode.HYBRID

    def test_auto_round_zero_is_stateless(self) -> None:
        """Round 0 = no FIX yet — chaining doesn't apply."""
        out = _resolve_session_mode(
            SessionMode.AUTO,
            repair_round=0, implement_model="x", repair_model="x",
            chain_same_model_only=True,
        )
        assert out is SessionMode.STATELESS

    def test_auto_same_model_chains(self) -> None:
        """AUTO + matching models → CHAIN (the fast path)."""
        out = _resolve_session_mode(
            SessionMode.AUTO,
            repair_round=1,
            implement_model="claude-sonnet-4-6",
            repair_model="claude-sonnet-4-6",
            chain_same_model_only=True,
        )
        assert out is SessionMode.CHAIN

    def test_auto_different_models_hybrid(self) -> None:
        """AUTO + different models → HYBRID (re-prime via codemap)."""
        out = _resolve_session_mode(
            SessionMode.AUTO,
            repair_round=1,
            implement_model="claude-sonnet-4-6",
            repair_model="claude-opus-4-7",
            chain_same_model_only=True,
        )
        assert out is SessionMode.HYBRID

    def test_chain_across_models_when_disabled_safety_off(self) -> None:
        """If the operator turns off the same-model guard, AUTO will CHAIN
 even across different models. Documents the intentional escape hatch."""
        out = _resolve_session_mode(
            SessionMode.AUTO,
            repair_round=1,
            implement_model="claude-sonnet-4-6",
            repair_model="claude-opus-4-7",
            chain_same_model_only=False,
        )
        assert out is SessionMode.CHAIN


# ────────────────────────────────────────────────────────────────────────────
#  _extract_session_id — stream-json parsing
# ────────────────────────────────────────────────────────────────────────────
class TestExtractSessionId:
    def test_returns_none_on_empty(self) -> None:
        assert _extract_session_id("") is None

    def test_returns_none_when_no_session(self) -> None:
        assert _extract_session_id('{"type":"text","content":"hello"}') is None

    def test_extracts_from_json_event(self) -> None:
        out = '{"type":"init","session_id":"sess_abc123"}'
        assert _extract_session_id(out) == "sess_abc123"

    def test_extracts_first_session_id_from_multiline(self) -> None:
        """Stream-json emits many events; we want the first session_id seen."""
        stream = (
            '{"type":"init","session_id":"sess_first"}\n'
            '{"type":"message","content":"working..."}\n'
            '{"type":"end","session_id":"sess_first"}\n'
        )
        assert _extract_session_id(stream) == "sess_first"

    def test_tolerates_non_json_noise_lines(self) -> None:
        """Banner lines / colour codes shouldn't trip the parser."""
        stream = (
            "Claude Code v1.2.3\n"
            'some plain text without braces\n'
            '{"type":"init","session_id":"sess_xyz"}\n'
        )
        assert _extract_session_id(stream) == "sess_xyz"

    def test_falls_back_to_regex_when_lines_unparseable(self) -> None:
        """If JSON parsing fails on every line, the substring fallback runs."""
        stream = '\033[32mok\033[0m {"session_id": "sess_via_regex"} trailing'
        # The line is one big string; substring fallback should find it.
        assert _extract_session_id(stream) == "sess_via_regex"


# ────────────────────────────────────────────────────────────────────────────
#  ClaudeAgent — run(continue_session=...) and reset_session()
# ────────────────────────────────────────────────────────────────────────────
def _stream_result(stdout: str = "done", returncode: int = 0):
    return (stdout, returncode, "", 0.1)


class TestClaudeSessionLifecycle:
    @pytest.fixture
    def claude(self, mock_claude_bin: None) -> agents_module.ClaudeAgent:
        return agents_module.ClaudeAgent(model="claude-sonnet-4-6")

    @pytest.fixture
    def mock_stream(self, monkeypatch) -> MagicMock:
        mock = MagicMock(return_value=_stream_result("done"))
        monkeypatch.setattr(agents_module, "_stream_run", mock)
        return mock

    def test_initial_session_id_is_none(self, claude) -> None:
        assert claude.session_id is None

    def test_run_uses_stream_json_output_format(self, claude, mock_stream) -> None:
        """``--output-format stream-json`` is required for session id capture."""
        claude.invoke("task", "/proj", mutates_artifacts=True)
        cmd = mock_stream.call_args[0][0]
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "stream-json"
        # Stream-json + --print also requires --verbose to emit anything.
        assert "--verbose" in cmd

    def test_run_captures_session_id_from_stdout(self, claude, mock_stream) -> None:
        mock_stream.return_value = _stream_result(
            '{"type":"init","session_id":"sess_one"}'
        )
        claude.invoke("task", "/proj", mutates_artifacts=True)
        assert claude.session_id == "sess_one"

    def test_continue_session_without_id_runs_stateless(self, claude, mock_stream) -> None:
        """First call with continue_session=True still has no id → no --resume."""
        claude.invoke("task", "/proj", mutates_artifacts=True, continue_session=True)
        cmd = mock_stream.call_args[0][0]
        assert "--resume" not in cmd

    def test_second_run_with_continue_uses_resume_flag(self, claude, mock_stream) -> None:
        """After capturing an id, continue_session=True adds --resume <id>."""
        # 1st call: capture session_id
        mock_stream.return_value = _stream_result(
            '{"type":"init","session_id":"sess_chain"}'
        )
        claude.invoke("task1", "/proj", mutates_artifacts=True)
        assert claude.session_id == "sess_chain"
        # 2nd call: --resume should be present
        mock_stream.return_value = _stream_result("ok")
        claude.invoke("task2", "/proj", mutates_artifacts=True, continue_session=True)
        cmd = mock_stream.call_args[0][0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess_chain"

    def test_continue_false_omits_resume_even_with_id(self, claude, mock_stream) -> None:
        """Default continue_session=False → no --resume even after capture."""
        mock_stream.return_value = _stream_result(
            '{"type":"init","session_id":"sess_x"}'
        )
        claude.invoke("task1", "/proj", mutates_artifacts=True)
        claude.invoke("task2", "/proj", mutates_artifacts=True)  # default: stateless
        cmd = mock_stream.call_args[0][0]
        assert "--resume" not in cmd

    def test_reset_session_drops_id(self, claude, mock_stream) -> None:
        mock_stream.return_value = _stream_result(
            '{"type":"init","session_id":"sess_y"}'
        )
        claude.invoke("task1", "/proj", mutates_artifacts=True)
        assert claude.session_id == "sess_y"
        claude.reset_session()
        assert claude.session_id is None
        # And subsequent continue_session=True is silently ignored
        mock_stream.return_value = _stream_result("ok")
        claude.invoke("task2", "/proj", mutates_artifacts=True, continue_session=True)
        cmd = mock_stream.call_args[0][0]
        assert "--resume" not in cmd

    def test_session_preserved_across_run_with_no_id_in_output(
        self, claude, mock_stream
    ) -> None:
        """A run that returns no session_id (parse miss) must not blank an
 existing id — chains are fragile and we shouldn't lose them on noise."""
        mock_stream.return_value = _stream_result(
            '{"type":"init","session_id":"sess_keep"}'
        )
        claude.invoke("task1", "/proj", mutates_artifacts=True)
        assert claude.session_id == "sess_keep"
        # Second call's output has no session_id at all
        mock_stream.return_value = _stream_result("just text, no events")
        claude.invoke("task2", "/proj", mutates_artifacts=True)
        assert claude.session_id == "sess_keep"


#  ``PipelineMode`` enum DELETED. The dispatch surface now
# accepts ``profile_name: str`` directly — no enum sanity test needed
# (profile_loader.load_profiles_v2 covers unknown-name resolution
# in test_profile_loader.py).
