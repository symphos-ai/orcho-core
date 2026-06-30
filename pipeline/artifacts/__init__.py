"""pipeline.artifacts — typed artifact pipeline.

See `docs/architecture/artifact_pipeline.md` (Milestone 11) for the
two-axis taxonomy (internal/external × ephemeral/durable) and the
ArtifactProfile bundles (none / minimal / adr / docs / full).

Phase 1 ships the type foundation: ArtifactKind / ArtifactProfile /
ArtifactSpec / ArtifactsConfig / ArtifactRecord — all frozen dataclasses
with construction-time invariants. Milestone 11 wires the actual
generator pipeline + writer + ProjectRepoLock. The legacy str-based
``pipeline.plugins.ArtifactsConfig`` keeps serving the current
``PluginConfig.artifacts`` field until Milestone 11 swaps it for the
typed version below.
"""
from pipeline.artifacts.types import (
    ArtifactKind,
    ArtifactProfile,
    ArtifactRecord,
    ArtifactsConfig,
    ArtifactSpec,
)

__all__ = [
    "ArtifactKind",
    "ArtifactProfile",
    "ArtifactRecord",
    "ArtifactSpec",
    "ArtifactsConfig",
]
