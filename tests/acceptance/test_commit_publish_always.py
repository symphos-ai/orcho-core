"""Mock acceptance coverage for ``small_task`` delivery publication.

The scenario uses the shipped direct-checkout profile and a workspace-local
``commit.publish=always`` overlay.  Provider registration is faked in process:
no network, provider CLI, push, or real pull request is involved.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from agents.runtimes import MockAgentProvider
from pipeline.engine import delivery_publish
from pipeline.engine.delivery_branch import DeliveryPrIntent
from pipeline.engine.delivery_publish import DELIVERY_PROVIDER_GROUP, PublishResult
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline
from sdk.status import load_status

PLUGIN = PluginConfig(
    name="Commit publication acceptance project",
    language="Python",
    architecture="CLI",
    file_hints=["src.py"],
)


@pytest.fixture(autouse=True)
def _clear_app_config_cache() -> None:
    """Keep this workspace-local overlay from leaking into sibling tests."""
    from core.infra.config import AppConfig

    AppConfig.load.cache_clear()
    yield
    AppConfig.load.cache_clear()


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []

    def publish(
        self,
        pr_intent: DeliveryPrIntent,
        *,
        branch: str,
        cwd: Path,
        remote: str,
    ) -> PublishResult:
        self.calls.append(
            SimpleNamespace(
                pr_intent=pr_intent,
                branch=branch,
                cwd=cwd,
                remote=remote,
            )
        )
        return PublishResult(
            pushed=True, pr_url="https://example.invalid/pr/small-task"
        )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@orcho.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Orcho Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _configure_workspace(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    config_path = workspace / ".orcho" / "config.local.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"commit": {"publish": "always"}}), encoding="utf-8"
    )
    monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
    monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)
    from core.infra.config import AppConfig

    AppConfig.load.cache_clear()
    assert AppConfig.load().commit["publish"] == "always"


def _register(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider | None) -> None:
    def _discover(group: str, **_: Any) -> dict[str, _FakeProvider]:
        assert group == DELIVERY_PROVIDER_GROUP
        return {"fake": provider} if provider is not None else {}

    monkeypatch.setattr(delivery_publish, "discover_entry_points", _discover)


def _run_small_task(project: Path, run_dir: Path) -> dict[str, Any]:
    with patch("pipeline.project.session_run.load_plugin", return_value=PLUGIN):
        return run_pipeline(
            task="Add a tiny direct-checkout change",
            project_dir=str(project),
            output_dir=run_dir,
            profile_name="small_task",
            provider=MockAgentProvider(latency=0.0, test_pass_rate=1.0),
            no_interactive=True,
        )


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_small_task_always_publishes_committed_delivery_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "runs" / "small-task-with-provider"
    run_dir.mkdir(parents=True)
    _init_repo(project)
    _configure_workspace(monkeypatch, tmp_path)
    provider = _FakeProvider()
    _register(monkeypatch, provider)

    session = _run_small_task(project, run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    status = load_status(run_dir.name, runs_dir=run_dir.parent)
    delivery = meta["commit_delivery"]

    assert session["worktree"]["isolation"] == "off"
    assert meta["worktree"]["isolation"] == "off"
    assert delivery["status"] == "committed"
    assert delivery["delivery_branch"]
    assert delivery["pr_url"] == "https://example.invalid/pr/small-task"
    assert any("PR opened:" in notice for notice in delivery["delivery_notices"])
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call.branch == delivery["delivery_branch"]
    assert call.cwd == project
    assert call.remote == "origin"
    assert status.meta is not None
    assert status.meta.extra["commit_delivery"]["pr_url"] == delivery["pr_url"]
    assert evidence["run_id"] == run_dir.name
    assert evidence["status"] == meta["status"] == "done"


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
def test_small_task_always_without_provider_keeps_branch_ready_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "runs" / "small-task-no-provider"
    run_dir.mkdir(parents=True)
    _init_repo(project)
    _configure_workspace(monkeypatch, tmp_path)
    _register(monkeypatch, None)

    session = _run_small_task(project, run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    delivery = meta["commit_delivery"]

    assert session["worktree"]["isolation"] == "off"
    assert delivery["status"] == "committed"
    assert delivery["delivery_branch"]
    assert delivery["pr_url"] is None
    assert any(
        "is ready; open a pull request" in notice
        for notice in delivery["delivery_notices"]
    )
