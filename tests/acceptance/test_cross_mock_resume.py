"""Public-facade acceptance coverage for durable cross mock resumes."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@orcho.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Orcho Test"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _add_alpha_retry_gate(path: Path) -> None:
    """Commit an alpha-only gate that fails once in its durable worktree."""
    plugin = path / ".orcho" / "multiagent" / "plugin.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(
        "PLUGIN = " + repr({
            "verification": {
                "commands": {
                    "alpha_once": {
                        "run": [
                            "python", "-c",
                            "from pathlib import Path; p = Path('.alpha_gate_once'); "
                            "first = not p.exists(); p.touch(); raise SystemExit(1 if first else 0)",
                        ],
                    },
                },
                "required": ["alpha_once"],
                "gate_sets": {"required": {"commands": ["alpha_once"]}},
                "selection": [{"always": ["required"]}],
                "schedule": [{
                    "after_phase": "implement", "policy": "require",
                    "action": "handoff", "commands": ["alpha_once"],
                }],
            },
        }) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".orcho/multiagent/plugin.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add alpha retry gate"], cwd=path, check=True)


def _add_beta_required_gate(path: Path) -> None:
    """Commit a beta gate so its declaration ledger is resume-critical."""
    plugin = path / ".orcho" / "multiagent" / "plugin.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(
        "PLUGIN = " + repr({
            "verification": {
                "commands": {"beta_check": {"run": ["python", "-c", "pass"]}},
                "gate_sets": {"required": {"commands": ["beta_check"]}},
                "selection": [{"always": ["required"]}],
                "schedule": [{
                    "after_phase": "implement", "policy": "require",
                    "gate_sets": ["required"],
                }],
            },
        }) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".orcho/multiagent/plugin.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add beta required gate"], cwd=path, check=True)


def _cross_subprocess_script(argv: list[str], *, stop_after_beta_start: bool) -> str:
    """Build an isolated CLI invocation with acceptance-only projection setup."""
    stop_seam = ""
    if stop_after_beta_start:
        stop_seam = """
from pipeline.project import session_run

_original_init_run_session = session_run.init_run_session

def _stop_after_beta_running_session(**kwargs):
    session = _original_init_run_session(**kwargs)
    if kwargs.get("output_dir") is not None and kwargs["output_dir"].name == "beta":
        os.kill(os.getpid(), signal.SIGSTOP)
    return session

session_run.init_run_session = _stop_after_beta_running_session
"""
    return f"""
import os
import signal
from dataclasses import replace

from cli.orcho import build_parser, cmd_cross
from core.infra import config
from pipeline.cross_project import profile_projection

profile_projection._reject_non_bypass_handoff_in_projection = lambda *_args: None
profile_projection._reject_non_bypass_handoff = lambda *_args: None
_original_reset_config = config._reset_config

def _reset_with_delivery_disabled():
    _original_reset_config()
    _base_config = config.AppConfig.load()
    config.AppConfig.load = lambda: replace(
        _base_config, commit={{**_base_config.commit, "enabled": False}}
    )

config._reset_config = _reset_with_delivery_disabled
{stop_seam}
raise SystemExit(cmd_cross(build_parser().parse_args({argv!r})))
"""


def _event_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _snapshot_tree(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _wait_for_crash_window(run_dir: Path, *, timeout_seconds: float = 20) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        beta_meta = run_dir / "beta" / "meta.json"
        checkpoint = run_dir / "cross_checkpoint.json"
        if beta_meta.exists() and checkpoint.exists():
            beta = json.loads(beta_meta.read_text(encoding="utf-8"))
            ckpt = json.loads(checkpoint.read_text(encoding="utf-8"))
            beta_events = [
                event
                for event in _event_rows(run_dir / "events.jsonl")
                if event.get("payload", {}).get("project_alias") == "beta"
            ]
            has_beta_start = any(event.get("kind") == "run.start" for event in beta_events)
            typed_beta_operation = any(
                event.get("kind") in {"phase.start", "gate.start"}
                for event in beta_events
            )
            if (
                has_beta_start
                and beta.get("status") == "running"
                and ckpt.get("sub_status", {}).get("beta") == "running"
                and (run_dir / "beta" / "scheduled_gate_ledger.json").is_file()
                and not typed_beta_operation
            ):
                return
        time.sleep(0.05)
    raise AssertionError("producer did not reach the durable beta pre-dispatch crash window")


def test_cross_mock_resume_inherits_provider_mode_after_durable_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A facade checkpoint resume without ``--mock`` remains hermetically mock."""
    from cli.orcho import build_parser, cmd_cross
    from core.infra import config
    from pipeline.cross_project import cli as cross_cli, project_dispatch

    workspace = tmp_path / "workspace"
    first_project = workspace / "api"
    second_project = workspace / "web"
    _init_git_repo(first_project)
    _init_git_repo(second_project)
    run_id = "cross-mock-resume"
    run_dir = workspace / "runspace" / "runs" / run_id
    run_dir.parent.mkdir(parents=True)

    # No real provider construction or binary lookup is permitted.  The CLI
    # guard records the effective provider mode on both facade invocations.
    provider_modes: list[bool] = []
    real_provider_invocations = 0
    real_binary_invocations = 0
    actual_make_provider = cross_cli.make_provider

    def guarded_make_provider(mock: bool, *args, **kwargs):
        nonlocal real_provider_invocations
        provider_modes.append(mock)
        if not mock:
            real_provider_invocations += 1
            raise AssertionError("real provider construction is forbidden")
        return actual_make_provider(mock, *args, **kwargs)

    def forbidden_binary_lookup(*_args, **_kwargs):
        nonlocal real_binary_invocations
        real_binary_invocations += 1
        raise AssertionError("real provider binary lookup is forbidden")

    monkeypatch.setattr(cross_cli, "make_provider", guarded_make_provider)
    monkeypatch.setattr(config, "get_claude_bin", forbidden_binary_lookup)
    monkeypatch.setattr(config, "get_codex_bin", forbidden_binary_lookup)

    # This seam is intentionally after the real child call.  It proves both
    # the child result and the parent checkpoint exist before stopping the
    # process; KeyboardInterrupt follows the production CLI exit-130 path.
    original_child_run = project_dispatch._run_child_pipeline
    interrupted = False

    def interrupt_after_first_durable_child(alias, request):
        nonlocal interrupted
        result = original_child_run(alias, request)
        if not interrupted:
            interrupted = True
            child_meta = run_dir / alias / "meta.json"
            assert child_meta.is_file(), "child must be durable before interruption"
            assert json.loads(child_meta.read_text(encoding="utf-8"))["status"] == "done"
            assert (run_dir / "cross_checkpoint.json").is_file(), (
                "parent checkpoint must exist before interruption"
            )
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(
        project_dispatch, "_run_child_pipeline", interrupt_after_first_durable_child,
    )

    parser = build_parser()
    first_args = parser.parse_args([
        "cross", "--task", "Coordinate mock resume", "--profile", "feature",
        "--projects", f"api:{first_project}", f"web:{second_project}",
        "--workspace", str(workspace), "--output-dir", str(run_dir),
        "--no-interactive", "--mock",
    ])
    assert cmd_cross(first_args) == 130
    assert interrupted is True
    assert json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["mock"] is True

    # Deliberately omit --mock: only persisted parent intent may select it.
    resume_argv = [
        "cross", "--resume", run_id, "--workspace", str(workspace),
        "--no-interactive",
    ]
    assert "--mock" not in resume_argv
    resume_args = parser.parse_args(resume_argv)
    assert resume_args.mock is False
    assert cmd_cross(resume_args) == 0

    final_meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert final_meta["status"] == "done"
    assert final_meta["mock"] is True
    assert provider_modes == [True, True]
    assert real_provider_invocations == 0
    assert real_binary_invocations == 0


def test_cross_cli_project_handoff_resume_retries_alpha_then_runs_beta(
    tmp_path: Path,
) -> None:
    """Public CLI + SDK journey for project-handoff continuation."""
    from cli.orcho import build_parser, cmd_cross
    from sdk.phase_handoff import phase_handoff_decide

    workspace = tmp_path / "workspace"
    alpha = workspace / "alpha"
    beta = workspace / "beta"
    _init_git_repo(alpha)
    _init_git_repo(beta)
    _add_alpha_retry_gate(alpha)
    run_id = "cross-project-handoff-resume"
    run_dir = workspace / "runspace" / "runs" / run_id
    run_dir.parent.mkdir(parents=True)
    parser = build_parser()

    fresh = parser.parse_args([
        "cross", "--task", "Retry alpha before beta", "--profile", "feature",
        "--projects", f"alpha:{alpha}", f"beta:{beta}",
        "--workspace", str(workspace), "--output-dir", str(run_dir),
        "--no-interactive", "--mock",
    ])
    assert cmd_cross(fresh) == 4
    paused = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    handoff = paused["phase_handoff"]
    assert paused["status"] == "awaiting_phase_handoff"
    assert handoff["id"].startswith("project:alpha:")
    assert not (run_dir / "beta" / "meta.json").exists()

    phase_handoff_decide(
        run_id, handoff["id"], "retry_feedback", feedback="Retry the alpha gate.",
        runs_dir=run_dir.parent, cwd=None,
    )
    resumed = parser.parse_args([
        "cross", "--resume", run_id, "--workspace", str(workspace),
        "--no-interactive",
    ])
    assert cmd_cross(resumed) == 0

    alpha_receipts = sorted(
        (run_dir / "alpha" / "verification_command_receipts" / "executions").glob("*.json"),
    )
    assert [json.loads(path.read_text(encoding="utf-8"))["exit_code"] for path in alpha_receipts] == [1, 0]
    assert (run_dir / "beta" / "meta.json").is_file()
    alpha_meta = json.loads((run_dir / "alpha" / "meta.json").read_text(encoding="utf-8"))
    beta_meta = json.loads((run_dir / "beta" / "meta.json").read_text(encoding="utf-8"))
    parent_meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert alpha_meta["status"] == beta_meta["status"] == "done"
    assert parent_meta["status"] == "done"
    assert "phase_handoff" not in parent_meta
    assert parent_meta.get("halt_reason") != "cross_child_readiness_blocked"
    cfa = parent_meta["phases"]["cross_final_acceptance"]
    assert not any(
        blocker["id"].startswith("CFA_MISSING_CHILD_")
        for blocker in cfa["release_blockers"]
    )


@pytest.mark.serial
@pytest.mark.slow_process
@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_cross_subprocess_resume_rearms_beta_before_first_operation(tmp_path: Path) -> None:
    """A SIGKILL in beta's pre-dispatch seam resumes only that declared child."""
    workspace = tmp_path / "workspace"
    alpha = workspace / "alpha"
    beta = workspace / "beta"
    _init_git_repo(alpha)
    _init_git_repo(beta)
    _add_beta_required_gate(beta)
    run_id = "cross-subprocess-beta-crash"
    run_dir = workspace / "runspace" / "runs" / run_id
    run_dir.parent.mkdir(parents=True)
    core_root = Path(__file__).resolve().parents[2]
    fresh_argv = [
        "cross", "--task", "Resume beta after a process crash", "--profile", "feature",
        "--projects", f"alpha:{alpha}", f"beta:{beta}",
        "--workspace", str(workspace), "--output-dir", str(run_dir),
        "--no-interactive", "--mock",
    ]
    producer = subprocess.Popen(
        [sys.executable, "-c", _cross_subprocess_script(fresh_argv, stop_after_beta_start=True)],
        cwd=core_root,
        env={**os.environ, "ORCHO_WORKSPACE": str(workspace)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            _wait_for_crash_window(run_dir)
        except AssertionError:
            if producer.poll() is not None:
                producer_output, _ = producer.communicate(timeout=10)
                raise AssertionError(producer_output) from None
            raise
        alpha_before = _snapshot_tree(run_dir / "alpha")
        os.killpg(producer.pid, signal.SIGKILL)
        producer_output, _ = producer.communicate(timeout=10)
        assert producer.returncode == -signal.SIGKILL, producer_output

        consumer_argv = [
            "cross", "--resume", run_id, "--workspace", str(workspace),
            "--no-interactive",
        ]
        consumer = subprocess.run(
            [sys.executable, "-c", _cross_subprocess_script(consumer_argv, stop_after_beta_start=False)],
            cwd=core_root,
            env={**os.environ, "ORCHO_WORKSPACE": str(workspace)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        assert consumer.returncode == 0, consumer.stdout
    finally:
        if producer.poll() is None:
            os.killpg(producer.pid, signal.SIGKILL)
            producer.communicate(timeout=10)

    events = _event_rows(run_dir / "events.jsonl")
    alpha_starts = [
        event for event in events
        if event.get("kind") == "run.start"
        and event.get("payload", {}).get("project_alias") == "alpha"
    ]
    beta_starts = [
        event for event in events
        if event.get("kind") == "run.start"
        and event.get("payload", {}).get("project_alias") == "beta"
    ]
    assert len(alpha_starts) == 1
    assert len(beta_starts) >= 2
    assert _snapshot_tree(run_dir / "alpha") == alpha_before

    beta_meta = json.loads((run_dir / "beta" / "meta.json").read_text(encoding="utf-8"))
    parent_meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert beta_meta["status"] == "done"
    assert parent_meta["status"] == "done"
    cfa = parent_meta["phases"]["cross_final_acceptance"]
    assert not any(
        blocker["id"] == "CFA_MISSING_CHILD_beta"
        for blocker in cfa["release_blockers"]
    )
