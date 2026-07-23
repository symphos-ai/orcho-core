"""Integration tests for the detached-launch SDK surface.

Exercises :func:`sdk.run_control.launch_run` / :func:`resume_run` /
:func:`cancel_run` against a **real** ``python -m
pipeline.project_orchestrator --mock`` subprocess — not an in-process
``run_pipeline`` call. The point is to prove the *detached* mechanics:

* ``launch_run`` spawns a session-leader subprocess that drives the mock
  pipeline to a terminal ``meta.json`` status and writes
  ``run_supervisor.json`` with the spawn facts;
* ``cancel_run`` on a **live** run observably signals the process group
  (``os.killpg`` on the pgid, reachable only because
  ``start_new_session=True``) and the process actually dies;
* ``cancel_run`` is idempotent on terminal / dead runs and on the orphan
  path (dead pid, non-terminal meta) — never raising;
* ``resume_run`` continues a checkpoint-paused mock run past its pause,
  inheriting ``mock`` / ``output_mode`` from the persisted state.

Isolation: each test uses a throwaway git repo as ``project_dir`` and a
throwaway ``<tmp>/runspace/runs`` as ``runs_dir`` (passed explicitly on
``LaunchSpec`` / the resume+cancel calls) so nothing leaks from the
ambient workspace. ``PYTHONPATH`` is pinned to this checkout so the
detached ``-m pipeline.project_orchestrator`` resolves the engine from
*here*, not from a stale installed copy (the known PYTHONPATH-leak trap).

Every live subprocess is force-killed in teardown (``os.killpg`` +
``SIGKILL``, ``ProcessLookupError`` suppressed) so a failing assertion
can never strand a detached process in CI.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from sdk.errors import LaunchError, RunNotFound
from sdk.phase_handoff import phase_handoff_decide
from sdk.run_control import (
    CancelResult,
    CorrectionFollowupLaunchRequest,
    FromRunPlanLaunchRequest,
    LaunchResult,
    LaunchSpec,
    cancel_run,
    launch_correction_followup,
    launch_from_run_plan,
    launch_run,
    resume_run,
)
from sdk.run_control.launch import is_pid_alive, meta_status_is_terminal

pytestmark = [
    pytest.mark.slow_process,
    pytest.mark.git_worktree,
    pytest.mark.serial,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

_TERMINAL = frozenset({"done", "failed", "halted", "interrupted", "orphaned"})


# ── fixtures / helpers ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _pin_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the detached subprocess imports the engine from this checkout.

    ``launch_run`` spawns ``[sys.executable, '-m',
    'pipeline.project_orchestrator']`` with ``cwd=project_dir`` (the temp
    repo) and ``env=os.environ.copy()``. Without pinning PYTHONPATH the
    ``-m`` import could resolve ``pipeline`` from a foreign installed copy.
    Prepending the checkout root makes the run hermetic to *this* tree.
    """
    existing = os.environ.get("PYTHONPATH", "")
    pinned = str(_REPO_ROOT) + (os.pathsep + existing if existing else "")
    monkeypatch.setenv("PYTHONPATH", pinned)


def _init_git_repo(path: Path) -> None:
    """Make ``path`` a committed git repo — the engine's worktree resolver
    hard-fails on a non-git ``project_dir``."""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "test@orcho.invalid"],
        ["git", "config", "user.name", "Orcho Test"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(cmd, cwd=path, check=True)
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(project_dir, runs_dir)`` — an isolated repo + runs tree."""
    project_dir = tmp_path / "proj"
    _init_git_repo(project_dir)
    runs_dir = tmp_path / "ws" / "runspace" / "runs"
    runs_dir.mkdir(parents=True)
    return project_dir, runs_dir


@pytest.fixture
def live_runs() -> Iterator[Callable[[LaunchResult], LaunchResult]]:
    """Register launched runs so teardown force-kills any survivor group.

    A test registers each :class:`LaunchResult`; on teardown every pgid is
    ``SIGKILL``-ed (best effort) and its ``Popen`` reaped, so an early
    assertion failure can never leave a detached subprocess alive.
    """
    tracked: list[LaunchResult] = []

    def _track(res: LaunchResult) -> LaunchResult:
        tracked.append(res)
        return res

    yield _track

    for res in tracked:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(res.run.pgid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            res.popen.wait(timeout=10)


def _read_status(run_dir: Path) -> str | None:
    meta = run_dir / "meta.json"
    if not meta.is_file():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("status")
    except (OSError, json.JSONDecodeError):
        return None


def _wait_for_terminal(res: LaunchResult, timeout: float = 90.0) -> str:
    """Wait until the run's ``meta.json`` reports a terminal status."""
    res.popen.wait(timeout=timeout)
    # meta is written just before exit; give the final write a beat to land.
    deadline = time.time() + 5.0
    status = _read_status(res.run.run_dir)
    while status not in _TERMINAL and time.time() < deadline:
        time.sleep(0.05)
        status = _read_status(res.run.run_dir)
    assert status in _TERMINAL, f"run did not reach terminal meta: {status!r}"
    return status


def _wait_until_running(res: LaunchResult, timeout: float = 30.0) -> None:
    """Block until the subprocess has written ``status='running'`` and its
    pid is alive — i.e. it is observably mid-run, not yet terminal."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if res.popen.poll() is not None:
            pytest.fail(
                f"subprocess exited (rc={res.popen.returncode}) before it "
                "could be caught running — cannot exercise the live-cancel "
                "path"
            )
        if (
            is_pid_alive(res.run.pid)
            and _read_status(res.run.run_dir) == "running"
            and not meta_status_is_terminal(res.run.run_dir)
        ):
            return
        time.sleep(0.02)
    pytest.fail("timed out waiting for the run to report status='running'")


def _spec(project_dir: Path, runs_dir: Path, **kw: object) -> LaunchSpec:
    return LaunchSpec(
        project_dir=str(project_dir),
        task="Add a hello() helper.",
        mock=True,
        runs_dir=str(runs_dir),
        **kw,  # type: ignore[arg-type]
    )


# ── tests ────────────────────────────────────────────────────────────────────


def test_launch(env: tuple[Path, Path], live_runs) -> None:
    """launch_run spawns a real detached mock run to a terminal status and
    writes run_supervisor.json with the spawn facts."""
    project_dir, runs_dir = env
    res = live_runs(launch_run(_spec(project_dir, runs_dir, max_rounds=1)))

    run = res.run
    # Handle facts are real.
    assert run.run_id and run.run_dir.name == run.run_id
    assert run.run_dir.is_dir()
    assert run.pid > 0
    assert run.pgid == run.pid  # session leader → pgid == pid
    assert run.command[:3] == [sys.executable, "-m", "pipeline.project_orchestrator"]
    assert res.popen.poll() is None  # process is alive right after spawn

    # run_supervisor.json carries the spawn facts.
    state = json.loads((run.run_dir / "run_supervisor.json").read_text())
    assert state["run_id"] == run.run_id
    assert state["pid"] == run.pid
    assert state["pgid"] == run.pgid
    assert state["project_dir"] == str(project_dir.resolve())
    assert state["command"] == run.command
    assert state["mock"] is True
    assert state["status"] == "running"

    # The detached run drives itself to a terminal status.
    assert _wait_for_terminal(res) == "done"


def test_cancel_live_signal(env: tuple[Path, Path], live_runs) -> None:
    """cancel_run on a LIVE detached run signals the process group and the
    process actually dies — the os.killpg(pgid) path is observably covered."""
    project_dir, runs_dir = env
    # No max_rounds cap → the mock run keeps working long enough to be
    # caught mid-flight and cancelled.
    res = live_runs(launch_run(_spec(project_dir, runs_dir)))
    _wait_until_running(res)

    result = cancel_run(res.run.run_id, runs_dir=str(runs_dir), mode="graceful")
    assert isinstance(result, CancelResult)

    if result.status == "signal_sent(graceful)":
        try:
            res.popen.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Mock ignored SIGTERM in this window — escalate to hard and
            # assert the kill path still terminates it.
            hard = cancel_run(res.run.run_id, runs_dir=str(runs_dir), mode="hard")
            assert hard.status == "signal_sent(hard)"
            res.popen.wait(timeout=10)
    else:  # pragma: no cover - defensive: run finished in the catch window
        pytest.fail(
            f"expected signal_sent(graceful) on a live run, got "
            f"{result.status!r}"
        )

    # The whole group is gone.
    assert res.popen.poll() is not None
    assert not is_pid_alive(res.run.pid)

    # Idempotent: cancelling an already-killed run does not raise and
    # reports a settled status.
    again = cancel_run(res.run.run_id, runs_dir=str(runs_dir), mode="graceful")
    assert again.status in {"already_dead", "already_done"}


def test_cancel_hard_live_signal(env: tuple[Path, Path], live_runs) -> None:
    """mode='hard' on a live run sends SIGKILL to the group and it dies."""
    project_dir, runs_dir = env
    res = live_runs(launch_run(_spec(project_dir, runs_dir)))
    _wait_until_running(res)

    result = cancel_run(res.run.run_id, runs_dir=str(runs_dir), mode="hard")
    assert result.status == "signal_sent(hard)"
    res.popen.wait(timeout=10)
    assert res.popen.poll() is not None
    assert not is_pid_alive(res.run.pid)


def test_cancel_idempotent(env: tuple[Path, Path], live_runs) -> None:
    """Cancelling a terminal run is idempotent in both modes: already_done,
    no exception."""
    project_dir, runs_dir = env
    res = live_runs(launch_run(_spec(project_dir, runs_dir, max_rounds=1)))
    assert _wait_for_terminal(res) == "done"

    graceful = cancel_run(res.run.run_id, runs_dir=str(runs_dir), mode="graceful")
    assert graceful.status == "already_done"
    hard = cancel_run(res.run.run_id, runs_dir=str(runs_dir), mode="hard")
    assert hard.status == "already_done"


def test_cancel_orphan(env: tuple[Path, Path]) -> None:
    """A dead pid behind a non-terminal meta.json yields already_dead and a
    rewritten state, without raising."""
    project_dir, runs_dir = env

    # A definitively-dead pid: spawn a trivial process, reap it, reuse its id.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_pid = dead.pid
    assert not is_pid_alive(dead_pid)

    run_id = "20260101_000000_orphan"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_supervisor.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": dead_pid,
                "pgid": dead_pid,
                "command": ["x"],
                "project_dir": str(project_dir),
                "started_at": "2026-01-01T00:00:00.000Z",
                "status": "running",
                "mock": True,
                "output_mode": "summary",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Non-terminal meta so the terminal-status guard does not short-circuit.
    (run_dir / "meta.json").write_text(
        json.dumps({"status": "running", "task": "t"}), encoding="utf-8"
    )

    result = cancel_run(run_id, runs_dir=str(runs_dir), mode="graceful")
    assert result.status == "already_dead"

    # State rewritten with a settled status + halt reason. The seeded state
    # carried status="running"; cancel must overwrite it so no reader mistakes
    # the orphaned run for an active one.
    state = json.loads((run_dir / "run_supervisor.json").read_text())
    assert state["status"] == "interrupted"
    assert state["halt_reason"] == "interrupted_orphan"


def test_cancel_missing_state_raises(env: tuple[Path, Path]) -> None:
    """A run id with no run_supervisor.json raises RunNotFound (the only
    non-idempotent cancel outcome besides a bad mode)."""
    _project_dir, runs_dir = env
    with pytest.raises(RunNotFound):
        cancel_run("nope_20260101", runs_dir=str(runs_dir))
    with pytest.raises(ValueError):
        cancel_run("nope_20260101", runs_dir=str(runs_dir), mode="sideways")


def test_resume(env: tuple[Path, Path], live_runs) -> None:
    """resume_run continues a checkpoint-paused mock run past its pause,
    inheriting mock/output_mode from the persisted state."""
    project_dir, runs_dir = env
    # reject rounds exhaust the validate_plan gate → the run pauses at a
    # phase-handoff decision point (awaiting_phase_handoff), process exits.
    res = live_runs(
        launch_run(_spec(project_dir, runs_dir, mock_validate_plan_reject=3))
    )
    res.popen.wait(timeout=90)
    meta = json.loads((res.run.run_dir / "meta.json").read_text())
    assert meta.get("status") == "awaiting_phase_handoff"

    handoff = meta.get("phase_handoff") or {}
    handoff_id = handoff.get("id")
    assert handoff_id, "paused run has no phase_handoff id to decide on"
    assert "continue" in (handoff.get("available_actions") or [])

    # Record the human 'continue' decision, then resume past the pause.
    phase_handoff_decide(
        res.run.run_id, handoff_id, "continue", runs_dir=str(runs_dir), cwd=None
    )

    resumed = live_runs(resume_run(res.run.run_id, runs_dir=str(runs_dir)))
    # Inherited from the persisted state, not re-specified by the caller.
    assert resumed.run.mock is True
    assert resumed.run.output_mode == "summary"
    assert resumed.run.run_id == res.run.run_id

    resumed.popen.wait(timeout=90)
    final = _read_status(resumed.run.run_dir)
    assert final in _TERMINAL and final != "awaiting_phase_handoff", (
        f"resume did not move the run past its pause: {final!r}"
    )


def test_resume_missing_run_raises(env: tuple[Path, Path]) -> None:
    """resume_run on an unknown run id raises RunNotFound (reused, not a new
    error type)."""
    _project_dir, runs_dir = env
    with pytest.raises(RunNotFound):
        resume_run("does_not_exist_20260101", runs_dir=str(runs_dir))


def test_finalized_resume_preflight_never_spawns(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, runs_dir = env
    run_dir = runs_dir / "parent"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"status": "failed", "task": "t"}))
    (run_dir / "run_supervisor.json").write_text(json.dumps({"project_dir": str(project_dir)}))
    (run_dir / "scheduled_gate_ledger.json").write_text(json.dumps({
        "schema_version": "1", "finalized": True, "rows": [], "trail": [],
    }))
    monkeypatch.setattr(
        "sdk.run_control.launch._spawn_detached",
        lambda *_args, **_kwargs: pytest.fail("finalized parent must not spawn"),
    )
    with pytest.raises(LaunchError, match="finalized scheduled-gate ledger"):
        resume_run("parent", runs_dir=str(runs_dir))


def test_followup_and_plan_children_use_fresh_lineage(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, runs_dir = env
    worktree = project_dir.parent / "retained"
    _init_git_repo(worktree)
    (worktree / "change.txt").write_text("retained\n")
    parent = runs_dir / "parent"
    parent.mkdir()
    (parent / "meta.json").write_text(json.dumps({
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": str(project_dir), "task": "fix", "worktree": {
            "path": str(worktree), "isolation": "per_run",
        }, "phases": {"final_acceptance": {"verdict": "REJECTED"}},
    }))
    spawned: list[list[str]] = []

    class _Popen:
        pid = 123

    monkeypatch.setattr(
        "sdk.run_control.launch._spawn_detached",
        lambda cmd, **_kwargs: spawned.append(cmd) or _Popen(),
    )
    child = launch_correction_followup(
        CorrectionFollowupLaunchRequest("parent", "исправить", runs_dir=str(runs_dir)),
        run_id="followup",
    )
    assert child.run.run_id != "parent"
    assert child.run.run_dir == runs_dir / "followup"
    assert "--resume" in spawned[0] and "parent" in spawned[0]
    assert not (child.run.run_dir / "scheduled_gate_ledger.json").exists()

    # A plan child is a distinct operation, never a correction resume.
    (parent / "parsed_plan.json").write_text("{}")
    plan_parent = json.loads((parent / "meta.json").read_text())
    plan_parent.update({"halt_reason": "other", "worktree": {}})
    (parent / "meta.json").write_text(json.dumps(plan_parent))
    plan = launch_from_run_plan(
        FromRunPlanLaunchRequest("parent", runs_dir=str(runs_dir)), run_id="plan-child",
    )
    assert plan.run.run_id != "parent"
    assert "--from-run-plan" in spawned[1]
    assert "--resume" not in spawned[1]
