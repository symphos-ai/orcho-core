"""CodexAgent runtime adapter contracts.

After Phase 7 the runtime exposes a single :meth:`CodexAgent.invoke` that
dispatches by ``mutates_artifacts``:

* ``mutates_artifacts=False`` → ``codex exec`` in a read-only sandbox.
* ``mutates_artifacts=True`` → ``codex exec`` with write-capable Orcho flags.

Both paths use ``--json`` so the adapter can capture a resumable session id
and continue it with ``codex exec resume <id>``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agents as agents_module
from agents.runtimes.codex import (
    _FALLBACK_TRUSTED_CWD,
    CodexAgent,
    _extract_codex_assistant_text,
    _extract_codex_session_id,
    _extract_codex_tokens,
    _extract_codex_usage,
    _safe_cwd,
)
from agents.stream_parsers import format_codex_line_for_stdout
from core.io.retry import AgentAuthenticationError

# ── helpers / fixtures ─────────────────────────────────────────────────────


def _subprocess_result(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Shape mimicking ``subprocess.CompletedProcess``."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


def _stream_result(
    stdout: str = "done",
    returncode: int = 0,
    stderr: str = "",
    duration: float = 0.1,
):
    """``_stream_run`` return tuple."""
    return (stdout, returncode, stderr, duration)


@pytest.fixture
def codex(mock_codex_bin: None) -> CodexAgent:
    return CodexAgent(model="gpt-test")


def test_missing_codex_binary_error_names_runtime(monkeypatch) -> None:
    from core.infra import config

    def missing() -> str:
        raise RuntimeError("Cannot find 'codex' binary")

    monkeypatch.setattr(config, "get_codex_bin", missing)
    agent = CodexAgent(model="gpt-test")

    with pytest.raises(RuntimeError, match="codex runtime cannot start"):
        _ = agent.bin


@pytest.fixture
def mock_subprocess_run(monkeypatch) -> MagicMock:
    """Patch ``agents.subprocess.run`` (one-shot) so plan/review paths
    don't fire a real subprocess. Default: empty success."""
    mock = MagicMock(return_value=_subprocess_result(""))
    monkeypatch.setattr(agents_module.subprocess, "run", mock)
    return mock


@pytest.fixture
def mock_stream_run(monkeypatch) -> MagicMock:
    """Patch ``agents._stream_run`` (PTY-based) so ``run()`` doesn't
    fire a real subprocess. Default: success with 'done' output."""
    mock = MagicMock(return_value=_stream_result("done"))
    monkeypatch.setattr(agents_module, "_stream_run", mock)
    return mock


# ── _extract_codex_tokens ──────────────────────────────────────────────────


class TestExtractCodexTokens:
    def test_parses_trailer_with_thousands_separator(self) -> None:
        out = "regular output here\ntokens used\n9,498\n"
        assert _extract_codex_tokens(out) == 9498

    def test_parses_simple_integer(self) -> None:
        out = "tokens used\n42\n"
        assert _extract_codex_tokens(out) == 42

    def test_empty_input_returns_none(self) -> None:
        assert _extract_codex_tokens("") is None

    def test_missing_trailer_returns_none(self) -> None:
        # Codex output without the "tokens used\nN" line — older CLI
        # versions, or call killed mid-stream.
        assert _extract_codex_tokens("just review text, no trailer") is None


class TestExtractCodexUsage:
    def test_parses_turn_completed_usage(self) -> None:
        out = (
            '{"type":"turn.started"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,'
            '"cached_input_tokens":3,"output_tokens":2,'
            '"reasoning_output_tokens":1}}\n'
        )
        assert _extract_codex_usage(out) == {
            "input_tokens": 10,
            "cached_input_tokens": 3,
            "output_tokens": 2,
            "reasoning_output_tokens": 1,
        }

    def test_missing_usage_returns_none(self) -> None:
        assert _extract_codex_usage('{"type":"turn.completed"}') is None
        assert _extract_codex_usage("not-json") is None


class TestExtractCodexAssistantText:
    def test_returns_last_agent_message(self) -> None:
        out = (
            '{"type":"thread.started","thread_id":"t"}\n'
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"First."}}\n'
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"Second."}}\n'
        )
        assert _extract_codex_assistant_text(out) == "Second."

    def test_empty_final_text_does_not_fall_back_to_preamble(self) -> None:
        out = (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"preamble"}}\n'
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":""}}\n'
        )
        assert _extract_codex_assistant_text(out) == ""

    def test_missing_agent_messages_returns_empty(self) -> None:
        assert _extract_codex_assistant_text('{"type":"turn.completed"}') == ""


class TestExtractCodexSessionId:
    def test_parses_top_level_session_id(self) -> None:
        out = '{"type":"thread.started","session_id":"sess-123"}\n'
        assert _extract_codex_session_id(out) == "sess-123"

    def test_parses_nested_thread_id(self) -> None:
        out = '{"type":"event","thread":{"thread_id":"thread-abc"}}\n'
        assert _extract_codex_session_id(out) == "thread-abc"

    def test_falls_back_to_regex_for_noisy_lines(self) -> None:
        out = '\x1b[36m{"conversation_id": "conv-xyz"}\x1b[0m'
        assert _extract_codex_session_id(out) == "conv-xyz"

    def test_missing_id_returns_none(self) -> None:
        assert _extract_codex_session_id("") is None
        assert _extract_codex_session_id('{"type":"message","text":"hi"}') is None


# ── _safe_cwd ──────────────────────────────────────────────────────────────


class TestSafeCwd:
    def test_returns_git_root_when_cwd_is_one(self, tmp_path: Path) -> None:
        # tmp_path itself is a git root (has .git marker).
        (tmp_path / ".git").mkdir()
        assert _safe_cwd(str(tmp_path)) == str(tmp_path)

    def test_walks_up_to_find_git_root(self, tmp_path: Path) -> None:
        # Caller passes a deep subdirectory; _safe_cwd walks parents
        # until it finds .git, then returns that ancestor.
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "src" / "deep" / "subdir"
        nested.mkdir(parents=True)
        assert _safe_cwd(str(nested)) == str(tmp_path)

    def test_walks_down_one_level_for_svn_primary_layout(
        self, tmp_path: Path
    ) -> None:
        # Project root has no .git, but a top-level child does
        # (Unity/_Match-Three-Common style). Downward search finds it.
        child = tmp_path / "Assets"
        child.mkdir()
        (child / ".git").mkdir()
        assert _safe_cwd(str(tmp_path)) == str(child)

    def test_walks_down_two_levels(self, tmp_path: Path) -> None:
        # Deepest case: grandchild has the .git marker.
        grandchild = tmp_path / "Assets" / "Common"
        grandchild.mkdir(parents=True)
        (grandchild / ".git").mkdir()
        assert _safe_cwd(str(tmp_path)) == str(grandchild)

    def test_no_git_anywhere_falls_back_to_workspace(
        self, tmp_path: Path
    ) -> None:
        # Neither up nor down two levels contains .git → fallback path.
        assert _safe_cwd(str(tmp_path)) == _FALLBACK_TRUSTED_CWD

    def test_none_input_returns_fallback(self) -> None:
        # ``review_file(cwd=None)`` ends up here.
        assert _safe_cwd(None) == _FALLBACK_TRUSTED_CWD


# ── plan() ─────────────────────────────────────────────────────────────────


# ── invoke(): read-only path (codex review -) ──────────────────────────────


class TestInvokeReadOnly:
    def test_happy_path_returns_stdout(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        def fake_stream(cmd, **_kwargs):
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_text("planner stdout text", encoding="utf-8")
            return _stream_result('{"session_id":"sess-read"}')

        mock_stream_run.side_effect = fake_stream
        assert (
            codex.invoke("any read-only prompt", "/project")
            == "planner stdout text"
        )
        assert codex.session_id == "sess-read"

    def test_nonzero_returncode_raises_runtime_error(
        self,
        codex: CodexAgent,
        mock_stream_run: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Generic stderr (no transient signature) → classified as a generic
        # AgentCallError and raised at once, no retry. stderr still surfaces.
        mock_stream_run.return_value = _stream_result(
            stdout="", returncode=2, stderr="codex crashed",
        )
        with pytest.raises(RuntimeError):
            codex.invoke("task", "/project")
        captured = capsys.readouterr()
        assert "codex crashed" in captured.out

    def test_auth_failure_raises_user_facing_error(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            stdout="Failed to authenticate. API Error: 401 Invalid authentication credentials",
            returncode=1,
            stderr="",
        )
        with pytest.raises(AgentAuthenticationError) as exc_info:
            codex.invoke("task", "/project")

        msg = str(exc_info.value)
        assert "runtime='codex'" in msg
        assert "model='gpt-test'" in msg
        assert "Runtime credentials were rejected" in msg
        assert "codex login" in msg
        assert "codex login status" in msg
        assert "codex exec --json --dangerously-bypass-approvals-and-sandbox" in msg
        assert "codex login --with-api-key" in msg
        assert "Original CLI error" not in msg
        assert "Failed to authenticate" not in msg

    def test_uses_exec_subcommand_with_bypass_flag(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        # Read calls now use ``--dangerously-bypass-approvals-and-sandbox``
        # — same flag as write calls. ``--sandbox read-only`` was
        # blocking reviewer subprocess execution (pytest, ruff,
        # git verification) without buying enough safety to
        # justify the cost. Destructive git ops are still caught
        # by the streaming guardrail.
        codex.invoke("the prompt", "/project")
        cmd = mock_stream_run.call_args[0][0]
        assert cmd[1] == "exec"
        assert "resume" not in cmd[:3]
        assert "--json" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        # ``--sandbox`` is no longer threaded through — pin its absence
        # so a regression cannot silently bring back the restrictive
        # mode.
        assert "--sandbox" not in cmd
        assert cmd[-1] == "the prompt"


# ── invoke(): write path (codex exec) ──────────────────────────────────────


class TestInvokeWrite:
    def test_happy_path_returns_stdout(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        def fake_stream(cmd, **_kwargs):
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_text("exec output", encoding="utf-8")
            return _stream_result('{"type":"thread.started","thread_id":"sess"}')

        mock_stream_run.side_effect = fake_stream
        assert (
            codex.invoke("do thing", "/project", mutates_artifacts=True)
            == "exec output"
        )

    def test_uses_exec_subcommand_with_write_flags(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        codex.invoke("task", "/project", mutates_artifacts=True)
        cmd = mock_stream_run.call_args[0][0]
        assert "exec" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--json" in cmd

    def test_wires_codex_stdout_and_log_filters(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        codex.invoke("task", "/project", mutates_artifacts=True)
        kwargs = mock_stream_run.call_args[1]
        assert kwargs["stdout_filter"] is format_codex_line_for_stdout
        assert kwargs["log_filter"] is format_codex_line_for_stdout
        assert kwargs["return_filter"].__name__ == "elide_tool_result_line_for_model"

    def test_on_line_parses_codex_events_before_guard(
        self, codex: CodexAgent, mock_stream_run: MagicMock, monkeypatch,
    ) -> None:
        import agents.stream_parsers as parsers

        seen: list[str] = []

        def fake_parse(line: str, *, agent_label: str | None = None) -> None:
            seen.append(f"{agent_label}:{line.strip()}")

        def fake_stream(_cmd, **kwargs):
            kwargs["on_line"]('{"type":"turn.started"}\n')
            return _stream_result("")

        monkeypatch.setattr(parsers, "parse_codex_line", fake_parse)
        mock_stream_run.side_effect = fake_stream

        codex.invoke("task", "/project", mutates_artifacts=True)

        assert seen == ['invoke:{"type":"turn.started"}']

    def test_first_call_captures_session_id(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            '{"type":"thread.started","session_id":"sess-first"}\n',
        )
        codex.invoke("task", "/project", mutates_artifacts=True)
        assert codex.session_id == "sess-first"

    def test_continue_session_uses_exec_resume(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        codex.session_id = "sess-prior"
        codex.invoke(
            "task", "/project", mutates_artifacts=True, continue_session=True,
        )
        cmd = mock_stream_run.call_args[0][0]
        assert cmd[:3] == [codex.bin, "exec", "resume"]
        assert "sess-prior" in cmd
        assert cmd[-2:] == ["sess-prior", "task"]
        assert "--cd" not in cmd

    def test_nonzero_returncode_raises_runtime_error(
        self,
        codex: CodexAgent,
        mock_stream_run: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_stream_run.return_value = _stream_result(
            stdout="", returncode=1, stderr="exec broke",
        )
        with pytest.raises(RuntimeError):
            codex.invoke("task", "/project", mutates_artifacts=True)
        captured = capsys.readouterr()
        assert "exec broke" in captured.out

    def test_guardrail_blocked_returns_sentinel_message(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        from agents.command_guard import ORCHO_GUARDRAIL_BLOCKED
        mock_stream_run.return_value = _stream_result(
            stdout="",
            returncode=1,
            stderr=f"{ORCHO_GUARDRAIL_BLOCKED}: destructive_git: rm -rf .git",
        )
        out = codex.invoke("task", "/project", mutates_artifacts=True)
        assert out.startswith(ORCHO_GUARDRAIL_BLOCKED)
        assert "destructive_git" in out

    def test_empty_last_message_falls_back_to_agent_message_text(
        self, codex: CodexAgent, mock_stream_run: MagicMock,
    ) -> None:
        stdout = (
            '{"type":"thread.started","thread_id":"sess-fallback"}\n'
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"JSONL body stripped."}}\n'
        )
        mock_stream_run.return_value = _stream_result(stdout)

        out = codex.invoke("task", "/project", mutates_artifacts=True)

        assert out == "JSONL body stripped."
        assert "item.completed" not in out

    def test_capture_tokens_prefers_turn_completed_usage(
        self, codex: CodexAgent,
    ) -> None:
        """Split fields are exposed; subset fields must not double-count."""
        codex._capture_tokens(
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"command":"pytest"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,'
            '"cached_input_tokens":3,"output_tokens":2,'
            '"reasoning_output_tokens":1}}\n',
            stderr="",
        )
        assert codex.last_tokens_in == 10
        assert codex.last_tokens_in_cache_read == 3
        assert codex.last_tokens_in_fresh == 7
        assert codex.last_tokens_out == 2
        assert codex.last_tokens_out_reasoning == 1
        assert codex.last_tokens_total == 12
        assert codex.last_tool_use_count == 1

    def test_capture_tokens_keeps_legacy_trailer_fallback(
        self, codex: CodexAgent,
    ) -> None:
        codex.last_tokens_in = 10
        codex.last_tokens_out = 2
        codex.last_tokens_in_cache_read = 3
        codex.last_tokens_out_reasoning = 1
        codex._capture_tokens("tokens used\n1,234\n", stderr="")
        assert codex.last_tokens_total == 1234
        assert codex.last_tokens_in is None
        assert codex.last_tokens_out is None
        assert codex.last_tokens_in_cache_read is None
        assert codex.last_tokens_out_reasoning is None

    def test_capture_tokens_preserves_missing_cache_as_unknown(
        self, codex: CodexAgent,
    ) -> None:
        codex._capture_tokens(
            '{"type":"turn.completed","usage":{"input_tokens":10,'
            '"output_tokens":2}}\n',
            stderr="",
        )
        assert codex.last_tokens_in == 10
        assert codex.last_tokens_out == 2
        assert codex.last_tokens_total == 12
        assert codex.last_tokens_in_cache_read is None
        assert codex.last_tokens_in_fresh is None
        assert codex.last_tokens_out_reasoning is None


class TestResetSession:
    def test_clears_session_id(self, codex: CodexAgent) -> None:
        codex.session_id = "decorative-id"
        codex.reset_session()
        assert codex.session_id is None


# ── _capture_brain_telemetry: rollout-sourced context attrs ────────────────


class TestCaptureBrainTelemetry:
    def test_capture_brain_telemetry_populates_runtime_context_attrs(
        self, codex: CodexAgent, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agents.runtimes.codex_telemetry import CodexTelemetrySnapshot

        codex.session_id = "fake-sid"
        snap = CodexTelemetrySnapshot(
            context_window_tokens=258400,
            context_used_tokens=42000,
            context_remaining_tokens=216400,
            rate_limits={"plan_type": "prolite"},
            raw_source_path="/tmp/rollout-fake.jsonl",
        )
        monkeypatch.setattr(
            "agents.runtimes.codex.load_codex_telemetry",
            lambda *_args, **_kwargs: snap,
        )
        codex._capture_brain_telemetry()
        assert codex.last_context_window_tokens == 258400
        assert codex.last_context_used_tokens == 42000
        assert codex.last_context_remaining_tokens == 216400
        assert codex.last_codex_rate_limits == {"plan_type": "prolite"}
        assert codex.last_codex_telemetry_source == "/tmp/rollout-fake.jsonl"

    def test_capture_brain_telemetry_without_rollout_leaves_attrs_none(
        self, codex: CodexAgent, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        codex.session_id = "fake-sid"
        # Pre-populate to prove the reset path clears everything.
        codex.last_context_window_tokens = 999
        codex.last_context_used_tokens = 999
        codex.last_context_remaining_tokens = 999
        codex.last_codex_rate_limits = {"stale": True}
        codex.last_codex_telemetry_source = "/stale"
        monkeypatch.setattr(
            "agents.runtimes.codex.load_codex_telemetry",
            lambda *_args, **_kwargs: None,
        )
        codex._capture_brain_telemetry()
        assert codex.last_context_window_tokens is None
        assert codex.last_context_used_tokens is None
        assert codex.last_context_remaining_tokens is None
        assert codex.last_codex_rate_limits is None
        assert codex.last_codex_telemetry_source is None

    def test_partial_snapshot_does_not_populate_context_attrs(
        self, codex: CodexAgent, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both-or-neither: used without window must not become RUNTIME_REPORTED.

        Debug attrs are still set independently.
        """
        from agents.runtimes.codex_telemetry import CodexTelemetrySnapshot

        codex.session_id = "fake-sid"
        snap = CodexTelemetrySnapshot(
            context_window_tokens=None,
            context_used_tokens=100,
            context_remaining_tokens=None,
            rate_limits={"plan_type": "prolite"},
            raw_source_path="/tmp/rollout.jsonl",
        )
        monkeypatch.setattr(
            "agents.runtimes.codex.load_codex_telemetry",
            lambda *_args, **_kwargs: snap,
        )
        codex._capture_brain_telemetry()
        assert codex.last_context_window_tokens is None
        assert codex.last_context_used_tokens is None
        assert codex.last_context_remaining_tokens is None
        assert codex.last_codex_rate_limits == {"plan_type": "prolite"}
        assert codex.last_codex_telemetry_source == "/tmp/rollout.jsonl"

    def test_no_session_id_returns_without_loading(
        self, codex: CodexAgent, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called: list[bool] = []

        def _spy(*_args, **_kwargs):
            called.append(True)
            return None
        monkeypatch.setattr(
            "agents.runtimes.codex.load_codex_telemetry", _spy,
        )
        codex.session_id = None
        codex._capture_brain_telemetry()
        assert called == []
        assert codex.last_context_window_tokens is None

    def test_reset_session_clears_brain_telemetry(
        self, codex: CodexAgent,
    ) -> None:
        codex.session_id = "sid"
        codex.last_context_window_tokens = 1
        codex.last_context_used_tokens = 1
        codex.last_context_remaining_tokens = 1
        codex.last_codex_rate_limits = {"x": 1}
        codex.last_codex_telemetry_source = "/tmp/x"
        codex.reset_session()
        assert codex.session_id is None
        assert codex.last_context_window_tokens is None
        assert codex.last_context_used_tokens is None
        assert codex.last_context_remaining_tokens is None
        assert codex.last_codex_rate_limits is None
        assert codex.last_codex_telemetry_source is None


# ── invoke(): _safe_cwd integration ────────────────────────────────────────


class TestInvokeSafeCwd:
    def test_stream_run_receives_resolved_git_root(
        self,
        codex: CodexAgent,
        mock_stream_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Pass a deep subdirectory; ``codex exec`` must run from the
        upward-resolved git root, not the original nested path.
        """
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "src" / "feature"
        nested.mkdir(parents=True)

        codex.invoke("any prompt", str(nested))

        assert mock_stream_run.call_args[1]["cwd"] == str(tmp_path)


# ── invoke(): attachment defensive contract ────────────────────────────────


class TestInvokeAttachmentsContract:
    def test_text_attachment_raises_value_error(
        self, codex: CodexAgent, mock_subprocess_run: MagicMock,
    ) -> None:
        from pipeline.runtime.roles import AttachmentKind
        from pipeline.runtime.steps import Attachment

        text_att = Attachment(
            kind=AttachmentKind.TEXT, name="ctx.md", content_b64="aGk=",
        )
        with pytest.raises(ValueError, match="TEXT"):
            codex.invoke("hi", "/project", attachments=(text_att,))


# ── probe_identity (account diagnostics) ───────────────────────────────────


class TestCodexProbeIdentity:
    def test_returns_unavailable_without_account_surface(self, codex) -> None:
        ident = codex.probe_identity()
        assert ident.available is False
        assert ident.runtime == "codex"
        assert ident.source == "no_account_surface"
        assert ident.account_label is None
        assert ident.email is None

    def test_does_not_fire_a_subprocess(self, codex, monkeypatch) -> None:
        # The current Codex status surface carries no account signal, so the
        # probe must short-circuit without spawning anything.
        run = MagicMock()
        stream = MagicMock()
        monkeypatch.setattr(agents_module.subprocess, "run", run)
        monkeypatch.setattr(agents_module, "_stream_run", stream)
        codex.probe_identity()
        run.assert_not_called()
        stream.assert_not_called()

    def test_does_not_resolve_binary(self, monkeypatch) -> None:
        from core.infra import config

        def missing() -> str:
            raise RuntimeError("Cannot find 'codex' binary")

        monkeypatch.setattr(config, "get_codex_bin", missing)
        agent = CodexAgent(model="gpt-test")
        ident = agent.probe_identity()  # must not raise, must not touch .bin
        assert ident.available is False
