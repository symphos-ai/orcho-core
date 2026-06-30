"""Profile resolution + projection for the project pipeline.

The typed project entry surface (``pipeline/project/app.py``) must
resolve the run's v2 ``Profile`` and every value derived from it before
the run-setup logging fires. This module owns that work:

* profile-name resolution (``ORCHO_PIPELINE`` override → requested name);
* loading the v2 ``Profile`` (or accepting an in-memory ``profile_obj``
  supplied by the cross-project orchestrator);
* ``--from-run-plan`` parent-plan load + planning-block projection;
* ``session_split_override`` application;
* cross/change handoff resolution and profile mode-gates.

:func:`setup_profile` returns a typed :class:`ProfileSetup` so the
coordinator builds the run off a single structured object instead of a
train of locals. The projection + projected-profile name are resolved
here, upstream of the ``run.start`` emit, so events / ``meta.json`` /
checkpoint config all carry the final projected profile name together.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from core.infra import config
from core.infra.paths import CONFIG_DIR
from pipeline.profiles.loader import load_profiles_v2_with_plugins
from pipeline.profiles.session_split_override import apply_session_split_overrides
from pipeline.project.profile_dispatch import resolve_mode_gates as _resolve_mode_gates
from pipeline.project.types import PresentationPolicy

if TYPE_CHECKING:
    from pipeline.plan_parser import ParsedPlan


@dataclasses.dataclass(frozen=True)
class ProfileSetup:
    """Resolved profile state for one run.

    Built before run-setup logging so the projected profile name reaches
    the ``run.start`` event and ``meta.json`` together. ``plan_source`` is
    the (possibly ``--from-run-plan`` overridden) value the rest of the
    run lifecycle threads into logging, session init, and dispatch.
    """

    v2_profile: Any
    resolved_profile_name: str
    projected_profile_name: str | None
    from_run_plan_loaded: ParsedPlan | None
    from_run_plan_stripped: tuple[str, ...]
    plan_source: str
    cross_handoff_text: str
    change_handoff: str
    do_plan: bool
    do_build: bool
    do_review: bool
    max_rounds: int
    #: Inspectable provenance: True only when the ``ORCHO_PIPELINE`` env
    #: A/B override actually displaced the caller-provided profile name.
    #: It can fire only on a fresh explicit start — a resume / follow-up
    #: passes ``allow_env_override=False`` so durable ``meta['profile']``
    #: inheritance can never be silently hijacked by ambient env.
    env_profile_override_applied: bool = False


def setup_profile(
    *,
    profile_name: str,
    profile_obj: Any | None,
    from_run_plan_parent_dir: Path | None,
    plan_source: str,
    handoff_path: str | None,
    max_rounds: int,
    presentation: PresentationPolicy,
    allow_env_override: bool = True,
) -> ProfileSetup:
    """Resolve the run's profile and every value derived from it.

    ``allow_env_override`` gates the ``ORCHO_PIPELINE`` A/B env override.
    Callers signal a fresh explicit start with the default ``True``; a
    resume / follow-up passes ``False`` so the inherited durable profile
    (already resolved via :func:`resolve_resume_profile`) cannot be
    silently displaced by an ambient ``ORCHO_PIPELINE``. The override,
    when it fires, is recorded on ``ProfileSetup.env_profile_override_applied``
    (inspectable provenance, not a silent global).

    Side-effect free apart from the ``--from-run-plan`` projection notice
    and the env-override notice, both gated on ``presentation`` so silent
    callers see only the structured facts and not the terminal arrow lines.
    """
    # ``--from-run-plan`` follow-up: load the parent's parsed plan now so it
    # is available for profile projection (skip the planning block) and
    # state hydration downstream. ``plan_source`` defaults to ``"run"`` on
    # this path; an explicit override wins.
    from_run_plan_loaded: ParsedPlan | None = None
    if from_run_plan_parent_dir is not None:
        from pipeline.plan_artifacts import load_parsed_plan_artifact
        from_run_plan_loaded = load_parsed_plan_artifact(
            from_run_plan_parent_dir,
        )
        if plan_source == "local":
            plan_source = "run"

    _name_resolution = _resolve_profile_name_provenance(
        profile_name=profile_name,
        allow_env_override=allow_env_override,
    )
    resolved_profile_name = _name_resolution.name
    if _name_resolution.env_override_applied and (
        presentation is PresentationPolicy.TERMINAL
    ):
        print(
            "  ↳ ORCHO_PIPELINE override applied: "
            f"{_name_resolution.requested!r} → {resolved_profile_name!r} "
            "(fresh explicit start)",
        )
    # ``projected_profile`` surfaces the synthetic in-memory profile name
    # (e.g. ``advanced#project``) when the cross orchestrator supplied a
    # projected child profile via ``profile_obj``. ``resolved_profile_name``
    # stays the requested profile (``advanced``) for canonical metadata.
    projected_profile_name: str | None = None
    if profile_obj is not None and profile_obj.name != resolved_profile_name:
        projected_profile_name = profile_obj.name

    # An in-memory ``profile_obj`` short-circuits name resolution — used by
    # the cross-project orchestrator to pass a projected child profile.
    # ``resolved_profile_name`` already reflects the env decision, so the
    # nested load resolves the name as-is (``allow_env_override=False``)
    # to avoid a redundant second env application.
    if profile_obj is not None:
        v2_profile = profile_obj
    else:
        v2_profile = _resolve_v2_profile(
            profile_name=resolved_profile_name,
            allow_env_override=False,
        )
    if v2_profile is None:
        _raise_unresolved_profile(resolved_profile_name)

    # ``--from-run-plan`` projection: strip the leading planning block from
    # the selected profile so the child run does not re-produce the plan it
    # already has from the parent. The projected profile gets a synthetic
    # ``<name>#from_run_plan`` name so evidence / dashboards see the
    # derivation; ``meta.profile`` keeps the requested name. No-op when the
    # profile has no leading planning block (idempotent).
    from_run_plan_stripped: tuple[str, ...] = ()
    if from_run_plan_loaded is not None:
        from pipeline.control.from_run_plan import (
            project_profile_for_from_run_plan,
        )
        _projection = project_profile_for_from_run_plan(v2_profile)
        if not _projection.is_noop:
            v2_profile = _projection.profile
            from_run_plan_stripped = _projection.stripped_phases
            projected_profile_name = v2_profile.name
            if presentation is PresentationPolicy.TERMINAL:
                print(
                    "  ↳ --from-run-plan projected profile "
                    f"{resolved_profile_name!r} → {v2_profile.name!r} "
                    f"(stripped: {list(from_run_plan_stripped)})",
                )

    _session_split_overrides = config.AppConfig.load().pipeline.get(
        "session_split_override",
        {},
    )
    if _session_split_overrides:
        v2_profile = apply_session_split_overrides(
            v2_profile,
            _session_split_overrides,
        )

    cross_handoff_text = _resolve_cross_handoff(
        profile=v2_profile,
        plan_source=plan_source,
        handoff_path=handoff_path,
    )
    change_handoff = _resolve_change_handoff(v2_profile)
    do_plan, do_build, do_review, max_rounds = _resolve_mode_gates(
        v2_profile, max_rounds,
    )

    return ProfileSetup(
        v2_profile=v2_profile,
        resolved_profile_name=resolved_profile_name,
        projected_profile_name=projected_profile_name,
        from_run_plan_loaded=from_run_plan_loaded,
        from_run_plan_stripped=from_run_plan_stripped,
        plan_source=plan_source,
        cross_handoff_text=cross_handoff_text,
        change_handoff=change_handoff,
        do_plan=do_plan,
        do_build=do_build,
        do_review=do_review,
        max_rounds=max_rounds,
        env_profile_override_applied=_name_resolution.env_override_applied,
    )


@dataclasses.dataclass(frozen=True)
class _ProfileNameResolution:
    """Outcome of profile-name resolution, with env-override provenance.

    ``name`` is the resolved profile name; ``requested`` is the caller-
    provided one; ``env_override_applied`` is True only when an
    ``ORCHO_PIPELINE`` value was allowed *and* actually changed the name.
    Keeping the provenance typed (instead of a bare string) lets the
    coordinator surface / assert that the A/B env override fired only on
    a fresh explicit start — never on resume / follow-up inheritance.
    """

    name: str
    requested: str
    env_override_applied: bool


def _resolve_profile_name_provenance(
    *,
    profile_name: str,
    allow_env_override: bool = True,
) -> _ProfileNameResolution:
    """Resolve the run profile name and record where it came from.

    Resolution priority:
      1. ``ORCHO_PIPELINE`` env var — but only when ``allow_env_override``
         is True (fresh explicit start). It is a deliberate A/B knob for
         a brand-new run; on resume / follow-up the caller passes
         ``allow_env_override=False`` so the inherited durable profile
         (already resolved via ``resolve_resume_profile``) wins.
      2. ``profile_name`` argument (caller-provided).

    An env value equal to the requested name is a no-op and reports
    ``env_override_applied=False``; the override is recorded as applied
    only when it genuinely changed the resolved name.
    """
    env_name = os.environ.get("ORCHO_PIPELINE", "").strip()
    if allow_env_override and env_name:
        return _ProfileNameResolution(
            name=env_name,
            requested=profile_name,
            env_override_applied=env_name != profile_name,
        )
    return _ProfileNameResolution(
        name=profile_name,
        requested=profile_name,
        env_override_applied=False,
    )


def _resolve_profile_name(
    profile_name: str,
    *,
    allow_env_override: bool = True,
) -> str:
    """Phase 6: profile-name passthrough with gated env override.

    Thin string wrapper over :func:`_resolve_profile_name_provenance`.
    ``allow_env_override`` defaults to True (fresh explicit start);
    resume / follow-up callers pass False so an ambient ``ORCHO_PIPELINE``
    cannot silently override an inherited durable profile.

    Returns the resolved name string. Caller looks it up in the
    profile registry. Pre-Phase-6 this helper had a ``PipelineMode +
    do_plan → profile name`` matrix; that translation is gone — the
    CLI now takes profile names directly.
    """
    return _resolve_profile_name_provenance(
        profile_name=profile_name,
        allow_env_override=allow_env_override,
    ).name


def _resolve_v2_profile(
    *,
    profile_name: str,
    allow_env_override: bool = True,
):
    """Phase 6: load the v2 ``Profile`` instance for this run.

    Resolution priority:
      1. ``ORCHO_PIPELINE`` env var (only when ``allow_env_override``).
      2. ``profile_name`` argument.

    Returns the typed ``Profile`` instance or ``None`` when:
      * v2 profiles file missing (caller raises with diagnostic)
      * resolved name doesn't match a registered profile

    Profile registry/load errors are intentionally not swallowed. A malformed
    ``profiles_v2`` overlay or profile JSON is a startup contract failure and
    must surface with its original diagnostic; returning ``None`` would mislead
    the operator into thinking the requested profile is missing.

    Phase 5d step 4 invariant: ``run_pipeline`` raises if this returns
    None — there is no imperative path anymore. Phase 6 simplified the
    name resolution: no more ``PipelineMode + do_plan → name`` matrix.

    Phase 7c: switched from ``load_profiles_v2`` to
    ``load_profiles_v2_with_plugins`` so customer plugins shipping
    profiles via ``orcho.profiles`` entry_points are merged in
    alongside the shipped JSON registry. Plugin profiles can override
    shipped names (supported customer-overlay mechanism).
    """
    v2_path = CONFIG_DIR / "pipeline_profiles_v2.json"
    if not v2_path.exists():
        return None

    profiles = load_profiles_v2_with_plugins(v2_path)
    name = _resolve_profile_name(
        profile_name=profile_name,
        allow_env_override=allow_env_override,
    )
    return profiles.get(name)


def _raise_unresolved_profile(profile_name: str) -> NoReturn:
    """Fail loudly when a requested profile name does not resolve to a Profile.

    Splits the two failure modes the prior single ``RuntimeError`` conflated:

    * the v2 catalogue file is genuinely missing → a packaging / startup
      contract failure (``RuntimeError``);
    * the file exists but ``profile_name`` is not a registered name — e.g. a
      dead legacy name like ``advanced`` (removed when ``feature`` absorbed its
      recipe) — → a ``ValueError`` naming every available profile, so the
      operator sees exactly what to pick.

    There is deliberately NO silent fallback to a default here: a removed
    legacy / alias name must die loudly, never quietly revive as some other
    profile. ``profiles.get(name)`` returning ``None`` already proved the name
    is absent from the merged catalogue (shipped JSON + plugin overlays), so an
    alias table reviving ``advanced`` cannot exist without registering it.
    """
    v2_path = CONFIG_DIR / "pipeline_profiles_v2.json"
    if not v2_path.exists():
        raise RuntimeError(
            f"pipeline_profiles_v2.json not found at {v2_path}: the shipped "
            "profile catalogue is missing, so no profile can be resolved. This "
            "is a packaging / startup contract failure, not an unknown profile "
            "name."
        )
    available = ", ".join(sorted(load_profiles_v2_with_plugins(v2_path)))
    raise ValueError(
        f"Unknown pipeline profile {profile_name!r}. "
        f"Available profiles: {available}."
    )


def _profile_phase_names(profile) -> set[str]:
    """Flatten ``profile.steps`` (including LoopStep inner steps) to phase names."""
    from pipeline.runtime import LoopStep, PhaseStep
    names: set[str] = set()
    for entry in profile.steps:
        if isinstance(entry, LoopStep):
            names.update(s.phase for s in entry.steps)
        elif isinstance(entry, PhaseStep):
            names.add(entry.phase)
    return names


# Module-level constants consumed by ``_resolve_cross_handoff``.
# Single-use coupling, not a candidate for further extraction.
_VALID_PLAN_SOURCES = frozenset({"local", "cross", "none", "run"})
_HANDOFF_REQUIRED_PHASES = frozenset({"implement", "repair_changes"})


def _resolve_cross_handoff(
    *,
    profile,
    plan_source: str,
    handoff_path: str | None,
) -> str:
    """Validate ``plan_source`` / ``handoff_path`` against the projected
    profile and return the handoff body to inject into ``state.extras``.

    Rules:
      * ``plan_source`` must be one of ``local`` / ``cross`` / ``none``.
      * ``plan_source="cross"`` requires a non-empty ``handoff_path`` IFF
        the profile contains ``implement`` or ``repair_changes``. Review-
        only profiles run without a handoff.
      * ``handoff_path`` points at the canonical ``implementation_handoff
        .json`` (ADR 0050). The body the runtime consumes is *rendered
        from the typed object*, not read from a hand-authored markdown
        blob, so a stray source path cannot leak into the prompt. Returns
        the rendered body when present, empty string otherwise. Phase
        handlers consume it via ``state.extras["cross_handoff"]``.
    """
    if plan_source not in _VALID_PLAN_SOURCES:
        raise ValueError(
            f"plan_source must be one of {sorted(_VALID_PLAN_SOURCES)}, "
            f"got {plan_source!r}"
        )
    if plan_source != "cross":
        return ""

    needs_handoff = bool(
        _profile_phase_names(profile) & _HANDOFF_REQUIRED_PHASES
    )
    if not needs_handoff:
        return ""

    if not handoff_path:
        raise ValueError(
            "plan_source='cross' with implement/repair_changes phases "
            "requires a non-empty handoff_path"
        )
    p = Path(handoff_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(
            f"cross handoff artifact missing or not a file: {p}"
        )
    from pipeline.cross_project.handoff import load_handoff, render_handoff_markdown
    handoff = load_handoff(p)
    return render_handoff_markdown(handoff)


def _resolve_change_handoff(profile) -> str:
    """Resolve profile/config-owned code-change handoff strategy.

    Profile value wins when set; otherwise AppConfig.pipeline supplies the
    global default. Validation is typed here so a bad local config fails at
    run start instead of much later inside a prompt builder.
    """
    from pipeline.runtime import ChangeHandoffMode

    raw = getattr(profile, "change_handoff", None)
    if raw is None:
        raw = config.AppConfig.load().pipeline.get("change_handoff", "uncommitted")
    try:
        if isinstance(raw, ChangeHandoffMode):
            return raw.value
        return ChangeHandoffMode(str(raw)).value
    except ValueError:
        valid = ", ".join(m.value for m in ChangeHandoffMode)
        raise ValueError(
            f"Invalid change_handoff {raw!r}; expected one of: {valid}"
        ) from None


__all__ = [
    "ProfileSetup",
    "setup_profile",
    "_raise_unresolved_profile",
    "_resolve_profile_name",
    "_resolve_profile_name_provenance",
    "_resolve_v2_profile",
    "_profile_phase_names",
    "_resolve_cross_handoff",
    "_resolve_change_handoff",
]
