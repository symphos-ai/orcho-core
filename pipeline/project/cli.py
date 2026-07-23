"""``orcho-run`` CLI adapter.

Moved out of :mod:`pipeline.project_orchestrator` per ADR 0042 Phase H.
This module is the **leaf** of the project pipeline layering: nothing
under :mod:`pipeline.project`, :mod:`pipeline.cross_project`,
:mod:`pipeline.runtime`, :mod:`pipeline.control`, or :mod:`sdk` may
import from here. CLI-targeted tests under ``tests/unit/cli/**`` are
the only consumers permitted by ADR 0042 stop condition #9.

Three concerns live here:

* :func:`main` — the ``orcho-run`` entry point. Parses 33+ argparse
  flags, hydrates ``--resume`` / ``--from-run-plan`` follow-up
  context, builds the :class:`pipeline.agents.registry.PhaseAgentConfig`
  via :func:`pipeline.project.phase_config.build_phase_config_from_overrides`,
  and dispatches into :func:`pipeline.project.app.run_pipeline`.
  Returns the rc based on the persisted session status (``done`` →
  0, ``halted`` → 3 / 4 depending on cause).

* :func:`_resolve_resume_latest` — handles the ``--resume latest``
  sentinel. CLI-only because it ``sys.exit(2)`` s on workspace /
  discovery failure (with a CLI-shaped error message via
  :func:`print_error`); programmatic callers should use
  :func:`pipeline.control.resolve_latest_run` directly.

* :func:`_apply_resume_runs_context` — aligns env-backed config to
  the resolved runs dir when ``--resume`` is invoked without
  ``--project``. CLI-only for the same reason.

A bottom-of-file ``if __name__ == "__main__": main()`` guard makes
``python -m pipeline.project.cli --help`` work as a smoke command.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from agents.protocols import SessionMode
from core.infra import config
from core.io.terminal_input import stdio_interactive as _stdio_interactive
from core.observability.logging import (
    apply_output_mode,
    warn,
)
from pipeline.plugins import load_plugin
from pipeline.project.app import (
    print_error,
    run_project_pipeline,
)
from pipeline.project.auto_detect import (
    AUTO_DETECT_PROFILE_TOKEN,
    ProviderWorkKindDetector,
    resolve_auto_detect,
    scoped_autodetect_decision_env,
)
from pipeline.project.bootstrap import (
    PhaseHandoffHaltedError,
    RunIdCollisionError,
    autoderive_workspace_from_cwd as _autoderive_workspace_from_cwd,
    infer_workspace_from_project as _infer_workspace_from_project,
)
from pipeline.project.constants import DEFAULT_PROFILE_NAME
from pipeline.project.correction_followup import (
    drive_correction_followups as _drive_correction_followups,
    is_correction_fix_halt as _is_correction_fix_halt,
)
from pipeline.project.followup_worktree import FollowupPlanContinuationError
from pipeline.project.phase_config import build_phase_config_from_overrides
from pipeline.project.profile_setup import _resolve_profile_name, _resolve_v2_profile
from pipeline.project.project_aliases import resolve_project_alias
from pipeline.project.types import ProjectRunRequest
from pipeline.project.workspace_picker import (
    WorkspaceProjectPickError,
    pick_project_for_fresh_run,
)
from pipeline.runtime.resume import LoopResumeBlockedError

_PROJECT_GROUP_CHILD_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "composer.json",
    "go.mod",
    "Cargo.toml",
)
_PROJECT_GROUP_EXCLUDED_CHILDREN = {
    "workspace-orchestrator",
    "node_modules",
    ".venv",
    ".git",
    "__pycache__",
    ".idea",
    ".vscode",
}


def run_pipeline(**kwargs: object) -> dict:
    """CLI patch seam that routes through the typed project boundary."""
    request = ProjectRunRequest.from_kwargs(**kwargs)
    return run_project_pipeline(request).session


def _run_correction_followup_prompt(
    *, run_id: str, meta: dict, runs_dir: Path | None,
) -> Literal["not_correction", "exit", "started", "error"]:
    """Render the correction-only followup/exit interaction for ``--resume``.

    The explicit outcome lets :func:`main` preserve a successful exit status
    only for an operator-selected exit or a launched child.  Invalid or
    blocked correction decisions are operator-action errors, not successful
    resumes.
    """
    from pipeline.control.continuation import resolve_continuation_decision
    from sdk.run_control.launch import (
        CorrectionFollowupLaunchRequest,
        launch_correction_followup,
    )

    parent_dir = runs_dir / run_id if runs_dir is not None else None
    decision = resolve_continuation_decision(
        run_id=run_id, meta=meta, parent_run_dir=parent_dir,
    )
    if decision.continuation_subject != "retained_change":
        return "not_correction"
    if decision.blocked:
        print_error(f"Correction follow-up is blocked: {decision.reason}")
        return "error"

    print(f"Run {run_id} requires a correction follow-up.")
    choice = input("Choose [followup/exit] (followup): ").strip().lower() or "followup"
    if choice == "exit":
        return "exit"
    if choice != "followup":
        print_error("Choose exactly 'followup' or 'exit'.")
        return "error"
    comment = input("Operator comment (required): ").strip()
    if not comment:
        print_error("Operator comment is required for a correction follow-up.")
        return "error"
    launched = launch_correction_followup(
        CorrectionFollowupLaunchRequest(
            parent_run_id=run_id,
            runs_dir=str(runs_dir) if runs_dir is not None else None,
            operator_comment=comment,
        ),
    )
    print(f"Started correction follow-up {launched.run.run_id}.")
    return "started"


def _looks_like_single_project(path: Path) -> bool:
    return any((path / marker).exists() for marker in _PROJECT_GROUP_CHILD_MARKERS)


def _detect_child_projects(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    projects: list[Path] = []
    for child in sorted(path.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if child.name in _PROJECT_GROUP_EXCLUDED_CHILDREN:
            continue
        if child.name.startswith("."):
            continue
        if _looks_like_single_project(child):
            projects.append(child)
    return projects


def _reject_project_group_root_if_needed(project: str) -> None:
    project_path = Path(project).resolve()
    if _looks_like_single_project(project_path):
        return
    child_projects = _detect_child_projects(project_path)
    if not child_projects:
        return

    lines = [
        "Project path looks like a project group root, not a single project:",
        f"  {project_path}",
        "",
        "Detected child projects:",
    ]
    lines.extend(f"  - {p.name}: {p}" for p in child_projects)
    lines.extend([
        "",
        "Run a concrete project, for example:",
        f"  orcho run --project {shlex.quote(str(child_projects[0]))} "
        "--task '...'",
    ])
    if len(child_projects) > 1:
        projects_arg = " ".join(
            shlex.quote(f"{p.name}:{p}") for p in child_projects
        )
        lines.extend([
            "",
            "Or run a cross-project task:",
            f"  orcho cross --projects {projects_arg} --task '...'",
        ])
    print_error("\n".join(lines))
    sys.exit(2)


def _resolve_resume_latest(
    *,
    prefer_incomplete: bool = False,
    workspace: str | None = None,
    runs_dir: Path | None = None,
) -> str:
    """Resolve the ``--resume latest`` sentinel to the newest single-project
    run id on disk.

    Wraps :func:`pipeline.control.resolve_latest_run` with ``kind="run"``
    so cross-project runs do not get picked up by ``orcho run --resume``.
    Exits the process with rc=2 on workspace/discovery failure — same
    convention as the surrounding resume-meta loader.

    When ``runs_dir`` is supplied it is used as the source of truth; this
    keeps the selected latest id and the later ``meta.json`` load on the
    same directory. When ``workspace`` is supplied it is forwarded to ``find_runs_dir``
    so the explicit ``--workspace`` flag bypasses cwd walk-up (which
    otherwise beats ``$ORCHO_WORKSPACE`` and finds the wrong workspace
    when the CLI runs from inside another workspace tree).
    """
    from pipeline.control import LatestRunNotFound, resolve_latest_run
    from sdk.runs import NoWorkspace, find_runs_dir
    try:
        resolved_runs_dir = runs_dir or (
            find_runs_dir(workspace=workspace) if workspace
            else find_runs_dir()
        )
    except NoWorkspace as exc:
        print_error(str(exc))
        sys.exit(2)
    try:
        return resolve_latest_run(
            runs_dir=resolved_runs_dir,
            kind="run",
            prefer_incomplete=prefer_incomplete,
            include_terminal_success=True,
            require_existing_project=True,
        )
    except LatestRunNotFound as exc:
        print_error(str(exc))
        sys.exit(2)


def _apply_resume_runs_context(
    *,
    workspace: str | None,
) -> Path:
    """Resolve the resume runs dir and align env-backed config to it.

    Bare ``--resume`` is allowed to omit ``--project`` because the task and
    project come from the parent ``meta.json``. In that shape the usual
    project-based workspace inference cannot run, so we use the SDK's run
    discovery resolver (which includes cwd walk-up) as the source of truth
    and point ``config.get_runs_dir()`` at the same directory before any
    config or meta lookup happens.
    """
    from sdk.runs import NoWorkspace, find_runs_dir

    try:
        runs_dir = (
            find_runs_dir(workspace=workspace) if workspace
            else find_runs_dir()
        )
    except NoWorkspace as exc:
        print_error(str(exc))
        sys.exit(2)

    if runs_dir.name == "runs" and runs_dir.parent.name == "runspace":
        runspace = runs_dir.parent.resolve()
        workspace_dir = runspace.parent
        os.environ["ORCHO_WORKSPACE"] = str(workspace_dir)
        os.environ["ORCHO_RUNSPACE"] = str(runspace)
        config._reset_config()
    return runs_dir



def _print_active_followup_recommendation(active, *, parent_run_id: str) -> None:
    """Non-interactive lineage hint: recommend resuming the active child.

    Never switches the run id silently — only prints the command the
    operator can copy-paste to resume the in-progress follow-up.
    """
    handoff_hint = (
        f", active handoff {active.active_handoff_id}"
        if active.active_handoff_id else ""
    )
    print(
        f"Run {parent_run_id} has an in-progress follow-up "
        f"{active.child_run_id} (status: {active.child_status}{handoff_hint}). "
        f"Resuming the parent as requested; to resume the follow-up instead:\n"
        f"  orcho run --resume {active.child_run_id}",
        file=sys.stderr,
    )


def _handle_checkpoint_resume_preflight(
    *,
    run_id: str,
    run_dir: Path,
    meta: dict,
    no_interactive: bool,
) -> None:
    """Intercept a checkpoint resume into an undecided active handoff.

    Without this, ``run_pipeline`` re-enters and ``apply_phase_handoff_
    resume`` trips ``load_handoff_decision_validated`` with a RuntimeError
    (active ``meta.phase_handoff`` but no decision artifact). Detection
    lives in :mod:`pipeline.control.resume_preflight`; this stays thin.

    Interactive (TTY, not ``--no-interactive``): show the same menu a
    fresh handoff uses, record the decision through the SDK, then return
    so the same command continues the resume (``run_pipeline`` now finds
    the artifact). Non-interactive: print a copy-pasteable hint and exit
    ``4`` (handoff pending) without mutating the run. An aborted prompt
    also leaves the run paused (exit ``4``).

    The interactive vs non-interactive decision uses the SAME gate a
    freshly fired handoff uses (:func:`should_prompt_for_phase_handoff`):
    a piped / CI run with non-TTY stdin/stdout — even *without*
    ``--no-interactive`` — takes the hint path and never records a
    decision (no run mutation).
    """
    from pipeline.control.handoff_prompt import (
        should_prompt_for_phase_handoff,
    )
    from pipeline.control.resume_preflight import (
        detect_active_handoff_without_decision,
        render_noninteractive_hint,
        resolve_active_handoff_interactively,
    )

    preflight = detect_active_handoff_without_decision(
        run_id=run_id, run_dir=run_dir, meta=meta,
    )
    if preflight is None:
        return

    if not should_prompt_for_phase_handoff(no_interactive=no_interactive):
        print(render_noninteractive_hint(preflight), file=sys.stderr)
        sys.exit(4)

    recorded = resolve_active_handoff_interactively(
        preflight, runs_dir=run_dir.parent,
    )
    if not recorded:
        print(
            f"No decision recorded for handoff {preflight.handoff_id!r}; "
            f"leaving run {preflight.run_id} paused.",
            file=sys.stderr,
        )
        sys.exit(4)


def _build_mock_work_kind_detector(task: str):
    """A hermetic auto-detect detector for ``--mock`` runs.

    Mock runs must not call a real provider detector, but they MUST still carry
    the deterministic topology recommendation exactly like
    :meth:`pipeline.project.auto_detect.ProviderWorkKindDetector.detect` merges
    it (F3) — otherwise a mock auto-detect smoke would report ``mono`` / empty
    projects and never exercise the cross-recommendation projection. The
    topology axis is a provider-neutral heuristic over the task text (no LLM),
    so applying it here keeps the mock and provider paths' topology /
    delivery_projects projection identical.
    """
    from pipeline.runtime.topology_detection import recommend_topology
    from pipeline.runtime.work_kind_detection import (
        AutoDetectDecision,
        StaticWorkKindDetector,
    )

    topology = recommend_topology(task)
    return StaticWorkKindDetector(
        AutoDetectDecision(
            recommended_profile=DEFAULT_PROFILE_NAME,
            recommended_mode="fast",
            confidence=1.0,
            rationale="mock auto-detect",
            recommended_topology=topology.topology,
            delivery_projects=topology.projects,
            topology_reason=topology.reason,
        )
    )


def main():
    from core.io.encoding import ensure_utf8_stdio

    # Force UTF-8 stdio before any rendering so non-ASCII output (emoji / box
    # drawing) does not crash on a legacy Windows console code page.
    ensure_utf8_stdio()

    parser = argparse.ArgumentParser(
        description="Multi-Agent Core: Antigravity orchestrates Claude Code + Codex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Plugin:
  Add .orcho/multiagent/plugin.py to your project to inject project context.
  See plugin_loader.py -> PluginConfig for all available fields.

Examples:
  python orchestrator.py --task "Add feature X" --project /path/to/project
  python orchestrator.py --task-file task.md --project . --max-rounds 2
  python orchestrator.py --task "Fix bug" --project . --profile task --model opus
        """
    )
    parser.add_argument("--task", "-t",   type=str, help="Task description")
    parser.add_argument(
        "--task-file",
        type=str,
        help="Read task from .md file; bare NAME.md resolves from .orcho/.task-files",
    )
    parser.add_argument(
        "--project", "-p", type=str, default=None,
        help=(
            "Project directory to run against. Required for fresh runs; "
            "optional on --resume (resolves from persisted meta.json)."
        ),
    )
    parser.add_argument(
        "--workspace", "-w", type=str, default=None,
        help="Path to workspace-orchestrator dir. Required (or set $ORCHO_WORKSPACE) "
             "so pipeline runs land in <workspace>/runspace/runs/<ts>/, not in the orcho engine repo.",
    )
    parser.add_argument(
        "--no-interactive", action="store_true",
        help=(
            "Never prompt on stdin. Skips the resume-intent chooser "
            "(checkpoint vs follow-up) and any other interactive UX; "
            "the orchestrator falls back to a non-interactive default "
            "or exits cleanly with a hint. Use for MCP / CI transports."
        ),
    )
    parser.add_argument("--max-rounds",   type=int, default=1)
    parser.add_argument(
        "--session-split",
        action="append",
        default=None,
        metavar="PHASE=SPLIT",
        help=(
            "Override a profile phase's prompt-session split for this run "
            "(split: stateless, per_phase, per_role, common). May repeat."
        ),
    )
    parser.add_argument(
        "--mock-validate-plan-reject", type=int, default=0, metavar="N",
        help="Mock-only: how many initial validate_plan reviews emit "
             "REJECTED JSON before flipping to APPROVED JSON. Lets you "
             "exercise the manual-approval gate UI without a real LLM. "
             "Default 0 (always approve). Combined with --mock.",
    )
    parser.add_argument(
        "--hypothesis", action=argparse.BooleanOptionalAction, default=None,
        help="Override the pre-PLAN hypothesis gut-check on/off for fresh "
             "runs. ``--hypothesis`` forces it on, ``--no-hypothesis`` skips "
             "it entirely (useful for feature work where the diagnostic "
             "step adds no value). Checkpoint resumes and follow-up runs "
             "always skip hypothesis. Omit to use the selected profile's "
             "plan-step ``hypothesis`` value on fresh runs.",
    )
    parser.add_argument("--model",        type=str, default=None)
    # Phase 6: ``--skip-plan`` REMOVED. Use ``--profile task`` for the
    # build-only flow (or any custom profile that omits the plan loop).
    parser.add_argument("--output-dir",   type=str,
                        help="Output dir for logs/session")
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--mock",         action="store_true",
                        help="Run full pipeline flow with mock agents (no real API calls)")
    parser.set_defaults(output=config.cli_output_mode())
    parser.add_argument(
        "--output", choices=("summary", "live", "debug"), dest="output",
        help="Run transcript mode: summary (default), live, or debug. "
             "Default is configurable via `cli.output_mode` in "
             "config.local.json or the ORCHO_OUTPUT_MODE env var.",
    )
    parser.add_argument(
        "--stream-output", action="store_const", const="live", dest="output",
        help="Alias for --output live.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_const", const="debug", dest="output",
        help="Alias for --output debug.",
    )
    parser.add_argument(
        "--run-id", type=str, default=None, metavar="RUN_ID",
        help="Explicit run_id for the new run (overrides internally-minted "
             "timestamp). Used by external supervisors (orcho-mcp) so the "
             "run folder name matches the checkpoint key. CLI users normally "
             "omit this and let the orchestrator generate session_ts. Equivalent "
             "to setting $ORCHO_RUN_ID in the environment.",
    )
    parser.add_argument(
        "--resume", type=str, nargs="?", const="latest", default=None,
        metavar="RUN_ID",
        help="Resume from existing run_dir (skip phases that have a checkpoint). "
             "RUN_ID must be the basename of a directory under "
             "<workspace>/runspace/runs/, for example 20260504_154134. "
             "Pass bare --resume or --resume latest to select the newest run "
             "in the active workspace automatically.",
    )
    parser.add_argument(
        "--from-run-plan", type=str, default=None, metavar="RUN_ID_OR_DIR",
        help=(
            "Start a new run that reuses the parsed plan from a parent "
            "run. Accepts either a bare run id (basename of a runs/ "
            "subdir) or an explicit path. The parent must contain "
            "parsed_plan.json — hard-fail with a clear diagnostic "
            "otherwise (no markdown fallback). The selected profile is "
            "projected to skip its leading plan + validate_plan block, "
            "so the child run starts at implement (or the first phase "
            "after planning). Mutually exclusive with --resume; use "
            "--resume to continue the SAME run, --from-run-plan to "
            "spawn a NEW run that inherits the parent's plan. "
            "Task and project are inherited from the parent's "
            "meta.json when omitted; explicit --task / --project "
            "always win. Incompatible with --profile plan / review "
            "(those profiles have nothing to run on top of the "
            "inherited plan)."
        ),
    )
    # Per-phase model overrides. When any of these is set the orchestrator
    # builds a PhaseAgentConfig from the registry; otherwise it falls back to
    # the legacy module-level config defaults.
    parser.add_argument("--model-plan",            type=str, default=None,
                        help="Override model for plan phase")
    parser.add_argument("--model-implement",       type=str, default=None,
                        help="Override model for implement phase")
    parser.add_argument("--model-repair-changes",  type=str, default=None,
                        help="Override model for repair_changes (rounds 1+ and escalation)")
    parser.add_argument("--model-review-changes",  type=str, default=None,
                        help="Override model for validate_plan / review_changes / final_acceptance")
    # Runtime overrides — orthogonal to --model-*. If omitted the per-phase
    # default runtime from AppConfig is used. Use these to swap CLIs per phase.
    parser.add_argument("--runtime-plan",            type=str, default=None,
                        help="Override runtime for plan phase")
    parser.add_argument("--runtime-implement",       type=str, default=None,
                        help="Override runtime for implement phase")
    parser.add_argument("--runtime-repair-changes",  type=str, default=None,
                        help="Override runtime for repair_changes (rounds 1+ and escalation)")
    parser.add_argument("--runtime-review-changes",  type=str, default=None,
                        help="Override runtime for validate_plan / review_changes / final_acceptance")
    # Wave 3 + Phase 6: session selector + profile selector. Defaults
    # preserve the canonical feature pipeline while AUTO picks STATELESS
    # or CHAIN based on the model match.
    parser.add_argument(
        "--session-mode", type=str, default="auto",
        choices=[m.value for m in SessionMode],
        help="How to chain implement → repair_changes (auto/stateless/chain/hybrid)",
    )
    parser.add_argument(
        "--profile", type=str, default=None,
        help=(
            "semantic work-kind profile to dispatch (default: "
            "``feature`` for fresh runs; inherits from ``meta.profile`` "
            "on ``--resume``). Built-ins: ``feature`` / ``small_task`` / "
            "``complex_feature`` (Common) + ``planning`` / "
            "``delivery_audit`` / ``code_review`` / ``research`` / "
            "``refactor`` / ``migration`` (Focused). Pass "
            "``--profile auto-detect`` to have Orcho recommend a work kind + "
            "mode (accept/override on a confirm-policy TTY; trusted threshold "
            "auto-select on a non-interactive run). Custom profiles ship "
            "via ``orcho.profiles`` entry_points (Phase 7). The legacy "
            "``--mode {full,plan}`` flag is gone; pass ``--profile task`` "
            "instead of ``--mode full --skip-plan``. Explicit "
            "``--profile`` on resume = deliberate profile switch."
        ),
    )
    # Verification strictness override (T6). Distinct from the retired
    # cross ``--mode {full,plan}`` slice selector — this mono flag selects
    # the run's verification posture (``work_mode``) and wins over the
    # profile's projected ``default_mode``. Threaded to the run via the
    # ``ORCHO_WORK_MODE`` env (run_pipeline's signature is locked), so it
    # never reaches the cross argv child-dispatch as ``cross_mode``.
    parser.add_argument(
        "--mode", type=str, default=None,
        choices=["fast", "pro", "governed"],
        help=(
            "verification strictness for this run (fast / pro / governed). "
            "Overrides the profile's default mode; when omitted the run uses "
            "the profile's default_mode (e.g. feature → fast, "
            "complex_feature → pro). ``governed`` is opt-in only — never a "
            "built-in default."
        ),
    )
    # Phase 4.5: prompt-context attachments. Multiple flags allowed; each
    # path becomes one Attachment threaded through state.attachments.
    # ``--attach`` auto-detects kind by extension; the typed flags force
    # the kind for paths whose extension would mis-detect.
    parser.add_argument(
        "--attach", action="append", default=None, metavar="PATH",
        help="File to attach as prompt context (kind auto-detected from "
             "extension). May be repeated.",
    )
    parser.add_argument(
        "--attach-text", action="append", default=None, metavar="PATH",
        help="File to attach as TEXT (forces kind regardless of extension).",
    )
    parser.add_argument(
        "--attach-image", action="append", default=None, metavar="PATH",
        help="File to attach as IMAGE (.png/.jpg/etc). Multimodal "
             "translation per runtime ships in Phase 7.",
    )
    parser.add_argument(
        "--attach-binary", action="append", default=None, metavar="PATH",
        help="File to attach as BINARY (passthrough; runtime decides handling).",
    )
    parser.add_argument(
        "--no-worktree-isolation",
        action="store_true",
        default=False,
        help=(
            "Escape valve for ADR 0033 per-run worktree isolation. "
            "When set, the run mutates the user's source checkout "
            "directly (pre-GWT-1 behaviour). Equivalent to setting "
            "``worktree.enabled=false`` in config for this single run."
        ),
    )
    parser.add_argument(
        "--feedback-file", type=str, default=None, metavar="PATH",
        help=(
            "Read operator feedback for an interactive phase-handoff "
            "retry_feedback / continue_with_waiver decision from this "
            "file instead of typing / pasting it at the prompt. The safe "
            "path for long, multi-line verdicts. Contents must be "
            "non-empty."
        ),
    )
    args = parser.parse_args()

    # Register the long-feedback file (if any) so the in-process
    # phase-handoff prompt sources retry_feedback / continue_with_waiver
    # feedback from it instead of the TTY.
    from pipeline.control.handoff_prompt import set_feedback_file_override
    set_feedback_file_override(args.feedback_file)

    try:
        config.apply_session_split_override_env(args.session_split)
    except ValueError as exc:
        print_error(str(exc))
        sys.exit(2)

    # ``--from-run-plan`` is mutually exclusive with ``--resume``.
    # The two flags carry different intents and cannot be combined:
    #   --resume     continues the SAME run from its checkpoint.
    #   --from-run-plan starts a NEW run that inherits the parent's
    #                   parsed plan and skips its planning block.
    # Combining them would be ambiguous (resume which run with whose
    # plan?), so fail fast with a clear message.
    if args.resume is not None and args.from_run_plan is not None:
        print_error(
            "--resume and --from-run-plan are mutually exclusive. "
            "Use --resume to continue the same run from its checkpoint, "
            "or --from-run-plan to spawn a new run that inherits the "
            "parent's parsed plan."
        )
        sys.exit(2)

    # ``--from-run-plan`` only makes sense for profiles that have phases
    # AFTER the planning block — that block is what gets stripped by the
    # projection. Profiles whose entire content IS the planning block
    # (``plan``) or which have no planning + no implementation
    # (``review``) leave nothing meaningful to run, so reject them
    # up front with a clear next-step before workspace resolve / task
    # prompt. Without this guard the contradiction surfaces deep
    # inside ``run_pipeline`` as a ValueError("profile consists
    # entirely of planning phases") after the operator already typed
    # a task; that error is technically correct but UX-hostile.
    # Keyed by the semantic work kinds whose recipes contradict
    # ``--from-run-plan`` (shared with the plan-only follow-up promotion so the
    # two guards cannot drift): the plan-only recipe (planning / research) has
    # no phases after the planning block, and the review-only recipe
    # (delivery_audit / code_review) has no planning or implementation phases.
    from pipeline.control.from_run_plan import (
        CONTRADICTORY_FROM_RUN_PLAN_PROFILES as _CONTRADICTORY_FROM_RUN_PLAN_PROFILES,
    )
    if args.from_run_plan is not None:
        # Check the EFFECTIVE profile, not just ``args.profile``. The
        # ``ORCHO_PIPELINE`` env var overrides ``--profile`` per
        # :func:`_resolve_profile_name` semantics; without this
        # routing through the same resolver, a contradictory env
        # override (``ORCHO_PIPELINE=planning orcho run --from-run-plan
        # ... --profile feature``) would silently bypass the guard
        # and crash deeper in the projection helper.
        _effective_profile = _resolve_profile_name(
            profile_name=args.profile or DEFAULT_PROFILE_NAME,
        )
        if _effective_profile in _CONTRADICTORY_FROM_RUN_PLAN_PROFILES:
            _reason = _CONTRADICTORY_FROM_RUN_PLAN_PROFILES[_effective_profile]
            # Surface where the offending profile came from so the
            # operator can fix the right thing — flag or env.
            _source = (
                f"--profile {_effective_profile}"
                if args.profile == _effective_profile
                else f"ORCHO_PIPELINE={_effective_profile} env override"
            )
            print_error(
                f"--from-run-plan + {_source} is contradictory: "
                f"{_reason}. Pick a profile that has implementation / review "
                "phases downstream of planning (feature, complex_feature, "
                "task)."
            )
            sys.exit(2)

    # Implicit-cwd workspace auto-derive: when no ``--workspace`` and no
    # ``--project`` is supplied, the project defaults to ``Path.cwd()``
    # downstream. Mirror the explicit ``--project`` branch below and walk
    # up from cwd to find a sibling ``workspace-orchestrator/``; if found,
    # override env so runs land next to the project the operator is
    # actually staring at. Catches the classic foot-gun: a stale
    # ``$ORCHO_WORKSPACE`` from a prior disposable-demo session (often
    # pointing at ``/tmp/...``) silently winning over the obvious
    # workspace next to cwd.
    # Cwd walk-up auto-derive (symmetric with ``--project``): if the
    # operator is standing inside a workspace tree, that workspace wins
    # over whatever stale env var their shell carries. Resume /
    # from-run-plan don't get an exemption — a run id is local to the
    # workspace you're standing in; resume context resolution downstream
    # errors cleanly when the id isn't there.
    if not args.workspace and not args.project:
        _autoderive_workspace_from_cwd()

    # Workspace propagation: --workspace wins, then $ORCHO_WORKSPACE.
    # Set the environment variable before the first config.RUNS_DIR access so
    # the shared resolver in platform.py observes the current value instead of
    # a snapshot captured at import time. See _LazyPath in config.py.
    #
    # ``config.get_runs_dir()`` (used below for resume meta + output dir
    # lookup) reads ``ORCHO_RUNSPACE`` before ``ORCHO_WORKSPACE``, so an
    # ambient ``ORCHO_RUNSPACE`` pointing at a different runspace would
    # silently win over explicit ``--workspace``. Override both env vars
    # together so the comment above ("CLI flag wins") actually holds.
    if args.workspace:
        _ws_resolved = Path(args.workspace).resolve()
        os.environ["ORCHO_WORKSPACE"] = str(_ws_resolved)
        os.environ["ORCHO_RUNSPACE"] = str(_ws_resolved / "runspace")
        config._reset_config()
    elif args.project:
        # Auto-derive from --project location: walk-up looking for a
        # ``workspace-orchestrator/`` directory, override the global
        # $ORCHO_WORKSPACE if found. Without this, ``orcho run --project
        # ~/www/atas/bot_1`` writes runs into ``$ORCHO_WORKSPACE`` (often
        # qcg) instead of atas, which surprises everyone with multi-project
        # layouts. The sibling layout (``<root>/<project>/`` next to
        # ``<root>/workspace-orchestrator/``) is the convention these
        # workspaces use, so a single walk-up reliably finds it.
        #
        # ``--resume`` without ``--project`` skips this — the project
        # comes from the persisted meta.json below and the resume
        # run_dir is already on disk under the active workspace.
        inferred = _infer_workspace_from_project(args.project)
        if inferred is not None:
            os.environ["ORCHO_WORKSPACE"] = str(inferred)
            os.environ["ORCHO_RUNSPACE"] = str(inferred / "runspace")
            config._reset_config()
            print(f"  ↳ workspace auto-derived from --project: {inferred}")

    _resume_runs_dir: Path | None = None
    if args.resume == "latest" and not args.project:
        _resume_runs_dir = _apply_resume_runs_context(
            workspace=args.workspace,
        )

    _pipeline_cfg = config.AppConfig.load().pipeline
    if args.model is None:
        args.model = config.AppConfig.load().phase_model_map.get(
            "implement",
            "claude-opus-4-8[1m]",
        )

    # Resume context resolution: when ``--resume RUN_ID`` is supplied,
    # required fresh-run inputs (task, project) may be omitted and
    # resolved from the persisted meta.json. Load it once here so the
    # downstream task/project/output_dir resolution sees the hydrated
    # values without each call site re-reading the file.
    from pipeline.control import (
        ResumeContextError as _ResumeContextError,
        ResumeMode as _ResumeMode,
        build_checkpoint_followup_lineage as _build_checkpoint_followup_lineage,
        build_followup_resume_fields as _build_followup_resume_fields,
        classify_resume_mode as _classify_resume_mode,
        detect_active_followup_child as _detect_active_followup_child,
        get_resume_intent_options as _get_resume_intent_options,
        is_terminal_final_acceptance_rejected as _is_terminal_fa_rejected,
        is_terminal_phase_handoff_halt as _is_terminal_phase_handoff_halt,
        is_terminal_success as _is_terminal_success,
        load_resume_meta as _load_resume_meta,
        prompt_resume_intent as _prompt_resume_intent,
        resolve_project as _resolve_project,
        resolve_resume_profile as _resolve_resume_profile,
        resolve_task as _resolve_task,
        should_prompt_for_resume_intent as _should_prompt_for_resume_intent,
    )

    # ``--from-run-plan`` early resolution: load the parent run dir
    # AND parent meta NOW so (a) task / project can inherit from the
    # parent before ``_resolve_task`` runs, and (b) the followup
    # slots below see ``_from_run_plan_parent_dir`` as already
    # resolved. Inheritance keeps the CLI ergonomic — the over-run
    # follow-up plan's doc example
    # (``orcho run --from-run-plan <id> --profile feature``) works
    # without re-typing the parent's task and project.
    _from_run_plan_parent_dir: Path | None = None
    _from_run_plan_parent_meta = None  # ResumedMeta | None
    if args.from_run_plan is not None:
        from pipeline.plan_artifacts import (
            ParsedPlanArtifactError,
            resolve_parent_run_dir,
        )
        from sdk.runs import NoWorkspace, find_runs_dir
        try:
            _runs_dir_for_resolution = (
                find_runs_dir(workspace=args.workspace)
                if args.workspace else find_runs_dir()
            )
        except NoWorkspace:
            _runs_dir_for_resolution = None
        try:
            _from_run_plan_parent_dir = resolve_parent_run_dir(
                args.from_run_plan,
                runs_dir=_runs_dir_for_resolution,
            )
        except ParsedPlanArtifactError as exc:
            print_error(str(exc))
            sys.exit(2)
        # Parent meta is the source of inheritance. parsed_plan.json
        # presence was already asserted by resolve_parent_run_dir;
        # meta.json may be missing for older runs / partial states —
        # in that case operator must supply --task / --project
        # explicitly (the resolve_task / resolve_project calls below
        # will error with their usual diagnostics).
        try:
            _from_run_plan_parent_meta = _load_resume_meta(
                _from_run_plan_parent_dir,
            )
        except _ResumeContextError as exc:
            print_error(
                f"--from-run-plan: cannot read parent meta.json: {exc}",
            )
            sys.exit(2)
        # Inherit task / project from parent when not explicitly
        # supplied. Explicit args always win — the over-run doc's
        # rule is "child run starts NEW but inherits parent context",
        # not "child run silently overrides operator intent".
        if _from_run_plan_parent_meta is not None:
            if not args.task and not args.task_file:
                _meta_task = _from_run_plan_parent_meta.meta.get("task")
                if isinstance(_meta_task, str) and _meta_task.strip():
                    args.task = _meta_task
                    print(
                        "  ↳ --from-run-plan: task inherited from parent run "
                        f"{_from_run_plan_parent_dir.name!r}",
                    )
            if not args.project:
                _meta_project = _from_run_plan_parent_meta.meta.get("project")
                if isinstance(_meta_project, str) and _meta_project.strip():
                    args.project = _meta_project
                    print(
                        "  ↳ --from-run-plan: project inherited from parent "
                        f"run {_from_run_plan_parent_dir.name!r}: "
                        f"{args.project}",
                    )

    if args.resume == "latest":
        if _resume_runs_dir is None:
            try:
                _resume_runs_dir = config.get_runs_dir()
            except config.WorkspaceNotResolvedError as exc:
                print_error(str(exc))
                sys.exit(2)
        args.resume = _resolve_resume_latest(
            prefer_incomplete=not (args.task or args.task_file),
            workspace=args.workspace,
            runs_dir=_resume_runs_dir,
        )
        print(f"  ↳ --resume auto-resolved to latest run: {args.resume}")

    _resumed = None
    if args.resume:
        try:
            if _resume_runs_dir is None:
                _resume_runs_dir = config.get_runs_dir()
            _resume_dir = _resume_runs_dir / args.resume
            _resumed = _load_resume_meta(_resume_dir)
            if _resumed is None:
                print(
                    "--resume: meta.json not found at "
                    f"{_resume_dir / 'meta.json'}",
                    file=sys.stderr,
                )
        except config.WorkspaceNotResolvedError as exc:
            print_error(str(exc))
            sys.exit(2)
        except _ResumeContextError as exc:
            print_error(str(exc))
            sys.exit(2)

    # Capture the *operator* profile (what the user actually passed on the
    # command line: ``None`` when ``--profile`` was omitted, otherwise the
    # given value including ``AUTO_DETECT_PROFILE_TOKEN``) before the first
    # mutation of ``args.profile`` below. The active-follow-up branch needs
    # the original operator intent — not the already-resolved value — so an
    # inherited parent profile does not masquerade as an explicit override
    # for a newly selected child.
    _operator_profile = args.profile

    # Resolve effective profile: explicit ``--profile`` wins; otherwise
    # inherit from ``meta.profile`` on resume; else fall back to the
    # fresh-run default. Assign back to ``args.profile`` so every
    # downstream consumer (run_pipeline call, --profile echoing,
    # session-key derivation) sees the resolved value.
    args.profile = _resolve_resume_profile(
        explicit_profile=args.profile,
        resumed=_resumed,
        fresh_default=DEFAULT_PROFILE_NAME,
    )

    # Retained-change correction is a distinct control surface, not a generic
    # task-bearing follow-up.  The CLI only renders its two core intents and
    # delegates spawning to the detached client-neutral launch seam.
    if (
        args.resume
        and _resumed is not None
        and not args.task
        and not args.task_file
    ):
        if _stdio_interactive() and not bool(getattr(args, "no_interactive", False)):
            _correction_prompt_outcome = _run_correction_followup_prompt(
                run_id=args.resume,
                meta=_resumed.meta,
                runs_dir=_resume_runs_dir,
            )
            if _correction_prompt_outcome == "error":
                sys.exit(2)
            if _correction_prompt_outcome in {"exit", "started"}:
                sys.exit(0)
        else:
            from pipeline.control.continuation import resolve_continuation_decision

            _continuation = resolve_continuation_decision(
                run_id=args.resume,
                meta=_resumed.meta,
                parent_run_dir=(
                    _resume_runs_dir / args.resume
                    if _resume_runs_dir is not None else None
                ),
            )
            if _continuation.continuation_subject == "retained_change":
                print_error(
                    "Correction follow-up requires operator input: "
                    "followup or exit, plus a non-empty operator comment."
                )
                sys.exit(2)

    # Lineage: a newer, still-unfinished follow-up child of this parent
    # is a likely better resume target. Detected once here; offered (never
    # silently switched to) in the interactive prompt, surfaced as a
    # copy-paste hint on the non-interactive path.
    _active_followup = None
    if (
        args.resume
        and not args.task
        and not args.task_file
    ):
        _active_followup = _detect_active_followup_child(
            parent_run_id=args.resume,
            runs_dir=_resume_runs_dir,
        )

    # Interactive resume-intent chooser: only fires when the user passed
    # ``--resume`` without a task and stdin is a TTY. Mutates args.task
    # locally when the user picks "follow-up". Skips itself on the
    # non-interactive transports (MCP / CI / piped invocations).
    if _should_prompt_for_resume_intent(
        resume=args.resume,
        explicit_task=args.task,
        explicit_task_file=args.task_file,
        no_interactive=bool(getattr(args, "no_interactive", False)),
    ):
        _intent_options = _get_resume_intent_options(
            parent_meta=(_resumed.meta if _resumed is not None else None),
            has_new_task=False,
        )
        if _intent_options.can_checkpoint and _resumed is not None:
            from pipeline.project.loop_resume import inspect_checkpoint_resume

            try:
                resume_profile = _resolve_v2_profile(
                    profile_name=args.profile,
                    allow_env_override=False,
                )
                if resume_profile is None:
                    raise LoopResumeBlockedError(
                        f"Profile {args.profile!r} is not available."
                    )
                inspect_checkpoint_resume(
                    resume_profile,
                    run_dir=_resume_dir,
                    run_id=args.resume,
                )
            except LoopResumeBlockedError as exc:
                _intent_options = dataclasses.replace(
                    _intent_options,
                    can_checkpoint=False,
                    default_mode=_ResumeMode.FOLLOWUP,
                    checkpoint_blocked_reason=str(exc),
                )
        _intent = _prompt_resume_intent(
            run_id=args.resume, options=_intent_options,
            active_followup=_active_followup,
        )
        if _intent.mode is None:
            sys.exit(0)
        if _intent.resume_run_id and _intent.resume_run_id != args.resume:
            # Explicit operator choice to resume the active follow-up
            # child. Switch the resume target and reload its meta — never
            # a silent switch (the operator selected this option).
            args.resume = _intent.resume_run_id
            _resume_dir = _resume_runs_dir / args.resume
            _resumed = _load_resume_meta(_resume_dir)
            # Re-resolve against the *original operator* profile, not the
            # value already resolved for the parent above. When the operator
            # did not pass ``--profile`` (``_operator_profile is None``) the
            # selected child's durable ``meta.profile`` must win; passing the
            # mutated ``args.profile`` here would let the parent's inherited
            # profile hijack the child. A real explicit ``--profile`` stays a
            # non-empty override and still beats the child's meta.
            args.profile = _resolve_resume_profile(
                explicit_profile=_operator_profile,
                resumed=_resumed,
                fresh_default=DEFAULT_PROFILE_NAME,
            )
            print(
                f"Resuming active follow-up {args.resume} (operator choice).",
                file=sys.stderr,
            )
        if _intent.mode == _ResumeMode.FOLLOWUP and _intent.task:
            args.task = _intent.task
    elif _active_followup is not None:
        # Non-interactive (CI / MCP / piped): print the recommended
        # command instead of switching the run id.
        _print_active_followup_recommendation(
            _active_followup, parent_run_id=args.resume,
        )

    _resume_mode = _classify_resume_mode(
        resume=args.resume,
        explicit_task=args.task,
        explicit_task_file=args.task_file,
    )

    # Non-interactive guard: bare ``--resume`` on a terminal parent has no
    # follow-up task and cannot meaningfully checkpoint. Print the hint and
    # exit 0 so CI / MCP see a clean signal instead of a confusing
    # rerun-into-completed-run. The dead-end set mirrors the shared
    # resume_context vocabulary (terminal success, phase-handoff halt, and the
    # rejected final-acceptance halts) rather than a separate CLI classifier;
    # commit/delivery decision gates are intentionally excluded here because
    # their next action is ``decide_delivery``, not a follow-up.
    if (
        _resume_mode == _ResumeMode.CHECKPOINT
        and _resumed is not None
        and (
            _is_terminal_success(_resumed.meta)
            or _is_terminal_phase_handoff_halt(_resumed.meta)
            or _is_terminal_fa_rejected(_resumed.meta)
        )
    ):
        print(
            f"Run {args.resume} cannot be resumed from checkpoint "
            f"(status: {_resumed.meta.get('status')}); pass --task with "
            "--resume to create a follow-up.",
            file=sys.stderr,
        )
        sys.exit(0)

    try:
        task = _resolve_task(
            explicit_task=args.task,
            explicit_task_file=args.task_file,
            explicit_project=args.project,
            resumed=_resumed,
        )
        if args.project is None and _resume_mode == _ResumeMode.FRESH:
            try:
                picked = pick_project_for_fresh_run(
                    cwd=Path.cwd(),
                    workspace=args.workspace,
                    no_interactive=bool(
                        getattr(args, "no_interactive", False),
                    ),
                )
            except WorkspaceProjectPickError as exc:
                print_error(f"{exc.message}\n{exc.hint}")
                sys.exit(2)
            args.project = str(picked)
        args.project = _resolve_project(
            explicit_project=args.project,
            resumed=_resumed,
        )
    except _ResumeContextError as exc:
        print_error(str(exc))
        sys.exit(1)

    # Validate project path before materialising a run dir or any
    # logging — keeps the workspace clean when the user typos the path.
    if args.project:
        resolved_alias = resolve_project_alias(
            args.project,
            workspace=args.workspace,
        )
        if resolved_alias is not None:
            args.project = str(resolved_alias)
    if args.project and not Path(args.project).expanduser().resolve().exists():
        print_error(f"Project not found: {Path(args.project).expanduser().resolve()}")
        sys.exit(2)
    if args.project and _resume_mode != _ResumeMode.CHECKPOINT:
        _reject_project_group_root_if_needed(args.project)

    # Auto-detect dispatch (Stage C / T3): the single point that resolves the
    # ``auto-detect`` selector into a concrete profile + mode. Runs only when
    # the effective profile is the selector token — a manual concrete profile
    # never enters here and never invokes the detector. Placed AFTER task /
    # project resolution and BEFORE ORCHO_WORK_MODE / run_pipeline so the run
    # starts with the resolved profile and the resolution is pinned to what
    # actually runs. The typed AutoDetectResolution is carried into the run
    # via a scoped env channel around the run_pipeline call below.
    _autodetect_resolution = None
    if args.profile == AUTO_DETECT_PROFILE_TOKEN:
        _interactive = (
            sys.stdin.isatty()
            and not bool(getattr(args, "no_interactive", False))
        )
        _detector = None
        if getattr(args, "mock", False):
            # Hermetic mock detector: no real provider, but the deterministic
            # topology recommendation is merged in just like the provider path
            # (F3). See ``_build_mock_work_kind_detector``.
            _detector = _build_mock_work_kind_detector(task)
        else:
            _detector = ProviderWorkKindDetector(model=args.model)
        _autodetect_resolution = resolve_auto_detect(
            task=task,
            project=str(args.project or Path.cwd()),
            interactive=_interactive,
            explicit_mode=getattr(args, "mode", None),
            detector=_detector,
        )
        if _autodetect_resolution.detection_state.value == "failed":
            print_error(
                "auto-detect could not determine a work kind "
                f"({_autodetect_resolution.fallback_reason or _autodetect_resolution.error_reason}). "
                "Re-run with an explicit --profile."
            )
            sys.exit(2)
        # Topology axis (T3): for a high-confidence cross recommendation, show
        # the 'Auto-detect result' block + three explicit choices and map the
        # operator's pick into delivery_scope. A non-interactive run only
        # records the recommendation (strict mono, no cross start, no delivery
        # widening). Choice 1 never converts this mono process into a cross run
        # — it surfaces the explicit ``orcho cross`` directive. The semantic
        # profile is already resolved, so confirm semantics are untouched.
        from cli._profile_prompt import (
            CrossRunRequested,
            resolve_topology_choice,
        )
        try:
            _autodetect_resolution = resolve_topology_choice(
                _autodetect_resolution, interactive=_interactive,
            )
        except CrossRunRequested as _cross_directive:
            # Operator chose 'Start cross run' in the auto-detect block. F2 holds:
            # this mono process never becomes a cross run in place and never
            # persists a cross delivery_scope on a mono run that never starts.
            # Resolve the projected aliases to repo paths and launch a *fresh*
            # cross process, carrying the task through — replacing this process
            # rather than mutating it is what keeps the mono run from starting.
            from cli._cross_launch import launch_cross_from_directive
            sys.exit(
                launch_cross_from_directive(
                    projects=_cross_directive.projects,
                    task=task,
                    current_project=str(args.project or Path.cwd()),
                    profile=_autodetect_resolution.actual_profile.value,
                    work_mode=_autodetect_resolution.actual_mode.value,
                    model=getattr(args, "model", None),
                    mock=getattr(args, "mock", False),
                    interactive=_interactive,
                )
            )
        # Pin the resolved profile to what the run actually starts with. The
        # resolved ``actual_mode`` is NOT written to ORCHO_WORK_MODE here: it is
        # scoped around ``run_pipeline`` by ``scoped_autodetect_decision_env``
        # below (F1), so a recommended mode cannot leak into a later manual run
        # in the same process.
        args.profile = _autodetect_resolution.actual_profile.value

    # Output dir rules:
    #   FRESH      → new run dir (timestamp or --run-id)
    #   CHECKPOINT → parent run dir (existing)
    #   FOLLOWUP   → new run dir; parent stays untouched as context
    if args.output_dir:
        output_dir = Path(args.output_dir)
    elif _resume_mode == _ResumeMode.CHECKPOINT:
        try:
            runs_dir = _resume_runs_dir or config.get_runs_dir()
            output_dir = runs_dir / args.resume
        except config.WorkspaceNotResolvedError as exc:
            print_error(str(exc))
            sys.exit(2)
        if not output_dir.is_dir():
            print_error(
                f"--resume {args.resume!r}: run_dir does not exist: {output_dir}.\n"
                f"Available runs: ls {output_dir.parent}"
            )
            sys.exit(2)
    elif _resume_mode == _ResumeMode.FOLLOWUP:
        # Follow-up runs are brand-new runs that just remember their
        # parent. Mint a fresh timestamp run_id; the parent run dir
        # stays untouched as historical context.
        session_ts_cli = (
            args.run_id
            or os.environ.get("ORCHO_RUN_ID", "").strip()
            or datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        try:
            runs_dir = _resume_runs_dir or config.get_runs_dir()
            output_dir = runs_dir / session_ts_cli
        except config.WorkspaceNotResolvedError as exc:
            print_error(str(exc))
            sys.exit(2)
    else:
        # Default: <workspace>/runspace/runs/{ts}/, one atomic folder per run.
        # If the workspace cannot be resolved, get_runs_dir() raises
        # WorkspaceNotResolvedError with remediation guidance.
        # ``--run-id`` (or $ORCHO_RUN_ID) overrides the timestamp so external
        # supervisors that pre-create the run folder can ensure folder name
        # equals the run_id used in checkpoint/meta downstream (P2.5 contract).
        session_ts_cli = (
            args.run_id
            or os.environ.get("ORCHO_RUN_ID", "").strip()
            or datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        try:
            output_dir = config.get_runs_dir() / session_ts_cli
        except config.WorkspaceNotResolvedError as exc:
            print_error(str(exc))
            sys.exit(2)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pin the run id chosen by the CLI before entering run_pipeline.
    # Otherwise the CLI can create ``runs/<ts>`` and the lower bootstrap
    # layer can tick into the next second, producing checkpoints/worktrees
    # under a different id. Checkpoint resumes are already authoritative via
    # ``resume_from`` and must not rewrite the ambient env.
    if _resume_mode != _ResumeMode.CHECKPOINT:
        os.environ["ORCHO_RUN_ID"] = output_dir.name

    # ADR 0131: stable per-worktree isolation namespace. v1 isolation is
    # per-run, so this is the run/worktree identity — the documented contract a
    # project keys external-resource isolation on (e.g.
    # ``COMPOSE_PROJECT_NAME=orcho_$ORCHO_ISOLATION_ID`` + ephemeral ports).
    # Set in ALL modes, including checkpoint resume (which reuses the same
    # worktree via ``output_dir.name``), so worktree_bootstrap and gate commands
    # target the same stack across a resume. Not stripped from gate env
    # (RUN_SCOPED_ENV_CHANNELS), so both bootstrap and gates inherit it.
    os.environ["ORCHO_ISOLATION_ID"] = output_dir.name

    # Propagate --mode (verification strictness) into ORCHO_WORK_MODE so the
    # default-mode projection at contract assembly picks it up as the explicit
    # per-run override. run_pipeline's signature is locked, so env is the
    # threading channel (mirrors ORCHO_RUN_ID). Distinct from cross
    # ``--mode {full,plan}`` — that lives on the cross parser and is forwarded
    # as ``cross_mode`` in argv; this never enters argv child-dispatch.
    #
    # Skipped for an auto-detect run: there ``actual_mode`` already folds in any
    # explicit ``--mode`` and ORCHO_WORK_MODE is owned (and scoped/restored) by
    # ``scoped_autodetect_decision_env`` below, so writing it here unscoped would
    # re-introduce the F1 leak.
    if getattr(args, "mode", None) and _autodetect_resolution is None:
        os.environ["ORCHO_WORK_MODE"] = args.mode

    # Always go through build_phase_config_from_overrides — even when no CLI
    # overrides are present — so the resulting PhaseAgentConfig carries the
    # per-phase models AND per-phase efforts from AppConfig. The previous
    # ``phase_config=None`` short-circuit fell back to a synthetic config
    # built from a single review_model for all reviewer slots, which silently
    # ignored per-phase model overrides (gpt-5.4 vs gpt-5.5) AND lost effort
    # entirely (codex inheriting xhigh from ~/.codex/config.toml).
    phase_config = build_phase_config_from_overrides(
        plan=args.model_plan,
        implement=args.model_implement,
        repair_changes=args.model_repair_changes,
        review_changes=args.model_review_changes,
        runtime_plan=args.runtime_plan,
        runtime_implement=args.runtime_implement,
        runtime_repair_changes=args.runtime_repair_changes,
        runtime_review_changes=args.runtime_review_changes,
        plugin=load_plugin(args.project),
    )

    from agents.runtimes import make_mock_phase_config, make_provider
    apply_output_mode(args.output)
    _provider = make_provider(
        args.mock,
        mock_validate_plan_reject_rounds=int(getattr(args, "mock_validate_plan_reject", 0) or 0),
    )
    _session_mode = SessionMode.STATELESS if args.mock else SessionMode(args.session_mode)

    # Hermetic mock mode: replace every PhaseAgentConfig slot with inline
    # stubs. Without this override, validate_plan / review / final_acceptance would
    # continue to invoke real ``codex`` CLI even with ``--mock`` because
    # those phases read off ``phase_config`` directly, not the
    # ``provider`` arg. ``--mock`` MUST guarantee zero real provider CLI
    # calls — otherwise CI / integration tests / first-run smoke breaks
    # in any environment without a configured codex/claude binary.
    if args.mock:
        phase_config = make_mock_phase_config(
            validate_plan_reject_rounds=int(getattr(args, "mock_validate_plan_reject", 0) or 0),
        )

    # Phase 4.5: load CLI --attach* paths into Attachment objects.
    attachments: tuple = ()
    if any((args.attach, args.attach_text, args.attach_image, args.attach_binary)):
        from pipeline.attachment_loader import load_attachment
        from pipeline.runtime import AttachmentKind
        loaded: list = []
        try:
            for p in args.attach or ():
                loaded.append(load_attachment(p))
            for p in args.attach_text or ():
                loaded.append(load_attachment(p, kind=AttachmentKind.TEXT))
            for p in args.attach_image or ():
                loaded.append(load_attachment(p, kind=AttachmentKind.IMAGE))
            for p in args.attach_binary or ():
                loaded.append(load_attachment(p, kind=AttachmentKind.BINARY))
        except (FileNotFoundError, ValueError) as e:
            warn(f"attachment error: {e}")
            sys.exit(2)
        attachments = tuple(loaded)

    # Resume vs follow-up split: CHECKPOINT continues into the parent
    # run dir and re-uses the checkpoint store; FOLLOWUP mints a new
    # run and only carries the parent's id/dir/status as historical
    # context — no checkpoint hydration.
    _checkpoint_resume_from: str | None = (
        args.resume if _resume_mode == _ResumeMode.CHECKPOINT else None
    )
    _followup_fields = _build_followup_resume_fields(
        resume_mode=_resume_mode,
        resume_run_id=args.resume,
        resumed=_resumed,
    )
    _followup_parent_run_id: str | None = _followup_fields.parent_run_id
    _followup_parent_run_dir: str | None = _followup_fields.parent_run_dir
    _followup_parent_status: str | None = _followup_fields.parent_status
    _followup_base_task: str | None = _followup_fields.base_task
    _followup_session_seeds: dict[str, str] | None = _followup_fields.session_seeds
    # Display-only lineage for a CHECKPOINT resume of an existing follow-up
    # child: surface parent ↔ child in the run header on a plain
    # ``--resume <child>``. Never seeds provider sessions (the child resumes
    # its own checkpoint) and re-stamps the child's own parent linkage
    # idempotently.
    _followup_child_status: str | None = None
    _followup_active_handoff_id: str | None = None
    if _resume_mode == _ResumeMode.CHECKPOINT:
        _ckpt_lineage = _build_checkpoint_followup_lineage(_resumed)
        if _ckpt_lineage is not None:
            _followup_parent_run_id = _ckpt_lineage.parent_run_id
            _followup_parent_run_dir = _ckpt_lineage.parent_run_dir
            _followup_parent_status = _ckpt_lineage.parent_status
            _followup_base_task = _ckpt_lineage.base_task
            _followup_child_status = _ckpt_lineage.child_status
            _followup_active_handoff_id = _ckpt_lineage.active_handoff_id

    # ``--from-run-plan`` followup slot stamping: parent_dir was
    # resolved early (see the --from-run-plan block above, before the
    # --resume processing), where it was needed for task / project
    # inheritance. Here we only stamp the ``_followup_parent_*`` slots
    # so the meta-recording machinery downstream sees parent_run_id /
    # parent_run_dir uniformly with the --resume FOLLOWUP branch.
    # The actual plan hydration happens inside ``run_pipeline`` once
    # the parent dir is threaded through as ``from_run_plan_parent_dir``.
    if _from_run_plan_parent_dir is not None:
        _followup_parent_run_id = _from_run_plan_parent_dir.name
        _followup_parent_run_dir = str(_from_run_plan_parent_dir)
        # ``--from-run-plan`` does not seed provider sessions from the
        # parent (Phase 1 MVP scope: "do not depend on provider session
        # continuity"). Phase 2 of the follow-up plan introduces an
        # opt-in ``--from-run-plan-session`` flag.
        _followup_session_seeds = None

    # Skip the pre-PLAN hypothesis gut-check on follow-up runs. A follow-up
    # is already grounded by parent run context and, when available, parent
    # provider sessions; hypothesis is only the first turn of a fresh run.
    if _resume_mode == _ResumeMode.FOLLOWUP:
        _hypothesis_was_default = args.hypothesis is None
        if args.hypothesis is True:
            print(
                "  ↳ ignoring --hypothesis on follow-up "
                "(hypothesis only runs at the start of a fresh run)."
            )
        args.hypothesis = False
        if _hypothesis_was_default:
            print(
                "  ↳ hypothesis skipped on follow-up "
                "(parent run context is already attached)."
            )
    elif _resume_mode == _ResumeMode.CHECKPOINT and args.hypothesis is True:
        args.hypothesis = False
        print(
            "  ↳ ignoring --hypothesis on checkpoint resume "
            "(hypothesis only runs at the start of a fresh run)."
        )

    _no_interactive = bool(getattr(args, "no_interactive", False))

    # Checkpoint resume into an undecided active handoff: prompt + decide
    # (interactive) or print a hint and exit (non-interactive) BEFORE
    # run_pipeline re-enters and trips load_handoff_decision_validated.
    if _resume_mode == _ResumeMode.CHECKPOINT and _resumed is not None:
        _handle_checkpoint_resume_preflight(
            run_id=args.resume,
            run_dir=output_dir,
            meta=_resumed.meta,
            no_interactive=_no_interactive,
        )

    # Kwargs that stay constant across an auto-correction follow-up loop
    # (everything except the per-round task / output dir / followup-parent
    # slots). Shared by the first invocation and every correction round so
    # the two paths cannot drift on provider / profile / phase config.
    _stable_followup_kwargs = {
        "project_dir": args.project,
        "max_rounds": args.max_rounds,
        "model": args.model,
        "dry_run": args.dry_run,
        "provider": _provider,
        "phase_config": phase_config,
        "session_mode": _session_mode,
        "profile_name": args.profile,
        "attachments": attachments,
        "no_interactive": _no_interactive,
        "worktree_config_override": (
            {"enabled": False}
            if getattr(args, "no_worktree_isolation", False)
            else None
        ),
    }
    if _no_interactive:
        _stable_followup_kwargs["unattended"] = True

    # Scoped auto-detect env channels (T3 fixes F1+F2): for an auto-detect run
    # serialize the typed AutoDetectResolution into ORCHO_AUTODETECT_DECISION and
    # set ORCHO_WORK_MODE to the resolved actual_mode strictly around the run,
    # restoring / removing both on exit so neither the decision evidence nor the
    # recommended mode leaks into a later manual run in the same process. A
    # manual concrete profile leaves ``_autodetect_resolution`` None → the
    # decision channel is cleared for this run (so a stale value left in the
    # environment cannot make run_setup persist meta.auto_detect) and restored
    # afterwards.
    with scoped_autodetect_decision_env(_autodetect_resolution):
        try:
            _result_session = run_pipeline(
                task=task,
                output_dir=output_dir,
                resume_from=_checkpoint_resume_from,
                hypothesis_enabled=args.hypothesis,
                resume_mode=(
                    _resume_mode.value
                    if _resume_mode == _ResumeMode.FOLLOWUP else None
                ),
                followup_parent_run_id=_followup_parent_run_id,
                followup_parent_run_dir=_followup_parent_run_dir,
                followup_parent_status=_followup_parent_status,
                followup_base_task=_followup_base_task,
                followup_session_seeds=_followup_session_seeds,
                followup_child_status=_followup_child_status,
                followup_active_handoff_id=_followup_active_handoff_id,
                from_run_plan_parent_dir=_from_run_plan_parent_dir,
                **_stable_followup_kwargs,
            )

            # Auto-correction follow-up (ADR 0070): at a TTY, the operator's
            # ``fix`` choice at the correction gate halts the run with
            # ``commit_decision_fix``. Rather than make them re-run
            # ``--resume`` by hand, re-enter the pipeline as a follow-up that
            # carries the rejection's remediation and reuses the retained
            # worktree. The loop is operator-gated (each round ends back at
            # the gate), so it stops the moment they pick approve / apply /
            # skip / halt or acceptance approves. The interactivity guard is
            # the SAME stdin+stdout-TTY test the commit-delivery gate uses, so
            # the loop fires under exactly the conditions that produced the
            # interactive ``fix`` halt; piped / CI / MCP runs leave the run
            # halted for an external controller to resume.
            if (
                not _no_interactive
                and _stdio_interactive()
                and _is_correction_fix_halt(_result_session)
            ):
                base_task_for_followup = _followup_base_task or task
                _result_session = _drive_correction_followups(
                    prev_session=_result_session,
                    prev_output_dir=output_dir,
                    base_task=base_task_for_followup,
                    stable_kwargs=_stable_followup_kwargs,
                    run_pipeline=run_pipeline,
                    mint_run_id=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"),
                    announce=print,
                )

            # Quality-gate: orchestrator may pause when a phase handoff fires
            # (e.g. ``validate_plan`` rejected on the final round under
            # ``human_feedback_on_reject``). Surface this via a dedicated exit
            # code so the dashboard (and CI) can pivot into manual-review
            # without confusing it with crash/failure. Checked on the FINAL
            # session — AFTER any auto-correction follow-up — so a correction
            # round that itself pauses for a handoff still yields rc=4 and the
            # CLI contract holds no matter how many rounds ran.
            if (
                _result_session
                and _result_session.get("status") == "awaiting_phase_handoff"
            ):
                sys.exit(4)
        except RunIdCollisionError as exc:
            print_error(str(exc))
            sys.exit(2)
        except PhaseHandoffHaltedError as exc:
            # Resume attempt on a halted run — caller error, not a crash.
            # rc=2 mirrors RunIdCollisionError ("user must start a new run").
            print_error(str(exc))
            sys.exit(2)
        except FollowupPlanContinuationError as exc:
            # Implicit plan-only follow-up promoted to a from-run-plan
            # continuation, but the selected child profile has no
            # implement/review phases downstream of planning. Operator error,
            # not a crash — mirror the explicit ``--from-run-plan``
            # contradictory-profile guard (rc=2 + clear message) rather than
            # letting the ValueError become a traceback.
            print_error(str(exc))
            sys.exit(2)
        except LoopResumeBlockedError as exc:
            print_error(f"Cannot resume from checkpoint: {exc}")
            sys.exit(2)
        except KeyboardInterrupt:
            print("\nInterrupted")
            sys.exit(130)



if __name__ == "__main__":
    main()
