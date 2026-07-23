"""sdk.run_control.launch — framework-neutral detached-run launch surface.

Public, synchronous, framework-neutral primitives for spawning, resuming,
and cancelling an Orcho pipeline as a *detached* subprocess. Any embedder
(an MCP server, a TUI, a plain CLI wrapper) can build its own concurrency,
reaping, and capacity policy on top of these calls without re-implementing
the spawn / resume / signal mechanics.

Design boundaries (deliberate, load-bearing):

- **Neutral by construction.** This module imports no ``asyncio``, no
  ``textual``, and nothing from ``orcho_mcp``. It is the mechanism; the
  embedder owns the policy. A unit guard asserts the neutrality via AST.
- **No in-memory registry.** ``cancel_run`` / ``resume_run`` are
  state-file driven: they read pid / pgid / status back from
  ``run_supervisor.json`` on disk, so an embedder that lost its handles
  (process restart) can still drive a live run.
- **Core owns spawn mechanics; the embedder owns concurrency.** There is
  no lock, no ``_reap`` task, and no ``_max_runs`` capacity gate here.
  Owners that need to reap can ``wait()`` on the returned ``Popen``.

The types mirror the shape of the reference MCP supervisor handle but
carry **no** ``Popen`` on the neutral :class:`LaunchedRun`; the live
process object rides only on :class:`LaunchResult.popen`, for owners that
reap through ``wait()``.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.observability.logging import normalize_output_mode
from pipeline.argv import build_orch_argv
from pipeline.control.continuation import ContinuationRequest
from pipeline.project.correction_followup import (
    compose_correction_context,
    compose_correction_task,
)
from sdk.errors import LaunchError, RunNotFound
from sdk.run_control.continuation import preflight_continuation
from sdk.runs import find_runs_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Neutral supervisor state file. Deliberately NOT ``mcp_supervisor.json``
#: — this is a fresh, framework-neutral artifact with no MCP coupling and
#: no back-compat shim.
STATE_FILE = "run_supervisor.json"

_TASK_FILES_DIR = Path(".orcho") / ".task-files"

# Statuses that mean "the pipeline finished" from cancel's point of view.
# Anchored to the pipeline's contract (``meta.json:status``), not to any
# in-memory handle, because meta is the authoritative completion signal.
# ``awaiting_phase_handoff`` is intentionally NOT included: it is a paused
# state (rc=4), not a finished one, so cancelling a paused run is a
# legitimate action rather than a no-op.
META_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"done", "failed", "halted", "interrupted", "orphaned"}
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchSpec:
    """Immutable description of a detached run to launch.

    ``project_dir`` is resolved once to an absolute path so the subprocess
    ``cwd`` and the ``--project`` argv flag agree; ``runs_dir`` /
    ``workspace`` locate the runs directory (walk-up disabled — the
    embedder sets context explicitly).
    """

    project_dir: str
    task: str | None = None
    task_file: str | None = None
    workspace: str | None = None
    runs_dir: str | None = None
    profile: str = "feature"
    mock: bool = False
    max_rounds: int | None = None
    mock_validate_plan_reject: int = 0
    output_mode: str = "summary"
    session_mode: str = "auto"
    attach: list[str] | None = None
    attach_text: list[str] | None = None
    attach_image: list[str] | None = None
    attach_binary: list[str] | None = None
    from_run_plan: str | None = None


@dataclass(frozen=True)
class CorrectionFollowupLaunchRequest:
    """Client-neutral request for an ordinary retained-change follow-up.

    No profile or runtime override is accepted: correction recovery always uses
    the fixed correction profile and the parent's retained worktree.
    """

    parent_run_id: str
    operator_comment: str
    runs_dir: str | None = None
    workspace: str | None = None
    output_mode: str = "summary"


@dataclass(frozen=True, slots=True)
class FromRunPlanLaunchRequest:
    """Request a fresh implementation child from a persisted parent plan."""

    parent_run_id: str
    runs_dir: str | None = None
    workspace: str | None = None
    profile: str = "feature"
    output_mode: str = "summary"


@dataclass(frozen=True)
class LaunchedRun:
    """Framework-neutral record of a launched run.

    Carries the durable facts of the spawn — no ``Popen``, no asyncio —
    so it can be serialised, handed across process boundaries, or wrapped
    by any embedder. The live process object rides only on
    :class:`LaunchResult`.
    """

    run_id: str
    pid: int
    pgid: int
    run_dir: Path
    project_dir: str
    command: list[str]
    started_at: str
    mock: bool
    output_mode: str
    status: str = "running"


@dataclass(frozen=True)
class LaunchResult:
    """Result of :func:`launch_run` / :func:`resume_run`.

    ``run`` is the neutral, serialisable record; ``popen`` is the live
    process handle for owners that reap through ``Popen.wait()``.
    """

    run: LaunchedRun
    popen: subprocess.Popen = field(repr=False)


@dataclass(frozen=True)
class CancelResult:
    """Result of :func:`cancel_run`.

    ``status`` is one of ``signal_sent(<mode>)``, ``already_done``, or
    ``already_dead``.
    """

    run_id: str
    status: str


# ---------------------------------------------------------------------------
# Time / process / state helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """ISO-8601 in UTC, e.g. ``2026-05-06T14:30:22.123Z``."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def is_pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is alive (or exists with a different uid)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but belongs to another user — treat as alive.
        return True


def _mint_run_id() -> str:
    """``YYYYMMDD_HHMMSS_xxxxxx`` — ts + 6 hex chars for collision safety."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:6]
    return f"{ts}_{token}"


def _dump_state(run_dir: Path, payload: dict[str, Any]) -> None:
    (run_dir / STATE_FILE).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _launch_env(run_id: str) -> dict[str, str]:
    """Build a detached-launch environment without profile override leakage.

    SDK launches always carry their effective profile explicitly in argv.
    ``ORCHO_PIPELINE`` remains a direct-CLI A/B surface, but an ambient value
    must not displace the profile chosen by an embedder or leak into project
    verification subprocesses spawned by the run.
    """
    env = os.environ.copy()
    env["ORCHO_RUN_ID"] = run_id
    env.pop("ORCHO_PIPELINE", None)
    return env


def write_launch_state(run: LaunchedRun) -> None:
    """Persist (or update) ``<run_dir>/run_supervisor.json``.

    Records the spawn facts an embedder needs to re-attach, cancel, or
    reason about the run after losing its in-memory handle.
    """
    payload: dict[str, Any] = {
        "run_id": run.run_id,
        "pid": run.pid,
        "pgid": run.pgid,
        "command": run.command,
        "project_dir": run.project_dir,
        "started_at": run.started_at,
        "status": run.status,
        "mock": run.mock,
        "output_mode": run.output_mode,
    }
    _dump_state(run.run_dir, payload)


def read_launch_state(run_dir: Path) -> dict[str, Any] | None:
    """Read ``run_supervisor.json`` for ``run_dir``, tolerant of IO errors."""
    state_path = run_dir / STATE_FILE
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_meta(run_dir: Path) -> dict[str, Any] | None:
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_meta_task(run_dir: Path) -> str | None:
    """Return ``meta.json:task`` for ``run_dir`` or None.

    Resume validates that a task was recorded before classifying the
    spawn as a checkpoint continuation.
    """
    meta = _read_meta(run_dir)
    if meta is None:
        return None
    task = meta.get("task")
    return task if isinstance(task, str) and task else None


def read_meta_profile(run_dir: Path) -> str | None:
    """Return ``meta.json:profile`` for ``run_dir`` or None."""
    meta = _read_meta(run_dir)
    if meta is None:
        return None
    profile = meta.get("profile")
    return profile if isinstance(profile, str) and profile.strip() else None


def meta_status_is_terminal(run_dir: Path) -> bool:
    """Return True iff ``meta.json:status`` reports a finished run.

    Missing / malformed meta returns False so callers fall back to a
    liveness probe; never asserts terminality on absent evidence.
    ``awaiting_phase_handoff`` is paused, not finished — see
    :data:`META_TERMINAL_STATUSES`.
    """
    meta = _read_meta(run_dir)
    if meta is None:
        return False
    status = meta.get("status")
    return isinstance(status, str) and status in META_TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------


def _resolve_project_dir(project_dir: str) -> str:
    """Resolve ``project_dir`` to an absolute path once.

    The caller passes this to ``subprocess.Popen`` as both ``cwd=`` and
    (via ``--project``) to the orchestrator argv. Resolving once here
    keeps cwd and ``--project`` on the same absolute path and avoids the
    workspace-relative segment doubling regression.
    """
    if not project_dir or not project_dir.strip():
        raise LaunchError("project_dir is required and must be a non-empty path")
    resolved = Path(project_dir).expanduser().resolve()
    if not resolved.is_dir():
        raise LaunchError(
            f"project_dir does not exist or is not a directory: "
            f"{project_dir!r} (resolved to {resolved})"
        )
    return str(resolved)


def _workspace_from_runs_dir(runs_dir: Path) -> str:
    """Return the workspace directory for ``<workspace>/runspace/runs``."""
    return str(runs_dir.parent.parent)


def _task_file_lookup_dirs(project_dir: Path) -> tuple[Path, ...]:
    return tuple(
        dict.fromkeys(
            ancestor / _TASK_FILES_DIR
            for ancestor in (project_dir, *project_dir.parents)
        )
    )


def _resolve_task_file(task_file: str | None, *, project_dir: str) -> str | None:
    """Resolve ``task_file`` before spawning a pipeline subprocess.

    Mirrors the core CLI's short-name convention: a bare ``*.md`` name is
    looked up under the reserved ``.orcho/.task-files`` directories;
    anything else resolves relative to ``project_dir``.
    """
    if task_file is None:
        return None
    if not task_file.strip():
        raise LaunchError("task_file is required and must be a non-empty path")

    project_path = Path(project_dir).expanduser().resolve()
    path = Path(task_file).expanduser()
    resolved = path if path.is_absolute() else (project_path / path)

    if (
        not path.is_absolute()
        and path.parent == Path(".")
        and path.suffix.lower() == ".md"
    ):
        for task_dir in _task_file_lookup_dirs(project_path):
            candidate = task_dir / path.name
            if candidate.is_file():
                return str(candidate.resolve())

    if resolved.is_file():
        return str(resolved.resolve())

    if resolved.exists() and not resolved.is_file():
        raise LaunchError(f"task_file is not a file: {resolved.resolve()}")

    raise LaunchError(f"task_file not found: {resolved}")


# ---------------------------------------------------------------------------
# Spawn helper
# ---------------------------------------------------------------------------


def _spawn_detached(
    cmd: list[str],
    *,
    project_dir: str,
    env: dict[str, str],
    log_fd: Any,
) -> subprocess.Popen:
    """Launch ``cmd`` detached in its own session.

    ``start_new_session=True`` makes the child a session leader so its
    pgid equals its pid and ``killpg`` reaches the whole process tree.
    Raises :class:`LaunchError` on any spawn failure.
    """
    try:
        return subprocess.Popen(
            cmd,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            cwd=project_dir,
            env=env,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as e:
        raise LaunchError(f"failed to spawn pipeline subprocess: {e}") from e


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def launch_run(spec: LaunchSpec, *, run_id: str | None = None) -> LaunchResult:
    """Spawn a new detached pipeline subprocess. Returns immediately.

    Mints a run id (unless supplied), builds the orchestrator argv, and
    launches ``python -m pipeline.project_orchestrator`` in its own
    session with a ``run_supervisor.json`` recording the spawn facts. No
    lock, no reaping, no capacity gate — those are the embedder's policy.

    Raises:
        LaunchError: on spawn failure (OSError / FileNotFoundError) or
            an invalid ``project_dir`` / ``task_file``.
        NoWorkspace: when the runs directory cannot be resolved.
    """
    project_dir = _resolve_project_dir(spec.project_dir)
    task_file = _resolve_task_file(spec.task_file, project_dir=project_dir)
    runs_dir = find_runs_dir(
        workspace=spec.workspace, runs_dir=spec.runs_dir, cwd=None
    )
    output_mode = normalize_output_mode(spec.output_mode)

    run_id = run_id or _mint_run_id()
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)

    argv = build_orch_argv(
        project=project_dir,
        task=spec.task,
        task_file=task_file,
        workspace=_workspace_from_runs_dir(runs_dir),
        run_id=run_id,
        output_dir=str(run_dir),
        mock=spec.mock,
        max_rounds=spec.max_rounds,
        mock_validate_plan_reject=spec.mock_validate_plan_reject,
        output_mode=output_mode,
        session_mode=spec.session_mode,
        profile=spec.profile,
        attach=spec.attach,
        attach_text=spec.attach_text,
        attach_image=spec.attach_image,
        attach_binary=spec.attach_binary,
        from_run_plan=spec.from_run_plan,
    )
    cmd = [sys.executable, "-m", "pipeline.project_orchestrator", *argv]

    env = _launch_env(run_id)

    log_fd = (run_dir / "runner.log").open("w", encoding="utf-8")
    popen = _spawn_detached(cmd, project_dir=project_dir, env=env, log_fd=log_fd)

    run = LaunchedRun(
        run_id=run_id,
        pid=popen.pid,
        pgid=popen.pid,
        run_dir=run_dir,
        project_dir=project_dir,
        command=cmd,
        started_at=now_iso(),
        mock=spec.mock,
        output_mode=output_mode,
    )
    write_launch_state(run)
    return LaunchResult(run=run, popen=popen)


def resume_run(
    run_id: str, *, runs_dir: str | None = None, profile: str | None = None
) -> LaunchResult:
    """Continue an existing run from its checkpoint via ``--resume``.

    Inherits ``mock`` / ``output_mode`` from the persisted state so a
    paused mock run does not silently switch providers. Profile resolves
    explicit → ``meta.profile`` → ``"feature"``. ``--task`` is
    deliberately omitted so core classifies the spawn as a CHECKPOINT
    continuation (re-using the existing run dir) rather than a follow-up.

    Raises:
        RunNotFound: no run dir, no state file, missing ``project_dir``,
            or ``meta.json`` missing the recorded ``task``.
        LaunchError: on spawn failure.
    """
    rd = find_runs_dir(runs_dir=runs_dir, cwd=None)
    run_dir = rd / run_id
    if not run_dir.is_dir():
        raise RunNotFound(f"run not found: {run_id} (in {rd})")

    preflight = preflight_continuation(
        ContinuationRequest(run_id=run_id, intent="resume"), parent_run_dir=run_dir,
    )
    if preflight.resolution.blocker:
        raise LaunchError(f"resume cannot start: {preflight.resolution.blocker}")

    state = read_launch_state(run_dir)
    if state is None:
        raise RunNotFound(
            f"run {run_id}: no {STATE_FILE} — cannot resume"
        )
    project_dir = state.get("project_dir")
    if not project_dir:
        raise RunNotFound(f"run {run_id}: state file missing project_dir")

    # Core's resume falls back to ``meta.task`` when ``--task`` is absent;
    # validate it exists so we surface a structured error when the meta was
    # never written (run killed before initial meta.json).
    if not read_meta_task(run_dir):
        raise RunNotFound(
            f"run {run_id}: meta.json missing 'task' — resume cannot "
            "synthesise the orchestrator argv."
        )

    if profile is None or not profile.strip():
        effective_profile = read_meta_profile(run_dir) or "feature"
    else:
        effective_profile = profile

    original_mock = bool(state.get("mock", False))
    try:
        original_output_mode = normalize_output_mode(
            state.get("output_mode") or "summary"
        )
    except ValueError:
        original_output_mode = "summary"

    argv = build_orch_argv(
        project=project_dir,
        workspace=_workspace_from_runs_dir(rd),
        resume=run_id,
        run_id=run_id,
        output_dir=str(run_dir),
        profile=effective_profile,
        mock=original_mock,
        output_mode=original_output_mode,
    )
    cmd = [sys.executable, "-m", "pipeline.project_orchestrator", *argv]

    env = _launch_env(run_id)

    log_fd = (run_dir / "runner.log").open("a", encoding="utf-8")
    log_fd.write(f"\n=== resume @ {now_iso()} ===\n")
    log_fd.flush()
    popen = _spawn_detached(cmd, project_dir=project_dir, env=env, log_fd=log_fd)

    run = LaunchedRun(
        run_id=run_id,
        pid=popen.pid,
        pgid=popen.pid,
        run_dir=run_dir,
        project_dir=project_dir,
        command=cmd,
        started_at=now_iso(),
        mock=original_mock,
        output_mode=original_output_mode,
    )
    write_launch_state(run)
    return LaunchResult(run=run, popen=popen)


def launch_correction_followup(
    request: CorrectionFollowupLaunchRequest, *, run_id: str | None = None,
) -> LaunchResult:
    """Spawn a correction child after rechecking the parent's durable state."""
    comment = request.operator_comment.strip()
    if not comment:
        raise LaunchError("operator_comment is required for a correction follow-up")
    runs_dir = find_runs_dir(
        workspace=request.workspace, runs_dir=request.runs_dir, cwd=None,
    )
    parent_dir = runs_dir / request.parent_run_id
    preflight = preflight_continuation(
        ContinuationRequest(
            run_id=request.parent_run_id, intent="followup", operator_comment=comment,
        ),
        parent_run_dir=parent_dir,
    )
    parent_meta = preflight.parent_meta
    if parent_meta is None:
        raise RunNotFound(f"run {request.parent_run_id}: meta.json is unavailable")
    if preflight.resolution.blocker:
        raise LaunchError(f"correction follow-up cannot start: {preflight.resolution.blocker}")
    project_dir = parent_meta.get("project")
    task = parent_meta.get("task")
    if not isinstance(project_dir, str) or not project_dir.strip():
        raise LaunchError("parent meta.json missing project")
    if not isinstance(task, str) or not task.strip():
        raise LaunchError("parent meta.json missing task")
    child_id = run_id or _mint_run_id()
    if child_id == request.parent_run_id:
        raise LaunchError("follow-up child run_id must differ from parent_run_id")
    child_dir = runs_dir / child_id
    if child_dir.exists():
        raise LaunchError(f"follow-up child run already exists: {child_id}")
    child_dir.mkdir(parents=True)
    context = child_dir / "correction_context.md"
    context.write_text(
        compose_correction_context(parent_meta)
        + "\n\n## Operator comment\n\n"
        + comment + "\n",
        encoding="utf-8",
    )
    correction_task = (
        compose_correction_task(parent_meta)
        + f"\n\nDetailed rejection context: {context}\n\nOperator comment: {comment}"
    )
    output_mode = normalize_output_mode(request.output_mode)
    argv = build_orch_argv(
        project=project_dir, task=correction_task,
        workspace=_workspace_from_runs_dir(runs_dir), resume=request.parent_run_id,
        run_id=child_id, output_dir=str(child_dir), profile="correction",
        output_mode=output_mode, no_interactive=True,
    )
    cmd = [sys.executable, "-m", "pipeline.project_orchestrator", *argv]
    env = _launch_env(child_id)
    log_fd = (child_dir / "runner.log").open("w", encoding="utf-8")
    popen = _spawn_detached(cmd, project_dir=project_dir, env=env, log_fd=log_fd)
    run = LaunchedRun(
        run_id=child_id, pid=popen.pid, pgid=popen.pid, run_dir=child_dir,
        project_dir=project_dir, command=cmd, started_at=now_iso(), mock=False,
        output_mode=output_mode,
    )
    write_launch_state(run)
    return LaunchResult(run=run, popen=popen)


def launch_from_run_plan(
    request: FromRunPlanLaunchRequest, *, run_id: str | None = None,
) -> LaunchResult:
    """Spawn a fresh child that consumes only a parent's parsed-plan artifact."""
    runs_dir = find_runs_dir(
        workspace=request.workspace, runs_dir=request.runs_dir, cwd=None,
    )
    parent_dir = runs_dir / request.parent_run_id
    preflight = preflight_continuation(
        ContinuationRequest(run_id=request.parent_run_id, intent="from_run_plan"),
        parent_run_dir=parent_dir,
    )
    parent_meta = preflight.parent_meta
    if parent_meta is None:
        raise RunNotFound(f"run {request.parent_run_id}: meta.json is unavailable")
    if preflight.resolution.blocker:
        raise LaunchError(f"from-run-plan cannot start: {preflight.resolution.blocker}")
    project_dir = parent_meta.get("project")
    task = parent_meta.get("task")
    if not isinstance(project_dir, str) or not project_dir.strip():
        raise LaunchError("parent meta.json missing project")
    if not isinstance(task, str) or not task.strip():
        raise LaunchError("parent meta.json missing task")
    child_id = run_id or _mint_run_id()
    if child_id == request.parent_run_id:
        raise LaunchError("from-run-plan child run_id must differ from parent_run_id")
    child_dir = runs_dir / child_id
    if child_dir.exists():
        raise LaunchError(f"from-run-plan child run already exists: {child_id}")
    child_dir.mkdir(parents=True)
    output_mode = normalize_output_mode(request.output_mode)
    argv = build_orch_argv(
        project=project_dir, task=task, workspace=_workspace_from_runs_dir(runs_dir),
        run_id=child_id, output_dir=str(child_dir), profile=request.profile,
        output_mode=output_mode, from_run_plan=request.parent_run_id,
    )
    cmd = [sys.executable, "-m", "pipeline.project_orchestrator", *argv]
    env = _launch_env(child_id)
    log_fd = (child_dir / "runner.log").open("w", encoding="utf-8")
    popen = _spawn_detached(cmd, project_dir=project_dir, env=env, log_fd=log_fd)
    run = LaunchedRun(
        run_id=child_id, pid=popen.pid, pgid=popen.pid, run_dir=child_dir,
        project_dir=project_dir, command=cmd, started_at=now_iso(), mock=False,
        output_mode=output_mode,
    )
    write_launch_state(run)
    return LaunchResult(run=run, popen=popen)


def cancel_run(
    run_id: str, *, runs_dir: str | None = None, mode: str = "graceful"
) -> CancelResult:
    """Send SIGTERM (``graceful``) or SIGKILL (``hard``) to a run's group.

    State-file driven: reads pid / pgid back from ``run_supervisor.json``,
    so it works even when the caller never held (or lost) the live
    ``Popen``. Idempotent — a terminal ``meta.json`` returns
    ``already_done``, a dead pid returns ``already_dead``. Never raises on
    a dead/finished run; only :class:`RunNotFound` (missing state) and
    ``ValueError`` (bad ``mode``) propagate.
    """
    if mode not in ("graceful", "hard"):
        raise ValueError(
            f"cancel mode must be 'graceful' or 'hard', got {mode!r}"
        )
    sig = signal.SIGTERM if mode == "graceful" else signal.SIGKILL

    rd = find_runs_dir(runs_dir=runs_dir, cwd=None)
    run_dir = rd / run_id
    state = read_launch_state(run_dir)
    if state is None:
        raise RunNotFound(f"run {run_id}: no {STATE_FILE}")

    # Pipeline truth first: a terminal meta.json means the run finished
    # even if the OS has not finalised the subprocess yet.
    if meta_status_is_terminal(run_dir):
        return CancelResult(run_id=run_id, status="already_done")

    pid = int(state.get("pid", 0))
    pgid = int(state.get("pgid", pid))

    if not is_pid_alive(pid):
        # Orphan path: dead pid but non-terminal meta. Overwrite the state
        # with a settled status so subsequent probes never see a still-running
        # run, then report already_dead. A live-launch state carries
        # status="running"; keeping it here would leave the neutral state file
        # claiming the run is active after cancel returned already_dead.
        state["status"] = "interrupted"
        if not state.get("halt_reason"):
            state["halt_reason"] = "interrupted_orphan"
        _dump_state(run_dir, state)
        return CancelResult(run_id=run_id, status="already_dead")

    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return CancelResult(run_id=run_id, status="already_dead")
    return CancelResult(run_id=run_id, status=f"signal_sent({mode})")


__all__ = [
    "CancelResult",
    "CorrectionFollowupLaunchRequest",
    "FromRunPlanLaunchRequest",
    "LaunchResult",
    "LaunchSpec",
    "LaunchedRun",
    "META_TERMINAL_STATUSES",
    "STATE_FILE",
    "cancel_run",
    "is_pid_alive",
    "launch_run",
    "launch_correction_followup",
    "launch_from_run_plan",
    "meta_status_is_terminal",
    "now_iso",
    "read_launch_state",
    "read_meta_profile",
    "read_meta_task",
    "resume_run",
    "write_launch_state",
]
