"""
pipeline/engine/artifact_mirror.py — Optional mirror of run artifacts into
project repos for git-tracking.

Context. Every pipeline run writes everything to
``<workspace>/runspace/runs/<ts>/`` — the canonical location. Optionally
(via the ``artifacts.mirror_to_project`` config flag) ONLY semantic
artifacts (plan, todo, review, diff) can be copied into
``<project>/<mirror_dir>/`` so they can be committed to the project repo.

Low-level output (output.log, checkpoints.db, metrics.json, progress.log,
meta.json) is NEVER mirrored — it stays in runspace/runs/ only.

Public API:
    mirror_to_projects(run_dir, projects, cfg) -> list[Path]

projects:
    None or {} → single-mode: write into every registered project
                 (the caller passes {alias: project_dir}).
    {alias: Path} → cross-mode: for each alias look for artifacts first
                    in ``run_dir/<alias>/``, then fall back to ``run_dir/``.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from collections.abc import Iterable
from pathlib import Path

_HEADER_TEMPLATE = (
    "<!-- mirrored from {source_rel} at {iso_ts} -->\n"
    "<!-- original artifact lives in workspace worktree; this copy is for git tracking -->\n\n"
)


def _inject_header(content: str, source_rel: str) -> str:
    """Prefix marking the source. Applied only to markdown — binary and
    .patch files are copied verbatim."""
    iso = _dt.datetime.now().isoformat(timespec="seconds")
    return _HEADER_TEMPLATE.format(source_rel=source_rel, iso_ts=iso) + content


def _copy_with_provenance(src: Path, dst: Path, source_rel: str) -> None:
    """Copy src → dst atomically. Markdown files get a header, everything
    else is copied verbatim."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in (".md", ".markdown"):
        try:
            text = src.read_text(encoding="utf-8")
            dst.write_text(_inject_header(text, source_rel), encoding="utf-8")
            return
        except (OSError, UnicodeDecodeError):
            pass
    shutil.copy2(src, dst)


def _resolve_sources(run_dir: Path, alias: str | None, patterns: Iterable[str]) -> list[Path]:
    """Find the files to mirror for a specific project alias.

    In cross mode look at ``run_dir/<alias>/`` first (per-project
    artifacts), then ``run_dir/`` (shared cross_plan.md, diff). In
    single mode alias=None — only run_dir/.

    Deduplicated by basename: once an alias-specific plan.md is found,
    a same-named file in the shared run_dir/ is ignored (per-project wins).
    """
    search_dirs: list[Path] = []
    if alias:
        search_dirs.append(run_dir / alias)
    search_dirs.append(run_dir)

    found: list[Path] = []
    seen_names: set[str] = set()
    for d in search_dirs:
        if not d.exists():
            continue
        for pattern in patterns:
            for match in sorted(d.glob(pattern)):
                if not match.is_file() or match.name in seen_names:
                    continue
                seen_names.add(match.name)
                found.append(match)
    return found


def mirror_to_projects(
    run_dir: Path,
    projects: dict[str, Path] | None,
    cfg: dict,
) -> list[Path]:
    """Copy matching artifacts from run_dir into the projects' mirror dirs.

    Args:
        run_dir: Path to ``<workspace>/runspace/runs/<ts>/``.
        projects: ``{alias: project_dir}``. None / empty dict → no-op
            (no projects to mirror into). A single-mode caller passes
            ``{"<basename>": project_dir}``.
        cfg: dict from ``AppConfig.artifacts``: keys mirror_to_project,
            mirror_patterns, mirror_dir.

    Returns:
        List of paths the copies were written to. Empty list when
        mirror_to_project=False / no sources / no projects.
    """
    if not cfg.get("mirror_to_project", False):
        return []
    if not projects:
        return []

    patterns = list(cfg.get("mirror_patterns") or [])
    if not patterns:
        return []
    mirror_dir = str(cfg.get("mirror_dir") or ".orcho/artifacts")

    is_cross = len(projects) > 1 or any(
        (run_dir / alias).is_dir() for alias in projects
    )
    written: list[Path] = []
    for alias, project_dir in projects.items():
        sources = _resolve_sources(
            run_dir, alias if is_cross else None, patterns,
        )
        for src in sources:
            dst = Path(project_dir) / mirror_dir / src.name
            try:
                source_rel = str(src.relative_to(run_dir.parent.parent.parent))
            except ValueError:
                source_rel = str(src)
            try:
                _copy_with_provenance(src, dst, source_rel)
                written.append(dst)
            except OSError:
                # Mirroring is best-effort; don't fail the pipeline over a readonly fs.
                continue
    return written
