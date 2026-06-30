"""Import-path stability contract for the cross-project application boundary.

This is a Stage 0 guard. Its sole job is to assert that the load-bearing
module import paths of the cross-project pipeline boundary stay resolvable — it
is NOT a signature lock, field-parity, presentation, or typed-boundary consumer
test (those live in ``tests/unit/pipeline/cross_project/test_cross_run_request.py``
and ``tests/integration/cross/test_typed_boundary_consumer.py``).

The MCP typed-pilot consumer (``orcho-mcp``) and future SDK / library clients
bind to the cross boundary modules by exact module path; the in-tree cross
runner reaches the typed boundary the same way. Relocating or renaming
``pipeline.cross_project.app``, ``pipeline.cross_project.app_types``, or the
``run_cross_pipeline`` back-compat wrapper in
``pipeline.cross_project.orchestrator`` would break those consumers at import
time. The Stage 1/2 project/cross app refactor must keep these paths
resolvable.

See the project/cross Stage 0 baseline planning record (internal) (the Stage 0
baseline contract, §4 and §6) and the project/cross app refactor roadmap.

Pure in-process imports only — no subprocess, git worktree, or network.
"""

from __future__ import annotations


def test_cross_app_entry_point_importable() -> None:
    from pipeline.cross_project.app import run_cross_project_pipeline

    assert callable(run_cross_project_pipeline)


def test_cross_app_types_dtos_importable() -> None:
    from pipeline.cross_project.app_types import CrossRunRequest, CrossRunResult

    assert CrossRunRequest is not None
    assert CrossRunResult is not None


def test_cross_orchestrator_backcompat_wrapper_importable() -> None:
    from pipeline.cross_project.orchestrator import run_cross_pipeline

    assert callable(run_cross_pipeline)
