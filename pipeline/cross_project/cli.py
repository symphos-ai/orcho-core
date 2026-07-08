"""Cross-project CLI leaf (``orcho-cross`` entry point).

ADR 0047 Phase G. Owns the CLI surface that used to live inline at
the bottom of :mod:`pipeline.cross_project.orchestrator`:

  * :func:`main` — the ``orcho-cross`` argparse entry. Reads ``argv``,
    resolves the workspace + resume mode + profile + projects +
    operator decisions, builds an :class:`agents.registry.PhaseAgentConfig`
    from the CLI override flags, calls the legacy
    :func:`pipeline.cross_project.orchestrator.run_cross_pipeline`
    wrapper (which routes through
    :func:`pipeline.cross_project.app.run_cross_project_pipeline`),
    and maps the returned ``session["status"]`` to a process exit
    code.
  * :func:`print_error` — CLI-only red-on-stderr error printer. The
    single-project CLI's ``print_error`` lives in
    :mod:`pipeline.project.cli`; cross keeps its own three-liner so
    it doesn't reach across to a peer CLI module (ADR 0042 stop #9
    inherited).
  * :func:`_resolve_cross_resume_latest` — ``--resume latest``
    chooser. Process-exiting (``sys.exit(2)`` on workspace /
    discovery failure), so structurally CLI-only.

**Import discipline (ADR 0047 D2 inherited).** This module sits at
the leaf — non-CLI cross peers MUST NOT import from
:mod:`pipeline.cross_project.cli`. The AST guard in
:mod:`tests.unit.pipeline.cross_project.test_cross_cli_isolation`
pins that direction. The reverse — ``cli`` importing from
``orchestrator`` for the back-compat ``run_cross_pipeline`` wrapper
and ``parse_projects`` — is the intended direction during the
Phase G → Phase I transition.

**Status → exit code mapping.** Mirrors the single-project CLI:

  * ``done`` → exit 0;
  * ``awaiting_gate_decision`` / ``awaiting_phase_handoff`` →
    exit 4 (resumable pause — CI / dashboards pivot into manual
    review without treating it as a failed run);
  * ``failed`` → exit 1, with ``failure_reason`` echoed to stderr so
    the cause is grep-able in CI logs;
  * ``KeyboardInterrupt`` → exit 130 (POSIX convention).
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# The cross orchestrator module is imported under a stable alias and
# every cross-namespace symbol is accessed via attribute lookup
# (``_xo.run_cross_pipeline``, ``_xo.parse_projects``,
# ``_xo._assert_fresh_run_dir_available``). Late binding through the
# module attribute keeps the ~30 legacy tests under
# ``tests/unit/cli/test_cross_orchestrator_main.py`` working: they
# patch via ``monkeypatch.setattr(cross, "run_cross_pipeline", ...)``
# expecting the patched value to land in the CLI's resolution path.
# Using ``from … import name`` would copy the binding into this
# module's namespace at import time and silently bypass those patches.
import pipeline.cross_project.orchestrator as _xo
from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from agents.runtimes import (
    make_mock_phase_config,
    make_provider,
)
from core.infra import config
from core.io.ansi import C, paint
from pipeline.cross_project.constants import CROSS_DEFAULT_PROFILE
from pipeline.cross_project.rendering import success, warn
from pipeline.project.bootstrap import RunIdCollisionError
from pipeline.project.phase_config import build_phase_config_from_overrides


def print_error(message: str) -> None:
    """Cross-project local copy of the CLI-shaped error printer.

    The single-project CLI's ``print_error`` lives in
    :mod:`pipeline.project.cli`; cross-project must not depend on
    that surface (it's a peer CLI module, not a parent). ADR 0042
    stop #9 inherited — cross keeps its own three-liner.
    """
    # Stderr-bound output passes stream=sys.stderr so auto-detect
    # consults stderr's TTY status, not sys.stdout's — see the
    # Terminal color discipline rule in orcho-core/CLAUDE.md.
    print(
        f"{paint('Error:', C.RED, C.BOLD, stream=sys.stderr)} "
        f"{paint(message, C.RED, stream=sys.stderr)}",
        file=sys.stderr,
    )


#: Handoff id prefixes the cross CLI prompt loop resolves in-process.
#:
#: * ``cross_plan:`` / ``cfa:`` — cross-owned: the decision applies to
#:   the cross run itself (planning-loop / CFA resume branches).
#: * ``project:<alias>:<child_id>`` — project-proxy: a child sub-pipeline
#:   handoff bubbled up to the cross parent. The cross CLI now prompts
#:   for these too. The decision is recorded against the *parent* id on
#:   the cross run (identical record + re-enter-resume path as the
#:   cross-owned shapes); the ``phase_handoff_kind == "project"`` resume
#:   router in ``pipeline.cross_project.app`` then routes
#:   ``phase_handoff_decide`` to the child run with the correct
#:   ``run_id=<alias>`` / ``runs_dir=<cross run dir>``. This automates
#:   the off-band ``decide(parent) + resume`` sequence regression-pinned
#:   by ``test_cross_project_handoff_resume_writes_child_decision``.
#:
#: Anything else is unknown and forces the loop to break out to exit 4
#: so off-band tooling resolves the pause.
_CROSS_OWNED_HANDOFF_PREFIXES: tuple[str, ...] = ("cross_plan:", "cfa:")
_PROJECT_PROXY_HANDOFF_PREFIX: str = "project:"


def _is_cross_owned_handoff_id(handoff_id: str) -> bool:
    """Return True when the handoff id prefix matches a cross-owned
    pause (``cross_plan:`` / ``cfa:``) whose decision applies to the
    cross run itself."""
    return any(
        handoff_id.startswith(prefix)
        for prefix in _CROSS_OWNED_HANDOFF_PREFIXES
    )


def _is_project_proxy_handoff_id(handoff_id: str) -> bool:
    """Return True for a project-proxy pause (``project:<alias>:...``)
    bubbled up from a child sub-pipeline. The cross CLI prompts for it
    and records the decision against the parent id; the resume router
    routes it to the child run."""
    return handoff_id.startswith(_PROJECT_PROXY_HANDOFF_PREFIX)


def _is_promptable_handoff_id(handoff_id: str) -> bool:
    """Return True when the cross CLI prompt loop may resolve this
    handoff in-process (cross-owned or project-proxy). False forces the
    loop to break out and exit 4 so off-band tooling resolves the
    pause."""
    return (
        _is_cross_owned_handoff_id(handoff_id)
        or _is_project_proxy_handoff_id(handoff_id)
    )


def _build_handoff_signal_from_payload(payload: dict) -> object | None:
    """Hydrate a :class:`PhaseHandoffRequested` from the persisted payload.

    The cross-level pause stores the same byte-shape as a single-run
    handoff (ADR 0038), but the cross planning loop never built the
    runtime dataclass — it persisted the dict and exited. To drive the
    same TTY prompt the mono CLI uses, the cross CLI rehydrates the
    dataclass from the dict before handing it to
    :func:`prompt_phase_handoff_action`.

    Returns ``None`` if the payload is missing a required field; the
    caller treats that as "fall back to non-interactive pause" rather
    than guessing defaults — a malformed payload is an integrity issue
    that needs operator attention, not a silent retry.
    """
    from pipeline.runtime.handoff import PhaseHandoffRequested
    from pipeline.runtime.roles import PhaseHandoffType

    try:
        h_type = PhaseHandoffType(payload["type"])
        return PhaseHandoffRequested(
            handoff_id=payload["id"],
            phase=payload["phase"],
            type=h_type,
            trigger=payload.get("trigger", ""),
            verdict=payload.get("verdict", ""),
            approved=bool(payload.get("approved", False)),
            round_extras_key=payload.get("round_extras_key", ""),
            round=int(payload["round"]),
            loop_max_rounds=int(payload["loop_max_rounds"]),
            available_actions=tuple(payload.get("available_actions") or ()),
            artifacts=dict(payload.get("artifacts") or {}),
            last_output=str(payload.get("last_output") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _resolve_cross_resume_latest(
    *,
    prefer_incomplete: bool = False,
    workspace: str | None = None,
) -> str:
    """Resolve the cross ``--resume latest`` sentinel to the newest
    cross run id on disk.

    Wraps :func:`pipeline.control.resolve_latest_run` with
    ``kind="cross"`` so single-project runs do not get picked up by
    ``orcho cross --resume``. Exits the process with rc=2 on
    workspace / discovery failure.

    When ``workspace`` is supplied it is forwarded to
    ``find_runs_dir`` so the explicit ``--workspace`` flag bypasses
    cwd walk-up (which otherwise beats ``$ORCHO_WORKSPACE`` and finds
    the wrong workspace when the CLI runs from inside another
    workspace tree).
    """
    from pipeline.control import LatestRunNotFound, resolve_latest_run
    from sdk.runs import NoWorkspace, find_runs_dir
    try:
        runs_dir = (
            find_runs_dir(workspace=workspace) if workspace
            else find_runs_dir()
        )
    except NoWorkspace as exc:
        print_error(str(exc))
        sys.exit(2)
    try:
        return resolve_latest_run(
            runs_dir=runs_dir,
            kind="cross",
            prefer_incomplete=prefer_incomplete,
            include_terminal_success=True,
            require_existing_project=True,
        )
    except LatestRunNotFound as exc:
        print_error(str(exc))
        sys.exit(2)


# ── CLI ──────────────────────────────────────────────────────────────


def main():
    from core.io.encoding import ensure_utf8_stdio

    # Force UTF-8 stdio before any rendering so non-ASCII output (emoji / box
    # drawing) does not crash on a legacy Windows console code page.
    ensure_utf8_stdio()

    parser = argparse.ArgumentParser(
        description="Cross-Project Multi-Agent Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Unity + API (two projects)
  orcho-cross \\
    --task "Add AdaptiveEvent: Unity sends → API stores → Stats shows" \\
    --projects unity:/path/to/mag_unity_new-copy api:/path/to/magica_api_new

  # All three projects
  orcho-cross \\
    --task "Add new player metric end-to-end" \\
    --projects unity:mag_unity_new-copy api:magica_api_new stats:magica_stats \\
    --model opus --max-rounds 1
        """,
    )
    parser.add_argument("--task", "-t",    type=str, help="Task description")
    parser.add_argument(
        "--task-file",
        type=str,
        help="Read task from .md file; bare NAME.md resolves from .orcho/.task-files",
    )
    parser.add_argument(
        "--projects", "-p", nargs="+", default=None,
        help=(
            "alias:/path pairs, e.g. unity:/path/to/unity api:/path/to/api. "
            "Required for fresh runs; optional on --resume (resolves from "
            "persisted meta.json)."
        ),
    )
    parser.add_argument(
        "--decision", action="append", default=None,
        metavar="TARGET=DECISION",
        help=(
            "Override an operator-decision target (e.g. "
            "contract_check=run). May repeat."
        ),
    )
    parser.add_argument(
        "--decision-feedback", type=str, default=None, metavar="TEXT",
        help=(
            "Free-form feedback attached to a single --decision. "
            "Supplying with more than one --decision is an error."
        ),
    )
    parser.add_argument(
        "--no-interactive", action="store_true",
        help=(
            "Never prompt on stdin; fall through to a resumable "
            "pending-decision state for non-interactive transports."
        ),
    )
    parser.add_argument("--max-rounds",    type=int, default=1)
    parser.add_argument(
        "--session-split",
        action="append",
        default=None,
        metavar="PHASE=SPLIT",
        help=(
            "Override a child profile phase's prompt-session split for this "
            "run (split: stateless, per_phase, per_role, common). May repeat."
        ),
    )
    parser.add_argument(
        "--mock-validate-plan-reject", type=int, default=0, metavar="N",
        help="Mock-only: how many initial validate_plan reviews per sub-pipeline "
             "return REJECTED before flipping to APPROVED. Default 0.",
    )
    parser.add_argument(
        "--hypothesis", action=argparse.BooleanOptionalAction, default=None,
        help="Override CROSS_HYPOTHESIS on/off for this run. "
             "``--hypothesis`` forces it on, ``--no-hypothesis`` skips the "
             "pre-cross-plan gut-check entirely (useful for feature work "
             "where the diagnostic step adds no value). Omit to use the "
             "requested profile's plan-step ``hypothesis`` value.",
    )
    parser.add_argument("--model",         type=str, default=None)
    parser.add_argument("--output-dir",    type=str)
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--mock",          action="store_true",
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
    parser.add_argument("--model-plan",    type=str, default=None,
                        help="Override model for PLAN phase (cross + per-project)")
    parser.add_argument("--model-build",   type=str, default=None,
                        help="Override model for implement phase")
    parser.add_argument("--model-fix",     type=str, default=None,
                        help="Override model for repair_changes phase")
    parser.add_argument("--model-review",  type=str, default=None,
                        help="Override model for validate_plan / review_changes / final_acceptance / contract check")
    # Runtime overrides — orthogonal to --model-*.
    parser.add_argument("--runtime-plan",   type=str, default=None,
                        help="Override runtime for PLAN phase")
    parser.add_argument("--runtime-build",  type=str, default=None,
                        help="Override runtime for implement phase")
    parser.add_argument("--runtime-fix",    type=str, default=None,
                        help="Override runtime for repair_changes phase")
    parser.add_argument("--runtime-review", type=str, default=None,
                        help="Override runtime for validate_plan / review_changes / final_acceptance / contract check")
    # Cross-mode: "full" runs plan → implement → review_changes for all
    # projects; "plan" generates cross_plan.json (canonical) + cross_plan.md
    # (audit render) and stops for human review. Per-project ``--profile``
    # is independent — each sub-pipeline picks its own profile via env /
    # project plugin.
    parser.add_argument(
        "--mode", type=str, default="full",
        choices=["full", "plan"],
        help="Slice of the cross pipeline to run (full / plan). "
             "``plan`` emits cross_plan.json (editable) + cross_plan.md "
             "(audit) and stops for human review.",
    )
    parser.add_argument(
        "--plan-file", type=str, default=None,
        help="Pre-existing cross_plan.json (human-edited after --mode plan). "
             "JSON only — a markdown plan is rejected. If set, Phase 0 is "
             "skipped and the plan is read + validated from disk.",
    )
    parser.add_argument(
        "--workspace", "-w", type=str, default=None,
        help="Path to workspace-orchestrator dir. Required (or set $ORCHO_WORKSPACE) "
             "so run artifacts are written to <workspace>/runspace/runs/<ts>/.",
    )
    parser.add_argument(
        "--resume", type=str, nargs="?", const="latest", default=None,
        metavar="RUN_ID",
        help="Resume cross-run by RUN_ID (basename in runspace/runs/). "
             "cross-checkpoint.json and per-alias checkpoints.db decide what to skip. "
             "Pass bare --resume or --resume latest to auto-select the newest "
             "cross run in the active workspace.",
    )
    parser.add_argument(
        "--profile", type=str, default=None,
        help=(
            "Work kind (default: feature for fresh runs; inherits "
            "from meta.profile on --resume). The profile's per-step cross "
            "policy splits work into a global cross level and per-project "
            "sub-pipelines. Profiles without cross policy are rejected "
            "for cross runs. Explicit --profile on resume = deliberate "
            "profile switch."
        ),
    )
    args = parser.parse_args()

    try:
        config.apply_session_split_override_env(args.session_split)
    except ValueError as exc:
        print_error(str(exc))
        sys.exit(2)

    # Validate operator decisions early so a bogus target fails before
    # workspace inference or filesystem work.
    from pipeline.control import (
        OperatorDecisionError as _OperatorDecisionError,
        ResumeContextError as _ResumeContextError,
        ResumeMode as _ResumeMode,
        classify_resume_mode as _classify_resume_mode,
        get_resume_intent_options as _get_resume_intent_options,
        is_terminal_phase_handoff_halt as _is_terminal_phase_handoff_halt,
        is_terminal_success as _is_terminal_success,
        load_resume_meta as _load_resume_meta,
        parse_operator_decisions as _parse_operator_decisions,
        prompt_resume_intent as _prompt_resume_intent,
        resolve_projects_argv as _resolve_projects_argv,
        resolve_resume_profile as _resolve_resume_profile,
        resolve_task as _resolve_task,
        should_prompt_for_resume_intent as _should_prompt_for_resume_intent,
    )

    try:
        _decisions = _parse_operator_decisions(
            args.decision, args.decision_feedback, subcommand="cross",
        )
    except _OperatorDecisionError as exc:
        print_error(str(exc))
        sys.exit(2)
    # Stash on args so the runner reads from one place.
    args.parsed_decisions = _decisions

    # Apply explicit ``--workspace`` to the environment BEFORE any
    # resume-time filesystem lookup. ``_resolve_cross_resume_latest``
    # and ``_load_resume_meta`` both reach ``config.get_runs_dir()``
    # which reads ``ORCHO_WORKSPACE``; without this early env write
    # ``orcho cross --resume … --workspace …`` fails with "no workspace"
    # even when the workspace was supplied explicitly. The walk-up
    # branch (needs ``projects`` to be resolved) still runs later.
    #
    # ``config.get_runs_dir()`` reads ``ORCHO_RUNSPACE`` before
    # ``ORCHO_WORKSPACE`` — an ambient ``ORCHO_RUNSPACE`` pointing at
    # a different runspace would silently win over explicit
    # ``--workspace``. Override both so the "CLI flag wins" contract
    # actually holds for resume lookup.
    if args.workspace:
        _ws_resolved = Path(args.workspace).resolve()
        os.environ["ORCHO_WORKSPACE"] = str(_ws_resolved)
        os.environ["ORCHO_RUNSPACE"] = str(_ws_resolved / "runspace")
        config._reset_config()
    else:
        # Cwd walk-up: symmetric with ``orcho run``. Must fire BEFORE
        # the resume meta lookup below (which reaches
        # ``config.get_runs_dir()``), otherwise a stale env makes the
        # ``--resume <id>`` lookup chase the wrong workspace. The
        # ``--projects``-based first-project walk-up later still runs
        # as a defensive fallback for the case where cwd walk-up finds
        # nothing but project paths do (operator running ``cross`` from
        # somewhere unrelated with explicit absolute project paths).
        from pipeline.project.bootstrap import autoderive_workspace_from_cwd
        autoderive_workspace_from_cwd()

    if args.resume == "latest":
        args.resume = _resolve_cross_resume_latest(
            prefer_incomplete=not (args.task or args.task_file),
            workspace=args.workspace,
        )
        print(f"  ↳ --resume auto-resolved to latest cross run: {args.resume}")

    # Hydrate persisted meta.json early so task / projects resolution
    # below can fall back to the resumed run's values.
    _resumed = None
    if args.resume:
        try:
            _resume_dir = config.get_runs_dir() / args.resume
            _resumed = _load_resume_meta(_resume_dir)
        except config.WorkspaceNotResolvedError as exc:
            print_error(str(exc))
            sys.exit(2)
        except _ResumeContextError as exc:
            print_error(str(exc))
            sys.exit(2)
    args.resumed_meta = _resumed

    # Resolve effective profile: explicit ``--profile`` wins; otherwise
    # inherit from ``meta.profile`` on resume; else fall back to the
    # cross fresh-run default. Mirrors the same logic on the single-
    # project side (``pipeline.project_orchestrator.main``) — the
    # helper is shared so CLI and MCP cross resume paths agree.
    args.profile = _resolve_resume_profile(
        explicit_profile=args.profile,
        resumed=_resumed,
        fresh_default=CROSS_DEFAULT_PROFILE,
    )

    # Interactive resume-intent chooser: same UX as ``orcho run``. Only
    # fires when --resume is set, no task is supplied, and stdin is a
    # TTY. MCP / CI transports pass --no-interactive and skip it.
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
        _intent = _prompt_resume_intent(
            run_id=args.resume, options=_intent_options,
        )
        if _intent.mode is None:
            sys.exit(0)
        if _intent.mode == _ResumeMode.FOLLOWUP and _intent.task:
            args.task = _intent.task

    _resume_mode = _classify_resume_mode(
        resume=args.resume,
        explicit_task=args.task,
        explicit_task_file=args.task_file,
    )

    # Non-interactive guard for terminal parents (mirror single-run
    # main(): see project_orchestrator for rationale). ``done`` runs
    # can't be checkpoint-resumed, and ``halted`` runs that reached
    # their terminal state via ADR 0038's ``phase_handoff_decide(halt)``
    # must not be re-entered through ``orcho_run_resume`` — the SDK
    # halt path already wrote the terminal ``halt_reason`` /
    # ``halted_at`` / ``evidence.json``, and a checkpoint resume that
    # re-ran ``_finalize_cross_terminal`` would overwrite those
    # timestamps with the resume-time values. Code review fix.
    if (
        _resume_mode == _ResumeMode.CHECKPOINT
        and _resumed is not None
        and (
            _is_terminal_success(_resumed.meta)
            or _is_terminal_phase_handoff_halt(_resumed.meta)
        )
    ):
        print(
            f"Run {args.resume} cannot be resumed from checkpoint "
            f"(status: {_resumed.meta.get('status')}); pass --task "
            "with --resume to create a follow-up.",
            file=sys.stderr,
        )
        sys.exit(0)

    # See project_orchestrator.main(): set env before the first config.RUNS_DIR access.
    try:
        task = _resolve_task(
            explicit_task=args.task,
            explicit_task_file=args.task_file,
            explicit_project=None,
            resumed=_resumed,
        )
        projects_argv = _resolve_projects_argv(
            explicit_projects=args.projects,
            resumed=_resumed,
        )
    except _ResumeContextError as exc:
        print_error(str(exc))
        sys.exit(1)

    try:
        projects = _xo.parse_projects(projects_argv, workspace=args.workspace)
    except (ValueError, FileNotFoundError) as e:
        print_error(str(e))
        sys.exit(1)

    # Workspace walk-up: only fires when ``--workspace`` wasn't supplied
    # (handled early above, before resume lookups). Cross-runs typically
    # span sibling projects under one workspace (e.g. unity / api / stats
    # all under ``~/www/qcg/``), so the first project's walk-up reliably
    # finds the right one. When projects come from disjoint workspaces —
    # unusual but legal — we warn and use the first; pass --workspace
    # explicitly to disambiguate.
    if not args.workspace:
        from pipeline.project.bootstrap import (
            infer_workspace_from_project as _infer_workspace_from_project,
        )
        first_alias, first_path = next(iter(projects.items()))
        inferred = _infer_workspace_from_project(str(first_path))
        if inferred is not None:
            mismatched = []
            for alias, p in projects.items():
                other = _infer_workspace_from_project(str(p))
                if other is not None and other != inferred:
                    mismatched.append(f"{alias}={other}")
            if mismatched:
                print(
                    f"  ⚠ Cross run: projects span multiple workspaces — "
                    f"using {inferred} (from '{first_alias}'). "
                    f"Other projects resolve to: {', '.join(mismatched)}. "
                    f"Pass --workspace explicitly to override.",
                    file=sys.stderr,
                )
            os.environ["ORCHO_WORKSPACE"] = str(inferred)
            os.environ["ORCHO_RUNSPACE"] = str(Path(inferred) / "runspace")
            config._reset_config()
            print(f"  ↳ workspace auto-derived from --projects: {inferred}")

    _pipeline_cfg = config.AppConfig.load().pipeline
    if args.model is None:
        args.model = config.AppConfig.load().phase_model_map.get(
            "implement",
            "claude-opus-4-8[1m]",
        )

    # Output dir rules (parallel single-project):
    #   FRESH      → new run dir
    #   CHECKPOINT → parent run dir (existing)
    #   FOLLOWUP   → new run dir; parent stays untouched as context
    if args.output_dir:
        output_dir = Path(args.output_dir)
    elif _resume_mode == _ResumeMode.CHECKPOINT:
        try:
            output_dir = config.get_runs_dir() / args.resume
        except config.WorkspaceNotResolvedError as exc:
            print_error(str(exc))
            sys.exit(2)
        if not output_dir.is_dir():
            print_error(
                f"--resume {args.resume!r}: cross run_dir does not exist: {output_dir}"
            )
            sys.exit(2)
    else:
        # FRESH or FOLLOWUP: mint a fresh timestamp.
        session_ts_cli = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            output_dir = config.get_runs_dir() / session_ts_cli
        except config.WorkspaceNotResolvedError as exc:
            print_error(str(exc))
            sys.exit(2)

    # CHECKPOINT continues into the parent dir; only the FOLLOWUP/FRESH
    # case must guarantee a fresh run dir for collision detection.
    _resume_for_collision_check = (
        args.resume if _resume_mode == _ResumeMode.CHECKPOINT else None
    )
    try:
        _xo._assert_fresh_run_dir_available(
            output_dir, resume_from=_resume_for_collision_check,
        )
    except RunIdCollisionError as exc:
        print_error(str(exc))
        sys.exit(2)

    output_dir.mkdir(parents=True, exist_ok=True)

    # ADR 0022 kwarg mapping: CLI --model-build/-fix/-review keep
    # their historical names (public surface), but
    # build_phase_config_from_overrides expects the post-rename
    # implement / repair_changes / review_changes parameters. Always build
    # this after workspace env resolution so workspace-local config drives
    # reviewer models even when no CLI --model-* override is supplied.
    phase_config = build_phase_config_from_overrides(
        plan=args.model_plan,
        implement=args.model_build,
        repair_changes=args.model_fix,
        review_changes=args.model_review,
        runtime_plan=args.runtime_plan,
        runtime_implement=args.runtime_build,
        runtime_repair_changes=args.runtime_fix,
        runtime_review_changes=args.runtime_review,
        # Per-project plugin hints don't apply to cross-pipeline overrides;
        # the same overrides win for every sub-project.
        plugin=None,
    )

    from core.observability.logging import apply_output_mode
    apply_output_mode(args.output)
    _provider = make_provider(
        args.mock,
        mock_validate_plan_reject_rounds=args.mock_validate_plan_reject,
    )
    _session_mode = SessionMode.STATELESS if args.mock else SessionMode.AUTO

    # Hermetic mock mode: replace every PhaseAgentConfig slot with inline
    # stubs. Without this override, child sub-pipelines and cross gates
    # (cross_validate_plan / contract_check / cross_final_acceptance) still
    # invoke real ``claude`` / ``codex`` CLI even with ``--mock`` because
    # those phases read off ``phase_config`` directly, not the ``provider``
    # arg. ``--mock`` MUST guarantee zero real CLI calls — mirrors the
    # symmetric override in ``project_orchestrator``.
    if args.mock:
        _configured_phase_config = phase_config
        phase_config = make_mock_phase_config(
            validate_plan_reject_rounds=args.mock_validate_plan_reject,
        )
        for _slot in PhaseAgentConfig.__dataclass_fields__:
            _configured_agent = getattr(_configured_phase_config, _slot, None)
            _mock_agent = getattr(phase_config, _slot, None)
            _configured_model = getattr(_configured_agent, "model", None)
            if isinstance(_configured_model, str) and _mock_agent is not None:
                _mock_agent.model = _configured_model

    # CHECKPOINT passes ``resume_from`` so the engine hydrates from the
    # parent cross-checkpoint; FOLLOWUP intentionally clears it — a new
    # cross run shouldn't reuse the parent's per-alias checkpoints.
    _checkpoint_resume_from: str | None = (
        args.resume if _resume_mode == _ResumeMode.CHECKPOINT else None
    )
    _followup_parent_run_id: str | None = None
    _followup_parent_run_dir: str | None = None
    _followup_parent_status: str | None = None
    _followup_base_task: str | None = None
    _followup_session_seeds_per_alias: dict[str, dict[str, str]] | None = None
    if _resume_mode == _ResumeMode.FOLLOWUP and _resumed is not None:
        _followup_parent_run_id = args.resume
        _followup_parent_run_dir = str(_resumed.path.parent.resolve())
        _parent_status_val = _resumed.meta.get("status")
        _followup_parent_status = (
            _parent_status_val if isinstance(_parent_status_val, str) else None
        )
        _parent_task_val = _resumed.meta.get("task")
        _followup_base_task = (
            _parent_task_val if isinstance(_parent_task_val, str) else None
        )
        # Per-alias seed map: each child sub-pipeline writes its own
        # meta.json under <cross_parent_dir>/<alias>/meta.json (Step-0
        # shape). Extract once here; pass through to run_cross_pipeline
        # which slices per alias inside its per-alias loop.
        from pipeline.control import (
            extract_cross_followup_session_seeds as _extract_cross_seeds,
        )
        _followup_session_seeds_per_alias = _extract_cross_seeds(
            _resumed.path.parent,
            list(projects.keys()),
        )
        if not _followup_session_seeds_per_alias:
            _followup_session_seeds_per_alias = None
    # The engine only hydrates phase state for CHECKPOINT; FOLLOWUP wants
    # an empty session even though we keep the parent meta for context.
    _resumed_meta_for_pipeline: dict | None = None
    if _resume_mode == _ResumeMode.CHECKPOINT and _resumed is not None:
        _resumed_meta_for_pipeline = _resumed.meta

    # Lazy imports — sdk.phase_handoff pulls back into the cross
    # orchestrator (resume path), so a top-level import here would
    # build the same circular graph mono dodges via in-body imports.
    from pipeline.control.handoff_prompt import (
        HANDOFF_PROMPT_ABORTED as _HANDOFF_PROMPT_ABORTED,
        prompt_phase_handoff_action as _prompt_phase_handoff_action,
        should_prompt_for_phase_handoff as _should_prompt_for_phase_handoff,
    )
    from sdk.errors import (
        InvalidPhaseHandoffState as _SDKInvalidPhaseHandoffState,
    )
    from sdk.phase_handoff import (
        phase_handoff_decide as _sdk_phase_handoff_decide,
    )

    _result_session: dict | None = None
    _status: str | None = None
    try:
        while True:
            _result_session = _xo.run_cross_pipeline(
                task=task,
                projects=projects,
                max_rounds=args.max_rounds,
                model=args.model,
                output_dir=output_dir,
                dry_run=args.dry_run,
                provider=_provider,
                phase_config=phase_config,
                cross_mode=args.mode,
                plan_file=args.plan_file,
                resume_from=_checkpoint_resume_from,
                hypothesis_enabled=args.hypothesis,
                profile_name=args.profile,
                operator_decisions=getattr(args, "parsed_decisions", None),
                no_interactive=getattr(args, "no_interactive", False),
                resumed_meta=_resumed_meta_for_pipeline,
                resume_mode=(
                    _resume_mode.value
                    if _resume_mode == _ResumeMode.FOLLOWUP else None
                ),
                followup_parent_run_id=_followup_parent_run_id,
                followup_parent_run_dir=_followup_parent_run_dir,
                followup_parent_status=_followup_parent_status,
                followup_base_task=_followup_base_task,
                followup_session_seeds_per_alias=(
                    _followup_session_seeds_per_alias
                ),
            )
            _status = (
                _result_session.get("status") if _result_session else None
            )

            # Interactive handoff parity with single-project ``orcho run``
            # (``pipeline.project.handoff.process_pending_phase_handoffs``).
            # Cross-owned handoffs (cross_plan + cfa) resolve against the
            # cross run itself. Project-proxy pauses
            # (``project:<alias>:<child_id>``) are prompted here too: the
            # decision is recorded against the parent id on the cross run
            # and the ``phase_handoff_kind == "project"`` resume router
            # (``pipeline.cross_project.app``) routes it to the child run
            # — the same off-band ``decide(parent) + resume`` sequence the
            # operator would otherwise have to drive by hand.
            if _status != "awaiting_phase_handoff":
                break
            payload = (_result_session or {}).get("phase_handoff") or {}
            handoff_id = (
                payload.get("id") if isinstance(payload, dict) else None
            )
            if not (
                isinstance(handoff_id, str)
                and _is_promptable_handoff_id(handoff_id)
            ):
                break
            if not _should_prompt_for_phase_handoff(
                no_interactive=getattr(args, "no_interactive", False),
            ):
                break
            signal = _build_handoff_signal_from_payload(payload)
            if signal is None:
                break
            decision_input = _prompt_phase_handoff_action(signal)
            if decision_input is _HANDOFF_PROMPT_ABORTED:
                warn(
                    "Interactive phase-handoff decision aborted; "
                    "leaving cross run paused for off-band resolution."
                )
                break

            run_id = output_dir.name
            runs_root = output_dir.parent
            try:
                _sdk_phase_handoff_decide(
                    run_id,
                    handoff_id,
                    decision_input.action,
                    feedback=decision_input.feedback,
                    note=decision_input.note,
                    runs_dir=runs_root,
                    cwd=None,
                )
            except (ValueError, _SDKInvalidPhaseHandoffState) as exc:
                print_error(
                    f"phase_handoff_decide({decision_input.action!r}) "
                    f"rejected: {exc}"
                )
                sys.exit(1)

            success(f"Decision recorded: {decision_input.action}")
            print(paint(
                f"  ↳ Resuming cross run after "
                f"{decision_input.action} decision...",
                C.GREY,
            ))

            # Re-enter the cross runner in checkpoint-resume mode. The
            # cross planning loop's ``_resume_handoff_decision`` branch
            # reads the just-written decision artifact and applies halt
            # / continue / retry_feedback; the loop continues until the
            # status leaves ``awaiting_phase_handoff`` (terminal or a
            # fresh re-pause from another rejected round).
            try:
                _resumed = _load_resume_meta(output_dir)
            except _ResumeContextError as exc:
                print_error(str(exc))
                sys.exit(2)
            _checkpoint_resume_from = run_id
            _resumed_meta_for_pipeline = _resumed.meta
            # The FOLLOWUP seed bundle only applies to the original
            # entry; on interactive resume we're a CHECKPOINT loop on
            # the same run, so clear FOLLOWUP-only inputs to avoid
            # accidentally re-seeding child sub-pipelines.
            _resume_mode = _ResumeMode.CHECKPOINT
            _followup_parent_run_id = None
            _followup_parent_run_dir = None
            _followup_parent_status = None
            _followup_base_task = None
            _followup_session_seeds_per_alias = None

        # Status → exit code mapping. Mirrors single-project quality-gate
        # signalling: exit 4 distinguishes "waiting for human review" from
        # crash / non-zero failure so CI and dashboards can pivot into
        # manual-review without treating it as a failed run. ``failed``
        # (contract_check parse error or rejected verdict) → exit 1.
        if _status in ("awaiting_gate_decision", "awaiting_phase_handoff"):
            # Reached when the run is non-interactive (``--no-interactive``
            # / non-TTY), the operator aborted the TTY prompt, the payload
            # was malformed, or the handoff id is unknown (neither
            # cross-owned nor project-proxy — off-band decision tools own
            # it). The same resumable-pause exit code applies so CI /
            # dashboards / MCP supervisors pivot into manual review
            # without treating it as a failed run.
            sys.exit(4)
        if _status == "failed":
            # Surface ``failure_reason`` on stderr so the cause is
            # grep-able in CI logs and dashboards. ADR 0025 Phase 3
            # uses this text to distinguish ``cross_final_acceptance``
            # gate rejections from ``contract_check`` failures.
            _reason = (
                _result_session.get("failure_reason")
                if _result_session else None
            )
            if _reason:
                print(
                    f"Cross-project pipeline failed: {_reason}",
                    file=sys.stderr,
                )
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠ Interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()


__all__ = ["main", "print_error"]
