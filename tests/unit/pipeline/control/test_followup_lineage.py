"""Follow-up lineage detection, header render, and resume prompt (T5)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from core.io.transcript import render_run_header
from pipeline.control.resume_context import (
    ActiveFollowupChild,
    ResumedMeta,
    ResumeIntentOptions,
    ResumeMode,
    build_checkpoint_followup_lineage,
    detect_active_followup_child,
)
from pipeline.control.resume_prompt import prompt_resume_intent


class TestCheckpointFollowupLineage:
    """Checkpoint resume of an existing follow-up child surfaces lineage."""

    def _child_meta(self, **over) -> dict:
        base = {
            "resume_mode": "followup",
            "parent_run_id": "20260606_232511",
            "parent_run_dir": "/runs/20260606_232511",
            "parent_status": "awaiting_phase_handoff",
            "status": "interrupted",
            "base_task": "do X",
            "phase_handoff": {"id": "review_changes:repair_round:1"},
        }
        base.update(over)
        return base

    def _resumed(self, meta: dict) -> ResumedMeta:
        return ResumedMeta(path=Path("/runs/child/meta.json"), meta=meta)

    def test_extracts_full_lineage(self) -> None:
        lineage = build_checkpoint_followup_lineage(
            self._resumed(self._child_meta()),
        )
        assert lineage is not None
        assert lineage.parent_run_id == "20260606_232511"
        assert lineage.parent_status == "awaiting_phase_handoff"
        assert lineage.child_status == "interrupted"
        assert lineage.active_handoff_id == "review_changes:repair_round:1"
        assert lineage.base_task == "do X"

    def test_none_when_not_followup(self) -> None:
        meta = self._child_meta()
        del meta["resume_mode"]
        assert build_checkpoint_followup_lineage(self._resumed(meta)) is None

    def test_none_without_parent_run_id(self) -> None:
        meta = self._child_meta()
        del meta["parent_run_id"]
        assert build_checkpoint_followup_lineage(self._resumed(meta)) is None

    def test_none_for_no_resumed(self) -> None:
        assert build_checkpoint_followup_lineage(None) is None

    def test_no_active_handoff_is_none_field(self) -> None:
        meta = self._child_meta()
        del meta["phase_handoff"]
        lineage = build_checkpoint_followup_lineage(self._resumed(meta))
        assert lineage is not None
        assert lineage.active_handoff_id is None


def _write_run(runs: Path, run_id: str, meta: dict) -> None:
    rd = runs / run_id
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


# ── detection ──────────────────────────────────────────────────────────


class TestDetectActiveFollowupChild:
    def test_finds_unfinished_followup_child(self, tmp_path) -> None:
        _write_run(tmp_path, "20260101_000000", {"status": "interrupted"})
        _write_run(tmp_path, "20260102_000000", {
            "status": "interrupted", "resume_mode": "followup",
            "parent_run_id": "20260101_000000",
            "phase_handoff": {"id": "review_changes:repair_round:2"},
        })
        child = detect_active_followup_child(
            parent_run_id="20260101_000000", runs_dir=tmp_path,
        )
        assert child is not None
        assert child.child_run_id == "20260102_000000"
        assert child.child_status == "interrupted"
        assert child.active_handoff_id == "review_changes:repair_round:2"

    def test_picks_newest_when_multiple(self, tmp_path) -> None:
        for ts in ("20260102_000000", "20260104_000000", "20260103_000000"):
            _write_run(tmp_path, ts, {
                "status": "interrupted", "resume_mode": "followup",
                "parent_run_id": "P",
            })
        child = detect_active_followup_child(parent_run_id="P", runs_dir=tmp_path)
        assert child is not None
        assert child.child_run_id == "20260104_000000"

    def test_terminal_child_ignored(self, tmp_path) -> None:
        _write_run(tmp_path, "20260102_000000", {
            "status": "done", "resume_mode": "followup", "parent_run_id": "P",
        })
        assert detect_active_followup_child(
            parent_run_id="P", runs_dir=tmp_path,
        ) is None

    def test_non_followup_child_ignored(self, tmp_path) -> None:
        # parent_run_id matches but it is not a follow-up (e.g. cross child).
        _write_run(tmp_path, "20260102_000000", {
            "status": "interrupted", "parent_run_id": "P",
            "project_alias": "api",
        })
        assert detect_active_followup_child(
            parent_run_id="P", runs_dir=tmp_path,
        ) is None

    def test_unrelated_parent_ignored(self, tmp_path) -> None:
        _write_run(tmp_path, "20260102_000000", {
            "status": "interrupted", "resume_mode": "followup",
            "parent_run_id": "OTHER",
        })
        assert detect_active_followup_child(
            parent_run_id="P", runs_dir=tmp_path,
        ) is None

    def test_missing_inputs_return_none(self, tmp_path) -> None:
        assert detect_active_followup_child(
            parent_run_id=None, runs_dir=tmp_path,
        ) is None
        assert detect_active_followup_child(
            parent_run_id="P", runs_dir=None,
        ) is None


# ── header render ──────────────────────────────────────────────────────


class TestFollowupHeaderRender:
    def test_lineage_rows_present(self) -> None:
        hdr = render_run_header(
            run_id="20260102_000000",
            project="/p", task="t", agents=[], profile="advanced",
            session_mode="auto", rounds=1, plan=True,
            followup_parent_run_id="20260101_000000",
            followup_parent_status="awaiting_phase_handoff",
            followup_child_status="interrupted",
            followup_active_handoff_id="review_changes:repair_round:2",
        )
        assert "follow-up of 20260101_000000" in hdr
        assert "Parent status" in hdr
        assert "awaiting_phase_handoff" in hdr
        assert "This run status" in hdr
        assert "interrupted" in hdr
        assert "Active handoff" in hdr
        assert "review_changes:repair_round:2" in hdr
        # Explicit which run is resumed (child, not parent).
        assert "Resuming" in hdr
        assert "follow-up child 20260102_000000" in hdr

    def test_non_followup_has_no_lineage_rows(self) -> None:
        hdr = render_run_header(
            run_id="r", project="/p", task="t", agents=[],
            profile="advanced", session_mode="auto", rounds=1, plan=True,
        )
        assert "Parent status" not in hdr
        assert "Active handoff" not in hdr


# ── resume prompt ──────────────────────────────────────────────────────


def _opts(**kw) -> ResumeIntentOptions:
    base = dict(
        can_checkpoint=True, can_followup=True,
        default_mode=ResumeMode.CHECKPOINT,
        parent_status="interrupted", reason="incomplete-parent",
    )
    base.update(kw)
    return ResumeIntentOptions(**base)


def _child() -> ActiveFollowupChild:
    return ActiveFollowupChild(
        child_run_id="20260102_000000",
        child_status="interrupted",
        parent_run_id="20260101_000000",
        active_handoff_id="review_changes:repair_round:2",
    )


class TestResumePromptActiveFollowup:
    def test_offers_recommended_option_and_returns_child_id(self) -> None:
        out = _FakeTTY()
        res = prompt_resume_intent(
            run_id="20260101_000000", options=_opts(),
            active_followup=_child(),
            stdin=_FakeTTY("1\n"), stdout=out,
        )
        body = out.getvalue()
        assert "Resume active follow-up 20260102_000000" in body
        assert "[recommended]" in body
        # Explicit switch target — no silent auto-switch.
        assert res.mode == ResumeMode.CHECKPOINT
        assert res.resume_run_id == "20260102_000000"

    def test_parent_checkpoint_still_selectable(self) -> None:
        res = prompt_resume_intent(
            run_id="20260101_000000", options=_opts(),
            active_followup=_child(),
            stdin=_FakeTTY("2\n"), stdout=_FakeTTY(),
        )
        # Option 2 = resume parent from checkpoint (no run-id switch).
        assert res.mode == ResumeMode.CHECKPOINT
        assert res.resume_run_id is None

    def test_exit_choice_returns_none(self) -> None:
        res = prompt_resume_intent(
            run_id="20260101_000000", options=_opts(),
            active_followup=_child(),
            stdin=_FakeTTY("4\n"), stdout=_FakeTTY(),
        )
        assert res.mode is None
        assert res.resume_run_id is None

    def test_no_active_followup_uses_classic_menu(self) -> None:
        # Without an active follow-up, the recommended option is absent.
        out = _FakeTTY()
        prompt_resume_intent(
            run_id="r", options=_opts(),
            stdin=_FakeTTY("1\n"), stdout=out,
        )
        assert "[recommended]" not in out.getvalue()
        assert "Resume from checkpoint" in out.getvalue()
