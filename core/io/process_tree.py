"""Platform-owned process-tree creation and termination.

This is deliberately the only production module which knows about POSIX
process groups and Windows Job Objects.  Callers retain the returned
``ProcessTree`` until the child has reached a terminal state.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProcessTree:
    """A child and the platform resource which owns its descendants."""

    process: subprocess.Popen[Any]
    platform: str
    job: Any | None = None
    pgid: int | None = None

    @property
    def pid(self) -> int:
        return self.process.pid


def _is_windows() -> bool:
    return sys.platform == "win32"


def detached_spawn_kwargs() -> dict[str, bool | int]:
    """Platform launch flags for a detached, independently owned child."""
    if _is_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def detached_tree_descriptor(pid: int) -> dict[str, str | int | bool]:
    """Durable ownership facts for a detached child, safe to JSON encode."""
    return {
        "platform": "windows" if _is_windows() else "posix",
        "root_pid": pid,
        "group_id": pid,
        "group_owned": True,
    }


def _windows_pid_is_alive(pid: int) -> bool:
    """Query one PID without touching it.

    ``os.kill`` is not a probe on Windows: for every signal except
    ``CTRL_C_EVENT`` / ``CTRL_BREAK_EVENT`` CPython calls ``TerminateProcess``,
    so ``os.kill(pid, 0)`` would *kill* the process it claims to inspect.
    ``OpenProcess`` + ``GetExitCodeProcess`` is the read-only equivalent.
    A process that exited with code 259 is indistinguishable from a running
    one; reporting it alive keeps the caller on the terminate path, which is
    the harmless direction.
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # A live process owned by another user denies the query; a dead one
        # reports an invalid parameter.
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def pid_is_alive(pid: int) -> bool:
    """Cross-platform, race-safe, side-effect-free liveness probe for one PID."""
    if pid <= 0:
        return False
    if _is_windows():
        try:
            return _windows_pid_is_alive(pid)
        except (OSError, AttributeError, ValueError):
            # An unusable probe must not be read as "dead" — that would let a
            # caller skip terminating a live tree.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _new_windows_job(process: subprocess.Popen[Any]) -> Any | None:
    """Assign ``process`` to a kill-on-close Job Object when pywin32 exists."""
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32job  # type: ignore[import-not-found]

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation,
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info,
        )
        handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception:
        # Job assignment can be forbidden by a parent job.  The bounded
        # taskkill fallback below remains confined to this recorded PID.
        return None


def spawn_process(*args: Any, **kwargs: Any) -> ProcessTree:
    """Start a child in an independently owned tree without changing argv."""
    if _is_windows():
        flags = int(kwargs.pop("creationflags", 0))
        kwargs["creationflags"] = flags | int(detached_spawn_kwargs()["creationflags"])
    else:
        kwargs.setdefault("start_new_session", detached_spawn_kwargs()["start_new_session"])
    process = subprocess.Popen(*args, **kwargs)
    job = _new_windows_job(process) if _is_windows() else None
    try:
        pgid = os.getpgid(process.pid) if not _is_windows() else None
    except OSError:
        # A very short-lived direct child can be reaped before this lookup.
        # It has no durable group descriptor, but must not turn spawn success
        # into an exception.
        pgid = None
    return ProcessTree(
        process=process, platform="windows" if _is_windows() else "posix", job=job, pgid=pgid,
    )


def process_is_alive(process: subprocess.Popen[Any]) -> bool:
    """Race-safe liveness check; a vanished PID is simply not alive."""
    if process.poll() is not None:
        return False
    if _is_windows():
        return True
    try:
        os.kill(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _terminate_posix(tree: ProcessTree) -> None:
    process = tree.process
    # The direct child may already have exited while a descendant still owns
    # its pipes.  Its process group can nevertheless still exist, so do not
    # use ``process.poll`` as permission to skip the group-wide kill.
    try:
        group = tree.pgid if tree.pgid is not None else os.getpgid(process.pid)
        # Never signal our own group if a caller supplied a pathological child.
        if group != os.getpgrp():
            os.killpg(group, 9)
            return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    with suppress(ProcessLookupError, OSError):
        process.kill()


def _taskkill(pid: int, deadline: float) -> None:
    """Kill a Windows tree, spending only the caller's remaining budget."""
    try:
        killer = subprocess.Popen(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, OSError):
        return
    # The kill is issued by the spawn itself; the deadline bounds only how long
    # this thread waits for taskkill to report back. An exhausted budget must
    # never turn termination into a no-op.
    remaining = _remaining(deadline)
    if remaining <= 0:
        return
    try:
        killer.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        killer.kill()


def terminate_tree(tree: ProcessTree, *, deadline: float) -> None:
    """Request hard termination without extending ``deadline``.

    A Job Object owns the entire Windows tree.  If it is unavailable or
    termination fails, taskkill is bounded by the same absolute deadline.
    """
    if tree.platform == "posix":
        _terminate_posix(tree)
        return
    if tree.job is not None:
        try:
            import win32job  # type: ignore[import-not-found]
            win32job.TerminateJobObject(tree.job, 1)
            return
        except Exception:
            pass
    _taskkill(tree.pid, deadline)


def signal_process_group(pgid: int, *, hard: bool) -> None:
    """Signal one recorded POSIX group; callers retain their own policy."""
    os.killpg(pgid, 9 if hard else 15)


def _windows_break_group(pid: int) -> bool:
    """Ask a ``CREATE_NEW_PROCESS_GROUP`` tree to stop; True when delivered.

    ``CTRL_BREAK_EVENT`` is one of the two signals Windows actually delivers
    instead of terminating, so it is the only honest counterpart to POSIX
    ``SIGTERM``. It needs a shared console, which a detached supervisor may
    not have — hence the boolean, so the caller can report what it really did.
    """
    import signal as _signal
    try:
        os.kill(pid, _signal.CTRL_BREAK_EVENT)
    except (OSError, AttributeError, ValueError):
        return False
    return True


def terminate_recorded_tree(
    descriptor: object, *, fallback_pid: int, hard: bool, deadline: float,
) -> str:
    """Terminate the recorded detached tree; return the mode actually used.

    The return value exists because Windows cannot always honour a graceful
    request: when no console is shared, the only remaining move is a hard tree
    kill, and the caller must report *that* rather than claim a graceful stop
    the pipeline never received.
    """
    details = descriptor if isinstance(descriptor, dict) else {}
    platform = details.get("platform")
    root_pid = details.get("root_pid")
    pid = root_pid if isinstance(root_pid, int) and root_pid > 0 else fallback_pid
    if platform == "windows" or (platform not in {"posix", "windows"} and _is_windows()):
        if not hard and _windows_break_group(pid):
            return "graceful"
        _taskkill(pid, deadline)
        return "hard"
    group = details.get("group_id")
    signal_process_group(group if isinstance(group, int) and group > 0 else pid, hard=hard)
    return "hard" if hard else "graceful"


__all__ = [
    "ProcessTree", "detached_spawn_kwargs", "detached_tree_descriptor", "pid_is_alive",
    "process_is_alive", "signal_process_group", "spawn_process", "terminate_recorded_tree",
    "terminate_tree",
]
