"""coverage.

Pins canonical SKILL.md parsing + directory loader semantics:

* Frontmatter parsing (top-level scalars, lists, block scalars, nested
 mappings under ``metadata.orcho``)
* Lenient name fallback (directory basename, slug normalisation)
* Strict description requirement (Agent Skills standard)
* Resource manifest enumeration + sort stability (checksum determinism)
* Resource path traversal rejection
* Discovery: missing roots, duplicate names, malformed packages, mixed
 good/bad packages
"""
from __future__ import annotations

import hashlib
import os
import textwrap
from pathlib import Path

import pytest

from pipeline.skills import (
    SkillPackage,
    SkillParseError,
    discover_skills_in_root,
    load_skill_package,
    parse_skill_md,
)

# ── Fixtures ──────────────────────────────────────────────────────────


def _write_skill(
    root: Path,
    name: str,
    *,
    body: str = "How to do the thing.",
    description: str = "Test skill description.",
    extra_frontmatter: str = "",
    resources: dict[str, str] | None = None,
) -> Path:
    """Write a minimal SKILL.md package and return its directory."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"name: {name}", f"description: {description}"]
    if extra_frontmatter.strip():
        fm_lines.append(extra_frontmatter.rstrip())
    skill_md = (
        "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body + "\n"
    )
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

    for relative_path, content in (resources or {}).items():
        target = skill_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


# ── parse_skill_md ────────────────────────────────────────────────────


class TestParseSkillMd:
    def test_minimal(self) -> None:
        text = textwrap.dedent(
            """\
            ---
            name: backend
            description: implements REST endpoints
            ---

            # Body
            """
        )
        fm, body = parse_skill_md(text)
        assert fm == {
            "name": "backend",
            "description": "implements REST endpoints",
        }
        assert body == "# Body"

    def test_missing_fence_raises(self) -> None:
        with pytest.raises(SkillParseError, match="frontmatter"):
            parse_skill_md("just markdown, no fence\n")

    def test_block_scalar(self) -> None:
        text = textwrap.dedent(
            """\
            ---
            name: x
            description: y
            compatibility: |
              line one
              line two
            ---

            body
            """
        )
        fm, body = parse_skill_md(text)
        assert fm["compatibility"] == "line one\nline two"
        assert body == "body"

    def test_folded_block_scalar(self) -> None:
        text = textwrap.dedent(
            """\
            ---
            name: x
            description: >-
              line one
              line two
            ---

            body
            """
        )
        fm, body = parse_skill_md(text)
        assert fm["description"] == "line one line two"
        assert body == "body"

    def test_folded_block_scalar_with_clip_chomping(self) -> None:
        text = textwrap.dedent(
            """\
            ---
            name: x
            description: >
              line one
              line two
            ---

            body
            """
        )
        fm, _ = parse_skill_md(text)
        assert fm["description"] == "line one line two"

    def test_plain_scalar_continuation(self) -> None:
        text = textwrap.dedent(
            """\
            ---
            name: x
            description: first line
              second line
              third line
            ---

            body
            """
        )
        fm, _ = parse_skill_md(text)
        assert fm["description"] == "first line second line third line"

    def test_inline_list(self) -> None:
        text = textwrap.dedent(
            """\
            ---
            name: x
            description: y
            allowed-tools: [bash, edit, test]
            ---

            body
            """
        )
        fm, _ = parse_skill_md(text)
        assert fm["allowed-tools"] == ["bash", "edit", "test"]

    def test_block_list(self) -> None:
        text = textwrap.dedent(
            """\
            ---
            name: x
            description: y
            tools:
              - bash
              - edit
            ---

            body
            """
        )
        fm, _ = parse_skill_md(text)
        assert fm["tools"] == ["bash", "edit"]

    def test_nested_metadata_orcho(self) -> None:
        # R9 requires metadata.orcho.{applicable_phases, file_patterns}.
        text = textwrap.dedent(
            """\
            ---
            name: backend
            description: REST endpoints
            metadata:
              orcho:
                applicable_phases: [implement, review_changes]
                file_patterns:
                  - "src/Controller/**"
                  - "src/Routing/*.php"
            ---

            body
            """
        )
        fm, _ = parse_skill_md(text)
        assert fm["metadata"]["orcho"]["applicable_phases"] == [
            "implement",
            "review_changes",
        ]
        assert fm["metadata"]["orcho"]["file_patterns"] == [
            "src/Controller/**",
            "src/Routing/*.php",
        ]

    def test_quoted_scalar(self) -> None:
        text = textwrap.dedent(
            """\
            ---
            name: "x"
            description: 'with: colon'
            ---

            body
            """
        )
        fm, _ = parse_skill_md(text)
        assert fm["name"] == "x"
        assert fm["description"] == "with: colon"


# ── load_skill_package ────────────────────────────────────────────────


class TestLoadSkillPackage:
    def test_minimal(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "backend")
        pkg = load_skill_package(skill_dir, source="project")
        assert isinstance(pkg, SkillPackage)
        assert pkg.name == "backend"
        assert pkg.description == "Test skill description."
        assert pkg.source == "project"
        assert pkg.body == "How to do the thing."
        assert pkg.root_dir == skill_dir.resolve()
        assert pkg.skill_md_path == (skill_dir / "SKILL.md").resolve()
        assert pkg.resources == ()
        assert pkg.resource_manifest == ()
        assert len(pkg.checksum) == 64  # sha256 hex

    def test_missing_skill_md_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SkillParseError, match="missing SKILL.md"):
            load_skill_package(empty)

    def test_missing_description_raises(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "bad"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: bad\n---\n\nbody\n", encoding="utf-8",
        )
        with pytest.raises(SkillParseError, match="description"):
            load_skill_package(skill_dir)

    def test_name_falls_back_to_directory_basename(
        self, tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "fallback-name"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: y\n---\n\nbody\n", encoding="utf-8",
        )
        pkg = load_skill_package(skill_dir)
        assert pkg.name == "fallback-name"

    def test_name_slug_normalised(
        self, tmp_path: Path, capsys,
    ) -> None:
        # Spaces and uppercase get slug-normalised; not fatal, just warned.
        skill_dir = tmp_path / "case"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: My Backend Skill\ndescription: y\n---\n\nbody\n",
            encoding="utf-8",
        )
        pkg = load_skill_package(skill_dir)
        assert pkg.name == "my-backend-skill"
        captured = capsys.readouterr()
        assert "normalised" in captured.out

    def test_resource_manifest_enumerated(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(
            tmp_path,
            "with_resources",
            resources={
                "scripts/check.sh": "#!/bin/sh\necho hi\n",
                "references/routing.md": "# routing\n",
                "assets/diagram.svg": "<svg/>",
            },
        )
        pkg = load_skill_package(skill_dir)
        relatives = {e.relative_path for e in pkg.resource_manifest}
        assert relatives == {
            "scripts/check.sh",
            "references/routing.md",
            "assets/diagram.svg",
        }
        # Sizes populated.
        for entry in pkg.resource_manifest:
            assert entry.size_bytes > 0
            assert entry.mtime_ns > 0
        # Paths point at real files inside the skill root.
        for path in pkg.resources:
            assert path.is_file()
            assert pkg.root_dir in path.parents

    def test_resource_manifest_sorted(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(
            tmp_path,
            "ordered",
            resources={
                "scripts/zzz.sh": "z",
                "scripts/aaa.sh": "a",
                "references/y.md": "y",
            },
        )
        pkg = load_skill_package(skill_dir)
        ordered = [e.relative_path for e in pkg.resource_manifest]
        assert ordered == sorted(ordered)

    def test_checksum_stable_across_loads(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(
            tmp_path,
            "stable",
            resources={"scripts/x.sh": "echo\n"},
        )
        first = load_skill_package(skill_dir)
        second = load_skill_package(skill_dir)
        assert first.checksum == second.checksum

    def test_checksum_changes_on_body_edit(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "edits")
        before = load_skill_package(skill_dir).checksum
        (skill_dir / "SKILL.md").write_text(
            "---\nname: edits\ndescription: y\n---\n\nedited body\n",
            encoding="utf-8",
        )
        after = load_skill_package(skill_dir).checksum
        assert before != after

    def test_checksum_changes_on_resource_add(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path, "addres")
        before = load_skill_package(skill_dir).checksum
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        (scripts / "new.sh").write_text("echo\n", encoding="utf-8")
        after = load_skill_package(skill_dir).checksum
        assert before != after

    def test_checksum_known_value(self, tmp_path: Path) -> None:
        # Pin the bytes-in / digest-out contract so future refactors of
        # the checksum routine surface as test failures, not silent
        # reproducibility drift.
        skill_dir = tmp_path / "pinned"
        skill_dir.mkdir()
        skill_md_text = (
            "---\nname: pinned\ndescription: pinned skill\n---\n\nbody\n"
        )
        (skill_dir / "SKILL.md").write_text(skill_md_text, encoding="utf-8")

        pkg = load_skill_package(skill_dir)

        # Manually rebuild the checksum the way the loader does, to
        # confirm we're hashing what we documented.
        h = hashlib.sha256()
        h.update(skill_md_text.encode("utf-8"))
        h.update(b"\x1f")
        assert pkg.checksum == h.hexdigest()

    def test_path_traversal_resource_rejected(
        self, tmp_path: Path,
    ) -> None:
        if os.name == "nt":
            pytest.skip("symlinks require admin on Windows")
        skill_dir = _write_skill(tmp_path, "trav")
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        scripts = skill_dir / "scripts"
        scripts.mkdir()
        (scripts / "leak.lnk").symlink_to(outside)
        with pytest.raises(SkillParseError, match="escapes"):
            load_skill_package(skill_dir)

    def test_metadata_orcho_preserved_in_frontmatter(
        self, tmp_path: Path,
    ) -> None:
        skill_dir = _write_skill(
            tmp_path,
            "routed",
            extra_frontmatter=textwrap.dedent(
                """\
                metadata:
                  orcho:
                    applicable_phases: [implement]
                    file_patterns:
                      - src/**
                """
            ),
        )
        pkg = load_skill_package(skill_dir)
        assert pkg.frontmatter["metadata"]["orcho"]["applicable_phases"] == [
            "implement",
        ]
        assert pkg.frontmatter["metadata"]["orcho"]["file_patterns"] == [
            "src/**",
        ]


# ── discover_skills_in_root ───────────────────────────────────────────


class TestDiscoverSkillsInRoot:
    def test_missing_root_is_empty(self, tmp_path: Path) -> None:
        result = discover_skills_in_root(
            tmp_path / "nonexistent", source="user",
        )
        assert result == {}

    def test_basic_discovery(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        _write_skill(tmp_path, "beta")
        result = discover_skills_in_root(tmp_path, source="project")
        assert set(result) == {"alpha", "beta"}
        assert all(p.source == "project" for p in result.values())

    def test_directory_without_skill_md_ignored(
        self, tmp_path: Path,
    ) -> None:
        # A loose directory (e.g. a stray folder) shouldn't break
        # discovery — only directories carrying SKILL.md count.
        _write_skill(tmp_path, "good")
        (tmp_path / "not_a_skill").mkdir()
        result = discover_skills_in_root(tmp_path, source="project")
        assert set(result) == {"good"}

    def test_loose_files_ignored(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "good")
        (tmp_path / "README.md").write_text("# notes\n", encoding="utf-8")
        result = discover_skills_in_root(tmp_path, source="project")
        assert set(result) == {"good"}

    def test_malformed_package_skipped_with_diagnostic(
        self, tmp_path: Path, capsys,
    ) -> None:
        _write_skill(tmp_path, "good")
        bad = tmp_path / "bad"
        bad.mkdir()
        # Missing description → SkillParseError → discovery skips.
        (bad / "SKILL.md").write_text(
            "---\nname: bad\n---\n\nbody\n", encoding="utf-8",
        )
        result = discover_skills_in_root(tmp_path, source="project")
        assert set(result) == {"good"}
        captured = capsys.readouterr()
        assert "skipping" in captured.out
        assert "bad" in captured.out

    def test_duplicate_names_keep_first(
        self, tmp_path: Path, capsys,
    ) -> None:
        # Two skills in the same root resolving to the same slug — the
        # first wins, second is logged. (Cross-source priority is
        #  territory.)
        a = _write_skill(tmp_path, "First-Name")
        b = _write_skill(tmp_path, "first-name")
        # _write_skill creates the second under the slugged path; both
        # resolve to slug "first-name". Add a unique suffix-rename to
        # force the collision deterministically.
        del a, b
        # Recreate to pin behaviour:
        for sub in tmp_path.iterdir():
            for child in sub.iterdir():
                child.unlink()
            sub.rmdir()
        first = tmp_path / "first"
        second = tmp_path / "second"
        for d in (first, second):
            d.mkdir()
            (d / "SKILL.md").write_text(
                "---\nname: shared\ndescription: y\n---\n\nbody\n",
                encoding="utf-8",
            )
        result = discover_skills_in_root(tmp_path, source="project")
        assert set(result) == {"shared"}
        # The first directory wins (sorted iterdir → "first" < "second").
        assert result["shared"].root_dir == first.resolve()
        captured = capsys.readouterr()
        assert "duplicate name" in captured.out
