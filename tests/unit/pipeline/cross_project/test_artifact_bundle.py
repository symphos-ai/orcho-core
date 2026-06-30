"""Tests for ``pipeline.cross_project.artifact_bundle``.

Unit-level checks on the pure helpers; the runner integration (single
Codex call, per-alias mirror, ``source="artifact_bundle"``) is covered
in ``test_runner_gate_policy`` / ``test_codex_total_only_metrics`` /
``test_cross_orchestrator``.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.cross_project.artifact_bundle import (
    build_bundle_markdown,
    build_bundle_review_prompt,
    mirror_review_to_aliases,
)


class TestBuildBundleMarkdown:
    def test_includes_task_and_cross_plan(self) -> None:
        body = build_bundle_markdown(
            task="Align email field",
            projects={
                "api": Path("/p/api"), "web": Path("/p/web"),
            },
            session_phases={},
            cross_plan_markdown="# Cross plan\nstep 1",
        )
        assert "Align email field" in body
        assert "step 1" in body
        assert "[api]" in body and "/p/api" in body
        assert "[web]" in body and "/p/web" in body

    def test_no_per_alias_summary_when_absent(self) -> None:
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases={},
            cross_plan_markdown="",
        )
        assert "final_acceptance verdict" not in body

    def test_per_alias_summary_carries_child_verdict(self) -> None:
        session_phases = {
            "projects": {
                "api": {
                    "phases": {
                        "final_acceptance": {
                            "verdict": "APPROVED",
                            "short_summary": "ok",
                        },
                    },
                },
            },
        }
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases=session_phases,
            cross_plan_markdown="",
        )
        assert "final_acceptance verdict: APPROVED" in body
        assert "summary: ok" in body

    def test_per_alias_summary_carries_release_shape_signals(self) -> None:
        """Beyond verdict + summary, the bundle must thread the
        release-shape signals that final_acceptance already produced —
        ship_ready, contract_status, release_blockers (count + ids),
        and verification_gaps (count + risks). Without these the
        cross-contract reviewer is missing half the picture each
        child already discovered."""
        session_phases = {
            "projects": {
                "api": {
                    "phases": {
                        "final_acceptance": {
                            "verdict": "REJECTED",
                            "ship_ready": False,
                            "short_summary": "schema drift",
                            "contract_status": {
                                "task_contract": "satisfied",
                                "interfaces": "broken",
                                "persistence": "safe",
                                "tests": "weak",
                            },
                            "release_blockers": [
                                {"id": "R1", "severity": "P1",
                                 "title": "interface mismatch"},
                                {"id": "R2", "severity": "P2",
                                 "title": "regression risk"},
                            ],
                            "verification_gaps": [
                                {"risk": "Schema migration order unclear",
                                 "missing_evidence": "no rollback note",
                                 "required_check": "confirm order"},
                            ],
                        },
                    },
                },
            },
        }
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases=session_phases,
            cross_plan_markdown="",
        )
        assert "final_acceptance verdict: REJECTED" in body
        assert "ship_ready: False" in body
        assert "interfaces=broken" in body
        assert "tests=weak" in body
        # release_blockers compactly: count + ids (titles stay in child).
        assert "release_blockers (2): R1, R2" in body
        # verification_gaps compactly: count + risk-prefix.
        assert "verification_gaps (1):" in body
        assert "Schema migration order unclear" in body

    def test_per_alias_summary_handles_partial_final_acceptance(self) -> None:
        """Children that produced only a partial final_acceptance entry
        (e.g. crashed mid-gate, very old run) must not crash the
        bundle builder; available fields are surfaced, missing fields
        are silently skipped."""
        session_phases = {
            "projects": {
                "api": {
                    "phases": {
                        "final_acceptance": {
                            "verdict": "APPROVED",
                            # No ship_ready, no contract_status,
                            # release_blockers as malformed list.
                            "release_blockers": [{"no_id": "x"}],
                            "verification_gaps": [],
                        },
                    },
                },
            },
        }
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases=session_phases,
            cross_plan_markdown="",
        )
        assert "final_acceptance verdict: APPROVED" in body
        assert "ship_ready" not in body
        # malformed blocker list falls back to the count-only line.
        assert "release_blockers: 1 (no ids)" in body
        # empty verification_gaps list omits the line entirely.
        assert "verification_gaps" not in body

    def test_per_alias_summary_truncates_long_blocker_lists(self) -> None:
        """Catalogue of 20 blockers should not blow the bundle out —
        first 8 ids land, the rest collapse to an ellipsis."""
        blockers = [
            {"id": f"R{i}", "severity": "P1", "title": "x"}
            for i in range(20)
        ]
        session_phases = {
            "projects": {
                "api": {
                    "phases": {
                        "final_acceptance": {
                            "verdict": "REJECTED",
                            "ship_ready": False,
                            "short_summary": "many issues",
                            "release_blockers": blockers,
                            "verification_gaps": [],
                            "contract_status": {
                                "task_contract": "incomplete",
                                "interfaces": "broken",
                                "persistence": "safe",
                                "tests": "weak",
                            },
                        },
                    },
                },
            },
        }
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases=session_phases,
            cross_plan_markdown="",
        )
        assert "release_blockers (20):" in body
        assert "R0, R1, R2, R3, R4, R5, R6, R7…" in body
        # R8+ are not inlined.
        assert "R10" not in body
        assert "R19" not in body

    def test_cross_plan_size_cap_truncates(self) -> None:
        # A bloated cross plan would defeat the cost-reduction purpose
        # of the bundle. The helper trims to the configured cap and
        # appends an explicit truncation marker so the reviewer can
        # tell something was cut.
        big_plan = "x" * 50_000
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases={},
            cross_plan_markdown=big_plan,
            cross_plan_char_cap=200,
        )
        assert "truncated for bundle review" in body
        # The cap is respected within the plan section (200 chars of
        # body + the truncation marker).
        plan_block = body.split("```", 2)[1]
        assert "x" * 200 in plan_block
        assert "x" * 201 not in plan_block

    def test_cross_plan_short_enough_is_not_truncated(self) -> None:
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases={},
            cross_plan_markdown="tiny plan",
            cross_plan_char_cap=200,
        )
        assert "tiny plan" in body
        assert "truncated for bundle review" not in body


class TestPerAliasDiffs:
    """ADR 0053 — the bundle splices each alias's actual diff so the
    cross-contract reviewer checks changed code, not just claims."""

    def test_diffs_rendered_per_alias_in_projects_order(self) -> None:
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api"), "web": Path("/p/web")},
            session_phases={},
            cross_plan_markdown="",
            alias_diffs={
                "api": "diff --git a/payload.py b/payload.py\n+ email",
                "web": "diff --git a/contracts.ts b/contracts.ts\n+ email",
            },
        )
        assert "## Per-alias diffs" in body
        assert "### [api] diff" in body
        assert "### [web] diff" in body
        assert "a/payload.py" in body
        assert "a/contracts.ts" in body
        # Order follows the projects mapping (api before web).
        assert body.index("### [api] diff") < body.index("### [web] diff")

    def test_empty_and_sentinel_diffs_skipped(self) -> None:
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api"), "web": Path("/p/web"), "db": Path("/p/db")},
            session_phases={},
            cross_plan_markdown="",
            alias_diffs={
                "api": "diff --git a/x b/x\n+ y",
                "web": "(no diff)",       # verification-only alias
                "db": "(diff unavailable)",
            },
        )
        assert "### [api] diff" in body
        assert "### [web] diff" not in body
        assert "### [db] diff" not in body

    def test_section_omitted_when_no_diffs_at_all(self) -> None:
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases={},
            cross_plan_markdown="",
            alias_diffs={"api": "(no diff)"},
        )
        assert "## Per-alias diffs" not in body

    def test_section_omitted_when_alias_diffs_none(self) -> None:
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases={},
            cross_plan_markdown="",
        )
        assert "## Per-alias diffs" not in body

    def test_diff_is_capped_with_truncation_marker(self) -> None:
        big = "x" * 50_000
        body = build_bundle_markdown(
            task="T",
            projects={"api": Path("/p/api")},
            session_phases={},
            cross_plan_markdown="",
            alias_diffs={"api": big},
            alias_diff_char_cap=200,
        )
        assert "truncated for bundle review" in body
        diff_block = body.split("```diff", 1)[1]
        assert "x" * 200 in diff_block
        assert "x" * 201 not in diff_block

    def test_checklist_grounds_findings_in_diffs_when_present(self) -> None:
        with_diff = build_bundle_markdown(
            task="T", projects={"api": Path("/p/api")},
            session_phases={}, cross_plan_markdown="",
            alias_diffs={"api": "diff --git a/x b/x\n+ y"},
        )
        without_diff = build_bundle_markdown(
            task="T", projects={"api": Path("/p/api")},
            session_phases={}, cross_plan_markdown="",
        )
        assert "Ground every finding in the per-alias diffs" in with_diff
        assert "Ground every finding in the per-alias diffs" not in without_diff


class TestBundleReviewPromptOmitsReviewTargetStrategy:
    """The artifact-bundle prompt MUST go through its own inline
    artifact surface, not the uncommitted-review surface. The latter attaches
    ``review_target_strategy`` ("review the working tree / commit /
    commit_set") which tells the runtime to scan per-project repos —
    that's exactly what the bundle eliminates.

    These tests are the contract that protects the cost-reduction
    purpose of commit 4: if anyone routes the bundle back through
    ``runtime_review_uncommitted_prompt`` the tests fail loudly.
    """

    def test_bundle_review_prompt_has_no_review_target_strategy(self) -> None:
        turn = build_bundle_review_prompt(
            build_bundle_markdown(
                task="T",
                projects={"api": Path("/p/api"), "web": Path("/p/web")},
                session_phases={},
                cross_plan_markdown="cross plan body",
            ),
            project_dir="/tmp/cwd",
        )
        prompt = turn.text
        # The review_target_strategy template renders sentences that
        # begin with "Review target mode:" (uncommitted / commit /
        # commit_set). None of those should appear in the bundle prompt.
        assert "Review target mode:" not in prompt
        # The review_json_contract IS expected — the bundle still needs
        # the typed review envelope.
        assert "review_json" in prompt or '"verdict"' in prompt

    def test_uncommitted_surface_does_carry_review_target_strategy(self) -> None:
        # Sanity: confirm the marker exists on the surface we're
        # avoiding, so the negative test above is meaningful.
        from pipeline.prompts.builders import runtime_review_uncommitted_prompt

        turn = runtime_review_uncommitted_prompt(
            focus="anything", project_dir="/tmp/cwd",
        )
        assert "Review target mode:" in turn.text


class TestMirrorReviewToAliases:
    def test_mirrors_all_aliases_and_tags_source(self) -> None:
        template = {
            "approved": True,
            "verdict": "APPROVED",
            "short_summary": "ok",
            "findings": [],
            "risks": [],
            "checks": [],
        }
        out = mirror_review_to_aliases(
            aliases=("api", "web"),
            parsed_dict_template=template,
            raw_response='{"verdict":"APPROVED"}',
            rendered="rendered md",
        )
        assert set(out) == {"api", "web"}
        for _alias, entry in out.items():
            assert entry["approved"] is True
            assert entry["verdict"] == "APPROVED"
            assert entry["source"] == "artifact_bundle"
            assert entry["rendered"] == "rendered md"
            assert entry["raw_response"] == '{"verdict":"APPROVED"}'

    def test_each_alias_has_independent_lists(self) -> None:
        template = {
            "approved": False,
            "verdict": "REJECTED",
            "short_summary": "x",
            "findings": [{"id": "F1"}],
            "risks": [],
            "checks": [],
        }
        out = mirror_review_to_aliases(
            aliases=("api", "web"),
            parsed_dict_template=template,
            raw_response="raw",
            rendered="r",
        )
        # Mutating one alias's findings must not bleed into the other.
        out["api"]["findings"].append({"id": "F2"})
        assert {f["id"] for f in out["api"]["findings"]} == {"F1", "F2"}
        assert {f["id"] for f in out["web"]["findings"]} == {"F1"}
