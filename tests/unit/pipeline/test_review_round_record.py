"""Per-attempt review sub-records inside ``session['phases']['rounds']``.

Every ``review_changes`` dispatch records its own verdict under the round
entry, keyed by the attempt's pass (``review`` / ``reverify``). These tests
pin the identity rules — where the pass comes from, what overwrites what,
and which attempts record nothing at all.
"""
from __future__ import annotations

import pytest

from pipeline.plugins import PluginConfig
from pipeline.review_round_record import (
    PASS_REVERIFY,
    PASS_REVIEW,
    REVERIFY_FLAG,
    ReviewRoundAdapter,
    write_review_round_record,
)
from pipeline.runtime import PipelineState
from pipeline.session_adapters import SessionAdapter


def _state(**kw) -> PipelineState:
    return PipelineState(task="t", project_dir="/p", plugin=PluginConfig(), **kw)


def _session() -> dict:
    return {"phases": {}}


def _review_log(
    *,
    verdict: str = "REJECTED",
    approved: bool = False,
    summary: str = "blockers remain",
    findings: list | None = None,
    **extra,
) -> dict:
    log = {
        "output":        "rendered",
        "raw_output":    "{}",
        "meta":          {"session_id": "sid-1", "continue_session": False},
        "clean":         approved,
        "approved":      approved,
        "verdict":       verdict,
        "critique":      "" if approved else "fix it",
        "short_summary": summary,
        "findings":      findings if findings is not None else [
            {"id": "F1", "severity": "blocker", "title": "null deref"},
        ],
    }
    log.update(extra)
    return log


# ── write_review_round_record ────────────────────────────────────────────────

class TestWriteReviewRoundRecord:
    def test_creates_provisional_round_entry(self) -> None:
        """The reviewer runs before RoundAdapter composes the round, so the
        first attempt of a round has no entry to attach to yet."""
        sess = _session()
        record = write_review_round_record(
            sess, round_n=1, log=_review_log(), pass_kind=PASS_REVIEW,
        )
        assert sess["phases"]["rounds"] == [{"round": 1, "review": record}]
        assert record == {
            "pass":          "review",
            "attempt":       1,
            "verdict":       "REJECTED",
            "approved":      False,
            "clean":         False,
            "repair_preceded": False,
            "short_summary": "blockers remain",
            "findings":      [
                {"id": "F1", "severity": "blocker", "title": "null deref"},
            ],
            "session_id":       "sid-1",
            "continue_session": False,
        }

    def test_attaches_to_existing_round_entry(self) -> None:
        sess = {"phases": {"rounds": [{"round": 1, "critique": "fix it"}]}}
        write_review_round_record(
            sess, round_n=1, log=_review_log(), pass_kind=PASS_REVERIFY,
        )
        entry = sess["phases"]["rounds"][0]
        assert entry["critique"] == "fix it"
        assert entry["reverify"]["pass"] == "reverify"
        assert len(sess["phases"]["rounds"]) == 1

    def test_bare_round_entry_means_the_repair_has_not_run(self) -> None:
        """The ordinary in-loop review pass reviews a pre-repair subject."""
        sess = _session()
        record = write_review_round_record(
            sess, round_n=1, log=_review_log(), pass_kind=PASS_REVIEW,
        )
        assert record["repair_preceded"] is False

    def test_repair_evidence_on_the_entry_marks_the_review_as_later(
        self,
    ) -> None:
        """The operator-feedback retry round runs ``repair_changes ->
        review_changes`` and stores the round's ``review`` pass, so the
        chronology has to come from the entry, not from the pass."""
        sess = {"phases": {"rounds": [{
            "round":          2,
            "critique":       "",
            "repair_output":  "patched",
            "repair_receipt": {"source_phase": "review_changes", "fixed": []},
        }]}}
        record = write_review_round_record(
            sess, round_n=2, log=_review_log(), pass_kind=PASS_REVIEW,
        )
        assert record["repair_preceded"] is True

    def test_reverify_is_post_repair_by_definition(self) -> None:
        """Even when the round recorded no repair evidence to inspect."""
        sess = _session()
        record = write_review_round_record(
            sess, round_n=1, log=_review_log(), pass_kind=PASS_REVERIFY,
        )
        assert record["repair_preceded"] is True

    def test_parse_error_attempt_is_recorded_as_invalid(self) -> None:
        sess = _session()
        write_review_round_record(
            sess,
            round_n=2,
            log=_review_log(findings=[], parse_error="bad JSON at line 1"),
            pass_kind=PASS_REVERIFY,
        )
        record = sess["phases"]["rounds"][0]["reverify"]
        assert record["parse_error"] == "bad JSON at line 1"
        assert record["approved"] is False

    def test_parse_error_omitted_when_absent(self) -> None:
        sess = _session()
        write_review_round_record(
            sess, round_n=1, log=_review_log(), pass_kind=PASS_REVIEW,
        )
        assert "parse_error" not in sess["phases"]["rounds"][0]["review"]

    def test_session_fields_omitted_without_meta(self) -> None:
        log = _review_log()
        log["meta"] = {}
        sess = _session()
        write_review_round_record(
            sess, round_n=1, log=log, pass_kind=PASS_REVIEW,
        )
        record = sess["phases"]["rounds"][0]["review"]
        assert "session_id" not in record
        assert "continue_session" not in record

    def test_reverify_without_review_is_recorded_alone(self) -> None:
        """An attempt is recorded from its own signal, never gated on a
        sibling attempt existing — the resolver orders what is there."""
        sess = _session()
        write_review_round_record(
            sess, round_n=1, log=_review_log(), pass_kind=PASS_REVERIFY,
        )
        entry = sess["phases"]["rounds"][0]
        assert PASS_REVIEW not in entry
        assert entry["reverify"]["attempt"] == 1

    @pytest.mark.parametrize(
        "log",
        [
            pytest.param(None, id="absent"),
            pytest.param({}, id="empty"),
            pytest.param(
                {"output": "", "meta": {}, "clean": True,
                 "skipped": "no uncommitted changes"},
                id="skipped-no-uncommitted",
            ),
            pytest.param(
                {"verdict": "REJECTED", "approved": False,
                 "skipped": "implement delivery incomplete"},
                id="skipped-with-verdict",
            ),
            pytest.param(
                {"output": "x", "meta": {}, "clean": False},
                id="no-verdict",
            ),
        ],
    )
    def test_no_verdict_writes_nothing(self, log) -> None:
        sess = _session()
        assert write_review_round_record(
            sess, round_n=1, log=log, pass_kind=PASS_REVIEW,
        ) is None
        assert sess == {"phases": {}}

    def test_missing_round_n_raises(self) -> None:
        with pytest.raises(ValueError, match="round_n"):
            write_review_round_record(
                _session(), round_n=None, log=_review_log(),
                pass_kind=PASS_REVIEW,
            )

    def test_unknown_pass_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown review pass"):
            write_review_round_record(
                _session(), round_n=1, log=_review_log(), pass_kind="audit",
            )


# ── ReviewRoundAdapter ───────────────────────────────────────────────────────

class TestReviewRoundAdapter:
    def test_satisfies_session_adapter_protocol(self) -> None:
        assert isinstance(ReviewRoundAdapter(), SessionAdapter)

    def test_review_and_reverify_are_separate_attempts_in_one_round(
        self,
    ) -> None:
        """The post-repair re-verify pass is identified by the runner flag
        alone — never by "a ``review`` key is already there"."""
        s = _state()
        sess = _session()
        adapter = ReviewRoundAdapter()

        s.phase_log["review_changes"] = _review_log()
        adapter.write("review_changes", s, sess, round_n=1)

        s.phase_log["review_changes"] = _review_log(
            verdict="APPROVED", approved=True, summary="repair verified",
            findings=[],
        )
        s.extras[REVERIFY_FLAG] = True
        adapter.write("review_changes", s, sess, round_n=1)

        assert len(sess["phases"]["rounds"]) == 1
        entry = sess["phases"]["rounds"][0]
        assert entry["review"]["verdict"] == "REJECTED"
        assert entry["review"]["pass"] == "review"
        assert entry["reverify"]["verdict"] == "APPROVED"
        assert entry["reverify"]["pass"] == "reverify"

    def test_same_attempt_twice_overwrites_one_key(self) -> None:
        """Without the runner flag both dispatches are the same attempt:
        one ``review`` key, last write wins, no phantom ``reverify``."""
        s = _state()
        sess = _session()
        adapter = ReviewRoundAdapter()

        s.phase_log["review_changes"] = _review_log()
        adapter.write("review_changes", s, sess, round_n=1)
        s.phase_log["review_changes"] = _review_log(
            verdict="APPROVED", approved=True, summary="second look",
            findings=[],
        )
        adapter.write("review_changes", s, sess, round_n=1)

        entry = sess["phases"]["rounds"][0]
        assert sorted(entry) == ["review", "round"]
        assert entry["review"]["short_summary"] == "second look"
        assert PASS_REVERIFY not in entry

    def test_reverify_flag_only_counts_when_true(self) -> None:
        s = _state()
        s.extras[REVERIFY_FLAG] = "yes"
        s.phase_log["review_changes"] = _review_log()
        sess = _session()
        ReviewRoundAdapter().write("review_changes", s, sess, round_n=1)
        assert PASS_REVIEW in sess["phases"]["rounds"][0]

    def test_skip_adapter_marker_writes_nothing(self) -> None:
        s = _state()
        s.phase_log["review_changes"] = _review_log()
        s.phase_log["rounds_pending"] = {"_skip_adapter": True}
        sess = _session()
        ReviewRoundAdapter().write("review_changes", s, sess, round_n=1)
        assert sess == {"phases": {}}

    def test_no_round_context_is_a_noop(self) -> None:
        """``delivery_audit`` / ``code_review`` run review_changes outside a
        review/repair loop: no round exists, and inventing one would
        fabricate loop state."""
        s = _state()
        s.phase_log["review_changes"] = _review_log()
        sess = _session()
        ReviewRoundAdapter().write("review_changes", s, sess, round_n=None)
        assert sess == {"phases": {}}

    def test_never_writes_a_review_changes_session_phase(self) -> None:
        """``_fsm_checkpoint`` saves any key under ``session['phases']`` as a
        completed checkpoint phase with a loop cursor; a ``review_changes``
        key there would change review-loop resume."""
        s = _state()
        s.phase_log["review_changes"] = _review_log()
        sess = _session()
        ReviewRoundAdapter().write("review_changes", s, sess, round_n=1)
        assert set(sess["phases"]) == {"rounds"}


# ── Through the real loop runner ─────────────────────────────────────────────

class TestReverifyThroughTheLoopRunner:
    """End-to-end over ``run_profile``: the pass identity is produced by the
    runner's own flag, not by anything the tests stage by hand."""

    def _run(self, verdicts: list[bool]) -> dict:
        from pipeline.lifecycle import default_lifecycle_context
        from pipeline.runtime import (
            LoopStep,
            PhaseHandoffPolicy,
            PhaseHandoffType,
            PhaseRegistry,
            PhaseStep,
            Profile,
            run_profile,
        )
        from pipeline.session_adapters import default_session_adapter_registry

        calls = {"i": 0}

        def review_changes(state: PipelineState) -> PipelineState:
            idx = calls["i"]
            calls["i"] = idx + 1
            approved = verdicts[idx]
            state.phase_log["review_changes"] = {
                "approved":      approved,
                "clean":         approved,
                "verdict":       "APPROVED" if approved else "REJECTED",
                "critique":      "" if approved else f"critique-{idx + 1}",
                "short_summary": f"pass {idx + 1}",
                "findings":      [] if approved else [{"id": f"F{idx + 1}"}],
                "meta":          {},
            }
            return state

        def repair_changes(state: PipelineState) -> PipelineState:
            # Mirrors the real handler's clean-review short-circuit: a round
            # whose review approved records a critique-only round entry.
            critique = state.phase_log["review_changes"]["critique"]
            if not critique:
                state.phase_log["repair_changes"] = {"skipped": "review clean"}
                state.phase_log["rounds_pending"] = {"critique": ""}
                return state
            state.phase_log["repair_changes"] = {"output": "fixed"}
            state.phase_log["rounds_pending"] = {
                "critique": critique, "repair_output": "fixed",
            }
            return state

        reg = PhaseRegistry()
        reg.register("review_changes", review_changes)
        reg.register("repair_changes", repair_changes)

        session = _session()
        ctx = default_lifecycle_context(
            phase_registry=reg,
            session_adapter_registry=default_session_adapter_registry(),
            run_config={"session": session},
        )
        profile = Profile(
            name="review_loop",
            steps=(
                LoopStep(
                    steps=(
                        PhaseStep(
                            phase="review_changes",
                            handoff=PhaseHandoffPolicy(
                                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                            ),
                        ),
                        PhaseStep(phase="repair_changes"),
                    ),
                    until="review_changes.clean",
                    max_rounds=1,
                    round_extras_key="repair_round",
                ),
            ),
        )
        run_profile(profile, _state(), reg, ctx=ctx)
        return session

    def test_repair_fixed_it_records_review_then_reverify(self) -> None:
        session = self._run([False, True])
        rounds = session["phases"]["rounds"]
        assert len(rounds) == 1
        entry = rounds[0]
        assert entry["round"] == 1
        assert entry["repair_output"] == "fixed"
        assert entry["review"]["verdict"] == "REJECTED"
        assert entry["review"]["findings"] == [{"id": "F1"}]
        assert entry["reverify"]["verdict"] == "APPROVED"
        assert entry["reverify"]["findings"] == []
        # Attempts live in the round entry, never as a checkpointable phase.
        assert "review_changes" not in session["phases"]

    def test_clean_first_pass_records_only_the_review_attempt(self) -> None:
        session = self._run([True])
        entry = session["phases"]["rounds"][0]
        assert entry["review"]["verdict"] == "APPROVED"
        assert PASS_REVERIFY not in entry
        assert "repair_output" not in entry

    def test_still_rejected_after_repair_keeps_both_verdicts(self) -> None:
        session = self._run([False, False])
        entry = session["phases"]["rounds"][0]
        assert entry["review"]["verdict"] == "REJECTED"
        assert entry["reverify"]["verdict"] == "REJECTED"
        assert entry["reverify"]["short_summary"] == "pass 2"
