"""
pipeline/sandbox/context.py — active-policy ContextVar (ADR 0034).

Mirrors :mod:`pipeline.engine.worktree`'s ``_active_checkout``
contract: the orchestrator sets the resolved policy once per run,
the agent runtimes read it at dispatch, the cleanup path resets
via the stored ``Token``. ContextVar gives per-thread / per-async-
context isolation so concurrent sub-runs (DAG-2 future) each see
their own value without global lock contention.

Kept in its own module (not in ``policy.py``) so ``policy.py``
stays pure-data and importable from anywhere without contextvars
state side effects.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

from pipeline.sandbox.policy import SandboxPolicy

_active_policy: ContextVar[SandboxPolicy | None] = ContextVar(
    "_orcho_active_sandbox_policy", default=None
)


def set_active_sandbox_policy(policy: SandboxPolicy | None) -> Token[SandboxPolicy | None]:
    """Record the active sandbox policy for this execution context.

    Call from the orchestrator after the resolver returns. Pass the
    returned token to :func:`reset_active_sandbox_policy` in the
    run's cleanup / ``finalize()`` path to avoid leaking across
    runs in the same thread (important for tests).
    """
    return _active_policy.set(policy)


def reset_active_sandbox_policy(token: Token[SandboxPolicy | None]) -> None:
    """Reset the ContextVar to its pre-run state using the stored token."""
    _active_policy.reset(token)


def get_active_sandbox_policy() -> SandboxPolicy | None:
    """Return the active sandbox policy, or ``None`` if no run is in scope.

    Agent runtimes call this at the top of ``invoke`` and pass the
    result through to :func:`agents.stream._stream_run`. ``None``
    preserves the pre-L1 behaviour verbatim — no env filtering,
    no rlimit, no masking. The orchestrator always sets a policy
    on real runs, so ``None`` appears only in direct unit tests of
    the runtime that bypass the orchestrator entrypoint.
    """
    return _active_policy.get()


__all__ = [
    "get_active_sandbox_policy",
    "reset_active_sandbox_policy",
    "set_active_sandbox_policy",
]
