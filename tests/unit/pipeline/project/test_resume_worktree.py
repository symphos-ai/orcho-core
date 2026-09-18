# SPDX-License-Identifier: Apache-2.0
"""Retained-subject worktree continuity for checkpoint-resume (T1).

A checkpoint-resume of a run paused after ``review_changes`` rejected its
change must reuse the **same** physical worktree that holds the rejected diff
— even when the resumed run-dir name no longer matches the original
``wt_<id>`` (the incident shape). These tests pin every classification class:

* (b) recorded isolated worktree present + registered -> reuse exactly it,
  for any checkpoint-resume, with no new ``wt_<run_id>`` created;
* (c)-error: recorded worktree gone + an active review-retry -> recoverable
  operator error naming the missing path, before any checkout materialises;
* (c)-passthrough: recorded worktree gone + no review-retry -> no new error
  (resolver keeps current behaviour);
* (a): no prior block / ``isolation=off`` -> passthrough.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.engine.worktree import (
    resolve_worktree_for_run,
)
from pipeline.project.resume_worktree import (
    classify_resume_worktree,
    detect_env_retry_active,
    detect_review_retry_active,
    resolve_resume_worktree,
)

pytestmark = pytest.mark.git_worktree


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "f.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _make_registered_worktree(project: Path, runs_dir: Path, run_id: str) -> dict:
    """Create a real registered orcho worktree and return its meta block."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = resolve_worktree_for_run(
        run_id=run_id,
        project_dir=project,
        run_dir=run_dir,
        worktree_config={"enabled": True, "isolation": "per_run"},
    )
    assert ctx.is_isolated
    return ctx.to_dict()


def _review_handoff(round_n: int = 1) -> dict:
    return {
        "id": f"review_changes:review:{round_n}",
        "phase": "review_changes",
        "type": "review",
    }


def _write_meta(
    run_dir: Path, *, worktree: dict | None, phase_handoff: dict | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {"id": run_dir.name, "status": "awaiting_phase_handoff"}
    if worktree is not None:
        meta["worktree"] = worktree
    if phase_handoff is not None:
        meta["phase_handoff"] = phase_handoff
    (run_dir / "meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")


def _write_decision(
    run_dir: Path, *, handoff_id: str, action: str, stem: str | None = None,
) -> None:
    """Persist a decision artifact; ``stem`` overrides the filename.

    The override exists for the corrupted-record case, where the file is
    addressed to one handoff and persists another id inside.
    """
    decisions = run_dir / "phase_handoff_decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    name = stem if stem is not None else handoff_id.replace(":", "_")
    (decisions / f"{name}.json").write_text(
        json.dumps({"handoff_id": handoff_id, "action": action}) + "\n",
        encoding="utf-8",
    )


# ── (b) retained subject available -> reuse exactly it ──────────────────────


def test_retained_subject_reused_for_incident_run_dir_name(tmp_path: Path) -> None:
    """Incident form: run-dir name != original worktree id -> reuse the path.

    The original run materialised ``wt_20260612_213531``; the resume is driven
    by a run dir named ``20260612_213530`` (a different id). The classifier
    must still pick the recorded retained worktree, and the resolver must
    attach to it instead of minting a fresh ``wt_20260612_213530``.
    """
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    worktree = _make_registered_worktree(project, runs_dir, "20260612_213531")
    retained_path = Path(worktree["path"])

    resume_run_dir = runs_dir / "20260612_213530"
    _write_meta(resume_run_dir, worktree=worktree, phase_handoff=_review_handoff())

    decision = resolve_resume_worktree(
        resume_from="20260612_213530",
        output_dir=resume_run_dir,
        project_dir=project,
    )
    assert decision is not None
    assert decision.blocked is False
    assert decision.retained_subject is not None
    assert decision.path == str(retained_path)
    assert decision.to_dict() == {
        "mode_label": f"retained retry subject {retained_path}",
        "path": str(retained_path),
        "source": "meta.worktree",
    }

    # The resolver attaches to the recorded path, NOT a fresh wt_<run_id>.
    ctx = resolve_worktree_for_run(
        run_id="20260612_213530",
        project_dir=project,
        run_dir=resume_run_dir,
        worktree_config={"enabled": True, "isolation": "per_run"},
        resume_prior_worktree=decision.retained_subject,
    )
    assert ctx.path == retained_path
    assert not (runs_dir.parent / "worktrees" / "wt_20260612_213530").exists()


def test_retained_subject_reused_without_review_retry(tmp_path: Path) -> None:
    """Reuse applies to ANY checkpoint-resume when the path is available."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    worktree = _make_registered_worktree(project, runs_dir, "20260101_orig")

    resume_run_dir = runs_dir / "20260101_orig"
    _write_meta(resume_run_dir, worktree=worktree)  # no active handoff

    decision = resolve_resume_worktree(
        resume_from="20260101_orig",
        output_dir=resume_run_dir,
        project_dir=project,
    )
    assert decision is not None
    assert decision.blocked is False
    assert decision.path == worktree["path"]


def test_resolver_retained_subject_appends_run_to_manifest(tmp_path: Path) -> None:
    """Attaching records the resume run id in the worktree manifest."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    worktree = _make_registered_worktree(project, runs_dir, "20260202_a")

    ctx = resolve_worktree_for_run(
        run_id="20260202_b",
        project_dir=project,
        run_dir=runs_dir / "20260202_b",
        worktree_config={"enabled": True, "isolation": "per_run"},
        resume_prior_worktree=worktree,
    )
    manifest = json.loads(Path(ctx.manifest_path).read_text(encoding="utf-8"))
    assert "20260202_b" in manifest["attached_run_ids"]
    assert ctx.path == Path(worktree["path"])


# ── (c) retained subject unavailable ────────────────────────────────────────


def test_blocked_when_unavailable_and_review_retry_active(tmp_path: Path) -> None:
    """Gone worktree + active review handoff -> recoverable error naming path."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    gone = tmp_path / "worktrees" / "wt_gone" / "checkout"
    worktree = {
        "isolation": "per_run",
        "path": str(gone),
        "base_ref": "deadbeef",
        "branch_ref": "orcho/run/20260612_213531",
        "worktree_id": "wt_20260612_213531",
        "root_run_id": "20260612_213531",
    }
    resume_run_dir = runs_dir / "20260612_213530"
    _write_meta(resume_run_dir, worktree=worktree, phase_handoff=_review_handoff())

    decision = resolve_resume_worktree(
        resume_from="20260612_213530",
        output_dir=resume_run_dir,
        project_dir=project,
    )
    assert decision is not None
    assert decision.blocked is True
    assert decision.retained_subject is None
    assert str(gone) in decision.block_message
    assert "review retry" in decision.block_message
    # The full prior meta.worktree block is carried so the caller can restore
    # the subject (path / isolation / base_ref) and keep the run decidable /
    # re-resumable after recovery, rather than persisting a truncated block.
    assert decision.prior_worktree == worktree


def test_blocked_via_recorded_retry_feedback_decision(tmp_path: Path) -> None:
    """Active review-retry can also come from a recorded retry_feedback decision."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    gone = tmp_path / "worktrees" / "wt_gone" / "checkout"
    worktree = {
        "isolation": "per_run",
        "path": str(gone),
        "base_ref": "deadbeef",
        "worktree_id": "wt_orig",
        "root_run_id": "orig",
    }
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(resume_run_dir, worktree=worktree)  # no active handoff payload
    _write_decision(
        resume_run_dir, handoff_id="review_changes:review:1", action="retry_feedback",
    )

    decision = resolve_resume_worktree(
        resume_from="resume_dir",
        output_dir=resume_run_dir,
        project_dir=project,
    )
    assert decision is not None
    assert decision.blocked is True


def test_generic_resume_unavailable_path_is_passthrough(tmp_path: Path) -> None:
    """Non-regression: gone worktree + NO review-retry -> no new error."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    gone = tmp_path / "worktrees" / "wt_gone" / "checkout"
    worktree = {
        "isolation": "per_run",
        "path": str(gone),
        "base_ref": "deadbeef",
        "worktree_id": "wt_orig",
        "root_run_id": "orig",
    }
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(resume_run_dir, worktree=worktree)  # no handoff, no decision

    decision = resolve_resume_worktree(
        resume_from="resume_dir",
        output_dir=resume_run_dir,
        project_dir=project,
    )
    assert decision is None


# ── (a) passthrough classes ─────────────────────────────────────────────────


def test_mode_off_is_passthrough(tmp_path: Path) -> None:
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(
        resume_run_dir,
        worktree={"isolation": "off", "path": str(project), "base_ref": "x"},
        phase_handoff=_review_handoff(),
    )
    assert (
        resolve_resume_worktree(
            resume_from="resume_dir", output_dir=resume_run_dir, project_dir=project,
        )
        is None
    )


def test_no_prior_worktree_block_is_passthrough(tmp_path: Path) -> None:
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(resume_run_dir, worktree=None, phase_handoff=_review_handoff())
    assert (
        resolve_resume_worktree(
            resume_from="resume_dir", output_dir=resume_run_dir, project_dir=project,
        )
        is None
    )


def test_non_resume_is_passthrough(tmp_path: Path) -> None:
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    worktree = _make_registered_worktree(project, runs_dir, "20260303_a")
    resume_run_dir = runs_dir / "20260303_a"
    _write_meta(resume_run_dir, worktree=worktree, phase_handoff=_review_handoff())
    # resume_from None -> not a checkpoint resume.
    assert (
        resolve_resume_worktree(
            resume_from=None, output_dir=resume_run_dir, project_dir=project,
        )
        is None
    )


# ── review-retry detection ──────────────────────────────────────────────────


def test_detect_review_retry_active_signals() -> None:
    assert detect_review_retry_active(
        prior_meta={"phase_handoff": {"phase": "review_changes"}}, decisions=[],
    )
    assert detect_review_retry_active(
        prior_meta={},
        decisions=[{"action": "retry_feedback", "handoff_id": "review_changes:r:2"}],
    )
    # A non-review handoff or a non-retry decision is not a review-retry.
    assert not detect_review_retry_active(
        prior_meta={"phase_handoff": {"phase": "validate_plan"}},
        decisions=[{"action": "halt", "handoff_id": "review_changes:r:1"}],
    )
    assert not detect_review_retry_active(prior_meta={}, decisions=[])


def test_classify_unavailable_without_retry_returns_none(tmp_path: Path) -> None:
    project = tmp_path / "api"
    _init_repo(project)
    worktree = {
        "isolation": "per_run",
        "path": str(tmp_path / "nope" / "checkout"),
        "base_ref": "x",
    }
    assert (
        classify_resume_worktree(
            prior_worktree=worktree, review_retry_active=False, project_dir=project,
        )
        is None
    )


def test_reclaimed_review_retry_is_blocked_before_historical_path_probe(tmp_path: Path) -> None:
    project = tmp_path / "api"
    _init_repo(project)
    decision = classify_resume_worktree(
        prior_worktree={
            "isolation": "per_run", "path": str(tmp_path / "historical"),
            "reclaimed": {"at": "2026-07-29T00:00:00Z"},
        },
        review_retry_active=True,
        project_dir=project,
    )
    assert decision is not None and decision.blocked is True
    assert decision.path is None
    assert "historical" in (decision.block_message or "")


def test_persisted_continuity_shape_is_unchanged_outside_a_blocked_env_retry(
    tmp_path: Path,
) -> None:
    """``resume_continuity`` is durable run state, so its shape is pinned.

    Only a blocked env retry adds a key, because only there is "no checkout was
    materialised" the whole outcome a reader has to act on. A reused subject
    and a blocked *review* retry keep the exact three-key record they have
    always persisted — widening those would change what an existing reader of
    an already-written run sees.
    """
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    retained_block = _make_registered_worktree(project, runs_dir, "20260917_orig")
    legacy_keys = {"mode_label", "path", "source"}

    reused = classify_resume_worktree(
        prior_worktree=retained_block,
        review_retry_active=True,
        project_dir=project,
    )
    assert reused is not None and reused.blocked is False
    assert set(reused.to_dict()) == legacy_keys

    blocked_review = classify_resume_worktree(
        prior_worktree={
            "isolation": "per_run", "path": str(tmp_path / "gone"),
        },
        review_retry_active=True,
        project_dir=project,
    )
    assert blocked_review is not None and blocked_review.blocked is True
    assert set(blocked_review.to_dict()) == legacy_keys

    blocked_env = classify_resume_worktree(
        prior_worktree={
            "isolation": "per_run", "path": str(tmp_path / "gone"),
        },
        review_retry_active=False,
        env_retry_active=True,
        project_dir=project,
    )
    assert blocked_env is not None and blocked_env.blocked is True
    assert blocked_env.to_dict() == {
        "mode_label": "blocked: retained retry subject unavailable",
        "path": str(tmp_path / "gone"),
        "source": "meta.worktree",
        "blocked": True,
    }


# ── env-retry: the retained subject the failing receipts observed ───────────


def _gate_handoff(handoff_id: str = "gate:lint:1") -> dict:
    return {"id": handoff_id, "trigger": "verification_gate_failed"}


def test_env_retry_blocks_when_retained_worktree_is_missing(tmp_path: Path) -> None:
    """A retry_verification resume may never re-measure a fresh checkout.

    The whole point of the retry is to re-run the same commands on the same
    tree the failing receipts observed. Minting a clean checkout would publish
    a pass for a subject the gate never failed on, so a gone retained path is
    a block, not a resolver fallback.
    """
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    gone = tmp_path / "worktrees" / "wt_gone" / "checkout"
    worktree = {
        "isolation": "per_run",
        "path": str(gone),
        "base_ref": "deadbeef",
        "worktree_id": "wt_orig",
        "root_run_id": "orig",
    }
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(resume_run_dir, worktree=worktree, phase_handoff=_gate_handoff())
    _write_decision(
        resume_run_dir, handoff_id="gate:lint:1", action="retry_verification",
    )

    decision = resolve_resume_worktree(
        resume_from="resume_dir", output_dir=resume_run_dir, project_dir=project,
    )

    assert decision is not None
    assert decision.blocked is True
    assert decision.retained_subject is None
    assert str(gone) in decision.block_message
    assert "verification worktree" in decision.block_message
    # The prior block is carried so the operator can restore the subject and
    # re-resume rather than being left with a truncated record.
    assert decision.prior_worktree == worktree


def test_env_retry_blocks_when_retained_worktree_is_unregistered(
    tmp_path: Path,
) -> None:
    """The directory existing is not proof: it must still be a git worktree."""
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    orphan = tmp_path / "worktrees" / "wt_orphan" / "checkout"
    orphan.mkdir(parents=True)
    worktree = {
        "isolation": "per_run", "path": str(orphan), "base_ref": "deadbeef",
    }
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(resume_run_dir, worktree=worktree, phase_handoff=_gate_handoff())
    _write_decision(
        resume_run_dir, handoff_id="gate:lint:1", action="retry_verification",
    )

    decision = resolve_resume_worktree(
        resume_from="resume_dir", output_dir=resume_run_dir, project_dir=project,
    )

    assert decision is not None
    assert decision.blocked is True
    assert decision.retained_subject is None


def test_env_retry_reclaimed_subject_is_blocked(tmp_path: Path) -> None:
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    worktree = {
        "isolation": "per_run",
        "path": str(tmp_path / "historical" / "checkout"),
        "reclaimed": {"at": "2026-09-01T00:00:00Z"},
    }
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(resume_run_dir, worktree=worktree, phase_handoff=_gate_handoff())
    _write_decision(
        resume_run_dir, handoff_id="gate:lint:1", action="retry_verification",
    )

    decision = resolve_resume_worktree(
        resume_from="resume_dir", output_dir=resume_run_dir, project_dir=project,
    )

    assert decision is not None
    assert decision.blocked is True
    assert decision.retained_subject is None
    assert decision.prior_worktree == worktree


def test_env_retry_with_a_corrupted_decision_id_still_holds_the_subject(
    tmp_path: Path,
) -> None:
    """The guard runs long before anything validates the decision artifact.

    A file addressed to this handoff but persisting a different id is refused
    by the strict reader — inside the resume router, well after the resolver
    could have minted a clean checkout. Releasing the retained subject on the
    strength of a record nobody has validated yet would destroy the only tree
    the retry could ever have re-measured.
    """
    from sdk.phase_handoff import safe_handoff_id

    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    gone = tmp_path / "worktrees" / "wt_gone" / "checkout"
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(
        resume_run_dir,
        worktree={
            "isolation": "per_run", "path": str(gone), "base_ref": "deadbeef",
        },
        phase_handoff=_gate_handoff(),
    )
    _write_decision(
        resume_run_dir,
        handoff_id="gate:lint:1-tampered",
        action="retry_verification",
        stem=safe_handoff_id("gate:lint:1"),
    )

    decision = resolve_resume_worktree(
        resume_from="resume_dir", output_dir=resume_run_dir, project_dir=project,
    )

    assert decision is not None
    assert decision.blocked is True
    assert decision.retained_subject is None


def test_verification_pause_without_a_retry_decision_is_passthrough(
    tmp_path: Path,
) -> None:
    """Non-regression: an undecided gate pause keeps the resolver's behaviour.

    Only a recorded ``retry_verification`` promises to re-measure this exact
    tree; a pause the operator has not decided yet may still be halted or
    continued, so it must not start refusing resumes.
    """
    project = tmp_path / "api"
    _init_repo(project)
    runs_dir = tmp_path / "workspace-orchestrator" / "runspace" / "runs"
    worktree = {
        "isolation": "per_run",
        "path": str(tmp_path / "worktrees" / "wt_gone" / "checkout"),
        "base_ref": "deadbeef",
    }
    resume_run_dir = runs_dir / "resume_dir"
    _write_meta(resume_run_dir, worktree=worktree, phase_handoff=_gate_handoff())
    _write_decision(resume_run_dir, handoff_id="gate:lint:1", action="halt")

    assert resolve_resume_worktree(
        resume_from="resume_dir", output_dir=resume_run_dir, project_dir=project,
    ) is None


def test_detect_env_retry_active_needs_both_halves() -> None:
    retry = [{"action": "retry_verification", "handoff_id": "gate:lint:1"}]

    assert detect_env_retry_active(
        prior_meta={"phase_handoff": _gate_handoff()}, decisions=retry,
    )
    # A decision for another handoff, another action, another trigger, or no
    # active pause at all are each insufficient on their own.
    assert not detect_env_retry_active(
        prior_meta={"phase_handoff": _gate_handoff("gate:tests:1")},
        decisions=retry,
    )
    assert not detect_env_retry_active(
        prior_meta={"phase_handoff": _gate_handoff()},
        decisions=[{"action": "halt", "handoff_id": "gate:lint:1"}],
    )
    assert not detect_env_retry_active(
        prior_meta={"phase_handoff": _review_handoff()}, decisions=retry,
    )
    assert not detect_env_retry_active(prior_meta={}, decisions=retry)
