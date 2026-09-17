"""Delivery ledger: intent → committed → recorded, and read-only Git reconciliation.

ADR 0191. Every reconciliation state is pinned against a real temporary git
repository; the ledger is the durable fact a resume and the diagnosis
read-model consume, so its states must be exact.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.engine import delivery_ledger as dl

pytestmark = [pytest.mark.git_worktree]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


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


def _commit(repo: Path, subject: str) -> str:
    (repo / "app.txt").write_text(
        (repo / "app.txt").read_text(encoding="utf-8") + "more\n", encoding="utf-8",
    )
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _intent(run_dir: Path, repo: Path, **overrides) -> dl.DeliveryLedgerRecord:
    kwargs = {
        "run_id": "r1",
        "decision_id": "r1",
        "action": "approve",
        "commit_target": repo,
        "baseline_ref": "HEAD",
        "message": "feat: update app\n\nbody",
        "strategy": "release_summary",
        "staged_paths": ("app.txt",),
    }
    kwargs.update(overrides)
    return dl.record_delivery_intent(run_dir, **kwargs)


def test_intent_records_head_and_branch_before_the_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    run_dir = tmp_path / "run"

    record = _intent(run_dir, repo)

    assert record.stage == dl.STAGE_INTENT
    assert record.head_before == head
    assert record.branch_before == "main"
    assert record.subject == "feat: update app"
    assert record.commit_sha is None
    assert dl.load_delivery_ledger(run_dir, "r1") == record
    path = dl.ledger_path(run_dir, "r1")
    assert path.name == "r1.delivery.json"
    assert json.loads(path.read_text())["schema_version"] == "1"


def test_commit_and_audit_advance_the_stages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    record = _intent(run_dir, repo)

    committed = dl.record_delivery_commit(run_dir, record, "a" * 40)
    assert committed.stage == dl.STAGE_COMMITTED
    assert committed.commit_sha == "a" * 40
    assert committed.committed_at
    assert dl.load_delivery_ledger(run_dir, "r1") == committed

    recorded = dl.record_delivery_audit(run_dir, committed)
    assert recorded.stage == dl.STAGE_RECORDED
    assert recorded.recorded_at
    assert dl.load_delivery_ledger(run_dir, "r1") == recorded


def test_no_ledger_and_no_commit_reconciles_to_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    recon = dl.reconcile_delivery(
        tmp_path / "run", run_id="r1", decision_id="r1", project_path=repo,
    )
    assert recon.state == dl.RECON_NONE
    assert not recon.found


def test_recorded_stage_is_reported_as_recorded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    sha = _commit(repo, "feat: update app")
    record = dl.record_delivery_audit(
        run_dir, dl.record_delivery_commit(run_dir, _intent(run_dir, repo), sha),
    )
    recon = dl.reconcile_delivery(run_dir, run_id="r1", decision_id="r1", project_path=repo)
    assert recon.state == dl.RECON_RECORDED
    assert recon.found
    assert recon.commit_sha == sha
    assert recon.record == record


def test_commit_fact_without_audit_is_committed_unrecorded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    sha = _commit(repo, "feat: update app")
    dl.record_delivery_commit(run_dir, _intent(run_dir, repo), sha)

    recon = dl.reconcile_delivery(run_dir, run_id="r1", decision_id="r1", project_path=repo)
    assert recon.state == dl.RECON_COMMITTED_UNRECORDED
    assert recon.found
    assert recon.commit_sha == sha


def test_commit_fact_git_does_not_have_is_commit_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    dl.record_delivery_commit(run_dir, _intent(run_dir, repo), "b" * 40)

    recon = dl.reconcile_delivery(run_dir, run_id="r1", decision_id="r1", project_path=repo)
    assert recon.state == dl.RECON_COMMIT_MISSING
    assert not recon.found


def test_intent_matches_the_commit_by_parent_and_subject(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    run_dir = tmp_path / "run"
    _intent(run_dir, repo)

    # A different commit landed first: the intent is not satisfied by it.
    _commit(repo, "unrelated operator commit")
    recon = dl.reconcile_delivery(run_dir, run_id="r1", decision_id="r1", project_path=repo)
    assert recon.state == dl.RECON_INTENT_ONLY
    assert not recon.found

    # The intended commit must sit directly on the recorded HEAD-before.
    _git(repo, "reset", "-q", "--hard", head)
    sha = _commit(repo, "feat: update app")
    recon = dl.reconcile_delivery(run_dir, run_id="r1", decision_id="r1", project_path=repo)
    assert recon.state == dl.RECON_COMMITTED_UNRECORDED
    assert recon.commit_sha == sha
    assert "matches the recorded intent" in recon.detail


def test_legacy_commit_is_found_by_the_default_subject_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    _commit(repo, "feat: something else")
    assert dl.reconcile_delivery(
        run_dir, run_id="r1", decision_id="r1", project_path=repo,
    ).state == dl.RECON_NONE

    sha = _commit(repo, dl.default_delivery_subject("r1"))
    recon = dl.reconcile_delivery(run_dir, run_id="r1", decision_id="r1", project_path=repo)
    assert recon.state == dl.RECON_LEGACY_COMMIT
    assert recon.found
    assert recon.commit_sha == sha
    assert recon.record is None
    # Another run's delivery is never mistaken for this one.
    assert dl.reconcile_delivery(
        run_dir, run_id="r2", decision_id="r2", project_path=repo,
    ).state == dl.RECON_NONE


def test_an_unreadable_ledger_is_reported_not_ignored(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    path = dl.ledger_path(run_dir, "r1")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(dl.DeliveryLedgerError):
        dl.load_delivery_ledger(run_dir, "r1")
    recon = dl.reconcile_delivery(run_dir, run_id="r1", decision_id="r1", project_path=None)
    assert recon.state == dl.RECON_UNREADABLE
    assert not recon.found


def test_describe_commit_reads_message_parents_and_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    sha = _commit(repo, "feat: update app")

    facts = dl.describe_commit(repo, sha[:10])
    assert facts is not None
    assert facts.sha == sha
    assert facts.parents == (head,)
    assert facts.message.strip() == "feat: update app"
    assert facts.files == ("app.txt",)
    assert "Orcho Test" in facts.author
    assert dl.describe_commit(repo, "c" * 40) is None


def test_safe_decision_id_matches_the_audit_key() -> None:
    assert dl.safe_decision_id("20260907_100016_02dcb1") == "20260907_100016_02dcb1"
    assert dl.safe_decision_id("a/b c") == "a_b_c"
    assert dl.safe_decision_id("...") == "run"
