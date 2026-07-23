from __future__ import annotations

import subprocess

from agents.owned_child import OwnedChildRegistry, OwnedChildState


class FakePopen:
    pid = 4815

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.poll_calls = 0
        self.wait_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        self.poll_calls += 1
        return self.returncode

    def wait(self, *, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1


def test_registered_fake_child_moves_from_running_to_memoized_exit() -> None:
    proc = FakePopen()
    registry = OwnedChildRegistry()
    handle = registry.register(proc)  # type: ignore[arg-type]

    assert registry.poll(handle).state is OwnedChildState.RUNNING
    proc.returncode = 17
    assert registry.poll(handle).exit_code == 17
    assert registry.wait(handle, timeout=0).exit_code == 17
    assert proc.poll_calls == 2
    assert proc.wait_calls == 0


def test_bounded_wait_times_out_without_sleeping() -> None:
    proc = FakePopen()
    handle = OwnedChildRegistry().register(proc)  # type: ignore[arg-type]

    observation = OwnedChildRegistry().wait(handle, timeout=0.01)
    assert observation.state is OwnedChildState.UNAVAILABLE

    registry = OwnedChildRegistry()
    handle = registry.register(proc)  # type: ignore[arg-type]
    assert registry.wait(handle, timeout=0.01).state is OwnedChildState.RUNNING
    assert proc.wait_calls == 1


def test_cancel_uses_pid_when_child_group_is_our_parent_group(monkeypatch) -> None:
    proc = FakePopen()
    registry = OwnedChildRegistry()
    handle = registry.register(proc, group_owned=True)  # type: ignore[arg-type]
    monkeypatch.setattr("agents.owned_child.os.getpgid", lambda _pid: 9)
    monkeypatch.setattr("agents.owned_child.os.getpgrp", lambda: 9)
    killpg_calls: list[int] = []
    monkeypatch.setattr(
        "agents.owned_child.os.killpg", lambda pgid, _signal: killpg_calls.append(pgid),
    )

    registry.cancel(handle)

    assert proc.kill_calls == 1
    assert killpg_calls == []


def test_cancel_uses_confirmed_distinct_owned_group(monkeypatch) -> None:
    proc = FakePopen()
    registry = OwnedChildRegistry()
    handle = registry.register(proc, group_owned=True)  # type: ignore[arg-type]
    monkeypatch.setattr("agents.owned_child.os.getpgid", lambda _pid: 12)
    monkeypatch.setattr("agents.owned_child.os.getpgrp", lambda: 9)
    killpg_calls: list[int] = []
    monkeypatch.setattr(
        "agents.owned_child.os.killpg", lambda pgid, _signal: killpg_calls.append(pgid),
    )

    registry.cancel(handle)

    assert killpg_calls == [12]
    assert proc.kill_calls == 0
