"""DONE summary rendering for profile-driven runs."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import sdk.phase_handoff as _sdk_handoff
from agents.entities import SubTask
from core.io.ansi import strip_ansi
from pipeline.evidence.verification_receipt import (
    COMMAND_RECEIPTS_DIRNAME,
)
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.project import (
    handoff as handoff_mod,
    handoff_advice as _adv,
    handoff_advice_policy as _policy,
)
from pipeline.project.finalization import (
    FinalizationContext,
    _phase_usage_rows,
    _render_ci_agent_advice_summary,
    _render_done_summary,
    _render_evidence_summary,
    _render_roi_summary,
    _render_task_summary,
    _subtask_usage_rows,
    finalize_with_terminal_output,
)
from pipeline.project.handoff import (
    PhaseHandoffResumeOutcome,
    process_pending_phase_handoffs,
)
from pipeline.project.handoff_advice import AdvisorResult, HandoffAdvice
from pipeline.project.handoff_advice_policy import HandoffAdvicePolicy
from pipeline.runtime import LoopStep, PhaseStep
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)


def test_done_summary_uses_profile_phase_ids_in_order() -> None:
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="alpha"),
            LoopStep(
                steps=(
                    PhaseStep(phase="beta"),
                    PhaseStep(phase="gamma"),
                ),
                until="gamma.done",
                max_rounds=2,
            ),
            PhaseStep(phase="delta"),
        ),
    )

    assert _render_done_summary(
        profile,
        {
            "alpha": {"output": "done"},
            "beta": {"skipped": "not needed"},
            "gamma": {"parse_error": "bad json"},
            "unprofiled": {"output": "ignored"},
        },
    ) == "alpha=ok | beta=skip | gamma=fail | delta=skip"


def test_done_summary_counts_resume_skips_as_already_ok() -> None:
    """A checkpoint resume may walk past phases that already ran earlier
    in the same run. The banner can mention the resume skip, but DONE chips
    should summarize the whole run history, not the last dispatcher pass."""
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="plan"),
            PhaseStep(phase="validate_plan"),
            PhaseStep(phase="implement"),
        ),
    )

    assert _render_done_summary(
        profile,
        {
            "plan": {
                "skipped": "completed earlier in this run (resumed)",
            },
            "validate_plan": {
                "skipped": "completed in prior run (resumed)",
            },
            "implement": {"skipped": "not applicable for correction route"},
        },
    ) == "plan=ok | validate_plan=ok | implement=skip"


def test_done_summary_falls_back_to_phase_log_without_profile() -> None:
    assert _render_done_summary(
        None,
        {
            "first": {"output": "done"},
            "second": {"error": "boom"},
        },
    ) == "first=ok | second=fail"


def test_done_summary_marks_halted_phase_as_halt() -> None:
    """A state.halt-driven halted IMPLEMENT renders ``implement=halt``,
    not ``ok``, even though its phase_log carries partial output."""
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="plan"),
            PhaseStep(phase="implement"),
            PhaseStep(phase="review_changes"),
        ),
    )

    assert _render_done_summary(
        profile,
        {
            "plan": {"output": "planned"},
            "implement": {"output": "partial build"},
        },
        halted_phase="implement",
    ) == "plan=ok | implement=halt | review_changes=skip"


def test_done_summary_delivery_halt_does_not_degrade_chips() -> None:
    """A delivery/no-diff halt passes ``halted_phase=None``; genuinely
    completed phases keep their real outcomes (no spurious halt chip)."""
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="plan"),
            PhaseStep(phase="implement"),
        ),
    )

    assert _render_done_summary(
        profile,
        {
            "plan": {"output": "planned"},
            "implement": {"output": "built"},
        },
        halted_phase=None,
    ) == "plan=ok | implement=ok"


def test_done_summary_marks_rejected_final_acceptance_as_reject() -> None:
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="implement"),
            PhaseStep(phase="final_acceptance"),
        ),
    )

    assert _render_done_summary(
        profile,
        {
            "implement": {"output": "built"},
            "final_acceptance": {
                "verdict": "REJECTED",
                "ship_ready": False,
            },
        },
        halted_phase=None,
    ) == "implement=ok | final_acceptance=reject"


def test_done_summary_marks_bypassed_validate_plan_as_advisory() -> None:
    """A ``small_task``-style flow rejects validate_plan and proceeds to
    implement WITHOUT a replan loop: the critique was forwarded as advisory.
    The chip must read ``advisory`` — not ``ok`` (the plan was never approved)
    and not ``halt`` (the run advanced) — while implement stays ``ok``."""
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="plan"),
            PhaseStep(phase="validate_plan"),
            PhaseStep(phase="implement"),
        ),
    )

    assert _render_done_summary(
        profile,
        {
            "plan": {"output": "planned"},
            "validate_plan": {
                "verdict": "REJECTED",
                "approved": False,
                "findings": [{"id": "F1", "severity": "P1"}],
            },
            "implement": {"output": "built"},
        },
        halted_phase=None,
    ) == "plan=ok | validate_plan=advisory | implement=ok"


def test_done_summary_provider_runtime_failure_not_rendered_as_reject() -> None:
    """ADR 0118 — a recoverable provider/runtime failure must never read as a
    review/diff rejection in the DONE summary.

    A ``provider_runtime`` failure is a hard-``failed`` run rendered by the red
    FAILED banner (``run.py``), so it never reaches this summary. This pins the
    invariant defensively: a ``final_acceptance`` / ``validate_plan`` record
    that carries only a provider-call ``error`` (and NO verdict / ship_ready /
    approved signal — the shape such a failure would leave) is never rendered
    as ``reject``; the verdict-reject branch stays gated on a genuine verdict.
    """
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="plan"),
            PhaseStep(phase="validate_plan"),
            PhaseStep(phase="implement"),
            PhaseStep(phase="final_acceptance"),
        ),
    )
    provider_error = {"error": "RateLimitError: usage limit reached for this session"}
    summary = _render_done_summary(
        profile,
        {
            "plan": {"output": "planned"},
            # provider/runtime error left on a finding-bearing phase record:
            # no verdict, no ship_ready/approved — must NOT read as reject.
            "validate_plan": dict(provider_error),
            "implement": {"output": "built"},
            "final_acceptance": dict(provider_error),
        },
        halted_phase=None,
    )
    # The verdict-reject chip never appears for a bare provider error …
    assert "reject" not in summary
    assert "advisory" not in summary
    # … the honest generic outcome is ``fail`` (a code-failure verdict was
    # NEVER reached); the reject branch stays reserved for real verdicts.
    assert summary == (
        "plan=ok | validate_plan=fail | implement=ok | final_acceptance=fail"
    )


def test_done_summary_marks_rejected_final_acceptance_still_reject() -> None:
    """Guard the other side: a GENUINE release-verdict rejection is unchanged by
    the ADR 0118 provider/runtime work — the verdict-reject branch still fires."""
    profile = SimpleNamespace(
        steps=(
            PhaseStep(phase="implement"),
            PhaseStep(phase="final_acceptance"),
        ),
    )
    assert _render_done_summary(
        profile,
        {
            "implement": {"output": "built"},
            "final_acceptance": {"verdict": "REJECTED", "ship_ready": False},
        },
        halted_phase=None,
    ) == "implement=ok | final_acceptance=reject"


def test_task_summary_uses_first_markdown_heading() -> None:
    task = """# Orcho Task: Cross Task Model Preparation

Read and follow this roadmap:
"""

    assert _render_task_summary(task) == (
        "Orcho Task: Cross Task Model Preparation"
    )


def test_task_summary_shortens_long_single_line() -> None:
    task = " ".join(["Audit current token surfaces"] * 20)

    rendered = _render_task_summary(task, width=42)

    assert rendered == "Audit current token surfaces Audit..."


def test_evidence_summary_reports_no_findings() -> None:
    assert _render_evidence_summary({"phases": {}}) == (
        "Evidence",
        "  Tasks: 0 planned · 0 completed · 0 failed · 0 incomplete",
        "  Review findings: 0",
        "  Run findings: 0",
        "  Open risks: none",
    )


def test_evidence_summary_counts_findings_by_phase_and_resolution() -> None:
    summary = _render_evidence_summary({
        "phases": {
            "validate_plan": [
                {
                    "attempt": 1,
                    "verdict": "REJECTED",
                    "findings": [
                        {"id": "F1", "severity": "P1"},
                        {"id": "F2", "severity": "P2"},
                    ],
                },
                {
                    "attempt": 2,
                    "verdict": "APPROVED",
                    "findings": [],
                },
            ],
            "review_changes": [
                {
                    "attempt": 1,
                    "verdict": "REJECTED",
                    "findings": [{"id": "R1", "severity": "P2"}],
                },
            ],
            "final_acceptance": {
                "verdict": "APPROVED",
                "ship_ready": True,
                "findings": [],
            },
            "implement": {
                "meta": {
                    "subtask_count": 3,
                    "completed_count": 2,
                    "failed_count": 1,
                    "skipped_count": 0,
                },
                "delivery_status": "repaired",
                "implementation_receipts": [
                    {"subtask_id": "T1", "state": "done"},
                    {
                        "subtask_id": "T2",
                        "state": "done",
                        "attestation_repaired": True,
                    },
                    {"subtask_id": "T3", "state": "failed"},
                ],
            },
        },
    })

    assert summary == (
        "Evidence",
        "  Tasks: 3 planned · 2 completed · 1 failed · 1 incomplete",
        "  Release: approved",
        "  Review findings: 3 | (P1=1, P2=2)"
        " | phases: validate_plan=2, review_changes=1"
        " | resolved: 2 (P1=1, P2=1) | active: 1 (P2=1)",
        "  Run findings: 3",
        "    - attestation: 1 resolved, 1 active",
        "    - handoff: 1 resolved",
        "  Open risks: review=1 run=1",
    )


def test_evidence_summary_keeps_bypassed_plan_findings_active() -> None:
    # Advisory-critique bypass path: a ``small_task``-style flow rejects
    # validate_plan and proceeds to a successful WHOLE-PLAN implement WITHOUT a
    # replan loop. The rejected plan findings were forwarded into implement as
    # advisory critique — they were NOT remediated by a later approved
    # validate_plan attempt. The DONE evidence must stay honest: those findings
    # are shown as ``advisory`` (visible, forwarded to implement), they are NOT
    # ``resolved`` (resolved stays 0) and the original REJECTED verdict remains
    # in the data — but they are NOT counted as active blocking release risks,
    # so ``Open risks`` does not flag them. The total still counts them.
    summary = _render_evidence_summary({
        "phases": {
            "validate_plan": [
                {
                    "attempt": 1,
                    "verdict": "REJECTED",
                    "findings": [
                        {"id": "F1", "severity": "P1"},
                        {"id": "F2", "severity": "P2"},
                    ],
                },
            ],
            "implement": {"output": "built"},
        },
    })

    assert summary == (
        "Evidence",
        "  Tasks: 0 planned · 0 completed · 0 failed · 0 incomplete",
        "  Review findings: 2 | (P1=1, P2=1) | phases: validate_plan=2"
        " | advisory: 2 (P1=1, P2=1) — forwarded to implement"
        " | resolved: 0 (none) | active: 0 (none)",
        "  Run findings: 0",
        "  Open risks: none",
    )


def test_evidence_summary_counts_whole_plan_implement_as_completed() -> None:
    # Direct (whole-plan) implement: no subtask-DAG meta, but the implement
    # succeeded (non-empty output, no guardrail/failed/error). The single
    # planned task (plan.total_atomic_tasks) must read as completed, so Tasks
    # shows 1 completed rather than the misleading 0 the absent DAG counters
    # would otherwise produce.
    summary = _render_evidence_summary({
        "phases": {
            "plan": {"total_atomic_tasks": 1},
            "implement": {"output": "built", "meta": {"session_id": "s1"}},
        },
    })

    assert summary == (
        "Evidence",
        "  Tasks: 1 planned · 1 completed · 0 failed · 0 incomplete",
        "  Review findings: 0",
        "  Run findings: 0",
        "  Open risks: none",
    )


def test_evidence_summary_does_not_count_failed_whole_plan_implement() -> None:
    # A stopped/failed whole-plan implement keeps the planned task uncompleted:
    # no output and a guardrail block mean completed stays 0.
    summary = _render_evidence_summary({
        "phases": {
            "plan": {"total_atomic_tasks": 1},
            "implement": {"guardrail_blocked": True},
        },
    })

    assert summary[1] == (
        "  Tasks: 1 planned · 0 completed · 0 failed · 0 incomplete"
    )


def _done_subtask(sid: str, state: str = "done") -> dict:
    # A per-subtask usage record as emitted into
    # ``metrics["subtasks"]["implement"]`` — usage plus the final ``state``.
    rec: dict[str, Any] = {
        "subtask_id": sid,
        "tokens_in": 10,
        "tokens_out": 5,
        "total_tokens": 15,
    }
    if state:
        rec["state"] = state
    return rec


def _state_marker(sid: str, state: str = "skipped") -> dict:
    # A state-only slice marker: no usage fields, just the terminal state.
    # subtask_dag ``_append_receipt_state_records`` emits these into
    # ``metrics["subtasks"]["implement"]`` for every subtask with no metered
    # usage capture — skipped ones AND run subtasks whose runtime surfaced no
    # usable outcome — so the slice is a COMPLETE receipt state mirror.
    return {"subtask_id": sid, "state": state}


def _skipped_subtask(sid: str) -> dict:
    return _state_marker(sid, "skipped")


def test_task_counts_resume_golden_full_reads_plan_total_and_slice() -> None:
    # Full delivery across resume segments: the plan attempt list carries
    # total_atomic_tasks=6, the LAST implement wave's meta only saw 3 subtasks,
    # but the cross-segment subtasks slice records all 6 as done. planned must
    # come from the plan total (6), completed from the deduped slice (6) — never
    # the last wave's meta (3). Both the Tasks line and the ROI line must agree.
    session = {
        "phases": {
            "plan": [
                {"total_atomic_tasks": 6},
                {"total_atomic_tasks": 6},
            ],
            "implement": {
                "meta": {"subtask_count": 3, "completed_count": 3},
                "output": "built",
            },
        },
    }
    metrics = {
        "total_tokens_in": 120_000,
        "total_tokens_out": 3_456,
        "total_tokens": 123_456,
        "subtasks": {
            "implement": [_done_subtask(f"T{n}") for n in range(1, 7)],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 6 planned · 6 completed · 0 failed · 0 incomplete"
    )

    roi = _render_roi_summary(session, metrics, include_accounting=False)
    assert "6/6 tasks" in roi


def test_task_counts_multi_segment_partial_counts_all_segments() -> None:
    # F1 core: a PARTIAL multi-segment resume. planned=6 from the plan total;
    # the subtasks slice holds 4 done (from EARLIER waves) + 1 failed + 1
    # incomplete (no terminal state) from the residual wave. Release is NOT
    # approved so the whole-plan collapse must NOT fire. completed must reflect
    # every segment's done receipts (4), not the last wave's meta (1) and not
    # the plan total (6).
    session = {
        "status": "halted",
        "phases": {
            "plan": {"total_atomic_tasks": 6},
            "implement": {
                # Last wave only saw a single completion — proves the count is
                # NOT taken from meta.completed_count.
                "meta": {"subtask_count": 2, "completed_count": 1},
            },
        },
    }
    metrics = {
        "subtasks": {
            "implement": [
                _done_subtask("T1"),
                _done_subtask("T2"),
                _done_subtask("T3"),
                _done_subtask("T4"),
                _done_subtask("T5", state="failed"),
                _done_subtask("T6", state=""),  # incomplete: no terminal state
            ],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 6 planned · 4 completed · 1 failed · 1 incomplete"
    )
    # Collapse did not fire: it would have zeroed failed/incomplete to 6/0/0/0.
    assert "6 completed" not in summary[1]
    assert "1 completed" not in summary[1]

    roi = _render_roi_summary(session, metrics, include_accounting=False)
    assert "4/6 tasks" in roi


def test_task_counts_slice_dedupes_retried_subtask_by_final_state() -> None:
    # No double count: a raw retry pair (T4 failed then T4 done) for one
    # subtask_id must collapse to the FINAL state (done), so T4 is counted once
    # as completed — never once as failed AND once as completed.
    session = {
        "phases": {
            "plan": {"total_atomic_tasks": 6},
            "implement": {"meta": {"subtask_count": 6, "completed_count": 6}},
        },
    }
    metrics = {
        "subtasks": {
            "implement": [
                _done_subtask("T1"),
                _done_subtask("T2"),
                _done_subtask("T3"),
                _done_subtask("T4", state="failed"),
                _done_subtask("T4", state="done"),  # retry -> final state wins
                _done_subtask("T5"),
                _done_subtask("T6"),
            ],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 6 planned · 6 completed · 0 failed · 0 incomplete"
    )


def test_task_counts_non_resumed_full_dag_reads_slice() -> None:
    # A single-segment full DAG (plan as a single mapping, all subtasks done)
    # reports 6/6 from the slice.
    session = {
        "phases": {
            "plan": {"total_atomic_tasks": 6},
            "implement": {"meta": {"subtask_count": 6, "completed_count": 6}},
        },
    }
    metrics = {
        "subtasks": {
            "implement": [_done_subtask(f"T{n}") for n in range(1, 7)],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 6 planned · 6 completed · 0 failed · 0 incomplete"
    )
    assert "6/6 tasks" in _render_roi_summary(
        session, metrics, include_accounting=False,
    )


def test_task_counts_whole_plan_without_slice_stays_one_of_one() -> None:
    # Regression: a direct whole-plan implement carries NO subtasks slice. The
    # meta-fallback + whole-plan collapse path must still read 1/1.
    session = {
        "phases": {
            "plan": {"total_atomic_tasks": 1},
            "implement": {"output": "built", "meta": {"session_id": "s1"}},
        },
    }
    metrics = {"total_tokens": 123_456}  # no ``subtasks`` key

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 1 planned · 1 completed · 0 failed · 0 incomplete"
    )
    assert "1/1 tasks" in _render_roi_summary(
        session, metrics, include_accounting=False,
    )


def test_task_counts_all_non_terminal_slice_counts_as_incomplete() -> None:
    # F1 regression: a non-empty subtasks slice whose records carry NO terminal
    # state must stay on the metrics path — every record is honest incomplete
    # work. It must NOT fall back to meta.completed_count (1 here), which would
    # print "1 completed · 0 incomplete" instead of "0 completed · 6 incomplete".
    session = {
        "status": "halted",
        "phases": {
            "plan": {"total_atomic_tasks": 6},
            "implement": {
                "meta": {"subtask_count": 6, "completed_count": 1},
            },
        },
    }
    metrics = {
        "subtasks": {
            "implement": [_done_subtask(f"T{n}", state="") for n in range(1, 7)],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 6 planned · 0 completed · 0 failed · 6 incomplete"
    )
    assert "0/6 tasks" in _render_roi_summary(
        session, metrics, include_accounting=False,
    )


def test_task_counts_skipped_dependency_read_from_metrics_slice() -> None:
    # A skipped subtask never invokes an agent (its dependency failed / a
    # stop-on-failure wave short-circuited it), so it produces NO usage capture.
    # subtask_dag folds it into the metrics slice as a state-only ``skipped``
    # marker so the slice stays the SINGLE authoritative source. With T1 failed
    # and T2 skipped both in the slice, the rollup reads 1 failed · 1 skipped ·
    # 0 incomplete — with no second read of the raw implement receipts.
    session = {
        "status": "halted",
        "phases": {
            "plan": {"total_atomic_tasks": 2},
            "implement": {
                "meta": {
                    "subtask_count": 2,
                    "completed_count": 0,
                    "failed_count": 1,
                    "skipped_count": 1,
                },
                "implementation_receipts": [
                    {"subtask_id": "T1", "state": "failed"},
                    {"subtask_id": "T2", "state": "skipped"},
                ],
            },
        },
    }
    metrics = {
        "subtasks": {
            # The ran-and-failed subtask has a usage record; the skipped one is a
            # state-only marker folded in upstream.
            "implement": [
                _done_subtask("T1", state="failed"),
                _skipped_subtask("T2"),
            ],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 2 planned · 0 completed · 1 failed · 1 skipped · 0 incomplete"
    )
    # The skipped subtask must not be silently misclassified as incomplete.
    assert "incomplete" in summary[1] and "0 incomplete" in summary[1]
    assert "0/2 tasks" in _render_roi_summary(
        session, metrics, include_accounting=False,
    )


def test_task_counts_unmetered_slice_counts_failed_and_skipped_states() -> None:
    # R1 (review): when a runtime surfaces no metered usage, subtask_dag folds
    # BOTH the failed and the skipped subtask into the slice as state-only
    # markers so it stays a COMPLETE state mirror — never a skipped-only partial
    # slice. Given that complete slice (no usage on either record), the rollup
    # must read 1 failed · 1 skipped · 0 incomplete — the unmetered failed
    # subtask must NOT be miscounted as incomplete.
    session = {
        "status": "halted",
        "phases": {
            "plan": {"total_atomic_tasks": 2},
            "implement": {
                "meta": {
                    "subtask_count": 2,
                    "completed_count": 0,
                    "failed_count": 1,
                    "skipped_count": 1,
                },
                "implementation_receipts": [
                    {"subtask_id": "T1", "state": "failed"},
                    {"subtask_id": "T2", "state": "skipped"},
                ],
            },
        },
    }
    metrics = {
        "subtasks": {
            # Neither record carries usage: the runtime published no metered
            # outcome, so both ride the slice as state-only markers.
            "implement": [
                _state_marker("T1", "failed"),
                _state_marker("T2", "skipped"),
            ],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 2 planned · 0 completed · 1 failed · 1 skipped · 0 incomplete"
    )
    assert "0/2 tasks" in _render_roi_summary(
        session, metrics, include_accounting=False,
    )
    # The unmetered subtasks carry no usage, so the "Subtask usage" block is
    # omitted entirely rather than rendering hollow tokens=0 rows.
    assert _subtask_usage_rows(metrics, include_accounting=False) == ()


def test_task_counts_slice_is_sole_source_raw_skip_receipts_ignored() -> None:
    # R1 (release gate): with a metrics slice present, completed/failed/skipped
    # come ONLY from the deduped slice — the raw ``implementation_receipts`` are
    # never consulted as a second source. Here the slice resolves T1 and T2 as
    # done, but the raw receipts carry stale/partial ``skipped`` markers,
    # including T3 which the authoritative slice never contains. If receipts were
    # overlaid, T2 could flip to skipped and T3 would appear as a phantom third
    # bucket entry, pushing the bucket sum past ``planned`` (2). They must be
    # ignored: the run reads 2 planned · 2 completed · 0 skipped · 0 incomplete.
    session = {
        "phases": {
            "plan": {"total_atomic_tasks": 2},
            "implement": {
                "meta": {"subtask_count": 2, "completed_count": 2},
                "implementation_receipts": [
                    {"subtask_id": "T2", "state": "skipped"},
                    {"subtask_id": "T3", "state": "skipped"},
                ],
            },
        },
    }
    metrics = {
        "subtasks": {
            "implement": [
                _done_subtask("T1"),
                _done_subtask("T2"),
            ],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 2 planned · 2 completed · 0 failed · 0 incomplete"
    )
    assert "2/2 tasks" in _render_roi_summary(
        session, metrics, include_accounting=False,
    )


def test_task_counts_whole_plan_collapse_never_overwrites_slice() -> None:
    # F2 regression: an implement with output present and meta WITHOUT a positive
    # subtask_count makes `_implement_whole_plan_delivered` return true. But a
    # non-empty subtasks slice is authoritative and the collapse must not fire —
    # a partial 2 done + 1 failed + 1 incomplete slice must stay honest, not be
    # rewritten to planned/0/0/0.
    session = {
        "phases": {
            "plan": {"total_atomic_tasks": 6},
            "implement": {
                "output": "built",
                "meta": {"session_id": "s1"},
            },
        },
    }
    metrics = {
        "subtasks": {
            "implement": [
                _done_subtask("T1"),
                _done_subtask("T2"),
                _done_subtask("T3", state="failed"),
                _done_subtask("T4", state=""),
            ],
        },
    }

    summary = _render_evidence_summary(session, metrics)
    assert summary[1] == (
        "  Tasks: 6 planned · 2 completed · 1 failed · 3 incomplete"
    )
    assert "6 completed" not in summary[1]
    assert "2/6 tasks" in _render_roi_summary(
        session, metrics, include_accounting=False,
    )


def test_evidence_summary_reports_rejected_release_correction_request() -> None:
    summary = _render_evidence_summary({
        "status": "halted",
        "halt_reason": "commit_decision_fix",
        "phases": {
            "implement": {
                "meta": {"subtask_count": 4, "completed_count": 4},
            },
            "final_acceptance": {
                "verdict": "REJECTED",
                "ship_ready": False,
                "findings": [{"severity": "P1"}],
                "verification_gaps": [{"risk": "missing regression"}],
                "release_blockers": [{"id": "R1"}],
            },
        },
    })

    assert summary == (
        "Evidence",
        "  Tasks: 4 planned · 4 completed · 0 failed · 0 incomplete",
        "  Release: rejected -> correction requested",
        "  Release blockers: 1",
        "    - R1: untitled",
        "  Review findings: 1 | (P1=1) | phases: final_acceptance=1"
        " | resolved: 0 (none) | active: 1 (P1=1)",
        "  Run findings: 2",
        "    - verification: 1 active",
        "    - delivery: 1 active",
        "  Open risks: review=1 run=2",
    )


def test_evidence_summary_does_not_claim_unknown_delivery_was_blocked() -> None:
    # A terminal ``done`` run whose release was REJECTED but which carries no
    # canonical delivery record has an unknown delivery disposition. It must
    # remain a rejected release, but cannot claim delivery was blocked.
    summary = _render_evidence_summary({
        "status": "done",
        "phases": {
            "implement": {
                "meta": {"subtask_count": 2, "completed_count": 2},
            },
            "final_acceptance": {
                "verdict": "REJECTED",
                "ship_ready": False,
                "findings": [{"severity": "P1"}],
            },
        },
    })

    assert "  Release: rejected" in summary
    assert not any("delivery blocked" in line for line in summary)
    # The correction-request phrasing belongs only to the commit_decision_fix
    # halt path and must NOT appear for a plain done+rejected run.
    assert "  Release: rejected -> correction requested" not in summary


def test_evidence_summary_reports_non_convergence_block() -> None:
    # ADR 0098: a fixed-point correction carries the durable
    # ``correction_fixed_point`` block. The evidence summary surfaces a
    # non-convergence line with the repeated identities + parent/child run ids,
    # so the halted outcome never reads as a green DONE.
    summary = _render_evidence_summary({
        "status": "halted",
        "halt_reason": "correction_not_converging",
        "correction_fixed_point": {
            "repeated": [
                "final_acceptance|release_blocker|r1|p0|pipeline/foo.py",
            ],
            "parent_run_id": "20260619_parent",
            "child_run_id": "20260619_child",
            "suggested_actions": ["retry with new instructions"],
            "reason": "no relevant progress",
        },
        "phases": {
            "correction_triage": {
                "kind": "code_fix",
                "route": {"kind": "code_fix", "skip_phases": [], "halt": False},
            },
            "implement": {},
            "final_acceptance": {
                "verdict": "REJECTED",
                "ship_ready": False,
                "release_blockers": [{"id": "R1", "severity": "P0"}],
                "findings": [{"severity": "P0"}],
            },
        },
    })

    assert "  Correction: not converging" in summary
    assert (
        "    repeated blockers: "
        "final_acceptance|release_blocker|r1|p0|pipeline/foo.py" in summary
    )
    assert (
        "    parent run: 20260619_parent | child run: 20260619_child" in summary
    )


def test_evidence_summary_omits_non_convergence_for_normal_runs() -> None:
    # A run without the fixed-point block renders no non-convergence line, and
    # the existing lines/order are untouched.
    summary = _render_evidence_summary({"phases": {}})
    assert summary == (
        "Evidence",
        "  Tasks: 0 planned · 0 completed · 0 failed · 0 incomplete",
        "  Review findings: 0",
        "  Run findings: 0",
        "  Open risks: none",
    )
    assert not any("not converging" in line for line in summary)


def test_halt_banner_correction_not_converging_is_amber() -> None:
    from core.io.ansi import C
    from pipeline.project.finalization import _halt_banner

    label, color = _halt_banner("correction_not_converging")
    assert "not converging" in label
    assert color == C.YELLOW


def test_evidence_summary_labels_approved_correction_followup_without_tasks() -> None:
    summary = _render_evidence_summary({
        "status": "done",
        "phases": {
            "correction_triage": {
                "kind": "code_fix",
                "route": {"kind": "code_fix", "skip_phases": [], "halt": False},
            },
            "implement": {},
            "final_acceptance": {
                "verdict": "APPROVED",
                "ship_ready": True,
                "findings": [],
            },
        },
    })

    assert summary == (
        "Evidence",
        "  Tasks: correction follow-up (no subtask plan)",
        "  Correction: code_fix -> full correction path",
        "  Release: approved",
        "  Review findings: 0",
        "  Run findings: 0",
        "  Open risks: none",
    )


def test_evidence_summary_separates_environment_attestation_and_handoff() -> None:
    summary = _render_evidence_summary({
        "phases": {
            "validate_plan": [
                {"attempt": 1, "verdict": "REJECTED", "findings": []},
                {"attempt": 2, "verdict": "APPROVED", "findings": []},
            ],
            "implement": {
                "meta": {"subtask_count": 5, "completed_count": 5},
                "implementation_receipts": [
                    {
                        "subtask_id": "T1",
                        "state": "done",
                        "attestation_repaired": True,
                        "attestation_error": (
                            "environment preflight used stable install"
                        ),
                    },
                    {
                        "subtask_id": "T2",
                        "state": "done",
                        "attestation_repaired": True,
                    },
                ],
            },
        },
    })

    assert summary == (
        "Evidence",
        "  Tasks: 5 planned · 5 completed · 0 failed · 0 incomplete",
        "  Review findings: 0",
        "  Run findings: 3",
        "    - environment: 1 resolved",
        "    - attestation: 1 resolved",
        "    - handoff: 1 resolved",
        "  Open risks: none",
    )


def test_roi_summary_without_money_accounting_reports_token_roi() -> None:
    session = {
        "phases": {
            "implement": {
                "meta": {"subtask_count": 2, "completed_count": 2},
            },
            "review_changes": {
                "verdict": "REJECTED",
                "findings": [{"severity": "P1"}],
            },
        },
    }
    metrics = {
        "total_tokens_in": 120_000,
        "total_tokens_out": 3_456,
        "total_tokens": 123_456,
        "total_cost_usd_equivalent": 1.23,
    }

    assert _render_roi_summary(
        session,
        metrics,
        include_accounting=False,
    ) == (
        "ROI: tokens=123,456 (in=120,000 out=3,456) "
        "Outcome: 2/2 tasks, 0 run findings, 1 review findings"
    )


def test_roi_summary_counts_whole_plan_implement_as_completed_task() -> None:
    # A direct (whole-plan) successful implement must show '1/1 tasks' in the
    # ROI Outcome, not the misleading '0/1' the absent subtask-DAG counters
    # would otherwise yield.
    session = {
        "phases": {
            "plan": {"total_atomic_tasks": 1},
            "implement": {"output": "built", "meta": {"session_id": "s1"}},
        },
    }
    metrics = {
        "total_tokens_in": 120_000,
        "total_tokens_out": 3_456,
        "total_tokens": 123_456,
    }

    line = _render_roi_summary(session, metrics, include_accounting=False)
    assert "1/1 tasks" in line
    assert line == (
        "ROI: tokens=123,456 (in=120,000 out=3,456) "
        "Outcome: 1/1 tasks, 0 run findings, 0 review findings"
    )


def test_roi_summary_reports_correction_release_outcome_without_fake_zero_tasks(
) -> None:
    session = {
        "phases": {
            "correction_triage": {
                "kind": "code_fix",
                "route": {"kind": "code_fix", "skip_phases": [], "halt": False},
            },
            "final_acceptance": {
                "verdict": "APPROVED",
                "ship_ready": True,
                "findings": [],
            },
        },
    }
    metrics = {
        "total_tokens_in": 120_000,
        "total_tokens_out": 3_456,
        "total_tokens": 123_456,
    }

    assert _render_roi_summary(
        session,
        metrics,
        include_accounting=False,
    ) == (
        "ROI: tokens=123,456 (in=120,000 out=3,456) "
        "Outcome: correction=code_fix, release=approved, "
        "0 run findings, 0 review findings"
    )


def test_roi_summary_with_money_accounting_reports_tokens_and_cost() -> None:
    session = {
        "phases": {
            "implement": {
                "meta": {"subtask_count": 2, "completed_count": 2},
            },
            "validate_plan": [
                {
                    "verdict": "REJECTED",
                    "findings": [{"severity": "P1"}],
                },
                {"verdict": "APPROVED", "findings": []},
            ],
            "review_changes": {
                "verdict": "REJECTED",
                "findings": [{"severity": "P2"}],
            },
        },
    }
    metrics = {
        "total_tokens_in": 120_000,
        "total_tokens_out": 3_456,
        "total_tokens": 123_456,
        "total_cost_usd_equivalent": 1.23,
    }

    assert _render_roi_summary(
        session,
        metrics,
        include_accounting=True,
    ) == (
        "ROI: tokens=123,456 (in=120,000 out=3,456) "
        "cost_ref=runtime-reported:$1.23 "
        "Outcome: 2/2 tasks, 1/1 run findings resolved, 2 review findings"
    )


def test_roi_summary_marks_estimated_cost() -> None:
    assert _render_roi_summary(
        {"phases": {}},
        {
            "total_tokens_in": 120_000,
            "total_tokens_out": 3_456,
            "total_tokens": 123_456,
            "total_cost_usd_equivalent": 1.23,
            "cost_estimated": True,
        },
        include_accounting=True,
    ) == (
        "ROI: tokens=123,456 (in=120,000 out=3,456) "
        "cost_ref=estimated-api:~$1.23 "
        "Outcome: 0 tasks, 0 run findings, 0 review findings"
    )


def test_roi_summary_does_not_invent_cost_when_accounting_cost_is_missing() -> None:
    assert _render_roi_summary(
        {"phases": {}},
        {
            "total_tokens_in": 120_000,
            "total_tokens_out": 3_456,
            "total_tokens": 123_456,
        },
        include_accounting=True,
    ) == (
        "ROI: tokens=123,456 (in=120,000 out=3,456) "
        "Outcome: 0 tasks, 0 run findings, 0 review findings"
    )


def test_phase_usage_rows_include_cost_only_with_money_accounting() -> None:
    metrics = {
        "phases": {
            "plan": {
                "tokens_in": 1000,
                "tokens_out": 200,
                "total_tokens": 1200,
                "duration_s": 12.34,
                "attempts": 1,
                "cost_usd_equivalent": 0.12,
            },
            "review_changes": {
                "tokens_in": 2000,
                "tokens_out": 300,
                "total_tokens": 2300,
                "duration_s": 23.45,
                "attempts": 2,
                "cost_usd_equivalent": 0.34,
                "cost_estimated": True,
            },
            "final_acceptance": {
                "tokens_in": 3000,
                "tokens_in_cache_read": 2400,
                "tokens_out": 400,
                "total_tokens": 3400,
                "duration_s": 34.56,
                "attempts": 1,
            },
        },
    }

    assert _phase_usage_rows(metrics, include_accounting=False) == (
        "Usage by phase:",
        "  plan                   tokens=      1,200 "
        "(in=1,000 out=200) time=12.3s",
        "  review_changes         tokens=      2,300 "
        "(in=2,000 out=300) time=23.4s attempts=2",
        "  final_acceptance       tokens=      3,400 "
        "(in=3,000 cached=2,400 out=400) time=34.6s",
    )
    assert _phase_usage_rows(metrics, include_accounting=True) == (
        "Usage by phase:",
        "  plan                   tokens=      1,200 "
        "(in=1,000 out=200) time=12.3s "
        "cost_ref=runtime-reported:$0.12",
        "  review_changes         tokens=      2,300 "
        "(in=2,000 out=300) time=23.4s attempts=2 "
        "cost_ref=estimated-api:~$0.34",
        "  final_acceptance       tokens=      3,400 "
        "(in=3,000 cached=2,400 out=400) time=34.6s cost_ref=—",
    )


def _subtask_metrics() -> dict:
    return {
        "subtasks": {
            "implement": [
                {
                    "subtask_id": "T1-register",
                    "tokens_in": 1000,
                    "tokens_out": 200,
                    "total_tokens": 1200,
                    "duration_s": 12.3,
                    "tool_calls": 4,
                    "cost_usd_equivalent": 0.12,
                    "declared_files": ["a.py", "b.py"],
                },
                {
                    "subtask_id": "T2-modify",
                    "tokens_in": 2000,
                    "tokens_out": 300,
                    "total_tokens": 2300,
                    "duration_s": 23.4,
                    "tool_calls": 7,
                    "cost_usd_equivalent": 0.34,
                },
            ],
        },
    }


def test_subtask_usage_rows_render_per_subtask_block() -> None:
    metrics = _subtask_metrics()

    # No accounting: tokens/time/tools/files, but no cost.
    assert _subtask_usage_rows(metrics, include_accounting=False) == (
        "Subtask usage:",
        "  T1-register            "
        "tokens=1,200 (in=1,000 out=200) time=12.3s tools=4 files=2",
        "  T2-modify              "
        "tokens=2,300 (in=2,000 out=300) time=23.4s tools=7",
    )
    # With accounting: cost_ref injected before time, only when cost present.
    assert _subtask_usage_rows(metrics, include_accounting=True) == (
        "Subtask usage:",
        "  T1-register            "
        "tokens=1,200 (in=1,000 out=200) cost_ref=runtime-reported:$0.12 "
        "time=12.3s tools=4 files=2",
        "  T2-modify              "
        "tokens=2,300 (in=2,000 out=300) "
        "cost_ref=runtime-reported:$0.34 time=23.4s tools=7",
    )


def test_subtask_usage_rows_skips_state_only_skipped_records() -> None:
    # Skipped subtasks ride the slice as state-only markers so the rollup can
    # count them, but they carry no usage — the "Subtask usage" block is
    # attribution evidence, so a skipped marker must not render a hollow
    # tokens=0 row.
    metrics = {
        "subtasks": {
            "implement": [
                {
                    "subtask_id": "T1-register",
                    "tokens_in": 1000,
                    "tokens_out": 200,
                    "total_tokens": 1200,
                    "duration_s": 12.3,
                    "tool_calls": 4,
                },
                {"subtask_id": "T2-skipped", "state": "skipped"},
            ],
        },
    }

    rows = _subtask_usage_rows(metrics, include_accounting=False)
    assert rows == (
        "Subtask usage:",
        "  T1-register            "
        "tokens=1,200 (in=1,000 out=200) time=12.3s tools=4",
    )
    assert not any("T2-skipped" in row for row in rows)


def test_subtask_usage_rows_all_skipped_renders_no_block() -> None:
    # A wave where every subtask was skipped (all state-only markers) has no
    # budget consumer to show, so the block is omitted entirely rather than
    # rendering a bare "Subtask usage:" header.
    metrics = {
        "subtasks": {
            "implement": [
                {"subtask_id": "T1", "state": "skipped"},
                {"subtask_id": "T2", "state": "skipped"},
            ],
        },
    }
    assert _subtask_usage_rows(metrics, include_accounting=True) == ()


def test_subtask_usage_rows_empty_without_records() -> None:
    # whole_plan / non-subtask runs: no subtasks key at all → empty tuple,
    # so the summary renders no block and no misleading empty warning.
    assert _subtask_usage_rows({"phases": {}}, include_accounting=True) == ()
    assert _subtask_usage_rows(
        {"subtasks": {}}, include_accounting=True,
    ) == ()
    assert _subtask_usage_rows(
        {"subtasks": {"implement": []}}, include_accounting=True,
    ) == ()


# ── T4: 'Agent advice' block from the ci_agent aggregate ────────────────────


def test_render_ci_agent_advice_summary_with_retries() -> None:
    block = _render_ci_agent_advice_summary({
        "_ci_agent_advice": {
            "retries": 1, "resolved": 1, "stopped": 0,
            "last_recommendation": "retry_feedback", "last_confidence": "high",
        },
    })
    assert block is not None
    assert "Agent advice:" in block
    assert "ci_agent retries=1 resolved=1 stopped=0" in block
    assert "last recommendation=retry_feedback confidence=high" in block


def test_render_ci_agent_advice_summary_none_when_no_retries() -> None:
    assert _render_ci_agent_advice_summary({
        "_ci_agent_advice": {"retries": 0, "resolved": 0, "stopped": 0},
    }) is None
    assert _render_ci_agent_advice_summary({}) is None


# ── end-to-end: T3 integration → T4 finalization summary ────────────────────


def _advice(
    *,
    action: str = "retry_feedback",
    confidence: str = "high",
    expected_files: tuple[str, ...] = ("a.py",),
) -> HandoffAdvice:
    return HandoffAdvice(
        recommended_action=action,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        rationale="because",
        retry_feedback="Add the missing null check and a regression test.",
        expected_files=expected_files,
    )


def _signal() -> SimpleNamespace:
    return SimpleNamespace(
        handoff_id="h1", phase="implement", trigger="rejected", verdict="REJECTED",
        approved=False, available_actions=("retry_feedback",),
        artifacts={"findings": []}, last_output="reviewer rejected the change",
        round=1, loop_max_rounds=1,
    )


def _plan() -> ParsedPlan:
    return ParsedPlan(
        subtasks=(SubTask(id="t1", goal="g"),), source="json", owned_files=("a.py",),
    )


class _ResumeScript:
    def __init__(self, steps, *, new_signal=None) -> None:
        self.steps = list(steps)
        self.new_signal = new_signal
        self.calls = 0

    def __call__(self, run, profile, ctx, *, on_round_end=None):
        self.calls += 1
        if self.steps.pop(0) == "new_handoff":
            run.state.phase_handoff_request = self.new_signal
            return PhaseHandoffResumeOutcome(
                profile=profile, completed_phases=frozenset(), paused=True,
            )
        return PhaseHandoffResumeOutcome(
            profile=None, completed_phases=frozenset(), paused=False,
        )


def _make_run(tmp_path: Path) -> SimpleNamespace:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    state = SimpleNamespace(
        phase_handoff_request=_signal(), extras={}, task="do a thing",
        parsed_plan=_plan(), phase_config=None, halt=False, halt_reason=None,
        phase_log={},
    )
    run = SimpleNamespace(
        state=state, no_interactive=True,
        session={"status": "awaiting_phase_handoff"}, session_ts="20260613_1",
        output_dir=run_dir, git_cwd=str(run_dir), registry=None, _ckpt=None,
        _dispatch_active=False,
        # finalize surface
        task="# Orcho Task: ci advice", profile_name="default",
        parent_run_id=None, project_alias=None, project_path=project_dir,
        worktree_context=None, _done_summary_profile=None,
        _worktree_cvar_token=None, _sandbox_cvar_token=None,
        _metrics=SimpleNamespace(
            save=lambda d: d / "metrics.json", summary_line=lambda: "Tokens: 0",
            as_dict=lambda: {}, phases=[],
        ),
        _effective_diff_cwd=lambda: project_dir,
        _commit_delivery_baseline=lambda: "HEAD",
        _run_commit_delivery=lambda diff_cwd: None,
    )
    return run


def _wire_finalize_stubs(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.engine.run_diff.capture_run_diff", lambda *a, **k: None,
    )

    def _save(output_dir, _session):
        p = output_dir / "session.json"
        p.write_text("{}", encoding="utf-8")
        return p

    monkeypatch.setattr("pipeline.project.finalization.save_session", _save)
    monkeypatch.setattr(
        "pipeline.evidence.write_bundle_or_placeholder",
        lambda output_dir, *, run_id, status: output_dir / "evidence.json",
    )
    monkeypatch.setattr(
        "pipeline.engine.artifact_mirror.mirror_to_projects", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "pipeline.observability.context_pressure.format_context_summary",
        lambda _session: None,
    )
    monkeypatch.setattr(
        "core.infra.config.AppConfig.load",
        lambda: SimpleNamespace(artifacts={}, commit={}, accounting={}),
    )


def test_e2e_done_summary_reflects_real_ci_agent_aggregate(
    tmp_path, capsys, monkeypatch,
) -> None:
    # Stage 1: real T3 integration, resolved scenario → aggregate.
    monkeypatch.setattr(handoff_mod, "apply_phase_handoff_pause", lambda run: None)
    monkeypatch.setattr(_sdk_handoff, "phase_handoff_decide", lambda *a, **k: None)
    monkeypatch.setattr(
        _adv, "invoke_advisor",
        lambda run, ctx, **k: AdvisorResult(advice=_advice(), raw="{}", usage={}),
    )
    monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners",
        _ResumeScript(["resolve"]),
    )
    run = _make_run(tmp_path)
    process_pending_phase_handoffs(run, profile="P", ctx="C")
    assert run.state.extras["_ci_agent_advice"]["resolved"] == 1
    capsys.readouterr()  # drop stage-1 output

    # Stage 2: finalize the same run (DONE) and inspect the printed block.
    _wire_finalize_stubs(monkeypatch)
    finalize_with_terminal_output(FinalizationContext(run=run))
    out = strip_ansi(capsys.readouterr().out)
    assert "Pipeline complete" in out
    # T3: the DONE summary now renders the UNIFIED Agent-advice block from the
    # durable advice digest (collect_handoff_advice over the run dir) so its
    # counts match the evidence section — not the in-memory aggregate. One CI
    # advice artifact was persisted this run; the resolved/stopped split of the
    # in-memory aggregate is asserted above via run.state.extras.
    assert "Agent advice:" in out
    assert "calls=1 applied_retries=0 resolved=0 repeated=0 stopped=1" in out


def test_finalize_banner_approved_release_stays_green(
    tmp_path, capsys, monkeypatch,
) -> None:
    # An APPROVED release on a terminal ``done`` run keeps the green
    # 'Pipeline complete' headline and the 'Release: approved' evidence line.
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    run.session["phases"] = {
        "final_acceptance": {"verdict": "APPROVED", "ship_ready": True},
    }

    finalize_with_terminal_output(FinalizationContext(run=run))
    out = strip_ansi(capsys.readouterr().out)

    assert "Pipeline complete" in out
    assert "Release: approved" in out
    assert "DELIVERY BLOCKED" not in out


def test_finalize_banner_done_rejected_release_is_not_green(
    tmp_path, capsys, monkeypatch,
) -> None:
    # ADR 0106: a run whose release was REJECTED with no applied delivery
    # (delivery stubbed to a no-op here) must NOT finish as a green
    # 'Pipeline complete'. Finalization flips it to an actionable HALTED
    # terminal with an explicit ``final_acceptance_rejected`` reason and an
    # honest amber halt header — never a silent successful done.
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    run.session["phases"] = {
        "final_acceptance": {"verdict": "REJECTED", "ship_ready": False},
    }

    finalize_with_terminal_output(FinalizationContext(run=run))
    out = strip_ansi(capsys.readouterr().out)

    assert run.session["status"] == "halted"
    assert run.session["halt_reason"] == "final_acceptance_rejected"
    assert "Pipeline complete" not in out
    assert "HALTED" in out
    assert "release rejected" in out
    assert "Release: rejected" in out


def test_e2e_halted_summary_reflects_real_ci_agent_aggregate(
    tmp_path, capsys, monkeypatch,
) -> None:
    # Stage 1: real T3 integration, halt-after-one-retry scenario. The CI halt
    # path sets state.halt itself and returns continue_dispatch (NOT halted), so
    # the real caller (profile_dispatch) falls through to run.finalize().
    monkeypatch.setattr(handoff_mod, "apply_phase_handoff_pause", lambda run: None)
    monkeypatch.setattr(_sdk_handoff, "phase_handoff_decide", lambda *a, **k: None)
    monkeypatch.setattr(handoff_mod, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(
        _policy, "resolve_handoff_advice_policy",
        lambda run: HandoffAdvicePolicy(auto_retry_with_agent=True, max_agent_retries=2),
    )
    advice_seq = iter([_advice(), _advice(action="halt")])
    monkeypatch.setattr(
        _adv, "invoke_advisor",
        lambda run, ctx, **k: AdvisorResult(
            advice=next(advice_seq), raw="{}", usage={},
        ),
    )
    monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners",
        _ResumeScript(["new_handoff"], new_signal=_signal()),
    )
    run = _make_run(tmp_path)
    result = process_pending_phase_handoffs(run, profile="P", ctx="C")
    # The integration set the halt — NOT the test — so the real caller path runs.
    assert result.halted is False
    assert result.continue_dispatch is True
    assert run.state.halt is True
    assert run.state.halt_reason == "phase_handoff_halt"
    agg = run.state.extras["_ci_agent_advice"]
    assert agg["retries"] == 1
    assert agg["resolved"] == 0
    assert agg["stopped"] == 1
    capsys.readouterr()  # drop stage-1 output

    # Stage 2: emulate the caller's `else: return run.finalize()` branch — since
    # the loop result is continue_dispatch, the run finalizes, and state.halt
    # (set by the integration) drives _resolve_terminal_status to a HALTED banner
    # carrying the Agent advice block.
    assert not (result.paused or result.halted)  # caller would call finalize()
    _wire_finalize_stubs(monkeypatch)
    finalize_with_terminal_output(FinalizationContext(run=run))
    out = strip_ansi(capsys.readouterr().out)
    assert "HALTED" in out
    # T3: unified Agent-advice block from the durable digest. This run persisted
    # two divergent advice artifacts (retry then halt); neither decision was
    # applied on disk, so both durable outcomes classify as 'stopped'. The
    # in-memory retries/resolved/stopped aggregate is asserted above.
    assert "Agent advice:" in out
    assert "calls=2 applied_retries=0 resolved=0 repeated=0 stopped=2" in out


def test_paused_needs_operator_stop_tracked_in_extras_not_summary(
    tmp_path, capsys, monkeypatch,
) -> None:
    # A paused needs_operator stop (here: out_of_scope) never reaches DONE/HALTED
    # finalization — its outcome is asserted via the persisted aggregate.
    monkeypatch.setattr(handoff_mod, "apply_phase_handoff_pause", lambda run: None)
    monkeypatch.setattr(_sdk_handoff, "phase_handoff_decide", lambda *a, **k: None)
    monkeypatch.setattr(
        _adv, "invoke_advisor",
        lambda run, ctx, **k: AdvisorResult(
            advice=_advice(expected_files=("other/secret.py",)), raw="{}", usage={},
        ),
    )
    monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", _ResumeScript([]),
    )
    run = _make_run(tmp_path)
    result = process_pending_phase_handoffs(run, profile="P", ctx="C")
    assert result.paused is True
    agg = run.state.extras["_ci_agent_advice"]
    assert agg["retries"] == 0
    assert agg["stopped"] == 1
    assert agg["resolved"] == 0


# ── T3: 'Verification gates' DONE block ─────────────────────────────────────


def _gate_contract(
    required: list[str],
    *,
    default_env: str = "core-local",
    delivery_policy: str | None = None,
) -> Any:
    commands = {name: {"run": "true"} for name in required}
    verification: dict[str, Any] = {
        "default_env": default_env,
        "required": list(required),
        "commands": commands,
    }
    if delivery_policy is not None:
        verification["delivery_policy"] = delivery_policy
    plugin = PluginConfig(
        verification_envs={default_env: {}},
        verification=verification,
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _write_command_receipt(
    run_dir: Path, command: str, *, exit_code: int = 0, env: str = "core-local",
    assertions: list[dict[str, Any]] | None = None, detail: str = "",
) -> None:
    rdir = run_dir / COMMAND_RECEIPTS_DIRNAME
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{command}.json").write_text(
        json.dumps({
            "kind": "verification_command",
            "command": command,
            "env": env,
            "exit_code": exit_code,
            "assertions": assertions or [],
            "detail": detail,
            "git": {
                "checkout_head": None,
                "baseline_head": None,
                "changed_files_fingerprint": None,
            },
            "dependencies": [],
        }),
        encoding="utf-8",
    )


def _install_gate_contract(run: SimpleNamespace, contract: Any) -> None:
    run.state.extras["verification_contract"] = contract
    run.state.extras["verification_placeholders"] = placeholder_context_for(
        contract,
        checkout=str(run.project_path),
        project=str(run.project_path),
        workspace=str(run.output_dir),
        run_dir=str(run.output_dir),
    )


def _gate_events(run: SimpleNamespace, records: list[dict[str, Any]]) -> None:
    from pipeline.project.gate_repair import VERIFICATION_GATE_EVENTS_KEY

    run.state.extras[VERIFICATION_GATE_EVENTS_KEY] = records


def test_done_block_pre_final_autorun_with_receipts(
    tmp_path, capsys, monkeypatch,
) -> None:
    """A Stage 9 pre-final auto-run that ran its required commands renders one
    autorun event with ran/pass plus the on-disk receipt names."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    contract = _gate_contract(["lint", "unit"])
    _install_gate_contract(run, contract)
    for name in ("lint", "unit"):
        _write_command_receipt(run.output_dir, name)
    run.state.extras["verification_autorun"] = [
        {
            "attempted": True, "source": "stage9_autorun",
            "phase": "final_acceptance",
            "ran_commands": ["lint", "unit"], "failed": [],
            "skipped_fresh": [], "skipped_manual": [], "receipt_paths": [],
        },
    ]

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "Verification gates:" in out
    assert "events: 1 official gate events" in out
    assert "pre-final auto-run: 2 ran/pass" in out
    assert "receipts: lint, unit" in out


def test_done_block_correction_pre_review_skipped_fresh(
    tmp_path, capsys, monkeypatch,
) -> None:
    """A correction pre-review auto-run whose required receipts were already
    fresh renders the autorun event with a 'skipped fresh' bucket."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    # No contract -> no residual; the durable trail is the source of truth.
    run.state.extras["verification_autorun"] = [
        {
            "attempted": True, "source": "correction_pre_review",
            "phase": "review_changes",
            "ran_commands": [], "failed": [],
            "skipped_fresh": ["lint", "unit"], "skipped_manual": [],
            "receipt_paths": [],
        },
    ]

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "Verification gates:" in out
    assert "correction pre-review auto-run: 2 skipped fresh" in out


def test_done_block_scheduled_after_implement_ran_pass(
    tmp_path, capsys, monkeypatch,
) -> None:
    """Scheduled after_phase(implement) executed_pass decisions group into a
    single per-hook 'N ran/pass' event line."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    _gate_events(run, [
        {"hook": "after_phase", "phase": "implement", "command": c,
         "gate_set": "core", "decision": "executed_pass"}
        for c in ("lint", "unit", "smoke")
    ])

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "events: 1 official gate events" in out
    assert "after_phase(implement): 3 ran/pass" in out


def test_done_block_scheduled_before_delivery_mixed(
    tmp_path, capsys, monkeypatch,
) -> None:
    """A before_delivery hook that executed one gate and found two others fresh
    (without running them) renders a MIXED 'ran/pass, skipped fresh' line —
    fresh-without-run is shown as 'skipped fresh', never inferred ran/pass."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    _gate_events(run, [
        {"hook": "before_delivery", "phase": "", "command": "lint",
         "gate_set": "core", "decision": "executed_pass"},
        {"hook": "before_delivery", "phase": "", "command": "unit",
         "gate_set": "core", "decision": "skipped_fresh"},
        {"hook": "before_delivery", "phase": "", "command": "smoke",
         "gate_set": "core", "decision": "skipped_fresh"},
    ])

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "before_delivery: 1 ran/pass, 2 skipped fresh" in out


def test_done_block_failed_and_residual(tmp_path, capsys, monkeypatch) -> None:
    """An executed_fail decision renders 'ran/fail'; a required command with no
    receipt surfaces on the run-level 'residual: missing=...' line."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    contract = _gate_contract(["lint", "unit"])
    _install_gate_contract(run, contract)
    _gate_events(run, [
        {"hook": "after_phase", "phase": "implement", "command": "lint",
         "gate_set": "core", "decision": "executed_fail"},
    ])
    # lint failed on disk (classified failed, not residual); unit never ran ->
    # residual missing.
    _write_command_receipt(run.output_dir, "lint", exit_code=1)

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "after_phase(implement): 1 ran/fail" in out
    assert "residual: missing=unit" in out


def test_done_block_autorun_exit0_assertion_fail_renders_ran_fail(
    tmp_path, capsys, monkeypatch,
) -> None:
    """An auto-run command that exited 0 but whose receipt failed an assertion is
    carried in the trail's authoritative ``failed`` set, so the DONE timeline
    shows 'ran/fail', never a false-green 'ran/pass'."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    contract = _gate_contract(["lint"])
    _install_gate_contract(run, contract)
    # Exit 0 on disk, but a failed assertion -> authoritatively failed.
    _write_command_receipt(
        run.output_dir, "lint", exit_code=0,
        assertions=[{"name": "no-warnings", "passed": False}],
    )
    run.state.extras["verification_autorun"] = [
        {
            "attempted": True, "source": "stage9_autorun",
            "phase": "final_acceptance",
            "ran_commands": ["lint"], "failed": ["lint"],
            "skipped_fresh": [], "skipped_manual": [], "receipt_paths": [],
        },
    ]

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "pre-final auto-run: 1 ran/fail" in out
    assert "ran/pass" not in out


def test_done_block_stale_residual_carries_non_alarming_legend() -> None:
    """A pure ``stale`` residual (a gate that passed, then had HEAD move under
    it — e.g. the delivery commit) renders the ``stale=`` term WITH a ``note:``
    legend that explains the post-commit fingerprint shift, so a clean direct
    delivery never reads as a failed check. missing/failed buckets are absent
    here, so the legend is the only added line."""
    from pipeline.project.verification_timeline import (
        VerificationGateEvent,
        VerificationTimeline,
        render_verification_gate_done_block,
    )

    timeline = VerificationTimeline(
        events=(
            VerificationGateEvent(
                hook_label="after_phase(implement)", source="scheduled",
                ran_pass=("smoke",),
            ),
        ),
        residual_stale=("smoke",),
        receipts=("smoke",),
    )

    lines = render_verification_gate_done_block(timeline)
    block = "\n".join(lines)

    # The stale term is present...
    assert "residual: stale=smoke" in block
    # ...but it is explained, not left as a bare alarming token.
    note = next((line for line in lines if line.strip().startswith("note:")), None)
    assert note is not None
    assert "stale =" in note
    assert "not a failed check" in note
    assert "HEAD move" in note
    # No missing/failed buckets leaked into this stale-only case.
    assert "missing=" not in block
    assert "failed=" not in block


def test_done_block_waived_required_gate_renders_waived_line(
    tmp_path, monkeypatch,
) -> None:
    """A required gate whose failed receipt is excused by an exact durable waiver
    (``gate:<command>:<round>``) is pulled out of the residual/blocking buckets
    and rendered on its own ``waived (operator): ...`` line — never as failed,
    never as blocking, never as passed."""
    run = _make_run(tmp_path)
    contract = _gate_contract(["broad-non-e2e"], delivery_policy="require")
    _install_gate_contract(run, contract)
    # Failed receipt on disk → classified failed → would be a require blocker...
    _write_command_receipt(run.output_dir, "broad-non-e2e", exit_code=1)
    # ...but a durable waiver for exactly this gate excuses it.
    run.state.extras["phase_handoff_waiver"] = {
        "handoff_id": "gate:broad-non-e2e:1",
        "phase": "final_acceptance",
        "waiver_text": "accepted: pre-existing failure on this checkout",
    }

    from pipeline.project.verification_timeline import (
        build_verification_timeline,
        render_verification_gate_done_block,
    )

    timeline = build_verification_timeline(
        run_dir=run.output_dir, extras=run.state.extras, session=run.session,
    )
    assert timeline is not None
    # Criterion: in waived, absent from blocking_residual, out of residual_failed.
    assert "broad-non-e2e" in timeline.waived
    assert "broad-non-e2e" not in timeline.blocking_residual
    assert timeline.residual_failed == ()

    block = "\n".join(render_verification_gate_done_block(timeline))
    assert (
        "waived (operator): broad-non-e2e (handoff gate:broad-non-e2e:1) "
        "— required gate accepted via durable waiver"
    ) in block
    # Not shown as a failed residual or a require blocker, and no stale fix hint.
    assert "failed=broad-non-e2e" not in block
    assert "blocking (require): broad-non-e2e" not in block
    assert "fix:" not in block


def test_wrong_gate_waiver_keeps_gate_blocking_in_timeline(
    tmp_path, monkeypatch,
) -> None:
    """A waiver naming a DIFFERENT gate does not excuse the real require gap: the
    failed command stays blocking and is not surfaced as waived."""
    run = _make_run(tmp_path)
    contract = _gate_contract(["broad-non-e2e"], delivery_policy="require")
    _install_gate_contract(run, contract)
    _write_command_receipt(run.output_dir, "broad-non-e2e", exit_code=1)
    run.state.extras["phase_handoff_waiver"] = {
        "handoff_id": "gate:some-other-gate:1",
        "waiver_text": "accepted elsewhere",
    }

    from pipeline.project.verification_timeline import build_verification_timeline

    timeline = build_verification_timeline(
        run_dir=run.output_dir, extras=run.state.extras, session=run.session,
    )
    assert timeline is not None
    assert timeline.waived == ()
    assert "broad-non-e2e" in timeline.residual_failed
    assert "broad-non-e2e" in timeline.blocking_residual


def test_evidence_summary_approved_with_waived_gate_is_coherent() -> None:
    """final_acceptance APPROVED + a Stage-6 waived required gate reads as a
    coherent terminal state: ``Release: approved`` (not 'delivery blocked'), the
    waiver counted as an ACCEPTED verification finding (not an active risk), and
    no approved/halted contradiction."""
    session = {
        "status": "done",
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "ship_ready": True,
                "findings": [],
            },
        },
        "commit_delivery_verification_waived": [
            {
                "command": "broad-non-e2e",
                "gate_name": "broad-non-e2e",
                "handoff_id": "gate:broad-non-e2e:1",
                "waiver_preview": "accepted: pre-existing failure",
                "status": "failed",
            },
        ],
    }
    summary = _render_evidence_summary(session)

    assert "  Release: approved" in summary
    assert not any("delivery blocked" in line for line in summary)
    # The waiver is an accepted verification finding, not an open risk.
    assert "  Run findings: 1" in summary
    assert "    - verification: 1 resolved" in summary
    assert "  Open risks: none" in summary


def test_approved_with_waived_only_classifier_and_release_line() -> None:
    """The two finalization classifiers give a coherent picture for approved +
    waived-only: no false ``require`` blocker, and the release line stays
    ``approved`` (never approved+halted)."""
    from pipeline.project.finalization import (
        _approved_with_only_verification_warnings,
        _release_outcome_line,
    )
    from pipeline.project.verification_timeline import VerificationTimeline

    session = {
        "status": "done",
        "phases": {
            "final_acceptance": {"verdict": "APPROVED", "ship_ready": True},
        },
    }
    timeline = VerificationTimeline(
        residual_failed=(),
        waived=("broad-non-e2e",),
        waived_details=(("broad-non-e2e", "gate:broad-non-e2e:1"),),
        policy_by_command=(("broad-non-e2e", "require"),),
    )
    # Waived is excluded from blocking_residual → not a release blocker.
    assert timeline.blocking_residual == ()
    # No warn/suggest residual, so the warning framing stays off (waiver is
    # accepted, not "shipping allowed by policy").
    assert _approved_with_only_verification_warnings(session, timeline) is False
    # Release line is coherent: approved, not 'delivery blocked'.
    assert _release_outcome_line(session, session["phases"]) == "  Release: approved"


def test_done_block_verification_gates_omitted_without_evidence(
    tmp_path, capsys, monkeypatch,
) -> None:
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    # No contract, no receipts, no trail, no gate-events -> omitted entirely.

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "Verification gates" not in out


def test_done_block_verification_gates_omitted_without_output_dir(
    tmp_path, capsys, monkeypatch,
) -> None:
    """With no run output dir there is nothing to read — the block is omitted
    even if the run state would otherwise carry evidence."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    _gate_events(run, [
        {"hook": "after_phase", "phase": "implement", "command": "lint",
         "gate_set": "core", "decision": "executed_pass"},
    ])
    run.output_dir = None

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "Verification gates" not in out


def test_done_block_verification_gates_coexists_with_evidence_warnings(
    tmp_path, capsys, monkeypatch,
) -> None:
    """The gate block must not suppress the existing evidence summary —
    readiness-style missing/failed run findings still render alongside it."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    # A rejected final acceptance with verification gaps -> existing run-finding
    # warning ('verification: N active') in the evidence summary.
    run.session["phases"] = {
        "final_acceptance": {
            "verdict": "REJECTED",
            "ship_ready": False,
            "verification_gaps": [{"risk": "missing receipt"}],
        },
    }
    contract = _gate_contract(["lint"])
    _install_gate_contract(run, contract)
    _gate_events(run, [
        {"hook": "after_phase", "phase": "implement", "command": "lint",
         "gate_set": "core", "decision": "executed_pass"},
    ])
    _write_command_receipt(run.output_dir, "lint")

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    # Existing evidence/readiness warnings still render.
    assert "Evidence" in out
    assert "verification: 1 active" in out
    # New gate block renders too.
    assert "Verification gates:" in out
    assert "after_phase(implement): 1 ran/pass" in out


def _git_init(repo: Path) -> None:
    import subprocess

    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@orcho.invalid"],
        ["git", "config", "user.name", "T"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(argv, cwd=repo, check=True)
    (repo / "README.md").write_text("# x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _write_stale_receipt(run_dir: Path, command: str) -> None:
    """A valid exit-0 receipt recorded against a different HEAD → stale once the
    subject checkout is a real git repo at a different commit."""
    rdir = run_dir / COMMAND_RECEIPTS_DIRNAME
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{command}.json").write_text(
        json.dumps({
            "kind": "verification_command",
            "command": command,
            "env": "core-local",
            "exit_code": 0,
            "assertions": [],
            "detail": "",
            "git": {
                "checkout_head": "0" * 40,
                "baseline_head": None,
                "changed_files_fingerprint": None,
            },
            "schema_version": 3,
            "subject": {"status": "available", "identity": {
                "version": 1, "object_format": "sha1", "tree_oid": "0" * 40,
                "observed_head_oid": "0" * 40, "baseline_oid": None,
            }},
            "dependencies": [],
        }),
        encoding="utf-8",
    )


@pytest.mark.git_worktree
def test_done_block_rejected_acceptance_residual_failed_searched_fix(
    tmp_path, capsys, monkeypatch,
) -> None:
    """Integration: a rejected final acceptance whose required gates are
    missing/stale/failed renders, through ``finalize_with_terminal_output``, the
    run-level ``residual`` line (now carrying ``failed=``) plus the actionable
    ``searched run dirs`` / ``fix`` lines — visually distinct from the phase
    headers and the Evidence summary that still render alongside it."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    # A real git checkout so a head-mismatched receipt classifies stale.
    _git_init(run.project_path)
    run.session["phases"] = {
        "final_acceptance": {
            "verdict": "REJECTED",
            "ship_ready": False,
            "verification_gaps": [{"risk": "missing receipt"}],
        },
    }
    contract = _gate_contract(["lint", "unit", "smoke"])
    _install_gate_contract(run, contract)
    # lint failed (exit 1), smoke stale (head mismatch), unit missing (no receipt).
    _write_command_receipt(run.output_dir, "lint", exit_code=1)
    _write_stale_receipt(run.output_dir, "smoke")

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    # The block and its run-level residual (failed alongside missing/stale).
    assert "Verification gates:" in out
    assert "residual: missing=unit stale=smoke failed=lint" in out
    # Actionable diagnostics share their source with the readiness block.
    assert f"searched run dirs: {run.output_dir}" in out
    assert "fix: orcho verify" in out
    # Still coexists with the existing Evidence summary and its run findings.
    assert "Evidence" in out
    assert "verification: 1 active" in out


# ── T5: policy-aware DONE verification block (ADR 0097) ─────────────────────


def test_finalize_approved_with_only_warning_gates_reads_as_approved(
    tmp_path, capsys, monkeypatch,
) -> None:
    """APPROVED release whose only open gate is warn-policy renders as
    'approved + verification warning (shipping allowed by policy)' — green
    headline, no DELIVERY BLOCKED, the warning surfaced but not a blocker."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    run.session["phases"] = {
        "final_acceptance": {"verdict": "APPROVED", "ship_ready": True},
    }
    # Default (no delivery_policy) → warn boundary. No receipt for lint → a
    # missing warn-policy gap (shipping allowed by policy).
    contract = _gate_contract(["lint"])
    _install_gate_contract(run, contract)

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "Pipeline complete" in out          # approved stays green
    assert "DELIVERY BLOCKED" not in out
    assert "approved + verification warning (shipping allowed by policy)" in out
    # The gap is classified a warning (warn policy), never a blocking residual.
    assert "warning (warn/suggest): lint (warn)" in out
    assert "blocking (require):" not in out


def test_finalize_approved_with_require_gap_stays_blocking_not_warning(
    tmp_path, capsys, monkeypatch,
) -> None:
    """An APPROVED release with a require-policy residual is NOT softened into a
    verification warning — the gap is shown as blocking (never-falsely-green)."""
    _wire_finalize_stubs(monkeypatch)
    run = _make_run(tmp_path)
    run.session["phases"] = {
        "final_acceptance": {"verdict": "APPROVED", "ship_ready": True},
    }
    contract = _gate_contract(["lint"], delivery_policy="require")
    _install_gate_contract(run, contract)

    finalize_with_terminal_output(FinalizationContext(run=run))

    out = strip_ansi(capsys.readouterr().out)
    assert "blocking (require): lint" in out
    assert "approved + verification warning" not in out
    assert "shipping allowed by policy" not in out


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
