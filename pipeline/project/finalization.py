"""Silent + terminal-wrapper finalization for the single-project pipeline.

Moved out of :class:`pipeline.project.run._PipelineRun.finalize` per
ADR 0042 Phase G. The split is **silent service** vs **terminal
wrapper**:

* :func:`finalize_project_run` is the silent structured service. It
  writes ``session.json`` / ``metrics.json`` / ``diff.patch`` /
  ``evidence.json``, mutates ``session``, emits ``run.end``, closes
  the checkpoint, mirrors artifacts, tears down the worktree — but
  produces **no terminal output**. UI clients (future) consume this
  directly through :class:`FinalizationResult`.

* :func:`finalize_with_terminal_output` is the CLI-equivalent path:
  it calls the silent service and prints the legacy DONE banner +
  success chips + Session/Usage/Progress lines + mirror notice +
  worktree-teardown line. All terminal text is derived from the
  silent service's ``FinalizationResult`` so the two paths cannot
  drift on semantics.

"Silent" means no stdout / stderr / ``banner()`` / ``success()`` /
``warn()`` / ``print()``. The service DOES still write files, emit
events, mutate session, set checkpoint status, mirror artifacts, and
tear down the worktree — those are not "noise" by the same standard
(no progress text crosses the terminal boundary). See ADR 0042 r5 P1
for the framing.

**Ordering invariants (load-bearing):**

1. ``capture_run_diff_with_apply_check`` MUST run BEFORE
   ``_run_commit_delivery``. The captured ``diff.patch`` is the
   recovery source even when ``approve``/``apply`` succeeds and mutates
   the project checkout.
2. ``run.end`` event payload and checkpoint final status MUST read
   the **post-delivery** ``session["status"]``. Delivery can flip
   ``done`` → ``halted`` (commit_decision_halt / target_dirty /
   commit_failed / apply_failed); downstream consumers that key off
   ``meta.halt_reason`` must see the actual reason.

The :class:`FinalizationResult` field list is derived from
inspection of the legacy ``finalize()`` body — no fabricated paths.
Every artifact field carries the actual return value of the underlying
writer (``capture_run_diff_with_apply_check`` may return ``None`` when
there is no diff; ``mirror_to_projects`` returns ``list[Path]``,
possibly empty).
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.infra import config
from core.io.ansi import C, paint
from core.observability import (
    logging as _logging,
)
from core.observability.accounting_display import (
    ACCOUNTING_REFERENCE_NOTE,
    format_cost_reference_key_value,
)
from core.observability.logging import log_phase
from pipeline.checkpoint import PipelineStatus
from pipeline.engine import save_session
from pipeline.project.correction_route_display import (
    format_correction_route_summary,
)
from pipeline.project.handoff_advice_evidence import collect_handoff_advice
from pipeline.project.terminal_delivery import (
    TerminalDeliveryDisposition,
    TerminalDeliveryOutcome,
    project_terminal_delivery,
    render_delivery_destination_lines,
)
from pipeline.run_state.release_verdict import (
    is_approved,
    is_rejected,
    normalize_verdict,
)
from pipeline.run_state.terminal_outcome import (
    apply_no_diff_terminal,
    normalize_engine_reason,
    resolve_rejected_release_terminal,
    resolve_terminal_outcome,
    supersede_parent_meta,
    supersede_same_run_residue,
)

# ── helpers (Phase F moved them via ``pipeline.project.run``; Phase G
# lifts them here so finalize_project_run is self-contained). ────────────


_RESUME_COMPLETED_SKIP_REASONS: frozenset[str] = frozenset({
    "completed earlier in this run (resumed)",
    # Legacy spelling kept so older resumed artifacts do not render as
    # "skip" in DONE after the operator has already seen the phase run.
    "completed in prior run (resumed)",
})


def _profile_phase_names_in_order(profile: Any | None) -> list[str]:
    """Flatten profile phases in author order for the DONE summary.

    The session shape is not authoritative here: loop phases may be adapted
    into aggregate entries such as ``rounds``. The profile recipe owns the
    phase vocabulary; ``state.phase_log`` owns the outcome.
    """
    if profile is None:
        return []

    from pipeline.runtime import LoopStep, PhaseStep

    names: list[str] = []
    seen: set[str] = set()

    def append(name: str) -> None:
        if name not in seen:
            names.append(name)
            seen.add(name)

    def walk(entry: Any) -> None:
        if isinstance(entry, PhaseStep):
            append(entry.phase)
        elif isinstance(entry, LoopStep):
            for inner in entry.steps:
                walk(inner)

    for entry in getattr(profile, "steps", ()) or ():
        walk(entry)
    return names


def _done_phase_outcome(
    record: Any, *, phase: str = "", halted: bool = False,
) -> str:
    from collections.abc import Mapping

    # A halted phase is reported as ``halt`` regardless of the partial
    # record it left behind — it is not a completed checkpoint, so it must
    # never read as ``ok`` in the DONE summary.
    if halted:
        return "halt"
    if record is None:
        return "skip"
    if not isinstance(record, Mapping):
        return "ok"
    skipped_reason = record.get("skipped")
    if isinstance(skipped_reason, str) and (
        skipped_reason.strip() in _RESUME_COMPLETED_SKIP_REASONS
    ):
        return "ok"
    if skipped_reason:
        return "skip"
    if any(record.get(key) for key in ("failed", "error", "exception", "parse_error")):
        return "fail"
    if phase == "validate_plan":
        # The latest validate_plan attempt was REJECTED yet the run advanced
        # (it was neither halted nor failed above): the critique was forwarded
        # into implement as advisory and the replan loop was bypassed. Render
        # ``advisory`` instead of ``ok`` so the chip stays honest — the plan
        # was not approved, it was carried forward. An APPROVED latest attempt
        # (feature after replan) still falls through to ``ok`` below.
        verdict = record.get("verdict")
        rejected = is_rejected(verdict)
        if record.get("approved") is False or rejected:
            return "advisory"
        return "ok"
    if phase in {"final_acceptance", "cross_final_acceptance"}:
        # ``reject`` here means a genuine release/plan VERDICT rejection — the
        # gate ran and judged the diff unshippable. A recoverable
        # provider/runtime failure (ADR 0118, ``failure_kind='provider_runtime'``
        # in ``session['failure']``) is categorically different: the agent call
        # never produced a verdict, the run is hard-``failed`` and is rendered by
        # the red FAILED banner in ``run.py``, not by this DONE summary. Such a
        # run never reaches here, and even a stale provider-error record carries
        # no ``verdict`` / ``ship_ready=False`` / ``approved=False``, so it falls
        # through to the generic ``fail``/``ok`` paths above — it is NEVER
        # conflated with a review/diff rejection.
        verdict = record.get("verdict")
        if is_rejected(verdict):
            return "reject"
        if is_approved(verdict):
            return "ok"
        if record.get("ship_ready") is False or record.get("approved") is False:
            return "reject"
    return "ok"


def _render_done_summary(
    profile: Any | None, phase_log: Any, *, halted_phase: str | None = None,
) -> str:
    """Render the profile-driven DONE line as ``phase_id=outcome`` chips.

    ``halted_phase`` (when set) is the single phase whose ``state.halt``
    stopped the run; its chip renders ``halt`` instead of ``ok`` so a
    halted IMPLEMENT never reads as completed. Delivery/no-diff halts pass
    ``None`` here and leave every chip on its genuine per-phase outcome.
    """
    from collections.abc import Mapping

    names = _profile_phase_names_in_order(profile)
    if not names:
        names = list(phase_log.keys()) if isinstance(phase_log, Mapping) else []
    return " | ".join(
        f"{name}="
        f"{_done_phase_outcome(
            phase_log.get(name), phase=name, halted=name == halted_phase,
        )}"
        for name in names
    )


def _render_task_summary(task: Any, *, width: int = 160) -> str | None:
    """Return the first human-readable task line for terminal run identity."""
    if not isinstance(task, str):
        return None
    for raw_line in task.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        if not line:
            continue
        return textwrap.shorten(line, width=width, placeholder="...")
    return None


_FINDING_PHASES: tuple[str, ...] = (
    "validate_plan",
    "review_changes",
    "final_acceptance",
    "compliance_check",
    "cross_final_acceptance",
)

_RUN_FINDING_ORDER: tuple[str, ...] = (
    "environment",
    "attestation",
    "handoff",
    "verification",
    "delivery",
)


@dataclass(frozen=True, slots=True)
class _ReviewFindingSummary:
    total: int
    active: int
    by_severity: dict[str, int]
    active_by_severity: dict[str, int]
    by_phase: dict[str, int]
    # Findings from a bypassed validate_plan whose critique was forwarded into a
    # successful whole-plan implement. They are counted in ``total`` (visible,
    # not resolved) but kept OUT of ``active`` so they never read as blocking
    # release risks in Open risks.
    advisory: int = 0
    advisory_by_severity: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _RunFindingSummary:
    total: int
    active: int
    by_kind: dict[str, int]
    active_by_kind: dict[str, int]

    @property
    def resolved(self) -> int:
        return max(self.total - self.active, 0)


def _phase_attempts(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [value]
    return []


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _latest_mapping(value: Any) -> Mapping[str, Any]:
    attempts = _phase_attempts(value)
    return attempts[-1] if attempts else {}


def _attempt_approved(phase: str, attempt: Mapping[str, Any]) -> bool:
    if is_approved(attempt.get("verdict")):
        return True
    approved = attempt.get("approved")
    if isinstance(approved, bool):
        return approved
    if phase == "review_changes" and attempt.get("clean") is True:
        return True
    if phase in {"final_acceptance", "cross_final_acceptance"}:
        return attempt.get("ship_ready") is True
    return False


def _finding_severity(finding: Mapping[str, Any]) -> str:
    raw = finding.get("severity")
    value = str(raw or "P3").upper()
    return value if value in {"P0", "P1", "P2", "P3"} else "P3"


def _format_count_map(counts: Mapping[str, int], order: tuple[str, ...]) -> str:
    parts = [f"{key}={counts[key]}" for key in order if counts.get(key, 0)]
    return ", ".join(parts) if parts else "none"


def _subtask_state_records(metrics: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """Return the ``metrics["subtasks"]["implement"]`` per-subtask records.

    This is the cross-segment deduped slice: ``record_subtask_usage`` merges by
    ``subtask_id`` across every resume segment, so each subtask appears once with
    its accumulated usage and its final ``state``. Returns ``[]`` for whole-plan /
    non-subtask runs (no ``subtasks`` key) so the caller falls back to meta.
    """
    if not isinstance(metrics, Mapping):
        return []
    subtasks = metrics.get("subtasks")
    if not isinstance(subtasks, Mapping):
        return []
    rows = subtasks.get("implement")
    if not isinstance(rows, list):
        return []
    return [rec for rec in rows if isinstance(rec, Mapping)]


def _task_counts(
    phases: Mapping[str, Any],
    metrics: Mapping[str, Any] | None = None,
) -> tuple[int, int, int, int, int]:
    implement = phases.get("implement")
    impl = implement if isinstance(implement, Mapping) else {}
    meta = impl.get("meta")
    if not isinstance(meta, Mapping):
        meta = {}

    # planned: the plan total is authoritative and survives resume segmentation
    # (``_latest_mapping`` normalizes a list-of-attempts plan to its last
    # attempt). ``meta.subtask_count`` — the last implement wave only — is just
    # the fallback for whole-plan / non-subtask runs that carry no plan total.
    plan = _latest_mapping(phases.get("plan"))
    planned = _as_int(plan.get("total_atomic_tasks"))
    if planned == 0:
        planned = _as_int(meta.get("subtask_count"))

    # completed/failed/skipped: the cross-segment deduped subtask slice is the
    # SINGLE authoritative source. ``metrics["subtasks"]["implement"]`` carries
    # one final record per subtask across ALL resume segments (merged by
    # subtask_id), so counting it by final ``state`` reports honest N/M
    # regardless of how many resume waves ran — not just the last wave's meta
    # counters. Skipped subtasks (no agent invocation, no usage) are folded into
    # this slice upstream as state-only records (see subtask_dag
    # ``_append_skipped_subtask_records``), so completed/failed/skipped ALL come
    # from the slice — the raw ``implementation_receipts`` are never consulted as
    # a second source here (that could show skipped beyond the deduped slice and
    # push the bucket sum past ``planned``). Records without a terminal state
    # (e.g. an incomplete subtask that never produced a done/failed receipt) are
    # folded into ``incomplete``.
    records = _subtask_state_records(metrics)
    if records:
        # A non-empty subtask slice is authoritative: presence of the list — not
        # of a terminal state — selects this path. Records without a terminal
        # done/failed/skipped state are honest incomplete work, not a reason to
        # drop back to meta.
        state_by_id: dict[Any, str] = {}
        for rec in records:
            # Defensive re-dedup: the slice is merged-by-id upstream, but if a
            # raw retry pair (failed then done for one subtask_id) ever reaches
            # here, last record wins so a retried subtask is counted once by
            # final state.
            state_by_id[rec.get("subtask_id")] = str(rec.get("state") or "")
        states = list(state_by_id.values())
        completed = sum(1 for state in states if state == "done")
        failed = sum(1 for state in states if state == "failed")
        skipped = sum(1 for state in states if state == "skipped")
        # incomplete = plan tasks without a terminal done/failed/skipped state,
        # clamped ≥0 so the four buckets never exceed the accounted work.
        accounted = max(planned, len(states))
        incomplete = max(accounted - completed - failed - skipped, 0)
    else:
        # No subtask slice (whole-plan / non-subtask run): fall back to the
        # last-wave meta counters and the implement receipts.
        completed = _as_int(meta.get("completed_count"))
        failed = _as_int(meta.get("failed_count"))
        skipped = _as_int(meta.get("skipped_count"))
        receipts = impl.get("implementation_receipts")
        incomplete = (
            sum(
                1 for item in receipts
                if isinstance(item, Mapping) and item.get("state") != "done"
            )
            if isinstance(receipts, list)
            else 0
        )

        # A direct (whole-plan) implement carries no subtask-DAG meta, so its
        # completed/failed counters never populate. When such an implement
        # succeeded (output present, no guardrail_blocked/failed/error), the
        # planned task is delivered: count it as completed so Tasks/ROI read
        # 1/1, not 0/1. A stopped/failed whole-plan implement keeps the zeroed
        # counters. This collapse is scoped to the no-slice branch: an
        # authoritative subtask slice must never be overwritten by planned/0/0/0.
        if planned and _implement_whole_plan_delivered(phases):
            completed, failed, skipped, incomplete = planned, 0, 0, 0

    return planned, completed, failed, skipped, incomplete


def _delivery_closure(phases: Mapping[str, Any]) -> tuple[str, int, int]:
    implement = phases.get("implement")
    impl = implement if isinstance(implement, Mapping) else {}
    status = str(impl.get("delivery_status") or "")
    receipts = impl.get("implementation_receipts")
    repaired = (
        sum(
            1 for item in receipts
            if isinstance(item, Mapping)
            and item.get("attestation_repaired") is True
        )
        if isinstance(receipts, list)
        else 0
    )
    waived = 1 if impl.get("delivery_waived") is True else 0
    return status, repaired, waived


def _release_record(phases: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    for phase in ("final_acceptance", "cross_final_acceptance"):
        latest = _latest_mapping(phases.get(phase))
        if latest:
            return phase, latest
    return "", {}


def _release_outcome_token(phases: Mapping[str, Any]) -> str:
    _phase, record = _release_record(phases)
    if not record:
        return "none"

    verdict = record.get("verdict")
    ship_ready = record.get("ship_ready")
    if is_approved(verdict) or ship_ready is True:
        return "approved"
    if is_rejected(verdict) or ship_ready is False or record.get("approved") is False:
        return "rejected"
    return "pending"


def _approved_with_only_verification_warnings(
    session: Mapping[str, Any], timeline: Any,
) -> bool:
    """True when final acceptance APPROVED and the only open gates are warnings.

    A focused classifier for the DONE verification block (T5, ADR 0097): when the
    release was approved and every residual verification gate is ``warn`` /
    ``suggest`` (shipping allowed by policy) with NO ``require`` blocker, the
    block is framed as ``approved + verification warning`` rather than reading as
    a release block or a contradiction. An APPROVED release with a ``require``
    residual stays blocking (this returns False), so the never-falsely-green
    invariant holds.
    """
    if timeline is None:
        return False
    phases = session.get("phases")
    if not isinstance(phases, Mapping):
        return False
    approved = any(
        _attempt_approved(phase, latest)
        for phase in ("final_acceptance", "cross_final_acceptance")
        if (latest := _latest_mapping(phases.get(phase)))
    )
    if not approved:
        return False
    return bool(timeline.warning_residual) and not timeline.blocking_residual


def _release_outcome_line(
    session: Mapping[str, Any],
    phases: Mapping[str, Any],
    *,
    terminal_delivery: TerminalDeliveryOutcome | None = None,
) -> str | None:
    outcome = _release_outcome_token(phases)
    if outcome == "none":
        return None

    if outcome == "approved":
        return "  Release: approved"

    if outcome == "rejected":
        action = ""
        halt_reason = str(session.get("halt_reason") or "")
        if halt_reason == "commit_decision_fix":
            action = " -> correction requested"
        elif halt_reason == "commit_decision_halt":
            action = " -> halted by operator"
        elif session.get("status") == "halted":
            action = " -> halted"
        elif (
            (terminal_delivery or project_terminal_delivery(session)).disposition
            is TerminalDeliveryDisposition.NOT_DELIVERED
        ):
            # Delivery wording is driven solely by the durable delivery
            # disposition.  An override remains a rejected release, but must
            # never be described as blocked delivery.
            action = " -> delivery blocked"
        return f"  Release: rejected{action}"

    return None


def _release_blocker_lines(phases: Mapping[str, Any]) -> tuple[str, ...]:
    """Render the latest authoritative release blockers for terminal evidence."""
    _phase, record = _release_record(phases)
    blockers = record.get("release_blockers")
    if not isinstance(blockers, list) or not blockers:
        return ()

    lines = [f"  Release blockers: {len(blockers)}"]
    for blocker in blockers:
        if not isinstance(blocker, Mapping):
            continue
        identifier = str(blocker.get("id") or "?")
        title = str(blocker.get("title") or blocker.get("detail") or "untitled")
        why = str(blocker.get("why_blocks_release") or "").strip()
        detail = f" — {why}" if why else ""
        lines.append(f"    - {identifier}: {title}{detail}")
    return tuple(lines)


def _correction_kind(phases: Mapping[str, Any]) -> str:
    triage = phases.get("correction_triage")
    if not isinstance(triage, Mapping):
        return ""

    route = triage.get("route")
    if isinstance(route, Mapping):
        kind = str(route.get("kind") or "").strip()
        if kind:
            return kind

    return str(triage.get("kind") or "").strip()


def _correction_outcome_line(phases: Mapping[str, Any]) -> str | None:
    kind = _correction_kind(phases)
    if not kind:
        return None
    if kind == "code_fix":
        return "  Correction: code_fix -> full correction path"
    return f"  Correction: {kind}"


def _increment_count(
    counts: dict[str, int], key: str, amount: int = 1,
) -> None:
    if amount <= 0:
        return
    counts[key] = counts.get(key, 0) + amount


def _implement_whole_plan_delivered(phases: Mapping[str, Any]) -> bool:
    """True when implement ran a successful whole-plan (no subtask DAG).

    Universal (profile-name agnostic): the implement record carries ``output``,
    no ``guardrail_blocked``/``failed``/``error``, and no subtask DAG (its
    ``meta`` has no positive ``subtask_count``). This marks the small_task-style
    path where a bypassed validate_plan's critique was forwarded into a
    successful whole-plan implement, so those plan findings are advisory rather
    than active release blockers.
    """
    implement = phases.get("implement")
    if not isinstance(implement, Mapping):
        return False
    if not implement.get("output"):
        return False
    if any(
        implement.get(key) for key in ("guardrail_blocked", "failed", "error")
    ):
        return False
    meta = implement.get("meta")
    return not (isinstance(meta, Mapping) and _as_int(meta.get("subtask_count")) > 0)


def _review_finding_summary(phases: Mapping[str, Any]) -> _ReviewFindingSummary:
    total = 0
    active = 0
    advisory = 0
    by_severity: dict[str, int] = {}
    active_by_severity: dict[str, int] = {}
    advisory_by_severity: dict[str, int] = {}
    by_phase: dict[str, int] = {}

    whole_plan_delivered = _implement_whole_plan_delivered(phases)

    for phase in _FINDING_PHASES:
        attempts = _phase_attempts(phases.get(phase))
        if not attempts:
            continue
        for attempt in attempts:
            findings = attempt.get("findings")
            if not isinstance(findings, list):
                continue
            for finding in findings:
                if not isinstance(finding, Mapping):
                    continue
                total += 1
                _increment_count(by_phase, phase)
                severity = _finding_severity(finding)
                _increment_count(by_severity, severity)

        latest = attempts[-1]
        if _attempt_approved(phase, latest):
            continue
        # A bypassed validate_plan whose critique was forwarded into a
        # successful whole-plan implement: its latest findings are advisory
        # (visible, not resolved, not blocking), never active release risks.
        is_advisory = phase == "validate_plan" and whole_plan_delivered
        latest_findings = latest.get("findings")
        if isinstance(latest_findings, list):
            for item in latest_findings:
                if not isinstance(item, Mapping):
                    continue
                severity = _finding_severity(item)
                if is_advisory:
                    advisory += 1
                    _increment_count(advisory_by_severity, severity)
                else:
                    active += 1
                    _increment_count(active_by_severity, severity)

    return _ReviewFindingSummary(
        total=total,
        active=active,
        by_severity=by_severity,
        active_by_severity=active_by_severity,
        by_phase=by_phase,
        advisory=advisory,
        advisory_by_severity=advisory_by_severity,
    )


def _text_suggests_environment_issue(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "environment",
            "interpreter",
            "pythonpath",
            "pipeline.__file__",
            "stable install",
            "venv",
            "virtualenv",
            "workspace",
        )
    )


def _attestation_kind(item: Mapping[str, Any]) -> str:
    text = " ".join(
        str(item.get(key) or "")
        for key in (
            "attestation_error",
            "error",
            "reason",
            "summary",
            "attestation_summary",
        )
    )
    return "environment" if _text_suggests_environment_issue(text) else "attestation"


def _add_run_finding(
    *,
    by_kind: dict[str, int],
    active_by_kind: dict[str, int],
    kind: str,
    active: bool = False,
    amount: int = 1,
) -> None:
    _increment_count(by_kind, kind, amount)
    if active:
        _increment_count(active_by_kind, kind, amount)


def _run_finding_summary(
    session: Mapping[str, Any], phases: Mapping[str, Any],
) -> _RunFindingSummary:
    """Summarize process/run-level findings distinct from review findings."""
    by_kind: dict[str, int] = {}
    active_by_kind: dict[str, int] = {}

    for phase in _FINDING_PHASES:
        attempts = _phase_attempts(phases.get(phase))
        if len(attempts) > 1:
            _add_run_finding(
                by_kind=by_kind,
                active_by_kind=active_by_kind,
                kind="handoff",
                amount=len(attempts) - 1,
            )

    if isinstance(session.get("phase_handoff"), Mapping):
        _add_run_finding(
            by_kind=by_kind,
            active_by_kind=active_by_kind,
            kind="handoff",
            active=True,
        )

    if isinstance(session.get("phase_handoff_waiver"), Mapping):
        _add_run_finding(
            by_kind=by_kind,
            active_by_kind=active_by_kind,
            kind="handoff",
        )

    implement = phases.get("implement")
    impl = implement if isinstance(implement, Mapping) else {}
    receipts = impl.get("implementation_receipts")
    repaired_receipts = 0
    incomplete_receipts = 0
    if isinstance(receipts, list):
        for item in receipts:
            if not isinstance(item, Mapping):
                continue
            if item.get("attestation_repaired") is True:
                repaired_receipts += 1
                _add_run_finding(
                    by_kind=by_kind,
                    active_by_kind=active_by_kind,
                    kind=_attestation_kind(item),
                )
            if item.get("state") != "done":
                incomplete_receipts += 1
                _add_run_finding(
                    by_kind=by_kind,
                    active_by_kind=active_by_kind,
                    kind=_attestation_kind(item),
                    active=True,
                )

    attestation_incomplete = impl.get("attestation_incomplete")
    if isinstance(attestation_incomplete, Mapping):
        amount = len(attestation_incomplete)
        if amount > incomplete_receipts:
            _add_run_finding(
                by_kind=by_kind,
                active_by_kind=active_by_kind,
                kind="attestation",
                active=True,
                amount=amount - incomplete_receipts,
            )

    delivery_status = str(impl.get("delivery_status") or "").lower()
    if delivery_status == "repaired" and repaired_receipts == 0:
        _add_run_finding(
            by_kind=by_kind,
            active_by_kind=active_by_kind,
            kind="attestation",
        )
    elif delivery_status == "incomplete" and incomplete_receipts == 0:
        _add_run_finding(
            by_kind=by_kind,
            active_by_kind=active_by_kind,
            kind="attestation",
            active=True,
        )
    elif delivery_status == "waived":
        _add_run_finding(
            by_kind=by_kind,
            active_by_kind=active_by_kind,
            kind="handoff",
        )

    for phase in ("final_acceptance", "cross_final_acceptance"):
        latest = _latest_mapping(phases.get(phase))
        if not latest or _attempt_approved(phase, latest):
            continue
        gaps = latest.get("verification_gaps")
        if isinstance(gaps, list) and gaps:
            _add_run_finding(
                by_kind=by_kind,
                active_by_kind=active_by_kind,
                kind="verification",
                active=True,
                amount=sum(1 for item in gaps if isinstance(item, Mapping)),
            )
        blockers = latest.get("release_blockers")
        if isinstance(blockers, list) and blockers:
            _add_run_finding(
                by_kind=by_kind,
                active_by_kind=active_by_kind,
                kind="delivery",
                active=True,
                amount=sum(1 for item in blockers if isinstance(item, Mapping)),
            )

    # Stage 6 delivery-gate waivers (run.py persists ``commit_delivery_
    # verification_waived``): a required gate whose receipt was excused by an
    # exact durable operator waiver. Surfaced as an ACCEPTED verification finding
    # (counts toward the total) but NOT active — it is not an open release risk,
    # mirroring how blocking_residual excludes it. Absent key → no contribution
    # (byte-identical otherwise).
    waived_gates = session.get("commit_delivery_verification_waived")
    if isinstance(waived_gates, list):
        amount = sum(1 for item in waived_gates if isinstance(item, Mapping))
        if amount:
            _add_run_finding(
                by_kind=by_kind,
                active_by_kind=active_by_kind,
                kind="verification",
                amount=amount,
            )

    total = sum(by_kind.values())
    active = sum(active_by_kind.values())
    return _RunFindingSummary(
        total=total,
        active=active,
        by_kind=by_kind,
        active_by_kind=active_by_kind,
    )


def _format_run_kind_state(total: int, active: int) -> str:
    resolved = max(total - active, 0)
    if active and resolved:
        return f"{resolved} resolved, {active} active"
    if active:
        return f"{active} active"
    return f"{resolved} resolved"


def _non_convergence_lines(session: Mapping[str, Any]) -> tuple[str, ...]:
    """Evidence lines for a fixed-point (non-converging) correction (ADR 0098).

    A run re-marked ``correction_not_converging`` carries the durable
    ``correction_fixed_point`` block. Surface it so the outcome reads as a
    deliberate halted non-convergence, never as a green DONE: the repeated
    blocker identities plus the parent/child run ids the operator needs to act.
    Empty for every other session (the common case).
    """
    block = session.get("correction_fixed_point")
    if not isinstance(block, Mapping):
        return ()
    lines = ["  Correction: not converging"]
    repeated = block.get("repeated")
    if isinstance(repeated, (list, tuple)) and repeated:
        rendered = ", ".join(str(item) for item in repeated)
        lines.append(f"    repeated blockers: {rendered}")
    parent_run_id = str(block.get("parent_run_id") or "")
    child_run_id = str(block.get("child_run_id") or "")
    if parent_run_id or child_run_id:
        lines.append(
            f"    parent run: {parent_run_id} | child run: {child_run_id}"
        )
    return tuple(lines)


def _auto_detect_summary_line(auto_detect: Any) -> str | None:
    """Compact DONE-summary line for an auto-detect run, or ``None``.

    Rendered only when ``meta.auto_detect`` exists (i.e. the run started via the
    ``auto-detect`` selector). The wording reflects ``detection_state``
    (accepted / auto-selected / operator override / low-confidence fallback /
    detector-error fallback) and only shows ``confidence`` when it is present —
    a detector-error fallback carries no confidence and must not invent one.
    """
    if not isinstance(auto_detect, Mapping):
        return None
    state = auto_detect.get("detection_state")
    profile = auto_detect.get("actual_profile")
    mode = auto_detect.get("actual_mode")
    if not state or not profile or not mode:
        return None
    if state == "recommended":
        confirmation = auto_detect.get("confirmation_state")
        label = {
            "accepted": "accepted",
            "override": "operator override",
            "auto": "auto-selected",
        }.get(confirmation, "recommended")
    elif state == "low_confidence_fallback":
        label = "low-confidence fallback"
    elif state == "detector_error_fallback":
        label = "detector-error fallback"
    elif state == "failed":
        label = "failed"
    else:
        label = str(state)
    line = f"  Auto-detect: {profile} {mode} {label}"
    confidence = auto_detect.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        line += f" (confidence {float(confidence):.2f})"
    return line


def _scope_expansion_summary_lines(phases: Mapping[str, Any]) -> tuple[str, ...]:
    """Compact, always-visible scope-expansion lines for the Evidence block (F2).

    Reads ONLY the single canonical session path
    ``session['phases']['final_acceptance']['scope_expansion']`` (the projection
    of the durable ``phase_log`` dict T2 wrote) and renders it through the pure
    ``scope_expansion.render_scope_expansion_lines``. Returns ``()`` when the
    evidence is absent so an ordinary in-scope run stays byte-identical;
    finalization only reads the durable dict and delegates the rendering.
    """
    final_acceptance = phases.get("final_acceptance")
    if not isinstance(final_acceptance, Mapping):
        return ()
    evidence = final_acceptance.get("scope_expansion")
    if not isinstance(evidence, Mapping) or not evidence.get("items"):
        return ()
    from pipeline.engine.scope_expansion import render_scope_expansion_lines

    # Indent each rendered line two spaces so the header/items nest under the
    # ``Evidence`` block alongside the other ``  <label>`` sub-lines.
    return tuple(f"  {line}" for line in render_scope_expansion_lines(evidence))


def _render_evidence_summary(
    session: Mapping[str, Any],
    metrics: Mapping[str, Any] | None = None,
    *,
    terminal_delivery: TerminalDeliveryOutcome | None = None,
) -> tuple[str, ...]:
    """Return compact end-of-run evidence summary lines.

    This is deliberately a light terminal/reporting projection, not a new gate.
    ``resolved`` means the finding is not part of the latest non-approved
    attempt for its phase. In normal repair loops that corresponds to fixed;
    in operator-accepted paths it may mean waived/accepted, so the label stays
    slightly broader than "fixed".

    ``metrics`` is the run metrics dict; its cross-segment
    ``subtasks.implement`` slice drives the honest per-subtask task counts. It
    is optional (defaults to no slice) so session-only callers — which carry no
    subtask-DAG breakdown — keep reading the meta fallback path unchanged.
    """
    auto_detect_line = _auto_detect_summary_line(session.get("auto_detect"))
    phases = session.get("phases")
    if not isinstance(phases, Mapping):
        return (auto_detect_line,) if auto_detect_line else ()

    planned, completed, failed, skipped, incomplete = _task_counts(phases, metrics)
    correction_kind = _correction_kind(phases)
    if correction_kind and planned == completed == failed == skipped == incomplete == 0:
        task_line = "  Tasks: correction follow-up (no subtask plan)"
    else:
        task_parts = [
            f"{planned} planned",
            f"{completed} completed",
            f"{failed} failed",
        ]
        if skipped:
            task_parts.append(f"{skipped} skipped")
        task_parts.append(f"{incomplete} incomplete")
        task_line = f"  Tasks: {' · '.join(task_parts)}"

    review = _review_finding_summary(phases)
    run_findings = _run_finding_summary(session, phases)

    lines = [
        "Evidence",
        task_line,
    ]
    if auto_detect_line:
        lines.append(auto_detect_line)
    correction_line = _correction_outcome_line(phases)
    if correction_line:
        lines.append(correction_line)
    release_line = _release_outcome_line(
        session,
        phases,
        terminal_delivery=terminal_delivery,
    )
    if release_line:
        lines.append(release_line)
    lines.extend(_release_blocker_lines(phases))
    lines.extend(_non_convergence_lines(session))

    if review.total:
        # Advisory (forwarded) findings are neither resolved nor active, so they
        # are subtracted from both the resolved total and per-severity counts.
        resolved_by_severity = {
            severity: max(
                count
                - review.active_by_severity.get(severity, 0)
                - review.advisory_by_severity.get(severity, 0),
                0,
            )
            for severity, count in review.by_severity.items()
        }
        review_bits = [
            f"Review findings: {review.total}",
            f"({_format_count_map(review.by_severity, ('P0', 'P1', 'P2', 'P3'))})",
            f"phases: {_format_count_map(review.by_phase, _FINDING_PHASES)}",
        ]
        if review.advisory:
            review_bits.append(
                "advisory: "
                f"{review.advisory} "
                f"({_format_count_map(review.advisory_by_severity, ('P0', 'P1', 'P2', 'P3'))})"
                " — forwarded to implement"
            )
        review_bits.extend([
            "resolved: "
            f"{max(review.total - review.active - review.advisory, 0)} "
            f"({_format_count_map(resolved_by_severity, ('P0', 'P1', 'P2', 'P3'))})",
            "active: "
            f"{review.active} "
            f"({_format_count_map(review.active_by_severity, ('P0', 'P1', 'P2', 'P3'))})",
        ])
        lines.append("  " + " | ".join(review_bits))
    else:
        lines.append("  Review findings: 0")

    if run_findings.total:
        lines.append(f"  Run findings: {run_findings.total}")
        for kind in _RUN_FINDING_ORDER:
            count = run_findings.by_kind.get(kind, 0)
            if not count:
                continue
            active_count = run_findings.active_by_kind.get(kind, 0)
            lines.append(
                f"    - {kind}: {_format_run_kind_state(count, active_count)}"
            )
    else:
        lines.append("  Run findings: 0")

    if review.active or run_findings.active:
        lines.append(
            f"  Open risks: review={review.active} run={run_findings.active}"
        )
    else:
        lines.append("  Open risks: none")

    # F2: compact scope-expansion summary from the canonical session projection.
    # Always visible when present; nothing rendered (byte-identical) otherwise.
    lines.extend(_scope_expansion_summary_lines(phases))

    return tuple(lines)


def _finding_totals(phases: Mapping[str, Any]) -> tuple[int, int]:
    summary = _review_finding_summary(phases)
    return summary.total, summary.active


def _render_roi_summary(
    session: Mapping[str, Any],
    metrics: Mapping[str, Any],
    *,
    include_accounting: bool,
) -> str:
    """Return the end-of-run ROI line.

    Token ROI is always meaningful; dollar-denominated cost reference is
    appended only when accounting data is explicitly available.
    """
    phases = session.get("phases")
    if not isinstance(phases, Mapping):
        phases = {}
    planned, completed, _failed, _skipped, _incomplete = _task_counts(phases, metrics)
    review_findings, _active_review_findings = _finding_totals(phases)
    run_findings = _run_finding_summary(session, phases)
    tokens_in = _as_int(metrics.get("total_tokens_in"))
    tokens_out = _as_int(metrics.get("total_tokens_out"))
    tokens_total = (
        _as_int(metrics.get("total_tokens"))
        or tokens_in + tokens_out
    )

    correction_kind = _correction_kind(phases)
    if correction_kind and not planned:
        task_part = f"correction={correction_kind}"
    else:
        task_part = f"{completed}/{planned} tasks" if planned else f"{completed} tasks"
    release_outcome = _release_outcome_token(phases)
    release_part = (
        f"release={release_outcome}"
        if release_outcome != "none"
        else ""
    )
    run_part = (
        f"{run_findings.resolved}/{run_findings.total} run findings resolved"
        if run_findings.total
        else "0 run findings"
    )
    parts = [
        f"ROI: tokens={tokens_total:,} (in={tokens_in:,} out={tokens_out:,})",
    ]
    cost = metrics.get("total_cost_usd_equivalent")
    if (
        include_accounting
        and not isinstance(cost, bool)
        and isinstance(cost, (int, float))
    ):
        parts.append(
            format_cost_reference_key_value(
                float(cost),
                estimated=metrics.get("cost_estimated") is True,
            )
        )
    outcome_bits = [task_part]
    if release_part:
        outcome_bits.append(release_part)
    outcome_bits.extend([run_part, f"{review_findings} review findings"])
    parts.append("Outcome: " + ", ".join(outcome_bits))
    return " ".join(parts)


def _phase_usage_rows(
    metrics: Mapping[str, Any],
    *,
    include_accounting: bool,
) -> tuple[str, ...]:
    phases = metrics.get("phases")
    if not isinstance(phases, Mapping) or not phases:
        return ()

    any_cost = include_accounting and any(
        isinstance(value, Mapping)
        and not isinstance(value.get("cost_usd_equivalent"), bool)
        and isinstance(value.get("cost_usd_equivalent"), (int, float))
        for value in phases.values()
    )
    header = "Usage by phase:"
    rows = [header]
    for phase, raw in phases.items():
        if not isinstance(raw, Mapping):
            continue
        tokens_in = _as_int(raw.get("tokens_in"))
        tokens_out = _as_int(raw.get("tokens_out"))
        tokens_cached = _as_int(raw.get("tokens_in_cache_read"))
        tokens_total = _as_int(raw.get("total_tokens")) or tokens_in + tokens_out
        duration = raw.get("duration_s")
        duration_s = (
            f"{float(duration):.1f}s"
            if isinstance(duration, (int, float)) and not isinstance(duration, bool)
            else "-"
        )
        attempts = _as_int(raw.get("attempts"))
        attempt_part = f" attempts={attempts}" if attempts > 1 else ""
        line = (
            f"  {str(phase):<22} "
            f"tokens={tokens_total:>11,} "
            f"(in={tokens_in:,}"
            f"{f' cached={tokens_cached:,}' if tokens_cached else ''} "
            f"out={tokens_out:,}) "
            f"time={duration_s}{attempt_part}"
        )
        if any_cost:
            cost = raw.get("cost_usd_equivalent")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                cost_part = format_cost_reference_key_value(
                    float(cost),
                    estimated=raw.get("cost_estimated") is True,
                )
            else:
                cost_part = "cost_ref=—"
            line += f" {cost_part}"
        rows.append(line)
    return tuple(rows) if len(rows) > 1 else ()


def _subtask_usage_rows(
    metrics: Mapping[str, Any],
    *,
    include_accounting: bool,
) -> tuple[str, ...]:
    """Compact per-subtask usage block for the implement phase.

    Mirrors :func:`_phase_usage_rows`, but reads the additive
    ``metrics["subtasks"]["implement"]`` breakdown so an operator can see —
    at a glance, from the summary alone — which subtask produced the usage
    on a high-usage ``subtask_dag`` run. Returns an empty tuple when no
    records exist (whole_plan / non-subtask runs), so no hollow header or
    misleading empty warning is ever rendered. Order follows the record list,
    which is DAG execution order. This is analytical evidence, not a verdict:
    the caller renders it in neutral (cyan/grey) colors, not success-green.
    """
    subtasks = metrics.get("subtasks")
    if not isinstance(subtasks, Mapping):
        return ()
    rows_data = subtasks.get("implement")
    if not isinstance(rows_data, list) or not rows_data:
        return ()

    any_cost = include_accounting and any(
        isinstance(rec, Mapping)
        and not isinstance(rec.get("cost_usd_equivalent"), bool)
        and isinstance(rec.get("cost_usd_equivalent"), (int, float))
        for rec in rows_data
    )
    rows = ["Subtask usage:"]
    for rec in rows_data:
        if not isinstance(rec, Mapping):
            continue
        # State-only markers (skipped subtasks, or run subtasks whose runtime
        # surfaced no metered usage) ride in the slice so the rollup can count
        # their final state, but they carry no usage fields the operator can
        # attribute — they never belong in the usage-attribution block.
        if not any(
            key in rec for key in ("tokens_in", "tokens_out", "total_tokens")
        ):
            continue
        sid = str(rec.get("subtask_id", "?"))
        tokens_in = _as_int(rec.get("tokens_in"))
        tokens_out = _as_int(rec.get("tokens_out"))
        total_tokens = _as_int(rec.get("total_tokens")) or tokens_in + tokens_out
        duration = rec.get("duration_s")
        duration_s = (
            f"{float(duration):.1f}s"
            if isinstance(duration, (int, float)) and not isinstance(duration, bool)
            else "-"
        )
        tool_calls = _as_int(rec.get("tool_calls"))
        line = (
            f"  {sid:<22} "
            f"tokens={total_tokens:,} (in={tokens_in:,} out={tokens_out:,})"
        )
        if any_cost:
            cost = rec.get("cost_usd_equivalent")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                line += " " + format_cost_reference_key_value(
                    float(cost),
                    estimated=rec.get("cost_estimated") is True,
                )
        line += f" time={duration_s} tools={tool_calls}"
        declared = rec.get("declared_files")
        if isinstance(declared, list) and declared:
            line += f" files={len(declared)}"
        rows.append(line)
    return tuple(rows) if len(rows) > 1 else ()


#: ``run.state.extras`` slot carrying the CI advisor lifecycle aggregate
#: (written by ``pipeline.project.handoff``'s CI auto-retry integration).
_CI_ADVICE_AGGREGATE_KEY = "_ci_agent_advice"


def _render_agent_advice_summary(
    run_dir: Any,
    session: Mapping[str, Any],
    extras: Mapping[str, Any],
    *,
    include_accounting: bool,
) -> str | None:
    """Unified ``Agent advice`` block for the DONE/HALTED summary.

    Primary source is the durable advice evidence: the same normalized
    ``{calls, summary}`` digest the evidence bundle carries (T1/T2), via
    :func:`collect_handoff_advice`. ``session`` is the in-memory run session —
    the ``meta.json`` mapping with the ``'phases'`` key — i.e. exactly the input
    the normalizer expects; do not pass any other shape. Driving the block off
    the same digest keeps its counts consistent with the evidence section and
    covers BOTH the human-driven (``agent_advice``) and CI-policy (``ci_agent``)
    sources.

    Returns ``None`` (the block is omitted entirely) when there is no advice
    evidence — a run that never paused for advice renders nothing.

    Fallback: when there is no ``run_dir`` (no durable surface to read), fall
    back to the in-memory ``_ci_agent_advice`` aggregate on ``run.state.extras``
    so a CI run without a persisted run directory still surfaces its retries.
    """
    if run_dir is not None:
        advice = collect_handoff_advice(run_dir, session)
        if isinstance(advice, Mapping):
            calls = advice.get("calls")
            if isinstance(calls, list) and calls:
                summary = advice.get("summary")
                summary = summary if isinstance(summary, Mapping) else {}
                return _format_agent_advice_block(
                    summary, calls, include_accounting=include_accounting,
                )
        return None
    return _render_ci_agent_advice_summary(extras)


def _format_agent_advice_block(
    summary: Mapping[str, Any],
    calls: list[Any],
    *,
    include_accounting: bool,
) -> str:
    """Format the durable advice digest into the compact DONE/HALTED block.

    Counts come straight from the normalizer's ``summary`` so they match the
    evidence section. A per-source breakdown line is added when any call records
    a ``feedback_source``. The usage line is observe-only: tokens always, the
    cost reference ONLY when accounting is available AND the digest carried a
    cost — never invented, and never folded into the run ROI / totals.
    """
    def _count(key: str) -> int:
        try:
            return int(summary.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    n_calls = _count("calls") or len(calls)
    lines = [
        "Agent advice:",
        f"  calls={n_calls} applied_retries={_count('applied_retries')} "
        f"resolved={_count('resolved_retries')} repeated={_count('repeated')} "
        f"stopped={_count('stopped')}"
        + (f" unknown={_count('unknown')}" if _count("unknown") else ""),
    ]

    by_source: dict[str, int] = {}
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        source = call.get("feedback_source")
        if isinstance(source, str) and source:
            by_source[source] = by_source.get(source, 0) + 1
    if by_source:
        rendered = " ".join(
            f"{src}={by_source[src]}" for src in sorted(by_source)
        )
        lines.append(f"  by source: {rendered}")

    usage = summary.get("usage")
    if isinstance(usage, Mapping) and usage:
        tokens_in = usage.get("tokens_in")
        tokens_out = usage.get("tokens_out")
        tokens_total = 0
        for value in (tokens_in, tokens_out):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                tokens_total += int(value)
        usage_bits = [f"tokens={tokens_total:,}"]
        cost = usage.get("cost_usd_equivalent")
        if (
            include_accounting
            and isinstance(cost, (int, float))
            and not isinstance(cost, bool)
        ):
            usage_bits.append(
                format_cost_reference_key_value(
                    float(cost),
                    estimated=usage.get("cost_estimated") is True,
                )
            )
        lines.append("  usage: " + " ".join(usage_bits))

    return "\n".join(lines)


def _render_ci_agent_advice_summary(extras: Mapping[str, Any]) -> str | None:
    """Fallback ``Agent advice`` block built from the in-memory CI aggregate.

    Used by :func:`_render_agent_advice_summary` ONLY when there is no
    ``run_dir`` to read durable advice evidence from. Built from the real
    ``_ci_agent_advice`` aggregate the CI auto-retry integration accumulates on
    ``run.state.extras`` (durable per-advice detail lives in the advice
    artifacts, not here). Returns ``None`` when no ci_agent retry was ever
    attempted (``retries == 0``) so human / interactive / non-CI runs render
    nothing.
    """
    agg = extras.get(_CI_ADVICE_AGGREGATE_KEY) if isinstance(extras, Mapping) else None
    if not isinstance(agg, Mapping):
        return None
    try:
        retries = int(agg.get("retries") or 0)
    except (TypeError, ValueError):
        return None
    if retries <= 0:
        return None
    resolved = int(agg.get("resolved") or 0)
    stopped = int(agg.get("stopped") or 0)
    recommendation = str(agg.get("last_recommendation") or "")
    confidence = str(agg.get("last_confidence") or "")
    return (
        "Agent advice:\n"
        f"  ci_agent retries={retries} resolved={resolved} stopped={stopped}\n"
        f"  last recommendation={recommendation} confidence={confidence}"
    )


def _record_advice_usage_backstop(run: Any) -> None:
    """Re-derive observe-only advice usage from durable artifacts at finalize.

    The per-phase push (``pipeline.project.run._push_handoff_advice_usage`` in
    ``_fsm_metrics``) only runs at a phase-end, so an advice call with no
    following phase — an operator/CI stop, a non-retry recommendation, or a
    ``retry_with_advice`` that returned to the menu — would never reach
    ``metrics.json``. This finalize-time backstop closes that gap: it folds the
    same ``collect_handoff_advice`` summary the evidence bundle uses into the
    observe-only ``handoff_advice`` metrics slot, keeping metrics and evidence
    consistent. ``MetricsCollector.record_advice_usage`` has REPLACE semantics
    (the leaf re-derives the full aggregate), so this is idempotent with the
    per-phase push and never touches ``total_*``. Best-effort: a missing
    ``output_dir`` / record API or a usage-attribution failure must never break
    finalization.
    """
    output_dir = getattr(run, "output_dir", None)
    if output_dir is None:
        return
    record = getattr(getattr(run, "_metrics", None), "record_advice_usage", None)
    if not callable(record):
        return
    try:
        advice = collect_handoff_advice(output_dir, run.session)
        if not isinstance(advice, Mapping):
            return
        summary = advice.get("summary")
        usage = summary.get("usage") if isinstance(summary, Mapping) else None
        if isinstance(usage, Mapping) and usage:
            record(usage)
    except Exception:
        return


def _summary_line_color(summary_text: str) -> str:
    if any(
        marker in summary_text
        for marker in ("=reject", "=fail", "=halt")
    ):
        return C.YELLOW
    return C.GREEN


# ── dataclasses ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FinalizationContext:
    """Inputs for :func:`finalize_project_run`.

    ``run`` is the :class:`pipeline.project.run._PipelineRun`-shaped
    object whose attributes the silent service reads (``output_dir``,
    ``session``, ``state``, ``_metrics``, ``_ckpt``,
    ``worktree_context``, ``_done_summary_profile``, ``profile_name``,
    ``session_ts``, ``project_path``, ``parent_run_id``,
    ``project_alias``, ``_worktree_cvar_token``,
    ``_sandbox_cvar_token``) and methods it invokes
    (``_effective_diff_cwd``, ``_run_commit_delivery``). Duck-typed
    (``Any``) so the silent service stays testable without
    instantiating the real dataclass.
    """

    run: Any


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    """Structured outcome of :func:`finalize_project_run`.

    Every artifact field is the **actual return value** of the
    underlying writer (or ``None`` when the writer was skipped — e.g.
    ``output_dir`` was ``None``, or the run halted before any
    artifact was produced). Field list derived from inspection of
    the legacy ``finalize()`` body; do not add paths the underlying
    writers do not actually create.
    """

    status: Literal["done", "halted", "awaiting_human_review", "failed"]
    halt_reason: str | None
    summary_text: str

    # Correction-route presentation (ADR 0086): the compact DONE/HALTED
    # route line and whether it is the halting (amber) variant. ``(None,
    # False)`` means "no triage evidence" — a non-correction run.
    correction_route_line: str | None
    correction_route_halted: bool

    session_path: Path | None
    metrics_path: Path | None
    diff_path: Path | None
    evidence_path: Path | None
    mirrored_artifacts: list[Path]

    # Terminal-wrapper hints — derived during the silent pass so the
    # wrapper does not re-do any work. ``None`` / ``False`` means
    # "nothing to surface".
    context_summary_text: str | None
    has_api_equivalent_cost: bool
    is_subpipeline: bool
    mirror_error: str | None
    worktree_teardown_message: str | None  # "removed" / "retained at ..." / None
    run_id: str | None
    task_summary: str | None
    no_change_outcome: Mapping[str, Any] | None
    evidence_summary_lines: tuple[str, ...]
    roi_summary_line: str
    usage_breakdown_lines: tuple[str, ...]
    # Compact per-subtask usage block (subtask_dag runs only); empty for
    # whole_plan / non-subtask runs. Defaulted so existing construction sites
    # and tests that omit it keep working.
    subtask_usage_lines: tuple[str, ...] = ()
    # Compact CI handoff-advisor block ('Agent advice: ...'); ``None`` when no
    # ci_agent retry ran. Rendered in both the DONE and HALTED tails.
    ci_agent_advice_summary: str | None = None
    # Compact 'Verification gates' block for the DONE/HALTED tail; empty tuple
    # when there is no verification contract and no receipt/auto-run trail (the
    # omission case — no misleading empty block) or when ``output_dir`` is None.
    # Sourced read-only from durable receipt evidence by ``verification_timeline``.
    verification_gate_lines: tuple[str, ...] = ()
    # Release-gate outcome token for the terminal wrapper's banner choice:
    # 'approved' / 'rejected' / 'pending' / 'none'. Derived from the LAST
    # final_acceptance attempt via ``_release_outcome_token`` so a recovered
    # gate_rerun (REJECTED -> APPROVED) reads 'approved' and stays green.
    # Defaulted so the single construction site and existing tests are unaffected.
    release_outcome: str = "none"
    # Companion delivery caveat (T2): set when the primary checkout shipped
    # (committed / applied_uncommitted) but a declared companion repo is still
    # uncommitted (``dirty``). ``None`` for a clean single-repo run or a
    # fully-delivered multi-repo run. Carries the rendered caveat + actionable
    # next step; the terminal wrapper renders it amber and qualifies the DONE
    # header so a multi-repo run never reads as fully complete with a companion
    # left behind. Defaulted so existing construction sites/tests are unaffected.
    companion_caveat: CompanionDeliveryCaveat | None = None
    # F2 scope-expansion durable evidence, rendered from the single canonical
    # session path ``phases['final_acceptance']['scope_expansion']``. Carries the
    # compact lines for typed consumers (the same text is already folded into
    # ``evidence_summary_lines`` for the DONE/Evidence block). Empty tuple when
    # the run produced no out-of-plan scope evidence. Defaulted so existing
    # construction sites and tests are unaffected.
    scope_expansion_lines: tuple[str, ...] = ()
    # Compact 'Delivery: ...' destination line for the DONE tail, rendered from
    # the terminal ``commit_delivery`` audit record. One scannable line naming
    # where the diff landed (pushed delivery branch + PR / checkout commit /
    # applied uncommitted / skipped / not delivered). Empty tuple when the run
    # carries no terminal delivery record. Defaulted so existing construction
    # sites and tests are unaffected.
    delivery_summary_lines: tuple[str, ...] = ()
    # Computed once from post-reconcile durable facts; terminal presentation
    # consumes this without re-running delivery policy.
    terminal_delivery: TerminalDeliveryOutcome = field(
        default_factory=lambda: TerminalDeliveryOutcome(
            TerminalDeliveryDisposition.UNKNOWN,
        ),
    )


# ── companion delivery caveat (T2) ────────────────────────────────────────
#
# The primary checkout can deliver (commit / apply) while a declared companion
# repository — derived in T1 from the durable plan scope — is still dirty. A
# green DONE would hide that the multi-repo delivery is only half done. This
# focused helper turns the durable ``session['multi_project_delivery']`` block
# (propagated by ``run.py`` from the T1 disclosure — NO git re-scan here) into a
# caveat + actionable next step the finalize banner cannot present as complete.


#: Primary delivery statuses that count as "the primary shipped" — the only
#: states where a still-dirty companion is a delivery-completeness caveat.
_COMPANION_PRIMARY_DELIVERED: frozenset[str] = frozenset(
    {"committed", "applied_uncommitted"},
)


@dataclass(frozen=True, slots=True)
class CompanionDeliveryCaveat:
    """Caveat: the primary shipped but a companion repo did not.

    Built from the durable ``session['multi_project_delivery']`` block. Present
    ONLY when the primary checkout delivered (``committed`` /
    ``applied_uncommitted``) yet at least one declared companion repo is still
    ``dirty``. ``lines`` is the rendered DONE/HALTED-block view (caveat + next
    step); ``dirty_companions`` is the per-repo disclosure (alias / path /
    changed paths / state) for typed consumers.
    """

    primary_status: str
    dirty_companions: tuple[Mapping[str, Any], ...]
    lines: tuple[str, ...]


def _companion_changed_paths(entry: Mapping[str, Any]) -> list[str]:
    paths = entry.get("changed_paths")
    if not isinstance(paths, list):
        return []
    return [p for p in paths if isinstance(p, str) and p]


def _render_companion_caveat_lines(
    dirty: list[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Render the caveat header, per-repo disclosure, and actionable next step."""
    plural = "repo" if len(dirty) == 1 else "repos"
    lines = [
        f"Companion delivery incomplete: primary delivered, but {len(dirty)} "
        f"companion {plural} still uncommitted — delivery is NOT fully complete:",
    ]
    for entry in dirty:
        alias = str(entry.get("alias") or "?")
        paths = _companion_changed_paths(entry)
        rendered = ", ".join(paths) if paths else "uncommitted changes"
        lines.append(f"  - {alias}: {rendered}")
    lines.append(
        "  Next: review and commit the companion repo(s) above, or start a "
        "cross-run / follow-up for companion delivery.",
    )
    return tuple(lines)


def build_companion_delivery_caveat(
    session: Mapping[str, Any],
) -> CompanionDeliveryCaveat | None:
    """Build the companion caveat from ``session['multi_project_delivery']``.

    Returns ``None`` — no caveat — for a clean single-repo run (no block), a
    primary that did not deliver (status not committed / applied_uncommitted),
    and a multi-repo run whose every declared companion is already ``committed``
    (or only a non-dirty ``planned_requirement``). Pure: reads the durable block
    T1 propagated, never re-scans git.
    """
    block = session.get("multi_project_delivery")
    if not isinstance(block, Mapping):
        return None
    primary_status = str(block.get("primary_status") or "")
    if primary_status not in _COMPANION_PRIMARY_DELIVERED:
        return None
    companions = block.get("companions")
    if not isinstance(companions, list):
        return None
    dirty = [
        c for c in companions
        if isinstance(c, Mapping) and c.get("state") == "dirty"
    ]
    if not dirty:
        return None
    return CompanionDeliveryCaveat(
        primary_status=primary_status,
        dirty_companions=tuple(dirty),
        lines=_render_companion_caveat_lines(dirty),
    )


# ── silent service ───────────────────────────────────────────────────────


# Friendly terminal-banner labels per ``halt_reason``. Recoverable halts
# (operator-chosen fix / halt, dirty target) render amber; executor
# failures render red. Unknown reasons fall back to the raw reason so a
# new halt path is never silently mislabelled as a success.
_HALT_BANNER_LABELS: dict[str, tuple[str, str]] = {
    "commit_decision_fix": (
        "Run halted — correction follow-up requested", C.YELLOW,
    ),
    "commit_decision_halt": ("Run halted — operator halted", C.YELLOW),
    # ADR 0100: defer mode parked the delivery decision for a later operator
    # call. Recoverable through ``decide_delivery`` -> amber.
    "commit_delivery_pending": (
        "Run halted — delivery decision pending", C.YELLOW,
    ),
    "commit_delivery_target_dirty": (
        "Run halted — project checkout dirty", C.YELLOW,
    ),
    "commit_delivery_failed": ("Run halted — delivery failed", C.RED),
    "commit_delivery_verification_blocked": (
        "Run halted — verification receipts incomplete", C.YELLOW,
    ),
    # Stage C delivery-scope enforcement (T4): strict-mono sibling-repo edits
    # parked a reversible, decidable delivery-scope gate. Recoverable through
    # ``decide_delivery`` (skip / halt / expanded re-run) -> amber.
    "commit_delivery_scope_blocked": (
        "Run halted — delivery scope violation", C.YELLOW,
    ),
    "final_acceptance_no_diff": (
        "Run halted — final acceptance found no diff", C.YELLOW,
    ),
    # ADR 0106: the release was rejected and delivery never applied, so the run
    # halts at an actionable terminal instead of finishing as a green DONE.
    # Recoverable through a correction follow-up or operator halt -> amber.
    "final_acceptance_rejected": (
        "Run halted — release rejected", C.YELLOW,
    ),
    "correction_triage_blocked": (
        "Run halted — correction triage blocked", C.YELLOW,
    ),
    "correction_triage_missing_context": (
        "Run halted — correction triage missing context", C.YELLOW,
    ),
    # ADR 0098: a correction round repeated the same release blockers with no
    # relevant progress. Recoverable by an operator decision -> amber.
    "correction_not_converging": (
        "Run halted — correction not converging", C.YELLOW,
    ),
    "phase_handoff_unattended_halt": (
        "Run halted — unattended phase handoff", C.YELLOW,
    ),
}


def _halt_banner(halt_reason: str | None) -> tuple[str, str]:
    """Map a ``halt_reason`` to a (label, color) terminal-header pair."""
    if halt_reason and halt_reason in _HALT_BANNER_LABELS:
        return _HALT_BANNER_LABELS[halt_reason]
    suffix = f" — {halt_reason}" if halt_reason else ""
    return (f"Run halted{suffix}", C.YELLOW)


def _done_line(
    text: str,
    *,
    color: str = C.WHITE,
    icon: str = "✓",
    icon_color: str = C.GREEN,
    bold: bool = False,
) -> None:
    """Render one semantic DONE-block line without turning every line green."""
    body_codes = (color, C.BOLD) if bold else (color,)
    print(f"  {paint(icon, icon_color, C.BOLD)} {paint(text, *body_codes)}")


def _resolve_terminal_status(run: Any) -> None:
    """Set ``session.status`` / ``halt_reason`` / ``halt`` from
    ``state.halt`` + ``profile_name``. Mirrors the head of legacy
    ``finalize()``. Subsequent ``_run_commit_delivery`` may flip
    these again.

    The terminal status decision (``halted`` + nested halt compat block /
    ``awaiting_human_review`` plan-only tail / ``done``) lives in the single
    :func:`pipeline.run_state.terminal_outcome.resolve_terminal_outcome`
    reducer; this site only extracts the run facts it reads. The top-level
    ``halt_reason`` the reducer writes for ``state.halt`` mirrors the SDK halt
    path (``sdk/phase_handoff.py`` writes ``"phase_handoff_halt"``), so every
    state.halt-driven termination records a non-null reason instead of hiding
    it under ``meta.halt.reason``.
    """
    resolve_terminal_outcome(
        run.session,
        state_halt=bool(run.state.halt),
        halt_reason=run.state.halt_reason,
        current_phase=run.state.extras.get("_current_phase", ""),
        profile_name=run.profile_name,
        phase_handoff_override=bool(
            run.state.extras.get("phase_handoff_override"),
        ),
    )


def _record_diff_patch_block(run: Any, captured: Any) -> Path | None:
    """Persist the durable ``diff_patch`` apply-check block onto the session.

    ``captured`` is the :class:`CapturedRunDiff` (or ``None``) returned by
    ``capture_run_diff_with_apply_check``. When it carries an apply-check
    result, store the compact ``{status, reason, patch_path, baseline_ref,
    detail}`` block under ``session["diff_patch"]`` so the same ``meta.json``
    read by status/delivery surfaces records the captured patch's validity —
    not only the transient evidence event. Returns the artifact path (or
    ``None``) so the caller keeps the existing ``diff_path`` flow.
    """
    if captured is None:
        return None
    if captured.apply_check is not None:
        from pipeline.engine.diff_apply_check import diff_patch_durable_block

        run.session["diff_patch"] = diff_patch_durable_block(captured.apply_check)
    return captured.path


def _apply_no_diff_final_acceptance_outcome(
    run: Any, *, diff_path: Path | None,
) -> None:
    """Record an explicit no-change release-gate outcome.

    Routes through the single
    :func:`pipeline.run_state.terminal_outcome.apply_no_diff_terminal` reducer,
    which owns the no-diff verdict detectors and the
    ``halted`` + ``no_op_outcome`` / ``no_change_outcome`` markers. Runs AFTER
    diff capture + ``_run_commit_delivery`` (so a captured diff or a
    delivery-induced non-``done`` status already short-circuits it).
    """
    apply_no_diff_terminal(run.session, diff_path=diff_path)


# Delivery executor outcomes that settle an ordinary correction follow-up as
# successfully resolved (delivered or deliberately skipped). Reaching one of
# these closes out (supersedes) a rejected-FA / correction parent — see
# :func:`_supersede_parent_correction_after_followup`.
_DELIVERY_DELIVERED_STATUSES = frozenset(
    {"committed", "applied_uncommitted", "skipped"},
)


def _is_real_contract_final_acceptance(entry: Mapping[str, Any]) -> bool:
    """True only for a genuine release-contract verdict, not a stub.

    A real contract verdict carries a non-empty ``contract_status`` mapping
    (``ParsedRelease.contract_status.to_dict()``). Two non-contract shapes are
    excluded:

    * the parse-error path stamps ``contract_status=None``
      — a hard schema halt, not a release verdict;
    * the no-diff synth stub (``_write_no_diff_final_acceptance``) marks the
      entry with ``skipped`` / ``diff='none'`` / ``no_change_outcome`` — that
      path is already owned by ``_apply_no_diff_final_acceptance_outcome``.

    Older hand-built tests and persisted fixtures may omit ``contract_status``;
    absence alone is not a rejection, but it also should not hide an otherwise
    explicit verdict/blocker record. A present-but-invalid ``contract_status``
    remains excluded.
    """
    if "contract_status" in entry:
        contract_status = entry.get("contract_status")
        if not isinstance(contract_status, Mapping) or not contract_status:
            return False
    if entry.get("skipped"):
        return False
    if entry.get("diff") == "none":
        return False
    return "no_change_outcome" not in entry


def _final_acceptance_rejected_signal(entry: Mapping[str, Any]) -> bool:
    """True when a final-acceptance entry carries any rejection signal."""
    verdict = entry.get("verdict")
    rejected = is_rejected(verdict)
    not_ship_ready = entry.get("ship_ready") is False
    not_approved = entry.get("approved") is False
    blockers = entry.get("release_blockers")
    has_blockers = isinstance(blockers, (list, tuple)) and len(blockers) > 0
    return rejected or not_ship_ready or not_approved or has_blockers


def _release_rejected_from_phases(phases: Mapping[str, Any]) -> bool:
    """True when the persisted final acceptance recorded a rejected release.

    Source of truth is the persisted ``final_acceptance`` (or
    ``cross_final_acceptance``) record: a ``REJECTED`` verdict, ``ship_ready is
    False``, ``approved is False``, OR a non-empty ``release_blockers`` list.

    The ``release_blockers`` clause is explicit defense-in-depth on top of the
    schema invariant in ``core/contracts/release_schema.py`` (an ``APPROVED``
    verdict forbids blockers): even if a future writer leaked a blockers-only
    record past verdict/ship_ready, the presence of release blockers alone
    still reads as rejected here, so the terminal can never silently green.
    """
    _phase, record = _release_record(phases)
    if not record:
        return False
    return (
        _is_real_contract_final_acceptance(record)
        and _final_acceptance_rejected_signal(record)
    )


def _supersede_stale_rejection_residue(session: MutableMapping[str, Any]) -> None:
    """Finalization seam over :func:`terminal_outcome.supersede_same_run_residue`.

    Kept as a thin named delegator because the control-loop eviction-parity test
    imports it directly as the "site A" same-run supersede call-path. The whole
    decision (canonical eviction + the conditional phantom-gate
    ``commit_delivery`` drop that stays call-site decision-logic, NOT in
    ``TRANSIENT_SETTLE_KEYS``) now lives in the reducer; this carries no logic of
    its own (ADR 0115 slice 3b-1).
    """
    supersede_same_run_residue(session)


def _apply_rejected_release_terminal_outcome(run: Any) -> None:
    """Reconcile the finalization terminal + delivery-gate to the authoritative verdict.

    Runs AFTER ``_run_commit_delivery`` (and the no-diff outcome helper) on a
    still-``done`` session. Bidirectional against the last authoritative
    ``final_acceptance`` record:

    Rejected authoritative verdict (writes the rejection terminal + gate):

    * delivery NOT applied (no operator override) — flip a stale ``done`` to
      ``halted`` with ``halt_reason='final_acceptance_rejected'`` and record a
      structured ``rejected_outcome`` (modeled on ``no_op_outcome``) carrying
      the visible ``release_verdict`` / ``release_blockers`` / short summary,
      so a rejected run never reads as a silent successful ``done``.
    * delivery actually applied (operator override — ``committed`` /
      ``applied_uncommitted``) — keep ``done`` but record a durable
      ``delivery_override`` marker with ``release_verdict``,
      ``release_blockers`` and the override reason, so the outcome is
      observably distinct from a clean success.

    Approved authoritative verdict (supersedes stale rejection residue): a
    successful repeat ``final_acceptance`` evicts any terminal-rejection markers
    AND the phantom rejected ``commit_delivery`` gate left by a prior REJECTED
    attempt of the same run — see :func:`_supersede_stale_rejection_residue`.

    Only acts on a still-``done`` session: the no-diff reject path
    (``final_acceptance_no_diff``) and every delivery-induced halt have already
    settled their own non-``done`` terminal, so guarding on ``done`` leaves
    them untouched.

    The rejected branch is ADR 0106; the approved supersede branch is the
    bidirectional refinement in ADR 0109 (a successful repeat/resumed final
    acceptance reconciles both the terminal and the delivery-gate to the latest
    authoritative verdict).

    Thin seam (ADR 0115 slice 3b-1): this owns only the guard (still-``done``,
    not dry-run, ``phases`` is a Mapping) and reads the plain facts off the
    persisted record (rejected, verdict, blockers, short summary, the
    engine-backstop / verification-gaps cause, delivery status). The
    engine-backstop cause is read from the SAME ``final_acceptance`` record the
    handler wrote (no re-derive) and normalized via
    :func:`terminal_outcome.normalize_engine_reason`, so the REJECTED terminal
    carries the authoritative engine reason rather than only the agent's positive
    summary (ADR 0106). The flip done↔halted and the marker shapes live in the
    reducer :func:`terminal_outcome.resolve_rejected_release_terminal`; the
    verdict is read through the single ``release_verdict`` source.
    """
    if run.session.get("status") != "done":
        return
    if getattr(getattr(run, "state", None), "dry_run", False) is not False:
        return
    phases = run.session.get("phases")
    if not isinstance(phases, Mapping):
        return

    rejected = _release_rejected_from_phases(phases)
    _phase, record = _release_record(phases)
    verdict = normalize_verdict(record.get("verdict")) or "REJECTED"
    raw_blockers = record.get("release_blockers")
    blockers = list(raw_blockers) if isinstance(raw_blockers, list) else []
    short_summary = record.get("short_summary")
    # The engine-authoritative rejection cause is read off the SAME record the
    # handler wrote (no re-derive): a forced engine receipt backstop and/or
    # reviewer verification gaps. The reducer owns the marker shape; the seam
    # only normalizes the facts and delegates.
    engine_reason = normalize_engine_reason(
        verification_gaps=record.get("verification_gaps"),
        engine_backstop=record.get("engine_backstop"),
    )

    delivery = run.session.get("commit_delivery")
    delivery_status = (
        str(delivery.get("status")) if isinstance(delivery, Mapping) else ""
    )

    resolve_rejected_release_terminal(
        run.session,
        rejected=rejected,
        delivery_status=delivery_status,
        verdict=verdict,
        blockers=blockers,
        short_summary=short_summary,
        engine_reason=engine_reason,
    )


def _supersede_parent_correction_after_followup(run: Any) -> None:
    """Close a rejected-FA / correction parent once its follow-up child delivers.

    The cross-run analogue of :func:`_supersede_stale_rejection_residue` (which
    reconciles a single run on its own approved retry). When THIS run is a
    ordinary correction follow-up of a parent that dead-ended on a rejected final
    acceptance (``final_acceptance_rejected`` / ``final_acceptance_no_diff``) or a
    marked correction (``commit_decision_fix``), AND this child actually delivered
    (``commit_delivery.status`` in ``committed`` / ``applied_uncommitted`` /
    ``skipped``), reconcile the PARENT's ``meta.json`` so it stops reading as an
    active correction candidate across every surface:

    * evict the phantom rejected ``commit_delivery`` gate (and its
      ``multi_project_delivery`` mirror) so ``delivery_decision_state(parent)`` is
      no longer decidable as a correction and the parent's stale
      ``release_blockers`` are no longer authoritative;
    * evict the terminal-rejection residue (``rejected_outcome`` /
      ``halt_reason`` / ``halted_at`` / ``halt`` / ``delivery_override``);
    * settle the parent to ``done`` and stamp a durable ``superseded_by_followup``
      marker referencing this child, so the delivery gate, diagnose, and live
      status all read the parent as superseded/closed rather than active.

    Idempotent and guarded: a no-op unless this is a valid ordinary correction
    child (follow-up lineage, correction profile, and correction context), this
    child's delivery succeeded, and the parent is genuinely a rejected-FA / fix
    terminal. A re-run
    finds the parent already settled to ``done`` (no longer a rejected/fix
    terminal) and returns without change. Best-effort: any lookup / read / write
    failure degrades to a no-op and never breaks the child's own finalization. The
    same-run approved-retry path (:func:`_supersede_stale_rejection_residue`) is
    untouched — this only fires for a distinct correction child.
    """
    if not run.output_dir:
        return
    parent_run_id = run.session.get("parent_run_id")
    if not isinstance(parent_run_id, str) or not parent_run_id:
        extras = getattr(getattr(run, "state", None), "extras", None)
        if isinstance(extras, Mapping):
            parent_run_id = extras.get("parent_run_id")
    if not isinstance(parent_run_id, str) or not parent_run_id:
        return
    if (
        run.session.get("resume_mode") != "followup"
        or run.session.get("profile") != "correction"
        or not (Path(run.output_dir) / "correction_context.md").is_file()
    ):
        return
    delivery = run.session.get("commit_delivery")
    delivery_status = (
        str(delivery.get("status")) if isinstance(delivery, Mapping) else ""
    )
    if delivery_status not in _DELIVERY_DELIVERED_STATUSES:
        return

    try:
        from pipeline.control.resume_context import (
            is_terminal_commit_decision_fix,
            is_terminal_final_acceptance_rejected,
        )
        from sdk.runs import find_run, load_meta

        runs_dir = Path(run.output_dir).parent
        ref = find_run(parent_run_id, runs_dir=runs_dir, cwd=None)
        parent_meta = load_meta(ref.run_dir)
    except Exception:  # noqa: BLE001 — a cross-run reconcile must never break finalize
        return
    if not isinstance(parent_meta, MutableMapping):
        return
    # Guard + idempotency: only a genuine rejected-FA / fix terminal is
    # superseded. A re-run sees the already-settled ``done`` parent and stops.
    if not (
        is_terminal_final_acceptance_rejected(parent_meta)
        or is_terminal_commit_decision_fix(parent_meta)
    ):
        return

    child_run_id = run.session_ts or Path(run.output_dir).name
    # Pure parent-meta mutation lives in the reducer; this seam keeps only the
    # file IO + guards (ADR 0115 slice 3b-1). The unconditional delivery-drop and
    # the ``superseded_by_followup`` marker are owned by ``supersede_parent_meta``.
    supersede_parent_meta(
        parent_meta,
        child_run_id=child_run_id,
        child_status=str(run.session.get("status") or "done"),
        delivery_status=delivery_status,
    )

    try:
        save_session(ref.run_dir, parent_meta)
    except Exception:  # noqa: BLE001 — a failed parent write must not break finalize
        return


def _run_plugin_worktree_teardown(run: Any) -> None:
    """Best-effort ADR 0131 plugin ``worktree_teardown`` at run finalization.

    Runs the plugin's declared teardown steps in the worktree cwd before the git
    worktree is released — but only for a **terminal** run. A run paused awaiting
    a phase-handoff decision keeps its worktree AND its external stack for
    resume, so teardown is skipped there.

    Wholly best-effort: the run is already terminal, so any failure (import,
    config shape, a failing ``docker compose down``) is swallowed here — the
    engine step-runner itself also never raises — so cleanup can never mask the
    run's real outcome.
    """
    # Resumable pause: the worktree (and its stack) must survive for resume.
    if run.session.get("status") == "awaiting_phase_handoff":
        return
    steps = getattr(getattr(run, "plugin", None), "worktree_teardown", None)
    if not steps:
        return
    ctx = run.worktree_context
    try:
        from pipeline.engine.worktree_bootstrap import run_worktree_teardown
        run_worktree_teardown(
            steps,
            source_root=ctx.project_dir,
            worktree_path=ctx.path,
        )
    except Exception:  # noqa: BLE001 — terminal-run cleanup must never raise
        pass


def _teardown_worktree_for_finalization(run: Any) -> str | None:
    """Tear down an actual isolated worktree and describe the disposition.

    ``WorktreeContext(mode="off")`` is a real context object but represents the
    canonical checkout, not a disposable worktree. Its teardown is therefore
    not applicable and must not be rendered as ``removed``.
    """
    ctx = run.worktree_context
    if ctx is None or getattr(ctx, "mode", None) == "off":
        return None
    _run_plugin_worktree_teardown(run)
    from pipeline.engine.worktree import teardown_worktree

    result = teardown_worktree(ctx, retain=True)
    return result.error or "removed"


def finalize_project_run(ctx: FinalizationContext) -> FinalizationResult:
    """Silent structured finalization. No terminal output.

    Side-effects ARE present: writes session/metrics/diff/evidence,
    mutates session status (delivery may flip ``done`` → ``halted``),
    emits ``run.end`` + ``phase.end``, sets checkpoint final status,
    mirrors artifacts, tears down the worktree, resets ContextVar
    tokens. The invariant is **no stdout / stderr / banner / success /
    warn / print** — UI clients can drive the pipeline without
    terminal noise.

    Ordering (load-bearing):

    1. ``_resolve_terminal_status`` writes the pre-delivery status.
    2. ``capture_run_diff_with_apply_check`` writes ``diff.patch``
       BEFORE delivery so the patch is preserved even when
       ``approve``/``apply`` succeeds and mutates the project checkout.
    3. ``_run_commit_delivery`` may flip ``status`` to ``halted``
       and stamp a delivery-specific ``halt_reason``.
    4. ``run.end`` event payload + checkpoint final status read the
       **post-delivery** ``session["status"]``.
    """
    from core.observability import events as _events
    from core.observability.trace import vtrace

    run = ctx.run

    # Close the scheduled-gate artifact before any evidence/DONE consumer can
    # inspect the run, so every terminal surface sees one durable disposition.
    from pipeline.project.verification_ledger_runtime import finalize as finalize_ledger

    finalize_ledger(run)

    # 1) Status from state.halt + profile_name (pre-delivery).
    _resolve_terminal_status(run)

    # 2) Capture diff BEFORE delivery, then run delivery. Delivery may
    # flip session["status"] to halted (commit_decision_halt /
    # target_dirty / commit_failed / apply_failed) and stamp a
    # delivery-specific halt_reason.
    diff_path: Path | None = None
    diff_captured = False
    if run.output_dir:
        from pipeline.engine.diff_apply_check import capture_run_diff_with_apply_check
        effective_diff_cwd = run._effective_diff_cwd()
        delivery_baseline = run._commit_delivery_baseline()
        captured = capture_run_diff_with_apply_check(
            effective_diff_cwd,
            run.output_dir,
            baseline_ref=delivery_baseline,
        )
        diff_path = _record_diff_patch_block(run, captured)
        diff_captured = True
        run._run_commit_delivery(effective_diff_cwd)
        _apply_no_diff_final_acceptance_outcome(run, diff_path=diff_path)
        _apply_rejected_release_terminal_outcome(run)
        # Cross-run reconcile: a successful ordinary correction follow-up closes out
        # the rejected-FA / correction parent it was launched to fix, so the
        # parent stops reading as an active correction candidate everywhere.
        _supersede_parent_correction_after_followup(run)

    # Pure projection after delivery and rejected-release reconciliation.  The
    # terminal wrapper must consume this result rather than re-read session.
    terminal_delivery = project_terminal_delivery(run.session)

    # 3) DONE-banner log entry (file + event; no stdout). Mirrors the
    # legacy ``banner("DONE", ...)`` event-side: writes a "DONE
    # START" line to progress.log and emits phase.start("DONE").
    is_subpipeline = bool(run.parent_run_id and run.project_alias)
    if is_subpipeline:
        done_title = (
            f"Sub-pipeline [{run.project_alias}] complete  "
            f"— returning to cross-run {run.parent_run_id}"
        )
    else:
        done_title = "Pipeline complete"
    log_phase("DONE", done_title, "START")

    # 4) Summary string (phase chips). Built from the post-delivery
    # phase_log so a delivery-induced halt still shows the per-phase
    # outcomes that ran. ``halted_phase`` is taken ONLY from the
    # state.halt-driven nested ``halt`` block (set by
    # ``_resolve_terminal_status``); delivery/no-diff halts never set it,
    # so they leave every chip on its genuine outcome.
    halted_phase = run.session.get("halt", {}).get("phase") or None
    summary_text = _render_done_summary(
        run._done_summary_profile,
        run.state.phase_log,
        halted_phase=halted_phase,
    )
    log_phase("DONE", summary_text, "DONE")

    # 4b) Correction-route DONE line (ADR 0086 presentation). The route was
    # stamped onto ``correction_triage`` at its END and delivery has already
    # run (step 2), so ``session['phases']`` is final for route purposes
    # here. Emit a second ``DONE`` progress entry carrying the compact route
    # decision — strictly BEFORE ``run.end`` so no phase event follows it.
    # Strict no-op (no record, no event) for any non-correction run, keeping
    # progress.log and the event order byte-identical.
    correction_route_display = format_correction_route_summary(
        run.session.get("phases")
    )
    correction_route_line: str | None = None
    correction_route_halted = False
    if correction_route_display is not None:
        correction_route_line = correction_route_display.text
        correction_route_halted = correction_route_display.halted
        log_phase("DONE", correction_route_line, "DONE")

    # 5) Emit run.end (status is final by this point).
    run_end_payload: dict[str, Any] = {
        "status": run.session["status"],
        "summary": summary_text,
    }
    if run.session.get("status") == "halted":
        run_end_payload["halt_reason"] = run.session.get("halt_reason")
    _events.emit("run.end", **run_end_payload)

    # 6) Persist session + metrics + evidence (only when output_dir).
    session_path: Path | None = None
    metrics_path: Path | None = None
    evidence_path: Path | None = None
    context_summary_text: str | None = None
    has_api_equivalent_cost = False
    metrics_dict: Mapping[str, Any] = {}
    if run.output_dir:
        session_path = save_session(run.output_dir, run.session)
        vtrace("session", str(session_path), extra=f"{session_path.stat().st_size} bytes")
        # F2 backstop: re-derive observe-only handoff-advice usage from the
        # durable advice artifacts right before metrics.json is written, so an
        # advice call with NO following phase-end (operator/CI stop, a non-retry
        # recommendation, or a retry that returned to the menu) still lands in
        # metrics.json['handoff_advice'] — consistent with the evidence section.
        # REPLACE semantics make this idempotent with the per-phase push; it
        # never alters total_*.
        _record_advice_usage_backstop(run)
        metrics_path = run._metrics.save(run.output_dir)
        vtrace("metrics", str(metrics_path), extra=run._metrics.summary_line())
        metrics_dict = run._metrics.as_dict()
        run.session["metrics"] = metrics_dict
        # M14.4.2 — peak runtime-reported context fullness summary.
        # Computed once here so the wrapper can render it without
        # re-reading the session.
        from pipeline.observability.context_pressure import (
            format_context_summary,
        )
        context_summary_text = format_context_summary(run.session) or None
        # Defensive: if the capture above was skipped (only possible
        # when output_dir was None, which is excluded by the outer
        # ``if``), retry now. Preserves the legacy fallback for any
        # future caller that bypasses the diff_captured guard.
        if not diff_captured:
            from pipeline.engine.diff_apply_check import (
                capture_run_diff_with_apply_check,
            )
            captured = capture_run_diff_with_apply_check(
                run._effective_diff_cwd(),
                run.output_dir,
                baseline_ref=run._commit_delivery_baseline(),
            )
            diff_path = _record_diff_patch_block(run, captured)
        # REA-3: compose the v1 evidence bundle (evidence.json +
        # evidence.md) from meta + events + metrics. Falls back to
        # the REA-0 placeholder if collection fails so the run dir
        # always carries an evidence.json after finalize.
        from pipeline.evidence import write_bundle_or_placeholder
        evidence_path = write_bundle_or_placeholder(
            run.output_dir,
            run_id=run.session_ts,
            status=run.session["status"],
        )
        has_api_equivalent_cost = config.accounting_enabled() and any(
            p.cost_usd_equivalent is not None for p in run._metrics.phases
        )

    # 7) Checkpoint final status — reads post-delivery session["status"].
    if run._ckpt:
        final_ckpt_status = (
            PipelineStatus.HALTED
            if run.session.get("status") == "halted"
            else
            PipelineStatus.AWAITING_HUMAN_REVIEW
            if run.session.get("status") == "awaiting_human_review"
            else PipelineStatus.DONE
        )
        run._ckpt.set_status(final_ckpt_status)
        run._ckpt.close()

    # 8) Mirror to project (best-effort).
    mirrored: list[Path] = []
    mirror_error: str | None = None
    if run.output_dir is not None:
        try:
            from pipeline.engine.artifact_mirror import mirror_to_projects
            app_cfg = config.AppConfig.load()
            mirrored = mirror_to_projects(
                run.output_dir,
                {run.project_path.name: run.project_path},
                app_cfg.artifacts,
            )
        except Exception as exc:
            # Mirror — best-effort, never fail the pipeline.
            mirror_error = str(exc)

    # 9) Worktree teardown (ADR 0033) + ContextVar resets.
    # ADR 0131: plugin-declared external-resource teardown runs before an
    # isolated git worktree is released. Off-mode contexts are canonical
    # checkouts, so cleanup/presentation is not applicable to them.
    worktree_teardown_message = _teardown_worktree_for_finalization(run)
    if run._worktree_cvar_token is not None:
        from pipeline.engine.worktree import reset_active_worktree_checkout
        reset_active_worktree_checkout(run._worktree_cvar_token)
    # ADR 0034: release the sandbox-policy ContextVar so the frozen
    # policy from this run does not leak into a sibling run on the
    # same thread (important for tests).
    if getattr(run, "_sandbox_cvar_token", None) is not None:
        from pipeline.sandbox import reset_active_sandbox_policy
        reset_active_sandbox_policy(run._sandbox_cvar_token)

    # Verification-gate DONE block (T2): the per-hook official-gate timeline —
    # autorun + scheduled routing-decision events over the durable evidence
    # (auto-run trail + gate-event trail + read-only residual reclassification).
    # Omitted (empty tuple) when the model is None (no contract and no
    # receipt/trail/gate-events) or there is no run dir to read.
    verification_gate_lines: tuple[str, ...] = ()
    if run.output_dir is not None:
        from pipeline.project.verification_timeline import (
            build_verification_timeline,
            render_verification_gate_done_block,
        )
        gate_timeline = build_verification_timeline(
            run_dir=run.output_dir,
            extras=run.state.extras,
            session=run.session,
        )
        if gate_timeline is not None:
            verification_gate_lines = render_verification_gate_done_block(
                gate_timeline,
            )
            # APPROVED + only warn/suggest gaps reads as 'approved + verification
            # warning' (shipping allowed by policy), not a block or contradiction
            # (T5, ADR 0097). A require residual is NOT softened — it stays a
            # blocker and this framing line is withheld.
            if verification_gate_lines and _approved_with_only_verification_warnings(
                run.session, gate_timeline,
            ):
                head, *rest = verification_gate_lines
                verification_gate_lines = (
                    head,
                    "  approved + verification warning "
                    "(shipping allowed by policy)",
                    *rest,
                )

    return FinalizationResult(
        status=run.session["status"],
        halt_reason=run.session.get("halt_reason"),
        summary_text=summary_text,
        correction_route_line=correction_route_line,
        correction_route_halted=correction_route_halted,
        session_path=session_path,
        metrics_path=metrics_path,
        diff_path=diff_path,
        evidence_path=evidence_path,
        mirrored_artifacts=mirrored,
        context_summary_text=context_summary_text,
        has_api_equivalent_cost=has_api_equivalent_cost,
        is_subpipeline=is_subpipeline,
        mirror_error=mirror_error,
        worktree_teardown_message=worktree_teardown_message,
        run_id=str(getattr(run, "session_ts", "")).strip() or None,
        task_summary=_render_task_summary(getattr(run, "task", None)),
        no_change_outcome=run.session.get("no_change_outcome")
        if isinstance(run.session.get("no_change_outcome"), Mapping)
        else None,
        evidence_summary_lines=_render_evidence_summary(
            run.session,
            metrics_dict,
            terminal_delivery=terminal_delivery,
        ),
        roi_summary_line=_render_roi_summary(
            run.session,
            metrics_dict,
            include_accounting=has_api_equivalent_cost,
        ),
        usage_breakdown_lines=_phase_usage_rows(
            metrics_dict,
            include_accounting=has_api_equivalent_cost,
        ),
        subtask_usage_lines=_subtask_usage_rows(
            metrics_dict,
            include_accounting=has_api_equivalent_cost,
        ),
        # Advice cost is gated on accounting availability ALONE — not phase
        # cost presence. The advice digest can carry a real
        # ``usage.cost_usd_equivalent`` even when no phase reported an
        # cost reference (e.g. an operator/CI stop with no following
        # phase), so reusing ``has_api_equivalent_cost`` (phase-cost driven)
        # would wrongly suppress it. The digest-cost-presence check inside
        # ``_format_agent_advice_block`` still ensures cost renders iff the
        # advice digest actually carried one — never invented.
        ci_agent_advice_summary=_render_agent_advice_summary(
            run.output_dir,
            run.session,
            run.state.extras,
            include_accounting=config.accounting_enabled(),
        ),
        verification_gate_lines=verification_gate_lines,
        release_outcome=_release_outcome_token(
            run.session.get("phases")
            if isinstance(run.session.get("phases"), Mapping)
            else {}
        ),
        # T2: a primary that shipped while a declared companion repo stayed
        # dirty. Built from the durable ``multi_project_delivery`` block
        # ``run.py`` propagated from the T1 disclosure — no git re-scan here.
        companion_caveat=build_companion_delivery_caveat(run.session),
        # F2: typed carry of the compact scope-expansion lines, read from the
        # same canonical session projection the Evidence block renders from.
        scope_expansion_lines=_scope_expansion_summary_lines(
            run.session.get("phases")
            if isinstance(run.session.get("phases"), Mapping)
            else {}
        ),
        # Compact 'Delivery: ...' destination line, read from the terminal
        # ``commit_delivery`` audit record; empty when the run carries none.
        delivery_summary_lines=render_delivery_destination_lines(run.session),
        terminal_delivery=terminal_delivery,
    )


# ── terminal-wrapper path ────────────────────────────────────────────────


def finalize_with_terminal_output(ctx: FinalizationContext) -> FinalizationResult:
    """CLI-equivalent finalization: silent service + terminal banners.

    Calls :func:`finalize_project_run` first to run all side-effects
    silently, then prints the legacy DONE banner + success chips +
    Session/Usage/Progress lines + mirror notice + worktree-teardown
    line. All terminal text is derived from the silent service's
    ``FinalizationResult`` so the two paths cannot drift on
    semantics.

    The banner prints AFTER the side-effects so its content reflects
    the post-delivery status. Legacy ``finalize()`` printed the
    banner before ``save_session``, but every visible field (status,
    halt_reason, summary_text) is identical because the values are
    finalised by the silent service's status-resolution + delivery
    step before either path renders.
    """
    result = finalize_project_run(ctx)
    run = ctx.run

    # DONE banner — print only (no log_phase / event re-emit).
    # The silent service already emitted the canonical
    # ``phase.start("DONE")`` + ``phase.end("DONE")`` + ``run.end``
    # events via :func:`log_phase`. Calling :func:`banner` here would
    # double-emit a second ``phase.start("DONE")``, which drifts the
    # event-order snapshot in
    # ``tests/unit/pipeline/runtime/test_snapshot_session_parity.py``.
    # Render the colored header directly instead — the banner's stdout
    # half is the only thing this wrapper owns.
    from core.io.transcript import render_phase_header
    if result.is_subpipeline:
        print(render_phase_header(
            "DONE",
            f"Sub-pipeline [{run.project_alias}] complete  "
            f"— returning to cross-run {run.parent_run_id}",
            color=C.BLUE,
        ))
    elif result.status == "halted":
        # A run can flip ``done`` → ``halted`` inside delivery
        # (``_run_commit_delivery``) or carry a quality-gate halt that
        # was not short-circuited before ``finalize()``. Either way the
        # green "Pipeline complete" header would be a lie — render an
        # honest halt header keyed off the reason. Phase-handoff
        # pauses never reach this wrapper (they return before
        # ``finalize()`` in ``profile_dispatch``), so the only reasons
        # seen here are delivery-driven or quality-gate halts.
        label, header_color = _halt_banner(result.halt_reason)
        print(render_phase_header("HALTED", label, color=header_color))
    elif (
        result.terminal_delivery.disposition
        is TerminalDeliveryDisposition.DELIVERED_BY_OPERATOR_OVERRIDE
    ):
        print(render_phase_header(
            "DELIVERED BY OPERATOR OVERRIDE",
            "Release rejected; delivery was explicitly completed by operator override",
            color=C.YELLOW,
        ))
    elif (
        result.release_outcome == "rejected"
        and result.terminal_delivery.disposition
        is TerminalDeliveryDisposition.NOT_DELIVERED
    ):
        print(render_phase_header(
            "DELIVERY BLOCKED",
            "Release rejected — pipeline ran, delivery did not happen",
            color=C.YELLOW,
        ))
    elif result.release_outcome == "rejected":
        # A rejected release is never green.  ``UNKNOWN`` deliberately does
        # not claim delivery did not happen; a delivered record without the
        # corroborating override marker is likewise not a clean success.
        print(render_phase_header(
            "DELIVERY BLOCKED",
            "Release rejected — delivery disposition requires attention",
            color=C.YELLOW,
        ))
    elif result.companion_caveat is not None:
        # The primary delivered, but a declared companion repo is still
        # uncommitted (T2). A green "Pipeline complete" would read as a finished
        # multi-repo delivery — render an honest amber header instead. The phase
        # chips below stay on their genuine outcomes; only the headline changes,
        # and the caveat lines spell out the actionable next step.
        print(render_phase_header(
            "DONE — COMPANION DELIVERY INCOMPLETE",
            "Primary delivered; companion repo(s) still uncommitted",
            color=C.YELLOW,
        ))
    else:
        print(render_phase_header(
            "DONE", "Pipeline complete", color=C.GREEN,
        ))
    _done_line(
        result.summary_text,
        color=_summary_line_color(result.summary_text),
        bold=True,
    )
    # Companion delivery caveat (T2): rendered prominently right under the
    # summary, amber, for both the DONE and HALTED tails — the primary shipped
    # but a declared companion repo is still uncommitted. Absent (None) for a
    # clean single-repo run or a fully-delivered multi-repo run.
    if result.companion_caveat is not None:
        for index, line in enumerate(result.companion_caveat.lines):
            icon = "⚠" if index == 0 else "↳"
            _done_line(line, color=C.YELLOW, icon=icon, icon_color=C.YELLOW)
    # Correction-route line (ADR 0086): rendered for both the DONE and
    # HALTED tails (both flow through here after the banner). Amber for the
    # halting route, neutral for shortcut skips — never green, so a skipped
    # phase never reads as a real approval. Absent for non-correction runs.
    if result.correction_route_line:
        if result.correction_route_halted:
            _done_line(
                result.correction_route_line,
                color=C.YELLOW, icon="⚠", icon_color=C.YELLOW,
            )
        else:
            _done_line(
                result.correction_route_line,
                color=C.CYAN, icon="•", icon_color=C.CYAN,
            )
    # CI handoff-advisor block (T4): rendered for both DONE and HALTED tails
    # from the real ci_agent aggregate. Absent when no ci_agent retry ran.
    if result.ci_agent_advice_summary:
        for index, line in enumerate(result.ci_agent_advice_summary.splitlines()):
            if index == 0:
                _done_line(line, color=C.CYAN, icon="•", icon_color=C.CYAN)
            else:
                _done_line(line, color=C.GREY, icon="↳", icon_color=C.GREY)
    if result.run_id:
        _done_line(f"Run:     {result.run_id}", color=C.CYAN, icon="•", icon_color=C.CYAN)
    if result.task_summary:
        _done_line(f"Task:    {result.task_summary}", color=C.WHITE, icon="•", icon_color=C.CYAN)
    if result.no_change_outcome is not None:
        _done_line(
            "Outcome: no file changes (verification-only run)",
            color=C.CYAN,
            icon="•",
            icon_color=C.CYAN,
        )
    for line in result.evidence_summary_lines:
        _done_line(line, color=C.MAGENTA, icon="•", icon_color=C.MAGENTA)

    # Delivery destination line: where the diff actually landed (pushed delivery
    # branch + PR / checkout commit / applied uncommitted / skipped / not
    # delivered). Neutral cyan — a factual destination report, not a verdict.
    # Empty tuple -> nothing (no terminal delivery record for this run).
    for line in result.delivery_summary_lines:
        _done_line(line, color=C.CYAN, icon="•", icon_color=C.CYAN)

    # Verification gates block (T3): the official auto-run / gate_rerun outcome,
    # sourced from durable receipt evidence. Neutral cyan header + grey detail
    # rows — never success-green (a neutral pass/fresh/stale report, not a
    # verdict). Empty tuple -> nothing (omission, no misleading empty block).
    for index, line in enumerate(result.verification_gate_lines):
        if index == 0:
            _done_line(line, color=C.CYAN, icon="•", icon_color=C.CYAN)
        else:
            _done_line(line, color=C.GREY, icon="↳", icon_color=C.GREY)

    if run.output_dir:
        if result.session_path is not None:
            _done_line(f"Session: {result.session_path}", color=C.GREY, icon="↳", icon_color=C.GREY)
        if result.metrics_path is not None:
            _done_line(f"Usage:   {run._metrics.summary_line()}", color=C.CYAN, icon="•", icon_color=C.CYAN)
            if result.has_api_equivalent_cost:
                print(
                    f"           {paint('↳ ' + ACCOUNTING_REFERENCE_NOTE, C.GREY)}"
                )
            if result.roi_summary_line:
                _done_line(result.roi_summary_line, color=C.YELLOW, icon="•", icon_color=C.YELLOW)
            for index, line in enumerate(result.usage_breakdown_lines):
                if index == 0:
                    _done_line(line, color=C.CYAN, icon="•", icon_color=C.CYAN)
                else:
                    _done_line(line, color=C.GREY, icon="↳", icon_color=C.GREY)
            # Per-subtask breakdown: analytical evidence, so the header is
            # cyan and rows are grey ↳ — never the success-green verdict color.
            for index, line in enumerate(result.subtask_usage_lines):
                if index == 0:
                    _done_line(line, color=C.CYAN, icon="•", icon_color=C.CYAN)
                else:
                    _done_line(line, color=C.GREY, icon="↳", icon_color=C.GREY)
        if result.context_summary_text:
            _done_line(result.context_summary_text, color=C.GREY, icon="↳", icon_color=C.GREY)
        _done_line(f"Progress log: {_logging._progress_log}", color=C.GREY, icon="↳", icon_color=C.GREY)

    if result.mirror_error is not None:
        print(f"  ! mirror skipped: {result.mirror_error}")
    elif result.mirrored_artifacts:
        _done_line(
            f"Mirrored {len(result.mirrored_artifacts)} artifacts to project",
            color=C.CYAN,
            icon="•",
            icon_color=C.CYAN,
        )

    if result.worktree_teardown_message is not None:
        _done_line(
            f"Worktree: {result.worktree_teardown_message}",
            color=C.GREY,
            icon="↳",
            icon_color=C.GREY,
        )

    return result
