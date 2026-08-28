"""A launched run must survive its launcher's process tree (J1).

On Windows ``CREATE_NEW_PROCESS_GROUP`` only reroutes console control
events; it leaves the child inside the launcher's Job Object. When a client
supervises the launcher inside a kill-on-close job, killing or restarting
that launcher makes the kernel terminate every "detached" run with it — no
traceback, no atexit, no bytes in the run log, and nothing written down
that the run ever ended.

These tests pin the flag composition and the fallback contract: the
breakaway flag fails the spawn when the launcher's job forbids it, so a
refused breakaway must degrade to the old behaviour rather than refuse to
start the run.
"""
from __future__ import annotations

import subprocess
from typing import Any

import pytest

from core.io import process_tree

_CREATE_NEW_PROCESS_GROUP = 0x200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def test_windows_breakaway_flags_add_to_the_detached_group_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_tree, "_is_windows", lambda: True)

    flags = int(process_tree.breakaway_spawn_kwargs()["creationflags"])

    assert flags & _CREATE_NEW_PROCESS_GROUP
    assert flags & _CREATE_BREAKAWAY_FROM_JOB


def test_posix_breakaway_is_the_plain_detached_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A POSIX session has no job containment to escape."""
    monkeypatch.setattr(process_tree, "_is_windows", lambda: False)

    assert process_tree.breakaway_spawn_kwargs() == process_tree.detached_spawn_kwargs()
    assert process_tree.breakaway_spawn_kwargs() == {"start_new_session": True}


def _record_spawns(monkeypatch: pytest.MonkeyPatch, *, refuse_first: bool) -> list[dict]:
    seen: list[dict] = []

    def _popen(cmd: Any, **kwargs: Any) -> str:
        seen.append(kwargs)
        if refuse_first and len(seen) == 1:
            raise PermissionError(5, "Access is denied")
        return "popen"

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return seen


def test_launch_asks_for_breakaway_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from sdk.run_control import launch as launch_mod

    monkeypatch.setattr(launch_mod, "breakaway_spawn_kwargs", lambda: {"creationflags": 9})
    monkeypatch.setattr(launch_mod, "detached_spawn_kwargs", lambda: {"creationflags": 1})
    seen = _record_spawns(monkeypatch, refuse_first=False)

    launch_mod._spawn_detached(["x"], project_dir=".", env={}, log_fd=None)

    assert [kwargs["creationflags"] for kwargs in seen] == [9]


def test_a_job_that_forbids_breakaway_still_starts_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing to start is worse than starting inside the launcher's job."""
    from sdk.run_control import launch as launch_mod

    monkeypatch.setattr(launch_mod, "breakaway_spawn_kwargs", lambda: {"creationflags": 9})
    monkeypatch.setattr(launch_mod, "detached_spawn_kwargs", lambda: {"creationflags": 1})
    seen = _record_spawns(monkeypatch, refuse_first=True)

    result = launch_mod._spawn_detached(["x"], project_dir=".", env={}, log_fd=None)

    assert result == "popen"
    assert [kwargs["creationflags"] for kwargs in seen] == [9, 1]


def test_identical_flag_sets_are_attempted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX must not pay for a retry that cannot differ."""
    from sdk.errors import LaunchError
    from sdk.run_control import launch as launch_mod

    monkeypatch.setattr(
        launch_mod, "breakaway_spawn_kwargs", lambda: {"start_new_session": True},
    )
    monkeypatch.setattr(
        launch_mod, "detached_spawn_kwargs", lambda: {"start_new_session": True},
    )
    seen = _record_spawns(monkeypatch, refuse_first=True)

    with pytest.raises(LaunchError):
        launch_mod._spawn_detached(["x"], project_dir=".", env={}, log_fd=None)

    assert len(seen) == 1
