"""coverage.

Pins multi-source skill discovery semantics:

* Priority order (project > compat > workspace > user > entry_points)
* :class:`SkillTrustPolicy` defaults: workspace only
* ``include_untrusted=True`` override
* ``orcho.skills`` entry_points: SkillPackage instance, factory, Path
 fan-out, malformed entries
* Conflict resolution: lower-priority entries are shadowed (logged,
 not loaded into the merged registry)
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from pipeline.skills import (
    SkillPackage,
    SkillTrustPolicy,
    discover_skills,
    load_skill_package,
)
from pipeline.skills.discover import ENTRY_POINTS_GROUP

# ── Fixture helpers ───────────────────────────────────────────────────


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str | None = None,
) -> Path:
    """Drop a minimal SKILL.md package and return its directory."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    desc = description if description is not None else f"{name} skill"
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody for {name}\n",
        encoding="utf-8",
    )
    return skill_dir


@dataclass
class _FakeEP:
    name: str
    value: object

    def load(self):
        return self.value


@contextmanager
def _patch_entry_points(eps: list[_FakeEP]) -> Iterator[None]:
    """Replace ``importlib.metadata.entry_points`` for the duration."""

    def fake_entry_points(*, group: str):
        if group != ENTRY_POINTS_GROUP:
            return []
        return list(eps)

    with patch(
        "importlib.metadata.entry_points",
        side_effect=fake_entry_points,
    ):
        yield


# ── Trust policy gating ───────────────────────────────────────────────


class TestTrustPolicy:
    def test_global_sources_off_by_default(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        home = tmp_path / "home"
        _write_skill(home / ".agents/skills", "user-skill")
        package_root = tmp_path / "pkg_root"
        _write_skill(package_root, "package-skill")

        with _patch_entry_points([_FakeEP("vendor_pack", package_root)]):
            result = discover_skills(
                project_dir=tmp_path / "project",
                workspace_dir=workspace,
                home_dir=home,
            )

        assert result == {}

    def test_project_skills_off_by_default(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        _write_skill(project / ".agents/skills", "alpha")

        # Trust policy default: trust_project=False → project skills
        # NOT loaded.
        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=project,
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
            )
        assert result == {}

    def test_project_skills_loaded_when_trusted(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        _write_skill(project / ".agents/skills", "alpha")

        policy = SkillTrustPolicy(trust_project=True)
        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=project,
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=policy,
            )
        assert set(result) == {"alpha"}
        assert result["alpha"].source == "project"

    def test_include_untrusted_overrides_policy(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        _write_skill(project / ".agents/skills", "alpha")

        # Default policy refuses project, but include_untrusted=True
        # bypasses the gate (used by orcho skills list --all).
        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=project,
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                include_untrusted=True,
            )
        assert set(result) == {"alpha"}

    def test_compat_sources_off_by_default(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        _write_skill(project / ".claude/skills", "claudio")
        _write_skill(project / ".forge/skills", "forgey")

        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=project,
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
            )
        # Project, claude-compat, forge-compat all off → empty.
        assert result == {}

    def test_compat_claude_loads_when_trusted(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        _write_skill(project / ".claude/skills", "claudio")

        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=project,
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=SkillTrustPolicy(trust_compat_claude=True),
            )
        assert set(result) == {"claudio"}
        assert result["claudio"].source == "claude-compat"


# ── Source priority order ─────────────────────────────────────────────


class TestPriorityOrder:
    def test_project_beats_workspace(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        workspace = tmp_path / "workspace"
        project.mkdir()
        workspace.mkdir()

        # Same skill name in both layers.
        _write_skill(
            project / ".agents/skills",
            "shared",
            description="from project",
        )
        _write_skill(
            workspace / ".agents/skills",
            "shared",
            description="from workspace",
        )

        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=project,
                workspace_dir=workspace,
                home_dir=tmp_path / "home",
                trust_policy=SkillTrustPolicy(trust_project=True),
            )
        assert result["shared"].description == "from project"
        assert result["shared"].source == "project"

    def test_workspace_beats_user(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        workspace = tmp_path / "workspace"
        home = tmp_path / "home"
        for d in (project, workspace, home):
            d.mkdir()

        _write_skill(
            workspace / ".agents/skills",
            "shared",
            description="from workspace",
        )
        _write_skill(
            home / ".agents/skills",
            "shared",
            description="from user",
        )

        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=project,
                workspace_dir=workspace,
                home_dir=home,
                trust_policy=SkillTrustPolicy(trust_user=True),
            )
        assert result["shared"].source == "workspace"
        assert result["shared"].description == "from workspace"

    def test_user_beats_entry_points(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        workspace = tmp_path / "workspace"
        home = tmp_path / "home"
        for d in (project, workspace, home):
            d.mkdir()

        # Build a real package the entry-point can return as a Path.
        package_root = tmp_path / "pkg_root"
        _write_skill(package_root, "shared", description="from package")

        _write_skill(
            home / ".agents/skills",
            "shared",
            description="from user",
        )

        with _patch_entry_points(
            [_FakeEP("vendor_pack", package_root)],
        ):
            result = discover_skills(
                project_dir=project,
                workspace_dir=workspace,
                home_dir=home,
                trust_policy=SkillTrustPolicy(
                    trust_user=True,
                    trust_packages=True,
                ),
            )
        assert result["shared"].source == "user"
        assert result["shared"].description == "from user"

    def test_full_layering(self, tmp_path: Path) -> None:
        # All four layers contribute distinct skills; merged registry
        # contains all four.
        project = tmp_path / "project"
        workspace = tmp_path / "workspace"
        home = tmp_path / "home"
        for d in (project, workspace, home):
            d.mkdir()

        _write_skill(project / ".agents/skills", "p_only")
        _write_skill(workspace / ".agents/skills", "w_only")
        _write_skill(home / ".agents/skills", "u_only")

        package_root = tmp_path / "pkg_root"
        _write_skill(package_root, "pkg_only")

        with _patch_entry_points(
            [_FakeEP("vendor_pack", package_root)],
        ):
            result = discover_skills(
                project_dir=project,
                workspace_dir=workspace,
                home_dir=home,
                trust_policy=SkillTrustPolicy(
                    trust_project=True,
                    trust_user=True,
                    trust_packages=True,
                ),
            )
        assert set(result) == {"p_only", "w_only", "u_only", "pkg_only"}
        assert result["p_only"].source == "project"
        assert result["w_only"].source == "workspace"
        assert result["u_only"].source == "user"
        assert result["pkg_only"].source.startswith("package:")


# ── Entry-point coercion ──────────────────────────────────────────────


class TestEntryPointCoercion:
    def test_skillpackage_instance_loads(self, tmp_path: Path) -> None:
        # Build a real SkillPackage by running the loader, then ship it
        # back through an entry-point as a bare instance.
        skill_dir = _write_skill(tmp_path / "src", "vendor")
        pkg = load_skill_package(skill_dir, source="unknown")

        with _patch_entry_points([_FakeEP("vendor_pack", pkg)]):
            result = discover_skills(
                project_dir=tmp_path / "project",
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=SkillTrustPolicy(trust_packages=True),
            )
        assert set(result) == {"vendor"}
        # Source upgraded from "unknown" → "package:<entry_name>" so
        # audit trails identify the wheel of origin.
        assert result["vendor"].source == "package:vendor_pack"

    def test_zero_arg_factory_invoked(self, tmp_path: Path) -> None:
        skill_dir = _write_skill(tmp_path / "src", "vendor")
        pkg = load_skill_package(skill_dir, source="package:vendor_pack")

        def factory() -> SkillPackage:
            return pkg

        with _patch_entry_points([_FakeEP("vendor_pack", factory)]):
            result = discover_skills(
                project_dir=tmp_path / "project",
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=SkillTrustPolicy(trust_packages=True),
            )
        assert set(result) == {"vendor"}

    def test_path_fanout(self, tmp_path: Path) -> None:
        package_root = tmp_path / "pkg_root"
        _write_skill(package_root, "vendor_a")
        _write_skill(package_root, "vendor_b")

        with _patch_entry_points([_FakeEP("vendor_pack", package_root)]):
            result = discover_skills(
                project_dir=tmp_path / "project",
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=SkillTrustPolicy(trust_packages=True),
            )
        assert set(result) == {"vendor_a", "vendor_b"}
        for pkg in result.values():
            assert pkg.source == "package:vendor_pack"

    def test_unknown_value_type_logged(
        self, tmp_path: Path, capsys,
    ) -> None:
        with _patch_entry_points([_FakeEP("bad_pack", 42)]):
            result = discover_skills(
                project_dir=tmp_path / "project",
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=SkillTrustPolicy(trust_packages=True),
            )
        assert result == {}
        captured = capsys.readouterr()
        assert "unexpected value type" in captured.out
        assert "bad_pack" in captured.out

    def test_path_to_nondirectory_logged(
        self, tmp_path: Path, capsys,
    ) -> None:
        not_a_dir = tmp_path / "ghost"
        with _patch_entry_points([_FakeEP("bad_pack", not_a_dir)]):
            result = discover_skills(
                project_dir=tmp_path / "project",
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=SkillTrustPolicy(trust_packages=True),
            )
        assert result == {}
        captured = capsys.readouterr()
        assert "not a directory" in captured.out


# ── Edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_missing_directories_are_silent(self, tmp_path: Path) -> None:
        # Empty filesystem: project/workspace/home all don't exist.
        # discover_skills must not raise; it returns {}.
        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=tmp_path / "ghost_project",
                workspace_dir=tmp_path / "ghost_workspace",
                home_dir=tmp_path / "ghost_home",
            )
        assert result == {}

    def test_default_home_dir(self, monkeypatch, tmp_path: Path) -> None:
        # When home_dir is None, Path.home() is consulted. Patch it to
        # avoid touching the developer's real ~/.
        ghost_home = tmp_path / "no_such_home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: ghost_home))
        with _patch_entry_points([]):
            result = discover_skills(
                project_dir=tmp_path / "project",
                workspace_dir=tmp_path / "workspace",
                # No explicit home_dir → falls back to Path.home().
            )
        assert result == {}

    def test_shadow_logged(
        self, tmp_path: Path, capsys,
    ) -> None:
        project = tmp_path / "project"
        workspace = tmp_path / "workspace"
        for d in (project, workspace):
            d.mkdir()
        _write_skill(
            project / ".agents/skills",
            "shared",
            description="from project",
        )
        _write_skill(
            workspace / ".agents/skills",
            "shared",
            description="from workspace",
        )
        with _patch_entry_points([]):
            discover_skills(
                project_dir=project,
                workspace_dir=workspace,
                home_dir=tmp_path / "home",
                trust_policy=SkillTrustPolicy(trust_project=True),
            )
        captured = capsys.readouterr()
        assert "shadowed" in captured.out
        assert "shared" in captured.out


class TestNestedGitDir:
    """Regression (B4): skills living inside a nested git repo must be
    discoverable when the registered project dir is an outer dir (e.g. a
    Unity project under SVN with the git repo at Assets/_Match-Three-Common).
    """

    def test_skills_in_nested_git_root_are_discovered(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "unity_project"        # registered project (SVN root)
        git_root = project / "Assets/_Match-Three-Common"
        _write_skill(git_root / ".agents/skills", "beta")

        policy = SkillTrustPolicy(trust_project=True)
        with _patch_entry_points([]), patch(
            "pipeline.engine.run_diff.resolve_git_root",
            side_effect=lambda p: git_root if Path(p) == project else None,
        ):
            result = discover_skills(
                project_dir=project,
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=policy,
            )
        assert set(result) == {"beta"}
        assert result["beta"].source == "project"

    def test_project_dir_skill_wins_over_git_root_on_conflict(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "unity_project"
        git_root = project / "Assets/_Match-Three-Common"
        _write_skill(project / ".agents/skills", "shared", description="from project dir")
        _write_skill(git_root / ".agents/skills", "shared", description="from git root")

        policy = SkillTrustPolicy(trust_project=True)
        with _patch_entry_points([]), patch(
            "pipeline.engine.run_diff.resolve_git_root",
            side_effect=lambda p: git_root if Path(p) == project else None,
        ):
            result = discover_skills(
                project_dir=project,
                workspace_dir=tmp_path / "workspace",
                home_dir=tmp_path / "home",
                trust_policy=policy,
            )
        # project-dir layer has priority (first-wins); git-root layer is additive.
        assert set(result) == {"shared"}
        assert "from project dir" in result["shared"].description
