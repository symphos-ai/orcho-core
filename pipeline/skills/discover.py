"""pipeline/skills/discover.py — Phase 7d-2: multi-source skill discovery.

Walks the six canonical sources in priority order and merges results
into one ``{name: SkillPackage}`` registry. Higher-priority sources
shadow lower-priority ones; conflicts are logged but never fatal.

Priority (highest first)
========================

1. ``<project>/.agents/skills/``    — project canonical (Agent Skills)
2. ``<project>/.claude/skills/``    — Claude Code compat (read-only)
3. ``<project>/.forge/skills/``     — Forge compat (read-only)
4. ``<workspace>/.agents/skills/``  — workspace shared
5. ``~/.agents/skills/``            — user-level shared
6. ``orcho.skills`` entry_points    — pip-distributable packages

Project locations always win — that's the customer-overlay contract.
Compat sources sit between project and workspace because they're
project-bound but not orcho-canonical.

Trust policy
============

The :class:`SkillTrustPolicy` gates which sources are allowed to load.
Defaults: package + user + workspace skills load (reasonably trusted —
package authors sign their wheels, the user controls their home dir).
Project + compat sources are OFF by default to defend autonomous runs
against malicious SKILL.md from cloned untrusted projects.

Untrusted sources are silently skipped (with a one-line diagnostic).
``include_untrusted=True`` overrides for ``orcho skills list --all``-
style introspection (Phase 7d-4 binding layer still refuses to
activate untrusted skills regardless).

Compat readers
==============

For now, ``.claude/skills/`` and ``.forge/skills/`` use the canonical
loader with a different ``source`` tag. Phase 7d-3 will land per-format
frontmatter quirks (Claude Code's ``allowed-tools`` shape, Forge's
``compatibility`` semantics, etc.) when fixtures are available. Until
then, packages that follow the open standard load through both shims
unchanged.

Entry-point packages
====================

The ``orcho.skills`` entry_points group ships :class:`SkillPackage`
instances directly (or zero-arg factories returning them). Plugin
authors who need bespoke loading (parsing legacy in-house formats,
fetching from a private registry) can register a factory there.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.entry_points import discover_entry_points
from pipeline.skills.loader import discover_skills_in_root
from pipeline.skills.types import SkillPackage, SkillTrustPolicy

__all__ = [
    "ENTRY_POINTS_GROUP",
    "discover_skills",
]


ENTRY_POINTS_GROUP = "orcho.skills"

# Canonical relative paths per source. Each pair is
# (subdirectory, source_label_used_in_SkillPackage.source).
_PROJECT_CANONICAL = (".agents/skills", "project")
_PROJECT_CLAUDE_COMPAT = (".claude/skills", "claude-compat")
_PROJECT_FORGE_COMPAT = (".forge/skills", "forge-compat")
_WORKSPACE_CANONICAL = (".agents/skills", "workspace")
_USER_AGENTS_RELATIVE = ".agents/skills"


def discover_skills(
    *,
    project_dir: Path | str,
    workspace_dir: Path | str,
    home_dir: Path | str | None = None,
    trust_policy: SkillTrustPolicy | None = None,
    include_untrusted: bool = False,
) -> dict[str, SkillPackage]:
    """Build the merged skill registry for one run.

    Args:
        project_dir: project root checkout (absolute or resolvable).
        workspace_dir: workspace root (where multiple projects may share
            skills via ``.agents/skills/``).
        home_dir: user home directory. Defaults to ``Path.home()``.
        trust_policy: per-source trust gate. Defaults to a fresh
            :class:`SkillTrustPolicy` (project / compat OFF).
        include_untrusted: if ``True``, untrusted sources are loaded
            anyway. Used by ``orcho skills list --all`` introspection;
            production runs leave this ``False`` and rely on Phase 7d-4
            binding-time enforcement.

    Returns:
        ``{name: SkillPackage}`` with priority resolution applied.
    """
    project = Path(project_dir)
    workspace = Path(workspace_dir)
    home = Path(home_dir) if home_dir is not None else Path.home()
    policy = trust_policy or SkillTrustPolicy()

    # Each layer is collected as ``(label, registry)`` and applied in
    # priority order. ``_apply_layer`` does the merging + conflict log.
    merged: dict[str, SkillPackage] = {}

    layers: list[tuple[str, dict[str, SkillPackage]]] = []

    # 1. Project canonical
    if _trust_or_warn("project", policy.trust_project, include_untrusted):
        sub, src = _PROJECT_CANONICAL
        layers.append(
            ("project", discover_skills_in_root(project / sub, source=src))
        )
        # 1b. Project canonical at the *git root* when it differs from the
        # registered project dir (nested ``git_dir`` — e.g. a Unity project
        # under SVN with the git repo at ``Assets/_Match-Three-Common``).
        # Skills conventionally live inside the tracked repo, so without this
        # they would be invisible unless the operator symlinks them up to the
        # SVN root. Additive + lower priority than the project-dir layer; a
        # no-op when the project dir already is the git root. (Same git_dir
        # awareness as commit_delivery / run_diff — shared ``resolve_git_root``.)
        # Lazy import: ``pipeline.engine.run_diff`` pulls a heavy graph that
        # would form an import cycle at ``pipeline.skills`` init time.
        from pipeline.engine.run_diff import resolve_git_root

        git_root = resolve_git_root(project)
        if git_root is not None and git_root != project:
            layers.append(
                (
                    "project-git",
                    discover_skills_in_root(git_root / sub, source=src),
                )
            )
    # 2. Project Claude Code compat
    if _trust_or_warn(
        "claude-compat", policy.trust_compat_claude, include_untrusted
    ):
        sub, src = _PROJECT_CLAUDE_COMPAT
        layers.append(
            (
                "claude-compat",
                discover_skills_in_root(project / sub, source=src),
            )
        )
    # 3. Project Forge compat
    if _trust_or_warn(
        "forge-compat", policy.trust_compat_forge, include_untrusted
    ):
        sub, src = _PROJECT_FORGE_COMPAT
        layers.append(
            (
                "forge-compat",
                discover_skills_in_root(project / sub, source=src),
            )
        )
    # 4. Workspace canonical
    if _trust_or_warn("workspace", policy.trust_workspace, include_untrusted):
        sub, src = _WORKSPACE_CANONICAL
        layers.append(
            ("workspace", discover_skills_in_root(workspace / sub, source=src))
        )
    # 5. User-level
    if _trust_or_warn("user", policy.trust_user, include_untrusted):
        layers.append(
            (
                "user",
                discover_skills_in_root(
                    home / _USER_AGENTS_RELATIVE, source="user",
                ),
            )
        )
    # 6. Entry-point packages (lowest priority — defaults from wheels)
    if _trust_or_warn("packages", policy.trust_packages, include_untrusted):
        layers.append(("packages", _discover_entry_point_skills()))

    for label, registry in layers:
        _apply_layer(merged, registry, label)

    return merged


# ── Internals ─────────────────────────────────────────────────────────


def _trust_or_warn(
    label: str, allowed: bool, include_untrusted: bool,
) -> bool:
    """Return ``True`` if the source layer should be scanned.

    When ``allowed`` is ``False`` and ``include_untrusted`` is ``False``
    we silently skip — the warning would fire on every run and these
    defaults are deliberate (project-skills off until the user opts in
    via ``--trust-project-skills``).
    """
    return bool(allowed or include_untrusted)


def _apply_layer(
    merged: dict[str, SkillPackage],
    registry: dict[str, SkillPackage],
    label: str,
) -> None:
    """Insert higher-priority layer entries into ``merged``.

    ``merged`` already holds the higher-priority layers. We only
    insert names not yet present; collisions are reported as the
    lower-priority entry being shadowed.
    """
    for name, pkg in sorted(registry.items()):
        if name in merged:
            existing = merged[name]
            print(
                f"  ! skills: {label} skill {name!r} ({pkg.source}) "
                f"shadowed by {existing.source} skill at "
                f"{existing.root_dir!s}"
            )
            continue
        merged[name] = pkg


def _discover_entry_point_skills() -> dict[str, SkillPackage]:
    """Load every ``orcho.skills`` entry into a registry.

    Each entry's value resolves to a :class:`SkillPackage` instance
    (or a zero-arg callable returning one). Entries that resolve to
    something else are logged + skipped — same shape as
    :func:`pipeline.profiles.loader.load_profiles_v2_with_plugins`.

    Path-style entries (e.g. ``"my_pkg.skills:SKILLS_DIR"`` resolving
    to a :class:`Path`) are also accepted: we run the canonical loader
    against the directory and fan out to every package inside, tagged
    ``source="package:<entry_name>"`` so the binding layer can audit
    provenance.
    """
    raw = discover_entry_points(ENTRY_POINTS_GROUP)
    out: dict[str, SkillPackage] = {}
    for entry_name, obj in sorted(raw.items()):
        for pkg in _coerce_entry_point_value(entry_name, obj):
            if pkg.name in out:
                print(
                    f"  ! skills: entry_points name collision {pkg.name!r} "
                    f"(second source: {entry_name!r}); keeping first"
                )
                continue
            out[pkg.name] = pkg
    return out


def _coerce_entry_point_value(
    entry_name: str, obj: Any,
) -> list[SkillPackage]:
    """Translate one entry-point value into a list of SkillPackages.

    Accepted shapes:
      * :class:`SkillPackage` instance — returned as-is.
      * :class:`Path` (or path-like) pointing at a directory containing
        ``<name>/SKILL.md`` packages — fanned out via
        :func:`discover_skills_in_root`.
      * Anything else — logged + skipped.
    """
    if isinstance(obj, SkillPackage):
        # Re-tag with provenance so audit trails identify the package
        # of origin even when the author left ``source="unknown"``.
        if obj.source == "unknown":
            from dataclasses import replace

            obj = replace(obj, source=f"package:{entry_name}")
        return [obj]

    if isinstance(obj, (str, Path)):
        path = Path(obj)
        if not path.is_dir():
            print(
                f"  ! orcho.skills entry {entry_name!r}: path {path!s} is "
                "not a directory; skipping"
            )
            return []
        # Tag every package with the entry name so audit traces back
        # to a specific wheel.
        registry = discover_skills_in_root(
            path, source=f"package:{entry_name}",
        )
        return list(registry.values())

    print(
        f"  ! orcho.skills entry {entry_name!r}: unexpected value type "
        f"{type(obj).__name__}; expected SkillPackage or Path"
    )
    return []
