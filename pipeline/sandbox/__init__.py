"""
pipeline/sandbox — process-level isolation for agent subprocesses (ADR 0034).

L1 launch-layer hygiene: env allowlist, resource limits,
child-process cleanup, output token masking, capability
detection. Cross-platform (Linux / macOS / Windows). orcho does
not build L2 / L3 / L4 sandboxes — runtime CLIs already gate
file access and tool use; duplicating those checks here would
be defence-in-depth duplication, not coverage.

Public surface:

* :class:`SandboxPolicy`, :class:`SandboxLimits`,
  :class:`SandboxMasking` — frozen dataclasses that the resolver
  returns and the runtime consumes.
* :class:`SandboxMode` — accepted enum values (``off`` / ``env``).
* :func:`detect_capabilities` — host probe for the L1 backend
  (platform + pywin32 presence).
* :func:`select_launcher` — returns the :class:`SandboxLauncher`
  implementation for the resolved policy and host platform.
* :class:`TokenMasker` — applied by ``_stream_run`` to live output.
* :func:`set_active_sandbox_policy` / :func:`get_active_sandbox_policy`
  — ContextVar contract mirroring :mod:`pipeline.engine.worktree`.
"""
from __future__ import annotations

from pipeline.sandbox.capabilities import Capabilities, detect_capabilities
from pipeline.sandbox.context import (
    get_active_sandbox_policy,
    reset_active_sandbox_policy,
    set_active_sandbox_policy,
)
from pipeline.sandbox.launcher import PreparedLaunch, SandboxLauncher, select_launcher
from pipeline.sandbox.masking import TokenMasker
from pipeline.sandbox.policy import (
    SandboxLimits,
    SandboxMasking,
    SandboxMode,
    SandboxPolicy,
)
from pipeline.sandbox.resolver import (
    SandboxConfigError,
    materialize_masker,
    resolve_sandbox_policy,
)

__all__ = [
    "Capabilities",
    "PreparedLaunch",
    "SandboxConfigError",
    "SandboxLauncher",
    "SandboxLimits",
    "SandboxMasking",
    "SandboxMode",
    "SandboxPolicy",
    "TokenMasker",
    "detect_capabilities",
    "get_active_sandbox_policy",
    "materialize_masker",
    "reset_active_sandbox_policy",
    "resolve_sandbox_policy",
    "select_launcher",
    "set_active_sandbox_policy",
]
