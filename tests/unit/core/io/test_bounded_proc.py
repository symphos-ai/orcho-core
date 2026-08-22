from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from core.io.bounded_proc import Completed, SpawnFailure, TimedOut, run_bounded
from core.io.process_tree import pid_is_alive
from core.io.service_command import ServiceCommandEvent, observe_service_commands


def test_completed_preserves_binary_and_text_output() -> None:
    code = "import sys; sys.stdout.buffer.write(b'\\xff'); sys.stderr.write('err')"
    binary = run_bounded([sys.executable, "-c", code], timeout_s=1)
    text = run_bounded([sys.executable, "-c", code], timeout_s=1, text=True)

    assert isinstance(binary, Completed)
    assert binary.stdout == b"\xff"
    assert isinstance(text, Completed)
    assert text.stdout == "�"
    assert text.stderr == "err"


def test_spawn_failure_is_typed() -> None:
    result = run_bounded(["definitely-not-an-orcho-command"], timeout_s=0.1)
    assert isinstance(result, SpawnFailure)


def test_inherited_pipe_descendant_is_killed_within_total_budget(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    grandchild = "import time; time.sleep(30)"
    parent = (
        "import pathlib, subprocess, sys; "
        f"p=subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); print('parent-ready')"
    )
    started = time.monotonic()
    result = run_bounded([sys.executable, "-c", parent], timeout_s=0.15, reap_budget_s=0.5, text=True)
    elapsed = time.monotonic() - started

    assert isinstance(result, TimedOut)
    assert "parent-ready" in result.stdout
    assert elapsed <= 0.8
    pid = int(pid_file.read_text())
    # Probe with the platform adapter, never ``os.kill(pid, 0)``: on Windows
    # that is not a probe at all (it calls ``TerminateProcess``), and against a
    # dead pid it raises ``WinError 87`` rather than ``ProcessLookupError``.
    for _ in range(30):
        if not pid_is_alive(pid):
            break
        time.sleep(0.01)
    else:
        pytest.fail("grandchild survived bounded tree termination")


def test_timeout_reports_partial_output_and_reap_state() -> None:
    result = run_bounded(
        [sys.executable, "-c", "import sys,time; print('before'); sys.stdout.flush(); time.sleep(30)"],
        # Comfortably past interpreter startup: the assertion is that partial
        # output survives the timeout, not that a 50 ms ceiling can outrace a
        # cold ``python -c`` on a loaded machine.
        timeout_s=1.0,
        reap_budget_s=0.3,
        text=True,
    )

    assert isinstance(result, TimedOut)
    assert "before" in result.stdout
    assert result.reap_exhausted is False


def test_input_write_to_a_closed_child_is_not_fatal() -> None:
    """A child that never reads stdin must not turn into a spawn-side crash;
    the command's own outcome is the answer."""
    result = run_bounded(
        [sys.executable, "-c", "import sys; sys.stdin.close(); print('done')"],
        timeout_s=5, input_data="x" * (4 * 1024 * 1024), text=True,
    )

    assert isinstance(result, Completed)
    assert "done" in result.stdout


def test_exhausted_reap_budget_is_reported_rather_than_waited_out(monkeypatch) -> None:
    """When termination cannot settle the tree inside the reap budget, the
    result says so instead of blocking the pipeline — this is the state #250
    used to spend forever in."""
    monkeypatch.setattr("core.io.bounded_proc.terminate_tree", lambda tree, *, deadline: None)

    result = run_bounded(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        timeout_s=0.05, reap_budget_s=0.05, text=True,
    )

    assert isinstance(result, TimedOut)
    assert result.reap_exhausted is True
    assert result.returncode is None


class _Observer:
    def __init__(self, deadline: float | None = None) -> None:
        self.deadline = deadline
        self.events: list[ServiceCommandEvent] = []

    def on_start(self, event: ServiceCommandEvent) -> float | None:
        self.events.append(event)
        return self.deadline

    def on_terminal(self, event: ServiceCommandEvent) -> None:
        self.events.append(event)


def test_observer_sees_secret_free_start_and_completion() -> None:
    observer = _Observer()
    with observe_service_commands(observer):
        result = run_bounded(
            [sys.executable, "-c", "print('done')"], timeout_s=1,
            env={"SERVICE_SECRET": "must-not-appear"}, input_data="also-secret",
            text=True,
        )

    assert isinstance(result, Completed)
    assert [event.state for event in observer.events] == ["start", "completed"]
    start, terminal = observer.events
    assert start.identity.startswith(sys.executable)
    assert "must-not-appear" not in repr(start)
    assert "also-secret" not in repr(start)
    assert terminal.effective_timeout_s <= start.declared_timeout_s


def test_observer_deadline_tightens_timeout_without_changing_reap() -> None:
    started = time.monotonic()
    observer = _Observer(deadline=started + 0.04)
    with observe_service_commands(observer):
        result = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout_s=5, reap_budget_s=0.3,
        )
    elapsed = time.monotonic() - started

    assert isinstance(result, TimedOut)
    assert elapsed < 0.5
    assert [event.state for event in observer.events] == ["start", "timed_out"]
    assert observer.events[-1].effective_timeout_s < 0.1


def test_command_identity_is_bounded_without_observer() -> None:
    result = run_bounded([sys.executable, "-c", "print('ok')", "x" * 1000], timeout_s=1)

    assert isinstance(result, Completed)
