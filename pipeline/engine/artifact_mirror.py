"""
pipeline/engine/artifact_mirror.py — Optional mirror of run artifacts into
project repos for git-tracking.

Контекст. Каждый run pipeline-а пишет всё в ``<workspace>/runspace/runs/<ts>/``
— это canonical место. Опционально (по флагу config ``artifacts.mirror_to_project``)
можно копировать ТОЛЬКО семантические артефакты (план, todo, review, diff) в
``<project>/<mirror_dir>/``, чтобы их можно было закоммитить в репо проекта.

Низкоуровневое (output.log, checkpoints.db, metrics.json, progress.log,
meta.json) НИКОГДА не зеркалится — оно остаётся только в runspace/runs/.

Публичный API:
    mirror_to_projects(run_dir, projects, cfg) -> list[Path]

projects:
    None или {} → single-mode: пишем в каждый зарегистрированный проект
                  (caller передаёт {alias: project_dir}).
    {alias: Path} → cross-mode: для каждого alias-а ищем артефакты сначала
                    в ``run_dir/<alias>/``, потом fallback в ``run_dir/``.
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
    """Префикс с указанием источника. Применяем только к markdown — для
    бинарных или .patch файлов копируем как есть."""
    iso = _dt.datetime.now().isoformat(timespec="seconds")
    return _HEADER_TEMPLATE.format(source_rel=source_rel, iso_ts=iso) + content


def _copy_with_provenance(src: Path, dst: Path, source_rel: str) -> None:
    """Атомарно скопировать src → dst. Markdown-файлы получают header,
    остальное копируется как есть."""
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
    """Найти файлы которые надо зеркалить для конкретного project alias.

    Для cross-режима сначала смотрим ``run_dir/<alias>/`` (per-project
    артефакты), потом ``run_dir/`` (shared cross_plan.md, diff). Для
    single-режима alias=None — только run_dir/.

    Дедупликация по basename: если alias-specific plan.md уже найден,
    одноимённый файл из shared run_dir/ игнорируется (per-project выигрывает).
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
    """Скопировать matching-артефакты из run_dir в проектные mirror_dir-ы.

    Args:
        run_dir: Path к ``<workspace>/runspace/runs/<ts>/``.
        projects: ``{alias: project_dir}``. None / пустой dict → no-op
            (нет проектов для зеркалирования). Single-mode caller передаёт
            ``{"<basename>": project_dir}``.
        cfg: dict из ``AppConfig.artifacts``: keys mirror_to_project,
            mirror_patterns, mirror_dir.

    Returns:
        Список путей куда были записаны копии. Пустой список если
        mirror_to_project=False / нет источников / нет projects.
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
                # Mirror — best-effort, не валим pipeline из-за readonly fs.
                continue
    return written
