# SPDX-License-Identifier: Apache-2.0
"""Operator reconciliation of an unrecorded delivery commit (ADR 0191).

Models the dogfood run ``20260907_100016_02dcb1``: the engine committed a
rejected release, crashed before its audit, and left ``meta.json`` without a
delivery block and without the final-acceptance record (only the checkpoint
store held it). The SDK read-state must name the commit; the command must
record it with operator attribution, restore the release verdict from the
checkpoint, and settle a *reconciled* terminal — never a clean success and
never an "operator override".
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import pipeline.engine.commit_delivery as cd
from pipeline.checkpoint import CheckpointStore
from pipeline.engine import delivery_ledger as dl
from sdk.run_control import run_diagnosis
from sdk.run_control.delivery_reconcile import (
    inspect_delivery_reconciliation,
    reconcile_delivery_record,
)

pytestmark = [pytest.mark.sdk, pytest.mark.git_worktree]

RUN_ID = "20260907_100016_02dcb1"


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


def _legacy_delivery(repo: Path, run_id: str = RUN_ID) -> str:
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-q", "-s", "-m", dl.default_delivery_subject(run_id))
    return _git(repo, "rev-parse", "HEAD")


def _run(runs: Path, meta: dict, run_id: str = RUN_ID) -> Path:
    run_dir = runs / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_dir


def _meta(run_dir: Path) -> dict:
    return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))


def _rejected_by_backstop() -> dict:
    return {
        "critique": "...",
        "raw_response": '{"verdict": "APPROVED", "ship_ready": true}',
        "approved": False,
        "verdict": "REJECTED",
        "ship_ready": False,
        "short_summary": "Slice A introduces an independent event journal.",
        "release_blockers": [],
        "verification_gaps": [
            {
                "risk": "acceptance criterion C1 is missing",
                "missing_evidence": "missing: ",
                "required_check": "full web acceptance green",
            },
        ],
        "engine_backstop": {
            "reason": "acceptance_criteria_open",
            "gaps": [{"risk": "acceptance criterion C1 is missing"}],
            "model_verdict": "APPROVED",
            "model_ship_ready": True,
        },
    }


def _checkpoint_final_acceptance(run_dir: Path, record: dict) -> None:
    store = CheckpointStore(run_dir / "checkpoints.db", RUN_ID)
    store.save_config({})
    store.save_phase("final_acceptance", record)
    store._conn.close()


def test_inspect_names_the_unrecorded_legacy_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _legacy_delivery(repo)
    runs = tmp_path / "runs"
    _run(runs, {"status": "failed", "project": str(repo)})

    state = inspect_delivery_reconciliation(RUN_ID, runs_dir=runs, cwd=None)

    assert state.state == dl.RECON_LEGACY_COMMIT
    assert state.commit_sha == sha
    assert state.consistent is False
    assert state.recorded_status is None
    assert state.commit_target == str(repo)
    assert state.commit and state.commit["files"] == 1
    assert state.commit["subject"] == dl.default_delivery_subject(RUN_ID)


def test_inspect_is_consistent_when_meta_records_the_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _legacy_delivery(repo)
    runs = tmp_path / "runs"
    _run(runs, {
        "status": "done", "project": str(repo),
        "commit_delivery": {"status": "committed", "commit_sha": sha},
    })

    state = inspect_delivery_reconciliation(RUN_ID, runs_dir=runs, cwd=None)
    assert state.consistent is True
    assert state.recorded_sha == sha

    result = reconcile_delivery_record(
        RUN_ID, operator="op", commit=sha, runs_dir=runs, cwd=None,
    )
    assert result.accepted is False
    assert result.blocker == "already_recorded"


def test_reconcile_refuses_without_a_commit_and_on_a_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    runs = tmp_path / "runs"
    run_dir = _run(runs, {"status": "failed", "project": str(repo)})

    missing = reconcile_delivery_record(
        RUN_ID, operator="op", commit="abc", runs_dir=runs, cwd=None,
    )
    assert missing.accepted is False
    assert missing.blocker == "no_delivery_commit_found"

    sha = _legacy_delivery(repo)
    mismatch = reconcile_delivery_record(
        RUN_ID, operator="op", commit="deadbeef", runs_dir=runs, cwd=None,
    )
    assert mismatch.accepted is False
    assert mismatch.blocker == "commit_mismatch"
    assert mismatch.commit_sha == sha
    # A refusal writes nothing.
    assert _meta(run_dir) == {"status": "failed", "project": str(repo)}
    assert not (run_dir / "commit_decisions").exists()


def test_reconcile_records_the_commit_and_settles_a_reconciled_override(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    sha = _legacy_delivery(repo)
    runs = tmp_path / "runs"
    # The crash shape: meta never learned about final acceptance; the
    # checkpoint store did.
    run_dir = _run(runs, {
        "status": "failed",
        "halt_reason": "abnormal_exit:1",
        "project": str(repo),
        "worktree": {"path": str(tmp_path / "wt"), "base_ref": base},
    })
    _checkpoint_final_acceptance(run_dir, _rejected_by_backstop())

    result = reconcile_delivery_record(
        RUN_ID,
        operator="Eugen",
        commit=sha[:12],
        note="verified: 2093 tracked files match the implementation checkout",
        runs_dir=runs,
        cwd=None,
    )

    assert result.accepted is True
    assert result.state == dl.RECON_LEGACY_COMMIT
    assert result.commit_sha == sha
    assert result.terminal_outcome == "done"
    assert result.release_verdict == "REJECTED"
    assert any("checkpoint" in note for note in result.notes)

    meta = _meta(run_dir)
    delivery = meta["commit_delivery"]
    assert delivery["status"] == "committed"
    assert delivery["commit_sha"] == sha
    assert delivery["provenance"] == "reconciled"
    assert delivery["release_verdict"] == "REJECTED"
    assert delivery["files_staged"] == ["app.txt"]
    assert delivery["baseline_ref"] == base
    # Release verdict restored from the checkpoint, engine backstop intact.
    fa = meta["phases"]["final_acceptance"]
    assert fa["verdict"] == "REJECTED"
    assert fa["engine_backstop"]["model_verdict"] == "APPROVED"
    # Terminal: done by reconciliation, worded as such — never a clean success
    # and never an operator approval of the rejected release.
    assert meta["status"] == "done"
    override = meta["delivery_override"]
    assert override["provenance"] == "reconciled"
    assert "Operator override" not in override["message"]
    assert "reconciliation" in override["message"]
    assert override["delivery_status"] == "committed"
    assert "rejected_outcome" not in meta

    artifact = json.loads(
        (run_dir / "commit_decisions" / f"{RUN_ID}.json").read_text(encoding="utf-8"),
    )
    assert artifact["commit_status"] == "committed"
    assert artifact["commit_sha"] == sha
    assert artifact["operator"] == "Eugen"
    assert "2093 tracked files" in artifact["note"]
    assert artifact["strategy"] == "release_summary"
    assert result.artifact_path == str(run_dir / "commit_decisions" / f"{RUN_ID}.json")
    ledger = dl.load_delivery_ledger(run_dir, RUN_ID)
    assert ledger is not None
    assert ledger.stage == dl.STAGE_RECORDED
    assert ledger.provenance == "reconciled"

    # Afterwards every reader agrees: diagnosis no longer reports the
    # inconsistency, and a resolve adopts the recorded delivery instead of
    # probing Git again or delivering a second time.
    assert run_diagnosis(RUN_ID, runs_dir=runs, cwd=None).condition != "delivery_inconsistent"
    adopted = cd.resolve_commit_delivery(
        project_dir=repo,
        source_worktree=repo,
        run_dir=run_dir,
        run_id=RUN_ID,
        session={"status": "done"},
        commit_config={"enabled": True},
        no_interactive=True,
    )
    assert adopted.status == "committed"
    assert adopted.commit_sha == sha
    assert adopted.provenance == "reconciled"
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == 2

    again = reconcile_delivery_record(
        RUN_ID, operator="Eugen", commit=sha, runs_dir=runs, cwd=None,
    )
    assert again.accepted is False
    assert again.blocker == "already_recorded"


def test_reconcile_with_an_approved_release_settles_a_plain_done(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _legacy_delivery(repo)
    runs = tmp_path / "runs"
    run_dir = _run(runs, {
        "status": "failed",
        "project": str(repo),
        "phases": {"final_acceptance": {
            "verdict": "APPROVED", "approved": True, "ship_ready": True,
            "short_summary": "ok", "release_blockers": [],
        }},
    })

    result = reconcile_delivery_record(
        RUN_ID, operator="op", commit=sha, runs_dir=runs, cwd=None,
    )
    assert result.accepted is True
    assert result.release_verdict == "APPROVED"
    meta = _meta(run_dir)
    assert meta["status"] == "done"
    assert "delivery_override" not in meta
    assert "rejected_outcome" not in meta
    assert meta["commit_delivery"]["provenance"] == "reconciled"


def test_reconcile_without_any_release_record_says_the_verdict_is_unknown(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _legacy_delivery(repo)
    runs = tmp_path / "runs"
    _run(runs, {"status": "failed", "project": str(repo)})

    result = reconcile_delivery_record(
        RUN_ID, operator="op", commit=sha, runs_dir=runs, cwd=None,
    )
    assert result.accepted is True
    assert result.release_verdict is None
    assert any("unknown" in note for note in result.notes)


def test_operator_is_required() -> None:
    with pytest.raises(ValueError, match="operator"):
        reconcile_delivery_record(RUN_ID, operator="  ", commit="abc")
