"""pipeline.artifacts.types — Phase 1 artifact taxonomy + record types.

Implements the type contracts for the artifact pipeline (Milestone 11
delivers the actual generators / writer / repo lock; Phase 1 ships the
type foundation so PluginConfig and downstream phases can declare
artifact concerns without forward references).

The legacy str-based ``ArtifactsConfig`` in ``pipeline.plugins`` keeps
serving the existing ``PluginConfig.artifacts`` field for now;
Milestone 11 migrates it to the typed version below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ArtifactKind(StrEnum):
    """Two-axis taxonomy: audience × persistence.

    See ``docs/architecture/artifact_pipeline.md`` (Milestone 11) for the
    full table of examples per combination.
    """
    INTERNAL_EPHEMERAL = "internal_ephemeral"
    INTERNAL_DURABLE = "internal_durable"
    EXTERNAL_EPHEMERAL = "external_ephemeral"
    EXTERNAL_DURABLE = "external_durable"


class ArtifactProfile(StrEnum):
    """Declarative bundle selecting which generators run after final_acceptance.

    The actual SpecList per profile lives in
    ``_config/artifact_profiles.json`` (Milestone 11).
    """
    NONE = "none"
    MINIMAL = "minimal"
    ADR = "adr"
    DOCS = "docs"
    FULL = "full"


@dataclass(frozen=True)
class ArtifactSpec:
    """One artifact-generator declaration. ``generator`` references a
    name registered in ``orcho.artifact_generators`` entry_points.

    Three generator styles emerge in Milestone 11:
      * template — Jinja substitution from session dict
      * agent-prompt — architect-class agent writes prose from
        plan + diff context
      * hybrid — template seeds → agent refines
    """
    name: str
    kind: ArtifactKind
    output_path_template: str   # e.g. "docs/adr/{number:04d}-{slug}.md"
    generator: str              # registered name
    config: dict[str, Any] | None = None
    commit_message_template: str | None = None  # for auto_commit

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ArtifactSpec.name is empty")
        if not self.output_path_template.strip():
            raise ValueError(
                f"ArtifactSpec {self.name!r}: output_path_template required"
            )
        if not self.generator.strip():
            raise ValueError(
                f"ArtifactSpec {self.name!r}: generator required"
            )


@dataclass(frozen=True)
class ArtifactsConfig:
    """Per-project artifact pipeline configuration. Lives on
    ``PluginConfig.artifacts`` (Milestone 11 migrates the legacy
    str-based field to this typed version).

    ``profile`` selects a built-in bundle (none / minimal / adr / docs /
    full). ``overrides`` lets plugins customise per-spec output paths or
    generator config without forking the bundle. ``output_root``
    defaults to the project_dir; can be redirected for staging /
    preview. ``auto_commit`` controls whether orcho creates a git
    commit for the artifacts after final_acceptance. ``auto_push`` is gated on
    auto_commit + explicit user opt-in.
    """
    profile: ArtifactProfile = ArtifactProfile.NONE
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    output_root: str | None = None
    auto_commit: bool = False
    auto_push: bool = False

    def __post_init__(self) -> None:
        if self.auto_push and not self.auto_commit:
            raise ValueError(
                "ArtifactsConfig: auto_push requires auto_commit=True"
            )


@dataclass(frozen=True)
class ArtifactRecord:
    """Result returned by an artifact generator after writing one
    artifact. Records are accumulated in ``state.extras['artifacts_written']``
    and persisted in ``session['artifacts']`` for audit / dashboard
    consumption.
    """
    name: str                       # spec name (e.g. "deliverables_manifest")
    path: str                       # filesystem path actually written
    sha256: str                     # content hash at write time
    size_bytes: int
    generator_used: str             # registered generator name
    generation_time_s: float
    success: bool = True
    error: str | None = None        # populated when success=False
    cost_usd: float | None = None   # for inferential / agent-driven generators

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ArtifactRecord.name required")
        if not self.success and not self.error:
            raise ValueError(
                f"ArtifactRecord {self.name!r}: success=False requires error message"
            )
