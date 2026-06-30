"""Profile resolution + projection for the cross-project pipeline.

The typed cross entry surface (``pipeline/cross_project/app.py``) must
resolve the run's v2 ``Profile`` and every value derived from it before
the run-setup logging fires. This module owns that work:

* loading the v2 ``Profile`` registry (``load_profiles_v2_with_plugins``);
* resolving the requested profile by name (same errors as the inline
  body: ``FileNotFoundError`` when the registry file is missing,
  ``ValueError`` for an unknown profile, ``ValueError`` wrapping a
  ``CrossProjectionError``);
* projecting the profile into cross ``global_steps`` / ``project_steps``
  (``project_cross_profile``);
* building the synthetic ``<name>#project`` child profile;
* the cross gate policy lookups (``contract_check`` /
  ``cross_final_acceptance``);
* the set of global cross handlers.

:func:`setup_cross_profile` returns a typed :class:`CrossProfileSetup`
so the coordinator builds the run off a single structured object instead
of a train of locals. The projected profile name is resolved here,
upstream of the ``run.start`` emit, so events / ``meta.json`` carry the
final projected profile name together.

This module is a leaf peer: it MUST NOT import from
:mod:`pipeline.cross_project.orchestrator`.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipeline.cross_project.profile_projection import (
        CrossGatePolicy,
        CrossProjection,
    )
    from pipeline.runtime import Profile


def _flatten_profile_entries(entries) -> list[Any]:
    """Flatten PhaseStep / LoopStep profile entries for display only."""
    out: list[Any] = []
    for entry in entries:
        steps = getattr(entry, "steps", None)
        if isinstance(steps, tuple):
            out.extend(steps)
        else:
            out.append(entry)
    return out


def _gate_will_run(policy: Any) -> bool:
    if not bool(getattr(policy, "enabled", False)):
        return False
    run = getattr(getattr(policy, "run", None), "value", None)
    return run != "never"


@dataclasses.dataclass(frozen=True)
class CrossProfileSetup:
    """Resolved cross profile state for one run.

    Built before run-setup logging so the projected profile name reaches
    the ``run.start`` event together with the requested profile name.
    """

    requested_profile: Profile
    projection: CrossProjection
    child_profile: Profile | None
    projected_profile_name: str | None
    contract_gate_policy: CrossGatePolicy
    cfa_gate_policy: CrossGatePolicy
    global_handlers: frozenset[str]


def setup_cross_profile(*, profile_name: str) -> CrossProfileSetup:
    """Resolve the cross run's profile and every value derived from it.

    Raises the same errors the inline body did: ``FileNotFoundError`` when
    the v2 registry is missing, ``ValueError`` for an unknown profile, and
    ``ValueError`` wrapping a ``CrossProjectionError`` when the profile
    cannot run in cross mode.
    """
    from core.infra.paths import CONFIG_DIR
    from pipeline.cross_project.profile_projection import (
        CrossProjectionError,
        get_cross_gate_policy,
        project_cross_profile,
    )
    from pipeline.profiles.loader import load_profiles_v2_with_plugins
    from pipeline.runtime import Profile, ProfileKind

    profiles_path = CONFIG_DIR / "pipeline_profiles_v2.json"
    if not profiles_path.exists():
        raise FileNotFoundError(
            f"cross run requires pipeline_profiles_v2.json at {profiles_path}"
        )
    all_profiles = load_profiles_v2_with_plugins(profiles_path)
    requested_profile = all_profiles.get(profile_name)
    if requested_profile is None:
        raise ValueError(
            f"unknown profile {profile_name!r}; available: "
            f"{sorted(all_profiles)}"
        )
    try:
        projection = project_cross_profile(requested_profile)
    except CrossProjectionError as e:
        raise ValueError(
            f"profile {profile_name!r} cannot run in cross mode: {e}"
        ) from e
    child_profile = (
        Profile(
            name=f"{requested_profile.name}#project",
            kind=ProfileKind.CUSTOM,
            variant=None,
            description=f"Projected project steps from {requested_profile.name!r}",
            steps=projection.project_steps,
            change_handoff=requested_profile.change_handoff,
        )
        if projection.project_steps
        else None
    )
    projected_profile_name = (
        f"{requested_profile.name}#project"
        if child_profile is not None
        else None
    )

    global_handlers = frozenset(
        str(getattr(getattr(step, "cross", None), "handler", "") or "")
        for step in _flatten_profile_entries(projection.global_steps)
    )
    contract_gate_policy = get_cross_gate_policy(
        requested_profile, "contract_check",
    )
    cfa_gate_policy = get_cross_gate_policy(
        requested_profile, "cross_final_acceptance",
    )

    return CrossProfileSetup(
        requested_profile=requested_profile,
        projection=projection,
        child_profile=child_profile,
        projected_profile_name=projected_profile_name,
        contract_gate_policy=contract_gate_policy,
        cfa_gate_policy=cfa_gate_policy,
        global_handlers=global_handlers,
    )


__all__ = [
    "CrossProfileSetup",
    "setup_cross_profile",
    "_flatten_profile_entries",
    "_gate_will_run",
]
