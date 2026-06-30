"""
pipeline/engine/companion_scope.py — companion-repo derivation + per-repo state.

Focused home for the T1 companion-repo machinery layered onto the ADR 0102
delivery-scope axes. It carries the *new* responsibility the architecture
fitness gate forbids piling onto the already-large ``delivery_scope.py`` /
``commit_delivery.py`` bodies: deriving the set of mandatory companion
repositories from the **durable plan scope** and classifying each one's typed
state at delivery time. ``delivery_scope.evaluate_delivery_scope`` is the single
caller — it imports this module lazily so the package has no import cycle.

Three cleanly separated responsibilities:

**(A) Pure derivation (no I/O).** :func:`derive_companion_aliases` reads the
durable ``ParsedPlan`` scope (``owned_files`` / ``allowed_modifications`` at the
plan level and per subtask) plus ``meta.auto_detect.delivery_projects`` and
returns the set of companion aliases. Plan-scope references are recognised only
when they name a **registered workspace alias** (``known_aliases``), so a
``[subtask-id]`` tag or a primary-repo path never leaks in. No transcript
parsing — the plan artifact is the durable source.

**(B) Pure classification (no I/O).** :func:`classify_companion_state` maps the
observed signals to a typed :class:`CompanionRepoState`:

- ``dirty`` — the companion working tree has uncommitted changes (priority);
- ``committed`` — clean tree, but HEAD advanced past the recorded base revision
  over declared paths (commit-range diff) **or** a recorded companion delivery
  result names a ``commit_sha``;
- ``planned_requirement`` — declared by the plan, but neither dirty nor moved
  past its base.

This makes a clean-but-committed companion observably distinct from a
planned-but-untouched one — the durable base revision is the discriminator, not
a dirty-vs-clean heuristic.

**(C) Multi-repo assessment (I/O, delivery time only).**
:func:`assess_companion_repos` resolves each alias to a repo path, reuses
:func:`pipeline.engine.delivery_scope.collect_sibling_changes` as the dirty-path
collection base, reads the recorded base revision, computes the committed
commit-range via :func:`core.io.git_helpers.git_committed_files_since`, and
returns one :class:`CompanionRepo` per resolvable companion plus any newly
captured base revisions (so first detection records a durable base for later
committed observation). Every failure degrades softly: an unregistered / missing
alias yields no entry and never crashes delivery.

Provider-neutral throughout: aliases resolve through the generic workspace alias
config, with no provider-specific knowledge here.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from core.io.git_helpers import git_committed_files_since, git_head


class CompanionRepoState(StrEnum):
    """Typed delivery-time state of one declared companion repository.

    - ``DIRTY`` — uncommitted working-tree changes (the companion still needs a
      commit/follow-up before delivery is complete).
    - ``COMMITTED`` — clean working tree, but the companion HEAD advanced past
      its recorded base revision (or a recorded delivery result names a commit),
      i.e. the required companion edit landed as a real commit.
    - ``PLANNED_REQUIREMENT`` — declared by the durable plan scope, but neither
      dirty nor advanced past base: the required companion edit has not happened.
    """

    DIRTY = "dirty"
    COMMITTED = "committed"
    PLANNED_REQUIREMENT = "planned_requirement"


@dataclass(frozen=True, slots=True)
class CompanionRepo:
    """One companion repository's derived, classified delivery-time state.

    Fields
    ------
    alias:
        The registered workspace alias (e.g. ``"orcho-mcp"``).
    path:
        The resolved repo path (string for durable, JSON-safe serialisation).
    changed_paths:
        Normalised ``[alias]/rel`` paths that carry the change for this state —
        the dirty working-tree paths when ``DIRTY``, the committed commit-range
        paths when ``COMMITTED``, empty when ``PLANNED_REQUIREMENT``.
    state:
        The typed :class:`CompanionRepoState`.
    """

    alias: str
    path: str
    state: CompanionRepoState
    changed_paths: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Durable, JSON-safe view (enum → value). No transcript / patch text."""
        return {
            "alias": self.alias,
            "path": self.path,
            "state": self.state.value,
            "changed_paths": list(self.changed_paths),
        }


# ── (A) pure derivation ──────────────────────────────────────────────────────


# Split a scope reference into candidate path/alias tokens. Brackets (an
# ``[alias]`` reference or a ``[subtask-id]`` tag) become separators alongside
# whitespace and path separators, so both ``[orcho-mcp]/read.py`` and
# ``../orcho-mcp/**`` surface ``orcho-mcp`` as a token. A token is only kept as a
# companion when it matches a *registered* alias, so subtask tags never leak in.
_TOKEN_SPLIT_RE = re.compile(r"[\s/\[\]]+")


def _candidate_tokens(text: str) -> set[str]:
    if not isinstance(text, str) or not text:
        return set()
    return {tok for tok in _TOKEN_SPLIT_RE.split(text) if tok and tok != ".."}


def _plan_scope_strings(plan: Any) -> tuple[str, ...]:
    """All durable plan-scope reference strings: plan- and subtask-level.

    Reads ``owned_files`` / ``allowed_modifications`` from the ``ParsedPlan`` and
    each of its ``subtasks``. Defensive: any missing attribute is skipped, so a
    partial / unexpected plan shape degrades to fewer references rather than
    raising.
    """
    refs: list[str] = []

    def _extend(obj: Any) -> None:
        for attr in ("owned_files", "allowed_modifications"):
            values = getattr(obj, attr, None) or ()
            for value in values:
                if isinstance(value, str) and value:
                    refs.append(value)

    if plan is not None:
        _extend(plan)
        for subtask in getattr(plan, "subtasks", None) or ():
            _extend(subtask)
    return tuple(refs)


def derive_companion_aliases(
    *,
    plan: Any,
    delivery_projects: Sequence[str],
    known_aliases: Sequence[str],
    primary_alias: str = "",
) -> tuple[str, ...]:
    """Derive the companion-alias set from durable plan scope ∪ delivery_projects.

    ``delivery_projects`` (the ``meta.auto_detect`` cross-recommendation aliases)
    are taken verbatim — they are declared aliases by construction. Plan-scope
    references contribute an alias only when a token matches a registered
    ``known_aliases`` entry, which filters out ``[subtask-id]`` tags and
    primary-repo paths. The ``primary_alias`` is always excluded: the primary's
    own diff is what mono delivery already ships. Pure — no I/O.
    """
    known = {a for a in known_aliases if isinstance(a, str) and a}
    aliases: set[str] = set()
    for project in delivery_projects:
        if isinstance(project, str) and project.strip():
            aliases.add(project.strip())
    for ref in _plan_scope_strings(plan):
        for token in _candidate_tokens(ref):
            if token in known:
                aliases.add(token)
    aliases.discard("")
    if primary_alias:
        aliases.discard(primary_alias)
    return tuple(sorted(aliases))


def _declared_rels_for_alias(ref: str, alias: str) -> set[str]:
    """Relative path(s) a single plan-scope reference declares for ``alias``.

    Splits the reference into whitespace chunks (a subtask-tag prefix such as
    ``[T6-mcp-parity] ../orcho-mcp/**`` carries the tag and the path as separate
    chunks), normalises ``[`` / ``]`` to path separators, drops ``..`` and empty
    segments, and — for any chunk that names ``alias`` — returns everything after
    the alias as the declared relative path. ``[orcho-mcp]/server.py`` and
    ``../orcho-mcp/server.py`` both yield ``server.py``; ``../orcho-mcp/**``
    yields ``**``. A chunk that does not name ``alias`` contributes nothing.
    """
    rels: set[str] = set()
    for chunk in ref.split():
        norm = chunk.replace("[", "/").replace("]", "/")
        parts = [p for p in norm.split("/") if p and p != ".."]
        if alias in parts:
            rel = "/".join(parts[parts.index(alias) + 1:])
            if rel:
                rels.add(rel)
    return rels


def derive_companion_declared_paths(
    *,
    plan: Any,
    known_aliases: Sequence[str],
    primary_alias: str = "",
) -> dict[str, tuple[str, ...]]:
    """Per-alias declared relative paths from the durable plan scope (pure).

    Mirrors :func:`derive_companion_aliases` but keeps the *paths* each plan-scope
    reference declares per companion alias, so committed-state classification can
    require that a companion's HEAD advance actually touched a **declared** path
    (not any unrelated commit). Returns ``{alias: (rel, ...)}`` for every
    registered, non-primary alias referenced with at least one path; an alias that
    only appears via ``delivery_projects`` (no plan-scope path) is absent here, so
    the caller keeps the path-agnostic committed semantics for it.
    """
    known = {a for a in known_aliases if isinstance(a, str) and a}
    out: dict[str, set[str]] = {}
    for ref in _plan_scope_strings(plan):
        for token in _candidate_tokens(ref):
            if token in known and token != primary_alias:
                rels = _declared_rels_for_alias(ref, token)
                if rels:
                    out.setdefault(token, set()).update(rels)
    return {alias: tuple(sorted(rels)) for alias, rels in out.items()}


def _path_is_declared(rel: str, patterns: Sequence[str]) -> bool:
    """True when committed path ``rel`` matches any declared path ``patterns``.

    Supports the plan-scope reference shapes: an exact file
    (``src/orcho_mcp/x.py``), a ``**`` / ``*`` wildcard (whole-repo or glob), and
    a directory-prefix declaration (``src/orcho_mcp`` or ``src/orcho_mcp/**``
    covers nested files). Pure string matching — never touches the filesystem.
    """
    for raw in patterns:
        pat = str(raw).strip().rstrip("/")
        if not pat:
            continue
        if pat in ("**", "*"):
            return True
        if fnmatch.fnmatch(rel, pat):
            return True
        base = pat.rstrip("*").rstrip("/")
        if base and (rel == base or rel.startswith(base + "/")):
            return True
    return False


# ── (B) pure classification ──────────────────────────────────────────────────


def classify_companion_state(
    *,
    changed_files: Sequence[str],
    committed_files: Sequence[str] = (),
    recorded_delivery_commit: str | None = None,
    base_revision: str | None = None,
) -> CompanionRepoState:
    """Classify one companion from observed signals (priority order, pure).

    1. ``changed_files`` (dirty working tree) → ``DIRTY``.
    2. a recorded companion delivery result with a ``commit_sha`` → ``COMMITTED``.
    3. a recorded ``base_revision`` whose commit-range touched declared paths
       (``committed_files``) → ``COMMITTED``.
    4. otherwise → ``PLANNED_REQUIREMENT``.

    The ``base_revision`` + ``committed_files`` pair is the observable durable
    signal that makes a clean-but-committed companion distinct from a
    planned-but-untouched one — never a dirty-vs-clean heuristic.
    """
    if any(f for f in changed_files if f):
        return CompanionRepoState.DIRTY
    if isinstance(recorded_delivery_commit, str) and recorded_delivery_commit.strip():
        return CompanionRepoState.COMMITTED
    if (
        base_revision
        and str(base_revision).strip()
        and any(f for f in committed_files if f)
    ):
        return CompanionRepoState.COMMITTED
    return CompanionRepoState.PLANNED_REQUIREMENT


# ── (C) multi-repo assessment (I/O) ──────────────────────────────────────────


def capture_companion_bases(
    *,
    companion_aliases: Sequence[str],
    primary_project_dir: Path,
    workspace: str | Path | None,
    recorded_bases: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Capture each not-yet-recorded companion's current HEAD as its base (I/O).

    The **first-detection** base-capture used at run start — as soon as the
    companion set is derivable from the durable plan scope, before the run's
    implementation phase can advance any companion HEAD. Returns ``{alias: sha}``
    for every resolvable companion alias that has no recorded base yet; the caller
    persists it into durable ``meta.auto_detect.companion_base_revisions`` so a
    later delivery-time scan measures a committed advance against the *pre-work*
    revision instead of a HEAD that already moved. Capturing here (not at delivery)
    is what keeps a companion cleanly committed *during* the run observably
    ``committed`` rather than mis-read as ``planned_requirement``.

    An unregistered / missing alias, or the primary alias itself, is skipped —
    never an exception.
    """
    from pipeline.engine.delivery_scope import (
        _resolve_alias_path,
        _resolve_workspace,
        _safe_resolve,
    )

    ws = _resolve_workspace(primary_project_dir, workspace)
    primary = _safe_resolve(primary_project_dir)
    bases = dict(recorded_bases or {})

    new_bases: dict[str, str] = {}
    for alias in sorted({a for a in companion_aliases if isinstance(a, str) and a}):
        if alias in bases:
            continue  # base already recorded at an earlier detection
        repo = _resolve_alias_path(alias, ws)
        if repo is None:
            continue  # unregistered / missing alias → soft degrade
        if primary is not None and _safe_resolve(repo) == primary:
            continue  # the primary repo is never its own companion
        head = git_head(repo)
        if head:
            new_bases[alias] = head
    return new_bases


def assess_companion_repos(
    *,
    companion_aliases: Sequence[str],
    primary_project_dir: Path,
    workspace: str | Path | None,
    recorded_bases: Mapping[str, str] | None = None,
    recorded_delivery: Mapping[str, str] | None = None,
    declared_paths: Mapping[str, Sequence[str]] | None = None,
) -> tuple[tuple[CompanionRepo, ...], dict[str, str]]:
    """Assess every resolvable companion repo's typed state (I/O, delivery time).

    Returns ``(companions, new_bases)`` where ``companions`` is one
    :class:`CompanionRepo` per resolvable alias and ``new_bases`` maps each alias
    whose base revision was *not* already recorded to the HEAD captured now — so
    the caller can persist it into durable meta as the base for later committed
    observation (first-detection capture).

    Dirty paths reuse :func:`pipeline.engine.delivery_scope.collect_sibling_changes`
    (the same ``git_changed_files`` normalisation the strict/expanded gate uses).
    Committed paths come from the recorded base revision via
    :func:`core.io.git_helpers.git_committed_files_since`, then — when
    ``declared_paths`` carries plan-scope paths for the alias — are filtered to
    only the files the plan actually declared for that companion. This is what
    keeps a companion ``committed`` *only* when its HEAD advance touched a declared
    path: an unrelated commit (e.g. a docs tweak) over a companion whose required
    edit never landed stays ``planned_requirement``. An alias with no declared
    paths (it surfaced solely via ``delivery_projects``) keeps the path-agnostic
    behaviour — any committed file counts. An unregistered / missing alias, or the
    primary alias itself, yields no entry — never an exception.
    """
    # Lazy import avoids an engine import cycle (delivery_scope imports this
    # module lazily inside ``evaluate_delivery_scope``).
    from pipeline.engine.delivery_scope import (
        _resolve_alias_path,
        _resolve_workspace,
        _safe_resolve,
        collect_sibling_changes,
    )

    ws = _resolve_workspace(primary_project_dir, workspace)
    primary = _safe_resolve(primary_project_dir)
    bases = dict(recorded_bases or {})
    delivery = recorded_delivery or {}
    declared = declared_paths or {}

    unique_aliases = tuple(
        sorted({a for a in companion_aliases if isinstance(a, str) and a})
    )
    dirty_map = collect_sibling_changes(
        delivery_projects=unique_aliases,
        primary_project_dir=primary_project_dir,
        workspace=ws,
    )

    companions: list[CompanionRepo] = []
    new_bases: dict[str, str] = {}
    for alias in unique_aliases:
        repo = _resolve_alias_path(alias, ws)
        if repo is None:
            continue  # unregistered / missing alias → soft degrade
        if primary is not None and _safe_resolve(repo) == primary:
            continue  # the primary repo is never its own companion

        dirty = dirty_map.get(alias, ())
        base = bases.get(alias)
        if not base:
            # First detection: capture the current HEAD as the durable base so a
            # later delivery can observe a committed advance against it.
            head = git_head(repo)
            if head:
                new_bases[alias] = head

        committed_files: tuple[str, ...] = ()
        if base:
            committed = [rel for rel in git_committed_files_since(str(repo), base) if rel]
            alias_declared = declared.get(alias)
            if alias_declared:
                # Plan declared specific companion paths: a HEAD advance only
                # marks ``committed`` when it actually touched one of them, so an
                # unrelated commit cannot mask an undelivered required edit.
                committed = [
                    rel for rel in committed
                    if _path_is_declared(rel, alias_declared)
                ]
            committed_files = tuple(
                sorted(f"[{alias}]/{rel}" for rel in committed)
            )

        state = classify_companion_state(
            changed_files=dirty,
            committed_files=committed_files,
            recorded_delivery_commit=delivery.get(alias),
            base_revision=base,
        )
        if state is CompanionRepoState.DIRTY:
            changed_paths = tuple(dirty)
        elif state is CompanionRepoState.COMMITTED:
            changed_paths = committed_files
        else:
            changed_paths = ()
        companions.append(
            CompanionRepo(
                alias=alias,
                path=str(repo),
                state=state,
                changed_paths=changed_paths,
            )
        )
    return tuple(companions), new_bases


__all__ = [
    "CompanionRepo",
    "CompanionRepoState",
    "assess_companion_repos",
    "capture_companion_bases",
    "classify_companion_state",
    "derive_companion_aliases",
    "derive_companion_declared_paths",
]
