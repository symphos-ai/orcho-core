# SPDX-License-Identifier: Apache-2.0
"""Parity + composition coverage for the core RunDiagnosis read-model (ADR 0114).

Exercises :func:`sdk.run_control.diagnosis.run_diagnosis` over hand-built
``meta.json`` fixtures (filesystem-light synthetic run dirs — no real git), one
scenario per ``condition`` and per ``continuation_subject`` in the closed sets,
including the ``stop_unknown`` dead-end with a non-empty ``missing_facts``.

A living-documentation parity table pins each core output to the matching MCP
``project_run_diagnosis`` (condition) / ``project_recovery_lineage``
(continuation_subject) branch. orcho-mcp is NEITHER imported NOR modified here:
the MCP closed sets are mirrored as plain literals so the test fails loudly if
the core read-model ever emits a label outside the contract the MCP adapter
expects to consume in the follow-up P1-mcp migration.

A negative AST assert confirms this composition declares none of the guarded
run-lifecycle decision-table literals; the authoritative static owner is
``tests/unit/pipeline/test_lifecycle_classifier_ownership_guard.py`` (run
separately, stays green).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from sdk.run_control import diagnosis as diag
from sdk.run_control.diagnosis import run_diagnosis
from sdk.run_control.recovery_lineage import recovery_lineage
from sdk.run_control.types import RunDiagnosis

pytestmark = [pytest.mark.sdk, pytest.mark.filesystem_light]


# ── fixture helpers ───────────────────────────────────────────────────────────


def _mk(
    runs_dir: Path, run_id: str, meta: dict, files: dict[str, str] | None = None,
) -> str:
    """Write a synthetic run dir (``meta.json`` + optional artifacts)."""
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    for name, content in (files or {}).items():
        (d / name).write_text(content, encoding="utf-8")
    return run_id


def _diag(runs_dir: Path, run_id: str) -> RunDiagnosis:
    """Classify ``run_id`` from an explicit runs_dir (no ambient walk-up)."""
    return run_diagnosis(run_id, runs_dir=runs_dir, cwd=None)


# ── per-condition coverage ────────────────────────────────────────────────────


def test_active(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {"status": "running", "project": "/x"})
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_ACTIVE
    assert d.continuation_subject is None


def test_needs_decision_awaiting_handoff(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "awaiting_phase_handoff",
        "phase_handoff": {"id": "h1", "available_actions": ["continue", "halt"]},
        "project": "/x",
    })
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_NEEDS_DECISION
    assert d.handoff_id == "h1"
    assert d.available_actions == ("continue", "halt")


def test_needs_decision_torn_interrupted_with_active_payload(tmp_path: Path) -> None:
    # _is_decidable_handoff_status also accepts the torn ``interrupted`` form
    # (an active payload survives the interruption) — composition, not a status
    # re-comparison.
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "interrupted",
        "phase_handoff": {"id": "h9", "available_actions": ["halt"]},
        "project": "/x",
    })
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_NEEDS_DECISION
    assert d.handoff_id == "h9"


def test_needs_delivery_decision_approved_gate(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "commit_delivery_pending",
        "commit_delivery": {
            "status": "pending", "run_id": "r",
            "project_path": "/x", "source_path": "/x", "baseline_ref": "HEAD",
        },
        "project": "/x",
    })
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_NEEDS_DELIVERY_DECISION
    assert d.continuation_subject == diag.SUBJECT_DELIVERY_GATE
    assert d.recommended_next_action == diag.ACTION_DELIVERY_DECISION
    assert d.delivery_gate_kind == "delivery"


def test_correction_followup_required_fix_requested(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "commit_decision_fix",
        "commit_delivery": {
            "status": "fix_requested", "run_id": "r",
            "project_path": "/x", "source_path": "/x", "baseline_ref": "HEAD",
            "release_verdict": "REJECTED",
        },
        "project": "/x",
    })
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_BLOCKED_WORKTREE
    assert d.continuation_subject == diag.SUBJECT_RETAINED_CHANGE
    assert d.recommended_next_action == diag.ACTION_START_FOLLOWUP
    assert d.blocked is True


def test_superseded_by_child(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "20260101_000001", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })
    _mk(runs, "20260101_000002", {
        "status": "awaiting_phase_handoff", "resume_mode": "followup",
        "parent_run_id": "20260101_000001",
        "phase_handoff": {"id": "hc", "available_actions": ["continue"]},
        "project": "/x",
    })
    d = _diag(runs, "20260101_000001")
    assert d.condition == diag.CONDITION_SUPERSEDED_BY_CHILD
    assert d.continuation_subject == diag.SUBJECT_ACTIVE_CHILD_RUN
    assert d.recommended_next_action == diag.ACTION_RESUME_ACTIVE_CHILD
    assert d.recommended_run_id == "20260101_000002"


def test_blocked_worktree(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "parent_run_id": "p",
        "worktree": {"followup_continuity": {
            "blocked": True, "reason": "parent diff unavailable",
            "diff_source": "artifact",
        }},
        "project": "/x",
    })
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_BLOCKED_WORKTREE
    assert d.blocked is True
    assert d.block_message == "parent diff unavailable"
    assert d.recommended_run_id == "p"


def test_recover_via_source_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    # Resumable source: a failed (non-terminal) parent that kept its worktree.
    _mk(runs, "parent", {
        "status": "failed",
        "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
        "project": "/x",
    })
    _mk(runs, "child", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "parent_run_id": "parent", "project": "/x",
    })
    d = _diag(runs, "child")
    assert d.condition == diag.CONDITION_RECOVER_VIA_SOURCE_RUN
    assert d.continuation_subject == diag.SUBJECT_SOURCE_RUN_CHECKPOINT
    assert d.recommended_next_action == diag.ACTION_RESUME_SOURCE_RUN
    assert d.recommended_run_id == "parent"
    assert d.source_run_id == "parent"


def test_recover_via_source_run_respects_injected_source_facts(tmp_path: Path) -> None:
    # The source's on-disk meta is stale (``running`` + retained worktree → it
    # would read as resumable), but the embedder's supervisor-merge has already
    # settled it to a clean terminal ``done``. Feeding that resolved meta via
    # ``source_meta`` must suppress the blind ``recover_via_source_run`` — the
    # source is terminal, so the child is a dead-end stop, not a redirect.
    runs = tmp_path / "runs"
    _mk(runs, "parent", {
        "status": "running",
        "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
        "project": "/x",
    })
    _mk(runs, "child", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "parent_run_id": "parent", "project": "/x",
    })

    # Without injection: the stale on-disk source reads as resumable.
    stale = _diag(runs, "child")
    assert stale.condition == diag.CONDITION_RECOVER_VIA_SOURCE_RUN
    assert stale.continuation_subject == diag.SUBJECT_SOURCE_RUN_CHECKPOINT

    # With injected provider-resolved source facts: the source is terminal-done,
    # so it is no longer a resumable checkpoint → the child stops as unknown.
    resolved = run_diagnosis(
        "child", runs_dir=runs, cwd=None,
        source_meta={"parent": {"status": "done", "project": "/x"}},
    )
    assert resolved.condition == diag.CONDITION_RESUME_INERT_TERMINAL
    assert resolved.continuation_subject == diag.SUBJECT_UNKNOWN
    assert resolved.recommended_next_action == diag.ACTION_STOP_UNKNOWN
    assert diag._MISSING_SOURCE in resolved.missing_facts


def test_closed_by_followup(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "done",
        "superseded_by_followup": {"child_run_id": "kid"},
        "project": "/x",
    })
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_CLOSED_BY_FOLLOWUP
    assert d.continuation_subject == diag.SUBJECT_NONE
    assert d.recommended_run_id == "kid"


def test_resume_inert_terminal_clean_success_none(tmp_path: Path) -> None:
    # Clean terminal success with no source / plan → inert terminal, subject none,
    # next action start a fresh follow-up.
    runs = tmp_path / "runs"
    _mk(runs, "r", {"status": "done", "project": "/x"})
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_RESUME_INERT_TERMINAL
    assert d.continuation_subject == diag.SUBJECT_NONE
    assert d.recommended_next_action == diag.ACTION_START_FOLLOWUP
    assert d.missing_facts == ()


def test_resume_inert_terminal_plan_artifact(tmp_path: Path) -> None:
    # Terminal dead-end backed by a persisted plan artifact and a plan-only
    # profile → plan_artifact continuation.
    runs = tmp_path / "runs"
    _mk(
        runs, "r",
        {
            "status": "halted", "halt_reason": "final_acceptance_rejected",
            "plan_source": "local", "profile": "planning", "project": "/x",
        },
        files={"parsed_plan.json": "{}"},
    )
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_RESUME_INERT_TERMINAL
    assert d.continuation_subject == diag.SUBJECT_PLAN_ARTIFACT
    assert d.recommended_next_action == diag.ACTION_PLAN_ARTIFACT_CONTINUATION


def test_resume_inert_terminal_stop_unknown_missing_facts(tmp_path: Path) -> None:
    # Rejected dead-end with no resumable source, no plan, no gate, no child →
    # explicit unknown stop with the absent facts enumerated (not a blind resume).
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })
    d = _diag(runs, "r")
    assert d.condition == diag.CONDITION_RESUME_INERT_TERMINAL
    assert d.continuation_subject == diag.SUBJECT_UNKNOWN
    assert d.recommended_next_action == diag.ACTION_STOP_UNKNOWN
    assert d.missing_facts  # non-empty
    assert diag._MISSING_SOURCE in d.missing_facts
    assert diag._MISSING_PLAN in d.missing_facts


@pytest.mark.parametrize("status", ["halted", "failed", "interrupted"])
def test_resumable_non_terminal_stop(tmp_path: Path, status: str) -> None:
    # A non-terminal stop (no terminal halt_reason, no active handoff payload)
    # surfaces its own status as the condition and continues itself.
    runs = tmp_path / "runs"
    _mk(runs, "r", {"status": status, "project": "/x"})
    d = _diag(runs, "r")
    assert d.condition == status
    assert d.continuation_subject == diag.SUBJECT_NONE


def test_interrupted_in_flight_phase_recommends_plan_artifact(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    _mk(
        runs,
        "r",
        {"status": "interrupted", "project": "/x"},
        files={
            "parsed_plan.json": "{}",
            "events.jsonl": (
                '{"seq":1,"kind":"phase.start",'
                '"payload":{"phase":"implement"}}\n'
            ),
        },
    )

    d = _diag(runs, "r")

    assert d.condition == "interrupted"
    assert d.continuation_subject == diag.SUBJECT_PLAN_ARTIFACT
    assert (
        d.recommended_next_action
        == diag.ACTION_PLAN_ARTIFACT_CONTINUATION
    )
    assert d.recommended_run_id == "r"
    assert "before a resumable checkpoint" in d.reason


# ── parity table (living documentation; MCP not imported) ──────────────────────

# Mirror of MCP ``project_run_diagnosis`` condition labels (run_projection.py).
# Plus the residual resumable statuses, which surface the status string itself.
_MCP_CONDITIONS = frozenset({
    "needs_decision",
    "needs_delivery_decision",
    "correction_followup_required",
    "superseded_by_child",
    "closed_by_followup",
    "blocked_worktree",
    "recover_via_source_run",
    "resume_inert_terminal",
    "active",
    # residual resumable stop (status itself)
    "halted", "failed", "interrupted",
})

# Mirror of MCP ``project_recovery_lineage`` continuation subjects
# (run_lineage.ContinuationSubject); ``None`` covers branches that carry no
# recovery subject (needs_decision / blocked_worktree / active / residual).
_MCP_CONTINUATION_SUBJECTS = frozenset({
    "source_run_checkpoint",
    "active_child_run",
    "delivery_gate",
    "plan_artifact",
    "retained_change",
    "none",
    "unknown",
})


def _scenarios(runs: Path) -> list[tuple[str, str, str | None]]:
    """Build one run per branch; return (run_id, expected_condition, subject)."""
    table: list[tuple[str, str, str | None]] = []

    _mk(runs, "active", {"status": "running", "project": "/x"})
    table.append(("active", "active", None))

    _mk(runs, "decide", {
        "status": "awaiting_phase_handoff",
        "phase_handoff": {"id": "h", "available_actions": ["continue"]},
        "project": "/x",
    })
    table.append(("decide", "needs_decision", None))

    _mk(runs, "deliver", {
        "status": "halted", "halt_reason": "commit_delivery_pending",
        "commit_delivery": {
            "status": "pending", "run_id": "deliver",
            "project_path": "/x", "source_path": "/x", "baseline_ref": "HEAD",
        },
        "project": "/x",
    })
    table.append(("deliver", "needs_delivery_decision", "delivery_gate"))

    _mk(runs, "correct", {
        "status": "halted", "halt_reason": "commit_decision_fix",
        "commit_delivery": {
            "status": "fix_requested", "run_id": "correct",
            "project_path": "/x", "source_path": "/x", "baseline_ref": "HEAD",
            "release_verdict": "REJECTED",
        },
        "project": "/x",
    })
    table.append(("correct", "blocked_worktree", "retained_change"))

    _mk(runs, "20270101_000001", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })
    _mk(runs, "20270101_000002", {
        "status": "awaiting_phase_handoff", "resume_mode": "followup",
        "parent_run_id": "20270101_000001",
        "phase_handoff": {"id": "hc", "available_actions": ["continue"]},
        "project": "/x",
    })
    table.append(("20270101_000001", "superseded_by_child", "active_child_run"))

    _mk(runs, "blocked", {
        "status": "halted",
        "worktree": {"followup_continuity": {
            "blocked": True, "reason": "x", "diff_source": "artifact",
        }},
        "project": "/x",
    })
    table.append(("blocked", "blocked_worktree", None))

    _mk(runs, "src", {
        "status": "failed",
        "worktree": {"isolation": "per_run", "path": "/tmp/wt-src"},
        "project": "/x",
    })
    _mk(runs, "viasrc", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "parent_run_id": "src", "project": "/x",
    })
    table.append(("viasrc", "recover_via_source_run", "source_run_checkpoint"))

    _mk(runs, "closed", {
        "status": "done",
        "superseded_by_followup": {"child_run_id": "kid"},
        "project": "/x",
    })
    table.append(("closed", "closed_by_followup", "none"))

    _mk(
        runs, "planonly",
        {
            "status": "halted", "halt_reason": "final_acceptance_rejected",
            "plan_source": "local", "profile": "planning", "project": "/x",
        },
        files={"parsed_plan.json": "{}"},
    )
    table.append(("planonly", "resume_inert_terminal", "plan_artifact"))

    _mk(runs, "deadend", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })
    table.append(("deadend", "resume_inert_terminal", "unknown"))

    _mk(runs, "cleanok", {"status": "done", "project": "/x"})
    table.append(("cleanok", "resume_inert_terminal", "none"))

    _mk(runs, "resumable", {"status": "failed", "project": "/x"})
    table.append(("resumable", "failed", "none"))

    return table


def test_parity_against_mcp_branch_contract(tmp_path: Path) -> None:
    """Every core output maps onto a known MCP condition / continuation subject."""
    runs = tmp_path / "runs"
    table = _scenarios(runs)
    produced_conditions: set[str] = set()
    produced_subjects: set[str | None] = set()

    for run_id, expected_condition, expected_subject in table:
        d = _diag(runs, run_id)
        assert d.condition == expected_condition, (run_id, d.condition)
        assert d.continuation_subject == expected_subject, (run_id, d)
        # The expected labels are themselves the MCP contract — pin them.
        assert expected_condition in _MCP_CONDITIONS
        assert expected_subject in (_MCP_CONTINUATION_SUBJECTS | {None})
        produced_conditions.add(d.condition)
        produced_subjects.add(d.continuation_subject)

    # The closed condition set is fully exercised (residual collapses the three
    # resumable statuses to a representative ``failed``).
    expected_conditions = {
        diag.CONDITION_ACTIVE,
        diag.CONDITION_NEEDS_DECISION,
        diag.CONDITION_NEEDS_DELIVERY_DECISION,
        diag.CONDITION_SUPERSEDED_BY_CHILD,
        diag.CONDITION_BLOCKED_WORKTREE,
        diag.CONDITION_RECOVER_VIA_SOURCE_RUN,
        diag.CONDITION_RESUME_INERT_TERMINAL,
        diag.CONDITION_CLOSED_BY_FOLLOWUP,
        "failed",
    }
    assert produced_conditions == expected_conditions

    # The full closed continuation-subject set is exercised (incl. None branches).
    assert produced_subjects == {
        None,
        diag.SUBJECT_DELIVERY_GATE,
            diag.SUBJECT_PLAN_ARTIFACT,
            diag.SUBJECT_RETAINED_CHANGE,
        diag.SUBJECT_ACTIVE_CHILD_RUN,
        diag.SUBJECT_SOURCE_RUN_CHECKPOINT,
        diag.SUBJECT_NONE,
        diag.SUBJECT_UNKNOWN,
    }


# ── additivity regression: recovery attaches without disturbing condition ─────


def _diag_no_recovery_fields(d: RunDiagnosis) -> dict:
    """Every RunDiagnosis field except the additive ``recovery`` attachment."""
    import dataclasses

    return {
        f.name: getattr(d, f.name)
        for f in dataclasses.fields(d)
        if f.name != "recovery"
    }


def test_recovery_is_additive_recover_via_source_run(tmp_path: Path) -> None:
    # The pre-existing condition / continuation outputs are byte-identical to the
    # P1-core baseline; only the new ``recovery`` field is populated, and it is
    # consistent with the condition branch (same source_run_checkpoint subject).
    runs = tmp_path / "runs"
    _mk(runs, "parent", {
        "status": "failed",
        "worktree": {"isolation": "per_run", "path": "/tmp/wt-parent"},
        "project": "/x",
    })
    _mk(runs, "child", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "parent_run_id": "parent", "project": "/x",
    })
    d = _diag(runs, "child")

    # Prior fields unchanged — pinned verbatim from the existing branch coverage.
    prior = _diag_no_recovery_fields(d)
    assert prior["condition"] == diag.CONDITION_RECOVER_VIA_SOURCE_RUN
    assert prior["continuation_subject"] == diag.SUBJECT_SOURCE_RUN_CHECKPOINT
    assert prior["recommended_next_action"] == diag.ACTION_RESUME_SOURCE_RUN
    assert prior["recommended_run_id"] == "parent"
    assert prior["source_run_id"] == "parent"

    # The additive recovery read-model is populated and agrees on the subject.
    from sdk.run_control.types import RecoveryLineage

    assert isinstance(d.recovery, RecoveryLineage)
    assert d.recovery.continuation_subject == diag.SUBJECT_SOURCE_RUN_CHECKPOINT
    assert d.recovery.recommended_next_action == diag.ACTION_RESUME_SOURCE_RUN
    assert d.recovery.recommended_run_id == "parent"
    assert d.recovery.source_resumable is True


def test_recovery_is_additive_resume_inert_terminal_unknown(tmp_path: Path) -> None:
    # A terminal dead-end: prior unknown/stop_unknown outputs unchanged and the
    # attached recovery mirrors the same unknown subject + missing_facts.
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })
    d = _diag(runs, "r")

    prior = _diag_no_recovery_fields(d)
    assert prior["condition"] == diag.CONDITION_RESUME_INERT_TERMINAL
    assert prior["continuation_subject"] == diag.SUBJECT_UNKNOWN
    assert prior["recommended_next_action"] == diag.ACTION_STOP_UNKNOWN
    assert diag._MISSING_SOURCE in prior["missing_facts"]

    from sdk.run_control.types import RecoveryLineage

    assert isinstance(d.recovery, RecoveryLineage)
    assert d.recovery.continuation_subject == diag.SUBJECT_UNKNOWN
    assert d.recovery.recommended_next_action == diag.ACTION_STOP_UNKNOWN
    assert d.recovery.is_terminal_or_rejected is True
    assert diag._MISSING_SOURCE in d.recovery.missing_facts


def test_inspected_meta_seam_drives_delivery_branch(tmp_path: Path) -> None:
    # The inspected-run ``meta=`` seam must classify the delivery gate from the
    # passed (supervisor-merged) meta, not re-read the stale on-disk file. The
    # condition branch and its attached recovery read-model stay consistent —
    # both honour the same passed meta.
    runs = tmp_path / "runs"
    _mk(runs, "r", {
        "status": "halted", "halt_reason": "final_acceptance_rejected",
        "project": "/x",
    })

    # On disk: a terminal dead-end with no gate → resume_inert_terminal/unknown.
    on_disk = _diag(runs, "r")
    assert on_disk.condition == diag.CONDITION_RESUME_INERT_TERMINAL
    assert on_disk.continuation_subject == diag.SUBJECT_UNKNOWN

    # With the provider-merged meta carrying a pending gate → needs_delivery_decision,
    # and the attached recovery agrees on the delivery_gate subject.
    merged = {
        "status": "halted", "halt_reason": "commit_delivery_pending",
        "commit_delivery": {
            "status": "pending", "run_id": "r",
            "project_path": "/x", "source_path": "/x", "baseline_ref": "HEAD",
        },
        "project": "/x",
    }
    seamed = run_diagnosis("r", runs_dir=runs, cwd=None, meta=merged)
    assert seamed.condition == diag.CONDITION_NEEDS_DELIVERY_DECISION
    assert seamed.continuation_subject == diag.SUBJECT_DELIVERY_GATE
    assert seamed.recommended_next_action == diag.ACTION_DELIVERY_DECISION
    assert seamed.recovery is not None
    assert seamed.recovery.continuation_subject == diag.SUBJECT_DELIVERY_GATE


def test_recovery_attached_on_every_branch(tmp_path: Path) -> None:
    # Across the full scenario table, ``recovery`` is always populated (additive,
    # never None on a readable run) and never disturbs the condition output.
    from sdk.run_control.types import RecoveryLineage

    runs = tmp_path / "runs"
    for run_id, expected_condition, _subject in _scenarios(runs):
        d = _diag(runs, run_id)
        assert d.condition == expected_condition, (run_id, d.condition)
        assert isinstance(d.recovery, RecoveryLineage), run_id
        assert d.recovery.run_id == run_id


def _assert_recovery_matches_standalone(runs: Path, run_id: str) -> None:
    """Attached ``run_diagnosis(...).recovery`` == standalone ``recovery_lineage()``.

    The shared failure-resolution helper guarantees they are byte-identical, so
    the attached read-model never silently diverges on an unreadable inspected
    meta (the defect F1 closed: tolerant ``load_meta`` no longer leaks a bare
    ``none`` into the attachment).
    """
    standalone = recovery_lineage(run_id, runs_dir=runs, cwd=None)
    attached = _diag(runs, run_id).recovery
    assert attached == standalone
    assert attached is not None
    assert attached.continuation_subject == diag.SUBJECT_UNKNOWN
    assert attached.recommended_next_action == diag.ACTION_STOP_UNKNOWN
    assert attached.missing_facts == (
        diag._MISSING_SOURCE, diag._MISSING_PLAN, diag._MISSING_GATE,
        diag._MISSING_CHILD,
    )
    assert attached.reason.startswith("could not read run meta:")


def test_recovery_corrupt_meta_matches_standalone_unknown(tmp_path: Path) -> None:
    # A corrupt inspected ``meta.json`` is a read failure: the attached recovery
    # must be the same unknown/stop_unknown dead-end the standalone returns, not a
    # bare non-terminal ``none`` from the tolerant ``load_meta`` → ``{}`` path.
    runs = tmp_path / "runs"
    d = runs / "r"
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text("{ this is not json", encoding="utf-8")
    _assert_recovery_matches_standalone(runs, "r")


def test_recovery_missing_meta_matches_standalone_unknown(tmp_path: Path) -> None:
    # A run dir with no ``meta.json`` at all is likewise an inspected-meta read
    # failure → the attached recovery mirrors the standalone unknown/stop_unknown.
    runs = tmp_path / "runs"
    (runs / "r").mkdir(parents=True, exist_ok=True)
    _assert_recovery_matches_standalone(runs, "r")


# ── negative: no re-declared decision-table literals ──────────────────────────

# The value sets the ownership guard protects (status_vocab.py + commit_delivery
# halt-reason map). The diagnosis read-model must IMPORT / call predicates, never
# re-declare any of these as a local set literal.
_FORBIDDEN_SET_SIGNATURES: tuple[frozenset[str], ...] = (
    frozenset({"done", "success", "completed"}),
    frozenset({"halted", "failed", "interrupted"}),
    frozenset({"done", "failed", "halted", "cancelled"}),
    frozenset({"awaiting_phase_handoff", "interrupted"}),
)


def _string_set(node: ast.AST) -> frozenset[str] | None:
    if isinstance(node, ast.Set):
        elts = node.elts
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set"}
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Set | ast.List | ast.Tuple)
    ):
        elts = node.args[0].elts
    else:
        return None
    members: list[str] = []
    for elt in elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            members.append(elt.value)
        else:
            return None
    return frozenset(members)


def test_module_declares_no_decision_table_literal() -> None:
    """diagnosis.py composes predicates; it re-declares no guarded status set."""
    src = Path(diag.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = [
        members
        for node in ast.walk(tree)
        if (members := _string_set(node)) is not None
        and members in _FORBIDDEN_SET_SIGNATURES
    ]
    assert not hits, f"diagnosis.py re-declares a guarded decision table: {hits}"
