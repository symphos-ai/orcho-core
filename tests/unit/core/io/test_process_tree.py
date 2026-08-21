from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.io import process_tree


def test_posix_spawn_uses_new_session(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Popen:
        pid = 42

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return Popen()

    monkeypatch.setattr(process_tree.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(process_tree, "_is_windows", lambda: False)
    monkeypatch.setattr(process_tree.os, "getpgid", lambda _pid: 42)
    tree = process_tree.spawn_process(["command"])

    assert tree.platform == "posix"
    assert captured["start_new_session"] is True


def test_windows_spawn_assigns_job(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Popen:
        pid = 42

    monkeypatch.setattr(process_tree.subprocess, "Popen", lambda *a, **kw: captured.update(kw) or Popen())
    monkeypatch.setattr(process_tree, "_is_windows", lambda: True)
    monkeypatch.setattr(process_tree, "_new_windows_job", lambda process: "job")

    tree = process_tree.spawn_process(["command"])

    assert tree.job == "job"
    assert captured["creationflags"] & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)


def test_windows_taskkill_fallback_uses_remaining_budget(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []

    class Killer:
        def wait(self, *, timeout: float) -> None:
            calls.append(("wait", timeout))

        def kill(self) -> None:
            calls.append(("kill",))

    monkeypatch.setattr(process_tree.subprocess, "Popen", lambda argv, **kwargs: calls.append(tuple(argv)) or Killer())
    process_tree._taskkill(123, process_tree.time.monotonic() + 0.5)

    assert calls[0] == ("taskkill", "/PID", "123", "/T", "/F")
    assert 0 < calls[1][1] <= 0.5


def test_windows_without_job_uses_taskkill_fallback(monkeypatch) -> None:
    calls: list[tuple[int, float]] = []

    class Popen:
        pid = 77

    monkeypatch.setattr(process_tree, "_taskkill", lambda pid, deadline: calls.append((pid, deadline)))
    tree = process_tree.ProcessTree(process=Popen(), platform="windows")
    process_tree.terminate_tree(tree, deadline=123.0)

    assert calls == [(77, 123.0)]


def test_signal_process_group_keeps_graceful_and_hard_signal_shapes(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_tree.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))

    process_tree.signal_process_group(10, hard=False)
    process_tree.signal_process_group(11, hard=True)

    assert calls == [(10, 15), (11, 9)]


def test_windows_recorded_tree_uses_bounded_taskkill(monkeypatch) -> None:
    calls: list[tuple[int, float]] = []
    monkeypatch.setattr(process_tree, "_taskkill", lambda pid, deadline: calls.append((pid, deadline)))

    process_tree.terminate_recorded_tree(
        {"platform": "windows", "root_pid": 88},
        fallback_pid=77, hard=True, deadline=123.0,
    )

    assert calls == [(88, 123.0)]


def test_process_tree_is_the_only_production_process_signalling_owner() -> None:
    """One owner for every process signal, ``os.kill`` included.

    ``os.kill`` is the subtle member of this set: on Windows it terminates
    instead of signalling, so a stray "is it alive" probe elsewhere would kill
    the run it inspects. Keeping all three in one adapter is what makes that
    reviewable."""
    root = Path(__file__).parents[4]
    approved = root / "core" / "io" / "process_tree.py"
    banned = ("os.killpg", "signal.SIGKILL", "os.kill(")
    offenders = [
        path.relative_to(root)
        for directory in (root / "agents", root / "core", root / "pipeline", root / "sdk")
        for path in directory.rglob("*.py")
        if path != approved
        and any(token in path.read_text(encoding="utf-8") for token in banned)
    ]

    assert offenders == []


def test_windows_liveness_probe_never_signals_the_process(monkeypatch) -> None:
    """``os.kill`` is not a probe on Windows: every signal but the two console
    events calls ``TerminateProcess``, so probing with it would kill the run
    the caller is only inspecting."""
    monkeypatch.setattr(process_tree, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_tree.os, "kill",
        lambda *args: pytest.fail("liveness probe signalled the process"),
    )
    monkeypatch.setattr(process_tree, "_windows_pid_is_alive", lambda pid: True)

    assert process_tree.pid_is_alive(99) is True


def test_windows_liveness_probe_failure_reports_alive(monkeypatch) -> None:
    """An unusable probe must not read as "dead" — that would let a caller skip
    terminating a live tree."""
    monkeypatch.setattr(process_tree, "_is_windows", lambda: True)

    def _boom(pid: int) -> bool:
        raise OSError("no kernel32")

    monkeypatch.setattr(process_tree, "_windows_pid_is_alive", _boom)

    assert process_tree.pid_is_alive(99) is True


def test_taskkill_issues_the_kill_even_with_an_exhausted_budget(monkeypatch) -> None:
    """The deadline bounds how long we wait for taskkill, not whether the kill
    is issued at all."""
    spawned: list[tuple[str, ...]] = []

    class Killer:
        def wait(self, *, timeout: float) -> None:
            raise AssertionError("must not wait on an exhausted budget")

        def kill(self) -> None:
            raise AssertionError("must not kill the killer")

    monkeypatch.setattr(
        process_tree.subprocess, "Popen",
        lambda argv, **kwargs: spawned.append(tuple(argv)) or Killer(),
    )

    process_tree._taskkill(123, process_tree.time.monotonic() - 5.0)

    assert spawned == [("taskkill", "/PID", "123", "/T", "/F")]


def test_windows_graceful_cancel_breaks_the_group_before_any_hard_kill(monkeypatch) -> None:
    delivered: list[int] = []
    monkeypatch.setattr(process_tree, "_is_windows", lambda: True)
    monkeypatch.setattr(process_tree, "_windows_break_group", lambda pid: delivered.append(pid) or True)
    monkeypatch.setattr(
        process_tree, "_taskkill",
        lambda pid, deadline: pytest.fail("graceful must not hard-kill when the break lands"),
    )

    mode = process_tree.terminate_recorded_tree(
        {"platform": "windows", "root_pid": 42}, fallback_pid=42, hard=False, deadline=1.0,
    )

    assert (mode, delivered) == ("graceful", [42])


def test_windows_graceful_without_console_reports_hard(monkeypatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(process_tree, "_is_windows", lambda: True)
    monkeypatch.setattr(process_tree, "_windows_break_group", lambda pid: False)
    monkeypatch.setattr(process_tree, "_taskkill", lambda pid, deadline: killed.append(pid))

    mode = process_tree.terminate_recorded_tree(
        {"platform": "windows", "root_pid": 42}, fallback_pid=42, hard=False, deadline=1.0,
    )

    assert (mode, killed) == ("hard", [42])


class _FakeKernel32:
    """Minimal kernel32 stand-in: the probe must be exercised off Windows."""

    def __init__(self, *, handle: int, exit_code: int | None, query_ok: bool = True) -> None:
        self._handle = handle
        self._exit_code = exit_code
        self._query_ok = query_ok
        self.closed: list[int] = []

    def OpenProcess(self, _access: int, _inherit: bool, _pid: int) -> int:  # noqa: N802
        return self._handle

    def GetExitCodeProcess(self, _handle: int, ref) -> int:  # noqa: N802
        if not self._query_ok:
            return 0
        ref._obj.value = self._exit_code
        return 1

    def CloseHandle(self, handle: int) -> None:  # noqa: N802
        self.closed.append(handle)


def _install_fake_kernel32(monkeypatch, kernel: _FakeKernel32, *, last_error: int = 0) -> None:
    import ctypes

    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(259, True), (0, False)],
)
def test_windows_probe_reads_the_exit_code_without_touching_the_process(
    monkeypatch, exit_code: int, expected: bool,
) -> None:
    kernel = _FakeKernel32(handle=7, exit_code=exit_code)
    _install_fake_kernel32(monkeypatch, kernel)

    assert process_tree._windows_pid_is_alive(4321) is expected
    assert kernel.closed == [7]


@pytest.mark.parametrize(
    ("last_error", "expected"),
    [(5, True), (87, False)],
)
def test_windows_probe_reads_an_unopenable_process_from_the_error_code(
    monkeypatch, last_error: int, expected: bool,
) -> None:
    """Access denied means a live process owned by someone else; an invalid
    parameter means there is no such process."""
    _install_fake_kernel32(
        monkeypatch, _FakeKernel32(handle=0, exit_code=None), last_error=last_error,
    )

    assert process_tree._windows_pid_is_alive(4321) is expected


def test_windows_probe_reports_alive_when_the_query_itself_fails(monkeypatch) -> None:
    kernel = _FakeKernel32(handle=7, exit_code=None, query_ok=False)
    _install_fake_kernel32(monkeypatch, kernel)

    assert process_tree._windows_pid_is_alive(4321) is True
    assert kernel.closed == [7]


def test_pid_is_alive_rejects_non_positive_pids() -> None:
    assert process_tree.pid_is_alive(0) is False
    assert process_tree.pid_is_alive(-1) is False


@pytest.mark.parametrize(
    ("raised", "expected"),
    [(ProcessLookupError(), False), (PermissionError(), True), (None, True)],
)
def test_posix_probe_maps_kill_zero_outcomes(monkeypatch, raised, expected: bool) -> None:
    def fake_kill(_pid: int, _sig: int) -> None:
        if raised is not None:
            raise raised

    monkeypatch.setattr(process_tree, "_is_windows", lambda: False)
    monkeypatch.setattr(process_tree.os, "kill", fake_kill)

    assert process_tree.pid_is_alive(11) is expected


def test_process_is_alive_reports_a_polled_exit_and_windows_liveness(monkeypatch) -> None:
    class Exited:
        pid = 5

        def poll(self) -> int:
            return 0

    class Running:
        pid = 6

        def poll(self) -> None:
            return None

    assert process_tree.process_is_alive(Exited()) is False
    monkeypatch.setattr(process_tree, "_is_windows", lambda: True)
    assert process_tree.process_is_alive(Running()) is True


@pytest.mark.parametrize(
    ("raised", "expected"),
    [(ProcessLookupError(), False), (PermissionError(), True)],
)
def test_process_is_alive_maps_posix_probe_outcomes(monkeypatch, raised, expected: bool) -> None:
    class Running:
        pid = 6

        def poll(self) -> None:
            return None

    def fake_kill(_pid: int, _sig: int) -> None:
        raise raised

    monkeypatch.setattr(process_tree, "_is_windows", lambda: False)
    monkeypatch.setattr(process_tree.os, "kill", fake_kill)

    assert process_tree.process_is_alive(Running()) is expected


def test_windows_job_assignment_sets_kill_on_close(monkeypatch) -> None:
    """The Job Object exists so closing the handle takes the whole tree with
    it; the kill-on-close limit is the part that must not be dropped."""
    import sys
    import types

    info = {"BasicLimitInformation": {"LimitFlags": 0}}
    applied: list[object] = []

    class Handle:
        def Close(self) -> None:  # noqa: N802
            applied.append("closed")

    win32job = types.SimpleNamespace(
        CreateJobObject=lambda _a, _b: "job",
        QueryInformationJobObject=lambda _job, _cls: info,
        SetInformationJobObject=lambda _job, _cls, value: applied.append(value),
        AssignProcessToJobObject=lambda _job, _handle: applied.append("assigned"),
        JobObjectExtendedLimitInformation=9,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
    )
    monkeypatch.setitem(sys.modules, "win32job", win32job)
    monkeypatch.setitem(sys.modules, "win32api", types.SimpleNamespace(OpenProcess=lambda *a: Handle()))
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(PROCESS_ALL_ACCESS=0x1F0FFF))

    class Popen:
        pid = 42

    assert process_tree._new_windows_job(Popen()) == "job"
    assert info["BasicLimitInformation"]["LimitFlags"] & 0x2000
    assert applied[-2:] == ["assigned", "closed"]


def test_windows_job_assignment_degrades_to_none_without_pywin32() -> None:
    """A parent job can forbid assignment, and pywin32 may be absent entirely.
    Either way the taskkill fallback stays available."""

    class Popen:
        pid = 42

    assert process_tree._new_windows_job(Popen()) is None


def test_job_termination_preferred_over_taskkill(monkeypatch) -> None:
    import sys
    import types

    terminated: list[tuple[str, int]] = []
    monkeypatch.setitem(
        sys.modules, "win32job",
        types.SimpleNamespace(TerminateJobObject=lambda job, code: terminated.append((job, code))),
    )
    monkeypatch.setattr(
        process_tree, "_taskkill",
        lambda pid, deadline: pytest.fail("job termination must not fall through"),
    )

    class Popen:
        pid = 42

    process_tree.terminate_tree(
        process_tree.ProcessTree(process=Popen(), platform="windows", job="job"), deadline=1.0,
    )

    assert terminated == [("job", 1)]


def test_failed_job_termination_falls_back_to_taskkill(monkeypatch) -> None:
    import sys
    import types

    def _boom(_job: object, _code: int) -> None:
        raise RuntimeError("handle already closed")

    killed: list[int] = []
    monkeypatch.setitem(sys.modules, "win32job", types.SimpleNamespace(TerminateJobObject=_boom))
    monkeypatch.setattr(process_tree, "_taskkill", lambda pid, deadline: killed.append(pid))

    class Popen:
        pid = 42

    process_tree.terminate_tree(
        process_tree.ProcessTree(process=Popen(), platform="windows", job="job"), deadline=1.0,
    )

    assert killed == [42]


def test_posix_termination_falls_back_to_the_direct_child_without_a_group(monkeypatch) -> None:
    killed: list[str] = []

    class Popen:
        pid = 42

        def kill(self) -> None:
            killed.append("direct")

    def _no_group(_pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(process_tree.os, "getpgid", _no_group)
    monkeypatch.setattr(
        process_tree.os, "killpg",
        lambda *_args: pytest.fail("must not signal a group it could not resolve"),
    )

    process_tree.terminate_tree(
        process_tree.ProcessTree(process=Popen(), platform="posix"), deadline=1.0,
    )

    assert killed == ["direct"]


def test_posix_termination_never_signals_its_own_group(monkeypatch) -> None:
    """A pathological child sharing our group would otherwise take the
    orchestrator down with it."""
    killed: list[str] = []

    class Popen:
        pid = 42

        def kill(self) -> None:
            killed.append("direct")

    monkeypatch.setattr(process_tree.os, "getpgrp", lambda: 77)
    monkeypatch.setattr(
        process_tree.os, "killpg",
        lambda *_args: pytest.fail("must not signal our own group"),
    )

    process_tree.terminate_tree(
        process_tree.ProcessTree(process=Popen(), platform="posix", pgid=77), deadline=1.0,
    )

    assert killed == ["direct"]


def test_taskkill_survives_a_missing_binary(monkeypatch) -> None:
    def _missing(*_args, **_kwargs):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(process_tree.subprocess, "Popen", _missing)

    process_tree._taskkill(1, process_tree.time.monotonic() + 1.0)


def test_taskkill_kills_a_hung_killer(monkeypatch) -> None:
    events: list[str] = []

    class Killer:
        def wait(self, *, timeout: float) -> None:
            raise subprocess.TimeoutExpired("taskkill", timeout)

        def kill(self) -> None:
            events.append("killed")

    monkeypatch.setattr(process_tree.subprocess, "Popen", lambda *a, **kw: Killer())

    process_tree._taskkill(1, process_tree.time.monotonic() + 1.0)

    assert events == ["killed"]


def test_break_group_delivers_the_console_event(monkeypatch) -> None:
    import signal

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)
    monkeypatch.setattr(process_tree.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    assert process_tree._windows_break_group(42) is True
    assert sent == [(42, 1)]


def test_break_group_reports_failure_when_no_console_is_shared(monkeypatch) -> None:
    def _no_console(_pid: int, _sig: int) -> None:
        raise OSError("no console")

    import signal

    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)
    monkeypatch.setattr(process_tree.os, "kill", _no_console)

    assert process_tree._windows_break_group(42) is False


def test_posix_recorded_tree_signals_the_recorded_group(monkeypatch) -> None:
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(process_tree.os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))

    mode = process_tree.terminate_recorded_tree(
        {"platform": "posix", "root_pid": 9, "group_id": 9},
        fallback_pid=1, hard=False, deadline=1.0,
    )

    assert (mode, signalled) == ("graceful", [(9, 15)])


def test_windows_descriptor_and_spawn_flags(monkeypatch) -> None:
    monkeypatch.setattr(process_tree, "_is_windows", lambda: True)

    assert process_tree.detached_tree_descriptor(42) == {
        "platform": "windows", "root_pid": 42, "group_id": 42, "group_owned": True,
    }
    assert "creationflags" in process_tree.detached_spawn_kwargs()


def test_spawn_survives_a_child_reaped_before_the_group_lookup(monkeypatch) -> None:
    class Popen:
        pid = 42

    def _gone(_pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(process_tree.subprocess, "Popen", lambda *a, **kw: Popen())
    monkeypatch.setattr(process_tree, "_is_windows", lambda: False)
    monkeypatch.setattr(process_tree.os, "getpgid", _gone)

    assert process_tree.spawn_process(["command"]).pgid is None


def test_process_is_alive_reports_a_live_posix_child(monkeypatch) -> None:
    class Running:
        pid = 6

        def poll(self) -> None:
            return None

    monkeypatch.setattr(process_tree, "_is_windows", lambda: False)
    monkeypatch.setattr(process_tree.os, "kill", lambda _pid, _sig: None)

    assert process_tree.process_is_alive(Running()) is True
