"""coverage.

Pins the legacy-flat → R9-directory migration:

* Standard rewrite: ``model`` / ``provider`` dropped, ``files_pattern``
 hoisted to ``metadata.orcho.file_patterns``, ``prompt_extra``
 appended as a ``## Notes`` section.
* Idempotent + collision-safe (``overwrite=False`` skips, ``True``
 replaces).
* ``dry_run=True`` writes nothing.
* ``delete_legacy=True`` removes source after success.
* Round-trip: migrated SKILL.md loads through the canonical loader.
* Failure isolation: bad input doesn't abort sibling migration.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from pipeline.skills import (
    discover_skills_in_root,
    load_skill_package,
    migrate_legacy_skills,
    parse_skill_md,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _legacy_dir(project: Path) -> Path:
    d = project / ".agent/multiagent/skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_legacy(
    project: Path,
    filename: str,
    *,
    frontmatter: str,
    body: str = "Legacy skill body.",
) -> Path:
    path = _legacy_dir(project) / filename
    path.write_text(
        f"---\n{frontmatter.strip()}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


# ── Standard rewrite ──────────────────────────────────────────────────


class TestStandardRewrite:
    def test_basic_migration(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        legacy = _write_legacy(
            project,
            "backend.md",
            frontmatter=textwrap.dedent(
                """\
                name: backend-endpoint
                description: REST endpoints for the PHP backend.
                model: claude-sonnet-4-6
                provider: claude
                files_pattern:
                  - "src/Controller/**"
                  - "src/Routing/*.php"
                """
            ),
            body="Step-by-step body.",
        )

        report = migrate_legacy_skills(project)

        assert report.succeeded
        assert len(report.written) == 1
        record = report.written[0]
        assert record.skill_name == "backend-endpoint"
        assert record.legacy_path == legacy
        assert record.target_skill_md == (
            project / ".agents/skills/backend-endpoint/SKILL.md"
        )
        assert "model" in record.dropped_keys
        assert "provider" in record.dropped_keys
        assert ("files_pattern", "metadata.orcho.file_patterns") in record.moved_keys
        assert record.body_appended is False  # no prompt_extra in this case

    def test_migrated_file_loads_through_canonical_loader(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_legacy(
            project,
            "backend.md",
            frontmatter=textwrap.dedent(
                """\
                name: backend
                description: REST endpoints
                model: claude-sonnet-4-6
                provider: claude
                files_pattern:
                  - "src/**"
                """
            ),
            body="Original body.",
        )

        report = migrate_legacy_skills(project)
        assert report.succeeded

        # Round-trip: load the migrated file through the canonical
        # loader; frontmatter no longer carries deprecated keys, and
        # files_pattern is reachable under metadata.orcho.
        target_dir = project / ".agents/skills/backend"
        pkg = load_skill_package(target_dir, source="project")
        assert pkg.name == "backend"
        assert pkg.description == "REST endpoints"
        assert pkg.body == "Original body."
        assert "model" not in pkg.frontmatter
        assert "provider" not in pkg.frontmatter
        assert pkg.frontmatter["metadata"]["orcho"]["file_patterns"] == [
            "src/**",
        ]

    def test_prompt_extra_appended_as_notes_section(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_legacy(
            project,
            "x.md",
            frontmatter=textwrap.dedent(
                """\
                name: backend
                description: y
                prompt_extra: |
                  Always validate inputs with the DTO layer.
                  Prefer type-safe deserialisation.
                """
            ),
            body="Original body.",
        )

        report = migrate_legacy_skills(project)
        assert report.succeeded
        record = report.written[0]
        assert record.body_appended is True

        target = project / ".agents/skills/backend/SKILL.md"
        text = target.read_text(encoding="utf-8")
        assert "Original body." in text
        assert "## Notes" in text
        assert "Always validate inputs" in text

    def test_name_falls_back_to_filename(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        # No name in frontmatter — slug derives from file stem.
        _write_legacy(
            project,
            "fallback-skill.md",
            frontmatter="description: y",
        )
        report = migrate_legacy_skills(project)
        assert report.succeeded
        assert report.written[0].skill_name == "fallback-skill"

    def test_unknown_keys_preserved(self, tmp_path: Path) -> None:
        # Agent Skills standard fields like license / compatibility
        # should pass through untouched.
        project = tmp_path / "proj"
        project.mkdir()
        _write_legacy(
            project,
            "x.md",
            frontmatter=textwrap.dedent(
                """\
                name: x
                description: y
                license: Apache-2.0
                """
            ),
        )
        report = migrate_legacy_skills(project)
        assert report.succeeded
        target = project / ".agents/skills/x/SKILL.md"
        fm, _ = parse_skill_md(target.read_text(encoding="utf-8"))
        assert fm["license"] == "Apache-2.0"


# ── Collision / overwrite policy ──────────────────────────────────────


class TestCollisionPolicy:
    def test_existing_target_skipped_by_default(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_legacy(
            project, "x.md", frontmatter="name: x\ndescription: y",
        )
        # Pre-existing migrated file with manual edits — must not be
        # trampled.
        target = project / ".agents/skills/x"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(
            "---\nname: x\ndescription: manually edited\n---\n\ncustom\n",
            encoding="utf-8",
        )

        report = migrate_legacy_skills(project)
        assert report.written == []
        assert len(report.skipped) == 1
        path, reason = report.skipped[0]
        assert path.name == "x.md"
        assert "exists" in reason

        # Manual edit preserved.
        text = (target / "SKILL.md").read_text(encoding="utf-8")
        assert "manually edited" in text

    def test_overwrite_replaces_existing(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_legacy(
            project, "x.md", frontmatter="name: x\ndescription: from legacy",
        )
        target = project / ".agents/skills/x"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(
            "---\nname: x\ndescription: previous\n---\n\nold\n",
            encoding="utf-8",
        )

        report = migrate_legacy_skills(project, overwrite=True)
        assert report.succeeded
        assert len(report.written) == 1

        text = (target / "SKILL.md").read_text(encoding="utf-8")
        assert "from legacy" in text


# ── dry_run + delete_legacy ───────────────────────────────────────────


class TestModes:
    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        legacy = _write_legacy(
            project, "x.md", frontmatter="name: x\ndescription: y",
        )
        report = migrate_legacy_skills(project, dry_run=True)
        assert report.succeeded
        assert len(report.written) == 1  # report still describes the
                                          # planned write
        assert not (project / ".agents/skills/x/SKILL.md").exists()
        assert legacy.exists()  # source untouched

    def test_delete_legacy_removes_source(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        legacy = _write_legacy(
            project, "x.md", frontmatter="name: x\ndescription: y",
        )
        report = migrate_legacy_skills(project, delete_legacy=True)
        assert report.succeeded
        assert legacy not in project.rglob("*.md") or not legacy.exists()
        assert legacy in report.deleted_legacy

    def test_dry_run_does_not_delete_legacy(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        legacy = _write_legacy(
            project, "x.md", frontmatter="name: x\ndescription: y",
        )
        # delete_legacy=True gated on dry_run=False; should NOT delete.
        report = migrate_legacy_skills(
            project, dry_run=True, delete_legacy=True,
        )
        assert report.succeeded
        assert legacy.exists()
        assert report.deleted_legacy == []


# ── Failure isolation ─────────────────────────────────────────────────


class TestFailureIsolation:
    def test_missing_legacy_dir_returns_empty_report(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        report = migrate_legacy_skills(project)
        assert report.succeeded
        assert report.written == []
        assert report.skipped == []
        assert report.failed == []

    def test_missing_description_skipped_with_reason(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_legacy(project, "x.md", frontmatter="name: x")  # no description
        report = migrate_legacy_skills(project)
        assert report.written == []
        assert len(report.skipped) == 1
        path, reason = report.skipped[0]
        assert "description" in reason

    def test_malformed_legacy_recorded_as_failure(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        bad = _legacy_dir(project) / "broken.md"
        bad.write_text("no frontmatter at all\n", encoding="utf-8")
        report = migrate_legacy_skills(project)
        # succeeded property: only fails when failed list is non-empty.
        assert not report.succeeded
        assert len(report.failed) == 1
        path, msg = report.failed[0]
        assert path == bad
        assert "parse error" in msg

    def test_one_bad_does_not_block_siblings(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_legacy(
            project, "good.md", frontmatter="name: good\ndescription: y",
        )
        bad = _legacy_dir(project) / "bad.md"
        bad.write_text("malformed\n", encoding="utf-8")

        report = migrate_legacy_skills(project)
        # Good migrated; bad reported.
        names = [r.skill_name for r in report.written]
        assert names == ["good"]
        assert len(report.failed) == 1


# ── Discovery integration ─────────────────────────────────────────────


class TestDiscoveryIntegration:
    def test_migrated_skills_discoverable(self, tmp_path: Path) -> None:
        # End-to-end: legacy file → migrate → canonical loader sees
        # everything the new layout expects.
        project = tmp_path / "proj"
        project.mkdir()
        _write_legacy(
            project,
            "alpha.md",
            frontmatter="name: alpha\ndescription: a",
        )
        _write_legacy(
            project,
            "beta.md",
            frontmatter="name: beta\ndescription: b",
        )
        migrate_legacy_skills(project)

        registry = discover_skills_in_root(
            project / ".agents/skills", source="project",
        )
        assert set(registry) == {"alpha", "beta"}
