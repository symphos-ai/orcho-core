"""SDK contract tests for the generic phase-handoff decision service.

Pins the decision-transition semantics: artifact format and path,
exact-payload idempotency, ``halt`` flips meta.status and clears the
active payload, ``halt`` is idempotent against the persisted artifact
even after the active handoff is cleared, ``retry_feedback`` requires a
non-empty feedback string, ``handoff_id`` must match the active payload,
chosen ``action`` must be in the active ``available_actions``.

Plus a focused test for ``PhaseStep`` direct construction with both
``human_review`` and ``handoff`` set — the loader's mutual-exclusion
check is covered in ``tests/unit/pipeline/profiles/test_profile_loader``,
this one pins the same invariant at the dataclass layer for callers that
construct ``PhaseStep`` programmatically.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.runtime import (
    HumanReview,
    PhaseHandoffPolicy,
    PhaseHandoffType,
    PhaseStep,
    ReviewTiming,
)
from sdk import (
    InvalidPhaseHandoffState,
    PhaseHandoffDecision,
    RunNotFound,
    load_active_phase_handoff,
    load_phase_handoff_decisions,
    phase_handoff_decide,
    safe_handoff_id,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _seed_run(
    runs_dir: Path,
    run_id: str,
    *,
    status: str = "awaiting_phase_handoff",
    handoff_id: str = "validate_plan:plan_round:2",
    phase: str = "validate_plan",
    available_actions: list[str] | None = None,
    extra_meta: dict | None = None,
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    meta: dict = {
        "task": "demo task",
        "project": "/some/proj",
        "status": status,
        "phases": {
            "plan": [{"approved": True}],
            "validate_plan": [{"approved": False, "critique": "needs work"}],
        },
    }
    if status == "awaiting_phase_handoff":
        meta["phase_handoff"] = {
            "id": handoff_id,
            "phase": phase,
            "type": "human_feedback_on_reject",
            "trigger": "rejected",
            "verdict": "REJECTED",
            "approved": False,
            "round": 2,
            "loop_max_rounds": 2,
            "available_actions": (
                available_actions
                if available_actions is not None
                else ["continue", "retry_feedback", "halt"]
            ),
            "artifacts": {"plan_file": str(run_dir / "plan.md")},
            "last_output": "critique text",
        }
    if extra_meta:
        meta.update(extra_meta)
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    return run_dir


# ── safe_handoff_id ────────────────────────────────────────────────────────


class TestSafeHandoffId:
    def test_deterministic(self) -> None:
        a = safe_handoff_id("validate_plan:plan_round:2")
        b = safe_handoff_id("validate_plan:plan_round:2")
        assert a == b

    def test_distinct_ids_get_distinct_safe_names(self) -> None:
        a = safe_handoff_id("validate_plan:plan_round:2")
        b = safe_handoff_id("validate_plan:plan_round:3")
        assert a != b

    def test_slug_collision_resistance_via_hash(self) -> None:
        """Two ids whose sanitised slug would collide must still produce
        distinct safe names because the hash is taken over the *original*
        id, not the sanitised slug."""
        a = safe_handoff_id("foo:bar:baz")
        b = safe_handoff_id("foo/bar/baz")
        # Slugs collapse to the same alnum_form "foo_bar_baz" but the
        # appended hash differs because the raw input differs.
        assert a != b

    def test_filesystem_safe(self) -> None:
        s = safe_handoff_id("validate_plan:plan_round:2")
        assert "/" not in s
        assert ":" not in s
        # Only alnum / underscore from sanitisation, plus hex hash.
        assert all(c.isalnum() or c == "_" for c in s)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            safe_handoff_id("")


# ── happy paths ────────────────────────────────────────────────────────────


class TestDecideHappyPath:
    def test_continue_writes_artifact_without_touching_meta(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_120000_aaaaaa")

        result = phase_handoff_decide(
            "20260520_120000_aaaaaa",
            "validate_plan:plan_round:2",
            "continue",
            note="manual override",
            runs_dir=runs,
            cwd=None,
        )

        assert isinstance(result, PhaseHandoffDecision)
        assert result.run_id == "20260520_120000_aaaaaa"
        assert result.handoff_id == "validate_plan:plan_round:2"
        assert result.phase == "validate_plan"
        assert result.action == "continue"
        assert result.feedback is None
        assert result.note == "manual override"
        assert result.decided_at

        # Status untouched — decide does NOT spawn resume.
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["status"] == "awaiting_phase_handoff"
        # Active payload still in place.
        assert meta["phase_handoff"]["id"] == "validate_plan:plan_round:2"

        artifact_dir = run_dir / "phase_handoff_decisions"
        files = list(artifact_dir.iterdir())
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["action"] == "continue"
        assert payload["handoff_id"] == "validate_plan:plan_round:2"
        assert payload["phase"] == "validate_plan"

    def test_retry_feedback_writes_artifact_with_feedback(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_130000_bbbbbb")

        result = phase_handoff_decide(
            "20260520_130000_bbbbbb",
            "validate_plan:plan_round:2",
            "retry_feedback",
            feedback="please add an alternatives section",
            runs_dir=runs,
            cwd=None,
        )
        assert result.action == "retry_feedback"
        assert result.feedback == "please add an alternatives section"

        # Status remains awaiting_phase_handoff; resume is a separate call.
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["status"] == "awaiting_phase_handoff"

    def test_continue_with_waiver_writes_artifact_with_feedback(
        self, tmp_path: Path,
    ) -> None:
        """``continue_with_waiver`` behaves like ``continue`` for meta
        transition (status stays awaiting; resume is separate) but the
        SDK requires a non-empty operator verdict and persists it as the
        artifact feedback — that verdict is the durable waiver body."""
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(
            runs,
            "20260520_125000_wwwwww",
            available_actions=[
                "continue", "retry_feedback", "halt", "continue_with_waiver",
            ],
        )

        result = phase_handoff_decide(
            "20260520_125000_wwwwww",
            "validate_plan:plan_round:2",
            "continue_with_waiver",
            feedback="accepted risk: ship with the documented gap",
            runs_dir=runs,
            cwd=None,
        )
        assert result.action == "continue_with_waiver"
        assert result.feedback == "accepted risk: ship with the documented gap"

        # Status untouched — like continue, decide does NOT spawn resume.
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["status"] == "awaiting_phase_handoff"
        assert meta["phase_handoff"]["id"] == "validate_plan:plan_round:2"

        artifact_dir = run_dir / "phase_handoff_decisions"
        payload = json.loads(next(artifact_dir.iterdir()).read_text())
        assert payload["action"] == "continue_with_waiver"
        assert payload["feedback"] == "accepted risk: ship with the documented gap"

    def test_halt_writes_artifact_and_flips_meta_to_halted(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_140000_cccccc")

        result = phase_handoff_decide(
            "20260520_140000_cccccc",
            "validate_plan:plan_round:2",
            "halt",
            note="not salvageable",
            runs_dir=runs,
            cwd=None,
        )
        assert result.action == "halt"

        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["status"] == "halted"
        assert meta["halt_reason"] == "phase_handoff_halt"
        assert meta["halted_at"] == result.decided_at
        # Active payload cleared — halted run must not surface in pending queues.
        assert "phase_handoff" not in meta

    def test_halt_repairs_interrupted_active_handoff(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_140500_torn00")
        meta = json.loads((run_dir / "meta.json").read_text())
        meta["status"] = "interrupted"
        meta["halt_reason"] = "interrupted"
        (run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )

        result = phase_handoff_decide(
            "20260520_140500_torn00",
            "validate_plan:plan_round:2",
            "halt",
            note="operator chose halt after interrupted resume",
            runs_dir=runs,
            cwd=None,
        )

        meta = json.loads((run_dir / "meta.json").read_text())
        assert result.action == "halt"
        assert meta["status"] == "halted"
        assert meta["halt_reason"] == "phase_handoff_halt"
        assert "phase_handoff" not in meta

    def test_halt_finalizes_evidence_bundle_on_disk(
        self, tmp_path: Path,
    ) -> None:
        # A run halted from a phase-handoff pause is terminal. It must
        # land on disk with the same bundle shape as a ``done`` run, so
        # post-mortem tooling never has to special-case halted runs and
        # fall back to raw ``events.jsonl``. The pipeline subprocess
        # exited at the pause without writing the bundle; the SDK halt
        # path now finalizes it as part of the transition.
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_141000_aaaaaa")

        phase_handoff_decide(
            "20260520_141000_aaaaaa",
            "validate_plan:plan_round:2",
            "halt",
            runs_dir=runs,
            cwd=None,
        )

        evidence_path = run_dir / "evidence.json"
        assert evidence_path.is_file(), (
            "halt must finalize evidence.json so halted runs are not "
            "terminal-but-bundleless"
        )
        bundle = json.loads(evidence_path.read_text())
        # The bundle reflects the halted state — full v1 if the
        # collector could compose one, REA-0 placeholder otherwise. Both
        # carry status=halted and a stable run identifier so the
        # operator can correlate.
        assert bundle.get("status") == "halted"
        assert bundle.get("run_id") == "20260520_141000_aaaaaa"


# ── load helpers ───────────────────────────────────────────────────────────


class TestLoaders:
    def test_load_active_returns_payload(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_150000_dddddd")
        active = load_active_phase_handoff(
            "20260520_150000_dddddd", runs_dir=runs, cwd=None,
        )
        assert isinstance(active, dict)
        assert active["id"] == "validate_plan:plan_round:2"
        assert active["phase"] == "validate_plan"

    def test_load_active_returns_none_when_absent(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_151000_eeeeee", status="running")
        # No phase_handoff in meta for running runs.
        active = load_active_phase_handoff(
            "20260520_151000_eeeeee", runs_dir=runs, cwd=None,
        )
        assert active is None

    def test_load_decisions_returns_persisted_artifacts(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_160000_ffffff")

        phase_handoff_decide(
            "20260520_160000_ffffff",
            "validate_plan:plan_round:2",
            "retry_feedback",
            feedback="add edge cases",
            runs_dir=runs,
            cwd=None,
        )
        loaded = load_phase_handoff_decisions(
            "20260520_160000_ffffff", runs_dir=runs, cwd=None,
        )
        assert len(loaded) == 1
        assert loaded[0].action == "retry_feedback"
        assert loaded[0].feedback == "add edge cases"

    def test_load_decisions_returns_empty_when_no_dir(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_161000_gggggg")
        assert load_phase_handoff_decisions(
            "20260520_161000_gggggg", runs_dir=runs, cwd=None,
        ) == []

    def test_multiple_sequential_handoffs_create_separate_artifacts(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_170000_hhhhhh")

        # First decision on round 2.
        phase_handoff_decide(
            "20260520_170000_hhhhhh",
            "validate_plan:plan_round:2",
            "retry_feedback",
            feedback="round 2 critique",
            runs_dir=runs,
            cwd=None,
        )

        # Simulate the orchestrator pausing on a fresh handoff for round 3
        # by rewriting meta.phase_handoff in place. The active id changes;
        # the round-2 artifact remains on disk.
        meta = json.loads((run_dir / "meta.json").read_text())
        meta["phase_handoff"]["id"] = "validate_plan:plan_round:3"
        meta["phase_handoff"]["round"] = 3
        (run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )

        phase_handoff_decide(
            "20260520_170000_hhhhhh",
            "validate_plan:plan_round:3",
            "halt",
            note="give up",
            runs_dir=runs,
            cwd=None,
        )

        loaded = load_phase_handoff_decisions(
            "20260520_170000_hhhhhh", runs_dir=runs, cwd=None,
        )
        assert len(loaded) == 2
        handoff_ids = {d.handoff_id for d in loaded}
        assert handoff_ids == {
            "validate_plan:plan_round:2",
            "validate_plan:plan_round:3",
        }


# ── exact-payload idempotency ──────────────────────────────────────────────


class TestExactPayloadIdempotency:
    def test_same_payload_returns_unchanged_artifact(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_180000_iiiiii")

        first = phase_handoff_decide(
            "20260520_180000_iiiiii",
            "validate_plan:plan_round:2",
            "retry_feedback",
            feedback="add tests",
            note="from UI",
            runs_dir=runs,
            cwd=None,
        )
        artifact = (
            run_dir / "phase_handoff_decisions"
            / f"{safe_handoff_id('validate_plan:plan_round:2')}.json"
        )
        first_mtime = artifact.stat().st_mtime_ns

        second = phase_handoff_decide(
            "20260520_180000_iiiiii",
            "validate_plan:plan_round:2",
            "retry_feedback",
            feedback="add tests",
            note="from UI",
            runs_dir=runs,
            cwd=None,
        )
        assert first == second  # decided_at preserved bit-for-bit
        # No artifact rewrite — same mtime.
        assert artifact.stat().st_mtime_ns == first_mtime

    def test_different_action_for_same_handoff_id_raises(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_190000_jjjjjj")

        phase_handoff_decide(
            "20260520_190000_jjjjjj",
            "validate_plan:plan_round:2",
            "continue",
            runs_dir=runs,
            cwd=None,
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_190000_jjjjjj",
                "validate_plan:plan_round:2",
                "halt",
                runs_dir=runs,
                cwd=None,
            )
        assert "already decided" in str(exc.value)

    def test_different_feedback_for_same_action_raises(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_200000_kkkkkk")

        phase_handoff_decide(
            "20260520_200000_kkkkkk",
            "validate_plan:plan_round:2",
            "retry_feedback",
            feedback="first feedback",
            runs_dir=runs,
            cwd=None,
        )
        with pytest.raises(InvalidPhaseHandoffState):
            phase_handoff_decide(
                "20260520_200000_kkkkkk",
                "validate_plan:plan_round:2",
                "retry_feedback",
                feedback="second feedback",
                runs_dir=runs,
                cwd=None,
            )

    def test_different_note_for_same_action_raises(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_201000_llllll")

        phase_handoff_decide(
            "20260520_201000_llllll",
            "validate_plan:plan_round:2",
            "continue",
            note="first note",
            runs_dir=runs,
            cwd=None,
        )
        with pytest.raises(InvalidPhaseHandoffState):
            phase_handoff_decide(
                "20260520_201000_llllll",
                "validate_plan:plan_round:2",
                "continue",
                note="different note",
                runs_dir=runs,
                cwd=None,
            )


# ── halt-after-halt idempotency ─────────────────────────────────────────────


class TestHaltAfterHaltIdempotency:
    def test_replay_halt_after_active_cleared_succeeds(
        self, tmp_path: Path,
    ) -> None:
        """After the first ``halt`` flips status and clears the active
        payload, a repeat exact-payload ``halt`` for the same handoff_id
        must still succeed against the persisted artifact alone — no
        active ``meta.phase_handoff`` is required for idempotent replay.
        """
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_210000_mmmmmm")

        first = phase_handoff_decide(
            "20260520_210000_mmmmmm",
            "validate_plan:plan_round:2",
            "halt",
            note="terminal",
            runs_dir=runs,
            cwd=None,
        )
        # Replay: meta.status is now 'halted', no active phase_handoff.
        replay = phase_handoff_decide(
            "20260520_210000_mmmmmm",
            "validate_plan:plan_round:2",
            "halt",
            note="terminal",
            runs_dir=runs,
            cwd=None,
        )
        assert first == replay

    def test_replay_halt_with_different_note_raises(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_211000_nnnnnn")

        phase_handoff_decide(
            "20260520_211000_nnnnnn",
            "validate_plan:plan_round:2",
            "halt",
            note="first",
            runs_dir=runs,
            cwd=None,
        )
        with pytest.raises(InvalidPhaseHandoffState):
            phase_handoff_decide(
                "20260520_211000_nnnnnn",
                "validate_plan:plan_round:2",
                "halt",
                note="second",
                runs_dir=runs,
                cwd=None,
            )


# ── error contracts ────────────────────────────────────────────────────────


class TestErrorContracts:
    def test_unknown_run_raises_run_not_found(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        with pytest.raises(RunNotFound):
            phase_handoff_decide(
                "does_not_exist",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )

    def test_invalid_action_raises_value_error(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_220000_oooooo")
        with pytest.raises(ValueError) as exc:
            phase_handoff_decide(
                "20260520_220000_oooooo",
                "validate_plan:plan_round:2",
                "yolo",
                runs_dir=runs,
                cwd=None,
            )
        assert "continue" in str(exc.value)
        assert "retry_feedback" in str(exc.value)
        assert "halt" in str(exc.value)

    def test_empty_handoff_id_raises(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_221000_pppppp")
        with pytest.raises(ValueError):
            phase_handoff_decide(
                "20260520_221000_pppppp",
                "",
                "continue",
                runs_dir=runs,
                cwd=None,
            )

    def test_retry_feedback_without_feedback_raises_value_error(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_230000_qqqqqq")
        with pytest.raises(ValueError, match="retry_feedback requires"):
            phase_handoff_decide(
                "20260520_230000_qqqqqq",
                "validate_plan:plan_round:2",
                "retry_feedback",
                feedback=None,
                runs_dir=runs,
                cwd=None,
            )

    def test_retry_feedback_with_blank_feedback_raises(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_231000_rrrrrr")
        with pytest.raises(ValueError, match="retry_feedback requires"):
            phase_handoff_decide(
                "20260520_231000_rrrrrr",
                "validate_plan:plan_round:2",
                "retry_feedback",
                feedback="   ",
                runs_dir=runs,
                cwd=None,
            )

    def test_continue_with_waiver_without_feedback_raises_value_error(
        self, tmp_path: Path,
    ) -> None:
        """The waiver action carries the operator's accepted-risk verdict;
        an empty verdict is a contract violation (nothing to inject into
        downstream gates)."""
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(
            runs,
            "20260520_232000_wwwwww",
            available_actions=[
                "continue", "retry_feedback", "halt", "continue_with_waiver",
            ],
        )
        with pytest.raises(ValueError, match="continue_with_waiver requires"):
            phase_handoff_decide(
                "20260520_232000_wwwwww",
                "validate_plan:plan_round:2",
                "continue_with_waiver",
                feedback="   ",
                runs_dir=runs,
                cwd=None,
            )

    def test_decide_on_running_run_raises_invalid_state(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_240000_ssssss", status="running")
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_240000_ssssss",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "running" in str(exc.value) or "not awaiting" in str(exc.value)

    def test_wrong_handoff_id_raises_invalid_state(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(
            runs,
            "20260520_250000_tttttt",
            handoff_id="validate_plan:plan_round:2",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_250000_tttttt",
                "validate_plan:plan_round:99",  # stale UI
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "active handoff id" in str(exc.value)

    def test_action_not_in_available_actions_raises(
        self, tmp_path: Path,
    ) -> None:
        """The decision API enforces the persisted ``available_actions``
        set — any canonical action absent from it must be refused. We
        seed a synthetic narrower set here so the negative path is
        exercised regardless of what the active policy publishes."""
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(
            runs,
            "20260520_260000_uuuuuu",
            available_actions=["continue", "halt"],
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_260000_uuuuuu",
                "validate_plan:plan_round:2",
                "retry_feedback",
                feedback="please retry",
                runs_dir=runs,
                cwd=None,
            )
        assert "available_actions" in str(exc.value)


# ── input validation (P1 / P2 follow-up) ───────────────────────────────────


class TestInputValidation:
    """``find_run("")`` resolves to "newest run" by SDK convention.
    Decision API must not borrow that behaviour — empty run_id, empty
    handoff_id, non-string note → ValueError before touching the
    filesystem, so a bad MCP/UI call cannot mutate a different run."""

    def test_empty_run_id_raises_without_touching_filesystem(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        # Seed a run that would otherwise be "the newest".
        run_dir = _seed_run(runs, "20260520_in0001_aaaaaa")
        with pytest.raises(ValueError, match="run_id"):
            phase_handoff_decide(
                "",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        # No artifact written to the would-be-newest run.
        assert not (run_dir / "phase_handoff_decisions").exists()

    def test_non_string_run_id_raises(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_in0002_bbbbbb")
        with pytest.raises(ValueError, match="run_id"):
            phase_handoff_decide(
                None,  # type: ignore[arg-type]
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )

    def test_non_string_note_raises(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_in0003_cccccc")
        with pytest.raises(ValueError, match="note"):
            phase_handoff_decide(
                "20260520_in0003_cccccc",
                "validate_plan:plan_round:2",
                "continue",
                note=123,  # type: ignore[arg-type]
                runs_dir=runs,
                cwd=None,
            )

    def test_note_none_is_accepted(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(runs, "20260520_in0004_dddddd")
        result = phase_handoff_decide(
            "20260520_in0004_dddddd",
            "validate_plan:plan_round:2",
            "continue",
            note=None,
            runs_dir=runs,
            cwd=None,
        )
        assert result.note is None


# ── strict artifact shape (P2 follow-up) ───────────────────────────────────


class TestStrictArtifactShape:
    """The strict reader must reject every persisted-schema violation,
    not just bad JSON / wrong action / id mismatch. Mirrors the input
    validation surface so a manually-edited artifact cannot weaken the
    audit contract on the next decide."""

    def _decision_path(
        self, run_dir: Path, handoff_id: str,
    ) -> Path:
        return (
            run_dir / "phase_handoff_decisions"
            / f"{safe_handoff_id(handoff_id)}.json"
        )

    def _seed_artifact(
        self, run_dir: Path, handoff_id: str, payload: dict,
    ) -> None:
        artifact = self._decision_path(run_dir, handoff_id)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_non_string_note_in_artifact_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_sa0001_aaaaaa")
        self._seed_artifact(run_dir, "validate_plan:plan_round:2", {
            "run_id": "20260520_sa0001_aaaaaa",
            "handoff_id": "validate_plan:plan_round:2",
            "phase": "validate_plan",
            "action": "continue",
            "feedback": None,
            "note": 123,
            "decided_at": "2026-05-20T12:00:00+00:00",
        })
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_sa0001_aaaaaa",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "note" in str(exc.value)

    def test_non_string_feedback_in_artifact_rejected(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_sa0002_bbbbbb")
        self._seed_artifact(run_dir, "validate_plan:plan_round:2", {
            "run_id": "20260520_sa0002_bbbbbb",
            "handoff_id": "validate_plan:plan_round:2",
            "phase": "validate_plan",
            "action": "retry_feedback",
            "feedback": ["not", "a", "string"],
            "note": None,
            "decided_at": "2026-05-20T12:00:00+00:00",
        })
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_sa0002_bbbbbb",
                "validate_plan:plan_round:2",
                "retry_feedback",
                feedback="anything",
                runs_dir=runs,
                cwd=None,
            )
        assert "feedback" in str(exc.value)

    def test_empty_phase_in_artifact_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_sa0003_cccccc")
        self._seed_artifact(run_dir, "validate_plan:plan_round:2", {
            "run_id": "20260520_sa0003_cccccc",
            "handoff_id": "validate_plan:plan_round:2",
            "phase": "",
            "action": "continue",
            "feedback": None,
            "note": None,
            "decided_at": "2026-05-20T12:00:00+00:00",
        })
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_sa0003_cccccc",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "phase" in str(exc.value)

    def test_non_string_decided_at_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_sa0004_dddddd")
        self._seed_artifact(run_dir, "validate_plan:plan_round:2", {
            "run_id": "20260520_sa0004_dddddd",
            "handoff_id": "validate_plan:plan_round:2",
            "phase": "validate_plan",
            "action": "continue",
            "feedback": None,
            "note": None,
            "decided_at": ["2026-05-20T12:00:00+00:00"],
        })
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_sa0004_dddddd",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "decided_at" in str(exc.value)

    def test_missing_run_id_in_artifact_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_sa0005_eeeeee")
        self._seed_artifact(run_dir, "validate_plan:plan_round:2", {
            # run_id missing
            "handoff_id": "validate_plan:plan_round:2",
            "phase": "validate_plan",
            "action": "continue",
            "feedback": None,
            "note": None,
            "decided_at": "2026-05-20T12:00:00+00:00",
        })
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_sa0005_eeeeee",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "run_id" in str(exc.value)


# ── available_actions contract (P1) ────────────────────────────────────────


class TestAvailableActionsContract:
    """``available_actions`` is the runtime-produced source of action
    availability. A missing key, wrong type, or empty list is a runtime
    contract violation, not a license for the SDK to accept any
    canonical action — especially dangerous for ``human_feedback_always``
    on approved, where ``retry_feedback`` must not pass."""

    def test_missing_available_actions_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_aa0001_aaaaaa")
        meta = json.loads((run_dir / "meta.json").read_text())
        meta["phase_handoff"].pop("available_actions")
        (run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_aa0001_aaaaaa",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "available_actions" in str(exc.value)

    def test_non_list_available_actions_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_aa0002_bbbbbb")
        meta = json.loads((run_dir / "meta.json").read_text())
        meta["phase_handoff"]["available_actions"] = "continue,halt"
        (run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_aa0002_bbbbbb",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "available_actions" in str(exc.value)
        assert "list" in str(exc.value)

    def test_empty_available_actions_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(
            runs, "20260520_aa0003_cccccc", available_actions=[],
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_aa0003_cccccc",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "empty" in str(exc.value)

    def test_non_string_action_entries_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_aa0004_dddddd")
        meta = json.loads((run_dir / "meta.json").read_text())
        meta["phase_handoff"]["available_actions"] = ["continue", 42, ""]
        (run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_aa0004_dddddd",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "non-string" in str(exc.value) or "empty entry" in str(exc.value)

    def test_human_feedback_always_approved_accepts_retry_feedback(
        self, tmp_path: Path,
    ) -> None:
        """``human_feedback_always`` on approved now publishes the full
        action set so the human can disagree with the reviewer agent's
        approval. The decision API must accept ``retry_feedback`` on
        that payload (it would have been refused under the old
        ``[continue, halt]`` shape)."""
        runs = tmp_path / "runs"
        runs.mkdir()
        _seed_run(
            runs,
            "20260520_aa0005_eeeeee",
            available_actions=["continue", "retry_feedback", "halt"],
        )
        phase_handoff_decide(
            "20260520_aa0005_eeeeee",
            "validate_plan:plan_round:2",
            "retry_feedback",
            feedback="plan is too broad; only delete users",
            runs_dir=runs,
            cwd=None,
        )


# ── active payload completeness (P1 follow-up) ─────────────────────────────


class TestActivePayloadCompleteness:
    """``meta.status == awaiting_phase_handoff`` is a promise that
    ``meta.phase_handoff`` carries a complete payload. Missing/malformed
    fields are runtime contract violations, not silent skip cases."""

    def test_missing_phase_handoff_payload_rejected(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_ap0001_aaaaaa")
        meta = json.loads((run_dir / "meta.json").read_text())
        meta.pop("phase_handoff")
        (run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_ap0001_aaaaaa",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "phase_handoff" in str(exc.value)

    def test_missing_phase_in_payload_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_ap0002_bbbbbb")
        meta = json.loads((run_dir / "meta.json").read_text())
        meta["phase_handoff"].pop("phase")
        (run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_ap0002_bbbbbb",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "phase" in str(exc.value)


# ── persisted id idempotency check (P2a) ───────────────────────────────────


class TestPersistedIdIdempotency:
    """Exact-payload idempotency includes ``handoff_id`` and ``run_id``.
    A corrupted artifact whose persisted ids don't match the path is
    treated as terminal (rejected), not papered over."""

    def _decision_path(
        self, run_dir: Path, handoff_id: str,
    ) -> Path:
        return (
            run_dir / "phase_handoff_decisions"
            / f"{safe_handoff_id(handoff_id)}.json"
        )

    def test_persisted_handoff_id_mismatch_rejected(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_id0001_aaaaaa")
        artifact = self._decision_path(run_dir, "validate_plan:plan_round:2")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps({
                "run_id": "20260520_id0001_aaaaaa",
                "handoff_id": "validate_plan:plan_round:999",  # mismatch
                "phase": "validate_plan",
                "action": "continue",
                "feedback": None,
                "note": None,
                "decided_at": "2026-05-20T12:00:00+00:00",
            }) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_id0001_aaaaaa",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "corrupted" in str(exc.value) or "persists" in str(exc.value)

    def test_persisted_run_id_mismatch_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_id0002_bbbbbb")
        artifact = self._decision_path(run_dir, "validate_plan:plan_round:2")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps({
                "run_id": "SOMEONE_ELSES_RUN",  # mismatch
                "handoff_id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "action": "continue",
                "feedback": None,
                "note": None,
                "decided_at": "2026-05-20T12:00:00+00:00",
            }) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_id0002_bbbbbb",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "corrupted" in str(exc.value) or "persists" in str(exc.value)


# ── corrupt artifact terminal (P2b) ────────────────────────────────────────


class TestCorruptArtifactTerminal:
    """A decision artifact is the audit record of the *exact* human
    instruction used on resume. A present-but-corrupt artifact must be
    terminal (manual repair) — never silently overwritten by a new
    decision."""

    def _decision_path(
        self, run_dir: Path, handoff_id: str,
    ) -> Path:
        return (
            run_dir / "phase_handoff_decisions"
            / f"{safe_handoff_id(handoff_id)}.json"
        )

    def test_invalid_json_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_co0001_aaaaaa")
        artifact = self._decision_path(run_dir, "validate_plan:plan_round:2")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{ not json", encoding="utf-8")
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_co0001_aaaaaa",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "JSON" in str(exc.value) or "audit" in str(exc.value)

    def test_non_object_payload_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_co0002_bbbbbb")
        artifact = self._decision_path(run_dir, "validate_plan:plan_round:2")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
        with pytest.raises(InvalidPhaseHandoffState):
            phase_handoff_decide(
                "20260520_co0002_bbbbbb",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )

    def test_invalid_action_rejected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_co0003_cccccc")
        artifact = self._decision_path(run_dir, "validate_plan:plan_round:2")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps({
                "run_id": "20260520_co0003_cccccc",
                "handoff_id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "action": "fly_to_the_moon",
                "feedback": None,
                "note": None,
                "decided_at": "2026-05-20T12:00:00+00:00",
            }) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(InvalidPhaseHandoffState) as exc:
            phase_handoff_decide(
                "20260520_co0003_cccccc",
                "validate_plan:plan_round:2",
                "continue",
                runs_dir=runs,
                cwd=None,
            )
        assert "invalid action" in str(exc.value)

    def test_list_loader_skips_corrupt_artifact(self, tmp_path: Path) -> None:
        """The lenient list loader is for UI/audit display and must not
        crash on a single corrupt file — it skips it. The strict decide
        path above is the gatekeeper for new transitions."""
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _seed_run(runs, "20260520_co0004_dddddd")
        decisions_dir = run_dir / "phase_handoff_decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        # One bad file ...
        (decisions_dir / "broken.json").write_text(
            "{ not json", encoding="utf-8",
        )
        # ... alongside a real, well-formed decision.
        phase_handoff_decide(
            "20260520_co0004_dddddd",
            "validate_plan:plan_round:2",
            "continue",
            runs_dir=runs,
            cwd=None,
        )
        loaded = load_phase_handoff_decisions(
            "20260520_co0004_dddddd", runs_dir=runs, cwd=None,
        )
        assert len(loaded) == 1
        assert loaded[0].action == "continue"


# ── dataclass-layer guard (carry-over from Phase 0 reviewer note) ──────────


class TestPhaseStepHandoffHumanReviewMutex:
    """Loader rejection of ``handoff`` + ``human_review`` is covered in
    ``tests/unit/pipeline/profiles/test_profile_loader.py``. This pins the
    same invariant at the dataclass layer for callers that construct
    ``PhaseStep`` programmatically (tests, plugins, internal helpers)
    instead of going through the JSON loader."""

    def test_direct_construction_with_both_raises(self) -> None:
        with pytest.raises(
            ValueError,
            match="handoff and human_review are mutually exclusive",
        ):
            PhaseStep(
                phase="validate_plan",
                human_review=HumanReview(timing=ReviewTiming.AFTER),
                handoff=PhaseHandoffPolicy(
                    type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
                ),
            )

    def test_handoff_only_is_accepted(self) -> None:
        step = PhaseStep(
            phase="validate_plan",
            handoff=PhaseHandoffPolicy(
                type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
            ),
        )
        assert step.handoff is not None
        assert step.human_review is None

    def test_human_review_only_is_accepted(self) -> None:
        step = PhaseStep(
            phase="validate_plan",
            human_review=HumanReview(timing=ReviewTiming.AFTER),
        )
        assert step.human_review is not None
        assert step.handoff is None


# ── record_decision_artifact + synthetic waiver (ADR 0073) ──────────────────


class TestRecordDecisionArtifact:
    """The internal idempotent artifact writer used by the auto-waiver path.

    Distinct from the public ``phase_handoff_decide``: it does not require an
    active pause when ``skip_status_guard=True`` (the auto-waiver fires while
    the run is still ``running``), but keeps the same exact-payload idempotency
    / conflict semantics.
    """

    def _decision(self, run_id: str, feedback: str = "auto-waived") -> PhaseHandoffDecision:
        return PhaseHandoffDecision(
            run_id=run_id,
            handoff_id="implement:implement_handoff:1",
            phase="implement",
            action="continue_with_waiver",
            feedback=feedback,
            note=None,
            decided_at="2026-06-04T00:00:00+00:00",
        )

    def test_skip_status_guard_writes_on_running_run(self, tmp_path: Path) -> None:
        from sdk.phase_handoff import record_decision_artifact

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        dec = self._decision("run1")
        out = record_decision_artifact(
            run_dir, dec.handoff_id, dec, skip_status_guard=True,
        )
        assert out.action == "continue_with_waiver"
        files = list((run_dir / "phase_handoff_decisions").glob("*.json"))
        assert len(files) == 1

    def test_idempotent_exact_payload(self, tmp_path: Path) -> None:
        from sdk.phase_handoff import record_decision_artifact

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        dec = self._decision("run1")
        first = record_decision_artifact(run_dir, dec.handoff_id, dec)
        second = record_decision_artifact(run_dir, dec.handoff_id, dec)
        assert first == second
        files = list((run_dir / "phase_handoff_decisions").glob("*.json"))
        assert len(files) == 1

    def test_divergent_payload_conflicts(self, tmp_path: Path) -> None:
        from sdk.phase_handoff import record_decision_artifact

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        record_decision_artifact(
            run_dir, "implement:implement_handoff:1", self._decision("run1", "a"),
        )
        with pytest.raises(
            InvalidPhaseHandoffState, match="exact-payload idempotent",
        ):
            record_decision_artifact(
                run_dir,
                "implement:implement_handoff:1",
                self._decision("run1", "b"),
            )

    def test_status_guard_enforced_when_not_skipped(self, tmp_path: Path) -> None:
        from sdk.phase_handoff import record_decision_artifact

        runs = tmp_path / "runs"
        run_dir = _seed_run(runs, "20260604_000000_aaaaaa", status="running")
        dec = self._decision("20260604_000000_aaaaaa")
        with pytest.raises(
            InvalidPhaseHandoffState, match="not awaiting a phase handoff",
        ):
            record_decision_artifact(
                run_dir, dec.handoff_id, dec, skip_status_guard=False,
            )


class TestWriteSyntheticWaiverDecision:
    def test_creates_continue_with_waiver_artifact(self, tmp_path: Path) -> None:
        from sdk.phase_handoff import write_synthetic_waiver_decision

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        dec = write_synthetic_waiver_decision(
            run_dir,
            run_id="run1",
            handoff_id="implement:implement_handoff:1",
            phase="implement",
            feedback="auto-waived: criteria not closed after repair",
        )
        assert dec.action == "continue_with_waiver"
        assert dec.phase == "implement"
        assert dec.decided_at  # defaulted
        files = list((run_dir / "phase_handoff_decisions").glob("*.json"))
        assert len(files) == 1

    def test_empty_feedback_raises(self, tmp_path: Path) -> None:
        from sdk.phase_handoff import write_synthetic_waiver_decision

        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        with pytest.raises(ValueError, match="non-empty"):
            write_synthetic_waiver_decision(
                run_dir,
                run_id="run1",
                handoff_id="implement:implement_handoff:1",
                phase="implement",
                feedback="   ",
            )


# ── shared active/terminal action contract (T4 alignment) ──────────────────


class TestSharedActionContract:
    """The active (resumable) vs terminal (halt) split is sourced from the
    shared ``run_state.HandoffAction`` transition enum, so the SDK decide
    path and the project resume path classify one contract, not two."""

    def test_active_resume_actions_match_run_state_enum(self) -> None:
        from pipeline.run_state.types import HandoffAction
        from sdk.phase_handoff import _ACTIVE_RESUME_ACTIONS

        assert {a.value for a in HandoffAction} == _ACTIVE_RESUME_ACTIONS
        # Halt is terminal — never an active resume action.
        assert "halt" not in _ACTIVE_RESUME_ACTIONS

    def test_valid_actions_is_active_plus_halt(self) -> None:
        # The full wire set is the three active transitions plus the
        # terminal halt; nothing more, nothing less.
        from sdk.phase_handoff import _ACTIVE_RESUME_ACTIONS, _VALID_ACTIONS

        assert _ACTIVE_RESUME_ACTIONS | {"halt"} == _VALID_ACTIONS

    def test_handoff_decisions_classifier_agrees(self) -> None:
        from pipeline.control.handoff_decisions import _VALID_DECISION_ACTIONS
        from sdk.phase_handoff import _VALID_ACTIONS

        # Project-side classifier and SDK wire-validation share the 4-value set.
        assert _VALID_DECISION_ACTIONS == _VALID_ACTIONS
