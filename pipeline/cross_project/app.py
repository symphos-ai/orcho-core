"""Application-service facade for the cross-project pipeline.

Created in ADR 0047 Phase D. This module holds:

* :func:`run_cross_project_pipeline` — the **typed orchestration
  boundary**. Accepts a
  :class:`pipeline.cross_project.app_types.CrossRunRequest`, returns
  a :class:`pipeline.cross_project.app_types.CrossRunResult`.
  Documented entry surface for the cross-project pipeline. The legacy
  23-kwarg :func:`pipeline.cross_project.orchestrator.run_cross_pipeline`
  is now a thin back-compat wrapper that builds a ``CrossRunRequest``
  and routes through this function — direction enforced by the AST
  guard in :mod:`tests.unit.pipeline.cross_project.test_cross_app_isolation`.

The run coordinator lives in :mod:`pipeline.cross_project.session_run`
(:func:`pipeline.cross_project.session_run.run_cross_pipeline_session`),
which sequences the focused setup modules and the existing domain
modules and returns ``(session, output_dir, run_id)`` so the typed
boundary surfaces real run identifiers in :class:`CrossRunResult`
without guesswork. It never calls ``sys.exit`` — terminal status lives
in the returned session. This facade holds no orchestration of its own.

**Import discipline (ADR 0047 D2).** This module MUST NOT import
from :mod:`pipeline.cross_project.orchestrator`. The orchestrator is
the back-compat wrapper that routes through ``run_cross_project_pipeline``;
the reverse direction recreates the cycle Phase D exists to break.
:mod:`pipeline.cross_project.app_types` and
:mod:`pipeline.cross_project.session_run` are also forbidden from
importing the orchestrator (verified by the same AST guard).
"""

# NOTE: deliberately NOT using ``from __future__ import annotations``.
# The Phase D signature lock for ``run_cross_project_pipeline`` (in
# ``tests/unit/pipeline/cross_project/test_cross_run_request.py``)
# pins the resolved form ``(request: pipeline.cross_project.app_types.
# CrossRunRequest) -> pipeline.cross_project.app_types.CrossRunResult``;
# enabling PEP 563 here would stringify every annotation and break the
# byte-for-byte signature contract. Mirrors the project-side
# ``pipeline.project.app`` discipline.
from pipeline.cross_project.app_types import CrossRunRequest, CrossRunResult
from pipeline.cross_project.session_run import run_cross_pipeline_session

# ── Public typed orchestration boundary ───────────────────────────────


def run_cross_project_pipeline(
    request: CrossRunRequest,
) -> CrossRunResult:
    """Typed orchestration boundary for the cross-project pipeline.

    Owns the run lifecycle. Delegates to
    :func:`pipeline.cross_project.session_run.run_cross_pipeline_session`,
    the coordinator holding the setup → planning → dispatch → contract →
    release → finalize body. Wraps the persisted ``session`` dict plus the
    **actual** run identifiers (``output_dir``, ``run_id`` =
    ``session_ts``) into a :class:`CrossRunResult`.

    Mirrors :func:`pipeline.project.app.run_project_pipeline`'s shape
    one-for-one. The legacy 23-kwarg
    :func:`pipeline.cross_project.orchestrator.run_cross_pipeline` is
    now a thin back-compat wrapper that builds a
    :class:`CrossRunRequest` and routes through this function.
    """
    session, output_dir, run_id = run_cross_pipeline_session(request)
    return CrossRunResult(
        session=session,
        output_dir=output_dir,
        run_id=run_id,
    )


__all__ = ["run_cross_project_pipeline"]
