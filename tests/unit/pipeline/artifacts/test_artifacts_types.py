"""artifact taxonomy."""
import pytest

from pipeline.artifacts import (
    ArtifactKind,
    ArtifactProfile,
    ArtifactRecord,
    ArtifactsConfig,
    ArtifactSpec,
)

# ── ArtifactKind / ArtifactProfile StrEnums ──────────────────────────────────

class TestArtifactKind:
    def test_complete_set(self) -> None:
        assert {k.value for k in ArtifactKind} == {
            "internal_ephemeral", "internal_durable",
            "external_ephemeral", "external_durable",
        }


class TestArtifactProfile:
    def test_complete_set(self) -> None:
        assert {p.value for p in ArtifactProfile} == {
            "none", "minimal", "adr", "docs", "full",
        }


# ── ArtifactSpec ─────────────────────────────────────────────────────────────

class TestArtifactSpec:
    def test_minimal_construct(self) -> None:
        spec = ArtifactSpec(
            name="deliverables_manifest",
            kind=ArtifactKind.EXTERNAL_DURABLE,
            output_path_template="orcho/deliverables-{run_id}.md",
            generator="deliverables_manifest",
        )
        assert spec.config is None

    def test_with_commit_template(self) -> None:
        spec = ArtifactSpec(
            name="adr",
            kind=ArtifactKind.EXTERNAL_DURABLE,
            output_path_template="docs/adr/{number:04d}-{slug}.md",
            generator="adr_agent",
            commit_message_template="docs(adr): {number} — {slug}",
        )
        assert "{number}" in spec.commit_message_template

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name is empty"):
            ArtifactSpec(
                name="", kind=ArtifactKind.INTERNAL_DURABLE,
                output_path_template="x", generator="g",
            )

    def test_empty_template_rejected(self) -> None:
        with pytest.raises(ValueError, match="output_path_template required"):
            ArtifactSpec(
                name="x", kind=ArtifactKind.INTERNAL_DURABLE,
                output_path_template="", generator="g",
            )

    def test_empty_generator_rejected(self) -> None:
        with pytest.raises(ValueError, match="generator required"):
            ArtifactSpec(
                name="x", kind=ArtifactKind.INTERNAL_DURABLE,
                output_path_template="p", generator="",
            )


# ── ArtifactsConfig ──────────────────────────────────────────────────────────

class TestArtifactsConfig:
    def test_defaults(self) -> None:
        c = ArtifactsConfig()
        assert c.profile is ArtifactProfile.NONE
        assert c.auto_commit is False
        assert c.auto_push is False
        assert c.output_root is None
        assert c.overrides == {}

    def test_auto_push_without_commit_rejected(self) -> None:
        with pytest.raises(ValueError, match="auto_push requires auto_commit"):
            ArtifactsConfig(auto_push=True, auto_commit=False)

    def test_auto_push_with_commit_ok(self) -> None:
        c = ArtifactsConfig(auto_commit=True, auto_push=True)
        assert c.auto_push is True

    def test_full_profile_with_overrides(self) -> None:
        c = ArtifactsConfig(
            profile=ArtifactProfile.FULL,
            overrides={"adr": {"output_path_template": "ADRs/{number}.md"}},
            output_root="/staging/orcho-out",
        )
        assert c.profile is ArtifactProfile.FULL
        assert "adr" in c.overrides


class TestArtifactRecord:
    def test_success_record(self) -> None:
        r = ArtifactRecord(
            name="deliverables_manifest",
            path="orcho/deliverables-run-1.md",
            sha256="abc",
            size_bytes=1024,
            generator_used="deliverables_manifest",
            generation_time_s=0.05,
        )
        assert r.success is True
        assert r.error is None

    def test_failure_requires_error(self) -> None:
        with pytest.raises(ValueError, match="requires error message"):
            ArtifactRecord(
                name="adr",
                path="docs/adr/0042.md",
                sha256="",
                size_bytes=0,
                generator_used="adr_agent",
                generation_time_s=10.0,
                success=False,
            )

    def test_inferential_with_cost(self) -> None:
        r = ArtifactRecord(
            name="adr",
            path="docs/adr/0042.md",
            sha256="hash",
            size_bytes=2048,
            generator_used="adr_agent",
            generation_time_s=12.5,
            cost_usd=0.04,
        )
        assert r.cost_usd == 0.04

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name required"):
            ArtifactRecord(
                name="", path="p", sha256="h", size_bytes=0,
                generator_used="g", generation_time_s=0,
            )
