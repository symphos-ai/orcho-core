"""
pipeline/sandbox/backends/null_backend.py — no-op launcher for mode=off.

Selected when an operator deliberately disables sandboxing or when
the resolver degrades to ``off`` because no backend is available.
Returns the parent env verbatim, no preexec_fn, no post-spawn
hook — :func:`agents.stream._stream_run` then behaves exactly as
it did before ADR 0034 landed.
"""
from __future__ import annotations

from pipeline.sandbox.launcher import PreparedLaunch, SandboxLauncher
from pipeline.sandbox.policy import SandboxPolicy


class NullLauncher(SandboxLauncher):
    """Pass-through launcher.

    Does not consult ``policy.env_allowlist`` — ``mode=off`` is the
    explicit "no isolation" mode and stripping env would surprise an
    operator who chose ``off`` because their agent needs the
    unfiltered environment.
    """

    __slots__ = ("_policy",)

    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy

    def prepare(
        self,
        *,
        cmd: list[str],
        cwd: str | None,  # noqa: ARG002 — interface uniformity
        parent_env: dict[str, str],
    ) -> PreparedLaunch:
        return PreparedLaunch(
            cmd=list(cmd),
            env=dict(parent_env),
            preexec_fn=None,
            creationflags=0,
            post_spawn=None,
            policy=self._policy,
            env_stripped_count=0,
        )
