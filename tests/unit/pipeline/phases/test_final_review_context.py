"""Resolving the latest applicable review evidence from durable facts.

The resolver orders every recorded ``review_changes`` attempt by
``(round, pass)``, picks the last one that actually parsed, and reports what
that choice superseded, what never parsed, and what is merely claimed. These
tests pin the ordering rules and — just as importantly — the three things the
resolver refuses to do: treat an unparseable attempt as a verdict, let a
repair close a finding, or invent a context when there is nothing to say.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.lifecycle import default_lifecycle_context
from pipeline.phases.builtin.final_review_context import (
    FinalReviewContext,
    ReviewAttemptRef,
    render_final_review_context,
    resolve_final_review_context,
)
from pipeline.plugins import PluginConfig
from pipeline.runtime import PhaseRegistry, PipelineState

RUN_ID = "20260916_000301"


# ── Fixture builders ─────────────────────────────────────────────────────────

def _finding(fid: str = "F1", **over) -> dict:
    return {
        "id":           fid,
        "severity":     "P1",
        "title":        "null deref",
        "body":         "long reviewer prose that must not reach the context",
        "required_fix": "guard the pointer",
        "file":         "pipeline/x.py",
        "line":         42,
        **over,
    }


def _attempt(
    pass_kind: str,
    *,
    verdict: str = "REJECTED",
    approved: bool = False,
    findings: list | None = None,
    summary: str = "blockers remain",
    parse_error: str | None = None,
    repair_preceded: bool | None = None,
) -> dict:
    record = {
        "pass":          pass_kind,
        "verdict":       verdict,
        "approved":      approved,
        "clean":         approved,
        "short_summary": summary,
        "findings":      findings if findings is not None else [_finding()],
        "repair_preceded": (
            repair_preceded if repair_preceded is not None
            else pass_kind == "reverify"
        ),
    }
    if parse_error:
        record["parse_error"] = parse_error
    return record


def _session(rounds: list, **extra) -> dict:
    return {"phases": {"rounds": rounds}, **extra}


def _state(
    session: dict | None = None,
    *,
    output_dir: Path | None = None,
    run_id: str | None = RUN_ID,
    dry_run: bool = False,
    **extras,
) -> PipelineState:
    state = PipelineState(
        task="t", project_dir="/p", plugin=PluginConfig(), dry_run=dry_run,
    )
    state.output_dir = output_dir
    if run_id is not None:
        state.extras["run_id"] = run_id
    state.extras.update(extras)
    if session is not None:
        state.lifecycle_ctx = default_lifecycle_context(
            phase_registry=PhaseRegistry(),
            run_config={"session": session},
        )
    return state


def _resolve(rounds: list, **kw) -> FinalReviewContext | None:
    return resolve_final_review_context(_state(_session(rounds), **kw))


# ── Ordering and latest selection ────────────────────────────────────────────

class TestLatestAttempt:
    def test_highest_round_wins(self) -> None:
        ctx = _resolve([
            {"round": 1, "review": _attempt("review")},
            {"round": 2, "review": _attempt(
                "review", verdict="APPROVED", approved=True, findings=[],
            )},
        ])
        assert ctx is not None
        assert ctx.latest.round == 2
        assert ctx.latest.approved is True

    def test_reverify_sorts_after_review_of_the_same_round(self) -> None:
        """Ordering is by pass, not by dict insertion."""
        ctx = _resolve([{
            "round":    1,
            "reverify": _attempt(
                "reverify", verdict="APPROVED", approved=True, findings=[],
            ),
            "review":   _attempt("review"),
        }])
        assert ctx is not None
        assert ctx.latest.pass_kind == "reverify"

    def test_rounds_out_of_order_are_still_ordered(self) -> None:
        ctx = _resolve([
            {"round": 2, "review": _attempt(
                "review", verdict="APPROVED", approved=True, findings=[],
            )},
            {"round": 1, "review": _attempt("review")},
        ])
        assert ctx is not None
        assert ctx.latest.round == 2

    def test_approved_reverify_supersedes_the_rejected_review(self) -> None:
        ctx = _resolve([{
            "round":    2,
            "review":   _attempt(
                "review", findings=[_finding("F1"), _finding("F2")],
            ),
            "reverify": _attempt(
                "reverify", verdict="APPROVED", approved=True, findings=[],
            ),
        }])
        assert ctx is not None
        assert ctx.latest.pass_kind == "reverify"
        assert [(a.round, a.pass_kind) for a in ctx.superseded] == [
            (2, "review"),
        ]
        assert [f["id"] for f in ctx.superseded[0].findings] == ["F1", "F2"]

    def test_only_rejected_earlier_attempts_are_superseded(self) -> None:
        """An earlier APPROVED attempt is history, not something overruled."""
        ctx = _resolve([
            {"round": 1, "review": _attempt(
                "review", verdict="APPROVED", approved=True, findings=[],
            )},
            {"round": 2, "review": _attempt("review")},
        ])
        assert ctx is not None
        assert ctx.superseded == ()


# ── Unparseable attempts ─────────────────────────────────────────────────────

class TestInvalidAttempts:
    def test_parse_error_reverify_falls_back_to_the_same_round_review(
        self,
    ) -> None:
        """A re-verify that never parsed has no verdict to promote."""
        ctx = _resolve([{
            "round":    1,
            "review":   _attempt("review"),
            "reverify": _attempt(
                "reverify", parse_error="bad JSON at line 1", findings=[],
            ),
        }])
        assert ctx is not None
        assert (ctx.latest.round, ctx.latest.pass_kind) == (1, "review")
        assert [(a.round, a.pass_kind) for a in ctx.invalid] == [
            (1, "reverify"),
        ]
        assert ctx.superseded == ()

    def test_later_invalid_attempt_does_not_supersede(self) -> None:
        """Regression: only a *valid* later attempt can overrule a verdict."""
        ctx = _resolve([
            {"round": 1, "review": _attempt("review")},
            {"round": 2, "review": _attempt(
                "review", parse_error="truncated output", findings=[],
            )},
        ])
        assert ctx is not None
        assert ctx.latest.round == 1
        assert ctx.superseded == ()
        assert [a.round for a in ctx.invalid] == [2]

    def test_all_attempts_invalid_yields_no_context(self) -> None:
        assert _resolve([
            {"round": 1, "review": _attempt("review", parse_error="boom")},
        ]) is None

    def test_invalid_attempt_is_marked_in_the_render(self) -> None:
        ctx = _resolve([{
            "round":    1,
            "review":   _attempt("review"),
            "reverify": _attempt("reverify", parse_error="boom", findings=[]),
        }])
        assert ctx is not None
        text = render_final_review_context(ctx)
        assert "Invalid attempts (not evidence):" in text
        assert "invalid attempt: round 1 reverify — parse error" in text

    def test_valid_property_tracks_parse_error(self) -> None:
        assert ReviewAttemptRef(
            1, "review", "REJECTED", False, "s", (),
        ).valid is True
        assert ReviewAttemptRef(
            1, "review", "REJECTED", False, "s", (), parse_error="x",
        ).valid is False


# ── Unresolved findings and repair claims ────────────────────────────────────

class TestRepairIsAClaim:
    def test_repair_after_a_rejected_review_leaves_findings_unresolved(
        self,
    ) -> None:
        """No re-review ran, so the repair closes nothing."""
        ctx = _resolve([{
            "round":          1,
            "review":         _attempt("review", findings=[_finding("F1")]),
            "repair_receipt": {
                "source_phase": "review_changes",
                "repair_phase": "repair_changes",
                "fixed": [{"finding_id": "F1", "summary": "guarded"}],
            },
        }])
        assert ctx is not None
        assert [f["id"] for f in ctx.unresolved_findings] == ["F1"]
        assert ctx.repair_claim is not None
        assert ctx.repair_before_latest is None
        text = render_final_review_context(ctx)
        assert "Repair claim after this review (not re-reviewed" in text
        assert "## Repair Receipt" in text

    def test_repair_before_a_reverify_is_context_not_a_claim(self) -> None:
        ctx = _resolve([{
            "round":          1,
            "review":         _attempt("review"),
            "repair_receipt": {
                "source_phase": "review_changes",
                "repair_phase": "repair_changes",
                "fixed": [],
            },
            "reverify":       _attempt(
                "reverify", verdict="APPROVED", approved=True, findings=[],
            ),
        }])
        assert ctx is not None
        assert ctx.repair_claim is None
        assert ctx.repair_before_latest is not None
        text = render_final_review_context(ctx)
        assert "Repair before the latest review" in text
        assert "not re-reviewed" not in text

    def test_retry_round_review_after_repair_is_not_an_open_claim(
        self,
    ) -> None:
        """The operator-feedback retry round repairs first and reviews
        after, yet stores the round's ``review`` pass: its receipt was
        already reviewed, so it is context, never an unverified claim."""
        ctx = _resolve([{
            "round":          2,
            "critique":       "",
            "repair_receipt": {
                "source_phase": "review_changes",
                "repair_phase": "repair_changes",
                "fixed": [{"finding_id": "F1", "summary": "guarded"}],
            },
            "review":         _attempt(
                "review", verdict="APPROVED", approved=True, findings=[],
                repair_preceded=True,
            ),
        }])
        assert ctx is not None
        assert ctx.latest.pass_kind == "review"
        assert ctx.repair_claim is None
        assert ctx.repair_before_latest is not None
        text = render_final_review_context(ctx)
        assert "not re-reviewed" not in text
        assert "Repair before the latest review: round 2" in text

    def test_chronology_falls_back_to_the_pass_when_unrecorded(self) -> None:
        """A record written before the producer stated the chronology."""
        legacy = _attempt("review")
        legacy.pop("repair_preceded")
        ctx = _resolve([{
            "round":          1,
            "review":         legacy,
            "repair_receipt": {"source_phase": "review_changes", "fixed": []},
        }])
        assert ctx is not None
        assert ctx.latest.repair_preceded is False
        assert ctx.repair_claim is not None

    def test_approved_latest_has_no_unresolved_findings(self) -> None:
        ctx = _resolve([{
            "round":  1,
            "review": _attempt(
                "review", verdict="APPROVED", approved=True,
                findings=[_finding("F1")],
            ),
        }])
        assert ctx is not None
        assert ctx.unresolved_findings == ()

    def test_findings_are_compacted_to_the_actionable_fields(self) -> None:
        ctx = _resolve([{"round": 1, "review": _attempt("review")}])
        assert ctx is not None
        finding = ctx.unresolved_findings[0]
        assert set(finding) == {
            "id", "severity", "title", "file", "line", "required_fix",
        }
        assert "body" not in finding


# ── Operator records ─────────────────────────────────────────────────────────

_WAIVER = {
    "handoff_id":  "review_changes:repair_round:1",
    "phase":       "review_changes",
    "waiver_text": "accepted: shipping behind a flag",
    "note":        "tracked in T-42",
    "decided_at":  "2026-09-16T10:00:00+00:00",
    "findings":    [_finding("F1")],
}


class TestOperatorRecords:
    def test_waiver_from_state_extras(self) -> None:
        ctx = _resolve(
            [{"round": 1, "review": _attempt("review")}],
            phase_handoff_waiver=_WAIVER,
        )
        assert ctx is not None
        assert [o.kind for o in ctx.operator] == ["waiver"]
        record = ctx.operator[0]
        assert record.text == "accepted: shipping behind a flag"
        assert record.round == 1
        assert record.waived_finding_ids == ("id:F1",)

    def test_waiver_from_the_durable_session(self) -> None:
        state = _state(_session(
            [{"round": 1, "review": _attempt("review")}],
            phase_handoff_waiver=_WAIVER,
        ))
        ctx = resolve_final_review_context(state)
        assert ctx is not None
        assert [o.kind for o in ctx.operator] == ["waiver"]

    def test_waiver_for_another_phase_is_ignored(self) -> None:
        ctx = _resolve(
            [{"round": 1, "review": _attempt("review")}],
            phase_handoff_waiver={**_WAIVER, "phase": "implement"},
        )
        assert ctx is not None
        assert ctx.operator == ()

    def test_waived_finding_is_marked_in_the_render(self) -> None:
        ctx = _resolve(
            [{"round": 1, "review": _attempt("review")}],
            phase_handoff_waiver=_WAIVER,
        )
        assert ctx is not None
        text = render_final_review_context(ctx)
        assert (
            "(waived by operator: review_changes:repair_round:1)" in text
        )

    def test_waiver_matches_by_fingerprint_when_the_id_is_absent(self) -> None:
        """An id-less finding still matches on severity|title|file|line."""
        anon = _finding("")
        ctx = _resolve(
            [{"round": 1, "review": _attempt("review", findings=[anon])}],
            phase_handoff_waiver={**_WAIVER, "findings": [anon]},
        )
        assert ctx is not None
        assert ctx.operator[0].waived_finding_ids == (
            "fingerprint|P1|null deref|pipeline/x.py|42",
        )
        assert "(waived by operator:" in render_final_review_context(ctx)

    def test_unrelated_finding_is_not_marked_waived(self) -> None:
        ctx = _resolve(
            [{"round": 1, "review": _attempt(
                "review", findings=[_finding("F9")],
            )}],
            phase_handoff_waiver=_WAIVER,
        )
        assert ctx is not None
        assert "waived by operator" not in render_final_review_context(ctx)


class TestDecisionArtifacts:
    def _write(self, run_dir: Path, name: str, payload) -> None:
        decisions = run_dir / "phase_handoff_decisions"
        decisions.mkdir(parents=True, exist_ok=True)
        (decisions / name).write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )

    def _decision(self, **over) -> dict:
        return {
            "run_id":     RUN_ID,
            "handoff_id": "review_changes:repair_round:1",
            "phase":      "review_changes",
            "action":     "continue",
            "feedback":   "ship it, the risk is understood",
            "note":       None,
            "decided_at": "2026-09-16T11:00:00+00:00",
            **over,
        }

    def test_decisions_are_sorted_by_decided_at(self, tmp_path: Path) -> None:
        self._write(tmp_path, "b.json", self._decision(
            decided_at="2026-09-16T12:00:00+00:00", action="retry_feedback",
        ))
        self._write(tmp_path, "a.json", self._decision())
        state = _state(
            _session([{"round": 1, "review": _attempt("review")}]),
            output_dir=tmp_path,
        )
        ctx = resolve_final_review_context(state)
        assert ctx is not None
        assert [o.action for o in ctx.operator] == [
            "continue", "retry_feedback",
        ]
        text = render_final_review_context(ctx)
        assert "Operator decisions:" in text
        assert "rationale: ship it, the risk is understood" in text

    def test_other_phase_decisions_are_filtered_out(
        self, tmp_path: Path,
    ) -> None:
        self._write(tmp_path, "impl.json", self._decision(phase="implement"))
        state = _state(
            _session([{"round": 1, "review": _attempt("review")}]),
            output_dir=tmp_path,
        )
        ctx = resolve_final_review_context(state)
        assert ctx is not None
        assert ctx.operator == ()

    def test_malformed_decision_is_skipped_not_raised(
        self, tmp_path: Path,
    ) -> None:
        self._write(tmp_path, "broken.json", "{not json")
        self._write(tmp_path, "notes.txt", "ignored")
        self._write(tmp_path, "good.json", self._decision())
        state = _state(
            _session([{"round": 1, "review": _attempt("review")}]),
            output_dir=tmp_path,
        )
        ctx = resolve_final_review_context(state)
        assert ctx is not None
        assert [o.handoff_id for o in ctx.operator] == [
            "review_changes:repair_round:1",
        ]

    def test_waiver_precedes_decisions(self, tmp_path: Path) -> None:
        self._write(tmp_path, "a.json", self._decision())
        state = _state(
            _session([{"round": 1, "review": _attempt("review")}]),
            output_dir=tmp_path,
            phase_handoff_waiver=_WAIVER,
        )
        ctx = resolve_final_review_context(state)
        assert ctx is not None
        assert [o.kind for o in ctx.operator] == ["waiver", "decision"]


# ── Nothing to say ───────────────────────────────────────────────────────────

class TestAbsentContext:
    @pytest.mark.parametrize(
        "session",
        [
            pytest.param({"phases": {"rounds": []}}, id="empty-rounds"),
            pytest.param({"phases": {}}, id="no-rounds-key"),
            pytest.param({}, id="no-phases"),
            pytest.param({"phases": {"rounds": "nope"}}, id="rounds-not-list"),
        ],
    )
    def test_no_rounds_yields_none(self, session: dict) -> None:
        assert resolve_final_review_context(_state(session)) is None

    def test_rounds_without_attempts_yield_none(self) -> None:
        assert _resolve([{"round": 1, "critique": "fix it"}]) is None

    def test_attempt_without_a_verdict_is_not_an_attempt(self) -> None:
        assert _resolve([
            {"round": 1, "review": {"pass": "review", "approved": False}},
        ]) is None

    def test_missing_meta_json_yields_none(self, tmp_path: Path) -> None:
        assert resolve_final_review_context(
            _state(output_dir=tmp_path),
        ) is None

    def test_corrupt_meta_json_yields_none(self, tmp_path: Path) -> None:
        (tmp_path / "meta.json").write_text("{not json", encoding="utf-8")
        assert resolve_final_review_context(
            _state(output_dir=tmp_path),
        ) is None

    def test_no_output_dir_and_no_session_yields_none(self) -> None:
        assert resolve_final_review_context(_state()) is None

    def test_dry_run_returns_none_without_touching_the_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "meta.json").write_text(
            json.dumps(_session([{"round": 1, "review": _attempt("review")}])),
            encoding="utf-8",
        )

        def _boom(*_args, **_kwargs):
            raise AssertionError("dry run must not read the disk")

        monkeypatch.setattr(Path, "read_text", _boom)
        monkeypatch.setattr("builtins.open", _boom)
        assert resolve_final_review_context(
            _state(output_dir=tmp_path, dry_run=True),
        ) is None


# ── Fresh-process equivalence ────────────────────────────────────────────────

class TestLoadPathEquivalence:
    ROUNDS = [
        {
            "round":          1,
            "review":         _attempt("review", findings=[_finding("F1")]),
            "repair_receipt": {
                "source_phase": "review_changes",
                "repair_phase": "repair_changes",
                "fixed": [],
            },
        },
        {
            "round":    2,
            "review":   _attempt("review", findings=[_finding("F2")]),
            "reverify": _attempt(
                "reverify", verdict="APPROVED", approved=True, findings=[],
                summary="repair verified",
            ),
        },
    ]

    def _both(self, tmp_path: Path):
        session = _session(self.ROUNDS, phase_handoff_waiver=_WAIVER)
        (tmp_path / "meta.json").write_text(
            json.dumps(session), encoding="utf-8",
        )
        in_process = resolve_final_review_context(
            _state(session, output_dir=tmp_path),
        )
        # Fresh process: no lifecycle session, only the run dir on disk.
        fresh = resolve_final_review_context(_state(output_dir=tmp_path))
        assert in_process is not None and fresh is not None
        return in_process, fresh

    def test_to_dict_is_equal_across_load_paths(self, tmp_path: Path) -> None:
        in_process, fresh = self._both(tmp_path)
        assert in_process.to_dict() == fresh.to_dict()

    def test_render_is_byte_identical_across_load_paths(
        self, tmp_path: Path,
    ) -> None:
        in_process, fresh = self._both(tmp_path)
        assert render_final_review_context(in_process) == (
            render_final_review_context(fresh)
        )

    def test_to_dict_carries_no_load_path_marker(self, tmp_path: Path) -> None:
        in_process, _ = self._both(tmp_path)
        payload = json.dumps(in_process.to_dict())
        for leak in ("meta.json", "session", "source", "loaded", "lifecycle"):
            assert leak not in payload

    def test_provenance_is_run_id_and_round_pass_only(
        self, tmp_path: Path,
    ) -> None:
        in_process, _ = self._both(tmp_path)
        payload = in_process.to_dict()
        assert payload["run_id"] == RUN_ID
        assert (payload["latest"]["round"], payload["latest"]["pass"]) == (
            2, "reverify",
        )
        assert [(a["round"], a["pass"]) for a in payload["superseded"]] == [
            (1, "review"), (2, "review"),
        ]


# ── Rendered shape ───────────────────────────────────────────────────────────

class TestRender:
    def test_header_names_round_pass_verdict_and_run(self) -> None:
        ctx = _resolve([{
            "round":    2,
            "review":   _attempt("review"),
            "reverify": _attempt(
                "reverify", verdict="APPROVED", approved=True, findings=[],
            ),
        }])
        assert ctx is not None
        first = render_final_review_context(ctx).splitlines()[0]
        assert first == (
            "Latest applicable review: round 2 (post-repair re-review), "
            f"verdict APPROVED, run {RUN_ID}"
        )

    def test_review_pass_is_labelled_plainly(self) -> None:
        ctx = _resolve([{"round": 1, "review": _attempt("review")}])
        assert ctx is not None
        assert "round 1 (review), verdict REJECTED" in (
            render_final_review_context(ctx)
        )

    def test_finding_line_shape(self) -> None:
        ctx = _resolve([{"round": 1, "review": _attempt("review")}])
        assert ctx is not None
        assert (
            "- F1 [P1] null deref — pipeline/x.py:42 — guard the pointer"
            in render_final_review_context(ctx)
        )

    def test_superseded_section_lists_round_pass_verdict_and_ids(self) -> None:
        ctx = _resolve([{
            "round":    2,
            "review":   _attempt(
                "review", findings=[_finding("F1"), _finding("F2")],
            ),
            "reverify": _attempt(
                "reverify", verdict="APPROVED", approved=True, findings=[],
            ),
        }])
        assert ctx is not None
        text = render_final_review_context(ctx)
        assert "Superseded by the latest review:" in text
        assert "- round 2 review REJECTED (F1, F2)" in text

    def test_no_policy_language_about_backstops_or_readiness(self) -> None:
        """Framing is code-owned prompt text at the builder seam, not here."""
        ctx = _resolve(
            [{"round": 1, "review": _attempt("review")}],
            phase_handoff_waiver=_WAIVER,
        )
        assert ctx is not None
        lowered = render_final_review_context(ctx).lower()
        for leak in ("backstop", "readiness", "must not", "you must"):
            assert leak not in lowered
