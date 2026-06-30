"""pipeline/skills/migrate.py — Phase 7d-5: legacy skills layout migration.

Translates the legacy flat layout::

    <project>/.agent/multiagent/skills/<name>.md

into the Agent Skills R9 directory layout::

    <project>/.agents/skills/<slug>/SKILL.md

Frontmatter rewrites
====================

The legacy schema carried orcho-specific routing keys at the top level
that R9 explicitly rejects (skills must not select runtime, model, or
provider — that's execution policy owned by per-phase runtime config
and explicit ``PhaseStep.overrides``). The migration drops the offending
keys and hoists the routing hint that *is* still relevant:

==============  ==============================================
``model``       dropped (R9: skill never selects model)
``provider``    dropped (R9: skill never selects runtime)
``files_pattern`` → ``metadata.orcho.file_patterns``
``prompt_extra`` → appended to the SKILL.md body as a final section
                ("## Notes\\n\\n<extra>") so the content survives;
                authors can re-shape it later
==============  ==============================================

A migration report (``LegacySkillMigrationReport``) records every
file: written, skipped, or failed — so callers / CI can audit the
result. The migration is **idempotent**: running it twice with the
same source produces the same destination (the writer overwrites
existing SKILL.md only when ``overwrite=True``).

The legacy directory is left untouched by default. ``delete_legacy=True``
removes the original files after a successful write — opt-in because
deletion is irreversible without git.

This module is purely a one-shot migration aid; once a project has
been migrated it should not be needed again. Phase 7d-6 retires the
legacy loader entirely, at which point this script becomes the
canonical "your skills won't load until you migrate" instruction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.skills.loader import SkillParseError, parse_skill_md

__all__ = [
    "LEGACY_RELATIVE_DIR",
    "TARGET_RELATIVE_DIR",
    "DEPRECATED_KEYS",
    "LegacySkillMigrationReport",
    "MigratedSkill",
    "migrate_legacy_skills",
]


# Source / destination roots (relative to project_dir).
LEGACY_RELATIVE_DIR = ".agent/multiagent/skills"
TARGET_RELATIVE_DIR = ".agents/skills"

# Frontmatter keys the migration drops (R9: skills don't select runtime).
DEPRECATED_KEYS: tuple[str, ...] = ("model", "provider")


# Slug rule mirrors loader._slugify so output filenames match what the
# canonical loader will discover after migration.
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


@dataclass(frozen=True)
class MigratedSkill:
    """Per-file migration record."""
    legacy_path: Path
    target_dir: Path
    target_skill_md: Path
    skill_name: str
    dropped_keys: tuple[str, ...]
    moved_keys: tuple[tuple[str, str], ...]   # (old, new) pairs
    body_appended: bool                       # prompt_extra → body section


@dataclass
class LegacySkillMigrationReport:
    """Aggregate result of a migration run."""
    project_dir: Path
    legacy_dir: Path
    target_dir: Path
    written: list[MigratedSkill] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (path, reason)
    failed: list[tuple[Path, str]] = field(default_factory=list)   # (path, error msg)
    deleted_legacy: list[Path] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """``True`` when every legacy file resolved (written or skipped)."""
        return not self.failed


# ── Public API ────────────────────────────────────────────────────────


def migrate_legacy_skills(
    project_dir: Path | str,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    delete_legacy: bool = False,
) -> LegacySkillMigrationReport:
    """Migrate every legacy ``*.md`` skill into the new directory layout.

    Args:
        project_dir: project root containing
            ``.agent/multiagent/skills/*.md``.
        dry_run: when ``True`` no filesystem writes happen; the report
            still describes every translation that *would* occur. Useful
            for ``orcho skills migrate --dry-run`` previews.
        overwrite: when ``True`` an existing
            ``.agents/skills/<slug>/SKILL.md`` is replaced. Default
            ``False`` skips collisions (records as ``skipped``) so a
            partial re-run doesn't trample manual edits authors may
            have made post-initial-migration.
        delete_legacy: when ``True`` the original ``*.md`` is removed
            after a successful write. Off by default — the legacy file
            is harmless to keep, and deletion is irreversible without
            git.

    Returns:
        :class:`LegacySkillMigrationReport` with per-file records.

    Never raises — per-file failures are captured in ``report.failed``
    so a bad legacy skill doesn't abort migration of its siblings.
    """
    project = Path(project_dir).resolve()
    legacy_dir = project / LEGACY_RELATIVE_DIR
    target_dir = project / TARGET_RELATIVE_DIR

    report = LegacySkillMigrationReport(
        project_dir=project,
        legacy_dir=legacy_dir,
        target_dir=target_dir,
    )

    if not legacy_dir.is_dir():
        return report

    for legacy_path in sorted(legacy_dir.glob("*.md")):
        try:
            record = _migrate_one(
                legacy_path=legacy_path,
                target_dir=target_dir,
                dry_run=dry_run,
                overwrite=overwrite,
            )
        except _MigrationSkipped as exc:
            report.skipped.append((legacy_path, str(exc)))
            continue
        except SkillParseError as exc:
            report.failed.append((legacy_path, f"parse error: {exc}"))
            continue
        except Exception as exc:  # noqa: BLE001 — defensive
            report.failed.append((legacy_path, f"{type(exc).__name__}: {exc}"))
            continue

        report.written.append(record)

        if delete_legacy and not dry_run:
            try:
                legacy_path.unlink()
                report.deleted_legacy.append(legacy_path)
            except OSError as exc:
                report.failed.append(
                    (legacy_path, f"delete after write failed: {exc}")
                )

    return report


# ── Internals ─────────────────────────────────────────────────────────


class _MigrationSkipped(Exception):
    """Raised internally when a file is skipped (collision, empty name).

    Caught in :func:`migrate_legacy_skills` and recorded under
    ``report.skipped`` rather than ``failed`` — these aren't errors,
    they're outcomes.
    """


def _migrate_one(
    *,
    legacy_path: Path,
    target_dir: Path,
    dry_run: bool,
    overwrite: bool,
) -> MigratedSkill:
    """Translate one legacy file into the new layout.

    Raises :class:`_MigrationSkipped` for skip outcomes and
    :class:`SkillParseError` for parse failures (caught upstream).
    """
    raw_text = legacy_path.read_text(encoding="utf-8")
    frontmatter, body = parse_skill_md(raw_text)

    raw_name = str(frontmatter.get("name") or "").strip() or legacy_path.stem
    slug = _slugify(raw_name)
    if not slug:
        raise _MigrationSkipped("skill name empty after slug-normalisation")

    description = str(frontmatter.get("description") or "").strip()
    if not description:
        raise _MigrationSkipped(
            "missing description (Agent Skills mandates it; manual fix needed)"
        )

    # Plan the rewrite.
    dropped: list[str] = [
        key for key in DEPRECATED_KEYS if key in frontmatter
    ]
    moved: list[tuple[str, str]] = []
    body_appended = False

    new_frontmatter: dict[str, Any] = {
        "name": slug,
        "description": description,
    }
    # Preserve any keys we don't recognise but didn't drop (license,
    # compatibility, allowed-tools, etc. — Agent Skills standard).
    for key, value in frontmatter.items():
        if key in {"name", "description"}:
            continue
        if key in DEPRECATED_KEYS:
            continue
        if key == "files_pattern":
            new_frontmatter.setdefault("metadata", {}).setdefault("orcho", {})[
                "file_patterns"
            ] = value
            moved.append(("files_pattern", "metadata.orcho.file_patterns"))
            continue
        if key == "prompt_extra":
            # Defer body splice until we've finalised the body content.
            continue
        new_frontmatter[key] = value

    # Body splice: legacy `prompt_extra` becomes a "## Notes" section
    # appended to the body so authoring intent survives the schema
    # tightening.
    new_body = body.rstrip()
    prompt_extra = str(frontmatter.get("prompt_extra") or "").strip()
    if prompt_extra:
        if new_body:
            new_body = new_body + "\n\n## Notes\n\n" + prompt_extra
        else:
            new_body = "## Notes\n\n" + prompt_extra
        body_appended = True

    target_skill_dir = target_dir / slug
    target_skill_md = target_skill_dir / "SKILL.md"

    if target_skill_md.exists() and not overwrite:
        raise _MigrationSkipped(
            f"target {target_skill_md!s} exists (rerun with overwrite=True)"
        )

    if not dry_run:
        target_skill_dir.mkdir(parents=True, exist_ok=True)
        target_skill_md.write_text(
            _format_skill_md(new_frontmatter, new_body), encoding="utf-8",
        )

    return MigratedSkill(
        legacy_path=legacy_path,
        target_dir=target_skill_dir,
        target_skill_md=target_skill_md,
        skill_name=slug,
        dropped_keys=tuple(dropped),
        moved_keys=tuple(moved),
        body_appended=body_appended,
    )


def _slugify(name: str) -> str:
    """Slug rule consistent with :func:`pipeline.skills.loader._slugify`."""
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")


def _format_skill_md(frontmatter: dict[str, Any], body: str) -> str:
    """Re-emit a SKILL.md file from the rewritten frontmatter + body.

    Uses a small YAML emitter (mirroring the loader's stdlib subset)
    so the output round-trips through :func:`parse_skill_md`. Output
    is deterministic for migration tests: keys appear in canonical
    order (``name``, ``description``, then sorted others, then
    ``metadata`` last).
    """
    lines = ["---"]
    canonical_order = ["name", "description"]
    seen: set[str] = set()
    for key in canonical_order:
        if key in frontmatter:
            lines.extend(_emit_yaml_pair(key, frontmatter[key]))
            seen.add(key)
    others = sorted(k for k in frontmatter if k not in seen and k != "metadata")
    for key in others:
        lines.extend(_emit_yaml_pair(key, frontmatter[key]))
        seen.add(key)
    if "metadata" in frontmatter:
        lines.extend(_emit_yaml_pair("metadata", frontmatter["metadata"]))
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def _emit_yaml_pair(key: str, value: Any, indent: int = 0) -> list[str]:
    """Emit one key/value pair in the loader's YAML subset."""
    pad = " " * indent
    if isinstance(value, dict):
        out = [f"{pad}{key}:"]
        for sub_key in sorted(value):
            out.extend(_emit_yaml_pair(sub_key, value[sub_key], indent + 2))
        return out
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        out = [f"{pad}{key}:"]
        for item in value:
            out.append(f"{pad}  - {_emit_scalar(item)}")
        return out
    if isinstance(value, str) and ("\n" in value):
        out = [f"{pad}{key}: |"]
        for line in value.splitlines():
            out.append(f"{pad}  {line}")
        return out
    return [f"{pad}{key}: {_emit_scalar(value)}"]


def _emit_scalar(value: Any) -> str:
    """Quote a scalar when needed (contains ``:`` / leading/trailing space)."""
    text = str(value)
    if any(ch in text for ch in ":#\n") or text != text.strip():
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text
