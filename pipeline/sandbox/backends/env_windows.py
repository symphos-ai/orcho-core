"""
pipeline/sandbox/backends/env_windows.py — L1 backend for Windows.

Replaces the Unix preexec-fn + rlimit + setpgrp triad with the
Windows equivalent: a Job Object holds the child, the basic-limit
info on the job carries the CPU / memory caps, and
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` ensures the entire process
tree dies when orcho releases the job handle. Process groups are
unnecessary on Windows — closing the job handle is stronger than a
Unix ``killpg`` because it's kernel-enforced.

The Job Object is created at :meth:`prepare` time and assigned to
the child in a ``post_spawn`` hook (you can only assign a process
to a job after :func:`subprocess.Popen` returns its PID). The job
handle lives on the launcher instance for the lifetime of the
``PreparedLaunch`` — held by the caller — so the kernel cleanup
fires when ``_stream_run`` drops the prepared launch.

When :mod:`win32job` is not importable (pywin32 not installed),
the launcher degrades to env-allowlist + token-masking only and
records the missing capability for the operator to see in the
run manifest. The resolver's capability detector already surfaces
this state before the launcher runs — this defence here is
belt-and-braces for direct unit tests.
"""
from __future__ import annotations

from typing import Any

from pipeline.sandbox.backends._env_filter import compute_child_env
from pipeline.sandbox.launcher import PreparedLaunch, SandboxLauncher
from pipeline.sandbox.policy import SandboxLimits, SandboxPolicy

# Windows CreationFlag for "new process group". Always set so a
# Ctrl+C in the orcho parent does not propagate to the agent (we
# kill it deliberately via the job).
_CREATE_NEW_PROCESS_GROUP = 0x00000200

_MB = 1024 * 1024


def _build_job(limits: SandboxLimits) -> tuple[Any | None, Any | None]:
    """Create a Job Object with the requested limits.

    Returns ``(handle, win32job_module)`` for the post-spawn hook.
    Returns ``(None, None)`` if pywin32 is not installed or if Job
    Object creation fails. The launcher continues without job
    enforcement in that case — env filtering still applies.
    """
    try:
        import win32api  # noqa: F401
        import win32job
    except ImportError:
        return None, None

    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        basic = info["BasicLimitInformation"]
        flags = basic["LimitFlags"]

        # Kill the whole tree when the handle closes.
        flags |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if limits.cpu_seconds > 0:
            # PerProcessUserTimeLimit is 100-ns ticks.
            basic["PerProcessUserTimeLimit"] = limits.cpu_seconds * 10_000_000
            flags |= win32job.JOB_OBJECT_LIMIT_PROCESS_TIME

        if limits.memory_mb > 0:
            info["ProcessMemoryLimit"] = limits.memory_mb * _MB
            flags |= win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY

        basic["LimitFlags"] = flags
        info["BasicLimitInformation"] = basic
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        return job, win32job
    except Exception:  # pragma: no cover — pywin32 surface variance
        # Job Object setup failed (rare — usually a privilege issue
        # on locked-down corporate Windows). Continue without job
        # enforcement; env allowlist is still in effect.
        return None, None


def _build_post_spawn(job: Any | None, win32job: Any | None):
    """Return a callback that assigns the child to the job, or None."""
    if job is None or win32job is None:
        return None

    def _assign(proc: Any) -> None:
        try:
            import win32api
            handle = win32api.OpenProcess(
                # PROCESS_SET_QUOTA | PROCESS_TERMINATE
                0x0100 | 0x0001, False, proc.pid,
            )
            try:
                win32job.AssignProcessToJobObject(job, handle)
            finally:
                win32api.CloseHandle(handle)
        except Exception:  # pragma: no cover — best-effort
            # Assignment can fail if the child has already been
            # placed in a parent job that doesn't allow nested
            # jobs (pre-Win8 only). Ignore — env allowlist still
            # protects against the worst.
            pass

    return _assign


class EnvWindowsLauncher(SandboxLauncher):
    """L1 launcher for Windows.

    Builds the Job Object eagerly at construction (one per policy,
    not per spawn) so spawns are cheap and the handle's lifetime
    matches the policy's lifetime. When pywin32 is missing the
    launcher silently downgrades to env-only and lets the
    capability detector surface the gap to the operator.
    """

    __slots__ = ("_policy", "_job", "_win32job")

    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy
        self._job, self._win32job = _build_job(policy.limits)

    def prepare(
        self,
        *,
        cmd: list[str],
        cwd: str | None,  # noqa: ARG002 — interface uniformity
        parent_env: dict[str, str],
    ) -> PreparedLaunch:
        child_env, stripped = compute_child_env(parent_env, self._policy)
        post = _build_post_spawn(self._job, self._win32job)
        return PreparedLaunch(
            cmd=list(cmd),
            env=child_env,
            preexec_fn=None,
            creationflags=_CREATE_NEW_PROCESS_GROUP,
            post_spawn=post,
            policy=self._policy,
            env_stripped_count=stripped,
        )
