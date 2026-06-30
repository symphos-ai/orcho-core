"""
pipeline/sandbox/launcher.py — backend selection + Popen-kwargs preparation.

Two abstractions:

* :class:`PreparedLaunch` — the dict of kwargs (env, preexec_fn,
  creationflags, …) plus an optional ``post_spawn`` callback that
  ``_stream_run`` applies before / after :func:`subprocess.Popen`.
* :class:`SandboxLauncher` — strategy interface. Two backends ship:
  ``null`` (mode=off) and ``env`` (mode=env, two platform variants
  under the hood — :class:`EnvUnixLauncher` and
  :class:`EnvWindowsLauncher`).

The launcher is **not** responsible for masking. Masking lives in
the stream pipeline because it operates on output bytes, not on
process startup. The launcher returns the policy on the
:class:`PreparedLaunch` so ``_stream_run`` can instantiate a
:class:`TokenMasker` once and apply it inline.
"""
from __future__ import annotations

import platform
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pipeline.sandbox.policy import SandboxMode, SandboxPolicy


@dataclass(frozen=True, slots=True)
class PreparedLaunch:
    """Bundle of Popen kwargs + post-spawn hook + policy echo.

    ``env`` is the **resolved** child environment (post-allowlist,
    post-denylist). ``_stream_run`` passes it to Popen verbatim.

    ``preexec_fn`` is a zero-arg callable run in the child between
    fork and exec on Unix. ``None`` on Windows or when the backend
    has no work to do there.

    ``creationflags`` is the Windows Popen flag set (e.g.
    ``CREATE_NEW_PROCESS_GROUP``). 0 on Unix.

    ``post_spawn`` is invoked with the live :class:`subprocess.Popen`
    object after ``Popen()`` returns. Used by the Windows backend
    to assign the child to its Job Object. ``None`` when not
    needed.

    ``policy`` is echoed back so the caller does not have to thread
    it separately into the masker — a single object carries
    everything ``_stream_run`` needs.

    ``cmd`` is the launch command — passed through verbatim by the
    backends that ship today.

    ``env_stripped_count`` records how many parent variables were
    filtered out. Operator visibility via the run manifest.
    """
    cmd: list[str]
    env: dict[str, str]
    preexec_fn: Callable[[], None] | None
    creationflags: int
    post_spawn: Callable[[Any], None] | None
    policy: SandboxPolicy
    env_stripped_count: int = 0
    extra_popen_kwargs: dict[str, Any] = field(default_factory=dict)


class SandboxLauncher:
    """Strategy interface for sandbox backends.

    Subclasses implement :meth:`prepare`. They MUST be pure with
    respect to global state — ``prepare`` is called per agent
    invocation and may be called concurrently from sibling
    runtimes. Backends that need shared resources (e.g. a Job
    Object handle) hold them on the subclass instance, which is
    constructed once per policy by :func:`select_launcher`.
    """

    def prepare(
        self,
        *,
        cmd: list[str],
        cwd: str | None,
        parent_env: dict[str, str],
    ) -> PreparedLaunch:
        """Return Popen kwargs + post-spawn hook for ``cmd``.

        ``parent_env`` is the orcho parent's :data:`os.environ`
        captured *outside* the backend so tests can inject a
        synthetic environment without monkey-patching ``os``.
        """
        raise NotImplementedError


def select_launcher(policy: SandboxPolicy) -> SandboxLauncher:
    """Construct the backend that matches ``policy.mode`` and the host.

    Selection logic:

    * ``mode=off`` → :class:`NullLauncher` (no work at all).
    * ``mode=env`` → :class:`EnvUnixLauncher` on Linux/macOS,
                     :class:`EnvWindowsLauncher` on Windows.

    The host is detected via :func:`platform.system` so test
    suites can monkey-patch the module-level platform to exercise
    Windows behaviour from a Unix CI runner.
    """
    # Imported lazily so platforms that don't ship a backend
    # (Windows pywin32 missing) do not crash at module import.
    from pipeline.sandbox.backends.null_backend import NullLauncher

    if policy.mode is SandboxMode.OFF:
        return NullLauncher(policy)

    # mode is SandboxMode.ENV — the only other enum member.
    system = platform.system().lower()
    if system == "windows":
        from pipeline.sandbox.backends.env_windows import EnvWindowsLauncher
        return EnvWindowsLauncher(policy)
    from pipeline.sandbox.backends.env_unix import EnvUnixLauncher
    return EnvUnixLauncher(policy)


__all__ = [
    "PreparedLaunch",
    "SandboxLauncher",
    "select_launcher",
]
