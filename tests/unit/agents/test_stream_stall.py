# SPDX-License-Identifier: Apache-2.0
"""Stream-level stall monitor (T3).

Covers the two stall paths wired into ``_stream_run`` via the opt-in
``stall_sink``:

* **non-terminal** unsafe free-text process polling — written through to the
  provider-neutral sink AT DETECTION (mid-stream), never killing/raising, the
  emitted event consumable by the T1 live projector;
* **terminal** idle-timeout — the single auto-kill trigger: terminal event +
  scoped kill (own child group, no pgrep) + ``AgentCommandStalledError``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import agents.stream as stream
from agents.stall_protocol import (
    AgentCommandStalledError,
    EventStallDiagnosticSink,
    StalledCommand,
    StallReason,
)
from agents.stream import _stream_run
from agents.stream_stall import StreamStallMonitor
from core.observability import events as _events
from sdk.evidence_slices import active_stall_diagnostics


class _SpySink:
    """Records every StalledCommand handed to it (never raises)."""

    def __init__(self) -> None:
        self.recorded: list[StalledCommand] = []

    def record(self, stalled: StalledCommand) -> None:
        self.recorded.append(stalled)


# ─────────────────────────────────────────────────────────────────────────────
# Monitor unit behaviour (no subprocess)
# ─────────────────────────────────────────────────────────────────────────────


def test_inspect_line_writes_through_unsafe_polling_at_call() -> None:
    sink = _SpySink()
    mon = StreamStallMonitor(phase="implement", sink=sink)
    recorded = mon.inspect_line(
        'tool Bash: kill -0 $(pgrep -f "pytest -q -m")', elapsed_s=4.0,
    )
    assert recorded is True
    assert len(sink.recorded) == 1
    sc = sink.recorded[0]
    assert sc.reason == StallReason.UNSAFE_PROCESS_POLLING
    assert sc.phase == "implement"
    assert sc.elapsed_s == 4.0
    # Non-terminal diagnostic: not about a kill, so no own-group is attached.
    assert sc.process_group is None


def test_inspect_line_dedupes_repeated_poll_command() -> None:
    sink = _SpySink()
    mon = StreamStallMonitor(phase="implement", sink=sink)
    line = 'tool Bash: pgrep -f "pytest -q -m"'
    assert mon.inspect_line(line, elapsed_s=1.0) is True
    assert mon.inspect_line(line, elapsed_s=2.0) is False  # same command
    assert len(sink.recorded) == 1


def test_inspect_line_ignores_safe_and_foreign_argv() -> None:
    sink = _SpySink()
    mon = StreamStallMonitor(phase="implement", sink=sink)
    # A bare `pytest -q -m` argv (the dogfood foreign process) is NOT a poll.
    assert mon.inspect_line("tool Bash: pytest -q -m 'not e2e'", elapsed_s=1.0) is False
    # Polling the run's OWN child by PID is fine.
    assert mon.inspect_line("tool Bash: kill -0 12345", elapsed_s=1.0) is False
    # grep -f is not pgrep -f.
    assert mon.inspect_line("tool Bash: grep -f pats.txt out.log", elapsed_s=1.0) is False
    assert sink.recorded == []


def test_idle_stall_silent_vs_inactivity_classification() -> None:
    sink = _SpySink()
    mon = StreamStallMonitor(phase="implement", sink=sink, command_preview="claude --x")
    # No output ever → silent child.
    silent = mon.idle_stall(elapsed_s=300.0, process_group=4242)
    assert silent.reason == StallReason.SILENT_CHILD_COMMAND
    assert silent.process_group == 4242
    assert silent.command_preview == "claude --x"
    # After output, an idle window is output inactivity, carrying a tail.
    mon.note_output("some progress line\n")
    inactive = mon.idle_stall(elapsed_s=305.0, process_group=4242)
    assert inactive.reason == StallReason.OUTPUT_INACTIVITY
    assert "some progress line" in inactive.output_tail


# ─────────────────────────────────────────────────────────────────────────────
# _stream_run integration — non-terminal write-through (no kill / no raise)
# ─────────────────────────────────────────────────────────────────────────────


def test_stream_run_unsafe_poll_writes_through_without_kill(tmp_path: Path) -> None:
    """A single unsafe poll: sink.record fires mid-stream (before the phase's
    final line), the process runs to normal completion, no
    AgentCommandStalledError, no kill."""
    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    kills: list[bool] = []
    orig_kill = stream._kill_subprocess_tree

    def _spy_kill(proc, *, group_owned):
        kills.append(group_owned)
        return orig_kill(proc, group_owned=group_owned)

    sink = _SpySink()
    code = (
        "import time\n"
        "print('kill -0 $(pgrep -f \\'pytest -q -m\\')', flush=True)\n"
        "time.sleep(0.3)\n"
        "print('SENTINEL-END', flush=True)\n"
    )

    seen_sentinel_before_record: list[bool] = []

    def _on_line(line: str) -> None:
        if "SENTINEL-END" in line:
            # By the time the phase's LAST line is processed, the poll detected
            # earlier was already written through — proving at-detection, not
            # after-phase.
            seen_sentinel_before_record.append(bool(sink.recorded))

    try:
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(stream, "_kill_subprocess_tree", _spy_kill)
            _stdout, rc, _stderr, _dur = _stream_run(
                [sys.executable, "-c", code],
                on_line=_on_line,
                stall_sink=sink,
                stall_phase="implement",
            )
        # Ran to completion: no kill, normal exit.
        assert rc == 0
        assert kills == []
        # Write-through happened during the stream, at detection.
        assert len(sink.recorded) == 1
        assert sink.recorded[0].reason == StallReason.UNSAFE_PROCESS_POLLING
        assert seen_sentinel_before_record == [True]
    finally:
        _events.init_event_store(None)


def test_stream_run_unsafe_poll_default_sink_emits_non_terminal(tmp_path: Path) -> None:
    """The default EventStallDiagnosticSink emits a non-terminal event that the
    T1 live projector sees, written BEFORE the phase's final line — and the run
    still exits cleanly with no kill."""
    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    code = (
        "import time\n"
        "print('pgrep -f \\'pytest -q -m\\'', flush=True)\n"
        "time.sleep(0.3)\n"
        "print('SENTINEL-END', flush=True)\n"
    )

    def _on_line(line: str) -> None:
        if "SENTINEL-END" in line:
            _events.emit("test.marker", note="end")

    try:
        _stdout, rc, _stderr, _dur = _stream_run(
            [sys.executable, "-c", code],
            on_line=_on_line,
            stall_sink=EventStallDiagnosticSink(),
            stall_phase="review_changes",
        )
        assert rc == 0
        evs = _events.read_all(run_dir)
        # The non-terminal stall event is in the live store, non-terminal...
        stall = [e for e in evs if e.kind == "agent.command_stalled"]
        assert len(stall) == 1
        assert stall[0].payload["terminal"] is False
        # ...and was written BEFORE the end-of-phase marker (at detection).
        marker_seq = next(e.seq for e in evs if e.kind == "test.marker")
        assert stall[0].seq < marker_seq
        # T1 compatibility: the live projector consumes it.
        diags = active_stall_diagnostics(run_dir)
        assert len(diags) == 1
        assert diags[0].terminal is False
    finally:
        _events.init_event_store(None)


# ─────────────────────────────────────────────────────────────────────────────
# _stream_run integration — terminal idle-timeout (the single auto-kill)
# ─────────────────────────────────────────────────────────────────────────────


def test_stream_run_idle_timeout_escalates_to_stalled_error(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    kills: list[bool] = []
    orig_kill = stream._kill_subprocess_tree

    def _spy_kill(proc, *, group_owned):
        kills.append(group_owned)
        return orig_kill(proc, group_owned=group_owned)

    try:
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(stream, "_kill_subprocess_tree", _spy_kill)
            with pytest.raises(AgentCommandStalledError) as excinfo:
                _stream_run(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    idle_timeout=1,
                    stall_sink=_SpySink(),
                    stall_phase="implement",
                )
        stalled = excinfo.value.stalled
        # No output was produced → silent child.
        assert stalled.reason == StallReason.SILENT_CHILD_COMMAND
        assert stalled.phase == "implement"
        # The ONLY kill went through the scoped helper (no pgrep/pkill anywhere).
        assert kills == [False]  # no sandbox here → single-PID own-child kill
        # F2: the stream does NOT emit the terminal event — that is the single
        # authoritative job of the pipeline failure handler once the raised
        # AgentCommandStalledError propagates up. The carrier rides on the raise.
        evs = _events.read_all(run_dir)
        terminal = [
            e for e in evs
            if e.kind == "agent.command_stalled" and e.payload.get("terminal") is True
        ]
        assert terminal == []
        # No live non-terminal diagnostic either (a silent child never polled).
        assert active_stall_diagnostics(run_dir) == []
    finally:
        _events.init_event_store(None)


def test_idle_timeout_without_sink_keeps_legacy_return(tmp_path: Path) -> None:
    """Without a stall_sink the historical behaviour is preserved: idle-timeout
    kills and returns normally (no AgentCommandStalledError)."""
    stdout, rc, stderr, dur = _stream_run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        idle_timeout=1,
    )
    assert rc != 0
    assert "IDLE TIMEOUT" in stderr
    assert dur < 3


# ─────────────────────────────────────────────────────────────────────────────
# Production runtime wiring — the stall monitor is a default, not opt-in
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _runtime_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    mock_claude_bin: None,
    mock_codex_bin: None,
    mock_gemini_bin: None,
) -> None:
    """Keep runtime retry backoff instant and CLI lookup hermetic."""
    from agents.runtimes import _failures

    monkeypatch.setattr(_failures, "_sleep", lambda _s: None)


def _runtime_classes() -> list:
    from agents.runtimes.claude import ClaudeAgent
    from agents.runtimes.codex import CodexAgent
    from agents.runtimes.gemini import GeminiAgent

    return [ClaudeAgent, CodexAgent, GeminiAgent]


@pytest.mark.parametrize("agent_cls", _runtime_classes())
def test_runtime_invoke_wires_default_sink_and_phase(
    agent_cls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    _runtime_test_environment: None,
) -> None:
    """Through the REAL runtime invoke callsite (no manual opt-in): the
    production ``_stream_run`` call carries a provider-neutral
    ``EventStallDiagnosticSink`` and the active phase label. A non-terminal
    write-through via that sink lands a live ``agent.command_stalled`` event the
    T1 projector consumes while the phase is still running."""
    import agents as agents_module

    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    _events.set_phase("implement")
    captured: dict = {}

    def _fake_stream_run(cmd, *args, **kwargs):
        captured["stall_sink"] = kwargs.get("stall_sink")
        captured["stall_phase"] = kwargs.get("stall_phase")
        # Exercise the wired sink exactly as the stream monitor would on an
        # unsafe-poll detection: a non-terminal write-through, mid-invoke.
        sink = kwargs.get("stall_sink")
        if sink is not None:
            sink.record(
                StalledCommand(
                    phase=kwargs.get("stall_phase") or "",
                    elapsed_s=3.0,
                    command_preview="kill -0 $(pgrep -f 'pytest -q -m')",
                    output_tail="",
                    reason=StallReason.UNSAFE_PROCESS_POLLING,
                )
            )
        return ("hello", 0, "", 0.01)

    monkeypatch.setattr(agents_module, "_stream_run", _fake_stream_run)
    try:
        agent_cls(model="m-test").invoke("hi", str(tmp_path))

        # The production callsite passed a real default sink + the active phase.
        assert isinstance(captured["stall_sink"], EventStallDiagnosticSink)
        assert captured["stall_phase"] == "implement"

        # The write-through landed a live, non-terminal diagnostic.
        diags = active_stall_diagnostics(run_dir)
        assert len(diags) == 1
        assert diags[0].terminal is False
        assert diags[0].reason == "unsafe_process_polling"
    finally:
        _events.set_phase(None)
        _events.init_event_store(None)


@pytest.mark.parametrize("agent_cls", _runtime_classes())
def test_runtime_invoke_propagates_terminal_stalled_error(
    agent_cls,
    monkeypatch: pytest.MonkeyPatch,
    _runtime_test_environment: None,
) -> None:
    """An idle-timeout escalation raised inside ``_stream_run`` propagates out of
    the real runtime ``invoke()`` (it is NOT swallowed by the retry policy, which
    only catches transport/runtime errors) so the pipeline failure handler can
    record the terminal stall."""
    import agents as agents_module

    stalled = StalledCommand(
        phase="implement",
        elapsed_s=300.0,
        command_preview="claude --x",
        output_tail="",
        reason=StallReason.SILENT_CHILD_COMMAND,
        process_group=4242,
    )

    def _fake_stream_run(cmd, *args, **kwargs):
        raise AgentCommandStalledError(stalled)

    monkeypatch.setattr(agents_module, "_stream_run", _fake_stream_run)
    with pytest.raises(AgentCommandStalledError) as excinfo:
        agent_cls(model="m-test").invoke("hi", "/project")
    assert excinfo.value.stalled.reason == StallReason.SILENT_CHILD_COMMAND


def test_no_process_control_mechanism_in_stream_stall_source() -> None:
    """Guard the invariant: the monitor performs NO process control — no
    spawning, no signalling, no pgrep/pkill matching. Detection is text-only;
    the scoped kill lives in stream.py's ``_kill_subprocess_tree``."""
    src = Path(stream.__file__).with_name("stream_stall.py").read_text()
    for mechanism in (
        "import subprocess", "os.system", "Popen", "os.kill",
        "killpg", "pgrep ", "pkill ", "subprocess.run",
    ):
        assert mechanism not in src, f"unexpected process-control token: {mechanism!r}"


def test_unsafe_poll_then_idle_is_inactivity(tmp_path: Path) -> None:
    """A run that emits a poll line (output) then goes silent classifies the
    terminal idle as output_inactivity, not silent_child_command."""
    run_dir = tmp_path / "run"
    _events.init_event_store(run_dir)
    code = (
        "import time\n"
        "print('pgrep -f \\'pytest -q -m\\'', flush=True)\n"
        "time.sleep(5)\n"
    )
    try:
        with pytest.raises(AgentCommandStalledError) as excinfo:
            _stream_run(
                [sys.executable, "-c", code],
                idle_timeout=1,
                stall_sink=EventStallDiagnosticSink(),
                stall_phase="implement",
            )
        assert excinfo.value.stalled.reason == StallReason.OUTPUT_INACTIVITY
        # The stream emits ONLY the non-terminal write-through (from the poll
        # line). The terminal escalation event is emitted by the pipeline
        # failure handler, not here (F2) — so the stream store carries the
        # non-terminal record but no terminal one.
        evs = _events.read_all(run_dir)
        kinds = [
            (e.payload.get("terminal"))
            for e in evs if e.kind == "agent.command_stalled"
        ]
        assert False in kinds
        assert True not in kinds
    finally:
        _events.init_event_store(None)
