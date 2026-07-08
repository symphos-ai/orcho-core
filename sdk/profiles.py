"""Profile catalogue read-surface — the shipped v2 profile registry as
frozen, JSON-serialisable summaries.

A thin, read-only projection over the engine profile loader
(:func:`pipeline.profiles.loader.load_profiles_v2`). It mirrors every field
an MCP profile listing needs so downstream adapters stay pure projections:
identity (``name``/``kind``/``variant``/``semantic_profile``/``recipe_kind``/
``internal``), posture (``default_mode``/``isolated``), the flattened phase
sequence, the projected ``cross_gates`` policy map, and the plan
``hypothesis`` prelude.

Enum→string coercion happens here at the SDK boundary — callers see plain
strings (``OperatingMode`` values, gate run/skip ``.value``), never enum
objects. The module never parses JSON by hand, never mutates the engine, and
imports no CLI/UI code.

Catalogue resolution honours the ``ORCHO_PROFILES_V2_PATH`` environment
override (resolved per call so tests can monkeypatch it without reloading the
module); otherwise it falls back to the shipped
``CONFIG_DIR/pipeline_profiles_v2.json``. A missing catalogue file is a
documented empty result: :func:`list_profiles` returns an empty tuple rather
than raising, and :func:`catalogue_path` still exposes the resolved path for
diagnostics.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    """One catalogue entry, projected for read-only consumers.

    ``default_mode`` / ``semantic_profile`` / ``recipe_kind`` are the plain
    string ``.value`` of their engine enums (or ``None`` when the profile
    leaves them unset — e.g. ``task``/``correction`` carry no
    ``default_mode``). ``isolated`` is ``worktree_isolation != 'off'`` so an
    absent, ``per_run`` or ``per_phase`` policy reads as isolated and only an
    explicit ``off`` reads as ``False``. ``phases`` is the flattened phase
    sequence with loop bodies expanded in file order. ``cross_gates`` is
    ``None`` when the profile declares no ``cross_gates`` block, else a
    projected ``{gate: {enabled, run, on_skip, mode}}`` map. ``hypothesis``
    mirrors the plan-step prelude as ``{'attempts', 'format'}`` when attempts
    are enabled (``attempts > 0``), else ``None``.
    """

    name: str
    kind: str
    variant: str | None
    description: str
    default_mode: str | None
    isolated: bool
    phases: tuple[str, ...]
    cross_gates: dict[str, dict] | None
    hypothesis: dict | None
    semantic_profile: str | None
    recipe_kind: str | None
    internal: bool


__all__ = ["ProfileSummary", "list_profiles", "catalogue_path"]


def catalogue_path() -> Path:
    """Resolve the profile catalogue path.

    Honours the ``ORCHO_PROFILES_V2_PATH`` environment override (expanded
    user paths supported); falls back to the shipped
    ``CONFIG_DIR/pipeline_profiles_v2.json``. Resolved per call so a
    monkeypatched environment takes effect without reloading the module. The
    returned path is not guaranteed to exist — callers that need existence
    (see :func:`list_profiles`) must check it.
    """
    override = os.environ.get("ORCHO_PROFILES_V2_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    from core.infra.paths import CONFIG_DIR

    return CONFIG_DIR / "pipeline_profiles_v2.json"


def _enum_value(value: object) -> str | None:
    """Coerce an optional StrEnum (or plain string) to its string value."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _flatten_phases(profile: object) -> tuple[str, ...]:
    """Flatten the profile step tree into an ordered phase-name tuple.

    Top-level ``PhaseStep`` entries contribute their ``.phase``; ``LoopStep``
    wrappers (identified by a ``.steps`` collection) contribute each inner
    step's ``.phase`` in order.
    """
    phases: list[str] = []
    for step in profile.steps:
        if hasattr(step, "steps"):
            phases.extend(inner.phase for inner in step.steps)
        elif hasattr(step, "phase"):
            phases.append(step.phase)
    return tuple(phases)


def _project_cross_gates(profile: object) -> dict[str, dict] | None:
    """Project a non-empty ``cross_gates`` mapping into plain dicts.

    Returns ``None`` when the profile declares no cross gates so consumers
    can distinguish "no block" from "empty block".
    """
    gates = getattr(profile, "cross_gates", None)
    if not gates:
        return None
    return {
        name: {
            "enabled": policy.enabled,
            "run": _enum_value(policy.run),
            "on_skip": _enum_value(policy.on_skip),
            "mode": policy.mode,
        }
        for name, policy in gates.items()
    }


def _project_hypothesis(profile: object) -> dict | None:
    """Mirror the plan-step hypothesis prelude as ``{'attempts', 'format'}``.

    Locates the ``plan`` step (top-level or nested inside a loop) and returns
    its prelude only when attempts are enabled; a disabled prelude
    (``attempts == 0``) or an absent plan step projects to ``None``.
    """
    for step in profile.steps:
        candidates = step.steps if hasattr(step, "steps") else (step,)
        for inner in candidates:
            if getattr(inner, "phase", None) != "plan":
                continue
            prelude = getattr(inner, "hypothesis", None)
            if prelude is None:
                return None
            attempts = getattr(prelude, "attempts", 0)
            if attempts and attempts > 0:
                return {
                    "attempts": int(attempts),
                    "format": getattr(prelude, "format", None),
                }
            return None
    return None


def _summarize(profile: object) -> ProfileSummary:
    return ProfileSummary(
        name=profile.name,
        kind=_enum_value(profile.kind) or str(profile.kind),
        variant=profile.variant,
        description=profile.description,
        default_mode=_enum_value(profile.default_mode),
        isolated=profile.worktree_isolation != "off",
        phases=_flatten_phases(profile),
        cross_gates=_project_cross_gates(profile),
        hypothesis=_project_hypothesis(profile),
        semantic_profile=_enum_value(profile.semantic_profile),
        recipe_kind=profile.recipe_kind,
        internal=bool(getattr(profile, "internal", False)),
    )


def list_profiles() -> tuple[ProfileSummary, ...]:
    """List the shipped v2 profile catalogue as frozen summaries.

    Order follows the catalogue file (the loader preserves JSON insertion
    order). The ``auto-detect`` selector token — a CLI/MCP selector, not an
    executable profile — is skipped. A missing catalogue file (override path
    that does not exist, or an absent shipped file) is a documented empty
    result: an empty tuple is returned without raising.
    """
    path = catalogue_path()
    if not path.is_file():
        return ()

    from pipeline.profiles.loader import load_profiles_v2

    try:
        from pipeline.project.auto_detect import AUTO_DETECT_PROFILE_TOKEN
    except ImportError:  # pragma: no cover - defensive
        AUTO_DETECT_PROFILE_TOKEN = "auto-detect"

    profiles = load_profiles_v2(path)
    return tuple(
        _summarize(profile)
        for name, profile in profiles.items()
        if name != AUTO_DETECT_PROFILE_TOKEN
    )
