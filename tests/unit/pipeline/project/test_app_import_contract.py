"""Import-path stability contract for the project application boundary.

This is a Stage 0 guard. Its sole job is to assert that the load-bearing
module import paths of the project pipeline boundary stay resolvable — it is
NOT a signature lock, field-parity, presentation, or typed-boundary consumer
test (those live in ``tests/unit/pipeline/test_project_run_request.py`` and
``tests/integration/project/test_typed_boundary_consumer.py``).

The MCP typed-pilot consumer (``orcho-mcp``,
``src/orcho_mcp/run_control/typed_pilot.py``) and future SDK / library clients
bind to ``pipeline.project.app`` and ``pipeline.project.types`` by exact module
path. Relocating or renaming these symbols would break those out-of-tree
consumers at import time. The Stage 1/2 project/cross app refactor must keep
these paths resolvable.

See the project/cross Stage 0 baseline planning record (internal) (the Stage 0
baseline contract, §4 and §6) and the project/cross app refactor roadmap.

Pure in-process imports only — no subprocess, git worktree, or network.
"""

from __future__ import annotations


def test_project_app_entry_points_importable() -> None:
    from pipeline.project.app import run_pipeline, run_project_pipeline

    assert callable(run_project_pipeline)
    assert callable(run_pipeline)


def test_project_types_dtos_importable() -> None:
    from pipeline.project.types import (
        PresentationPolicy,
        ProjectRunRequest,
        ProjectRunResult,
    )

    assert ProjectRunRequest is not None
    assert ProjectRunResult is not None
    assert PresentationPolicy is not None


def test_presentation_policy_reexport_identity() -> None:
    from pipeline.presentation import PresentationPolicy as canonical
    from pipeline.project.types import PresentationPolicy as reexport

    assert reexport is canonical


def test_project_setup_modules_importable() -> None:
    """The ADR 0042 setup-module split relocated the per-run setup
    responsibilities out of ``app.py`` into focused ``pipeline.project.*``
    modules. ``app.py`` composes these by import; this narrow guard pins
    their public entry points so the coordinator's wiring stays
    resolvable. Project-scope only — no cross/MCP surfaces."""
    from pipeline.project.isolation_setup import (
        resolve_isolation_inputs,
        setup_isolation,
    )
    from pipeline.project.profile_setup import setup_profile
    from pipeline.project.run_setup import (
        init_run_session,
        print_pipeline_header,
        setup_checkpoint_and_metrics,
        setup_run_id,
    )
    from pipeline.project.runtime_setup import (
        apply_session_seeds,
        setup_runtime,
    )
    from pipeline.project.state_setup import (
        StateInputs,
        build_pipeline_state,
        hydrate_state_extras_from_session,
    )

    for fn in (
        setup_profile,
        setup_runtime,
        apply_session_seeds,
        setup_run_id,
        print_pipeline_header,
        init_run_session,
        setup_checkpoint_and_metrics,
        resolve_isolation_inputs,
        setup_isolation,
        build_pipeline_state,
        hydrate_state_extras_from_session,
    ):
        assert callable(fn)
    assert StateInputs is not None
