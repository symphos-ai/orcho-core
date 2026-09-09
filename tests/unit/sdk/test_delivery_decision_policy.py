"""An out-of-band delivery decision applies the run's delivery policy.

The deciding process (SDK ``decide_delivery``, ``orcho delivery decide``,
``orcho_delivery_decide``) resolves ``AppConfig`` from its own env / cwd. It
used to take ``branch_policy`` / ``publish`` / ``default_strategy`` from
there, so a gate parked under ``bypass`` (commit into the checkout) shipped
onto a published ``orcho/deliver/*`` branch when decided from a shell outside
the run's workspace — and the reverse would commit straight onto the target
checkout. The producer now stamps a normalised ``commit_policy`` snapshot on
the parked decision and the replay overlays it on the process config.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.io.git_helpers import create_worktree
from pipeline.engine.commit_delivery import resolve_commit_delivery
from sdk.run_control.delivery import _replay_commit_config, decide_delivery

pytestmark = [pytest.mark.sdk, pytest.mark.git_worktree]


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


def _park(tmp_path: Path, *, producer_cfg: dict, strip_snapshot: bool = False) -> tuple[Path, Path]:
    """Park a deferred gate under ``producer_cfg``. Returns ``(runs_dir, repo)``."""
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
    (run_dir / "checkout" / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=run_dir / "checkout",
        run_dir=run_dir,
        run_id="r1",
        session={"status": "done", "phases": {"final_acceptance": {
            "verdict": "APPROVED", "short_summary": "feat: x"}}},
        commit_config={"enabled": True, "add_untracked": True, **producer_cfg},
        no_interactive=True,
        decision_mode="defer",
    )
    assert decision.status == "pending" and decision.action == "none"
    ctx = decision.to_dict()
    if strip_snapshot:
        ctx.pop("commit_policy")
    meta = {"status": "halted", "halt_reason": "commit_delivery_pending",
            "project": str(repo), "commit_delivery": ctx}
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return runs, repo


@pytest.fixture
def process_policy_is_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deciding process's config says: published branch + always publish."""
    monkeypatch.setattr(
        "core.infra.config.AppConfig.load",
        lambda: type("Cfg", (), {"commit": {
            "branch_policy": "worktree_branch", "publish": "always",
            "default_strategy": "release_summary", "add_untracked": True,
        }})(),
    )


# ── producer: the parked gate carries the snapshot ───────────────────────────


def test_parked_gate_persists_the_normalised_policy(tmp_path: Path) -> None:
    runs, _repo = _park(
        tmp_path, producer_cfg={"branch_policy": "bypass", "publish": "off",
                                "default_strategy": "llm_generate"},
    )
    block = json.loads((runs / "r1" / "meta.json").read_text())["commit_delivery"]
    assert block["commit_policy"] == {
        "branch_policy": "bypass", "publish": "off", "default_strategy": "llm_generate",
    }


# ── replay config: snapshot beats the process config ─────────────────────────


def test_replay_config_pins_the_snapshot_over_the_process(
    process_policy_is_publish: None,
) -> None:
    cfg = _replay_commit_config({
        "include_untracked": False,
        "commit_policy": {"branch_policy": "bypass", "publish": "off",
                          "default_strategy": "llm_generate", "branch_name": "rel/x"},
    })
    assert cfg["branch_policy"] == "bypass"
    assert cfg["publish"] == "off"
    assert cfg["default_strategy"] == "llm_generate"
    assert cfg["branch_name"] == "rel/x"
    assert cfg["decision_mode"] == "auto"
    assert cfg["add_untracked"] is False


def test_replay_config_without_snapshot_keeps_the_process_policy(
    process_policy_is_publish: None,
) -> None:
    cfg = _replay_commit_config({"include_untracked": True})
    assert cfg["branch_policy"] == "worktree_branch"
    assert cfg["publish"] == "always"


# ── producer → consumer: approve from a foreign process lands where the run said ──


def test_approve_from_a_publish_process_commits_into_the_checkout_when_the_run_said_bypass(
    tmp_path: Path, process_policy_is_publish: None,
) -> None:
    runs, repo = _park(tmp_path, producer_cfg={"branch_policy": "bypass", "publish": "off"})
    before = _git(repo, "rev-parse", "HEAD")

    result = decide_delivery("r1", "approve", runs_dir=runs, cwd=None)

    assert result.accepted, result
    assert result.status == "committed"
    assert result.commit_sha and result.commit_sha != before
    assert _git(repo, "rev-parse", "HEAD") == result.commit_sha
    assert result.delivery_branch is None
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\nrun\n"
    assert not any(b.strip().startswith("orcho/deliver/") for b in _git(repo, "branch").splitlines())
    block = json.loads((runs / "r1" / "meta.json").read_text())["commit_delivery"]
    assert not any("commit_policy snapshot" in w for w in block.get("delivery_warnings", []))


def test_legacy_gate_without_snapshot_uses_the_process_policy_and_warns(
    tmp_path: Path, process_policy_is_publish: None,
) -> None:
    runs, repo = _park(
        tmp_path, producer_cfg={"branch_policy": "bypass", "publish": "off"}, strip_snapshot=True,
    )
    before = _git(repo, "rev-parse", "HEAD")

    result = decide_delivery("r1", "approve", runs_dir=runs, cwd=None)

    assert result.accepted, result
    # The process said published branch: the checkout HEAD is untouched.
    assert _git(repo, "rev-parse", "HEAD") == before
    assert result.delivery_branch and result.delivery_branch.startswith("orcho/deliver/")
    block = json.loads((runs / "r1" / "meta.json").read_text())["commit_delivery"]
    assert any("parked without a commit_policy snapshot" in w for w in block["delivery_warnings"])
