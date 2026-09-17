"""The commit message of a deferred delivery is authored at park time (ADR 0121).

An out-of-band ``decide_delivery`` (SDK / MCP) has no commit-message
generator. It used to fall back to the release ``short_summary`` in the
operator's plan language, so a Russian commit and PR title landed on a
public repository. The producer now authors the message while the run's own
agent is available and persists it on the parked gate; the replay pins it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import pipeline.engine.commit_delivery as cd
from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import (
    CommitMessageGenerationFailure,
    resolve_commit_delivery,
)
from sdk.run_control.delivery import decide_delivery

pytestmark = [pytest.mark.sdk, pytest.mark.git_worktree]

_ENGLISH = "feat(cli): show the next step after a parked delivery\n\nAuthored by the run agent."


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@orcho.invalid")
    _git(repo, "config", "user.name", "Orcho Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD")


def _park(tmp_path: Path, *, generator, cfg: dict) -> tuple[Path, Path, Path, object]:
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    run_dir.mkdir(parents=True)
    result = create_worktree(
        repo=repo, base_ref=head, target_path=run_dir / "checkout",
        branch_name="orcho/run/r1",
    )
    assert result.ok, result.error
    wt = run_dir / "checkout"
    (wt / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    session = {
        "status": "done",
        "phases": {"final_acceptance": {
            "verdict": "APPROVED", "approved": True,
            "short_summary": "Сохранённый дифф готов к выпуску",
        }},
    }
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=wt,
        run_dir=run_dir,
        run_id="r1",
        session=session,
        commit_config=cfg,
        no_interactive=True,
        decision_mode="defer",
        commit_message_generator=generator,
    )
    assert decision.status == "pending" and decision.action == "none"
    meta = {
        "run_id": "r1", "status": "halted", "halt_reason": "commit_delivery_pending",
        "project": str(repo), "phases": session["phases"],
        "commit_delivery": decision.to_dict(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return runs, repo, wt, decision


_BYPASS = {"enabled": True, "add_untracked": True, "branch_policy": "bypass",
           "default_strategy": "llm_generate", "publish": "off"}


def test_park_authors_the_message_with_the_run_agent(tmp_path: Path) -> None:
    calls: list[str] = []

    def generator(decision):
        calls.append(decision.run_id)
        return _ENGLISH

    runs, repo, wt, parked = _park(tmp_path, generator=generator, cfg=_BYPASS)

    assert calls == ["r1"]
    assert parked.final_message == _ENGLISH
    assert parked.commit_message_strategy == "llm_generate"
    block = json.loads((runs / "r1" / "meta.json").read_text())["commit_delivery"]
    assert block["final_message"] == _ENGLISH
    assert block["strategy"] == "llm_generate"


def test_out_of_band_approve_commits_the_pinned_message(tmp_path: Path) -> None:
    runs, repo, wt, _ = _park(tmp_path, generator=lambda _d: _ENGLISH, cfg=_BYPASS)
    # The replay process has a config of its own (worktree_branch by default);
    # the parked gate's commit_policy snapshot carries the run's ``bypass``.
    result = decide_delivery("r1", "approve", runs_dir=runs, cwd=None)

    assert result.accepted, result
    assert result.status == "committed"
    assert _git(repo, "log", "-1", "--format=%B").startswith(_ENGLISH)
    audit = json.loads((runs / "r1" / "commit_decisions" / "r1.json").read_text())
    assert audit["final_message"] == _ENGLISH
    assert audit["strategy"] == "llm_generate"


def test_generation_failure_at_park_keeps_the_fallback_and_a_warning(
    tmp_path: Path,
) -> None:
    def generator(_decision):
        return CommitMessageGenerationFailure("CommitMessageSchemaError: missing keys")

    _runs, _repo, _wt, parked = _park(tmp_path, generator=generator, cfg=_BYPASS)

    assert parked.commit_message_strategy == "release_summary"
    assert parked.final_message == "Сохранённый дифф готов к выпуску"
    assert any("used release_summary fallback" in w for w in parked.delivery_warnings)


def test_park_without_generator_or_llm_strategy_is_unchanged(tmp_path: Path) -> None:
    cfg = {**_BYPASS, "default_strategy": "release_summary"}
    _runs, _repo, _wt, parked = _park(
        tmp_path / "summary", generator=lambda _d: _ENGLISH, cfg=cfg,
    )
    assert parked.final_message is None
    assert parked.commit_message_strategy is None

    _runs, _repo, _wt, parked = _park(tmp_path / "nogen", generator=None, cfg=_BYPASS)
    assert parked.final_message is None


def test_publish_forces_authoring_even_with_release_summary_strategy(tmp_path: Path) -> None:
    cfg = {**_BYPASS, "default_strategy": "release_summary", "publish": "always",
           "branch_policy": "worktree_branch"}
    calls: list[str] = []

    def generator(decision):
        calls.append(decision.run_id)
        return _ENGLISH

    _runs, _repo, _wt, parked = _park(tmp_path, generator=generator, cfg=cfg)
    assert calls == ["r1"]
    assert parked.final_message == _ENGLISH
    assert cd._will_open_pr(cfg)
