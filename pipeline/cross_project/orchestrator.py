#!/usr/bin/env python3
"""
cross_orchestrator.py — Cross-Project Multi-Agent Pipeline
===========================================================
For tasks that span multiple projects (e.g. Unity + API + Stats).

Pipeline:
  Phase 0: CROSS-PLAN  — Claude plans the full change across ALL projects,
                          creates interface contracts, splits into per-project subtasks
  Phase 1..N: per-project pipelines (reuses orchestrator.run_pipeline)
  Phase X: CONTRACT CHECK — Codex reviews each project for interface consistency

Usage:
    python cross_orchestrator.py \\
        --task "Add AdaptiveEvent analytics: Unity sends → API stores → Stats shows" \\
        --projects unity:/path/to/unity api:/path/to/api stats:/path/to/stats

    python cross_orchestrator.py \\
        --task-file cross_task.md \\
        --projects unity:/path/to/unity api:/path/to/api \
        --model opus
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.prompts.turn import PromptTurn

from agents.registry import PhaseAgentConfig
from agents.runtimes import AgentProvider
from core.infra import config

# ── Render helpers ─────────────────────────────────────────────────────────────
# ADR 0047 Phase B — the render surface lives in the peer module
# ``pipeline.cross_project.rendering``. Cross peers (``planning_loop``,
# ``handoff_payloads``, ``app.py``) import from there directly; only
# ``banner`` is re-exported here for the test patch surface (see below).
# Default profile when ``orcho cross`` is invoked without ``--profile``.
# The cross runner loads this profile, projects it into global + project
# steps via ``profile_projection.project_cross_profile``, runs global
# steps at the cross level (plan / validate_plan), then dispatches each
# child through ``run_pipeline`` with the projected project profile.
# ``contract_check`` is appended as a cross-only terminal gate (not part
# of any shipped profile's ``steps``).
#
# ADR 0047 Phase C — moved to ``pipeline.cross_project.constants`` to
# break the ``app_types → orchestrator`` import edge that would cycle
# in Phase D (``orchestrator → app → app_types → orchestrator``).
# Re-exported here at the same name for back-compat with the legacy
# ``from pipeline.cross_project.orchestrator import CROSS_DEFAULT_PROFILE``
# import path used by orchestrator's own body + by tests.
# ADR 0047 Phase D — the helpers `_PHASE_AGENT_ATTRS`,
# `_flatten_profile_entries`, `_agent_model_for_phase`,
# `_agent_entries_for_project_steps`, `_gate_will_run`,
# `_read_plan_file`, `_capture_invoke_usage`, `_print_usage_snapshot`,
# `_print_cross_planning_usage`, `_print_cross_checks_usage` moved to
# :mod:`pipeline.cross_project.app` alongside the run body. Anything
# that still needs them imports from there directly.
from pipeline.cross_project.constants import (  # noqa: E402, F401
    CROSS_DEFAULT_PROFILE,
)
from pipeline.cross_project.prompts import (
    _ORCHESTRATOR_ROOT,
    contract_review_focus as _contract_review_focus_impl,
    cross_plan_prompt as _cross_plan_prompt_impl,
    cross_plan_review_focus as _cross_plan_review_focus_impl,
    cross_replan_prompt as _cross_replan_prompt_impl,
    set_orchestrator_root as _set_orchestrator_root,
)

# ``banner`` is re-exported here because tests reach it through the
# orchestrator namespace (``orchestrator.banner(...)``); the rest of the
# render surface (``C``, ``preview``, ``success``, ``warn``,
# ``_render_cross_plan_preview``) lives in ``pipeline.cross_project.rendering``
# and every other caller imports it from there directly.
from pipeline.cross_project.rendering import banner  # noqa: F401
from pipeline.plugins import load_plugin
from pipeline.project.bootstrap import (  # noqa: E402, F401
    # Re-exported so legacy tests that
    # ``monkeypatch.setattr(cross, "_assert_fresh_run_dir_available", …)``
    # still affect the CLI's resolution path. ``cli.main`` reaches
    # this symbol via ``_xo._assert_fresh_run_dir_available(…)`` so
    # the patch lands on the actual callee.
    assert_fresh_run_dir_available as _assert_fresh_run_dir_available,
)
from pipeline.project.project_aliases import resolve_project_alias


def parse_projects(
    project_args: list[str],
    *,
    workspace: str | None = None,
) -> dict[str, Path]:
    """
    Parse "alias:/path/to/project" args or workspace project aliases.
    Returns {"alias": Path, ...}
    """
    result = {}
    for arg in project_args:
        if ":" not in arg:
            path = resolve_project_alias(arg, workspace=workspace)
            if path is None:
                raise ValueError(
                    f"Unknown project alias: {arg!r}. Use an alias from "
                    "`orcho workspace init` or pass alias:/path/to/project."
                )
            if not path.exists():
                raise FileNotFoundError(f"Project not found: {path}")
            result[arg] = path
            continue
        alias, path_str = arg.split(":", 1)
        path = Path(path_str).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Project not found: {path}")
        result[alias] = path
    return result


def build_cross_context(projects: dict[str, Path]) -> str:
    """
    Build a combined context string from all project plugins.
    Injected into the cross-project planning prompt.
    """
    parts = []
    for alias, path in projects.items():
        plugin = load_plugin(str(path))
        parts.append(
            f"--- Project [{alias}] at {path} ---\n"
            f"Name: {plugin.name}\n"
            f"Language: {plugin.language}\n"
            f"Architecture: {plugin.architecture}\n"
            f"Key dirs: {', '.join(plugin.file_hints)}\n"
        )
    return "\n".join(parts)


def cross_plan_prompt(
    task: str,
    projects: dict[str, Path],
    cross_artifacts_dir: Path,
    *,
    professional_prompt_mode: str | None = None,
) -> "PromptTurn":
    _set_orchestrator_root(_ORCHESTRATOR_ROOT)
    return _cross_plan_prompt_impl(
        task,
        projects,
        cross_artifacts_dir,
        professional_prompt_mode=professional_prompt_mode,
    )


def cross_plan_review_focus(
    task: str,
    aliases: list[str],
    *,
    plan_artifact: str = "",
    plan_artifact_path: str = "cross_plan.md",
    professional_prompt_mode: str | None = None,
) -> "PromptTurn":
    _set_orchestrator_root(_ORCHESTRATOR_ROOT)
    return _cross_plan_review_focus_impl(
        task,
        aliases,
        plan_artifact=plan_artifact,
        plan_artifact_path=plan_artifact_path,
        professional_prompt_mode=professional_prompt_mode,
    )


def cross_replan_prompt(
    task: str,
    critique: str,
    projects: dict[str, Path],
    cross_artifacts_dir: Path,
    *,
    professional_prompt_mode: str | None = None,
) -> "PromptTurn":
    _set_orchestrator_root(_ORCHESTRATOR_ROOT)
    return _cross_replan_prompt_impl(
        task,
        critique,
        projects,
        cross_artifacts_dir,
        professional_prompt_mode=professional_prompt_mode,
    )


def contract_review_focus(task: str, projects: dict[str, Path]) -> "PromptTurn":
    _set_orchestrator_root(_ORCHESTRATOR_ROOT)
    return _contract_review_focus_impl(task, projects)




# ════════════════════════════════════════════════════════════════════════════
#  CROSS-PROJECT PIPELINE
# ════════════════════════════════════════════════════════════════════════════
# ADR 0047 Phase D — `_read_plan_file`, `_capture_invoke_usage`,
# `_print_usage_snapshot`, `_print_cross_planning_usage`,
# `_print_cross_checks_usage` moved to
# :mod:`pipeline.cross_project.app` alongside the run body.


def run_cross_pipeline(
    task: str,
    projects: dict[str, Path],
    max_rounds: int = 1,
    model: str = config.phase_model("implement", "claude-opus-4-8[1m]"),
    output_dir: Path | None = None,
    dry_run: bool = False,
    mock: bool = False,
    provider: "AgentProvider | None" = None,
    phase_config: PhaseAgentConfig | None = None,
    cross_mode: str = "full",
    plan_file: str | None = None,
    resume_from: str | None = None,
    hypothesis_enabled: bool | None = None,
    profile_name: str = CROSS_DEFAULT_PROFILE,
    operator_decisions: "tuple | None" = None,
    no_interactive: bool = False,
    resumed_meta: "dict | None" = None,
    resume_mode: str | None = None,
    followup_parent_run_id: str | None = None,
    followup_parent_run_dir: str | None = None,
    followup_parent_status: str | None = None,
    followup_base_task: str | None = None,
    followup_session_seeds_per_alias: (
        "dict[str, dict[str, str]] | None"
    ) = None,
) -> dict:
    """Cross-project orchestration entry (back-compat wrapper).

    ADR 0047 Phase D — the body of this function moved to
    :func:`pipeline.cross_project.app._run_cross_pipeline_session`.
    This wrapper preserves the legacy 23-kwarg signature (pinned by
    ``tests/unit/pipeline/cross_project/test_cross_run_request.py``)
    so SDK / cross-project / CLI callers that pass these arguments
    flat continue working byte-identical. Builds a
    :class:`pipeline.cross_project.app_types.CrossRunRequest` from the
    positional/keyword arguments and routes through
    :func:`pipeline.cross_project.app.run_cross_project_pipeline`,
    returning the resulting ``session`` dict.

    ``profile_name`` is the single workflow knob: the requested profile
    is loaded, projected into ``global_steps`` + ``project_steps`` by
    ``profile_projection.project_cross_profile``, and applied to both
    levels. Children run an in-memory ``Profile`` built from
    ``project_steps`` — no separate sub-profile flag.

    ``cross_mode`` selects which slice runs: ``"plan"`` stops after
    cross_plan.md, ``"full"`` runs the full projection plus the cross-
    only ``contract_check`` terminal gate.

    Status semantics: returns the session dict with
    ``status="done" | "failed" | "awaiting_human_review"``. The CLI
    entrypoint maps these to exit codes — this function never calls
    ``sys.exit``.
    """
    from pipeline.cross_project.app import run_cross_project_pipeline
    from pipeline.cross_project.app_types import CrossRunRequest
    return run_cross_project_pipeline(
        CrossRunRequest(
            task=task,
            projects=projects,
            max_rounds=max_rounds,
            model=model,
            output_dir=output_dir,
            dry_run=dry_run,
            mock=mock,
            provider=provider,
            phase_config=phase_config,
            cross_mode=cross_mode,
            plan_file=plan_file,
            resume_from=resume_from,
            hypothesis_enabled=hypothesis_enabled,
            profile_name=profile_name,
            operator_decisions=operator_decisions,
            no_interactive=no_interactive,
            resumed_meta=resumed_meta,
            resume_mode=resume_mode,
            followup_parent_run_id=followup_parent_run_id,
            followup_parent_run_dir=followup_parent_run_dir,
            followup_parent_status=followup_parent_status,
            followup_base_task=followup_base_task,
            followup_session_seeds_per_alias=followup_session_seeds_per_alias,
        ),
    ).session


# ADR 0047 Phase G r1 — ``main`` and ``print_error`` live in
# :mod:`pipeline.cross_project.cli`. Re-exported here at the legacy
# names so existing test patches (~30 sites under
# ``tests/unit/cli/test_cross_orchestrator_main.py``) and the SDK
# bridge in ``sdk.runner.run_cross_from_args`` continue to resolve
# through the orchestrator namespace during the Phase G → Phase I
# transition.
#
# The re-export is **lazy** (PEP 562 module-level ``__getattr__``) so
# ``import pipeline.cross_project`` — which loads this module via the
# package ``__init__`` — does NOT eagerly pull in
# :mod:`pipeline.cross_project.cli` (and its argparse / sys.exit
# surface). The eager edge was the Phase G r0 architectural tail the
# reviewer flagged: simple ``import pipeline.cross_project`` triggered
# ``RuntimeWarning: ... found in sys.modules after import of
# package ...`` when the CLI was also driven through ``python -m
# pipeline.cross_project.cli``, because both paths populate the same
# module entry.
#
# Identity invariant ``orchestrator.main is cli.main`` (pinned by
# :func:`tests.unit.pipeline.cross_project.test_cross_cli_isolation.
# test_cli_main_is_callable_from_canonical_module`) still holds —
# each access re-imports ``cli`` (cached after first hit) so the same
# function object is returned. ``monkeypatch.setattr(orchestrator,
# "main", fake)`` still wins: the patch writes into this module's
# ``__dict__``, which beats ``__getattr__`` on subsequent reads
# (Python attribute-lookup order).
_LAZY_CLI_ATTRS = frozenset({"main", "print_error"})


def __getattr__(name: str):  # noqa: ANN202
    """PEP 562 hook — lazy re-export of cli leaf attributes."""
    if name in _LAZY_CLI_ATTRS:
        from pipeline.cross_project import cli as _cli

        return getattr(_cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
