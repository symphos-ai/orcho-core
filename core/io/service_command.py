"""Low-noise observation for engine-owned service commands.

The observer deliberately receives command identity rather than process inputs:
environment variables and stdin are never represented here.  It is scoped with
a :class:`contextvars.ContextVar` so a project run can observe its own startup
commands without changing unrelated subprocess callers.
"""
from __future__ import annotations

import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

_MAX_COMMAND_IDENTITY_CHARS = 512


def bounded_command_identity(args: object) -> str:
    """Return a bounded, display-safe identity for a command's argv.

    This helper intentionally accepts only ``args``.  Callers cannot
    accidentally serialize stdin or environment data into a startup artifact.
    """
    if isinstance(args, str):
        text = args
    else:
        try:
            text = shlex.join(str(part) for part in args)  # type: ignore[union-attr]
        except TypeError:
            text = str(args)
    text = text.replace("\x00", "?")
    if len(text) > _MAX_COMMAND_IDENTITY_CHARS:
        return text[: _MAX_COMMAND_IDENTITY_CHARS - 1] + "…"
    return text


def utc_now() -> str:
    """Return an unambiguous UTC instant suitable for durable breadcrumbs."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ServiceCommandEvent:
    """The deliberately small, secret-free service-command observation."""

    state: Literal["start", "completed", "timed_out", "spawn_failure"]
    identity: str
    cwd: str | None
    started_at: str
    observed_at: str
    declared_timeout_s: float
    effective_timeout_s: float


class ServiceCommandObserver(Protocol):
    """Receives startup-service lifecycle observations.

    ``on_start`` may return an absolute ``time.monotonic()`` deadline.  The
    runner only ever tightens its declared deadline with that value.
    """

    def on_start(self, event: ServiceCommandEvent) -> float | None: ...

    def on_terminal(self, event: ServiceCommandEvent) -> None: ...


service_command_observer: ContextVar[ServiceCommandObserver | None] = ContextVar(
    "service_command_observer", default=None,
)


@contextmanager
def observe_service_commands(observer: ServiceCommandObserver) -> Iterator[None]:
    """Install ``observer`` for the current context and restore it afterwards."""
    token: Token[ServiceCommandObserver | None] = service_command_observer.set(observer)
    try:
        yield
    finally:
        service_command_observer.reset(token)


__all__ = [
    "ServiceCommandEvent",
    "ServiceCommandObserver",
    "bounded_command_identity",
    "observe_service_commands",
    "service_command_observer",
    "utc_now",
]
