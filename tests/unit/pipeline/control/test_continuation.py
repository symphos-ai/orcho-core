from __future__ import annotations

from pathlib import Path

from pipeline.control.continuation import resolve_continuation_decision


def _meta(path: Path) -> dict:
    return {
        "status": "halted",
        "halt_reason": "final_acceptance_rejected",
        "worktree": {"path": str(path), "isolation": "per_run"},
        "phases": {"final_acceptance": {"verdict": "REJECTED"}},
    }


def test_rejected_retained_worktree_is_followup_only(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # A tiny git repository proves the resolver uses the physical retained
    # subject rather than an artifact or transcript convention.
    import subprocess
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / "changed.txt").write_text("change\n")

    decision = resolve_continuation_decision(
        run_id="parent", meta=_meta(worktree), parent_run_dir=tmp_path,
    )

    assert decision.continuation_subject == "retained_change"
    assert decision.recommended_next_action == "start_followup"
    assert decision.allowed_intents == ("followup", "exit")
    assert decision.requires_operator_comment is True
    assert decision.checkpoint_resumable is False
    assert decision.diff_source == "worktree"
    assert decision.blocked is False


def test_artifact_only_correction_is_blocked_without_plan_fallback(tmp_path: Path) -> None:
    (tmp_path / "diff.patch").write_text("diff --git a/a b/a\n")
    decision = resolve_continuation_decision(
        run_id="parent", meta=_meta(tmp_path / "missing"), parent_run_dir=tmp_path,
    )

    assert decision.continuation_subject == "retained_change"
    assert decision.blocked is True
    assert decision.diff_source == "artifact"
    assert decision.recommended_next_action == "start_followup"


def test_off_isolation_parent_is_blocked_even_when_source_is_dirty(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "unrelated.txt").write_text("user change\n")
    meta = _meta(source)
    meta["project"] = str(source)
    meta["worktree"]["isolation"] = "off"

    decision = resolve_continuation_decision(
        run_id="parent", meta=meta, parent_run_dir=tmp_path,
    )

    assert decision.continuation_subject == "retained_change"
    assert decision.blocked is True
    assert decision.diff_source == "none"
    assert "not an isolated" in decision.reason


def test_fix_and_no_diff_are_retained_change_candidates(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / "changed.txt").write_text("change\n")

    for reason in ("commit_decision_fix", "final_acceptance_no_diff"):
        meta = _meta(worktree)
        meta["halt_reason"] = reason
        decision = resolve_continuation_decision(
            run_id=reason, meta=meta, parent_run_dir=tmp_path,
        )
        assert decision.continuation_subject == "retained_change"
        assert decision.blocked is False


def test_clean_correction_worktree_is_blocked(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)

    decision = resolve_continuation_decision(
        run_id="parent", meta=_meta(worktree), parent_run_dir=tmp_path,
    )

    assert decision.blocked is True
    assert decision.diff_source == "none"
    assert "clean" in decision.reason
