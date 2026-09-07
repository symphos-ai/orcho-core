"""An unresolved delivery action must never reach Git (ADR 0191).

Reproduces the dogfood failure of run ``20260907_100016_02dcb1``: a
``decision_mode='defer'`` run launched WITHOUT ``--no-interactive`` but with no
TTY (an MCP-supervised subprocess). ``resolve_commit_delivery`` parked the
decision as ``action='none'`` / ``status='pending'``; the caller applied it
anyway; ``apply_commit_delivery`` transported the patch and created a real
commit, then failed while writing the audit artifact because ``none`` is not
an auditable action. The commit existed; the run recorded nothing.

Every test here runs against real temporary git repositories and asserts on
the target checkout's HEAD / index, not on mocks.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import pipeline.engine.commit_delivery as cd
from core.contracts.commit_decision_schema import CommitDecisionSchemaError
from core.io.git_helpers import create_worktree
from pipeline.engine import delivery_ledger
from pipeline.engine.commit_delivery import (
    CommitDeliveryDecision,
    apply_commit_delivery,
    resolve_commit_delivery,
)

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


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD"))


def _seed(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """Target repo + run dir + run worktree carrying one run-owned change."""
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    run_dir = tmp_path / "run"
    result = create_worktree(
        repo=repo,
        base_ref=head,
        target_path=run_dir / "checkout",
        branch_name="orcho/run/r1",
    )
    assert result.ok, result.error
    worktree = run_dir / "checkout"
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    return repo, run_dir, worktree, head


def _session(verdict: str) -> dict:
    entry: dict = {"verdict": verdict, "short_summary": "feat: update app"}
    if verdict == "REJECTED":
        entry["approved"] = False
        entry["ship_ready"] = False
        entry["release_blockers"] = [{
            "id": "B1", "severity": "P0", "title": "open criterion",
            "body": "...", "required_fix": "prove C1",
            "why_blocks_release": "unproven",
        }]
    return {"status": "done", "phases": {"final_acceptance": entry}}


_CFG = {"enabled": True, "add_untracked": True, "branch_policy": "bypass"}


def _assert_untouched(repo: Path, head: str, run_dir: Path) -> None:
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert not (run_dir / "commit_decisions").exists()


def test_apply_refuses_unresolved_action_before_any_git_mutation(
    tmp_path: Path,
) -> None:
    repo, run_dir, worktree, head = _seed(tmp_path)
    parked = CommitDeliveryDecision(
        action="none",
        status="pending",
        run_id="r1",
        decision_id="r1",
        project_path=repo,
        source_path=worktree,
        baseline_ref="HEAD",
        dirty=True,
        patch_text=cd._run_owned_patch(worktree, "HEAD"),
        changed_paths=("app.txt",),
        decided_at="2026-09-07T12:09:48+00:00",
    )

    out = apply_commit_delivery(parked, run_dir=run_dir, commit_config=_CFG)

    # The decision comes back unchanged and undecided; nothing was written.
    assert out == parked
    _assert_untouched(repo, head, run_dir)


@pytest.mark.parametrize("verdict", ["APPROVED", "REJECTED"])
def test_defer_without_tty_parks_even_when_no_interactive_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str,
) -> None:
    # The exact launch shape of the failed dogfood resume: ``defer`` mode,
    # ``no_interactive=False`` (the flag was not passed), no TTY behind it.
    monkeypatch.setattr(cd, "stdio_interactive", lambda: False)
    repo, run_dir, worktree, head = _seed(tmp_path)

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(verdict),
        commit_config=_CFG,
        no_interactive=False,
        decision_mode="defer",
    )
    assert decision.action == "none"
    assert decision.status == "pending"
    assert decision.release_verdict == verdict

    # Even a caller that ignores the parked state cannot reach Git through it.
    applied = apply_commit_delivery(
        decision, run_dir=run_dir, commit_config=_CFG, no_interactive=False,
    )
    assert applied.status == "pending"
    assert applied.action == "none"
    _assert_untouched(repo, head, run_dir)


def test_audit_failure_after_commit_leaves_recoverable_fact_and_idempotent_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, run_dir, worktree, head = _seed(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session("APPROVED"),
        commit_config={**_CFG, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    assert decision.action == "approve"
    assert decision.status == "pending"

    # Simulate the audit write failing AFTER the commit landed (a full disk or
    # SIGKILL in that window; the observed schema refusal now fails before the
    # commit — see ``test_audit_is_validated_before_any_git_mutation``).
    real_persist = cd._persist

    def _fail_after_commit(decision, **kwargs):
        if kwargs.get("status") == "committed":
            raise OSError("simulated audit write failure after commit")
        return real_persist(decision, **kwargs)

    monkeypatch.setattr(cd, "_persist", _fail_after_commit)
    with pytest.raises(OSError, match="after commit"):
        apply_commit_delivery(decision, run_dir=run_dir, commit_config=_CFG)

    committed = _git(repo, "rev-parse", "HEAD")
    assert committed != head
    assert _commit_count(repo) == 2
    # The commit fact survived the audit failure.
    record = delivery_ledger.load_delivery_ledger(run_dir, "r1")
    assert record is not None
    assert record.stage == delivery_ledger.STAGE_COMMITTED
    assert record.commit_sha == committed
    assert record.head_before == head
    assert not cd._artifact_path(run_dir, "r1").exists()

    # A resume re-resolves delivery: the ledger-backed commit is adopted, the
    # audit is completed, and no second commit is created.
    monkeypatch.setattr(cd, "_persist", real_persist)
    resumed = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session("APPROVED"),
        commit_config={**_CFG, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    assert resumed.status == "committed"
    assert resumed.action == "approve"
    assert resumed.commit_sha == committed
    assert resumed.provenance == "resume_adopted"
    assert _commit_count(repo) == 2
    assert _git(repo, "rev-parse", "HEAD") == committed
    artifact = json.loads(cd._artifact_path(run_dir, "r1").read_text())
    assert artifact["commit_status"] == "committed"
    assert artifact["commit_sha"] == committed
    assert delivery_ledger.load_delivery_ledger(run_dir, "r1").stage == (
        delivery_ledger.STAGE_RECORDED
    )
    # Applying the adopted decision is a no-op (status is not pending).
    assert apply_commit_delivery(resumed, run_dir=run_dir, commit_config=_CFG) == resumed
    assert _commit_count(repo) == 2


def test_legacy_delivery_commit_without_ledger_is_never_redelivered(
    tmp_path: Path,
) -> None:
    # A commit created by an engine version that kept no ledger (the dogfood
    # run): the resolve refuses to deliver again and names the existing sha
    # instead of parking a fresh gate or transporting the patch a second time.
    repo, run_dir, worktree, head = _seed(tmp_path)
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-q", "-s", "-m", "chore: deliver orcho run r1")
    legacy_sha = _git(repo, "rev-parse", "HEAD")

    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session("REJECTED"),
        commit_config=_CFG,
        no_interactive=True,
        decision_mode="defer",
    )
    assert decision.status == "not_applicable"
    assert decision.action == "none"
    assert decision.commit_sha == legacy_sha
    assert decision.provenance == "existing_commit"
    assert legacy_sha[:12] in (decision.error or "")

    applied = apply_commit_delivery(decision, run_dir=run_dir, commit_config=_CFG)
    assert applied == decision
    assert _commit_count(repo) == 2
    assert _git(repo, "status", "--porcelain") == ""


def test_audit_is_validated_before_any_git_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A decision whose audit artifact can never validate (an unknown message
    # strategy on an approve) is refused before the patch is transported.
    repo, run_dir, worktree, head = _seed(tmp_path)
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session("APPROVED"),
        commit_config={**_CFG, "auto_in_ci": "approve"},
        no_interactive=True,
    )
    broken = cd.replace(decision, commit_message_strategy="not-a-strategy")

    with pytest.raises(CommitDecisionSchemaError):
        apply_commit_delivery(broken, run_dir=run_dir, commit_config=_CFG)

    _assert_untouched(repo, head, run_dir)
