"""
PTY streamer watchdog behavior.
"""

from __future__ import annotations

import sys

import agents.stream as stream_module
from agents.pty_diagnostics import PTY_EXHAUSTED_SENTINEL
from agents.stream import (
    _stream_run,
    set_agent_log,
    set_stdout_echo,
)
from agents.stream_log import append_agent_log_section
from agents.stream_parsers.claude_jsonl import format_claude_line_for_stdout
from core.io.output_elision import (
    elide_tool_result_line_for_model,
    utf8_len,
)


def test_stream_run_reports_pty_exhaustion_without_traceback(monkeypatch, tmp_path) -> None:
    log_path = tmp_path / "agent.log"

    def raise_pty_exhausted() -> object:
        # PTY-pool exhaustion now surfaces from transport construction
        # (``PtyTransport.__init__`` → ``pty.openpty``). The streamer classifies
        # the OSError and renders the recovery diagnostic instead of a traceback.
        raise OSError("out of pty devices")

    monkeypatch.setattr(stream_module, "select_transport", raise_pty_exhausted)
    set_agent_log(log_path)
    try:
        stdout, returncode, stderr, duration = _stream_run(
            [sys.executable, "-c", "print('never-started')"],
            label="test-agent",
        )
    finally:
        set_agent_log(None)

    assert stdout == ""
    assert returncode == 126
    assert duration >= 0
    assert PTY_EXHAUSTED_SENTINEL in stderr
    assert "system resource blocker" in stderr
    assert "python -c \"import pty; print(pty.openpty())\"" in stderr
    assert "Traceback" not in stderr

    log_text = log_path.read_text(encoding="utf-8")
    assert PTY_EXHAUSTED_SENTINEL in log_text
    assert "[EXIT code=126" in log_text


def test_append_agent_log_section_does_not_echo_stdout(capsys, tmp_path) -> None:
    log_path = tmp_path / "agent.log"
    set_agent_log(log_path)
    set_stdout_echo(True)
    try:
        append_agent_log_section(
            "Verification gates -- pre-final auto-run",
            "commands: env-provenance FRESH · lint FRESH",
        )
    finally:
        set_stdout_echo(False)
        set_agent_log(None)

    assert capsys.readouterr().out == ""
    log_text = log_path.read_text(encoding="utf-8")
    assert "Verification gates -- pre-final auto-run" in log_text
    assert "env-provenance FRESH" in log_text
    assert "lint FRESH" in log_text


def test_stream_run_idle_timeout_kills_silent_process() -> None:
    stdout, returncode, stderr, duration = _stream_run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        idle_timeout=1,
    )

    assert stdout == ""
    assert returncode != 0
    assert "IDLE TIMEOUT" in stderr
    assert duration < 3


def test_stream_run_idle_timeout_resets_on_output() -> None:
    code = (
        "import sys, time\n"
        "for i in range(4):\n"
        "    print(f'tick-{i}', flush=True)\n"
        "    time.sleep(0.25)\n"
    )
    stdout, returncode, stderr, duration = _stream_run(
        [sys.executable, "-c", code],
        idle_timeout=1,
    )

    assert returncode == 0
    assert "IDLE TIMEOUT" not in stderr
    assert "tick-0" in stdout
    assert "tick-3" in stdout
    assert duration >= 0.75


def test_stream_run_echoes_stdout_when_enabled(capsys) -> None:
    set_stdout_echo(True)
    try:
        stdout, returncode, stderr, _duration = _stream_run(
            [sys.executable, "-c", "print('hello-stream', flush=True)"],
        )
    finally:
        set_stdout_echo(False)

    assert returncode == 0
    assert stderr == ""
    assert "hello-stream" in stdout
    assert "hello-stream" in capsys.readouterr().out


def test_mock_agent_log_echoes_stdout_without_log(capsys) -> None:
    import agents.stream as _stream
    from agents.runtimes._strategy import _write_to_agent_log

    old_log = _stream._agent_log
    _stream._agent_log = None
    set_stdout_echo(True)
    try:
        _write_to_agent_log("MOCK phase", "mock-content\n", duration_s=0.12)
    finally:
        set_stdout_echo(False)
        _stream._agent_log = old_log

    out = capsys.readouterr().out
    assert "MOCK phase" in out
    assert "mock-content" in out


def test_stream_run_stdout_filter_keeps_returned_stdout_raw(capsys) -> None:
    system_line = '{"type":"system","subtype":"init"}'
    assistant_line = (
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"hello pretty"}]}}'
    )
    code = f"print({system_line!r}); print({assistant_line!r})"

    set_stdout_echo(True)
    try:
        stdout, returncode, stderr, _duration = _stream_run(
            [sys.executable, "-c", code],
            stdout_filter=format_claude_line_for_stdout,
        )
    finally:
        set_stdout_echo(False)

    echoed = capsys.readouterr().out
    assert returncode == 0
    assert stderr == ""
    assert system_line in stdout
    assert assistant_line in stdout
    assert "hello pretty" in echoed
    assert '"type":"system"' not in echoed


def test_stream_run_log_filter_keeps_returned_stdout_raw(tmp_path) -> None:
    raw_line = '{"type":"system","subtype":"init"}'
    text_line = (
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"hello log"}]}}'
    )
    code = f"print({raw_line!r}); print({text_line!r})"
    log_path = tmp_path / "agent.log"

    set_agent_log(log_path)
    try:
        stdout, returncode, stderr, _duration = _stream_run(
            [sys.executable, "-c", code],
            log_filter=format_claude_line_for_stdout,
        )
    finally:
        set_agent_log(None)

    log_text = log_path.read_text(encoding="utf-8")
    assert returncode == 0
    assert stderr == ""
    assert raw_line in stdout
    assert text_line in stdout
    assert "hello log" in log_text
    assert '"type":"system"' not in log_text


def test_stream_run_log_filter_exception_falls_back_to_raw(tmp_path) -> None:
    log_path = tmp_path / "agent.log"

    def broken_filter(_line: str) -> str | None:
        raise RuntimeError("formatter broke")

    set_agent_log(log_path)
    try:
        stdout, returncode, stderr, _duration = _stream_run(
            [sys.executable, "-c", "print('raw-fallback', flush=True)"],
            log_filter=broken_filter,
        )
    finally:
        set_agent_log(None)

    log_text = log_path.read_text(encoding="utf-8")
    assert returncode == 0
    assert stderr == ""
    assert "raw-fallback" in stdout
    assert "raw-fallback" in log_text


def test_stream_run_return_filter_caps_single_line_tool_result() -> None:
    code = (
        "import json\n"
        "print(json.dumps({"
        "'type':'tool_result',"
        "'tool_id':'shell',"
        "'status':'success',"
        "'output':'M' * (2 * 1024 * 1024)"
        "}), flush=True)\n"
    )

    stdout, returncode, stderr, _duration = _stream_run(
        [sys.executable, "-c", code],
        return_filter=lambda line: elide_tool_result_line_for_model(
            line, max_bytes=64 * 1024,
        ),
    )

    assert returncode == 0
    assert stderr == ""
    assert utf8_len(stdout) <= 64 * 1024
    assert "omitted" in stdout


def test_stream_run_log_elides_oversized_raw_formatter_output(tmp_path) -> None:
    log_path = tmp_path / "agent.log"
    code = "print('Z' * (2 * 1024 * 1024), flush=True)"

    set_agent_log(log_path)
    try:
        stdout, returncode, stderr, _duration = _stream_run(
            [sys.executable, "-c", code],
            log_filter=lambda line: line,
        )
    finally:
        set_agent_log(None)

    log_text = log_path.read_text(encoding="utf-8")
    assert returncode == 0
    assert stderr == ""
    assert utf8_len(stdout) > 1024 * 1024
    assert utf8_len(log_text) < 200 * 1024
    assert "omitted" in log_text
