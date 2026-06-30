"""
core/prompt_loader.py — Three-level prompt template resolution.

Resolution chain (first match wins):
  1. {project_dir}/.orcho/multiagent/prompts/{name}.md  ← project override
  2. {workspace_dir}/.orcho/multiagent/prompts/{name}.md ← workspace override
  3. core package ``_prompts/{name}.md``                 ← core agnostic

Workspace is auto-detected by walking up from project_dir to find a parent
directory that contains `.orcho/multiagent/prompts/` AND is NOT the project
itself. This supports monorepo layouts where multiple projects share prompts.

Core prompts ship as package data of the ``core`` package and are resolved
via :func:`importlib.resources.files` so the loader works under both
editable installs and built wheels. Project/workspace prompts override or
extend core prompts with domain-specific context.

Usage:
    from core.io.prompt_loader import render_prompt

    # Without project override (pure core):
    text = render_prompt("architect_plan", task=task, ...)

    # With project-level override resolution:
    text = render_prompt("developer_build", project_dir=cwd, task=task, ...)

Template syntax: Python string.Template — $var or ${var}.
safe_substitute is used so Claude-facing placeholders like <short_name>
or unmatched $tokens are passed through as-is.
"""

from __future__ import annotations

import functools
from pathlib import Path
from string import Template

from core.infra.paths import PROMPTS_DIR as _CORE_PROMPTS

# Per-project / workspace prompts subpath (relative to root).
_PROMPTS_SUBPATH = Path(".orcho") / "multiagent" / "prompts"

# Markdown files that live alongside prompt parts but are not prompt parts:
# READMEs and agent/editor navigator instruction docs.
_NON_PROMPT_DOCS = frozenset({"README.md", "AGENTS.md", "CLAUDE.md"})

# Legacy alias kept for backward compat with external code that imports it.
_PROJECT_PROMPTS_SUBPATH = _PROMPTS_SUBPATH


@functools.cache
def _load_core(name: str) -> Template:
    """Load and cache a core prompt template. Raises if not found."""
    path = _CORE_PROMPTS / _template_path(name)
    if not path.exists():
        available = list_core_prompts()
        raise FileNotFoundError(
            f"Core prompt not found: {path}\n"
            f"Available: {available}"
        )
    return Template(path.read_text(encoding="utf-8"))


def _template_path(name: str) -> Path:
    """Convert a prompt name like ``roles/reviewer`` into a safe md path."""
    rel = Path(name)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"Invalid prompt name: {name!r}")
    return rel.with_suffix(".md")


def _load_from_dir(name: str, base: Path) -> Template | None:
    """Try to load a prompt override from a specific directory."""
    path = base / _PROMPTS_SUBPATH / _template_path(name)
    if path.exists():
        return Template(path.read_text(encoding="utf-8"))
    return None


def _find_workspace_dir(project_dir: Path) -> Path | None:
    """Walk up from project_dir to find a workspace root with prompt overrides.

    A workspace root is a parent directory (NOT the project itself) that
    contains `.orcho/multiagent/prompts/`. Stops at filesystem root.
    """
    current = project_dir.resolve().parent
    # Stop at filesystem root or after 10 levels (safety)
    for _ in range(10):
        if current == current.parent:
            break
        prompts_dir = current / _PROMPTS_SUBPATH
        if prompts_dir.is_dir():
            return current
        current = current.parent
    return None


def render_prompt(
    name: str,
    *,
    project_dir: str | Path | None = None,
    **variables: object,
) -> str:
    """Render prompt *name* with *variables*.

    Resolution chain (first match wins):
      1. Project override: {project_dir}/.orcho/multiagent/prompts/{name}.md
      2. Workspace override: auto-detected parent with prompt dir
      3. Core: _prompts/{name}.md

    project_dir is used both for file resolution AND injected as $project_dir
    template variable automatically (so templates don't need it passed twice).
    Uses safe_substitute so unknown $placeholders pass through to Claude.
    """
    tmpl: Template | None = None

    if project_dir is not None:
        pd = Path(project_dir)
        # Level 1: project override
        tmpl = _load_from_dir(name, pd)
        # Level 2: workspace override
        if tmpl is None:
            ws = _find_workspace_dir(pd)
            if ws is not None:
                tmpl = _load_from_dir(name, ws)
        # Inject $project_dir for template use
        variables.setdefault("project_dir", str(project_dir))

    # Level 3: core
    if tmpl is None:
        tmpl = _load_core(name)

    return tmpl.safe_substitute(variables).strip()


def raw_template(
    name: str,
    *,
    project_dir: str | Path | None = None,
) -> str:
    """Return the unsubstituted template body for *name*.

    Resolution chain matches :func:`render_prompt` (project →
    workspace → core, first match wins). Used by the prompt
    composer to inspect which substitution variables a template
    references — without actually rendering — so the resulting
    :class:`~pipeline.prompts.types.PromptPart` can declare the
    correct stability metadata for prefix/payload partitioning.
    """
    tmpl: Template | None = None
    if project_dir is not None:
        pd = Path(project_dir)
        tmpl = _load_from_dir(name, pd)
        if tmpl is None:
            ws = _find_workspace_dir(pd)
            if ws is not None:
                tmpl = _load_from_dir(name, ws)
    if tmpl is None:
        tmpl = _load_core(name)
    return tmpl.template


def reload_cache() -> None:
    """Clear the core template cache (useful in tests)."""
    _load_core.cache_clear()


def list_core_prompts() -> list[str]:
    """Return sorted list of available core prompt names."""
    return sorted(
        p.relative_to(_CORE_PROMPTS).with_suffix("").as_posix()
        for p in _CORE_PROMPTS.rglob("*.md")
        if p.name not in _NON_PROMPT_DOCS
    )


def list_project_prompts(project_dir: str | Path) -> list[str]:
    """Return sorted list of project-level prompt overrides."""
    d = Path(project_dir) / _PROMPTS_SUBPATH
    if not d.exists():
        return []
    return sorted(
        p.relative_to(d).with_suffix("").as_posix()
        for p in d.rglob("*.md")
        if p.name not in _NON_PROMPT_DOCS
    )


def list_workspace_prompts(project_dir: str | Path) -> list[str]:
    """Return sorted list of workspace-level prompt overrides."""
    ws = _find_workspace_dir(Path(project_dir))
    if ws is None:
        return []
    d = ws / _PROMPTS_SUBPATH
    if not d.exists():
        return []
    return sorted(
        p.relative_to(d).with_suffix("").as_posix()
        for p in d.rglob("*.md")
        if p.name not in _NON_PROMPT_DOCS
    )


def resolution_chain(
    name: str,
    project_dir: str | Path | None = None,
) -> list[tuple[str, Path, bool]]:
    """Return the full resolution chain for a prompt name.

    Returns list of (level, path, exists) tuples for debugging/docs.
    """
    chain: list[tuple[str, Path, bool]] = []

    if project_dir is not None:
        pd = Path(project_dir)
        p_path = pd / _PROMPTS_SUBPATH / _template_path(name)
        chain.append(("project", p_path, p_path.exists()))

        ws = _find_workspace_dir(pd)
        if ws is not None:
            w_path = ws / _PROMPTS_SUBPATH / _template_path(name)
            chain.append(("workspace", w_path, w_path.exists()))

    c_path = _CORE_PROMPTS / _template_path(name)
    chain.append(("core", c_path, c_path.exists()))

    return chain


def resolution_source(
    name: str,
    project_dir: str | Path | None = None,
) -> str | None:
    """Return the first resolution level that contains ``name``."""
    for level, _path, exists in resolution_chain(name, project_dir):
        if exists:
            return level
    return None


def has_prompt_override(
    name: str,
    project_dir: str | Path | None = None,
) -> bool:
    """Return true when project/workspace overrides define ``name``."""
    return resolution_source(name, project_dir) in {"project", "workspace"}
