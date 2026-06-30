"""Process-tree lifetime contracts for sandbox-managed agent spawns.

Three invariants from ADR 0034 that the streamer + launcher have to
preserve together:

* When the sandbox places the child in its own process group, killing
  the streamer (timeout / idle / abort) must take the entire subtree
  down with it. A surviving grandchild is a hole in the sandbox.
* :func:`_spawn_with_sandbox` returns the launcher so the caller can
  pin its lifetime to the live subprocess. On Windows the launcher
  owns the Job Object handle; losing the reference closes the job
  mid-run and kills the assigned child.
* ``mode=off`` short-circuits to the legacy spawn path verbatim —
  no launcher, no masker, no process-group setup, no env filtering.
  Tests that explicitly opt out of L1 must not regress.
"""
from __future__ import annotations

import contextlib
import os
import platform
import pty
import sys
import time
from pathlib import Path

import pytest

from agents.stream import _spawn_with_sandbox, _stream_run
from pipeline.sandbox.policy import SandboxLimits, SandboxMode, SandboxPolicy


@pytest.mark.skipif(
    platform.system().lower() == "windows",
    reason="POSIX process-group kill semantics are tested separately on Windows via Job Object",
)
class TestProcessGroupCleanupOnUnix:
    """Watchdog-triggered termination has to reach the whole process group.

    Without process-group SIGKILL, an agent that forks a long-lived
    helper would leave the helper alive past the parent's death —
    contradicting the ADR 0034 commitment that the agent "and its
    grandchildren die" when the streamer aborts.
    """

    def test_grandchild_terminates_when_streamer_hits_idle_timeout(
        self, tmp_path: Path,
    ) -> None:
        marker = tmp_path / "grandchild.marker"
        gc_script = tmp_path / "grandchild.py"
        parent_script = tmp_path / "parent.py"
        # Grandchild waits long enough that the watchdog has plenty
        # of time to fire, then writes a marker. If the kill tears
        # down the whole process group, this write never lands.
        gc_script.write_text(
            "import pathlib, time\n"
            "time.sleep(4)\n"
            f"pathlib.Path({str(marker)!r}).write_text('alive')\n",
            encoding="utf-8",
        )
        # Parent spawns the grandchild and then idles silently so
        # the streamer's idle watchdog catches it.
        parent_script.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(gc_script)!r}])\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        policy = SandboxPolicy(mode=SandboxMode.ENV)
        _stdout, _rc, stderr, _dur = _stream_run(
            [sys.executable, str(parent_script)],
            idle_timeout=1,
            sandbox_policy=policy,
        )
        assert "IDLE TIMEOUT" in stderr
        # Give the grandchild's would-be write its chance to land.
        time.sleep(5)
        assert not marker.exists(), (
            "grandchild survived parent's idle-timeout kill — "
            "process-group SIGKILL did not propagate"
        )

    def test_unsandboxed_path_terminates_immediate_child_only(self) -> None:
        """``sandbox_policy=None`` keeps pre-L1 behaviour: the
        immediate child still dies via ``proc.kill()``. Grandchild
        cleanup is not promised on this path because no
        ``setpgrp`` was applied — there is no group to kill."""
        _stdout, _rc, stderr, _dur = _stream_run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            idle_timeout=1,
            sandbox_policy=None,
        )
        assert "IDLE TIMEOUT" in stderr


class TestLauncherLifetimePin:
    """``_spawn_with_sandbox`` must hand the launcher back so the
    caller can hold it alive for the duration of ``proc``. On
    Windows this protects the Job Object handle from premature GC;
    on Unix it is a no-op reference but the contract is uniform
    so a future refactor doesn't silently drop the Windows case."""

    def test_active_policy_yields_a_live_launcher(self) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            policy = SandboxPolicy(mode=SandboxMode.ENV)
            proc, masker, stripped, launcher = _spawn_with_sandbox(
                [sys.executable, "-c", "pass"],
                cwd=None, slave_fd=slave_fd, sandbox_policy=policy,
            )
            os.close(slave_fd)
            try:
                assert launcher is not None
                assert masker is not None
                assert stripped >= 0
                proc.wait(timeout=5)
            finally:
                with contextlib.suppress(OSError):
                    proc.kill()
        finally:
            with contextlib.suppress(OSError):
                os.close(master_fd)

    def test_off_mode_yields_no_launcher(self) -> None:
        """``mode=off`` short-circuits to the legacy spawn path; the
        returned launcher is ``None`` because there is no sandbox
        state to keep alive."""
        master_fd, slave_fd = pty.openpty()
        try:
            policy = SandboxPolicy(mode=SandboxMode.OFF)
            proc, masker, stripped, launcher = _spawn_with_sandbox(
                [sys.executable, "-c", "pass"],
                cwd=None, slave_fd=slave_fd, sandbox_policy=policy,
            )
            os.close(slave_fd)
            try:
                assert launcher is None
                assert masker is None
                assert stripped == 0
                proc.wait(timeout=5)
            finally:
                with contextlib.suppress(OSError):
                    proc.kill()
        finally:
            with contextlib.suppress(OSError):
                os.close(master_fd)


@pytest.mark.skipif(
    platform.system().lower() == "windows",
    reason="killpg fall-back guard is a POSIX-only concern",
)
class TestKillpgGuardWhenSetpgrpFailed:
    """If ``setpgrp`` silently fails inside ``preexec_fn``, the child
    inherits the orcho parent's process group. Sending SIGKILL to
    that group would terminate the orchestrator itself — a fatal
    self-DoS. The streamer must compare the child's effective pgid
    with its own before issuing ``killpg``, and fall back to a
    single-PID kill when they match."""

    def test_killpg_skipped_when_child_pgid_matches_parent(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agents import stream as stream_mod

        kill_calls: list[str] = []

        class _FakeProc:
            pid = 12345

            def kill(self) -> None:
                kill_calls.append("proc.kill")

        # Both ``getpgid(child)`` and ``getpgrp()`` return the same
        # value — modelling the world where setpgrp inside preexec
        # quietly failed.
        monkeypatch.setattr(stream_mod.os, "getpgid", lambda _pid: 42)
        monkeypatch.setattr(stream_mod.os, "getpgrp", lambda: 42)

        def _killpg_should_not_run(_pgid: int, _sig: int) -> None:
            kill_calls.append("killpg")
        monkeypatch.setattr(stream_mod.os, "killpg", _killpg_should_not_run)

        stream_mod._kill_subprocess_tree(_FakeProc(), group_owned=True)

        assert kill_calls == ["proc.kill"], (
            "killpg ran despite the child sharing the parent's process "
            "group — orchestrator self-DoS risk"
        )

    def test_killpg_runs_when_child_has_its_own_group(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agents import stream as stream_mod

        kill_calls: list[str] = []

        class _FakeProc:
            pid = 12345

            def kill(self) -> None:
                kill_calls.append("proc.kill")

        # Distinct values — setpgrp succeeded, killpg is safe.
        monkeypatch.setattr(stream_mod.os, "getpgid", lambda _pid: 99)
        monkeypatch.setattr(stream_mod.os, "getpgrp", lambda: 42)

        def _fake_killpg(_pgid: int, _sig: int) -> None:
            kill_calls.append("killpg")
        monkeypatch.setattr(stream_mod.os, "killpg", _fake_killpg)

        stream_mod._kill_subprocess_tree(_FakeProc(), group_owned=True)

        assert kill_calls == ["killpg"]


class TestSpawnCompletion:
    """End-to-end: under an active sandbox policy, a well-behaved
    child runs to completion and the streamer returns its output.
    This is the structural counterpart to the kill tests — it
    asserts that holding the launcher alive does not deadlock the
    completion path."""

    def test_well_behaved_child_returns_output(self) -> None:
        policy = SandboxPolicy(
            mode=SandboxMode.ENV,
            limits=SandboxLimits(cpu_seconds=30),
        )
        stdout, rc, _stderr, _dur = _stream_run(
            [sys.executable, "-c", "print('ok')"],
            sandbox_policy=policy,
        )
        assert rc == 0
        assert "ok" in stdout
