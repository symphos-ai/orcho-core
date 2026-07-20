"""In-memory lifecycle ownership for one exact spawned child process.

The public handle is deliberately safe to retain in runtime state: it contains
only an opaque registration id and the child pid.  The ``Popen`` object,
sandbox launcher (including a Windows Job Object), and process-group ownership
fact remain private to :class:`OwnedChildRegistry`.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class OwnedChildState(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class OwnedChildHandle:
    """An opaque reference to a child owned by one registry."""

    registration_id: str
    pid: int
    start_identity: str | None = None


@dataclass(frozen=True, slots=True)
class OwnedChildObservation:
    state: OwnedChildState
    exit_code: int | None = None


@dataclass(slots=True)
class _OwnedChild:
    proc: Any
    group_owned: bool
    launcher: object | None
    exit_code: int | None = None


class OwnedChildRegistry:
    """Own and observe spawned children without host-wide process discovery."""

    def __init__(self) -> None:
        self._children: dict[str, _OwnedChild] = {}
        self._last_handle: OwnedChildHandle | None = None

    def register(
        self,
        proc: subprocess.Popen[Any],
        *,
        group_owned: bool = False,
        launcher: object | None = None,
        start_identity: str | None = None,
    ) -> OwnedChildHandle:
        """Register exactly ``proc`` and pin any launcher until settlement."""
        registration_id = uuid.uuid4().hex
        self._children[registration_id] = _OwnedChild(
            proc=proc, group_owned=group_owned, launcher=launcher,
        )
        handle = OwnedChildHandle(
            registration_id=registration_id,
            pid=proc.pid,
            start_identity=start_identity,
        )
        self._last_handle = handle
        return handle

    @property
    def last_handle(self) -> OwnedChildHandle | None:
        """The latest exact child registered by this runtime invocation."""
        return self._last_handle

    def poll(self, handle: OwnedChildHandle) -> OwnedChildObservation:
        """Observe without blocking; unknown or broken ownership fails closed."""
        child = self._lookup(handle)
        if child is None:
            return OwnedChildObservation(OwnedChildState.UNAVAILABLE)
        if child.exit_code is not None:
            return OwnedChildObservation(OwnedChildState.EXITED, child.exit_code)
        try:
            code = child.proc.poll()
        except Exception:
            return OwnedChildObservation(OwnedChildState.UNAVAILABLE)
        if code is None:
            return OwnedChildObservation(OwnedChildState.RUNNING)
        return self._settle(child, code)

    def wait(
        self, handle: OwnedChildHandle, *, timeout: float | None,
    ) -> OwnedChildObservation:
        """Wait once for this owned child, with an explicit bounded timeout."""
        child = self._lookup(handle)
        if child is None:
            return OwnedChildObservation(OwnedChildState.UNAVAILABLE)
        if child.exit_code is not None:
            return OwnedChildObservation(OwnedChildState.EXITED, child.exit_code)
        try:
            code = child.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return OwnedChildObservation(OwnedChildState.RUNNING)
        except Exception:
            return OwnedChildObservation(OwnedChildState.UNAVAILABLE)
        return self._settle(child, code)

    def cancel(self, handle: OwnedChildHandle) -> OwnedChildObservation:
        """Signal only the exact registered child or its confirmed own group."""
        child = self._lookup(handle)
        if child is None:
            return OwnedChildObservation(OwnedChildState.UNAVAILABLE)
        observed = self.poll(handle)
        if observed.state is not OwnedChildState.RUNNING:
            return observed

        if child.group_owned:
            try:
                pgid = os.getpgid(handle.pid)
                if pgid != os.getpgrp():
                    os.killpg(pgid, signal.SIGKILL)
                    return self.poll(handle)
            except OSError:
                # A missing group may race with exit; first check, then use the
                # exact-Popen fallback if it is still alive.
                observed = self.poll(handle)
                if observed.state is not OwnedChildState.RUNNING:
                    return observed
        with contextlib.suppress(OSError):
            child.proc.kill()
        return self.poll(handle)

    def process_group(self, handle: OwnedChildHandle) -> int | None:
        """Return only this child's known group, never a discovered process."""
        child = self._lookup(handle)
        if child is None or not child.group_owned:
            return None
        if self.poll(handle).state is not OwnedChildState.RUNNING:
            return None
        try:
            return os.getpgid(handle.pid)
        except OSError:
            return None

    def _lookup(self, handle: OwnedChildHandle) -> _OwnedChild | None:
        child = self._children.get(handle.registration_id)
        if child is None or getattr(child.proc, "pid", None) != handle.pid:
            return None
        return child

    @staticmethod
    def _settle(child: _OwnedChild, code: int) -> OwnedChildObservation:
        # ``Popen.poll`` and ``Popen.wait`` both reap. Record the terminal
        # result once so later observations never touch the process again.
        if child.exit_code is None:
            child.exit_code = code
            child.launcher = None
        return OwnedChildObservation(OwnedChildState.EXITED, child.exit_code)
