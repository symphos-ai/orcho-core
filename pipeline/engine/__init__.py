"""
pipeline/engine/__init__.py — Public API of the engine package.

The engine package contains the shared core logic used by both:
  - pipeline.project_orchestrator   (orchestrate_project / run_pipeline)
  - pipeline.cross_project.orchestrator (orchestrate_projects / run_cross_pipeline)

Package layout:
  engine/
    session.py     — init_session(), save_session()
    run_logging.py — setup_run_logging(), is_sub_pipeline()
    hypothesis.py  — run_hypothesis_loop(), maybe_run_hypothesis()

    (A future ``research.py`` will host the deeper /unity-research-driven
    pre-PLAN mode — distinct from the fast hypothesis gut-check above.)

Public surface (import from pipeline.engine):
  init_session, save_session
  setup_run_logging, is_sub_pipeline
  run_hypothesis_loop, maybe_run_hypothesis
"""

from pipeline.engine.hypothesis import (
    format_validated_hypothesis_context,
    maybe_run_hypothesis,
    run_hypothesis_loop,
)
from pipeline.engine.run_logging import is_sub_pipeline, setup_run_logging
from pipeline.engine.session import init_session, save_session

__all__ = [
    # Session
    "init_session",
    "save_session",
    # Logging
    "setup_run_logging",
    "is_sub_pipeline",
    # Hypothesis loop
    "run_hypothesis_loop",
    "maybe_run_hypothesis",
    "format_validated_hypothesis_context",
]
