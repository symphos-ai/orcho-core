"""R9 skill model types."""
from pathlib import Path

import pytest

from pipeline.skills import (
    ResourceManifestEntry,
    SkillBinding,
    SkillPackage,
    SkillResourceBinding,
    SkillTrustPolicy,
)

# ── SkillPackage ──────────────────────────────────────────────────────────────

class TestSkillPackage:
    def _pkg(self, **kw) -> SkillPackage:
        defaults = dict(
            name="backend-endpoint",
            description="Implement REST endpoints",
            root_dir=Path("/p/.agents/skills/backend-endpoint"),
            skill_md_path=Path("/p/.agents/skills/backend-endpoint/SKILL.md"),
            body="# Backend Endpoint\n\nInstructions.",
            frontmatter={"name": "backend-endpoint", "description": "Implement REST endpoints"},
            source="project",
            checksum="abc123",
        )
        defaults.update(kw)
        return SkillPackage(**defaults)

    def test_minimal_construct(self) -> None:
        pkg = self._pkg()
        assert pkg.resources == ()
        assert pkg.resource_manifest == ()

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name required"):
            self._pkg(name="")

    def test_empty_description_rejected(self) -> None:
        with pytest.raises(ValueError, match="description required"):
            self._pkg(description="")

    def test_with_resources(self) -> None:
        pkg = self._pkg(
            resources=(Path("scripts/check.py"), Path("references/spec.md")),
            resource_manifest=(
                ResourceManifestEntry(
                    relative_path="scripts/check.py", size_bytes=512, mtime_ns=1,
                ),
                ResourceManifestEntry(
                    relative_path="references/spec.md", size_bytes=2048, mtime_ns=2,
                ),
            ),
        )
        assert len(pkg.resources) == 2
        assert pkg.resource_manifest[0].relative_path == "scripts/check.py"

    def test_compat_source(self) -> None:
        pkg = self._pkg(source="claude-compat")
        assert pkg.source == "claude-compat"


# ── SkillBinding ──────────────────────────────────────────────────────────────

class TestSkillBinding:
    def test_explicit_activation(self) -> None:
        b = SkillBinding(
            skill_name="backend-endpoint",
            activation="explicit",
            source="project",
            checksum="abc",
            phase="implement",
        )
        assert b.activation == "explicit"

    def test_architect_selected_with_subtask(self) -> None:
        b = SkillBinding(
            skill_name="frontend-ui",
            activation="architect_selected",
            source="user",
            checksum="def",
            subtask_id="t1",
        )
        assert b.subtask_id == "t1"

    def test_user_requested(self) -> None:
        b = SkillBinding(
            skill_name="security-audit",
            activation="user_requested",
            source="package:third-party-overlay",
            checksum="xyz",
        )
        assert b.activation == "user_requested"


# ── SkillResourceBinding ──────────────────────────────────────────────────────

class TestSkillResourceBinding:
    def test_load_record(self) -> None:
        rb = SkillResourceBinding(
            skill_name="backend-endpoint",
            relative_path="scripts/check.py",
            sha256="hash123",
            size_bytes=512,
            loaded_at_phase="implement",
        )
        assert rb.sha256 == "hash123"
        assert rb.loaded_at_phase == "implement"


# ── SkillTrustPolicy ──────────────────────────────────────────────────────────

class TestSkillTrustPolicy:
    def test_defaults_safe(self) -> None:
        """Project + compat skills OFF by default — autonomous-run security."""
        p = SkillTrustPolicy()
        assert p.trust_packages is True
        assert p.trust_user is True
        assert p.trust_workspace is True
        assert p.trust_project is False
        assert p.trust_compat_claude is False
        assert p.trust_compat_forge is False

    def test_opt_in_project(self) -> None:
        p = SkillTrustPolicy(trust_project=True)
        assert p.trust_project is True
