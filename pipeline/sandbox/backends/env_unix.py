"""
pipeline/sandbox/backends/env_unix.py — L1 backend for Linux + macOS.

Filters the child env via the allowlist + denylist, sets resource
limits with :func:`resource.setrlimit`, places the child in its
own process group so a parent-side abort can kill the whole
subtree with a single signal, and (Linux only) requests
``SIGTERM`` on parent death via ``prctl(PR_SET_PDEATHSIG)``.

All work happens in a ``preexec_fn`` so the child is locked down
before ``exec()`` returns control to the agent CLI. The function
is intentionally tiny and dependency-free so it survives the
fork/exec boundary without import-time surprises.
"""
from __future__ import annotations

import contextlib
import os
import platform
import resource

from pipeline.sandbox.backends._env_filter import compute_child_env
from pipeline.sandbox.launcher import PreparedLaunch, SandboxLauncher
from pipeline.sandbox.policy import SandboxLimits, SandboxPolicy

_MB = 1024 * 1024
_LINUX = platform.system().lower() == "linux"


def _build_preexec(limits: SandboxLimits) -> callable:
    """Return a closure that applies limits + process-group + pdeathsig.

    Captured by value so the closure has no live reference to the
    policy object — the child must not need any post-fork imports.
    Returns ``None`` if there is nothing to do (off + no limits +
    Darwin where pdeathsig is unavailable). The launcher checks
    for ``None`` to drop ``preexec_fn`` entirely, which slightly
    speeds the common-case spawn.
    """
    cpu = limits.cpu_seconds
    rss_bytes = limits.memory_mb * _MB if limits.memory_mb > 0 else 0
    nofile = limits.open_files
    fsize_bytes = limits.file_size_mb * _MB if limits.file_size_mb > 0 else 0

    has_limits = any(v > 0 for v in (cpu, rss_bytes, nofile, fsize_bytes))

    def _apply() -> None:
        # 1. New process group so a parent-side kill can take the
        #    whole subtree with one ``os.killpg(pgid, SIGKILL)``.
        # setpgrp can fail when the child is already a process group
        # leader (rare under PTY); ignore and continue rather than
        # aborting the spawn.
        with contextlib.suppress(OSError):
            os.setpgrp()

        # 2. Linux-only: ask the kernel to send SIGTERM to this
        #    process when the parent dies. Belt-and-braces on top
        #    of the process-group kill — protects against orcho
        #    crashing without graceful cleanup.
        if _LINUX:
            try:
                import ctypes
                import ctypes.util
                libc_name = ctypes.util.find_library("c")
                if libc_name:
                    libc = ctypes.CDLL(libc_name, use_errno=True)
                    # PR_SET_PDEATHSIG = 1, SIGTERM = 15
                    libc.prctl(1, 15, 0, 0, 0)
            except OSError:
                # ctypes/prctl failure is non-fatal — pgkill on
                # the parent side still works.
                pass

        # 3. Resource limits. Each setrlimit is best-effort: if
        #    the host policy rejects the value (e.g. unprivileged
        #    user can't raise a hard limit), we don't fail the
        #    spawn — the child runs without that cap and the
        #    operator sees the requested vs effective value via
        #    standard tooling.
        if not has_limits:
            return

        for rlim, value in (
            (resource.RLIMIT_CPU, cpu),
            (resource.RLIMIT_AS, rss_bytes),
            (resource.RLIMIT_NOFILE, nofile),
            (resource.RLIMIT_FSIZE, fsize_bytes),
        ):
            if value <= 0:
                continue
            try:
                resource.setrlimit(rlim, (value, value))
            except (OSError, ValueError):
                # Unsupported limit on this platform (e.g.
                # RLIMIT_AS on some Darwin versions) or value
                # exceeds the hard cap. Continue with the rest.
                continue

    return _apply


class EnvUnixLauncher(SandboxLauncher):
    """L1 launcher for Linux and macOS.

    All policy work happens at :meth:`prepare` time except the
    resource limits, which must apply post-fork — those are bundled
    into the returned ``preexec_fn``.
    """

    __slots__ = ("_policy",)

    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy

    def prepare(
        self,
        *,
        cmd: list[str],
        cwd: str | None,  # noqa: ARG002 — Unix backend ignores cwd; FS isolation lands with L2
        parent_env: dict[str, str],
    ) -> PreparedLaunch:
        child_env, stripped = compute_child_env(parent_env, self._policy)
        preexec = _build_preexec(self._policy.limits)
        return PreparedLaunch(
            cmd=list(cmd),
            env=child_env,
            preexec_fn=preexec,
            creationflags=0,
            post_spawn=None,
            policy=self._policy,
            env_stripped_count=stripped,
        )
