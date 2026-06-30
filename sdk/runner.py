"""Pipeline launch — re-exports plus thin Namespace adapters for the CLI.

The three primary entry points (`run_pipeline`, `run_cross_pipeline`,
`build_orch_argv`) are already library-shaped in their original
modules; this module is the canonical re-export point so embedders
import them from the SDK boundary.

`run_pipeline_from_args` and `run_cross_from_args` are the existing
CLI helpers (Namespace → argv → orchestrator `main()`) lifted out of
`cli/orcho.py`. They keep the `sys.argv`+`main()` path because too
much load-bearing CLI plumbing (task-file, workspace inference, mock
provider, trace, collision checks) sits inside the orchestrator's
argparse path. Retiring that path is out of scope for this milestone.
"""
from __future__ import annotations

import sys
from typing import Any

from core.io.retry import AgentCallError
from pipeline.argv import build_orch_argv
from pipeline.control import (
    OperatorDecisionError,
    parse_operator_decisions,
)
from pipeline.cross_project.orchestrator import run_cross_pipeline
from pipeline.project.app import run_pipeline
from sdk.errors import OrchoError


def build_orch_argv_from_args(args: Any) -> list[str]:
    """Translate an argparse `Namespace` into orchestrator argv.

    Pure: no side effects. Used by `run_pipeline_from_args` and by
    legacy CLI tests that exercise the Namespace adapter directly.
    """
    return build_orch_argv(
        project=args.project,
        task=getattr(args, "task", None),
        task_file=getattr(args, "task_file", None),
        workspace=getattr(args, "workspace", None),
        resume=getattr(args, "resume", None),
        run_id=getattr(args, "run_id", None),
        max_rounds=getattr(args, "max_rounds", None),
        mock_validate_plan_reject=getattr(args, "mock_validate_plan_reject", 0) or 0,
        model=getattr(args, "model", None),
        output_dir=getattr(args, "output_dir", None),
        dry_run=getattr(args, "dry_run", False),
        mock=getattr(args, "mock", False),
        output_mode=getattr(args, "output", None),
        verbose=getattr(args, "verbose", False),
        stream_output=getattr(args, "stream_output", False),
        profile=getattr(args, "profile", None),
        # ``orcho run --mode {fast,pro,governed}`` (T6) is the run's
        # verification strictness, NOT the cross ``--mode {full,plan}`` slice
        # selector. It is threaded to the run via the ORCHO_WORK_MODE env
        # (see ``cli.orcho.cmd_run``), so it must not leak into the mono
        # orchestrator argv as ``cross_mode``. The mono adapter never carries
        # a cross slice (cross goes through ``run_cross_from_args``).
        cross_mode=None,
        session_mode=getattr(args, "session_mode", None),
        session_split=getattr(args, "session_split", None),
        model_plan=getattr(args, "model_plan", None),
        model_implement=getattr(args, "model_implement", None),
        model_repair_changes=getattr(args, "model_repair_changes", None),
        model_review_changes=getattr(args, "model_review_changes", None),
        runtime_plan=getattr(args, "runtime_plan", None),
        runtime_implement=getattr(args, "runtime_implement", None),
        runtime_repair_changes=getattr(args, "runtime_repair_changes", None),
        runtime_review_changes=getattr(args, "runtime_review_changes", None),
        attach=getattr(args, "attach", None),
        attach_text=getattr(args, "attach_text", None),
        attach_image=getattr(args, "attach_image", None),
        attach_binary=getattr(args, "attach_binary", None),
        no_interactive=bool(getattr(args, "no_interactive", False)),
        from_run_plan=getattr(args, "from_run_plan", None),
        no_worktree_isolation=bool(getattr(args, "no_worktree_isolation", False)),
    )


def run_pipeline_from_args(args: Any) -> int:
    """Translate an argparse `Namespace` and invoke the project orchestrator.

    Returns the orchestrator's exit code; never raises `SystemExit`. A typed
    ``AgentCallError`` (API-client failure that already halted the run) is an
    expected terminal outcome here — it is caught and turned into exit code 1
    with its cause on stderr, not a Python traceback.
    """
    # Validate operator-decision overrides up front so a bogus target
    # on ``orcho run`` (where no decision targets are applicable today)
    # fails clearly instead of being silently dropped.
    try:
        parse_operator_decisions(
            getattr(args, "decision", None),
            getattr(args, "decision_feedback", None),
            subcommand="run",
        )
    except OperatorDecisionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    argv = build_orch_argv_from_args(args)

    # Lazy import keeps SDK import cheap. ``main`` is one of the four
    # stable shim names exported by ``pipeline.project_orchestrator``
    # (the canonical home is ``pipeline.project.cli`` but non-CLI code
    # must not import from the CLI leaf per ADR 0042 stop #9, so we go
    # through the shim).
    from pipeline.project_orchestrator import main as _cli_main

    sys.argv = ["orchestrator"] + argv
    try:
        _cli_main()
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    except AgentCallError as e:
        # An API-client failure (auth, API unreachable, rate limit, or any
        # other non-zero CLI exit) already halted the run and wrote a
        # structured ``failed`` record with the FAILED-in-<phase> block. The
        # exception is re-raised so callers/tests keep the type, but at the CLI
        # boundary it is an expected terminal outcome — exit with a code, not a
        # Python traceback. ``AgentAuthenticationError`` (a subclass) carries
        # multi-line login guidance; print it. Other shapes print their
        # one-line cause as a final summary.
        print(str(e), file=sys.stderr)
        return 1
    except OrchoError as e:
        print(str(e), file=sys.stderr)
        return e.exit_code


def run_cross_from_args(args: Any) -> int:
    """Translate cross-pipeline argparse `Namespace` and invoke the orchestrator."""
    # Validate decisions before assembling argv: clear errors here beat
    # surfacing them from deep inside the orchestrator.
    try:
        parse_operator_decisions(
            getattr(args, "decision", None),
            getattr(args, "decision_feedback", None),
            subcommand="cross",
        )
    except OperatorDecisionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    argv: list[str] = []
    if getattr(args, "task", None):
        argv += ["--task", args.task]
    if getattr(args, "task_file", None):
        argv += ["--task-file", args.task_file]
    if getattr(args, "projects", None):
        argv += ["--projects"] + args.projects
    if getattr(args, "workspace", None):
        argv += ["--workspace", args.workspace]
    if getattr(args, "resume", None):
        argv += ["--resume", args.resume]
    for raw in (getattr(args, "decision", None) or ()):
        argv += ["--decision", raw]
    if getattr(args, "decision_feedback", None):
        argv += ["--decision-feedback", args.decision_feedback]
    if getattr(args, "no_interactive", False):
        argv += ["--no-interactive"]
    if getattr(args, "max_rounds", None) is not None:
        argv += ["--max-rounds", str(args.max_rounds)]
    if int(getattr(args, "mock_validate_plan_reject", 0) or 0) > 0:
        argv += ["--mock-validate-plan-reject", str(int(args.mock_validate_plan_reject))]
    if getattr(args, "output_dir", None):
        argv += ["--output-dir", args.output_dir]
    if getattr(args, "dry_run", False):
        argv += ["--dry-run"]
    if getattr(args, "mock", False):
        argv += ["--mock"]
    output_mode = getattr(args, "output", None)
    if output_mode and output_mode != "summary":
        argv += ["--output", output_mode]
    elif getattr(args, "verbose", False):
        argv += ["--output", "debug"]
    elif getattr(args, "stream_output", False):
        argv += ["--output", "live"]
    if getattr(args, "mode", None) and args.mode != "full":
        argv += ["--mode", args.mode]
    # Always emit ``--profile`` when explicitly supplied (including
    # "feature") so the orchestrator can distinguish deliberate
    # override from inherit-from-meta on resume.
    if getattr(args, "profile", None) is not None:
        argv += ["--profile", args.profile]
    for raw in (getattr(args, "session_split", None) or ()):
        argv += ["--session-split", raw]
    if getattr(args, "plan_file", None):
        argv += ["--plan-file", args.plan_file]
    for flag, opt in [
        ("model_plan", "--model-plan"),
        ("model_build", "--model-build"),
        ("model_fix", "--model-fix"),
        ("model_review", "--model-review"),
        ("runtime_plan", "--runtime-plan"),
        ("runtime_build", "--runtime-build"),
        ("runtime_fix", "--runtime-fix"),
        ("runtime_review", "--runtime-review"),
    ]:
        val = getattr(args, flag, None)
        if val:
            argv += [opt, val]

    from pipeline.cross_project import cli as xcli

    sys.argv = ["cross_orchestrator"] + argv
    try:
        xcli.main()
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    except AgentCallError as e:
        # Same controlled-halt boundary as ``run_pipeline_from_args``: an
        # API-client failure is an expected terminal outcome, not a crash.
        print(str(e), file=sys.stderr)
        return 1
    except OrchoError as e:
        print(str(e), file=sys.stderr)
        return e.exit_code


__all__ = [
    "run_pipeline",
    "run_cross_pipeline",
    "build_orch_argv",
    "build_orch_argv_from_args",
    "run_pipeline_from_args",
    "run_cross_from_args",
]
