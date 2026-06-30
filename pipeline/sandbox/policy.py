"""
pipeline/sandbox/policy.py — declarative sandbox policy shapes (ADR 0034).

These dataclasses are the runtime contract: the resolver returns a
frozen :class:`SandboxPolicy`, the orchestrator stores it on a
ContextVar, every agent dispatch reads it. They are pure data — no
I/O, no subprocess, no platform detection. The launcher selection
and capability detection live in sibling modules.

ADR 0034 ships only the L1 layer; the schema is deliberately
narrow to match. There is no ``network`` / ``proxy`` knob and no
``mode: native|container`` enum value — those would imply orcho
plans to build a parallel FS / network / container sandbox, which
it does not (runtime CLIs already gate those rows).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SandboxMode(StrEnum):
    """Process-isolation layer requested by a profile.

    ``off`` — no isolation; agent inherits parent env and limits
              verbatim. Pre-L1 behaviour, retained as escape hatch
              for fixtures and operators that need the unfiltered
              environment.
    ``env`` — L1: env allowlist + rlimit/Job Object + token masking
              + child-process cleanup. Default.
    """
    OFF = "off"
    ENV = "env"


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Resource caps applied to the agent subprocess.

    All fields use ``0`` to mean "no limit" — matches the
    ``timeouts.*_seconds = 0`` convention in :file:`config.defaults.json`.
    Limits are best-effort per platform: Unix uses
    :func:`resource.setrlimit` in a ``preexec_fn``; Windows uses Job
    Object basic-limit fields. Some limits map cleanly across both
    (CPU seconds, process memory); others (open files, file size) are
    Unix-only and silently skipped on Windows. The resolver records
    the effective platform mapping in the run manifest.
    """
    cpu_seconds: int = 0
    memory_mb: int = 0
    open_files: int = 0
    file_size_mb: int = 0

    def __post_init__(self) -> None:
        for name in ("cpu_seconds", "memory_mb", "open_files", "file_size_mb"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"SandboxLimits.{name} must be int, got "
                    f"{type(value).__name__}"
                )
            if value < 0:
                raise ValueError(
                    f"SandboxLimits.{name} must be ≥ 0 (0 = no limit), "
                    f"got {value}"
                )


@dataclass(frozen=True, slots=True)
class SandboxMasking:
    """Token-masking configuration applied to live agent output.

    ``builtin_patterns`` — when ``True`` (default), the built-in
    regex set covers the agent providers shipped today: ``sk-ant-…``,
    ``sk-…``, ``AIza…``. Disable only for tests that need raw stdout.

    ``custom_patterns`` — additional regex strings compiled at
    resolver time. Authors are responsible for anchoring (no implicit
    ``^``/``$``) and for escaping. Bad regex surfaces at resolver
    load with a clear error, not at agent dispatch.
    """
    builtin_patterns: bool = True
    custom_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.builtin_patterns, bool):
            raise TypeError(
                "SandboxMasking.builtin_patterns must be bool, got "
                f"{type(self.builtin_patterns).__name__}"
            )
        if not isinstance(self.custom_patterns, tuple):
            raise TypeError(
                "SandboxMasking.custom_patterns must be tuple, got "
                f"{type(self.custom_patterns).__name__}"
            )
        for i, p in enumerate(self.custom_patterns):
            if not isinstance(p, str) or not p:
                raise ValueError(
                    f"SandboxMasking.custom_patterns[{i}] must be a "
                    f"non-empty string"
                )


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Resolved, immutable sandbox configuration for one run.

    Produced by :func:`pipeline.sandbox.resolver.resolve_sandbox_policy`
    from the merge of global config + profile override; stored on a
    ContextVar by the orchestrator; read by agent runtimes at dispatch.

    ``env_allowlist`` is the **effective** list (built-in + profile
    additions, minus denylist). The launcher applies it verbatim —
    it does not re-merge with built-in defaults.
    """
    mode: SandboxMode = SandboxMode.ENV
    env_allowlist: tuple[str, ...] = ()
    env_denylist: tuple[str, ...] = ()
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    masking: SandboxMasking = field(default_factory=SandboxMasking)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SandboxMode):
            raise TypeError(
                f"SandboxPolicy.mode must be SandboxMode, got "
                f"{type(self.mode).__name__}"
            )
        if not isinstance(self.env_allowlist, tuple):
            raise TypeError(
                "SandboxPolicy.env_allowlist must be tuple, got "
                f"{type(self.env_allowlist).__name__}"
            )
        if not isinstance(self.env_denylist, tuple):
            raise TypeError(
                "SandboxPolicy.env_denylist must be tuple, got "
                f"{type(self.env_denylist).__name__}"
            )
        for name, lst in (("env_allowlist", self.env_allowlist),
                          ("env_denylist", self.env_denylist)):
            for i, v in enumerate(lst):
                if not isinstance(v, str) or not v:
                    raise ValueError(
                        f"SandboxPolicy.{name}[{i}] must be a non-empty "
                        f"string"
                    )
        if not isinstance(self.limits, SandboxLimits):
            raise TypeError("SandboxPolicy.limits must be SandboxLimits")
        if not isinstance(self.masking, SandboxMasking):
            raise TypeError("SandboxPolicy.masking must be SandboxMasking")

    @property
    def isolation_active(self) -> bool:
        """True when the policy does any L1 work.

        ``mode=off`` short-circuits the launcher to a no-op so existing
        ``_stream_run`` behaviour is preserved verbatim. ``mode=env``
        activates env filtering, rlimit / Job Object, child-process
        cleanup and token masking — hence ``isolation_active``.
        """
        return self.mode is not SandboxMode.OFF


__all__ = [
    "SandboxLimits",
    "SandboxMasking",
    "SandboxMode",
    "SandboxPolicy",
]
