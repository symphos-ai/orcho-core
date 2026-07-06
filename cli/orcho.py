#!/usr/bin/env python3
"""
cli/orcho.py — Unified `orcho` CLI facade.

Thin wrapper over the public SDK (`sdk/`). Each handler is at most a
few lines: parse args → call SDK → format result → print → return
exit code. Read-only/report handlers route through `_run_cli` for
shared error-to-exit-code mapping; side-effect handlers
(`cmd_evidence` with `--out`, `cmd_pricing_refresh`) own their
branching.

Subcommands:
  orcho run     — Run single-project pipeline
  orcho cross   — Run cross-project pipeline
  orcho status  — Show status of last (or specific) run
  orcho metrics — Show token/time metrics for last N runs
  orcho history — List recent runs
  orcho evidence— Compose run evidence bundle
  orcho repair-state — Inspect / safely apply known run-state repairs
  orcho cost    — API-equivalent cost report
  orcho pricing — Show / refresh pricing table
  orcho prompts — Show prompt resolution chain
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from importlib import metadata
from pathlib import Path

# Bootstrap: ensure the engine root is on sys.path so `python -m cli.orcho`
# and the `orcho` console script both resolve sibling packages. Imports
# below depend on this path setup and must stay after it — Ruff E402 is
# silenced for the rest of this file.
_CLI_DIR = Path(__file__).parent.resolve()
_CORE_DIR = _CLI_DIR.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from cli.install_guard import guard_against_retained_worktree_install

guard_against_retained_worktree_install(_CORE_DIR)

# ruff: noqa: E402

from cli._formatters import (
    colorize_evidence_markdown,
    format_cost_report,
    format_error,
    format_evidence,
    format_fine_tune,
    format_history,
    format_metrics_history,
    format_metrics_run,
    format_pricing,
    format_pricing_refresh_models,
    format_pricing_refresh_written,
    format_prompts_list,
    format_prompts_resolution,
    format_run_diff,
    format_status,
    format_verify_env,
    format_verify_list,
    format_verify_run,
    format_workspace_init,
    format_written_paths,
    project_evidence_json,
)
from cli._help import (
    CROSS_EPILOG,
    QUICK_HELP,
    RUN_EPILOG,
    TAGLINE,
    iter_command_groups,
    print_quick_help,
    render_verbose_header,
)
from cli._profile_prompt import require_profile_or_exit
from cli._repair_state import format_repair_report, repair_report_to_json
from cli._run import _run_cli
from cli._task_prompt import prompt_for_task_if_needed
from core.infra import config
from core.io import prompt_loader as _prompt_loader
from core.io.ansi import is_color_active
from pipeline.run_state import repair_run_state
from sdk import (
    EvidenceInvalid,
    PricingFetchError,
    aggregate_cost,
    collect_evidence,
    find_run,
    fine_tune_project,
    get_run_diff,
    init_workspace,
    list_history,
    list_metrics,
    list_prompts,
    load_meta,
    load_status,
    refresh_pricing,
    render_evidence_md,
    resolve_prompt,
    run_cross_from_args,
    run_pipeline_from_args,
    show_pricing,
    to_jsonable,
    verify_env,
    verify_list,
    verify_run,
    write_evidence_bundle,
)
from sdk.errors import OrchoError, PromptNotFound


def _positive_int(value: str) -> int:
    """Argparse type for "must be a positive integer".

    Raising :class:`argparse.ArgumentTypeError` makes argparse emit a clean
    ``parser.error`` exit (with the message on stderr and a non-zero code)
    without the handler needing access to the parser object.
    """
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"must be an integer, got {value!r}",
        ) from exc
    if n <= 0:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer, got {n}",
        )
    return n


def _version_string() -> str:
    """Installed package versions for ``--version``.

    orcho-core degrades to an explanatory note when package metadata is
    absent (source checkout run without an install); orcho-mcp is listed
    only when it is installed alongside.
    """
    try:
        lines = [f"orcho-core {metadata.version('orcho-core')}"]
    except metadata.PackageNotFoundError:
        lines = ["orcho-core (version unknown: package metadata not found)"]
    with contextlib.suppress(metadata.PackageNotFoundError):
        lines.append(f"orcho-mcp {metadata.version('orcho-mcp')}")
    return "\n".join(lines)


def _nonempty_str(value: str) -> str:
    """Argparse type for "non-empty after whitespace strip".

    Returns the stripped value, so handlers always see normalized input.
    """
    stripped = value.strip()
    if not stripped:
        raise argparse.ArgumentTypeError("must be non-empty")
    return stripped

# ─────────────────────────────────────────────────────────────────────────────
# run / cross / web — conservative delegates (orchestrator owns argparse path)
# ─────────────────────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> int:
    prompt_for_task_if_needed(args)
    if args.task_file:
        from pipeline.control import ResumeContextError, resolve_task
        try:
            resolve_task(
                explicit_task=None,
                explicit_task_file=args.task_file,
                explicit_project=args.project,
                resumed=None,
            )
        except ResumeContextError as exc:
            print(format_error(exc), file=sys.stderr)
            return 1
    code = require_profile_or_exit(args, include_auto_detect=True)
    if code is not None:
        return code
    # Thread the verification-strictness override to the run via env (T6).
    # run_pipeline's signature is locked, so ORCHO_WORK_MODE is the channel
    # the default-mode projection reads at contract assembly. Distinct from
    # cross ``--mode {full,plan}`` — this never enters argv child-dispatch.
    if getattr(args, "mode", None):
        os.environ["ORCHO_WORK_MODE"] = args.mode
    return run_pipeline_from_args(args)


def cmd_cross(args: argparse.Namespace) -> int:
    from pipeline.cross_project.profile_projection import (
        CrossProjectionError,
        project_cross_profile,
    )

    def _cross_eligible(profile) -> bool:
        try:
            project_cross_profile(profile)
        except CrossProjectionError:
            return False
        return True

    code = require_profile_or_exit(args, profile_filter=_cross_eligible)
    if code is not None:
        return code
    return run_cross_from_args(args)


def cmd_web(args: argparse.Namespace) -> int:
    """Delegate to the ``orcho-web`` package, which owns the dashboard."""
    try:
        from orcho_web.launcher import main as web_main
    except ImportError:
        print(
            "orcho-web is not installed.\nInstall it with: pip install orcho-web",
            file=sys.stderr,
        )
        return 1
    argv = ["--port", str(args.port)]
    if getattr(args, "headless", False):
        argv.append("--headless")
    return web_main(argv)


def cmd_tui(args: argparse.Namespace) -> int:
    """Delegate to the ``orcho-tui`` package, which owns the terminal UI."""
    try:
        from orcho_tui.cli import main as tui_main
    except ImportError:
        # The terminal UI is an optional component. The `tui` command is always
        # reserved by the CLI; a selective install just needs the extra pulled in.
        print(
            'orcho-tui is not installed.\n'
            'Install it with:  pip install "orcho[tui]"   (or: pip install orcho-tui)',
            file=sys.stderr,
        )
        return 1
    argv: list[str] = []
    if getattr(args, "run_id", None):
        argv += ["--run-id", args.run_id]
    if getattr(args, "run_dir", None):
        argv += ["--run-dir", args.run_dir]
    if getattr(args, "follow", False):
        argv.append("--follow")
    if getattr(args, "replay", False):
        argv.append("--replay")
    return tui_main(argv)


# ─────────────────────────────────────────────────────────────────────────────
# Read/report handlers — route through _run_cli
# ─────────────────────────────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    """Status of a run. Preserves the legacy two-line empty-state output."""
    run_id = getattr(args, "run_id", None)
    workspace = getattr(args, "workspace", None)
    verbose = bool(getattr(args, "verbose", False))
    try:
        status = load_status(run_id, workspace=workspace)
    except OrchoError:
        # Empty workspace OR unknown run id → preserve legacy two-line
        # output that names the runs directory for the user.
        from sdk.runs import find_runs_dir
        try:
            rd = find_runs_dir(workspace=workspace)
        except OrchoError as exc:
            print(format_error(exc), file=sys.stderr)
            return exc.exit_code
        suffix = f" for id={run_id}" if run_id else ""
        print(f"No run found{suffix}.")
        print(f"Runs dir: {rd}")
        return 1
    print(format_status(status, verbose=verbose))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    return _run_cli(
        lambda: list_history(
            last=getattr(args, "last", 10),
            workspace=getattr(args, "workspace", None),
        ),
        format_history,
    )


def cmd_metrics(args: argparse.Namespace) -> int:
    run_id = getattr(args, "run_id", None)
    if run_id:
        from sdk import get_run_metrics
        return _run_cli(
            lambda: get_run_metrics(
                run_id, workspace=getattr(args, "workspace", None)
            ),
            format_metrics_run,
        )

    # Historical path: resolve runs_dir up front so the empty-state
    # formatter can name it (preserves byte-parity with legacy CLI).
    workspace = getattr(args, "workspace", None)
    try:
        from sdk.runs import find_runs_dir
        rd = find_runs_dir(workspace=workspace)
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code

    rows = list_metrics(last=getattr(args, "last", 10), workspace=workspace)
    print(format_metrics_history(rows, runs_dir=rd))
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    return _run_cli(
        lambda: aggregate_cost(
            workspace=getattr(args, "workspace", None),
            window=getattr(args, "window", "30d"),
            top_n=int(getattr(args, "top", 5) or 5),
        ),
        format_cost_report,
    )


def cmd_pricing_show(args: argparse.Namespace) -> int:
    return _run_cli(show_pricing, format_pricing)


def _profile_steps(profile) -> str:
    phases: list[str] = []

    def collect(entries) -> None:
        for entry in entries:
            loop = getattr(entry, "steps", None)
            if loop is not None:
                collect(loop)
                continue
            phase = getattr(entry, "phase", None)
            if phase:
                phases.append(str(phase))

    collect(getattr(profile, "steps", ()))
    return " -> ".join(phases)


def _format_profile_catalog(profiles: dict) -> str:
    lines = ["Profiles"]
    for name in sorted(profiles):
        profile = profiles[name]
        kind = getattr(getattr(profile, "kind", None), "value", None) or "custom"
        variant = getattr(profile, "variant", None) or "-"
        description = getattr(profile, "description", "")
        steps = _profile_steps(profile)
        # ADR 0085: internal/system profiles stay visible in the catalog
        # (the registry must show them) but carry an ``[internal]`` chip so
        # operators know they are not first-run choices.
        chip = " [internal]" if getattr(profile, "internal", False) else ""
        lines.append(f"  {name:<12} {kind:<10} {variant:<10} {steps}{chip}")
        if description:
            lines.append(f"    {description}")
    return "\n".join(lines)


def _load_profile_catalog() -> dict:
    from core.infra.paths import CONFIG_DIR
    from pipeline.profiles.loader import load_profiles_v2_with_plugins

    return load_profiles_v2_with_plugins(CONFIG_DIR / "pipeline_profiles_v2.json")


def cmd_profiles_list(args: argparse.Namespace) -> int:
    return _run_cli(_load_profile_catalog, _format_profile_catalog)


def cmd_workflows_list(args: argparse.Namespace) -> int:
    return cmd_profiles_list(args)


def cmd_diff(args: argparse.Namespace) -> int:
    """Render the run's captured ``diff.patch`` artifact for stdout.

    Mode default is ``full`` (the raw patch — the command name says
    "diff"). Color is enabled for ``preview`` / ``stat`` when stdout is a
    TTY and ``NO_COLOR`` / ``--no-color`` are unset; ``full`` is always
    colorless so the output stays pipeable to ``git apply``.

    Missing ``diff.patch`` for a valid run exits 0 with a clean message —
    "artifact absent but command worked". Unknown run / unresolved
    workspace exits via the SDK error → exit-code mapping in ``_run_cli``.
    """
    mode = getattr(args, "diff_mode", "full")
    no_color = bool(getattr(args, "no_color", False))
    use_color = mode != "full" and not no_color and is_color_active()

    def _call():
        record = get_run_diff(
            args.run_id,
            workspace=getattr(args, "workspace", None),
            mode=mode,
            path=getattr(args, "path", None),
            max_bytes=getattr(args, "max_bytes", None),
            color=use_color,
        )
        # Durable patch-integrity advisory goes to stderr so a piped ``full``
        # patch on stdout stays byte-clean for ``git apply``. The note rides on
        # the existing ``message`` field (no new wire field). This fires
        # regardless of ``found``: a missing/invalid run-level patch surfaces
        # the recorded reason+path even when ``found is False``.
        note = getattr(record, "message", None)
        if isinstance(note, str) and note.startswith("patch integrity"):
            print(note, file=sys.stderr)
        return record

    return _run_cli(_call, format_run_diff)


# ─────────────────────────────────────────────────────────────────────────────
# Side-effect handlers — own branching, reuse format_error for stderr
# ─────────────────────────────────────────────────────────────────────────────


def cmd_evidence(args: argparse.Namespace) -> int:
    """Compose evidence; either print to stdout or write to disk.

    With ``--diff[=preview|stat|full]`` the stdout output is augmented:

    - ``--format md``: append a ``## Diff`` section after the bundle.
    - ``--format json``: wrap the output as
      ``{"evidence": <bundle>, "diff": <record>}``. The schema-validated
      bundle inside ``"evidence"`` is byte-identical to today's output;
      this wrapper only appears when ``--diff`` is passed.

    ``--out`` writes the canonical bundle on disk (``evidence.json`` +
    ``evidence.md``) regardless of ``--diff`` — disk artifacts keep the
    schema-validated shape so existing consumers / tests don't break.
    """
    run_id = getattr(args, "run_id", None)
    workspace = getattr(args, "workspace", None)
    diff_mode = getattr(args, "diff", None)
    try:
        bundle = collect_evidence(run_id, workspace=workspace)
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code if not isinstance(exc, EvidenceInvalid) else 2

    out_dir = getattr(args, "out", None)
    if out_dir is not None:
        paths = write_evidence_bundle(bundle, Path(out_dir))
        print(format_written_paths(paths))
        return 0

    diff_record = None
    if diff_mode is not None:
        try:
            diff_record = get_run_diff(
                run_id,
                workspace=workspace,
                mode=diff_mode,
                color=False,
            )
        except OrchoError as exc:
            print(format_error(exc), file=sys.stderr)
            return exc.exit_code

    fmt = getattr(args, "format", "json")
    if fmt == "md":
        debug = bool(getattr(args, "debug", False))
        md = render_evidence_md(bundle, debug=debug) if debug else render_evidence_md(bundle)
        # The colorizer routes every ANSI insertion through paint(), so
        # the call-site no longer needs to gate on TTY / NO_COLOR — the
        # shared color policy (auto-detect + process override) decides
        # inside. Calling unconditionally lets set_color_enabled(True)
        # / set_color_enabled(False) reach the formatter; the pre-gate
        # would have vetoed that wiring before paint() ever ran.
        md = colorize_evidence_markdown(md)
        sys.stdout.write(md)
        if diff_record is not None:
            sys.stdout.write(_render_evidence_diff_markdown(diff_record))
    else:
        debug = bool(getattr(args, "debug", False))
        if diff_record is not None:
            payload = {
                "evidence": project_evidence_json(bundle, debug=debug),
                "diff": to_jsonable(diff_record),
            }
            sys.stdout.write(
                json.dumps(
                    payload, indent=2, ensure_ascii=False,
                ),
            )
            sys.stdout.write("\n")
        else:
            if debug:
                sys.stdout.write(
                    json.dumps(bundle.body, indent=2, ensure_ascii=False),
                )
            else:
                sys.stdout.write(format_evidence(bundle, fmt="json"))
            sys.stdout.write("\n")
    return 0


def _render_evidence_diff_markdown(record) -> str:  # noqa: ANN001
    """Append a ``## Diff`` section to the evidence markdown.

    Three branches mirror :func:`cli._formatters.format_run_diff`:

    - artifact absent → italic placeholder, no body.
    - matched-empty path filter → italic ``record.message``, no stat table.
    - normal case → stat-style preface line for every file, then the
      already-rendered ``record.content``. Truncation appends a footer
      using ``record.max_bytes``.
    """
    lines: list[str] = ["\n", "## Diff\n", "\n"]
    if not record.found:
        lines.append("_No diff artifact recorded._\n")
        return "".join(lines)
    if not record.files:
        msg = record.message or "No diff entries matched the filter."
        lines.append(f"_{msg}_\n")
        return "".join(lines)

    lines.append("```\n")
    for f in record.files:
        lines.append(f"{f.path} | +{f.added} -{f.removed}\n")
    lines.append("```\n")
    lines.append("\n")
    body = record.content
    if not body.endswith("\n"):
        body += "\n"
    lines.append("```diff\n")
    lines.append(body)
    lines.append("```\n")
    if record.truncated:
        lines.append(
            f"\n_... output truncated at {record.max_bytes} bytes ..._\n",
        )
    return "".join(lines)


def cmd_repair_state(args: argparse.Namespace) -> int:
    """Inspect and (with --apply) safely apply known run-state repairs.

    Resolves the run via ``find_run``, reads the current status from
    ``meta.json``, and delegates the actual diagnosis/mutation to
    ``repair_run_state(action='safe', apply=...)`` — the CLI owns no repair
    policy. ``--json`` prints a single JSON object on stdout; otherwise a
    human-readable block (always including the current status). Dry-run and
    refusal write nothing; a second ``--apply`` is an idempotent no-op.

    Error mapping (no traceback on any branch): unknown run id / unresolved
    workspace (``OrchoError``) map through ``format_error`` + ``exit_code``;
    an unsupported action (``ValueError``) exits 2; a failed write
    (``RuntimeError``) exits 1. In ``--json`` mode these go to stderr and no
    JSON is emitted.
    """
    want_json = bool(getattr(args, "json", False))
    apply_requested = bool(getattr(args, "apply", False))
    try:
        ref = find_run(args.run_id, workspace=getattr(args, "workspace", None))
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code

    current_status = load_meta(ref.run_dir).get("status")

    try:
        report = repair_run_state(ref.run_dir, action="safe", apply=apply_requested)
    except ValueError as exc:
        print(f"repair-state: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"repair-state: {exc}", file=sys.stderr)
        return 1

    if want_json:
        sys.stdout.write(
            json.dumps(
                repair_report_to_json(
                    report, run_id=ref.run_id, apply_requested=apply_requested
                ),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        )
    else:
        print(
            format_repair_report(
                report,
                run_id=ref.run_id,
                current_status=current_status,
                apply_requested=apply_requested,
            )
        )
    return 0


def _emit_delivery_setup_hints(result, project_group_root: str) -> None:
    """Best-effort: print one delivery setup hint after ``workspace init``.

    Gathers candidate project directories from the init ``result`` (detected +
    interactively-confirmed projects, plus the group root itself when it is a
    git repo), asks the provider-neutral
    :func:`~pipeline.engine.delivery_publish.collect_delivery_setup_hints`
    helper for setup advice, and prints the first non-empty hint once.

    The CLI carries no provider knowledge: all remote detection and hint
    wording live behind the helper. Every step is wrapped so a detection
    failure prints nothing and never disturbs the init exit code — this is a
    courtesy nudge, not part of the init contract. It only reads, so it is safe
    on ``--dry-run`` where it surfaces the same advice in the preview.
    """
    try:
        from pipeline.engine.delivery_publish import collect_delivery_setup_hints

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if path and path not in seen:
                seen.add(path)
                candidates.append(path)

        for proj in getattr(result, "detected_projects", ()):
            _add(getattr(proj, "path", ""))
        for proj in getattr(result, "extra_projects", ()):
            _add(getattr(proj, "path", ""))
        if project_group_root and (Path(project_group_root) / ".git").exists():
            _add(project_group_root)

        for path in candidates:
            hints = collect_delivery_setup_hints(Path(path))
            if hints:
                print(f"\nDelivery setup:\n  {hints[0]}")
                return
    except Exception:  # noqa: BLE001 — a hint must never break workspace init
        return


def cmd_workspace_init(args: argparse.Namespace) -> int:
    """Bootstrap an Orcho workspace under a project-group directory."""
    from sdk.workspace import (
        discover_undetected_candidates,
        preflight_workspace_target,
    )

    project_group_root = args.project_group_root or os.getcwd()
    no_interactive = bool(getattr(args, "no_interactive", False))
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))
    no_scaffold = bool(getattr(args, "no_scaffold", False))

    # Phase 0 (read-only): reject invalid targets (filesystem root, $HOME,
    # single repo-root) BEFORE any interactive discovery/prompt, so we never
    # mutate a child (e.g. `git init`) on a target we would ultimately refuse.
    try:
        preflight_workspace_target(project_group_root, force=force)
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code

    # Phase 1 (read-only): discover undetected folders.
    candidates = discover_undetected_candidates(project_group_root)

    # Phase 2: interactive prompt (TTY + not --no-interactive + not dry-run).
    extra_projects: list = []
    interactive = False
    if candidates and not no_interactive and not dry_run:
        from pipeline.project.project_discovery_prompt import prompt_for_extra_projects

        if getattr(sys.stdin, "isatty", lambda: False)():
            interactive = True
            extra_projects = prompt_for_extra_projects(candidates)

    try:
        result = init_workspace(
            project_group_root,
            workspace_name=getattr(args, "workspace_name", None),
            mcp_config=getattr(args, "mcp_config", None),
            mcp_server_name=getattr(args, "mcp_server_name", None),
            orcho_mcp_command=getattr(args, "orcho_mcp_command", "orcho-mcp"),
            force=force,
            dry_run=dry_run,
            extra_projects=extra_projects,
            undetected_count=len(candidates) - len(extra_projects),
            interactive=interactive,
            no_scaffold=no_scaffold,
        )
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code
    print(format_workspace_init(result))
    _emit_delivery_setup_hints(result, project_group_root)
    return 0


def cmd_workspace_fine_tune(args: argparse.Namespace) -> int:
    """Inspect a project and print a candidate verification contract.

    Stage 2 is pure-read: nothing is created or modified, with or without
    ``--dry-run``. The handler prints the proposed ``verification_envs`` /
    commands and a deferred-materialisation note, then exits 0.
    """
    project_dir = args.project_dir or os.getcwd()
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        result = fine_tune_project(project_dir, dry_run=dry_run)
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code
    print(format_fine_tune(result))
    return 0


def cmd_verify_env(args: argparse.Namespace) -> int:
    """Execute declared env-assertions for a verification_env.

    Resolves the run, proves it belongs to the project, runs the selected
    env's assertions from the declared checkout cwd, and writes an
    env-receipt. Resolution errors (project↔run mismatch, missing contract
    / env) map through ``format_error`` + ``exit_code`` with no receipt
    written; a passing run exits 0, a failing assertion set exits 1.
    """
    try:
        result = verify_env(
            project=getattr(args, "project", None),
            env=getattr(args, "env", None),
            run_id=getattr(args, "run_id", None),
            workspace=getattr(args, "workspace", None),
        )
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code
    print(format_verify_env(result))
    return 0 if result.all_passed else 1


def cmd_verify_list(args: argparse.Namespace) -> int:
    """List declared verification commands with placeholder-resolved run text.

    Pure projection: resolves the run/project/contract and prints each declared
    command (name, env, required marker, resolved run text). Executes nothing
    and writes nothing. Resolution errors map through ``format_error`` +
    ``exit_code``; success exits 0.
    """
    try:
        result = verify_list(
            project=getattr(args, "project", None),
            run_id=getattr(args, "run_id", None),
            workspace=getattr(args, "workspace", None),
        )
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code
    print(format_verify_list(result))
    return 0


def cmd_verify_run(args: argparse.Namespace) -> int:
    """Execute declared verification commands and persist a receipt each.

    No ``--env``: each command's env is its declared env. With ``--required``
    only the contract's required set runs; positional ``names`` select explicit
    declared commands; otherwise normal declared commands run, excluding
    manual/operator opt-in commands unless ``--include-manual`` is passed.
    Resolution errors (mismatch, missing contract, unknown command, empty
    required) exit 2 with nothing written; a command exiting non-zero exits 1;
    all-pass exits 0.

    ``--name`` is a thin alias for a single positional command: it resolves to
    ``commands=[name]`` and is rejected when combined with a positional command
    or ``--required``, so the two forms can never silently diverge.
    """
    name = getattr(args, "name", None)
    positional = args.names
    required = args.required
    if name is not None and positional:
        print(
            "verify run: use either a positional command or --name, not both",
            file=sys.stderr,
        )
        return 2
    if required and positional:
        print(
            "verify run: --required is incompatible with a positional command",
            file=sys.stderr,
        )
        return 2
    if required and name is not None:
        print(
            "verify run: --required is incompatible with --name",
            file=sys.stderr,
        )
        return 2
    if name is not None and not name.strip():
        print("verify run: --name must not be empty", file=sys.stderr)
        return 2
    commands = [name] if name is not None else (positional or None)
    try:
        result = verify_run(
            project=getattr(args, "project", None),
            run_id=getattr(args, "run_id", None),
            workspace=getattr(args, "workspace", None),
            commands=commands,
            required_only=required,
            include_manual=bool(getattr(args, "include_manual", False)),
        )
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code
    print(format_verify_run(result))
    return 0 if result.all_passed else 1


def cmd_pricing_refresh(args: argparse.Namespace) -> int:
    """Refresh ``~/.orcho/pricing.local.toml`` from a public pricing source."""
    print(f"  Fetching pricing for provider={args.provider!r} …")
    # Pre-write summary mirrors the legacy two-step flow: print parsed
    # models *before* writing, so the user can compare against the page.
    try:
        from core.observability import pricing_scrapers as _scrapers
        scrape = _scrapers.refresh(args.provider)
    except _scrapers.PricingScrapeError as exc:
        print(
            f"  ✗ Scrape failed: {exc}\n"
            "    Until orcho's scraper is updated, hand-edit\n"
            "    ~/.orcho/pricing.local.toml with rates from the\n"
            "    public pricing page.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            f"  ✗ Fetch failed: {exc}\n"
            "    Hand-edit ~/.orcho/pricing.local.toml or retry when\n"
            "    the network is available.",
            file=sys.stderr,
        )
        return 1

    print(format_pricing_refresh_models(scrape.models, scrape.provenance))

    if getattr(args, "dry_run", False):
        print("  --dry-run: not writing.")
        return 0

    try:
        result = refresh_pricing(args.provider, dry_run=False)
    except PricingFetchError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code

    print(format_pricing_refresh_written(result))
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# prompts — list view + resolution view (mixed; not pure read_cli)
# ─────────────────────────────────────────────────────────────────────────────


def cmd_prompts(args: argparse.Namespace) -> int:
    name = getattr(args, "name", None)
    project = getattr(args, "project", None)
    list_all = bool(getattr(args, "list", False))

    if list_all or not name:
        names = list(list_prompts())
        if project:
            names.extend(_prompt_loader.list_project_prompts(project))
            names.extend(_prompt_loader.list_workspace_prompts(project))
        names = sorted(set(names))

        winners: dict[str, str] = {}
        for n in names:
            chain = _prompt_loader.resolution_chain(n, project_dir=project)
            winner = next((lvl for lvl, _, ex in chain if ex), "unknown")
            winners[n] = winner

        print(format_prompts_list(names, project_dir=project, winners=winners))
        return 0

    try:
        res = resolve_prompt(name, project_dir=project)
    except PromptNotFound:
        # Render the chain anyway so the user sees what was tried, then
        # exit 1 — preserves the legacy CLI behaviour.
        chain = _prompt_loader.resolution_chain(name, project_dir=project)
        print()
        print(f"  Resolution chain for: '{name}'")
        if project:
            print(f"  Project: {project}\n")
        else:
            print()
        for level, path, exists in chain:
            icon = "✅" if exists else "○ "
            label = "(ACTIVE)" if exists else "(not found)"
            print(f"  {icon}  [{level:10s}]  {path}  {label}")
        print()
        print(f"  ✗ No template found for '{name}'")
        print()
        return 1

    print(
        format_prompts_resolution(
            res,
            project_dir=project,
            verbose=bool(getattr(args, "verbose", False)),
        )
    )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orcho",
        description=TAGLINE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=QUICK_HELP,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_version_string(),
        help="Show installed orcho package versions and exit",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_run = sub.add_parser(
        "run",
        help="Run single-project pipeline",
        description=(
            "Run one project as an inspectable AI software delivery workflow.\n\n"
            "Flow: plan -> implement -> review/repair -> final QA -> evidence."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=RUN_EPILOG,
    )
    _add_common_run_args(p_run)
    run_project = p_run.add_argument_group("Project")
    # Required for fresh runs; optional for ``--resume`` which resolves
    # the project from the persisted ``meta.json``. The orchestrator
    # validates the effective project after parse-time merge.
    run_project.add_argument(
        "--project", "-p", default=None,
        help="Project directory to run against (optional on --resume).",
    )
    run_profile = p_run.add_argument_group("Execution profile")
    run_profile.add_argument(
        "--session-mode", default="auto",
        choices=["auto", "stateless", "chain", "hybrid"],
        help="How implementation and repair phases share agent session state.",
    )
    # Verification strictness override (T6). Separate from the cross
    # ``--mode {full,plan}`` slice selector (which lives only on ``orcho
    # cross`` below): this mono flag selects the run's ``work_mode`` and wins
    # over the profile's projected ``default_mode``. Threaded to the run via
    # the ``ORCHO_WORK_MODE`` env in :func:`cmd_run`, never as cross_mode.
    run_profile.add_argument(
        "--mode", default=None,
        choices=["fast", "pro", "governed"],
        help=(
            "Verification strictness for this run (fast / pro / governed). "
            "Overrides the profile's default mode; omitted means use the "
            "profile default (e.g. feature -> fast, complex_feature -> pro). "
            "governed is opt-in only, never a built-in default."
        ),
    )
    run_profile.add_argument(
        "--profile", default=None,
        help=(
            "Work kind: auto-detect, feature, small_task, complex_feature, "
            "planning, delivery_audit, code_review, research, refactor, "
            "migration, or a custom installed profile. 'auto-detect' asks "
            "Orcho to recommend a work kind + mode (accept/override on a "
            "confirm-policy TTY; trusted threshold auto-select otherwise). "
            "On --resume / --from-run-plan, defaults to meta.profile "
            "(inherit); explicit --profile overrides and switches "
            "deliberately. On a fresh run without this flag, an interactive "
            "TTY shows a work-kind picker (with auto-detect as the first "
            "entry), while a non-interactive context (pipe, --no-interactive) "
            "requires --profile and errors out otherwise."
        ),
    )
    p_run.set_defaults(func=cmd_run)

    p_cross = sub.add_parser(
        "cross",
        help="Run cross-project pipeline",
        description=(
            "Keep one feature coherent across multiple projects.\n\n"
            "Flow: cross-plan -> per-project runs -> contract check -> evidence."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=CROSS_EPILOG,
    )
    _add_common_run_args(p_cross)
    cross_projects = p_cross.add_argument_group("Projects")
    # Required for fresh runs; optional for ``--resume`` which resolves
    # the project map from the persisted ``meta.json``. The orchestrator
    # validates the effective project map after parse-time merge.
    cross_projects.add_argument(
        "--projects", "-p", nargs="+", default=None,
        metavar="alias:/path",
        help=(
            "Project aliases and paths, e.g. api:./api web:./web "
            "(optional on --resume)."
        ),
    )
    cross_workflow = p_cross.add_argument_group("Cross workflow")
    cross_workflow.add_argument(
        "--mode", default="full",
        choices=["full", "plan"],
        help=(
            "full = run end-to-end; plan = stop after writing the plan "
            "(cross_plan.json canonical + cross_plan.md render)."
        ),
    )
    cross_workflow.add_argument(
        "--profile", default=None,
        help=(
            "Work kind: feature, small_task, complex_feature, planning, "
            "delivery_audit, code_review, research, refactor, migration, or "
            "a custom installed profile with cross policy. On --resume / "
            "--from-run-plan, defaults to meta.profile (inherit); explicit "
            "--profile overrides and switches deliberately. On a fresh run "
            "without this flag, an interactive TTY shows a picker of "
            "cross-eligible profiles, while a non-interactive context "
            "(pipe, --no-interactive) requires --profile and errors out "
            "otherwise."
        ),
    )
    cross_workflow.add_argument(
        "--plan-file", default=None,
        help=(
            "Pre-existing cross_plan.json (canonical; NOT the cross_plan.md "
            "render — the parser requires a single JSON object). Typically the "
            "cross_plan.json from a prior --mode plan run, optionally edited."
        ),
    )
    p_cross.set_defaults(func=cmd_cross)

    # ── status ────────────────────────────────────────────────────────────────
    p_status = sub.add_parser("status", help="Show status of a run")
    p_status.add_argument("run_id", nargs="?", default=None,
                          help="Run ID (default: last run)")
    p_status.add_argument("--verbose", "-v", action="store_true",
                          help="Show full session JSON data")
    p_status.add_argument(
        "--workspace", default=None,
        help="Override workspace dir (else $ORCHO_WORKSPACE / cwd walk-up)",
    )
    p_status.set_defaults(func=cmd_status)

    # ── metrics ───────────────────────────────────────────────────────────────
    p_metrics = sub.add_parser("metrics", help="Show token/time metrics")
    p_metrics.add_argument("run_id", nargs="?", default=None,
                           help="Run ID for single-run detail")
    p_metrics.add_argument("--last", "-n", type=int, default=10,
                           help="Show last N runs (default: 10)")
    p_metrics.add_argument(
        "--workspace", default=None,
        help="Override workspace dir (else $ORCHO_WORKSPACE / cwd walk-up)",
    )
    p_metrics.set_defaults(func=cmd_metrics)

    # ── history ───────────────────────────────────────────────────────────────
    p_hist = sub.add_parser("history", help="List recent runs")
    p_hist.add_argument("--last", "-n", type=int, default=10,
                        help="Show last N runs (default: 10)")
    p_hist.add_argument(
        "--workspace", default=None,
        help="Override workspace dir (else $ORCHO_WORKSPACE / cwd walk-up)",
    )
    p_hist.set_defaults(func=cmd_history)

    # ── evidence ──────────────────────────────────────────────────────────────
    p_evid = sub.add_parser(
        "evidence",
        help="Compose a run evidence bundle",
        description=(
            "Read the run dir and emit a v1 evidence bundle "
            "(plan + phases + gates + commands + artifacts + "
            "metrics + errors). Use --format md for the human "
            "summary, --out PATH to write both files to a directory."
        ),
    )
    p_evid.add_argument(
        "run_id", nargs="?", default=None,
        help="Run id (default: most recent run under runs/)",
    )
    p_evid.add_argument(
        "--format", "-f", choices=("json", "md"), default="json",
        help="Output format on stdout (default: json)",
    )
    p_evid.add_argument(
        "--out", type=Path, default=None,
        help=(
            "Directory to write evidence.json + evidence.md to "
            "(creates <out>/<run_id>/). When set, --format is ignored."
        ),
    )
    p_evid.add_argument(
        "--workspace", default=None,
        help="Override workspace dir (else $ORCHO_WORKSPACE / cwd walk-up)",
    )
    p_evid.add_argument(
        "--diff",
        nargs="?",
        const="preview",
        default=None,
        choices=("preview", "stat", "full"),
        help=(
            "Augment stdout output with the captured diff.patch artifact. "
            "Bare --diff defaults to preview. With --format json the output "
            "becomes a wrapper {\"evidence\": ..., \"diff\": ...}; --out is "
            "unchanged (disk bundle keeps the canonical raw schema)."
        ),
    )
    p_evid.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help=(
            "Include low-level diagnostic records in markdown/JSON stdout. "
            "Disk output is unchanged."
        ),
    )
    p_evid.set_defaults(func=cmd_evidence)

    # ── repair-state ────────────────────────────────────────────────────────────
    p_repair = sub.add_parser(
        "repair-state",
        help="Inspect and safely apply known run-state repairs",
        description=(
            "Diagnose a run's durable state against its event-derived "
            "projection and, with --apply, safely heal known torn shapes via "
            "the run-state repair API. Dry-run is the default (nothing is "
            "written). Refuses to flip an active, undecided handoff."
        ),
    )
    p_repair.add_argument("run_id", help="Run id to inspect / repair")
    p_repair.add_argument(
        "--apply", action="store_true", default=False,
        help="Apply the proposed repair (default: dry-run, nothing written)",
    )
    p_repair.add_argument(
        "--json", action="store_true", default=False,
        help="Emit a single JSON object on stdout instead of a text report",
    )
    p_repair.add_argument(
        "--workspace", default=None,
        help="Override workspace dir (else $ORCHO_WORKSPACE / cwd walk-up)",
    )
    p_repair.set_defaults(func=cmd_repair_state)

    # ── diff ──────────────────────────────────────────────────────────────────
    p_diff = sub.add_parser(
        "diff",
        help="Print the run's captured diff.patch artifact",
        description=(
            "Render the run's captured ``diff.patch`` artifact (written by "
            "the pipeline at run lifecycle time). The default is the raw "
            "unified patch; ``--preview`` shows a grouped Claude-style "
            "view, ``--stat`` shows a per-file +A -R table."
        ),
    )
    p_diff.add_argument(
        "run_id",
        help="Run id (required — diff viewing on the wrong run is a footgun)",
    )
    p_diff_mode = p_diff.add_mutually_exclusive_group()
    p_diff_mode.add_argument(
        "--full", dest="diff_mode", action="store_const", const="full",
        help="Raw unified patch (default).",
    )
    p_diff_mode.add_argument(
        "--preview", dest="diff_mode", action="store_const", const="preview",
        help="Claude-style grouped view with per-file +A -R headers.",
    )
    p_diff_mode.add_argument(
        "--stat", dest="diff_mode", action="store_const", const="stat",
        help="Per-file +A -R table only, no hunk content.",
    )
    p_diff.set_defaults(diff_mode="full")
    p_diff.add_argument(
        "--path", type=_nonempty_str, default=None, metavar="PATH",
        help=(
            "Restrict to files at this path. Exact match first; falls "
            "back to prefix match if no exact hit. Matches renames and "
            "deletes by either old or new name."
        ),
    )
    p_diff.add_argument(
        "--max-bytes", type=_positive_int, default=None, metavar="N",
        dest="max_bytes",
        help="Cap output to N bytes (UTF-8 safe). Default: unlimited.",
    )
    p_diff.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color for --preview / --stat output.",
    )
    p_diff.add_argument(
        "--workspace", default=None,
        help="Override workspace dir (else $ORCHO_WORKSPACE / cwd walk-up)",
    )
    p_diff.set_defaults(func=cmd_diff)

    # ── cost ──────────────────────────────────────────────────────────────────
    p_cost = sub.add_parser(
        "cost",
        help="API-equivalent cost report (sliding window over runs/)",
    )
    p_cost.add_argument(
        "--window", "-w", default="30d",
        help="Window: 30d / 7d / 24h / all (default: 30d)",
    )
    p_cost.add_argument(
        "--top", "-n", type=int, default=5,
        help="Show top-N most expensive runs (default: 5)",
    )
    p_cost.add_argument(
        "--workspace", default=None,
        help="Override workspace dir (else $ORCHO_WORKSPACE / cwd walk-up)",
    )
    p_cost.set_defaults(func=cmd_cost)

    # ── pricing ───────────────────────────────────────────────────────────────
    p_price = sub.add_parser(
        "pricing",
        help="Inspect / refresh pricing data used by ``orcho cost``",
    )
    p_price_sub = p_price.add_subparsers(dest="action", required=True)

    p_price_show = p_price_sub.add_parser(
        "show",
        help="Print the effective pricing table (user override + snapshot)",
    )
    p_price_show.set_defaults(func=cmd_pricing_show)

    p_price_refresh = p_price_sub.add_parser(
        "refresh",
        help="Scrape the public OpenAI pricing page and write "
             "~/.orcho/pricing.local.toml. User responsibility to verify.",
    )
    p_price_refresh.add_argument(
        "--provider", default="openai",
        choices=["openai", "pricepertoken"],
        help="Source: ``openai`` (developers.openai.com — official, "
             "Astro-rendered, fragile parser) or ``pricepertoken`` "
             "(pricepertoken.com — third-party aggregator, structured "
             "table, more reliable scrape but not authoritative)",
    )
    p_price_refresh.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be written without modifying the local file",
    )
    p_price_refresh.set_defaults(func=cmd_pricing_refresh)

    # ── profiles / workflows ─────────────────────────────────────────────────
    p_profiles = sub.add_parser(
        "profiles",
        help="List execution profiles",
    )
    p_profiles_sub = p_profiles.add_subparsers(dest="profiles_cmd", required=True)
    p_profiles_list = p_profiles_sub.add_parser(
        "list",
        help="List available execution profiles",
    )
    p_profiles_list.set_defaults(func=cmd_profiles_list)

    p_workflows = sub.add_parser(
        "workflows",
        help="List workflow profiles",
    )
    p_workflows_sub = p_workflows.add_subparsers(dest="workflows_cmd", required=True)
    p_workflows_list = p_workflows_sub.add_parser(
        "list",
        help="List available workflow profiles",
    )
    p_workflows_list.set_defaults(func=cmd_workflows_list)

    # ── prompts ───────────────────────────────────────────────────────────────
    p_prompts = sub.add_parser("prompts", help="Show prompt resolution chain")
    p_prompts.add_argument("name", nargs="?", default=None,
                           help="Prompt name (e.g. tasks/build)")
    p_prompts.add_argument("--project", default=None,
                           help="Project dir for override resolution")
    p_prompts.add_argument("--list", "-l", action="store_true",
                           help="List all available core prompts")
    p_prompts.add_argument("--verbose", "-v", action="store_true",
                           help="Print the contents of the resolved prompt template")
    p_prompts.set_defaults(func=cmd_prompts)

    # ── workspace ─────────────────────────────────────────────────────────────
    p_ws = sub.add_parser(
        "workspace",
        help="Manage Orcho workspaces",
        description=(
            "An Orcho workspace is a small folder that holds run state, "
            "an env-script, and (optionally) an MCP config snippet. "
            "Subcommand `init` is the user-facing bootstrap."
        ),
    )
    p_ws_sub = p_ws.add_subparsers(dest="workspace_cmd", required=True)

    p_ws_init = p_ws_sub.add_parser(
        "init",
        help="Initialise an Orcho workspace under a project-group directory",
        description=(
            "Create workspace-orchestrator/ under PROJECT_GROUP_ROOT with the "
            "runs directory, env script, and optional MCP config snippet. "
            "PROJECT_GROUP_ROOT should be a directory that holds one or more "
            "project repos (e.g. ~/www/my-org), not a single repo itself."
        ),
    )
    p_ws_init.add_argument(
        "project_group_root",
        nargs="?",
        default=None,
        help=(
            "Directory containing one or more project repos. Will be "
            "created if it doesn't exist. Defaults to the current "
            "working directory."
        ),
    )
    p_ws_init.add_argument(
        "--workspace-name", default=None,
        help=(
            "Logical name for the workspace; used as the default MCP "
            "server name suffix (orcho-<name>). Default: basename of "
            "project_group_root."
        ),
    )
    p_ws_init.add_argument(
        "--mcp-config", default=None, metavar="PATH",
        help=(
            "Optional path to a .mcp.json to create or merge into. When "
            "omitted, only the snippet is printed."
        ),
    )
    p_ws_init.add_argument(
        "--mcp-server-name", default=None, metavar="NAME",
        help=(
            "Server name to use in the MCP snippet/config. Default: "
            "orcho-<slug(basename(project_group_root))>."
        ),
    )
    p_ws_init.add_argument(
        "--orcho-mcp-command", default="orcho-mcp", metavar="CMD",
        help=(
            "Command/path the MCP host should run to launch the Orcho MCP "
            "server. Default: orcho-mcp (resolved on PATH)."
        ),
    )
    p_ws_init.add_argument(
        "--force", action="store_true",
        help=(
            "Allow initialising a target that itself looks like a project "
            "repo, and replace a conflicting MCP server entry in an "
            "existing .mcp.json."
        ),
    )
    p_ws_init.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be created/written without changing files.",
    )
    p_ws_init.add_argument(
        "--no-interactive", action="store_true",
        help=(
            "Skip interactive prompts for undetected folders. "
            "Use in CI or non-TTY contexts; the output will list "
            "how many folders were not auto-registered."
        ),
    )
    p_ws_init.add_argument(
        "--no-scaffold", action="store_true",
        help=(
            "Skip workspace extension-point scaffold files such as prompt "
            "override README files and the plugin template."
        ),
    )
    p_ws_init.set_defaults(func=cmd_workspace_init)

    p_ws_fine = p_ws_sub.add_parser(
        "fine-tune",
        help="Propose a verification contract from a project's shape",
        description=(
            "Inspect a project by its repo markers (pyproject.toml, "
            "package.json, composer.json, go.mod, Cargo.toml, *.sln, *.csproj) "
            "and print a candidate verification contract. If the directory is "
            "a workspace root, suggest child project roots. Stage 2 is "
            "read-only: no file is created or modified."
        ),
    )
    p_ws_fine.add_argument(
        "project_dir", nargs="?", default=None,
        help="Project to inspect. Defaults to the current working directory.",
    )
    p_ws_fine.add_argument(
        "--dry-run", action="store_true",
        help="Print the candidate contract without writing (the only Stage 2 mode).",
    )
    p_ws_fine.add_argument(
        "--workspace", "-w", default=None,
        help="Workspace directory (reserved; inspection is project-local).",
    )
    p_ws_fine.set_defaults(func=cmd_workspace_fine_tune)

    # ── web ───────────────────────────────────────────────────────────────────
    # Hidden from help until the interface package ships on PyPI (advertising it
    # would point users at an uninstallable ``pip install``). Still registered,
    # so it remains callable for anyone who already has the package.
    p_web = sub.add_parser("web", help=argparse.SUPPRESS)
    p_web.add_argument(
        "--port", "-p", type=int, default=8501,
        help="Порт для Streamlit (по умолчанию 8501)",
    )
    p_web.add_argument(
        "--headless", action="store_true",
        help="Run Streamlit without auto-opening a browser",
    )
    p_web.set_defaults(func=cmd_web)

    # ── tui ───────────────────────────────────────────────────────────────────
    # Hidden from help until the interface package ships on PyPI (see ``web``);
    # still registered so an installed package stays callable.
    p_tui = sub.add_parser("tui", help=argparse.SUPPRESS)
    p_tui.add_argument(
        "--run-id", help="Run id to open (resolved against the workspace)."
    )
    p_tui.add_argument(
        "--run-dir", help="Path to a run directory to open."
    )
    p_tui_mode = p_tui.add_mutually_exclusive_group()
    p_tui_mode.add_argument(
        "--follow", action="store_true", help="Follow a live run."
    )
    p_tui_mode.add_argument(
        "--replay", action="store_true", help="Replay a finished run."
    )
    p_tui.set_defaults(func=cmd_tui)

    # ── verify ────────────────────────────────────────────────────────────────
    p_verify = sub.add_parser(
        "verify",
        help="Execute declared verification-contract checks",
        description=(
            "Run the project's declared verification contract against a run. "
            "Subcommand `env` executes one verification_env's assertions and "
            "writes an env-receipt under the run directory."
        ),
    )
    p_verify_sub = p_verify.add_subparsers(dest="verify_cmd", required=True)
    p_verify_env = p_verify_sub.add_parser(
        "env",
        help="Execute a verification_env's declared assertions",
        description=(
            "Resolve a run, confirm it belongs to the project, then run the "
            "selected env's assertions from the declared checkout and persist "
            "an env-receipt under <run_dir>/verification_env_receipts/."
        ),
    )
    p_verify_env.add_argument(
        "--project", "-p", default=None,
        help=(
            "Project the run must belong to. When given, it is matched against "
            "the run's recorded project; on mismatch nothing is written."
        ),
    )
    p_verify_env.add_argument(
        "--env", default=None,
        help="verification_env to execute. Defaults to the contract's default_env.",
    )
    p_verify_env.add_argument(
        "--run-id", default=None,
        help="Run id whose directory receives the env-receipt. Default: newest run.",
    )
    p_verify_env.add_argument(
        "--workspace", "-w", default=None,
        help="Workspace/runs directory to resolve the run from.",
    )
    p_verify_env.set_defaults(func=cmd_verify_env)

    p_verify_list = p_verify_sub.add_parser(
        "list",
        help="List declared verification commands (resolved, not executed)",
        description=(
            "Resolve a run and print each declared verification command with "
            "its env, required marker, and placeholder-resolved run text. "
            "Executes nothing and writes no receipts."
        ),
    )
    p_verify_list.add_argument(
        "--project", "-p", default=None,
        help=(
            "Project the run must belong to. When given, it is matched against "
            "the run's recorded project."
        ),
    )
    p_verify_list.add_argument(
        "--run-id", default=None,
        help="Run id to resolve the contract checkout against. Default: newest run.",
    )
    p_verify_list.add_argument(
        "--workspace", "-w", default=None,
        help="Workspace/runs directory to resolve the run from.",
    )
    p_verify_list.set_defaults(func=cmd_verify_list)

    p_verify_run = p_verify_sub.add_parser(
        "run",
        help="Execute declared verification commands and persist receipts",
        description=(
            "Execute declared verification commands in the run worktree and "
            "persist one command-receipt each under "
            "<run_dir>/verification_command_receipts/. Each command's env is its "
            "declared env (there is no --env). With no names, normal declared "
            "commands run, excluding manual/operator opt-in commands; "
            "--include-manual restores the full declared-command sweep; "
            "--required runs exactly the contract's required set. --name is a "
            "synonym for a single positional command name."
        ),
    )
    p_verify_run.add_argument(
        "names", nargs="*",
        help=(
            "Declared command names to run. Omit to run normal declared commands "
            "except manual/operator opt-in commands."
        ),
    )
    p_verify_run.add_argument(
        "--name", default=None, metavar="NAME",
        help="Alias for a single positional command name; equivalent to `verify run NAME`.",
    )
    p_verify_run.add_argument(
        "--required", action="store_true",
        help="Run exactly the contract's required command set instead of names.",
    )
    p_verify_run.add_argument(
        "--include-manual", action="store_true",
        help=(
            "With no names, include manual_only/operator opt-in commands too. "
            "Explicit command names always run without this flag."
        ),
    )
    p_verify_run.add_argument(
        "--project", "-p", default=None,
        help=(
            "Project the run must belong to. When given, it is matched against "
            "the run's recorded project; on mismatch nothing is written."
        ),
    )
    p_verify_run.add_argument(
        "--run-id", default=None,
        help="Run id whose directory receives the command-receipts. Default: newest run.",
    )
    p_verify_run.add_argument(
        "--workspace", "-w", default=None,
        help="Workspace/runs directory to resolve the run from.",
    )
    p_verify_run.set_defaults(func=cmd_verify_run)

    # ── help ──────────────────────────────────────────────────────────────────
    def cmd_help(args: argparse.Namespace) -> int:
        if getattr(args, "verbose", False):
            parser.print_help()
            for title, names in iter_command_groups(sub.choices):
                print("\n" + "═" * 80)
                print(render_verbose_header(f"  {title.upper()}"))
                print("═" * 80 + "\n")
                for name in names:
                    print(f"[{name.upper()}]")
                    sub.choices[name].print_help()
                    print("\n" + "─" * 80 + "\n")
        else:
            print_quick_help()
        return 0

    p_help = sub.add_parser("help", help="Show this help message and exit")
    p_help.add_argument("--verbose", "-v", action="store_true", help="Show detailed help for all subcommands")
    p_help.set_defaults(func=cmd_help)

    return parser


def _add_common_run_args(p: argparse.ArgumentParser) -> None:
    """Add flags shared between `run` and `cross`."""
    # Task is required for fresh runs and optional for ``--resume``;
    # the validating handler resolves the effective task from
    # explicit flags or persisted ``meta.json``. Argparse cannot
    # express that conditional, so we accept either / neither here
    # and enforce the rule after parsing.
    task = p.add_argument_group("Task input")
    task_grp = task.add_mutually_exclusive_group(required=False)
    task_grp.add_argument("--task", "-t", help="Task description")
    task_grp.add_argument(
        "--task-file",
        help="Read task from .md file; bare NAME.md resolves from .orcho/.task-files",
    )

    decisions = p.add_argument_group("Operator decisions")
    decisions.add_argument(
        "--decision", action="append", default=None, metavar="TARGET=DECISION",
        help=(
            "Override an operator-decision target (e.g. "
            "contract_check=run). May repeat. Per-subcommand allowlist; "
            "unknown target or decision fails fast."
        ),
    )
    decisions.add_argument(
        "--decision-feedback", default=None, metavar="TEXT",
        help=(
            "Free-form feedback attached to a single --decision. "
            "Supplying with more than one --decision is an error."
        ),
    )
    decisions.add_argument(
        "--no-interactive", action="store_true",
        help=(
            "Never prompt on stdin; fall through to a resumable "
            "pending-decision state for non-interactive transports "
            "(MCP / CI / UI)."
        ),
    )

    control = p.add_argument_group("Run control")
    control.add_argument(
        "--max-rounds", type=int, default=1,
        help="Maximum implement/review/repair rounds per project (default: 1).",
    )
    control.add_argument(
        "--session-split",
        action="append",
        default=None,
        metavar="PHASE=SPLIT",
        help=(
            "Override a profile phase's prompt-session split for this run "
            "(split: stateless, per_phase, per_role, common). May repeat, "
            "e.g. --session-split implement=common."
        ),
    )
    control.add_argument("--dry-run", action="store_true", help="Print intent without running agents.")

    workspace = p.add_argument_group("Workspace and resume")
    workspace.add_argument(
        "--output-dir", default=None,
        help="Run output directory. Defaults to the active workspace run dir.",
    )
    workspace.add_argument(
        "--workspace", "-w", default=None,
        help="Workspace directory. Falls back to $ORCHO_WORKSPACE / cwd discovery.",
    )
    workspace.add_argument(
        "--resume", nargs="?", const="latest", default=None, metavar="RUN_ID",
        help=(
            "Resume an existing run by id, skipping phases already completed. "
            "Omit RUN_ID (bare --resume) or pass 'latest' to resume the most "
            "recent run in the active workspace."
        ),
    )
    workspace.add_argument(
        "--from-run-plan", default=None, metavar="RUN_ID_OR_DIR",
        help=(
            "Start a NEW run that inherits the parsed plan from a parent "
            "run. Accepts a bare run id or an explicit path; the parent "
            "must contain parsed_plan.json. The selected profile is "
            "projected to skip its leading plan + validate_plan block, "
            "so the child run starts at implement. Mutually exclusive "
            "with --resume."
        ),
    )

    mock = p.add_argument_group("Mock and testing")
    mock.add_argument(
        "--mock", action="store_true",
        help="Use mock agents: no real API calls.",
    )
    mock.add_argument(
        "--mock-validate-plan-reject", type=int, default=0, metavar="N",
        help=(
            "Mock-only: emit N rejected plan reviews before approving. "
            "Useful for testing manual approval flows."
        ),
    )
    mock.add_argument(
        "--no-worktree-isolation", action="store_true",
        help=(
            "Disable orcho-managed worktree isolation for this run; "
            "agent mutates the user's source checkout directly "
            "(legacy pre-GWT-1 behaviour)."
        ),
    )

    p.set_defaults(output=config.cli_output_mode())
    output = p.add_argument_group("Output")
    output.add_argument(
        "--output", choices=("summary", "live", "debug"), dest="output",
        help=(
            "Transcript mode: summary (default), live, or debug. "
            "Default is configurable via `cli.output_mode` in "
            "config.local.json or the ORCHO_OUTPUT_MODE env var. "
            "See the behavior matrix below."
        ),
    )
    output.add_argument(
        "--stream-output", action="store_const", const="live", dest="output",
        help="Alias for --output live.",
    )
    output.add_argument(
        "--verbose", "-v", action="store_const", const="debug", dest="output",
        help="Alias for --output debug.",
    )

    models = p.add_argument_group("Models and runtimes")
    models.add_argument(
        "--model", default=config.phase_model(
            "implement", "claude-opus-4-8[1m]",
        ),
        help="Default implementation model.",
    )
    models.add_argument("--model-plan", default=None, help="Override plan model.")
    models.add_argument("--model-implement", default=None, help="Override implement model.")
    models.add_argument(
        "--model-repair-changes", default=None,
        help="Override repair model.",
    )
    models.add_argument(
        "--model-review-changes", default=None,
        help="Override review model.",
    )
    models.add_argument(
        "--runtime-plan", default=None, choices=["claude", "codex", "gemini"],
        help="Override plan runtime.",
    )
    models.add_argument(
        "--runtime-implement", default=None,
        choices=["claude", "codex", "gemini"],
        help="Override implement runtime.",
    )
    models.add_argument(
        "--runtime-repair-changes", default=None,
        choices=["claude", "codex", "gemini"],
        help="Override repair runtime.",
    )
    models.add_argument(
        "--runtime-review-changes", default=None,
        choices=["claude", "codex", "gemini"],
        help="Override review runtime.",
    )

    attachments = p.add_argument_group("Attachments")
    attachments.add_argument(
        "--attach", action="append", default=None, metavar="PATH",
        help="File to attach as prompt context (kind auto-detected). May repeat.",
    )
    attachments.add_argument(
        "--attach-text", action="append", default=None, metavar="PATH",
        help="File to attach as TEXT (force kind regardless of extension).",
    )
    attachments.add_argument(
        "--attach-image", action="append", default=None, metavar="PATH",
        help="File to attach as IMAGE (.png/.jpg/etc). Runtime support may vary.",
    )
    attachments.add_argument(
        "--attach-binary", action="append", default=None, metavar="PATH",
        help="File to attach as BINARY (passthrough; runtime decides handling).",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    from core.io.encoding import ensure_utf8_stdio

    # Windows consoles default to a legacy code page that cannot encode the
    # emoji / box-drawing glyphs in Orcho's output; force UTF-8 before any
    # rendering so the CLI does not crash on the first non-ASCII line.
    ensure_utf8_stdio()

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        print_quick_help()
        sys.exit(0)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(func(args))


if __name__ == "__main__":
    main()
