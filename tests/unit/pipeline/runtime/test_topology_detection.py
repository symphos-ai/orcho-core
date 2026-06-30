"""Topology heuristic (Stage T1) — isolated unit tests.

Covers the deterministic ``recommend_topology`` heuristic, the
``TopologyRecommendation`` value object validation, and that the closed
``RunTopology`` / ``DeliveryScope`` enums never leak into ``SemanticProfile``.

The heuristic is provider-neutral and model-free: lower-case substring
matching against a data-driven signal table.
"""

from __future__ import annotations

import pytest

from pipeline.runtime.run_shape import (
    DeliveryScope,
    RunTopology,
    SemanticProfile,
)
from pipeline.runtime.topology_detection import (
    TopologyRecommendation,
    recommend_topology,
)

# A self-contained signal table so the test does not depend on config I/O.
_SIGNALS = {
    "_comment": "ignored",
    "mcp tool": ["orcho-core", "orcho-mcp"],
    "mcp schema": ["orcho-core", "orcho-mcp"],
    "sdk wire": ["orcho-core", "orcho-mcp"],
    "wire schema": ["orcho-core", "orcho-mcp"],
    "schema snapshot": ["orcho-core", "orcho-mcp"],
}


def test_sdk_wire_schema_mcp_task_recommends_cross() -> None:
    task = (
        "Change the core SDK wire format and update the matching MCP tool "
        "schema snapshot."
    )
    rec = recommend_topology(task, signals=_SIGNALS)

    assert rec.topology is RunTopology.CROSS_RECOMMENDED
    assert "orcho-core" in rec.projects
    assert "orcho-mcp" in rec.projects
    # orcho-core is ordered first as the primary.
    assert rec.projects[0] == "orcho-core"
    assert rec.confidence >= 0.7
    assert rec.reason != ""


def test_task_without_signals_is_mono() -> None:
    rec = recommend_topology(
        "Fix a typo in the README and tidy a docstring.", signals=_SIGNALS
    )

    assert rec.topology is RunTopology.MONO
    assert rec.projects == ()
    assert rec.confidence < 0.7
    assert rec.reason == ""


def test_projects_are_deduplicated_and_primary_first() -> None:
    # Two distinct signals both implicate the same alias pair.
    task = "Touch the sdk wire and regenerate the schema snapshot."
    rec = recommend_topology(task, signals=_SIGNALS)

    assert rec.projects == ("orcho-core", "orcho-mcp")


def test_default_signal_table_loads_from_config() -> None:
    # signals=None loads the shipped config.defaults.json table lazily.
    rec = recommend_topology(
        "Update the orcho-mcp schema snapshot for the new wire format."
    )

    assert rec.topology is RunTopology.CROSS_RECOMMENDED
    assert set(rec.projects) == {"orcho-core", "orcho-mcp"}


def test_recommendation_validates_confidence_range() -> None:
    with pytest.raises(ValueError):
        TopologyRecommendation(topology=RunTopology.MONO, confidence=1.5)


def test_recommendation_rejects_bare_string_projects() -> None:
    with pytest.raises(TypeError):
        TopologyRecommendation(
            topology=RunTopology.CROSS_RECOMMENDED, projects="orcho-core"
        )


def test_recommendation_coerces_enum_string() -> None:
    rec = TopologyRecommendation(topology="mono")
    assert rec.topology is RunTopology.MONO


def test_topology_and_scope_are_not_semantic_profiles() -> None:
    # The new axes are closed enums distinct from SemanticProfile.
    with pytest.raises(ValueError):
        SemanticProfile("cross")
    with pytest.raises(ValueError):
        SemanticProfile("auto-detect")
    # And the enums themselves carry the expected members.
    assert RunTopology("cross_recommended") is RunTopology.CROSS_RECOMMENDED
    assert DeliveryScope("strict_mono") is DeliveryScope.STRICT_MONO
    assert DeliveryScope("expanded_mono") is DeliveryScope.EXPANDED_MONO
    assert DeliveryScope("cross") is DeliveryScope.CROSS
