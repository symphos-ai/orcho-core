"""Application-service facade for the project pipeline.

Moved out of :mod:`pipeline.project_orchestrator` per ADR 0042 Phase I.
This module holds:

* :func:`run_project_pipeline` — the **typed orchestration boundary**.
  Accepts a :class:`pipeline.project.types.ProjectRunRequest`, returns
  a :class:`pipeline.project.types.ProjectRunResult`. Documented entry
  surface for the project pipeline. Legacy terminal output is
  preserved (banners + success chips fire via
  :func:`pipeline.project.finalization.finalize_with_terminal_output`)
  — this is a compat/parity boundary for this pass, not yet a fully
  silent UI surface. A later ADR elevates a truly silent app-level
  entry; UI clients that want silent structured finalization today
  compose against the lower-level services
  (:mod:`pipeline.project.bootstrap`,
  :mod:`pipeline.project.profile_dispatch`,
  :mod:`pipeline.project.handoff`,
  :func:`pipeline.project.finalization.finalize_project_run`).

* :func:`run_pipeline` — the **back-compat positional/keyword surface**.
  Signature byte-identical to the pre-Phase-I orchestrator function;
  pinned by ``tests/unit/pipeline/test_project_run_request.py``. SDK,
  cross-project dispatch, integration tests, and the ``orcho-run``
  CLI all call this name. The body builds a :class:`ProjectRunRequest`
  from its 28 positional/keyword arguments and delegates to
  :func:`run_project_pipeline`, returning the resulting
  ``session`` dict. The typed boundary is the orchestration owner;
  this function is a thin compatibility wrapper.

* :func:`print_error` — the shared CLI-facing red-stderr error printer.

The run coordinator lives in :mod:`pipeline.project.session_run`
(:func:`pipeline.project.session_run.run_project_pipeline_session`),
which wires the focused setup modules, builds the run, and dispatches
it. This facade holds no setup logic of its own; tests that need the
coordinator internals (including the ``load_plugin`` test-patch
surface) import them from :mod:`pipeline.project.session_run`, their
real home.
"""

# NOTE: deliberately NOT using ``from __future__ import annotations``.
# The legacy ``pipeline.project_orchestrator`` did not opt into PEP 563,
# so ``inspect.signature(run_pipeline)`` returned resolved annotation
# objects (e.g. ``task: str``) rather than string forms (``task: 'str'``).
# The Phase B signature-lock test in
# ``tests/unit/pipeline/test_project_run_request.py`` pins exactly that
# resolved form; enabling PEP 563 here would silently stringify every
# annotation and break the byte-for-byte signature contract.
# Phase H may revisit if ``main()`` + CLI helpers also avoid PEP 563.
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from core.infra import config
from core.io.ansi import C, paint
from pipeline.project.constants import DEFAULT_PROFILE_NAME
from pipeline.project.session_run import run_project_pipeline_session
from pipeline.project.types import (
    ProjectRunRequest,
    ProjectRunResult,
)

# ``AgentProvider`` is intentionally a forward reference (not a runtime
# import) — it appears in the SDK schema as ``"AgentProvider | None"`` and
# importing it at module level would change the wire format to a fully
# qualified ``agents.runtimes._strategy.AgentProvider | None``. Keep the
# TYPE_CHECKING guard so static type checkers still resolve the symbol.
if TYPE_CHECKING:
    from agents.runtimes._strategy import AgentProvider


def print_error(message: str) -> None:
    """Print a CLI-facing error in red on stderr.

    Lives here because ``run_pipeline`` and the isolation setup surface
    user-actionable error messages on the same shape. The CLI layer
    (:mod:`pipeline.project.cli`) and :mod:`pipeline.project.isolation_setup`
    re-use the same function via an import.
    """
    # Stderr-bound output passes stream=sys.stderr so auto-detect
    # consults stderr's TTY status, not sys.stdout's — see Terminal
    # color discipline rule in orcho-core/CLAUDE.md.
    print(
        f"{paint('Error:', C.RED, C.BOLD, stream=sys.stderr)} "
        f"{paint(message, C.RED, stream=sys.stderr)}",
        file=sys.stderr,
    )


def run_project_pipeline(
    request: ProjectRunRequest,
) -> ProjectRunResult:
    """Typed orchestration boundary for the project pipeline.

    Owns the run lifecycle. Delegates to
    :func:`pipeline.project.session_run.run_project_pipeline_session`,
    the coordinator that wires the focused setup modules, builds the run,
    and dispatches it, then wraps the persisted session plus the
    **actual** run
    identifiers (``output_dir``, ``run_id`` = ``session_ts``) into a
    :class:`ProjectRunResult`. No guesswork on identifiers — the
    private impl returns the real locals it computed during setup.

    Architecture direction (ADR 0042 Phase I corrected by review):
    the typed boundary is the orchestration owner; the legacy
    :func:`run_pipeline` is a back-compat wrapper that builds a
    ``ProjectRunRequest`` and routes through this function. UI
    clients consume the typed shape; the wide kwarg surface stays
    available exclusively for existing SDK / cross-project / CLI
    callers.

    Terminal output (banners + success chips) still fires through
    :func:`pipeline.project.finalization.finalize_with_terminal_output`
    during dispatch — this is the **compat/parity** boundary for
    this pass, not yet a fully silent UI surface. A later ADR
    elevates a silent app-level entry by routing finalization through
    :func:`pipeline.project.finalization.finalize_project_run`
    instead. That ADR re-introduces a ``deps`` (or
    presentation-policy) parameter at this seam when it has a
    concrete injection contract to ship. The Phase I ``deps:
    ProjectRunDeps | None = None`` placeholder was retired in Phase J
    per ADR r4 P2 ("empty ceremonial seams must not survive past J").
    """
    session, output_dir, session_ts = run_project_pipeline_session(request)
    return ProjectRunResult(
        session=session,
        output_dir=output_dir,
        run_id=session_ts,
    )


def run_pipeline(
    task: str,
    project_dir: str,
    max_rounds: int = 1,
    model: str = config.phase_model("implement", "claude-opus-4-8[1m]"),
    output_dir: Path | None = None,
    dry_run: bool = False,
    phase_config: PhaseAgentConfig | None = None,
    session_mode: SessionMode = SessionMode.AUTO,
    profile_name: str = DEFAULT_PROFILE_NAME,
    ma_artifacts_dir_override: str | None = None,
    provider: "AgentProvider | None" = None,
    resume_from: str | None = None,
    attachments: tuple = (),
    parent_run_id: str | None = None,
    project_alias: str | None = None,
    hypothesis_enabled: bool | None = None,
    profile_obj: "Any | None" = None,  # Profile, lazy-imported below
    plan_source: str = "local",
    handoff_path: str | None = None,
    resume_mode: str | None = None,
    followup_parent_run_id: str | None = None,
    followup_parent_run_dir: str | None = None,
    followup_parent_status: str | None = None,
    followup_base_task: str | None = None,
    followup_session_seeds: dict[str, str] | None = None,
    followup_child_status: str | None = None,
    followup_active_handoff_id: str | None = None,
    no_interactive: bool = False,
    from_run_plan_parent_dir: "Path | None" = None,
    worktree_config_override: dict[str, Any] | None = None,
) -> dict:
    """Back-compat positional/keyword surface for the project pipeline.

    Signature byte-for-byte preserved from the pre-Phase-I orchestrator
    function. Pinned by
    ``tests/unit/pipeline/test_project_run_request.py::TestSignatureLock``
    — drift here breaks SDK / cross-project / CLI callers that still
    pass these 28 kwargs flat. **Do not change the signature.**

    Body builds a :class:`ProjectRunRequest` from the positional/keyword
    arguments and routes through :func:`run_project_pipeline`, which is
    the actual orchestration owner. Returns the persisted session
    dict (``ProjectRunResult.session``) so existing callers that
    expect ``dict`` keep working unchanged.

    Notable parameters (the rest are passthrough):

    * ``profile_name`` (Phase 6) — semantic work-kind profile name
      string. Replaced the legacy ``pipeline_mode: PipelineMode`` enum.
      Pass ``"feature"`` (default), ``"small_task"``,
      ``"complex_feature"``, ``"planning"``, ``"delivery_audit"``,
      ``"code_review"``, ``"research"``, ``"refactor"``, ``"migration"``,
      the internal ``"task"``, or any custom profile shipped via
      ``orcho.profiles`` entry_points. The legacy ``skip_plan`` flag is
      gone — pass ``profile_name="task"`` for the build-only flow.
    * ``profile_obj`` — short-circuits ``profile_name`` resolution.
      Used by the cross-project orchestrator to pass a projected
      child profile.
    * ``from_run_plan_parent_dir`` — follow-up runs that reuse a
      parent run's parsed plan. Forces ``plan_source="run"`` when
      the caller leaves it at the default ``"local"``.

    Phase J may add a deprecation note here once the SDK / MCP wire
    is fully migrated to ``run_project_pipeline``; the function
    itself stays on the stable shim surface.
    """
    request = ProjectRunRequest(
        task=task,
        project_dir=project_dir,
        max_rounds=max_rounds,
        model=model,
        output_dir=output_dir,
        dry_run=dry_run,
        phase_config=phase_config,
        session_mode=session_mode,
        profile_name=profile_name,
        ma_artifacts_dir_override=ma_artifacts_dir_override,
        provider=provider,
        resume_from=resume_from,
        attachments=attachments,
        parent_run_id=parent_run_id,
        project_alias=project_alias,
        hypothesis_enabled=hypothesis_enabled,
        profile_obj=profile_obj,
        plan_source=plan_source,
        handoff_path=handoff_path,
        resume_mode=resume_mode,
        followup_parent_run_id=followup_parent_run_id,
        followup_parent_run_dir=followup_parent_run_dir,
        followup_parent_status=followup_parent_status,
        followup_base_task=followup_base_task,
        followup_session_seeds=followup_session_seeds,
        followup_child_status=followup_child_status,
        followup_active_handoff_id=followup_active_handoff_id,
        no_interactive=no_interactive,
        from_run_plan_parent_dir=from_run_plan_parent_dir,
        worktree_config_override=worktree_config_override,
    )
    return run_project_pipeline(request).session
