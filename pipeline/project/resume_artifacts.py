"""Generic recovery of durable phase outputs for fresh-process resume.

On a checkpoint resume the pipeline can start in a brand-new process
(MCP / Web) *after* an earlier phase already persisted its durable
output to the run directory. The in-memory state from the original
launch is gone, and the resume path does not re-run the producing phase,
so any later phase that needs that output finds it missing unless it is
lifted back from disk first.

This module centralises that lift behind a small, tested registry of
:class:`ResumeArtifactSpec` entries. Today there is exactly one
production spec — ``parsed_plan`` — but the runner
(:func:`bootstrap_resume_artifacts`) is deliberately generic: it never
branches on a spec's name. It iterates specs, asks each whether it is
*required* for this resume (via ``required_when`` against a
:class:`BootstrapContext`), skips ones whose value is already in state,
loads the rest through the spec's own loader, and classifies the result
into one of six categories.

Generalization point
--------------------
Future phases register their own durable outputs here by appending a
spec to :data:`REGISTRY` (e.g. verification receipts, implement
evidence, a no-diff outcome, handoff decision context, prompt/session
receipts). Each spec owns: where its artifact lives (``artifact``), how
to load it (``load``, raising a domain error on missing/corrupt), how to
project it onto ``state`` (``project``), when it is required for a resume
(``required_when``), and whether state already carries it
(``already_present``). The *only* owned signal this stage exposes is
:data:`RESUME_PLAN_REQUIRED_KEY`; a future spec that needs a distinct
"required but absent" operator signal extends the marker contract rather
than re-deriving it from a string literal scattered across modules.

Marker contract
---------------
:data:`RESUME_PLAN_REQUIRED_KEY` is the single owner of the marker
name. The runner sets ``state.extras[RESUME_PLAN_REQUIRED_KEY] = True``
whenever a spec is required for this resume. At this stage there is one
required spec (``parsed_plan``), so the marker means "this resume needs
a parsed plan". The reader of the marker is the ``subtask_dag`` guard,
which turns ``parsed_plan is None`` + marker into an instructive
operator error (wired in a later subtask). Writers are this runner and
the handoff strip-plan sites; this module never reads the marker.

Strict no-op invariants
-----------------------
The runner never creates files or directories. A fresh run (no
artifact, not required) is a strict no-op: no marker, no provenance
mutation, no directory creation, and fresh generation never depends on
a stale artifact. Provenance is written to
``state.extras['resume_artifacts']`` only for a successful load
(``{'source': 'artifact'}``, via :func:`project_parsed_plan`) and for a
*required* failure (``{'status': 'missing'|'corrupt'}``) — optional
failures leave ``state.extras`` untouched so non-resume paths stay
byte-identical.

No markdown fallback: a corrupt ``parsed_plan.json`` is a ``corrupt_*``
category, never a silent degrade to reconstructing a partial plan from
the human markdown projection (see :mod:`pipeline.plan_artifacts`).

Imports of :mod:`pipeline.plan_artifacts` / :mod:`pipeline.plan_markdown`
are lazy inside functions so this module imports with no side effects
and without requiring any local CLI binaries.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Single owner of the marker name. ``state.extras[RESUME_PLAN_REQUIRED_KEY]``
# is a bool set by the runner / handoff sites and read by the subtask_dag
# guard. Do not re-spell this literal in other modules.
RESUME_PLAN_REQUIRED_KEY = "resume_plan_required"

# Provenance bucket inside ``state.extras``. Maps a spec name to a small
# status dict describing how its value was (or was not) recovered.
_RESUME_ARTIFACTS_EXTRAS_KEY = "resume_artifacts"

# The parsed-plan artifact filename under the run dir. Mirrors
# ``pipeline.plan_artifacts.LATEST_FILENAME``; kept as a literal here so
# this module needs no eager import of that module (lazy-import rule).
_PARSED_PLAN_FILENAME = "parsed_plan.json"


@dataclasses.dataclass(frozen=True)
class BootstrapContext:
    """Inputs a spec's ``required_when`` predicate may read.

    ``completed_phases`` is the set of phase names a resume must treat as
    already finished — the concrete, observable source of the required
    signal. ``run_dir`` is provided for specs that need to locate their
    artifact during the predicate; the runner also passes it to ``load``.
    """

    completed_phases: frozenset[str]
    run_dir: Path | None = None


@dataclasses.dataclass(frozen=True)
class ResumeArtifactSpec:
    """One durable phase output that fresh-process resume may recover.

    Fields:

    * ``name`` — stable spec identity; used as the provenance key.
    * ``phase`` — the producing phase (documentation / correlation).
    * ``artifact`` — the artifact filename under ``run_dir``. The runner
      uses ``run_dir / artifact`` to tell a *missing* file from a
      *corrupt* one after ``load`` raises — the only filesystem access
      the runner performs, and it is read-only.
    * ``load`` — ``(run_dir) -> value``; raises a domain error on
      missing/corrupt (for ``parsed_plan`` this is
      ``load_parsed_plan_artifact`` raising ``ParsedPlanArtifactError``).
    * ``project`` — ``(state, value) -> None``; seed the loaded value
      onto ``state`` (and record loaded-provenance).
    * ``required_when`` — ``(ctx) -> bool``; True when this artifact must
      be present for the resume to proceed.
    * ``already_present`` — ``(state) -> bool``; True when state already
      carries the value (an explicit ``--from-run-plan`` plan, or a
      same-process resume). The runner then skips without overwriting.
    """

    name: str
    phase: str
    artifact: str
    load: Callable[[Path], Any]
    project: Callable[[Any, Any], None]
    required_when: Callable[[BootstrapContext], bool]
    already_present: Callable[[Any], bool]


@dataclasses.dataclass
class ResumeBootstrapResult:
    """Per-spec outcome of :func:`bootstrap_resume_artifacts`.

    Each list holds spec ``name`` values. The six categories are
    mutually exclusive per spec. No category hardcodes any spec identity.
    """

    loaded: list[str] = dataclasses.field(default_factory=list)
    skipped_already_present: list[str] = dataclasses.field(default_factory=list)
    missing_optional: list[str] = dataclasses.field(default_factory=list)
    missing_required: list[str] = dataclasses.field(default_factory=list)
    corrupt_optional: list[str] = dataclasses.field(default_factory=list)
    corrupt_required: list[str] = dataclasses.field(default_factory=list)


def _write_provenance(state: Any, name: str, status: str) -> None:
    """Record a ``{'status': ...}`` provenance entry for a required failure."""
    bucket = state.extras.setdefault(_RESUME_ARTIFACTS_EXTRAS_KEY, {})
    bucket[name] = {"status": status}


def bootstrap_resume_artifacts(
    state: Any,
    run_dir: Path | None,
    *,
    completed_phases: frozenset[str],
    specs: tuple[ResumeArtifactSpec, ...] | None = None,
) -> ResumeBootstrapResult:
    """Recover durable phase outputs into ``state`` for a fresh-process resume.

    Generic over ``specs`` (defaults to :data:`REGISTRY`): the loop never
    branches on a spec's name. For each spec, in order:

    * ``run_dir is None`` short-circuits the whole call to an empty
      result with no state mutation (output-dir-less runs / fixtures);
    * compute ``required = spec.required_when(ctx)``; if required, set the
      owned marker ``state.extras[RESUME_PLAN_REQUIRED_KEY] = True``;
    * if ``spec.already_present(state)``, record ``skipped_already_present``
      and never overwrite;
    * otherwise call ``spec.load(run_dir)``: on success, ``spec.project``
      seeds state and the spec records loaded-provenance;
    * on a load failure, classify *missing* (artifact file absent) vs
      *corrupt* (file present but unreadable/invalid) and bucket it as
      ``{missing,corrupt}_{required,optional}``. Required failures also
      write ``{'status': ...}`` provenance; optional failures leave
      ``state.extras`` untouched.

    Never creates files or directories.
    """
    result = ResumeBootstrapResult()
    if run_dir is None:
        return result

    active_specs = REGISTRY if specs is None else specs
    ctx = BootstrapContext(completed_phases=completed_phases, run_dir=run_dir)

    for spec in active_specs:
        required = spec.required_when(ctx)
        if required:
            state.extras[RESUME_PLAN_REQUIRED_KEY] = True

        if spec.already_present(state):
            result.skipped_already_present.append(spec.name)
            continue

        try:
            value = spec.load(run_dir)
        except Exception:  # noqa: BLE001 — spec-domain load failure; classified below
            missing = not (run_dir / spec.artifact).is_file()
            if missing and required:
                result.missing_required.append(spec.name)
                _write_provenance(state, spec.name, "missing")
            elif missing:
                result.missing_optional.append(spec.name)
            elif required:
                result.corrupt_required.append(spec.name)
                _write_provenance(state, spec.name, "corrupt")
            else:
                result.corrupt_optional.append(spec.name)
            continue

        spec.project(state, value)
        result.loaded.append(spec.name)

    return result


def project_parsed_plan(state: Any, plan: Any) -> None:
    """Seed a recovered ``ParsedPlan`` onto ``state`` with loaded-provenance.

    Sets ``state.parsed_plan`` (canonical contract) and
    ``state.plan_markdown`` (deterministic projection), and stamps
    ``state.extras['resume_artifacts']['parsed_plan'] = {'source': 'artifact'}``.
    """
    from pipeline.plan_markdown import render_plan_markdown

    state.parsed_plan = plan
    state.plan_markdown = render_plan_markdown(plan)
    bucket = state.extras.setdefault(_RESUME_ARTIFACTS_EXTRAS_KEY, {})
    bucket["parsed_plan"] = {"source": "artifact"}


def load_and_project_parsed_plan(state: Any, run_dir: Path | None) -> bool:
    """Thin parsed-plan recovery helper for handoff strip-plan sites.

    Returns False (no-op) when the plan is already in state or
    ``run_dir`` is None. Loads ``parsed_plan.json``; on
    ``ParsedPlanArtifactError`` returns False without any markdown
    fallback (a corrupt artifact is surfaced by the downstream guard, not
    silently reconstructed from prose). On success projects the plan and
    returns True.

    This helper does NOT set :data:`RESUME_PLAN_REQUIRED_KEY` — the
    calling handoff site owns the marker for its strip-plan decision.
    """
    if getattr(state, "parsed_plan", None) is not None:
        return False
    if run_dir is None:
        return False

    from pipeline.plan_artifacts import (
        ParsedPlanArtifactError,
        load_parsed_plan_artifact,
    )

    try:
        plan = load_parsed_plan_artifact(run_dir)
    except ParsedPlanArtifactError:
        return False

    project_parsed_plan(state, plan)
    return True


def _load_parsed_plan_artifact(run_dir: Path) -> Any:
    """Spec loader for ``parsed_plan`` (lazy import keeps module side-effect-free)."""
    from pipeline.plan_artifacts import load_parsed_plan_artifact

    return load_parsed_plan_artifact(run_dir)


PARSED_PLAN_SPEC = ResumeArtifactSpec(
    name="parsed_plan",
    phase="plan",
    artifact=_PARSED_PLAN_FILENAME,
    load=_load_parsed_plan_artifact,
    project=project_parsed_plan,
    required_when=lambda ctx: "plan" in ctx.completed_phases,
    already_present=lambda s: getattr(s, "parsed_plan", None) is not None,
)

# The only production spec at this stage. Future phases append their own.
REGISTRY: tuple[ResumeArtifactSpec, ...] = (PARSED_PLAN_SPEC,)


__all__ = [
    "REGISTRY",
    "RESUME_PLAN_REQUIRED_KEY",
    "BootstrapContext",
    "PARSED_PLAN_SPEC",
    "ResumeArtifactSpec",
    "ResumeBootstrapResult",
    "bootstrap_resume_artifacts",
    "load_and_project_parsed_plan",
    "project_parsed_plan",
]
