"""Defer-mode parking for the commit-delivery producer (ADR 0100, slice C2).

``commit.decision_mode='defer'`` parks a non-interactive run's delivery decision
as a recoverable ``pending`` gate (status halted, halt_reason
``commit_delivery_pending``) instead of auto-applying it. The default ``auto``
mode must stay byte-identical, so it still reaches ``apply_commit_delivery``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline.engine.commit_delivery as cd
from pipeline.project.finalization import _halt_banner
from pipeline.project.run import _PipelineRun


def _stub(run_dir: Path, project_dir: Path, *, no_interactive: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        output_dir=run_dir,
        session={"status": "done"},
        project_path=project_dir,
        parent_run_id=None,
        project_alias=None,
        no_interactive=no_interactive,
        worktree_context=None,
        session_ts="20260623_000000",
        _commit_delivery_baseline=lambda: "HEAD",
    )


def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cd, "_run_owned_patch",
        lambda *_a, **_kw: "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
    )
    monkeypatch.setattr(cd, "_changed_paths", lambda *_a, **_kw: ("a.py",))
    monkeypatch.setattr(cd, "_untracked_paths", lambda *_a, **_kw: ())


def test_defer_parks_pending_without_applying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _stub_git(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "add_untracked": False, "decision_mode": "defer"},
        ),
    )
    # apply must never run on the defer path — fail loudly if it does.
    monkeypatch.setattr(
        cd, "apply_commit_delivery",
        lambda *_a, **_kw: pytest.fail("apply_commit_delivery called in defer mode"),
    )

    stub = _stub(run_dir, project_dir)
    _PipelineRun._run_commit_delivery(stub, diff_cwd=project_dir)

    assert stub.session["status"] == "halted"
    assert stub.session["halt_reason"] == "commit_delivery_pending"
    parked = stub.session["commit_delivery"]
    assert parked["status"] == "pending"
    assert parked["changed_paths"] == ["a.py"]


def test_auto_mode_still_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _stub_git(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "add_untracked": False, "auto_in_ci": "skip"},
        ),
    )
    applied: list[str] = []

    def _fake_apply(decision, **_kw):
        applied.append(decision.action)
        return cd.CommitDeliveryDecision(
            action="skip",
            status="skipped",
            run_id=decision.run_id,
            decision_id=decision.decision_id,
            project_path=decision.project_path,
            source_path=decision.source_path,
            baseline_ref=decision.baseline_ref,
            decided_at=decision.decided_at,
        )

    monkeypatch.setattr(cd, "apply_commit_delivery", _fake_apply)

    stub = _stub(run_dir, project_dir)
    stub._presentation = None  # skip render branch
    _PipelineRun._run_commit_delivery(stub, diff_cwd=project_dir)

    # Default auto mode reaches apply and does not park the run.
    assert applied == ["skip"]
    assert stub.session["status"] == "done"
    assert stub.session.get("halt_reason") is None
    assert stub.session["commit_delivery"]["status"] == "skipped"


def _rejected_final_acceptance() -> dict:
    return {
        "verdict": "REJECTED",
        "ship_ready": False,
        "approved": False,
        "short_summary": "blocking data-loss defect",
        "release_blockers": [
            {
                "id": "B1",
                "severity": "P0",
                "title": "data loss on apply",
                "body": "...",
                "required_fix": "guard the write path",
                "why_blocks_release": "destroys user data",
            },
        ],
    }


def test_auto_rejected_release_persists_commit_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # auto / non-interactive run with a rejected final_acceptance: delivery is
    # refused (status not_applicable), but the decision must be PERSISTED to
    # meta so the rejection (verdict + summary) is visible downstream.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _stub_git(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "add_untracked": False},
        ),
    )
    # apply must never run — a rejected release hard-blocks before apply.
    monkeypatch.setattr(
        cd, "apply_commit_delivery",
        lambda *_a, **_kw: pytest.fail("apply_commit_delivery called on reject"),
    )

    stub = _stub(run_dir, project_dir)
    stub._presentation = None
    stub.session["phases"] = {"final_acceptance": _rejected_final_acceptance()}
    _PipelineRun._run_commit_delivery(stub, diff_cwd=project_dir)

    persisted = stub.session["commit_delivery"]
    assert persisted["status"] == "not_applicable"
    assert persisted["release_verdict"] == "REJECTED"
    assert persisted["release_summary"] == "blocking data-loss defect"
    # The run is not halted here — terminal status is owned by finalization.
    assert stub.session["status"] == "done"


def test_auto_rejected_release_blockers_available_without_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When short_summary is absent, release_summary falls back to the blockers
    # so the rejection reason is never lost from the persisted decision.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _stub_git(monkeypatch)
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "add_untracked": False},
        ),
    )
    monkeypatch.setattr(
        cd, "apply_commit_delivery",
        lambda *_a, **_kw: pytest.fail("apply_commit_delivery called on reject"),
    )

    rejected = _rejected_final_acceptance()
    rejected["short_summary"] = ""

    stub = _stub(run_dir, project_dir)
    stub._presentation = None
    stub.session["phases"] = {"final_acceptance": rejected}
    _PipelineRun._run_commit_delivery(stub, diff_cwd=project_dir)

    persisted = stub.session["commit_delivery"]
    assert persisted["release_verdict"] == "REJECTED"
    assert "data loss on apply" in persisted["release_summary"]


def test_auto_approved_no_diff_does_not_persist_commit_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Control: an APPROVED release whose worktree carries no diff resolves to
    # no_diff and must NOT write a commit_delivery record.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(cd, "_run_owned_patch", lambda *_a, **_kw: "(no diff)")
    monkeypatch.setattr(cd, "_changed_paths", lambda *_a, **_kw: ())
    monkeypatch.setattr(cd, "_untracked_paths", lambda *_a, **_kw: ())
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "add_untracked": False},
        ),
    )

    stub = _stub(run_dir, project_dir)
    stub._presentation = None
    stub.session["phases"] = {
        "final_acceptance": {
            "verdict": "APPROVED",
            "ship_ready": True,
            "approved": True,
            "short_summary": "all good",
            "release_blockers": [],
        },
    }
    _PipelineRun._run_commit_delivery(stub, diff_cwd=project_dir)

    assert "commit_delivery" not in stub.session
    assert stub.session["status"] == "done"


def test_disabled_delivery_does_not_persist_commit_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Control: commit delivery disabled -> disabled decision, never persisted.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(commit={"enabled": False}),
    )

    stub = _stub(run_dir, project_dir)
    stub._presentation = None
    stub.session["phases"] = {"final_acceptance": _rejected_final_acceptance()}
    _PipelineRun._run_commit_delivery(stub, diff_cwd=project_dir)

    assert "commit_delivery" not in stub.session


def test_finalization_banner_for_pending_is_amber() -> None:
    label, color = _halt_banner("commit_delivery_pending")
    assert "delivery decision pending" in label.lower()
    # Recoverable halts render amber, not red.
    from core.io.ansi import C

    assert color == C.YELLOW
