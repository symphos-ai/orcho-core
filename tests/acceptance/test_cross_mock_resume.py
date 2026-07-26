"""Public-facade acceptance coverage for durable cross mock resumes.

The interruption seam runs only after the first child has returned from its
real child pipeline call, so its child ``meta.json`` is already durable.  It is
not a timer: if that proof cannot be made, the test fails before attempting a
resume.
"""
from __future__ import annotations

import json
import subprocess
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
