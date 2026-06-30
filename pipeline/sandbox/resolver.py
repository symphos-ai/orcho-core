"""
pipeline/sandbox/resolver.py — global × profile → SandboxPolicy.

Single responsibility: take the raw config dicts (global
``config.defaults.json`` ``sandbox`` section + profile-level
``sandbox`` override) and return a frozen :class:`SandboxPolicy`.
Raises :class:`SandboxConfigError` for structural problems (unknown
``mode`` value, malformed limits, broken regex in masking custom
patterns).

The resolver does NOT read :data:`os.environ`, does NOT detect the
platform, does NOT spawn anything. Pure input-output. The
orchestrator wires it into the run-init sequence next to the
worktree resolver.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pipeline.sandbox.defaults import BUILTIN_TOKEN_PATTERNS
from pipeline.sandbox.masking import MaskingPatternError, TokenMasker
from pipeline.sandbox.policy import (
    SandboxLimits,
    SandboxMasking,
    SandboxMode,
    SandboxPolicy,
)


class SandboxConfigError(ValueError):
    """Raised when sandbox config / profile combination is structurally bad.

    Examples: mode value outside the enum, ``cpu_seconds`` negative,
    masking regex fails to compile. Inherits from :class:`ValueError`
    per the project's invariants policy so broader catches keep working.
    """


def _merge_tuple(*sources: tuple[str, ...] | None) -> tuple[str, ...]:
    """Merge several optional tuples into one, preserving order, dedup."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for src in sources:
        if not src:
            continue
        for item in src:
            if item in seen_set:
                continue
            seen.append(item)
            seen_set.add(item)
    return tuple(seen)


def _coerce_str_tuple(value: Any, field_path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise SandboxConfigError(
            f"{field_path}: must be a list of strings, got "
            f"{type(value).__name__}"
        )
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise SandboxConfigError(
                f"{field_path}[{i}]: must be a non-empty string"
            )
        out.append(item)
    return tuple(out)


def _coerce_limits(raw: Any, field_path: str) -> SandboxLimits:
    if raw is None:
        return SandboxLimits()
    if not isinstance(raw, Mapping):
        raise SandboxConfigError(
            f"{field_path}: must be an object, got {type(raw).__name__}"
        )
    known = {"cpu_seconds", "memory_mb", "open_files", "file_size_mb"}
    extra = set(raw.keys()) - known - {"_comment"}
    if extra:
        raise SandboxConfigError(
            f"{field_path}: unknown keys {sorted(extra)}; expected subset of "
            f"{sorted(known)}"
        )
    kwargs: dict[str, int] = {}
    for k in known:
        v = raw.get(k, 0)
        if v is None:
            v = 0
        if isinstance(v, bool) or not isinstance(v, int):
            raise SandboxConfigError(
                f"{field_path}.{k}: must be integer, got {type(v).__name__}"
            )
        kwargs[k] = v
    try:
        return SandboxLimits(**kwargs)
    except (TypeError, ValueError) as e:
        raise SandboxConfigError(f"{field_path}: {e}") from e


def _coerce_masking(raw: Any, field_path: str) -> SandboxMasking:
    if raw is None:
        return SandboxMasking()
    if not isinstance(raw, Mapping):
        raise SandboxConfigError(
            f"{field_path}: must be an object, got {type(raw).__name__}"
        )
    extra = set(raw.keys()) - {"builtin_patterns", "custom_patterns", "_comment"}
    if extra:
        raise SandboxConfigError(
            f"{field_path}: unknown keys {sorted(extra)}"
        )
    builtin = raw.get("builtin_patterns", True)
    if not isinstance(builtin, bool):
        raise SandboxConfigError(
            f"{field_path}.builtin_patterns: must be bool, got "
            f"{type(builtin).__name__}"
        )
    custom = _coerce_str_tuple(
        raw.get("custom_patterns"), f"{field_path}.custom_patterns"
    )
    try:
        # Compile-once probe — surfaces bad regex at resolver time
        # so agent dispatch never explodes.
        TokenMasker(custom)
    except MaskingPatternError as e:
        raise SandboxConfigError(f"{field_path}.custom_patterns: {e}") from e
    return SandboxMasking(builtin_patterns=builtin, custom_patterns=custom)


def _coerce_mode(value: Any, field_path: str) -> SandboxMode | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SandboxConfigError(
            f"{field_path}: must be a string, got {type(value).__name__}"
        )
    try:
        return SandboxMode(value)
    except ValueError as e:
        valid = [m.value for m in SandboxMode]
        raise SandboxConfigError(
            f"{field_path}: {value!r} is not one of {valid}"
        ) from e


def _parse_section(raw: Any, field_path: str) -> dict[str, Any]:
    """Parse one ``sandbox`` mapping (global or profile-level) into a
    normalized dict of typed fields. ``None`` fields are kept so the
    merge step can distinguish "not set" from "explicitly default".
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SandboxConfigError(
            f"{field_path}: must be an object, got {type(raw).__name__}"
        )
    known = {
        "mode", "env_allowlist", "env_denylist", "limits", "masking",
    }
    extra = set(raw.keys()) - known - {"_comment"}
    if extra:
        raise SandboxConfigError(
            f"{field_path}: unknown keys {sorted(extra)}; expected subset of "
            f"{sorted(known)}"
        )
    return {
        "mode": _coerce_mode(raw.get("mode"), f"{field_path}.mode"),
        "env_allowlist": _coerce_str_tuple(
            raw.get("env_allowlist"), f"{field_path}.env_allowlist"
        ) if raw.get("env_allowlist") is not None else None,
        "env_denylist": _coerce_str_tuple(
            raw.get("env_denylist"), f"{field_path}.env_denylist"
        ) if raw.get("env_denylist") is not None else None,
        "limits": _coerce_limits(raw.get("limits"), f"{field_path}.limits")
        if "limits" in raw else None,
        "masking": _coerce_masking(raw.get("masking"), f"{field_path}.masking")
        if "masking" in raw else None,
    }


def resolve_sandbox_policy(
    *,
    global_config: Mapping[str, Any] | None,
    profile_override: Mapping[str, Any] | None,
) -> SandboxPolicy:
    """Merge global + profile sandbox config into a :class:`SandboxPolicy`.

    Resolution order:

    1. Parse the global ``sandbox`` block (typically from
       ``config.defaults.json``). Missing block → defaults.
    2. Parse the profile-level ``sandbox`` block. Missing block →
       no overrides.
    3. Profile fields override global field-by-field (mode, limits,
       masking). Allowlist / denylist are **additive**: profile
       additions extend the global list, they do not replace it.
    """
    global_parsed = _parse_section(global_config, "config.sandbox")
    profile_parsed = _parse_section(profile_override, "profile.sandbox")

    def _pick(field: str) -> Any:
        prof = profile_parsed.get(field)
        if prof is not None:
            return prof
        return global_parsed.get(field)

    mode = _pick("mode") or SandboxMode.ENV

    allowlist = _merge_tuple(
        global_parsed.get("env_allowlist"),
        profile_parsed.get("env_allowlist"),
    )
    denylist = _merge_tuple(
        global_parsed.get("env_denylist"),
        profile_parsed.get("env_denylist"),
    )

    limits = profile_parsed.get("limits") or global_parsed.get("limits") \
        or SandboxLimits()
    masking = profile_parsed.get("masking") or global_parsed.get("masking") \
        or SandboxMasking()

    return SandboxPolicy(
        mode=mode,
        env_allowlist=allowlist,
        env_denylist=denylist,
        limits=limits,
        masking=masking,
    )


def materialize_masker(policy: SandboxPolicy) -> TokenMasker:
    """Build the :class:`TokenMasker` from a resolved policy.

    Built-in patterns are appended when
    :attr:`SandboxMasking.builtin_patterns` is True, then any
    profile-supplied ``custom_patterns`` follow. Order matters for
    a fused alternation only if patterns overlap (they don't, by
    construction — each issuer prefix is unique).
    """
    patterns: tuple[str, ...] = ()
    if policy.masking.builtin_patterns:
        patterns = patterns + BUILTIN_TOKEN_PATTERNS
    patterns = patterns + policy.masking.custom_patterns
    return TokenMasker(patterns)


__all__ = [
    "SandboxConfigError",
    "materialize_masker",
    "resolve_sandbox_policy",
]
