"""
pipeline/engine/delivery_scope.py — delivery-scope classification + collection.

Focused home for delivery-scope enforcement (Stage C / T4). Keeps the
``resolve_commit_delivery`` body thin: that executor calls one hook here right
after it computes the run's own changed paths, and this module owns the two
clearly separated responsibilities.

**(A) Pure classification (no I/O).** :class:`DeliveryScopeAssessment` and
:func:`assess_delivery_scope` decide, from a ``DeliveryScope`` plus a per-alias
map of sibling-repo changes, whether mono delivery may proceed:

- ``expanded_mono`` with sibling changes → delivery proceeds, the sibling files
  are *disclosed* per alias (never a violation);
- ``strict_mono`` with sibling changes → a typed, reversible blocker
  (``delivery_scope_violation``) — never an exception;
- empty sibling changes (or ``cross``, which is not a mono-delivery concern) →
  in-scope / no-op.

**(B) Multi-repo collection (I/O, delivery time only).**
:func:`collect_sibling_changes` resolves each delivery-project alias to a repo
path through the workspace config, skips the primary (its diff is already what
``resolve_commit_delivery`` ships), and collects each sibling repo's dirty
files via :func:`core.io.git_helpers.git_changed_files`. Every failure degrades
softly — an unregistered / missing alias yields no entry and never crashes
delivery.

Provider-neutral throughout: aliases and repo paths are resolved through the
generic workspace alias config, with no provider-specific knowledge here.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.io.git_helpers import git_changed_files
from pipeline.engine.companion_scope import CompanionRepo
from pipeline.runtime.run_shape import DeliveryScope

# Typed, provider-neutral reason a strict-mono scope violation surfaces. Shared
# with the SDK delivery decision layer so the blocker string never drifts.
DELIVERY_SCOPE_VIOLATION = "delivery_scope_violation"


@dataclass(frozen=True, slots=True)
class DeliveryScopeAssessment:
    """Pure classification of a mono delivery against its declared scope.

    Fields
    ------
    scope:
        The resolved :class:`DeliveryScope` this delivery runs under.
    sibling_changes:
        Per-alias map ``alias → (``[alias]/rel/path``, ...)`` of changes found
        in sibling repos (the primary is already excluded). Empty when no
        sibling repo is dirty.
    affected_projects:
        Sorted tuple of sibling aliases that actually carry changes.
    in_scope:
        ``True`` when delivery may proceed without a scope blocker (no sibling
        changes, ``expanded_mono`` disclosure, or the not-applicable ``cross``
        case). ``False`` only for a strict-mono violation.
    blocked:
        ``True`` for a strict-mono violation — the caller parks a reversible,
        decidable gate. Never raises.
    blocker:
        The typed reason (:data:`DELIVERY_SCOPE_VIOLATION`) when ``blocked``;
        ``None`` otherwise.
    disclosure:
        Sorted, flattened tuple of every sibling path (``[alias]/rel``) — the
        per-alias disclosure surfaced for ``expanded_mono`` and carried on a
        strict-mono blocker so the operator sees exactly which sibling files
        triggered it.
    companions:
        Per-repo :class:`~pipeline.engine.companion_scope.CompanionRepo`
        enrichment (T1): one entry per declared companion repository carrying its
        alias, path, changed paths, and typed ``dirty|committed|planned_requirement``
        state. Empty for a clean single-repo mono run. This is the durable,
        plan-scope-derived companion disclosure; ``disclosure`` /
        ``sibling_changes`` stay the backward-compatible dirty-sibling string view
        that drives the strict/expanded gate.
    """

    scope: DeliveryScope
    sibling_changes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    affected_projects: tuple[str, ...] = ()
    in_scope: bool = True
    blocked: bool = False
    blocker: str | None = None
    disclosure: tuple[str, ...] = ()
    companions: tuple[CompanionRepo, ...] = ()


def assess_delivery_scope(
    *,
    scope: DeliveryScope,
    sibling_changes: Mapping[str, Sequence[str]],
    primary_alias: str = "",
    companions: Sequence[CompanionRepo] = (),
) -> DeliveryScopeAssessment:
    """Classify a mono delivery against ``scope`` (pure, no I/O).

    ``sibling_changes`` maps each *sibling* alias to its normalised changed
    paths; ``primary_alias`` (when known) is defensively dropped so the
    primary's own changes are never mistaken for a violation. Empty change
    lists are ignored. ``companions`` is the additive per-repo typed enrichment
    (T1) attached unchanged to every branch — it never alters the strict/expanded
    blocking decision, which stays driven by the dirty ``sibling_changes``.

    Branches:

    - no sibling changes → ``in_scope=True`` (no-op);
    - ``EXPANDED_MONO`` + changes → ``in_scope=True`` with ``disclosure``;
    - ``STRICT_MONO`` + changes → ``blocked=True`` /
      ``blocker=delivery_scope_violation`` (reversible, never raises);
    - ``CROSS`` → not applicable to a mono delivery → ``in_scope=True``.
    """
    if not isinstance(scope, DeliveryScope):
        scope = DeliveryScope(scope)

    cleaned: dict[str, tuple[str, ...]] = {}
    for alias, paths in sibling_changes.items():
        if not isinstance(alias, str) or not alias or alias == primary_alias:
            continue
        normalized = tuple(p for p in paths if isinstance(p, str) and p)
        if normalized:
            cleaned[alias] = normalized

    affected = tuple(sorted(cleaned))
    disclosure = tuple(sorted(p for paths in cleaned.values() for p in paths))
    companion_tuple = tuple(companions)

    if not cleaned:
        return DeliveryScopeAssessment(
            scope=scope,
            sibling_changes=cleaned,
            affected_projects=(),
            in_scope=True,
            companions=companion_tuple,
        )

    if scope is DeliveryScope.STRICT_MONO:
        return DeliveryScopeAssessment(
            scope=scope,
            sibling_changes=cleaned,
            affected_projects=affected,
            in_scope=False,
            blocked=True,
            blocker=DELIVERY_SCOPE_VIOLATION,
            disclosure=disclosure,
            companions=companion_tuple,
        )

    # EXPANDED_MONO discloses; CROSS is not a mono-delivery concern. Both let
    # delivery proceed — sibling edits are never a violation here.
    return DeliveryScopeAssessment(
        scope=scope,
        sibling_changes=cleaned,
        affected_projects=affected,
        in_scope=True,
        disclosure=disclosure,
        companions=companion_tuple,
    )


def collect_sibling_changes(
    *,
    delivery_projects: Sequence[str],
    primary_project_dir: Path,
    workspace: str | Path | None = None,
) -> dict[str, tuple[str, ...]]:
    """Collect dirty files from every *sibling* delivery-project repo (I/O).

    Resolves each alias in ``delivery_projects`` to a repo path through the
    workspace alias config (:func:`pipeline.project.project_aliases.resolve_project_alias`),
    skips the alias whose resolved path is the primary (its diff is already what
    ``resolve_commit_delivery`` ships), and returns each remaining sibling's
    changed files normalised to ``[alias]/rel/path``.

    Soft degradation is total: an alias that is unregistered, missing, or whose
    repo cannot be read yields no entry — never an exception, never a crashed
    delivery. ``git_changed_files`` itself never raises and returns ``[]`` for a
    clean tree or a non-repo.
    """
    primary = _safe_resolve(primary_project_dir)
    ws = _resolve_workspace(primary_project_dir, workspace)

    out: dict[str, tuple[str, ...]] = {}
    for alias in delivery_projects:
        if not isinstance(alias, str) or not alias.strip():
            continue
        repo = _resolve_alias_path(alias, ws)
        if repo is None:
            continue  # unregistered / missing alias → soft degrade
        if primary is not None and _safe_resolve(repo) == primary:
            continue  # primary repo — already covered by the run-owned diff
        try:
            files = git_changed_files(str(repo))
        except Exception:  # noqa: BLE001 — collection must never crash delivery
            files = []
        normalized = tuple(
            sorted(f"[{alias}]/{rel}" for rel in files if rel)
        )
        if normalized:
            out[alias] = normalized
    return out


def primary_alias_for(
    delivery_projects: Sequence[str],
    primary_project_dir: Path,
    *,
    workspace: str | Path | None = None,
) -> str:
    """Best-effort: the delivery-project alias whose repo is the primary.

    Returns ``""`` when no alias resolves to ``primary_project_dir`` (e.g. the
    primary is not itself a registered delivery project). Used only to pass a
    defensive ``primary_alias`` into :func:`assess_delivery_scope`.
    """
    primary = _safe_resolve(primary_project_dir)
    if primary is None:
        return ""
    ws = _resolve_workspace(primary_project_dir, workspace)
    for alias in delivery_projects:
        if not isinstance(alias, str) or not alias.strip():
            continue
        repo = _resolve_alias_path(alias, ws)
        if repo is not None and _safe_resolve(repo) == primary:
            return alias
    return ""


def evaluate_delivery_scope(
    *,
    session: Mapping[str, object],
    primary_project_dir: Path,
    run_dir: Path | None = None,
    workspace: str | Path | None = None,
) -> DeliveryScopeAssessment | None:
    """The thin hook ``resolve_commit_delivery`` calls — never raises.

    Reads ``delivery_scope`` / ``delivery_projects`` from
    ``session['auto_detect']`` (the durable ``meta.auto_detect`` block written
    by the auto-detect run). Returns ``None`` — i.e. *no enforcement, behaviour
    unchanged* — for any run that did not record a delivery scope (a manual
    explicit-mono run).

    Otherwise it derives the full **companion** set from the durable plan scope
    (``run_dir/parsed_plan.json``) unioned with ``delivery_projects`` (T1),
    classifies each companion repo's typed state, records any first-detection
    base revisions back into ``session['auto_detect']['companion_base_revisions']``
    (a durable signal for later committed observation), and classifies the
    strict/expanded gate from the *dirty* companions only — so the existing
    blocking semantics are byte-identical. Any unexpected failure degrades to
    ``None`` rather than crashing delivery.
    """
    try:
        auto = session.get("auto_detect") if isinstance(session, Mapping) else None
        if not isinstance(auto, Mapping):
            return None
        scope_raw = auto.get("delivery_scope")
        if not scope_raw:
            return None
        try:
            scope = DeliveryScope(scope_raw)
        except ValueError:
            return None

        from pipeline.engine.companion_scope import (
            CompanionRepoState,
            assess_companion_repos,
            derive_companion_aliases,
            derive_companion_declared_paths,
        )

        projects = tuple(
            p for p in (auto.get("delivery_projects") or ())
            if isinstance(p, str) and p
        )
        primary_alias = primary_alias_for(
            projects, primary_project_dir, workspace=workspace,
        )
        plan = _load_durable_plan(run_dir)
        known_aliases = _known_workspace_aliases(primary_project_dir, workspace)
        companion_aliases = derive_companion_aliases(
            plan=plan,
            delivery_projects=projects,
            known_aliases=known_aliases,
            primary_alias=primary_alias,
        )
        declared_paths = derive_companion_declared_paths(
            plan=plan,
            known_aliases=known_aliases,
            primary_alias=primary_alias,
        )
        recorded_bases = _recorded_companion_bases(auto)
        recorded_delivery = _recorded_companion_delivery(session)
        companions, new_bases = assess_companion_repos(
            companion_aliases=companion_aliases,
            primary_project_dir=primary_project_dir,
            workspace=workspace,
            recorded_bases=recorded_bases,
            recorded_delivery=recorded_delivery,
            declared_paths=declared_paths,
        )
        if new_bases:
            _persist_companion_bases(session, recorded_bases, new_bases)

        # The strict/expanded gate stays driven by the dirty companions only —
        # the committed / planned states are additive disclosure, never a block.
        sibling = {
            c.alias: c.changed_paths
            for c in companions
            if c.state is CompanionRepoState.DIRTY and c.changed_paths
        }
        return assess_delivery_scope(
            scope=scope,
            sibling_changes=sibling,
            primary_alias=primary_alias,
            companions=companions,
        )
    except Exception:  # noqa: BLE001 — the hook must never break delivery
        return None


def record_companion_bases_at_detection(
    *,
    session: MutableMapping[str, object],
    primary_project_dir: Path,
    run_dir: Path | None = None,
    workspace: str | Path | None = None,
) -> None:
    """Capture companion base revisions at first detection — never raises.

    The early-capture hook the run calls as soon as the durable plan scope exists
    (plan phase end), *before* the implementation phase can advance any companion
    HEAD. It derives the companion set from the durable plan scope ∪
    ``delivery_projects`` and records the current HEAD of each not-yet-recorded
    companion into ``session['auto_detect']['companion_base_revisions']`` (durable
    ``meta.auto_detect``). Recording the base *here* — rather than letting the
    delivery-time scan capture it — is what makes a companion that is cleanly
    committed *during* the run observably ``committed`` instead of mis-read as
    ``planned_requirement`` (the delivery-time scan would otherwise capture the
    already-advanced HEAD as the base).

    Idempotent: an alias whose base is already recorded is left untouched, so
    re-firing across plan rounds / resumes never moves a base forward. A run with
    no recorded ``delivery_scope`` or no derivable companion is a strict no-op.
    Persistence to ``meta.json`` is the caller's concern (this only mutates the
    in-memory session block); any failure degrades silently.
    """
    try:
        auto = session.get("auto_detect") if isinstance(session, Mapping) else None
        if not isinstance(auto, Mapping) or not auto.get("delivery_scope"):
            return
        from pipeline.engine.companion_scope import (
            capture_companion_bases,
            derive_companion_aliases,
        )

        projects = tuple(
            p for p in (auto.get("delivery_projects") or ())
            if isinstance(p, str) and p
        )
        primary_alias = primary_alias_for(
            projects, primary_project_dir, workspace=workspace,
        )
        companion_aliases = derive_companion_aliases(
            plan=_load_durable_plan(run_dir),
            delivery_projects=projects,
            known_aliases=_known_workspace_aliases(primary_project_dir, workspace),
            primary_alias=primary_alias,
        )
        if not companion_aliases:
            return
        recorded_bases = _recorded_companion_bases(auto)
        new_bases = capture_companion_bases(
            companion_aliases=companion_aliases,
            primary_project_dir=primary_project_dir,
            workspace=workspace,
            recorded_bases=recorded_bases,
        )
        if new_bases:
            _persist_companion_bases(session, recorded_bases, new_bases)
    except Exception:  # noqa: BLE001 — early capture must never break the run
        return


# ── internals ────────────────────────────────────────────────────────────────


def _load_durable_plan(run_dir: Path | None) -> object | None:
    """Load the durable ``ParsedPlan`` from ``run_dir/parsed_plan.json``.

    The plan artifact is the durable source of the per-plan / per-subtask scope
    (``owned_files`` / ``allowed_modifications``) — never the transcript. Returns
    ``None`` when ``run_dir`` is absent or the artifact is missing / corrupt, so
    derivation falls back to ``delivery_projects`` alone.
    """
    if run_dir is None:
        return None
    try:
        from pipeline.plan_artifacts import load_parsed_plan_artifact

        return load_parsed_plan_artifact(Path(run_dir))
    except Exception:  # noqa: BLE001 — missing / corrupt plan degrades softly
        return None


def _known_workspace_aliases(
    primary_project_dir: Path, workspace: str | Path | None,
) -> tuple[str, ...]:
    """Registered workspace alias names, for plan-scope companion recognition."""
    ws = _resolve_workspace(primary_project_dir, workspace)
    try:
        from pipeline.project.project_aliases import load_workspace_project_aliases

        return tuple(load_workspace_project_aliases(workspace=ws))
    except Exception:  # noqa: BLE001 — no config / unreadable → no known aliases
        return ()


def _recorded_companion_bases(auto: Mapping[str, object]) -> dict[str, str]:
    """Companion base revisions previously recorded in ``meta.auto_detect``."""
    raw = auto.get("companion_base_revisions")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(alias): str(sha)
        for alias, sha in raw.items()
        if isinstance(alias, str) and alias and isinstance(sha, str) and sha
    }


def _recorded_companion_delivery(session: Mapping[str, object]) -> dict[str, str]:
    """Recorded companion delivery commit shas, if any (``alias → commit_sha``).

    Reads an optional ``session['companion_delivery']`` block — a recorded
    per-companion delivery result. Absent in T1's mono flow, so this is normally
    empty; it lets a later companion delivery result mark a companion
    ``committed`` even before any base-vs-HEAD advance is observable.
    """
    raw = session.get("companion_delivery") if isinstance(session, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for alias, entry in raw.items():
        if not isinstance(alias, str) or not alias:
            continue
        sha = entry.get("commit_sha") if isinstance(entry, Mapping) else entry
        if isinstance(sha, str) and sha.strip():
            out[alias] = sha.strip()
    return out


def _persist_companion_bases(
    session: Mapping[str, object],
    recorded_bases: Mapping[str, str],
    new_bases: Mapping[str, str],
) -> None:
    """Write first-detection base revisions into durable ``meta.auto_detect``.

    Merges ``new_bases`` over the already-recorded ones under
    ``session['auto_detect']['companion_base_revisions']`` so the run's persisted
    ``meta.json`` carries the observable base each companion's later committed
    state is measured against. Best-effort: a non-mutable / unexpected session
    shape is left untouched (the assessment still carries the live classification).
    """
    auto = session.get("auto_detect") if isinstance(session, Mapping) else None
    if not isinstance(auto, dict):
        return
    merged = dict(recorded_bases)
    merged.update(new_bases)
    auto["companion_base_revisions"] = merged


def _safe_resolve(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _resolve_alias_path(alias: str, workspace: str | Path | None) -> Path | None:
    """Resolve one alias to a repo path; ``None`` on any failure."""
    try:
        from pipeline.project.project_aliases import resolve_project_alias

        return resolve_project_alias(alias, workspace=workspace)
    except Exception:  # noqa: BLE001 — unresolved alias degrades softly
        return None


def _resolve_workspace(
    primary_project_dir: str | Path, workspace: str | Path | None,
) -> str | Path | None:
    """Workspace dir for alias resolution.

    Explicit ``workspace`` wins; otherwise infer from the primary project's
    location, then fall back to the configured workspace root. ``None`` lets
    ``resolve_project_alias`` apply its own ``$ORCHO_WORKSPACE`` fallback.
    """
    if workspace is not None and str(workspace).strip():
        return workspace
    try:
        from pipeline.project.bootstrap import infer_workspace_from_project

        inferred = infer_workspace_from_project(str(primary_project_dir))
        if inferred is not None:
            return inferred
    except Exception:  # noqa: BLE001 — inference is best-effort
        pass
    try:
        from core.infra.config import get_workspace_dir

        return get_workspace_dir()
    except Exception:  # noqa: BLE001 — no workspace configured
        return None


__all__ = [
    "DELIVERY_SCOPE_VIOLATION",
    "DeliveryScopeAssessment",
    "assess_delivery_scope",
    "collect_sibling_changes",
    "evaluate_delivery_scope",
    "primary_alias_for",
    "record_companion_bases_at_detection",
]
