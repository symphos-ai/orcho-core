"""pipeline.argv — public argv builder for the project orchestrator.

External consumers (orcho-mcp's runs supervisor today, future scripted
launchers) need to build the same command-line arguments the ``orcho`` CLI
generates internally. Before this module that logic was a private helper
``cli.orcho._build_orch_argv`` taking an ``argparse.Namespace`` — a fragile
contract that:
  - tied callers to argparse-shaped objects,
  - was named with a leading underscore (private by convention),
  - sat in a CLI module that external packages shouldn't import.

``build_orch_argv`` here takes plain keyword arguments so any caller can
construct the run without faking a Namespace. The legacy
``cli.orcho._build_orch_argv`` is kept as a thin Namespace adapter for
backward compatibility.

**Stability:** this is a public API. Breaking changes only in major
version. Adding new keyword arguments (with defaults) is backwards
compatible and allowed in minor releases.
"""
from __future__ import annotations

from core.observability.logging import OutputMode, normalize_output_mode

__all__ = ["build_orch_argv"]


def _resolve_output_mode(
    output_mode: str | None,
    *,
    verbose: bool,
    stream_output: bool,
) -> OutputMode:
    """Resolve the canonical output mode plus legacy boolean shims."""
    mode = normalize_output_mode(output_mode)
    if mode != "summary":
        return mode
    if verbose:
        return "debug"
    if stream_output:
        return "live"
    return mode


def build_orch_argv(
    *,
    project: str | None = None,
    task: str | None = None,
    task_file: str | None = None,
    workspace: str | None = None,
    resume: str | None = None,
    run_id: str | None = None,
    max_rounds: int | None = None,
    mock_validate_plan_reject: int = 0,
    model: str | None = None,
    output_dir: str | None = None,
    dry_run: bool = False,
    mock: bool = False,
    output_mode: str | None = "summary",
    verbose: bool = False,
    stream_output: bool = False,
    profile: str | None = None,
    cross_mode: str | None = None,
    session_mode: str | None = None,
    session_split: list[str] | None = None,
    model_plan: str | None = None,
    model_implement: str | None = None,
    model_repair_changes: str | None = None,
    model_review_changes: str | None = None,
    runtime_plan: str | None = None,
    runtime_implement: str | None = None,
    runtime_repair_changes: str | None = None,
    runtime_review_changes: str | None = None,
    attach: list[str] | None = None,
    attach_text: list[str] | None = None,
    attach_image: list[str] | None = None,
    attach_binary: list[str] | None = None,
    no_interactive: bool = False,
    from_run_plan: str | None = None,
    no_worktree_isolation: bool = False,
) -> list[str]:
    """Return the argv list for ``python -m pipeline.project_orchestrator``.

    Args:
        project: required absolute path to the project directory.
        task: inline task description (mutually exclusive with task_file).
        task_file: path to a markdown file containing the task.
        workspace: explicit workspace dir (overrides env / walkup).
        resume: resume an existing run by run_id.
        run_id: explicit run_id for the new run. Maps to ``--run-id`` and is
            picked up by ``run_pipeline`` instead of an internally minted
            ``session_ts``. Supervisor callers MUST set this so that the
            run's folder name (``output_dir.name``) and the checkpoint key
            agree — otherwise resume will reference a different run_id than
            the one on disk.
        max_rounds: review/fix loop cap (the plan loop's budget comes
            from the active profile's ``LoopStep.max_rounds``).
        mock_validate_plan_reject: integer count of synthetic
            validate_plan rejections for testing the phase handoff trigger.
        model: default model when per-phase overrides aren't set.
        output_dir: explicit run output directory.
        dry_run / mock: standard flags.
        output_mode: transcript mode ("summary", "live", or "debug").
        verbose / stream_output: legacy boolean shims. ``verbose=True`` maps
            to debug; ``stream_output=True`` maps to live.
        profile: semantic work-kind profile name ("feature" default;
            "small_task" / "complex_feature" / "planning" / "delivery_audit"
            / "code_review" / "research" / "refactor" / "migration", the
            internal "task", or any custom profile name). Phase 6
            replacement for the legacy ``mode`` arg.
        cross_mode: cross-orchestrator-only flag ("full" or "plan") —
            full cross vs plan-only cross. Threaded through to
            ``--mode`` on the cross command surface; per-project
            ``orcho run`` ignores this.
        session_mode: agent session mode ("auto"/"stateless"/"chain"/"hybrid").
        session_split: per-phase prompt-session split overrides, each as
            ``"phase=split"`` where split is stateless/per_phase/per_role/common.
        model_<phase> / provider_<phase>: per-phase model/provider overrides.

    Returns:
        argv list ready to be passed to ``subprocess.Popen`` after
        prepending an interpreter + ``-m pipeline.project_orchestrator``.
    """
    argv: list[str] = []
    if task:
        argv += ["--task", task]
    if task_file:
        argv += ["--task-file", task_file]
    # ``--project`` is required for fresh runs and optional on
    # ``--resume`` (the project orchestrator falls back to
    # ``meta.json["project"]``). Emit the flag only when a value is
    # supplied; emitting ``--project None`` would short-circuit that
    # fallback and break top-level ``orcho run --resume RUN_ID``.
    if project:
        argv += ["--project", project]
    if workspace:
        argv += ["--workspace", workspace]
    if resume:
        argv += ["--resume", resume]
    if from_run_plan:
        argv += ["--from-run-plan", from_run_plan]
    if no_interactive:
        argv += ["--no-interactive"]
    if no_worktree_isolation:
        argv += ["--no-worktree-isolation"]
    if run_id:
        argv += ["--run-id", run_id]
    if max_rounds is not None:
        argv += ["--max-rounds", str(max_rounds)]
    if int(mock_validate_plan_reject or 0) > 0:
        argv += ["--mock-validate-plan-reject", str(int(mock_validate_plan_reject))]
    if model:
        argv += ["--model", model]
    if output_dir:
        argv += ["--output-dir", output_dir]
    if dry_run:
        argv += ["--dry-run"]
    if mock:
        argv += ["--mock"]
    resolved_output = _resolve_output_mode(
        output_mode, verbose=verbose, stream_output=stream_output,
    )
    # Always forward the resolved mode — never omit ``summary``. Omitting it
    # let the orchestrator fall back to its own ``config.cli_output_mode()``
    # default, so an explicit ``orcho run --output summary`` was silently
    # overridden by a workspace/global ``cli.output_mode = live`` default. The
    # caller's resolved choice (which already folds in that config default when
    # the user passed nothing) must win.
    argv += ["--output", resolved_output]
    # Phase 6: ``--mode`` / ``--skip-plan`` removed. ``--profile`` is
    # the single dispatch knob for ``orcho run``. ``cross_mode`` is
    # the cross-only ``orcho cross --mode {full,plan}`` flag (full
    # cross vs plan-only cross) — separate concept, threaded through
    # for the cross command without affecting per-project profile
    # dispatch.
    #
    # Always emit ``--profile`` when the caller explicitly supplied a
    # value, including ``"feature"``. The orchestrator's argparse
    # default is now ``None`` so it can distinguish "explicit override"
    # from "use meta.profile inherit / fresh-run default" — silently
    # skipping ``--profile feature`` here would collapse the explicit
    # override into the inherit path on resume.
    if profile is not None:
        argv += ["--profile", profile]
    if cross_mode and cross_mode != "full":
        argv += ["--mode", cross_mode]
    if session_mode and session_mode != "auto":
        argv += ["--session-mode", session_mode]
    for value in session_split or ():
        argv += ["--session-split", value]
    for value, opt in [
        (model_plan,              "--model-plan"),
        (model_implement,         "--model-implement"),
        (model_repair_changes,    "--model-repair-changes"),
        (model_review_changes,    "--model-review-changes"),
        (runtime_plan,           "--runtime-plan"),
        (runtime_implement,      "--runtime-implement"),
        (runtime_repair_changes, "--runtime-repair-changes"),
        (runtime_review_changes, "--runtime-review-changes"),
    ]:
        if value:
            argv += [opt, value]
    # Phase 4.5: attachments — multiple flags emit one argv pair per entry.
    # `--attach <path>` auto-detects kind; `--attach-{text,image,binary}`
    # forces the kind for paths whose extension would otherwise mis-detect.
    for paths, opt in [
        (attach,        "--attach"),
        (attach_text,   "--attach-text"),
        (attach_image,  "--attach-image"),
        (attach_binary, "--attach-binary"),
    ]:
        for p in paths or ():
            argv += [opt, p]
    return argv
