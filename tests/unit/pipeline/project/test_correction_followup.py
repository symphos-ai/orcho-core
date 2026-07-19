"""Unit coverage for the auto-correction follow-up loop (ADR 0070).

Two surfaces:

* :mod:`pipeline.project.correction_followup` — correction-task synthesis,
  the ``commit_decision_fix`` predicate, and the operator-gated driver
  loop (``run_pipeline`` injected, so no real pipeline runs here).
* :func:`pipeline.project.finalization.finalize_with_terminal_output` — the
  banner regression: a ``halted`` run must NOT print the green
  ``Pipeline complete`` header.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pipeline.project.correction_followup import (
    CorrectionDecision,
    compose_correction_context,
    compose_correction_task,
    decide_correction_followup,
    drive_correction_followups,
    is_correction_fix_halt,
)
from pipeline.project.finalization import (
    FinalizationContext,
    _halt_banner,
    finalize_with_terminal_output,
)


def _fix_halt_session(**phases: Any) -> dict[str, Any]:
    return {
        "status": "halted",
        "halt_reason": "commit_decision_fix",
        "phases": phases,
    }


# ── is_correction_fix_halt ────────────────────────────────────────────────


class TestIsCorrectionFixHalt:
    def test_true_on_commit_decision_fix(self) -> None:
        assert is_correction_fix_halt(_fix_halt_session()) is True

    def test_false_on_done(self) -> None:
        assert is_correction_fix_halt({"status": "done"}) is False

    def test_false_on_other_halt_reason(self) -> None:
        assert is_correction_fix_halt(
            {"status": "halted", "halt_reason": "commit_decision_halt"},
        ) is False

    def test_false_on_none(self) -> None:
        assert is_correction_fix_halt(None) is False


# ── compose_correction_task ───────────────────────────────────────────────


class TestComposeCorrectionTask:
    def test_task_uses_structured_gaps_and_summary(self) -> None:
        session = _fix_halt_session(
            final_acceptance={
                "short_summary": "Mandatory gate is red.",
                "verification_gaps": [
                    {
                        "risk": "Could ship without a green gate.",
                        "missing_evidence": "pytest exited 1.",
                        "required_check": "Make the non-e2e gate green.",
                    },
                ],
                "critique": "## full critique markdown (should be ignored)",
            },
        )
        task = compose_correction_task(session)

        assert "Correction follow-up" in task  # framing header
        assert "Summary: Mandatory gate is red." in task
        assert "Make the non-e2e gate green." in task
        assert "Missing evidence: pytest exited 1." in task
        # Structured gaps win: critique markdown is not appended.
        assert "full critique markdown" not in task

    def test_task_stays_bounded_without_gaps(self) -> None:
        session = _fix_halt_session(
            final_acceptance={
                "short_summary": "Reviewer found a blocker.",
                "verification_gaps": [],
                "critique": "## Release gate\n\nblocked: do the thing\n" * 80,
            },
        )
        task = compose_correction_task(session)

        assert "Reviewer found a blocker." in task
        assert "Inspect the correction context artifact" in task
        assert "blocked: do the thing" not in task
        assert len(task) < 600

    def test_generic_fallback_without_any_detail(self) -> None:
        task = compose_correction_task(_fix_halt_session())

        assert "Inspect the correction context artifact" in task
        # Never empty — the round always has a task to run.
        assert task.strip()

    def test_context_keeps_full_critique(self) -> None:
        session = _fix_halt_session(
            final_acceptance={
                "short_summary": "Reviewer found a blocker.",
                "verification_gaps": [],
                "critique": "## Release gate\n\nblocked: do the thing",
            },
        )

        context = compose_correction_context(session)

        assert "Reviewer found a blocker." in context
        assert "## Full Final Acceptance Critique" in context
        assert "blocked: do the thing" in context


# ── decide_correction_followup (pure decision) ────────────────────────────


class TestDecideCorrectionFollowup:
    def test_continue_when_prev_is_fix_halt(self) -> None:
        decision = decide_correction_followup(
            _fix_halt_session(
                final_acceptance={"short_summary": "round0 reject"},
            ),
        )
        assert isinstance(decision, CorrectionDecision)
        assert decision.should_continue is True
        # Derived metadata is populated for the round.
        assert "round0 reject" in decision.task
        assert decision.context

    def test_stop_when_prev_not_fix_halt(self) -> None:
        # The loop stops the moment the latest child is no longer a
        # commit_decision_fix halt (approve / apply / skip / halt / approved).
        decision = decide_correction_followup({"status": "done"})
        assert decision.should_continue is False
        assert decision.task == ""
        assert decision.context == ""

    def test_stop_on_other_halt_reason(self) -> None:
        decision = decide_correction_followup(
            {"status": "halted", "halt_reason": "commit_decision_halt"},
        )
        assert decision.should_continue is False

    def test_stop_on_none_session(self) -> None:
        assert decide_correction_followup(None).should_continue is False

    def test_pure_decider_is_filesystem_free(self, tmp_path: Path) -> None:
        # Passing a context_path must NOT write anything — only the driver's
        # IO does that. The path is merely referenced in the task text.
        context_path = tmp_path / "correction_context.md"
        decision = decide_correction_followup(
            _fix_halt_session(
                final_acceptance={"short_summary": "reject"},
            ),
            context_path=context_path,
        )
        assert decision.should_continue is True
        assert "correction_context.md" in decision.task
        assert not context_path.exists()

    def test_task_is_bounded_and_omits_raw_critique(self) -> None:
        huge_critique = "## Final acceptance\n\n" + "blocked detail\n" * 100
        decision = decide_correction_followup(
            _fix_halt_session(
                final_acceptance={
                    "short_summary": "Release evidence invalid.",
                    "verification_gaps": [],
                    "critique": huge_critique,
                },
            ),
        )
        # Concise task: summary in, full raw critique/release-summary out.
        assert "Release evidence invalid." in decision.task
        assert "blocked detail" not in decision.task
        # Full critique stays in the (separate) context body.
        assert "blocked detail" in decision.context


# ── drive_correction_followups ────────────────────────────────────────────


class _ScriptedPipeline:
    """Returns a scripted sequence of sessions and records every call."""

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = sessions
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._sessions[len(self.calls) - 1]


def _mint_sequence() -> Any:
    counter = {"n": 0}

    def _mint() -> str:
        counter["n"] += 1
        return f"round{counter['n']}"

    return _mint


# ── fixed-point guard helpers (ADR 0098) ──────────────────────────────────


def _blocker(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "R1",
        "severity": "P0",
        "title": "Mandatory gate is red",
        "file": "pipeline/foo.py",
        "body": "prose",
        "why_blocks_release": "would ship without a green gate",
    }
    base.update(overrides)
    return base


def _rejected_fix_halt(
    *,
    release_blockers: list[dict[str, Any]] | None = None,
    engine_gaps: list[dict[str, Any]] | None = None,
    gate_rerun: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A ``commit_decision_fix`` halt whose final acceptance rejected w/ blockers.

    The loop keeps going on ``commit_decision_fix`` (so without the guard a
    fresh round would run); the rejected-with-blockers final-acceptance record
    is what the fixed-point evaluator reads.
    """
    fa: dict[str, Any] = {
        "verdict": "REJECTED",
        "ship_ready": False,
        "approved": False,
        "release_blockers": release_blockers or [],
        "verification_gaps": [],
    }
    if engine_gaps is not None:
        fa["engine_backstop"] = {
            "reason": "required_receipts_unproven",
            "gaps": engine_gaps,
        }
    phases: dict[str, Any] = {"final_acceptance": fa}
    if gate_rerun is not None:
        phases["correction_triage"] = {"gate_rerun_execution": gate_rerun}
    return {
        "status": "halted",
        "halt_reason": "commit_decision_fix",
        "phases": phases,
    }


def _write_command_receipt(
    run_dir: Path,
    command: str,
    *,
    exit_code: int = 0,
    assertions: list[dict[str, Any]] | None = None,
    detail: str = "",
    git: dict[str, Any] | None = None,
    dependencies: list[dict[str, Any]] | None = None,
    duration_s: float = 0.0,
) -> None:
    """Write a minimal command-receipt JSON, mirroring write_command_receipt."""
    from pipeline.evidence.verification_receipt import COMMAND_RECEIPTS_DIRNAME

    receipts_dir = run_dir / COMMAND_RECEIPTS_DIRNAME
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / f"{command}.json").write_text(
        json.dumps({
            "kind": "verification_command",
            "command": command,
            "env": "core-local",
            "exit_code": exit_code,
            "assertions": assertions or [],
            "detail": detail,
            # Noise fields that must NOT enter the fingerprint.
            "duration_s": duration_s,
            "stdout_tail": "some run output",
            "git": git or {
                "checkout_head": None,
                "baseline_head": None,
                "changed_files_fingerprint": None,
            },
            "dependencies": dependencies or [],
        }),
        encoding="utf-8",
    )


class _ArtifactPipeline:
    """Scripted pipeline that also writes a round's ``diff.patch`` artifact.

    ``diffs`` maps a zero-based round index to the patch text the round should
    leave in its ``output_dir`` — mirroring what ``capture_run_diff`` writes, so
    the driver's progress readers see real files. ``receipts`` maps a round index
    to a list of ``(command, receipt_kwargs)`` written into that round's
    command-receipt dir, mirroring the gate-rerun / auto-run materializer.
    """

    def __init__(
        self,
        sessions: list[dict[str, Any]],
        *,
        diffs: dict[int, str] | None = None,
        receipts: dict[int, list[tuple[str, dict[str, Any]]]] | None = None,
    ) -> None:
        self._sessions = sessions
        self._diffs = diffs or {}
        self._receipts = receipts or {}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        idx = len(self.calls)
        self.calls.append(kwargs)
        out_dir = kwargs["output_dir"]
        diff = self._diffs.get(idx)
        if diff is not None:
            (out_dir / "diff.patch").write_text(diff, encoding="utf-8")
        for command, receipt_kwargs in self._receipts.get(idx, []):
            _write_command_receipt(out_dir, command, **receipt_kwargs)
        return self._sessions[idx]


class TestDriveCorrectionFollowups:
    def test_loops_until_non_fix_status(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        parent_dir = runs / "parent"
        parent_dir.mkdir(parents=True)

        prev = _fix_halt_session(
            final_acceptance={"short_summary": "round0 reject"},
        )
        # Round 1 still rejects (operator picked fix again), round 2 is done.
        pipeline = _ScriptedPipeline([
            _fix_halt_session(
                final_acceptance={"short_summary": "round1 reject"},
            ),
            {"status": "done"},
        ])
        announcements: list[str] = []

        final = drive_correction_followups(
            prev_session=prev,
            prev_output_dir=parent_dir,
            base_task="original task",
            stable_kwargs={"project_dir": "/proj", "profile_name": "advanced"},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=announcements.append,
        )

        assert final == {"status": "done"}
        assert len(pipeline.calls) == 2
        assert len(announcements) == 2

        first, second = pipeline.calls
        # First correction round parents off the original run.
        assert first["followup_parent_run_id"] == "parent"
        assert first["followup_parent_run_dir"] == str(parent_dir)
        assert first["followup_parent_status"] == "halted"
        assert first["followup_base_task"] == "original task"
        assert first["resume_mode"] == "followup"
        assert first["resume_from"] is None
        assert first["hypothesis_enabled"] is False
        # Stable kwargs are threaded through unchanged...
        assert first["project_dir"] == "/proj"
        # ...except profile_name: the correction follow-up pins the internal
        # ``correction`` profile and the parent's "advanced" never leaks (T3).
        assert first["profile_name"] == "correction"
        assert second["profile_name"] == "correction"
        assert "advanced" not in {
            first["profile_name"], second["profile_name"],
        }
        # Every announce names the correction profile.
        assert all("correction" in msg for msg in announcements)
        # Task is synthesized from the parent's rejection.
        assert "round0 reject" in first["task"]
        assert "correction_context.md" in first["task"]
        assert (runs / "round1" / "correction_context.md").is_file()
        assert "round0 reject" in (
            runs / "round1" / "correction_context.md"
        ).read_text(encoding="utf-8")
        # Fresh sibling run dir was minted and created.
        assert first["output_dir"] == runs / "round1"
        assert (runs / "round1").is_dir()

        # Second round parents off the FIRST correction round.
        assert second["followup_parent_run_id"] == "round1"
        assert second["followup_parent_run_dir"] == str(runs / "round1")
        assert "round1 reject" in second["task"]
        assert (runs / "round2" / "correction_context.md").is_file()
        assert second["output_dir"] == runs / "round2"

    def test_full_critique_is_artifact_not_child_task(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        parent_dir = runs / "parent"
        parent_dir.mkdir(parents=True)
        huge_critique = "## Final acceptance\n\n" + "blocked detail\n" * 100
        prev = _fix_halt_session(
            final_acceptance={
                "short_summary": "Release evidence invalid.",
                "verification_gaps": [],
                "critique": huge_critique,
            },
        )
        pipeline = _ScriptedPipeline([{"status": "done"}])

        drive_correction_followups(
            prev_session=prev,
            prev_output_dir=parent_dir,
            base_task="original task",
            stable_kwargs={},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        task = pipeline.calls[0]["task"]
        context = (runs / "round1" / "correction_context.md").read_text(
            encoding="utf-8",
        )
        assert "Release evidence invalid." in task
        assert "blocked detail" not in task
        assert "correction_context.md" in task
        assert "blocked detail" in context

    def test_pins_orcho_run_id_to_minted_id_each_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A parent run launched with --run-id / ambient $ORCHO_RUN_ID must
        # NOT leak its stale id into follow-up rounds: bootstrap ranks
        # $ORCHO_RUN_ID above the minted dir name, so without pinning the
        # round's session_ts diverges from output_dir.name (P1).
        monkeypatch.setenv("ORCHO_RUN_ID", "stale-parent-id")
        runs = tmp_path / "runs"
        parent_dir = runs / "parent"
        parent_dir.mkdir(parents=True)

        seen_env: list[str | None] = []

        def _pipeline(**kwargs: Any) -> dict[str, Any]:
            # Env seen INSIDE the round must equal the minted dir name.
            seen_env.append(os.environ.get("ORCHO_RUN_ID"))
            return (
                _fix_halt_session(final_acceptance={"short_summary": "r"})
                if len(seen_env) == 1
                else {"status": "done"}
            )

        drive_correction_followups(
            prev_session=_fix_halt_session(
                final_acceptance={"short_summary": "round0 reject"},
            ),
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={},
            run_pipeline=_pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        # Each round saw its own minted id, not the stale parent id.
        assert seen_env == ["round1", "round2"]
        # The override never escapes the loop.
        assert os.environ.get("ORCHO_RUN_ID") == "stale-parent-id"

    def test_no_followup_when_prev_not_fix_halt(self, tmp_path: Path) -> None:
        parent_dir = tmp_path / "runs" / "parent"
        parent_dir.mkdir(parents=True)
        pipeline = _ScriptedPipeline([])

        final = drive_correction_followups(
            prev_session={"status": "done"},
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        assert final == {"status": "done"}
        assert pipeline.calls == []


# ── fixed-point guard (ADR 0098) ──────────────────────────────────────────


class TestFixedPointGuard:
    @staticmethod
    def _seed(tmp_path: Path, *, diff: str | None = None) -> tuple[Path, Path]:
        runs = tmp_path / "runs"
        parent_dir = runs / "parent"
        parent_dir.mkdir(parents=True)
        if diff is not None:
            (parent_dir / "diff.patch").write_text(diff, encoding="utf-8")
        return runs, parent_dir

    def test_repeated_blockers_no_progress_halts_non_converging(
        self, tmp_path: Path,
    ) -> None:
        # Parent and child reject on the SAME blocker with an identical diff and
        # no fresh receipts: the loop stops with a non-converging outcome and
        # never spawns round 2.
        runs, parent_dir = self._seed(tmp_path, diff="PATCH-A")
        prev = _rejected_fix_halt(release_blockers=[_blocker()])
        # round1 would itself be another fix-halt (operator picked fix) — the
        # guard, not the status, is what stops the loop.
        child = _rejected_fix_halt(release_blockers=[_blocker()])
        pipeline = _ArtifactPipeline([child], diffs={0: "PATCH-A"})
        announcements: list[str] = []

        final = drive_correction_followups(
            prev_session=prev,
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=announcements.append,
        )

        # Only one round ran — the guard stopped the loop.
        assert len(pipeline.calls) == 1
        assert final is child
        # The child was re-marked as a halted non-converging run.
        assert final["status"] == "halted"
        assert final["halt_reason"] == "correction_not_converging"
        block = final["correction_fixed_point"]
        assert block["parent_run_id"] == "parent"
        assert block["child_run_id"] == "round1"
        assert block["repeated"]  # the shared blocker identity is recorded
        assert "retry with new instructions" in block["suggested_actions"]
        # meta.json was rewritten with the new outcome.
        meta = json.loads(
            (runs / "round1" / "meta.json").read_text(encoding="utf-8"),
        )
        assert meta["halt_reason"] == "correction_not_converging"
        assert meta["correction_fixed_point"]["child_run_id"] == "round1"
        # The operator block was printed.
        assert any(
            "Correction is not converging." in msg for msg in announcements
        )

    def test_one_blocker_fixed_keeps_looping(self, tmp_path: Path) -> None:
        # Child resolved R1 but still rejects on R2 — identity changed, so this
        # is progress: the loop continues as before and settles on round 2.
        runs, parent_dir = self._seed(tmp_path, diff="PATCH-A")
        b1, b2 = _blocker(id="R1"), _blocker(id="R2", file="pipeline/bar.py")
        prev = _rejected_fix_halt(release_blockers=[b1, b2])
        child1 = _rejected_fix_halt(release_blockers=[b2])  # R1 fixed
        pipeline = _ArtifactPipeline(
            [child1, {"status": "done"}],
            diffs={0: "PATCH-A", 1: "PATCH-A"},
        )

        final = drive_correction_followups(
            prev_session=prev,
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        assert len(pipeline.calls) == 2
        assert final == {"status": "done"}
        assert "correction_fixed_point" not in child1

    def test_fresh_passing_receipts_keep_looping(self, tmp_path: Path) -> None:
        # Same missing-receipt blocker repeats, but the child's gate_rerun went
        # green (fresh passing receipts): that is progress, not a fixed point.
        runs, parent_dir = self._seed(tmp_path, diff="PATCH-A")
        engine_gap = {
            "risk": "required gate 'lint' unproven: receipt missing",
            "missing_evidence": "no passing receipt for lint",
            "required_check": "python -m ruff check .",
        }
        prev = _rejected_fix_halt(engine_gaps=[engine_gap])
        child1 = _rejected_fix_halt(
            engine_gaps=[engine_gap],
            gate_rerun={"attempted": True, "required_passed": True},
        )
        pipeline = _ArtifactPipeline(
            [child1, {"status": "done"}],
            diffs={0: "PATCH-A", 1: "PATCH-A"},
        )

        final = drive_correction_followups(
            prev_session=prev,
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        assert len(pipeline.calls) == 2
        assert final == {"status": "done"}
        assert "correction_fixed_point" not in child1

    def test_receipt_flipped_failed_to_passing_keeps_looping(
        self, tmp_path: Path,
    ) -> None:
        # Same blocker identity repeats, the diff is unchanged, AND the receipt
        # keeps the same command / exit_code / git fingerprint — but the parent's
        # receipt had a failed assertion (not passing) and the child's re-run is
        # green. That is real evidence progress: the guard must NOT fire even
        # though the coarse (command, exit_code, git) tuple is identical.
        runs, parent_dir = self._seed(tmp_path, diff="PATCH-A")
        git = {
            "checkout_head": "HEAD1",
            "baseline_head": "BASE1",
            "changed_files_fingerprint": "FP1",
        }
        # Parent round's receipt: exit 0 but a failed assertion -> not passing.
        _write_command_receipt(
            parent_dir, "lint",
            exit_code=0,
            assertions=[{"name": "ruff", "passed": False}],
            detail="ruff reported errors",
            git=git,
        )
        engine_gap = {
            "risk": "required gate 'lint' unproven: receipt failed",
            "missing_evidence": "no passing receipt for lint",
            "required_check": "python -m ruff check .",
        }
        prev = _rejected_fix_halt(engine_gaps=[engine_gap])
        child1 = _rejected_fix_halt(engine_gaps=[engine_gap])
        pipeline = _ArtifactPipeline(
            [child1, {"status": "done"}],
            diffs={0: "PATCH-A", 1: "PATCH-A"},
            # Child re-ran the SAME command on the SAME checkout fingerprint,
            # but now it passes (assertion green, empty detail).
            receipts={0: [(
                "lint",
                {
                    "exit_code": 0,
                    "assertions": [{"name": "ruff", "passed": True}],
                    "detail": "",
                    "git": git,
                },
            )]},
        )

        final = drive_correction_followups(
            prev_session=prev,
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        # The flipped receipt registered as progress -> a second round ran.
        assert len(pipeline.calls) == 2
        assert final == {"status": "done"}
        assert "correction_fixed_point" not in child1

    def test_fixed_point_is_not_approved_delivery(self, tmp_path: Path) -> None:
        # Regression: a fixed-point correction stays a terminal halted run — it
        # must never read as a done / approved delivery (reducer untouched).
        _runs, parent_dir = self._seed(tmp_path, diff="PATCH-A")
        prev = _rejected_fix_halt(release_blockers=[_blocker()])
        child = _rejected_fix_halt(release_blockers=[_blocker()])
        pipeline = _ArtifactPipeline([child], diffs={0: "PATCH-A"})

        final = drive_correction_followups(
            prev_session=prev,
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        assert final["status"] == "halted"
        assert final["status"] != "done"
        assert final.get("approved") is not True


# ── correction profile routing (ADR 0085, T3) ─────────────────────────────


class TestCorrectionProfileRouting:
    def test_every_round_dispatches_correction_profile(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        parent_dir = runs / "parent"
        parent_dir.mkdir(parents=True)

        prev = _fix_halt_session(
            final_acceptance={"short_summary": "round0 reject"},
        )
        # Two correction rounds before the run finally settles.
        pipeline = _ScriptedPipeline([
            _fix_halt_session(
                final_acceptance={"short_summary": "round1 reject"},
            ),
            _fix_halt_session(
                final_acceptance={"short_summary": "round2 reject"},
            ),
            {"status": "done"},
        ])

        drive_correction_followups(
            prev_session=prev,
            prev_output_dir=parent_dir,
            base_task="t",
            # Parent ran a non-correction profile — must not leak.
            stable_kwargs={"profile_name": "task", "project_dir": "/proj"},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        assert len(pipeline.calls) == 3
        assert all(
            call["profile_name"] == "correction" for call in pipeline.calls
        )
        # The parent's profile never appears in any child dispatch.
        assert all(
            call["profile_name"] != "task" for call in pipeline.calls
        )

    def test_parent_profile_does_not_leak_when_stable_kwargs_omits_it(
        self, tmp_path: Path,
    ) -> None:
        # Even when no profile_name is supplied, the driver injects the
        # correction profile rather than letting run_pipeline default.
        runs = tmp_path / "runs"
        parent_dir = runs / "parent"
        parent_dir.mkdir(parents=True)
        pipeline = _ScriptedPipeline([{"status": "done"}])

        drive_correction_followups(
            prev_session=_fix_halt_session(
                final_acceptance={"short_summary": "reject"},
            ),
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=lambda _msg: None,
        )

        assert pipeline.calls[0]["profile_name"] == "correction"

    def test_announce_names_correction_profile(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        parent_dir = runs / "parent"
        parent_dir.mkdir(parents=True)
        pipeline = _ScriptedPipeline([{"status": "done"}])
        announcements: list[str] = []

        drive_correction_followups(
            prev_session=_fix_halt_session(
                final_acceptance={"short_summary": "reject"},
            ),
            prev_output_dir=parent_dir,
            base_task="t",
            stable_kwargs={"profile_name": "advanced"},
            run_pipeline=pipeline,
            mint_run_id=_mint_sequence(),
            announce=announcements.append,
        )

        assert len(announcements) == 1
        assert "profile 'correction'" in announcements[0]


# ── _halt_banner mapping ──────────────────────────────────────────────────


class TestHaltBanner:
    def test_known_recoverable_reason_amber(self) -> None:
        label, color = _halt_banner("commit_decision_fix")
        assert "correction follow-up" in label

    def test_failure_reason_distinct_label(self) -> None:
        label, _color = _halt_banner("commit_delivery_failed")
        assert "delivery failed" in label

    def test_correction_triage_blocked_amber(self) -> None:
        from core.io.ansi import C

        label, color = _halt_banner("correction_triage_blocked")
        assert "correction triage blocked" in label
        assert color == C.YELLOW

    def test_correction_triage_missing_context_labelled(self) -> None:
        label, _color = _halt_banner("correction_triage_missing_context")
        assert "correction triage missing context" in label

    def test_unknown_reason_falls_back_to_raw(self) -> None:
        label, _color = _halt_banner("something_new")
        assert "something_new" in label

    def test_none_reason(self) -> None:
        label, _color = _halt_banner(None)
        assert label == "Run halted"


# ── banner regression: halted run does not print "Pipeline complete" ──────


class _FakeState:
    phase_log: dict[str, Any] = {}  # noqa: RUF012

    def __init__(self) -> None:
        self.halt = True
        self.halt_reason = "commit_decision_fix"
        self.extras: dict[str, Any] = {}


class _HaltedRun:
    """Minimal _PipelineRun stand-in that finalizes to a halted status.

    ``output_dir = None`` short-circuits every artifact write, so the
    silent service just resolves the status from ``state.halt`` and the
    terminal wrapper renders the header off ``result.status``.
    """

    output_dir: Any = None
    profile_name: str = "advanced"
    session_ts: str = "test-run"
    parent_run_id: str | None = None
    project_alias: str | None = None
    no_interactive: bool = True
    _ckpt: Any = None
    _metrics: Any = None
    _done_summary_profile: Any = None
    worktree_context: Any = None
    _worktree_cvar_token: Any = None
    _sandbox_cvar_token: Any = None

    def __init__(self) -> None:
        self.state = _FakeState()
        self.session: dict[str, Any] = {}


def test_halted_run_renders_halt_header_not_done(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = FinalizationContext(run=_HaltedRun())

    result = finalize_with_terminal_output(ctx)

    assert result.status == "halted"
    out = capsys.readouterr().out
    assert "HALTED" in out
    assert "correction follow-up requested" in out
    assert "Pipeline complete" not in out


class _DoneState:
    phase_log: dict[str, Any] = {}  # noqa: RUF012

    def __init__(self) -> None:
        self.halt = False
        self.halt_reason = None
        self.extras: dict[str, Any] = {}


class _DoneRun:
    """Minimal _PipelineRun stand-in that finalizes to a ``done`` status.

    ``state.halt = False`` and a non-plan-only ``profile_name`` make
    ``_resolve_terminal_status`` mark the run done; ``output_dir = None``
    short-circuits every artifact/delivery write, so the terminal banner is
    chosen purely from the supplied ``final_acceptance`` verdict's
    ``release_outcome``.
    """

    output_dir: Any = None
    profile_name: str = "advanced"
    session_ts: str = "test-run"
    parent_run_id: str | None = None
    project_alias: str | None = None
    no_interactive: bool = True
    _ckpt: Any = None
    _metrics: Any = None
    _done_summary_profile: Any = None
    worktree_context: Any = None
    _worktree_cvar_token: Any = None
    _sandbox_cvar_token: Any = None

    def __init__(self, final_acceptance: dict[str, Any]) -> None:
        self.state = _DoneState()
        self.session: dict[str, Any] = {
            "phases": {"final_acceptance": final_acceptance},
        }


def test_done_rejected_release_renders_honest_header_not_done(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A terminal ``done`` run whose release was REJECTED (no operator halt):
    # the green 'Pipeline complete' headline would lie about a delivery that
    # never happened, so an honest blocked-delivery header is rendered instead.
    run = _DoneRun({"verdict": "REJECTED", "ship_ready": False})

    result = finalize_with_terminal_output(FinalizationContext(run=run))

    assert result.status == "done"
    assert result.release_outcome == "rejected"
    out = capsys.readouterr().out
    assert "Pipeline complete" not in out
    assert "DELIVERY BLOCKED" in out
    assert "Release: rejected" in out


def test_done_approved_gate_rerun_release_stays_green(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A recovered gate_rerun / correction run that ends with an APPROVED
    # final_acceptance stays approved and keeps the green 'Pipeline complete'
    # headline — the honest-rejection branch must not capture it.
    run = _DoneRun({"verdict": "APPROVED", "ship_ready": True})

    result = finalize_with_terminal_output(FinalizationContext(run=run))

    assert result.status == "done"
    assert result.release_outcome == "approved"
    out = capsys.readouterr().out
    assert "Pipeline complete" in out
    assert "DELIVERY BLOCKED" not in out
    assert "Release: approved" in out


# ── correction-route orchestrator wiring (ADR 0086) ───────────────────────


class TestCorrectionRouteWiring:
    """``_PipelineRun`` applies the derived correction route at the pre-phase
    seam and stamps route evidence into the session at triage phase-end.

    The methods are exercised against a minimal stub ``self`` (no real run
    construction): the pre-phase gate evaluation no-ops without a
    ``_gate_profile``, and the evidence stamp runs before the
    ``_dispatch_active`` banner guard.
    """

    @staticmethod
    def _state(triage: dict[str, Any] | None) -> Any:
        from pipeline.plugins import PluginConfig
        from pipeline.runtime import PipelineState

        st = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
        if triage is not None:
            st.phase_log["correction_triage"] = triage
        return st

    @staticmethod
    def _pre_stub() -> Any:
        # No ``_gate_profile`` → evaluate_pre_phase_gates is inert.
        # ``_presentation=None`` (not TERMINAL) → the Stage 9 live block is a
        # no-op on the FINAL_PHASES pre-seam.
        from types import SimpleNamespace

        return SimpleNamespace(_presentation=None)

    def _call_pre(self, name: str, st: Any) -> None:
        from pipeline.project.run import _PipelineRun

        _PipelineRun._on_phase_pre(self._pre_stub(), name, st)

    def test_gate_rerun_marks_skip_for_implement(self) -> None:
        from pipeline.runtime.runner import PHASE_PRE_SKIP_KEY

        st = self._state({"kind": "gate_rerun", "summary": "stale blockers"})
        self._call_pre("implement", st)

        assert PHASE_PRE_SKIP_KEY in st.extras
        reason = st.extras[PHASE_PRE_SKIP_KEY]
        assert "gate_rerun" in reason
        assert "stale blockers" in reason

    def test_code_fix_does_not_mutate_extras(self) -> None:
        from pipeline.runtime.runner import PHASE_PRE_SKIP_KEY

        st = self._state({"kind": "code_fix", "summary": "narrow fix"})
        self._call_pre("implement", st)

        assert PHASE_PRE_SKIP_KEY not in st.extras

    def test_no_triage_record_is_noop(self) -> None:
        from pipeline.runtime.runner import PHASE_PRE_SKIP_KEY

        st = self._state(None)
        self._call_pre("implement", st)

        assert PHASE_PRE_SKIP_KEY not in st.extras
        assert st.extras == {}

    def test_non_skip_phase_under_shortcut_route_is_noop(self) -> None:
        from pipeline.runtime.runner import PHASE_PRE_SKIP_KEY

        # ``final_acceptance`` is NOT in the shortcut skip set — it must run.
        st = self._state({"kind": "contract_ack", "summary": "ack the contract"})
        self._call_pre("final_acceptance", st)

        assert PHASE_PRE_SKIP_KEY not in st.extras

    def test_route_evidence_stamped_into_session_at_phase_end(self) -> None:
        from types import SimpleNamespace

        from pipeline.project.run import _PipelineRun

        triage = {"kind": "gate_rerun", "summary": "stale blockers"}
        st = self._state(triage)
        stub = SimpleNamespace(
            session={"phases": {"correction_triage": dict(triage)}},
            _dispatch_active=False,
            _presentation=None,
            state=st,
            output_dir=None,
            project_path="/p",
        )

        _PipelineRun._on_phase_end(stub, "correction_triage", st)

        # phase_log record carries the flat route dict.
        route = st.phase_log["correction_triage"]["route"]
        assert route["kind"] == "gate_rerun"
        assert "implement" in route["skip_phases"]
        assert "stale blockers" in route["reason"]
        # session mirror carries the same route block.
        session_route = stub.session["phases"]["correction_triage"]["route"]
        assert session_route == route

    @staticmethod
    def _verification_contract(
        *, manual_required: bool = False,
    ) -> Any:
        """Real projected contract with two required commands.

        ``manual_required=True`` parks the ``manual_req`` required command behind
        a ``manual_only`` schedule, so the shared materializer must withhold it
        (``skipped_manual``) and gate-rerun must NOT report ``required_passed``.
        """
        from pipeline.plugins import PluginConfig
        from pipeline.verification_contract import VerificationContract

        verification: dict[str, Any] = {
            "default_env": "core-local",
            "commands": {
                "env-provenance": {"run": "true"},
                "lint": {"run": "true"},
            },
        }
        if manual_required:
            verification["required"] = ["lint", "manual_req"]
            verification["commands"]["manual_req"] = {"run": "true"}
            verification["gate_sets"] = {"manuals": {"commands": ["manual_req"]}}
            verification["schedule"] = [
                {"manual_only": True, "gate_sets": ["manuals"]},
            ]
        else:
            verification["required"] = ["env-provenance", "lint"]
        plugin = PluginConfig(
            verification_envs={"core-local": {}},
            verification=verification,
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        return contract

    def _run_gate_rerun(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        contract: Any,
        extra_extras: dict[str, Any] | None = None,
        presentation: Any = None,
    ) -> tuple[Any, list[tuple[str, dict[str, Any]]], Path, Path]:
        """Drive ``_on_phase_end`` through the delegated gate-rerun executor.

        Returns ``(state, calls, project, workspace)``. ``sdk.verify`` is mocked,
        so no real subprocess runs; ``verify_run`` writes a fresh present
        command-receipt to the run dir for every target command (exit 0), so the
        materializer's post-run on-disk re-classification reflects the
        materialization — proving ``required_passed`` is derived from real
        receipts, not the optimistic exit codes (F1).

        ``presentation`` sets the run's presentation policy on the stub; it
        defaults to SILENT so receipt/evidence assertions stay free of live-block
        stdout. The live-wiring test passes TERMINAL explicitly.
        """
        from pipeline.project.types import PresentationPolicy

        if presentation is None:
            presentation = PresentationPolicy.SILENT
        from types import SimpleNamespace

        from pipeline.project.run import _PipelineRun
        from pipeline.verification_contract import placeholder_context_for

        calls: list[tuple[str, dict[str, Any]]] = []

        def _verify_env(**kwargs):
            calls.append(("env", kwargs))
            return SimpleNamespace(
                all_passed=True,
                receipt_path=Path(kwargs["workspace"]) / "env.json",
            )

        def _write_receipt(command: str) -> Path:
            from pipeline.evidence.verification_receipt import write_command_receipt
            from pipeline.verification_subject import capture_verification_subject

            return write_command_receipt(
                output_dir=run_dir,
                result={
                    "command": command,
                    "env": "core-local",
                    "exit_code": 0,
                    "assertions": [],
                    "detail": "",
                    "git": {},
                    "subject": capture_verification_subject(project),
                    "dependencies": [],
                },
            )

        def _verify_run(**kwargs):
            calls.append(("run", kwargs))
            return SimpleNamespace(
                all_passed=True,
                outcomes=[
                    SimpleNamespace(
                        command=name,
                        exit_code=0,
                        receipt_path=_write_receipt(name),
                    )
                    for name in kwargs["commands"]
                ],
            )

        monkeypatch.setattr("sdk.verify.verify_env", _verify_env)
        monkeypatch.setattr("sdk.verify.verify_run", _verify_run)

        workspace = tmp_path / "workspace"
        run_dir = workspace / "runspace" / "runs" / "child_run"
        run_dir.mkdir(parents=True)
        project = tmp_path / "project"
        project.mkdir()
        (project / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
        subprocess.run(["git", "add", "base.txt"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
        triage = {"kind": "gate_rerun", "summary": "stale blockers"}
        st = self._state(triage)
        st.extras["verification_contract"] = contract
        st.extras["verification_placeholders"] = placeholder_context_for(
            contract,
            checkout=str(project),
            project=str(project),
            workspace=str(workspace),
            run_dir=str(run_dir),
        )
        # Extra run-level extras (e.g. the cached before_delivery routing plan)
        # so the gate-rerun executor threads the SAME full state.extras the
        # materializer reads — mirrors a real run's state.
        if extra_extras:
            st.extras.update(extra_extras)
        stub = SimpleNamespace(
            session={"phases": {"correction_triage": dict(triage)}},
            state=st,
            output_dir=run_dir,
            project_path=project,
            _dispatch_active=False,
            _presentation=presentation,
        )

        _PipelineRun._on_phase_end(stub, "correction_triage", st)
        return st, calls, project, workspace

    def test_gate_rerun_executes_current_child_required_receipts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        st, calls, project, workspace = self._run_gate_rerun(
            tmp_path, monkeypatch,
            contract=self._verification_contract(),
        )

        # One env pass (the shared default env) then one command pass over the
        # missing required commands — no ``required_only`` second runner.
        assert calls == [
            (
                "env",
                {
                    "project": str(project),
                    "env": "core-local",
                    "run_id": "child_run",
                    "workspace": str(workspace),
                    "subject_checkout": str(project),
                },
            ),
            (
                "run",
                {
                    "project": str(project),
                    "run_id": "child_run",
                    "workspace": str(workspace),
                    "commands": ["env-provenance", "lint"],
                    # The resolved subject checkout is pinned through so gates
                    # execute against the run's worktree, not a meta fallback.
                    "subject_checkout": str(project),
                },
            ),
        ]
        execution = st.phase_log["correction_triage"]["gate_rerun_execution"]
        assert execution["attempted"] is True
        assert execution["env_passed"] is True
        assert execution["required_passed"] is True
        # Current-child receipts were written (regression guard).
        assert execution["receipts"]
        assert execution == st.phase_log["correction_triage"][
            "gate_rerun_execution"
        ]

    def test_gate_rerun_required_passed_false_on_manual_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A required command that is also ``manual_only`` must be withheld from
        # the auto-run (``skipped_manual``); ``required_passed`` must NOT go
        # green while it stays unproven — the F2 "never falsely green" invariant.
        st, calls, project, workspace = self._run_gate_rerun(
            tmp_path, monkeypatch,
            contract=self._verification_contract(manual_required=True),
        )

        # Only the auto-eligible ``lint`` command runs; ``manual_req`` is skipped.
        run_calls = [c for kind, c in calls if kind == "run"]
        assert run_calls == [{
            "project": str(project),
            "run_id": "child_run",
            "workspace": str(workspace),
            "commands": ["lint"],
            "subject_checkout": str(project),
        }]
        execution = st.phase_log["correction_triage"]["gate_rerun_execution"]
        assert execution["attempted"] is True
        assert execution["required_passed"] is False

    @staticmethod
    def _path_selected_contract(*, manual_e2e: bool = False) -> Any:
        """Contract whose ``cli-sdk-unit`` gate is **path-selected** (``sdk/**``),
        not in static ``verification.required``; blanket delivery-position ``warn``
        schedule entries make it an auto-materialized delivery command once the
        gate set is selected.
        With ``manual_e2e`` a required ``e2e`` is parked manual_only."""
        from pipeline.plugins import PluginConfig
        from pipeline.verification_contract import VerificationContract

        commands: dict[str, Any] = {
            "lint": {"run": "true"},
            "cli-sdk-unit": {"run": "true"},
        }
        gate_sets: dict[str, Any] = {"cli-sdk": {"commands": ["cli-sdk-unit"]}}
        required = ["lint"]
        schedule: list[dict[str, Any]] = [
            {"before_delivery": True, "policy": "warn"},
            {"after_phase": "implement", "policy": "warn"},
        ]
        if manual_e2e:
            commands["e2e"] = {"run": "true"}
            gate_sets["manuals"] = {"commands": ["e2e"]}
            required.append("e2e")
            schedule.append({"manual_only": True, "gate_sets": ["manuals"]})
        plugin = PluginConfig(
            verification_envs={"core-local": {}},
            verification={
                "default_env": "core-local",
                "required": required,
                "commands": commands,
                "gate_sets": gate_sets,
                "selection": [{"paths": ["sdk/**"], "include": ["cli-sdk"]}],
                "schedule": schedule,
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        return contract

    @staticmethod
    def _cached_before_delivery_extras(
        contract: Any, touched: list[str],
    ) -> dict[str, Any]:
        """The cached ``before_delivery`` routing plan keyed by epoch — the same
        cache shape gate_repair writes and readiness/delivery read."""
        from pipeline.verification_readiness import ROUTING_PLANS_EXTRAS_KEY
        from pipeline.verification_selection import (
            build_scheduled_gate_plan,
            selection_context_from_extras,
        )

        plan = build_scheduled_gate_plan(
            contract,
            selection_context_from_extras(
                {}, contract, touched_paths=tuple(touched),
            ),
        )
        return {ROUTING_PLANS_EXTRAS_KEY: {"before_delivery:": plan}}

    def test_gate_rerun_materializes_path_selected_delivery_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Regression: a follow-up gate_rerun on a CLEAN checkout must materialize
        # the path-selected ``cli-sdk-unit`` because the full state.extras carries
        # the cached before_delivery plan that selected it — not just the static
        # ``verification.required``. A current-child receipt is written for it.
        monkeypatch.setattr(
            "core.io.git_helpers.git_changed_files", lambda cwd: [],
        )
        contract = self._path_selected_contract()
        extras = self._cached_before_delivery_extras(contract, ["sdk/foo.py"])
        st, calls, _project, _workspace = self._run_gate_rerun(
            tmp_path, monkeypatch, contract=contract, extra_extras=extras,
        )

        run_calls = [c for kind, c in calls if kind == "run"]
        assert run_calls and "cli-sdk-unit" in run_calls[0]["commands"]
        execution = st.phase_log["correction_triage"]["gate_rerun_execution"]
        assert execution["attempted"] is True
        # All delivery-selected receipts materialized green -> required_passed.
        assert execution["required_passed"] is True
        # The path-selected gate's current-child receipt landed on disk.
        assert any("cli-sdk-unit" in r for r in execution["receipts"])

    def test_gate_rerun_path_selected_with_manual_blocks_required_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The path-selected ``cli-sdk-unit`` still materializes, but a required
        # ``manual_only`` ``e2e`` stays withheld (skipped_manual), so
        # ``required_passed`` must NOT go green (F2 "never falsely green").
        monkeypatch.setattr(
            "core.io.git_helpers.git_changed_files", lambda cwd: [],
        )
        contract = self._path_selected_contract(manual_e2e=True)
        extras = self._cached_before_delivery_extras(contract, ["sdk/foo.py"])
        st, calls, _project, _workspace = self._run_gate_rerun(
            tmp_path, monkeypatch, contract=contract, extra_extras=extras,
        )

        run_calls = [c for kind, c in calls if kind == "run"]
        assert run_calls and "cli-sdk-unit" in run_calls[0]["commands"]
        # Manual e2e never handed to verify_run.
        assert "e2e" not in run_calls[0]["commands"]
        execution = st.phase_log["correction_triage"]["gate_rerun_execution"]
        assert execution["attempted"] is True
        assert execution["required_passed"] is False

    @staticmethod
    def _no_required_contract() -> Any:
        """Contract with declared commands but an empty required set, so the
        materializer resolves no required delivery command -> ``attempted=False``
        (the gate-rerun no-op)."""
        from pipeline.plugins import PluginConfig
        from pipeline.verification_contract import VerificationContract

        plugin = PluginConfig(
            verification_envs={"core-local": {}},
            verification={
                "default_env": "core-local",
                "required": [],
                "commands": {"lint": {"run": "true"}},
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        return contract

    def test_gate_rerun_live_block_printed_and_trail_appended(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # TERMINAL: the gate-rerun live block names the rerun commands (which the
        # fixed ``gate_rerun_execution`` evidence omits), and the raw result is
        # appended to the durable ``verification_autorun`` trail so the DONE
        # aggregator sees this gate-rerun's commands.
        from core.io.ansi import strip_ansi
        from pipeline.project.types import PresentationPolicy

        st, _calls, _project, _workspace = self._run_gate_rerun(
            tmp_path, monkeypatch,
            contract=self._verification_contract(),
            presentation=PresentationPolicy.TERMINAL,
        )

        out = strip_ansi(capsys.readouterr().out)
        assert "Verification gates — correction gate rerun" in out
        assert "env-provenance" in out and "lint" in out

        # Durable trail dozapisan (same fixed shape as the Stage 9 auto-run).
        trail = st.extras["verification_autorun"]
        assert isinstance(trail, list) and trail
        ran = set(trail[-1]["ran_commands"])
        assert {"env-provenance", "lint"} <= ran
        # ADR-fixed gate_rerun_execution keys are unchanged (no command names).
        execution = st.phase_log["correction_triage"]["gate_rerun_execution"]
        assert set(execution) == {
            "attempted", "reason", "env", "env_passed",
            "required_passed", "receipts", "error",
        }

    def test_gate_rerun_live_block_omitted_in_silent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # SILENT: no live block, but the gate-rerun still runs + records evidence.
        st, _calls, _project, _workspace = self._run_gate_rerun(
            tmp_path, monkeypatch, contract=self._verification_contract(),
        )
        assert "Verification gates" not in capsys.readouterr().out
        # Evidence + trail still recorded (the live block is presentation-only).
        assert st.phase_log["correction_triage"]["gate_rerun_execution"]["attempted"]
        assert st.extras["verification_autorun"]

    def test_gate_rerun_live_block_omitted_on_noop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # TERMINAL but a no-op (empty required -> attempted=False): the renderer
        # returns () so nothing prints — no misleading empty header.
        from pipeline.project.types import PresentationPolicy

        st, _calls, _project, _workspace = self._run_gate_rerun(
            tmp_path, monkeypatch,
            contract=self._no_required_contract(),
            presentation=PresentationPolicy.TERMINAL,
        )
        assert "Verification gates" not in capsys.readouterr().out
        assert (
            st.phase_log["correction_triage"]["gate_rerun_execution"]["attempted"]
            is False
        )

    def test_halted_triage_record_gets_no_route_evidence(self) -> None:
        from types import SimpleNamespace

        from pipeline.project.run import _PipelineRun

        triage = {
            "kind": "blocked",
            "summary": "no path",
            "halted": True,
            "reason": "correction_triage_blocked",
        }
        st = self._state(triage)
        stub = SimpleNamespace(
            session={"phases": {"correction_triage": dict(triage)}},
            _dispatch_active=False,
        )

        _PipelineRun._on_phase_end(stub, "correction_triage", st)

        assert "route" not in st.phase_log["correction_triage"]
        assert "route" not in stub.session["phases"]["correction_triage"]


class TestSkippedPhaseGateSuppression:
    """Reviewer F1: a runner-skipped phase performed no work, so neither its
    ``before_phase`` nor its ``after_phase`` verification gates may run —
    with an active contract, ``after_phase(implement)`` on a skipped
    ``implement`` would execute gate commands (and could repair/halt/handoff)
    against work that never happened.

    The stubs are gate-ACTIVE: ``_gate_profile`` is set and
    ``state.extras['verification_contract']`` is present, so
    ``evaluate_pre/post_phase_gates`` run for real; only the inner
    ``run_gate_hook`` (the gate-command executor) is recorded.
    """

    @staticmethod
    def _state(extras: dict[str, Any] | None = None) -> Any:
        from pipeline.plugins import PluginConfig
        from pipeline.runtime import PipelineState

        st = PipelineState(task="t", project_dir="/p", plugin=PluginConfig())
        st.extras["verification_contract"] = object()  # _gate_active probe
        st.extras.update(extras or {})
        return st

    @staticmethod
    def _gate_active_stub(st: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            state=st,
            _gate_profile=object(),
            _gate_ctx=object(),
            _dispatch_active=True,
            _presentation=None,
            session={},
        )

    @staticmethod
    def _record_gate_hooks(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
        calls: list[dict[str, str]] = []

        def _recording_hook(run, profile, ctx, *, hook, phase=""):
            calls.append({"hook": hook, "phase": phase})
            return None

        monkeypatch.setattr(
            "pipeline.project.gate_repair.run_gate_hook", _recording_hook,
        )
        return calls

    def test_skip_end_does_not_run_after_phase_gate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pipeline.project.run import _PipelineRun
        from pipeline.runtime.runner import PHASE_END_SKIPPED_KEY

        calls = self._record_gate_hooks(monkeypatch)
        monkeypatch.setattr(
            "pipeline.project.run.emit_phase_log_end", lambda *a, **k: None,
        )
        # State as the runner's _skip_phase leaves it while on_phase_end runs:
        # skipped record in the phase log + the consume-once skip-end context.
        st = self._state({PHASE_END_SKIPPED_KEY: "implement"})
        st.phase_log["implement"] = {
            "skipped": "not applicable for correction route 'gate_rerun'",
        }
        stub = self._gate_active_stub(st)

        _PipelineRun._on_phase_end(stub, "implement", st)

        assert calls == []  # no gate command ran for the skipped phase
        assert not st.halt
        assert st.phase_handoff_request is None

    def test_real_end_still_runs_after_phase_gate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Control: without the skip-end context the after_phase hook fires.
        from pipeline.project.run import _PipelineRun

        calls = self._record_gate_hooks(monkeypatch)
        monkeypatch.setattr(
            "pipeline.project.run.emit_phase_log_end", lambda *a, **k: None,
        )
        st = self._state()
        stub = self._gate_active_stub(st)

        _PipelineRun._on_phase_end(stub, "implement", st)

        assert calls == [{"hook": "after_phase", "phase": "implement"}]

    def test_route_skip_marking_suppresses_before_phase_gate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pipeline.project.run import _PipelineRun
        from pipeline.runtime.runner import PHASE_PRE_SKIP_KEY

        calls = self._record_gate_hooks(monkeypatch)
        st = self._state()
        st.phase_log["correction_triage"] = {
            "kind": "gate_rerun", "summary": "stale blockers",
        }
        stub = self._gate_active_stub(st)

        _PipelineRun._on_phase_pre(stub, "implement", st)

        # The skip was marked for the runner AND no before_phase gate ran.
        assert PHASE_PRE_SKIP_KEY in st.extras
        assert calls == []

    def test_non_skipped_phase_still_runs_before_phase_gate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Control: a phase outside the route's skip set keeps its pre gates.
        from pipeline.project.run import _PipelineRun

        calls = self._record_gate_hooks(monkeypatch)
        st = self._state()
        st.phase_log["correction_triage"] = {
            "kind": "gate_rerun", "summary": "stale blockers",
        }
        stub = self._gate_active_stub(st)

        _PipelineRun._on_phase_pre(stub, "final_acceptance", st)

        assert {"hook": "before_phase", "phase": "final_acceptance"} in calls


# ── T4: official-receipt vs ad-hoc transcript channel distinction ──────────


class TestTranscriptNotProofChannelDistinction:
    """When a required receipt is missing/stale and the run only carries ad-hoc
    transcript commands, the readiness/remediation block states plainly that
    transcript commands are not accepted as proof. The same single helper backs
    every surface (readiness and correction/review both render via
    ``render_readiness_block``)."""

    def _summary(self, **overrides: Any):
        from pipeline.verification_readiness import ReadinessSummary

        base: dict[str, Any] = {
            "env_statuses": (("ci", True),),
            "exploratory_count": 2,
            "exploratory_note": (
                "observed ad-hoc commands; exploratory only, not authoritative"
            ),
        }
        base.update(overrides)
        return ReadinessSummary(**base)

    def test_helper_is_single_source_of_exact_phrase(self) -> None:
        from pipeline.verification_readiness import (
            TRANSCRIPT_NOT_PROOF_NOTE,
            transcript_not_proof_note,
        )

        assert transcript_not_proof_note() == TRANSCRIPT_NOT_PROOF_NOTE
        assert TRANSCRIPT_NOT_PROOF_NOTE == (
            "official receipt missing; transcript commands are not accepted as proof"
        )

    def test_missing_required_with_exploratory_prints_distinction(self) -> None:
        from pipeline.verification_readiness import (
            TRANSCRIPT_NOT_PROOF_NOTE,
            render_readiness_block,
        )

        summary = self._summary(
            required_missing=("test",),
            suggested_commands=(
                "orcho verify run test --run-id rid --project /co",
            ),
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert TRANSCRIPT_NOT_PROOF_NOTE in rendered
        # The official verify command stays as remediation, not as proof.
        assert "orcho verify run test --run-id rid --project /co" in rendered

    def test_stale_required_with_exploratory_prints_distinction(self) -> None:
        from pipeline.verification_readiness import (
            TRANSCRIPT_NOT_PROOF_NOTE,
            render_readiness_block,
        )

        summary = self._summary(
            required_stale=("test",),
            stale_reasons=("test: subject drift",),
            suggested_commands=("orcho verify run test --run-id rid",),
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert TRANSCRIPT_NOT_PROOF_NOTE in rendered

    def test_present_receipt_does_not_print_distinction(self) -> None:
        from pipeline.verification_readiness import (
            TRANSCRIPT_NOT_PROOF_NOTE,
            render_readiness_block,
        )

        # A run with an official present receipt and ad-hoc commands: no blocker,
        # so the distinction note is never printed.
        summary = self._summary(required_present=("test",))
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert TRANSCRIPT_NOT_PROOF_NOTE not in rendered

    def test_no_exploratory_block_is_byte_identical(self) -> None:
        """Missing required receipt but no observed exploratory commands: the note
        is gated off, so that block is unchanged from before T4."""
        from pipeline.verification_readiness import (
            TRANSCRIPT_NOT_PROOF_NOTE,
            render_readiness_block,
        )

        summary = self._summary(
            required_missing=("test",),
            exploratory_count=0,
            exploratory_note="",
            suggested_commands=("orcho verify run test --run-id rid",),
        )
        rendered = render_readiness_block(summary)
        assert rendered is not None
        assert TRANSCRIPT_NOT_PROOF_NOTE not in rendered
        # Remediation (official commands) is still surfaced for the operator.
        assert "orcho verify run test --run-id rid" in rendered


@pytest.fixture(autouse=True)
def _live_output_mode_for_full_transcript():
    """Pin the full live transcript shape (T2 summary reconciliation).

    ``summary`` is the default run-output mode — the compact append-only
    arc that collapses phase headers to ``▶ <phase>`` and the review /
    plan / implement outcome blocks to single lines. These tests assert
    the full-fidelity transcript, so force ``live`` (rendering only; no
    echo / verbose / trace side effects) and restore afterwards.
    """
    from core.observability import logging as _logging

    _before = _logging.get_output_mode()
    _logging._output_mode = "live"
    try:
        yield
    finally:
        _logging._output_mode = _before
