"""Durable watchdog for the single-project pre-first-phase window."""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.infra.config import AppConfig
from core.io.service_command import (
    ServiceCommandEvent,
    ServiceCommandObserver,
    observe_service_commands,
)
from core.observability import events
from pipeline.engine import save_session
from pipeline.run_state.terminal import mark_run_halted

_ACTIVE_WATCHDOG: ContextVar[StartupWatchdog | None] = ContextVar(
    "startup_watchdog", default=None,
)
_ARTIFACT_NAME = "startup_command.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class StartupWatchdog(ServiceCommandObserver):
    """Own the startup deadline, breadcrumb, and terminal halt write."""

    output_dir: Path | None
    budget_s: float = field(default_factory=lambda: float(AppConfig.load().startup_stall_seconds))
    armed: bool = False
    disarmed: bool = False
    deadline: float | None = None
    started: float | None = None
    baseline_events_size: int = 0
    baseline_output_size: int = 0
    breadcrumb: dict[str, Any] | None = None
    cause: str | None = None
    _halted: bool = False

    def arm(self) -> None:
        """Open the window immediately after the durable ``run.start`` event."""
        if self.armed or self.disarmed:
            return
        self.armed = True
        self.started = time.monotonic()
        self.deadline = self.started + self.budget_s
        self._snapshot_baselines()
        self._write_artifact()

    def mark_progress(self) -> None:
        """Restart the idle budget after engine-owned work that leaves no trace.

        The window's only ambient progress signals are ``events.jsonl`` and
        ``output.log`` growth. Setup work that emits neither (worktree
        bootstrap steps such as a dependency install) would otherwise complete
        successfully and then be retro-halted at the next checkpoint. A
        heartbeat re-snapshots the baselines and moves the deadline, but keeps
        the watchdog armed so a later hang before the first phase is still
        caught. A recorded command timeout is not cleared: it remains a halt
        cause at the next checkpoint.
        """
        if not self.armed or self.disarmed or self._halted:
            return
        self.started = time.monotonic()
        self.deadline = self.started + self.budget_s
        self._snapshot_baselines()
        self._write_artifact()

    def on_start(self, event: ServiceCommandEvent) -> float | None:
        if not self.armed or self.disarmed or self.deadline is None:
            return None
        effective = max(0.0, self.deadline - time.monotonic())
        self.breadcrumb = {
            "identity": event.identity,
            "cwd": event.cwd,
            "started_at": event.started_at,
            "declared_timeout_s": event.declared_timeout_s,
            "effective_timeout_s": effective,
        }
        self._write_artifact()
        return self.deadline

    def on_terminal(self, event: ServiceCommandEvent) -> None:
        if self.armed and not self.disarmed and event.state == "timed_out":
            self.cause = "command_timeout"

    def checkpoint(self, session: dict[str, Any] | None = None) -> bool:
        """Materialize a halt when the startup budget expired without progress."""
        if not self.armed or self.disarmed or self._halted:
            return False
        if self._has_progress():
            self.disarm()
            return False
        if self.cause is None and (self.deadline is None or time.monotonic() < self.deadline):
            return False
        if session is None:
            return False
        elapsed = max(0.0, time.monotonic() - (self.started or time.monotonic()))
        halt = {
            "phase": "startup",
            "cause": self.cause or "startup_budget_exhausted",
            "budget_s": self.budget_s,
            "elapsed_s": elapsed,
            "command": dict(self.breadcrumb) if self.breadcrumb else None,
        }
        mark_run_halted(session, halt_reason="startup_stalled", halted_at=_utc_now())
        session["halt"] = halt
        if self.output_dir is not None:
            save_session(self.output_dir, session)
            with (self.output_dir / "progress.log").open("a", encoding="utf-8") as handle:
                handle.write("[startup] halted: startup_stalled\n")
        events.emit("run.end", status="halted", halt_reason="startup_stalled")
        self._halted = True
        self.disarmed = True
        return True

    def disarm(self) -> None:
        self.disarmed = True

    def _snapshot_baselines(self) -> None:
        self.baseline_events_size = self._size("events.jsonl")
        self.baseline_output_size = self._size("output.log")

    def _write_artifact(self) -> None:
        """Persist the current window: ``armed_at`` is the start of the idle budget."""
        if self.output_dir is None:
            return
        payload: dict[str, Any] = {
            "armed_at": _utc_now(), "budget_s": self.budget_s,
            "baseline_events_size": self.baseline_events_size,
            "baseline_output_size": self.baseline_output_size,
        }
        if self.breadcrumb is not None:
            payload["command"] = self.breadcrumb
        _atomic_json(self.output_dir / _ARTIFACT_NAME, payload)

    def _size(self, name: str) -> int:
        if self.output_dir is None:
            return 0
        try:
            return (self.output_dir / name).stat().st_size
        except OSError:
            return 0

    def _has_progress(self) -> bool:
        return (
            self._size("events.jsonl") > self.baseline_events_size
            or self._size("output.log") > self.baseline_output_size
        )


@contextmanager
def startup_watchdog_scope(output_dir: Path | None) -> Iterator[StartupWatchdog]:
    """Install the startup observer for one project-run setup sequence."""
    watchdog = StartupWatchdog(output_dir)
    token: Token[StartupWatchdog | None] = _ACTIVE_WATCHDOG.set(watchdog)
    try:
        with observe_service_commands(watchdog):
            yield watchdog
    finally:
        _ACTIVE_WATCHDOG.reset(token)


def arm_startup_watchdog() -> None:
    watchdog = _ACTIVE_WATCHDOG.get()
    if watchdog is not None:
        watchdog.arm()


def checkpoint_startup_watchdog(session: dict[str, Any] | None = None) -> bool:
    watchdog = _ACTIVE_WATCHDOG.get()
    return watchdog.checkpoint(session) if watchdog is not None else False


def disarm_startup_watchdog() -> None:
    watchdog = _ACTIVE_WATCHDOG.get()
    if watchdog is not None:
        watchdog.disarm()


def heartbeat_startup_watchdog() -> None:
    """Report setup progress that writes neither an event nor output."""
    watchdog = _ACTIVE_WATCHDOG.get()
    if watchdog is not None:
        watchdog.mark_progress()


__all__ = [
    "StartupWatchdog",
    "arm_startup_watchdog",
    "checkpoint_startup_watchdog",
    "disarm_startup_watchdog",
    "heartbeat_startup_watchdog",
    "startup_watchdog_scope",
]
