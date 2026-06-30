"""run_pipeline accepting an in-memory Profile + cross context."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.cross_project.handoff import Handoff, write_handoff
from pipeline.project.profile_setup import _resolve_cross_handoff
from pipeline.runtime import CrossScope, CrossStepPolicy, PhaseStep, Profile, ProfileKind


def _profile(name: str, *phases: str) -> Profile:
    steps = tuple(
        PhaseStep(phase=p, cross=CrossStepPolicy(scope=CrossScope.PROJECT))
        for p in phases
    )
    return Profile(name=name, kind=ProfileKind.CUSTOM, steps=steps)


def _write_handoff_json(alias_dir: Path, *, subtask: str = "Do the thing") -> Path:
    """ADR 0050: the child resolves the canonical JSON handoff and
    renders the body from it. Build a valid one for the read tests."""
    alias_dir.mkdir(parents=True, exist_ok=True)
    handoff = Handoff(
        parent_run_id="20260528_000000",
        profile="advanced",
        alias="api",
        project_path=str(alias_dir / "source_checkout"),
        approved_cross_plan_path=str(alias_dir / "cross_plan.md"),
        full_cross_plan_path=str(alias_dir / "cross_plan.md"),
        full_cross_plan_markdown="# Cross-Project Plan\n\nDetails.\n",
        cross_validation_summary="Looks good.",
        cross_validation_verdict={"verdict": "APPROVED"},
        project_subtask=subtask,
        sibling_aliases=("web",),
    )
    return write_handoff(handoff, alias_dir)


class TestResolveCrossHandoff:
    def test_local_plan_source_ignores_handoff_path(self, tmp_path: Path) -> None:
        # Even with implement in the profile, plan_source=local returns "" and
        # does not touch the filesystem.
        prof = _profile("p", "implement")
        assert _resolve_cross_handoff(
            profile=prof, plan_source="local", handoff_path=None,
        ) == ""
        assert _resolve_cross_handoff(
            profile=prof, plan_source="local", handoff_path=str(tmp_path / "x.md"),
        ) == ""

    def test_invalid_plan_source_raises(self) -> None:
        prof = _profile("p", "implement")
        with pytest.raises(ValueError, match="plan_source"):
            _resolve_cross_handoff(
                profile=prof, plan_source="bogus", handoff_path=None,
            )

    def test_cross_with_implement_requires_handoff(self) -> None:
        prof = _profile("p", "implement")
        with pytest.raises(ValueError, match="requires a non-empty handoff_path"):
            _resolve_cross_handoff(
                profile=prof, plan_source="cross", handoff_path=None,
            )

    def test_cross_with_repair_requires_handoff(self) -> None:
        prof = _profile("p", "repair_changes")
        with pytest.raises(ValueError, match="requires a non-empty handoff_path"):
            _resolve_cross_handoff(
                profile=prof, plan_source="cross", handoff_path="",
            )

    def test_cross_review_only_runs_without_handoff(self) -> None:
        # ``review_changes`` + ``final_acceptance`` alone do not trigger the
        # handoff invariant — review-only projections may run without one.
        prof = _profile("p", "review_changes", "final_acceptance")
        assert _resolve_cross_handoff(
            profile=prof, plan_source="cross", handoff_path=None,
        ) == ""

    def test_cross_with_implement_missing_file_raises(self, tmp_path: Path) -> None:
        prof = _profile("p", "implement")
        with pytest.raises(FileNotFoundError):
            _resolve_cross_handoff(
                profile=prof, plan_source="cross",
                handoff_path=str(tmp_path / "missing.md"),
            )

    def test_cross_with_implement_malformed_json_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "implementation_handoff.json"
        bad.write_text("   \n  \n", encoding="utf-8")
        prof = _profile("p", "implement")
        with pytest.raises(ValueError, match="parseable"):
            _resolve_cross_handoff(
                profile=prof, plan_source="cross", handoff_path=str(bad),
            )

    def test_cross_with_implement_reads_handoff(self, tmp_path: Path) -> None:
        json_path = _write_handoff_json(tmp_path / "api", subtask="Wire the endpoint")
        prof = _profile("p", "implement")
        text = _resolve_cross_handoff(
            profile=prof, plan_source="cross", handoff_path=str(json_path),
        )
        # Body is rendered from the typed object, not the raw file text.
        assert "Wire the endpoint" in text
        assert "## Full cross plan" in text

    def test_cross_handoff_body_omits_source_project_path(self, tmp_path: Path) -> None:
        """ADR 0050 scope (4): the rendered runtime body must never carry
        the source ``project_path`` — only the child worktree path (from
        the child's own context block) may reach the runtime."""
        alias_dir = tmp_path / "api"
        json_path = _write_handoff_json(alias_dir)
        prof = _profile("p", "implement")
        text = _resolve_cross_handoff(
            profile=prof, plan_source="cross", handoff_path=str(json_path),
        )
        assert str(alias_dir / "source_checkout") not in text

    def test_cross_handoff_json_is_source_of_truth_not_markdown(
        self, tmp_path: Path,
    ) -> None:
        """The child reads the JSON, not the .md sidecar: corrupting the
        audit markdown must not change the rendered body."""
        alias_dir = tmp_path / "api"
        json_path = _write_handoff_json(alias_dir, subtask="Canonical subtask")
        (alias_dir / "implementation_handoff.md").write_text(
            "GARBAGE AUDIT TEXT", encoding="utf-8",
        )
        prof = _profile("p", "implement")
        text = _resolve_cross_handoff(
            profile=prof, plan_source="cross", handoff_path=str(json_path),
        )
        assert "Canonical subtask" in text
        assert "GARBAGE AUDIT TEXT" not in text
