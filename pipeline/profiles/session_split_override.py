"""Apply per-phase prompt session-split overrides to v2 profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from pipeline.runtime.profile import LoopStep, Profile
from pipeline.runtime.steps import PhaseStep

_VALID_SESSION_SPLITS = frozenset({
    "stateless",
    "per_phase",
    "per_role",
    "common",
})


def apply_session_split_overrides(
    profile: Profile,
    overrides: Mapping[str, str] | None,
) -> Profile:
    """Return ``profile`` with selected phase ``session_split`` values patched.

    Overrides are phase-name scoped and apply to every matching ``PhaseStep``
    in the active profile, including phases inside retry loops. Phases absent
    from a scoped/projected profile are ignored: one workspace-level override
    (for example ``implement=common,repair_changes=common``) must remain usable
    across ``advanced``, ``task``, and scoped ``review`` profiles.
    """
    normalized = _normalize_overrides(overrides)
    if not normalized:
        return profile

    seen: set[str] = set()
    steps = tuple(_override_entry(entry, normalized, seen) for entry in profile.steps)
    return replace(profile, steps=steps)


def _normalize_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    if not overrides:
        return {}
    out: dict[str, str] = {}
    for phase, split in overrides.items():
        phase_key = str(phase).strip()
        split_value = str(split).strip()
        if not phase_key:
            raise ValueError("session_split_override contains an empty phase")
        if split_value not in _VALID_SESSION_SPLITS:
            raise ValueError(
                f"session_split_override {phase_key!r}={split_value!r} "
                f"is not one of {sorted(_VALID_SESSION_SPLITS)}"
            )
        out[phase_key] = split_value
    return out


def _override_entry(
    entry: PhaseStep | LoopStep,
    overrides: Mapping[str, str],
    seen: set[str],
) -> PhaseStep | LoopStep:
    if isinstance(entry, PhaseStep):
        return _override_phase(entry, overrides, seen)
    if isinstance(entry, LoopStep):
        return replace(
            entry,
            steps=tuple(
                _override_phase(step, overrides, seen)
                for step in entry.steps
            ),
        )
    return entry


def _override_phase(
    step: PhaseStep,
    overrides: Mapping[str, str],
    seen: set[str],
) -> PhaseStep:
    split = overrides.get(step.phase)
    if split is None:
        return step
    seen.add(step.phase)
    # Patch only the split axis; preserve every other ExecutionPolicy field
    # (notably the orthogonal ``session_continuity`` declaration) via
    # ``replace`` so a valid built-in continuity is not silently dropped to
    # ``None``, which would later make the phase-role resolver fail loudly.
    policy = replace(step.execution_policy, session_split=split)
    return replace(step, execution_policy=policy)


__all__ = ["apply_session_split_overrides"]
