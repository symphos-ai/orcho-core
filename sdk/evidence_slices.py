"""Typed inspection slices over the run-evidence surface.

The full ``collect_evidence`` bundle is exhaustive — it carries every
phase, gate, command, artifact, and error rollup in a single dict.
That's the audit-grade record. For a control-loop client (MCP, Web,
agent UIs) the relevant question is usually narrower:

    What does the plan say? Which findings blocked the run? Which
    commands did the pipeline shell out to? What artifacts landed?
    Why did it halt?

This module provides typed projections over those questions. Each
slice function returns a small dataclass list so callers can render
without scanning logs.

Severity filter (REA-4.3): findings carry a severity in
``{"P0", "P1", "P2", "P3"}`` (P0 = critical, P3 = informational).
``list_findings(severity_min="P1")`` returns only P0 + P1; the
filter is *minimum* in the criticality sense, not lexicographic.

Cross-run linkage: ``list_sub_runs`` returns the alias list for
cross-project parents.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.observability.events import Event, read_all
from pipeline.project.handoff_advice_evidence import collect_handoff_advice
from pipeline.run_state.provider_runtime import PROVIDER_RUNTIME_FAILURE_KIND
from pipeline.run_state.setup_failure import (
    merged_halt_reason,
    merged_status,
)
from pipeline.run_state.stalled_command import (
    STALL_RECOVERY_VERBS,
    STALLED_COMMAND_FAILURE_KIND,
)
from sdk.evidence import collect_evidence
from sdk.runs import _CWD_DEFAULT, find_run, load_meta

_SeverityLiteral = Literal["P0", "P1", "P2", "P3"]
_SEVERITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

#: Phases that emit findings (validate_plan, review_changes,
#: final_acceptance, compliance_check — ADR 0022 vocabulary).
#: Pinned here so adding a new reviewer phase upstream requires an
#: explicit update — not a silent omission.
FINDING_BEARING_PHASES: tuple[str, ...] = (
    "validate_plan",
    "review_changes",
    "final_acceptance",
    "compliance_check",
    # ADR 0025 Phase 3: cross runner's system release gate. Persisted
    # as a singleton dict (same shape as project final_acceptance);
    # ``_phase_attempts`` normalizes both shapes.
    "cross_final_acceptance",
)


# ─────────────────────────────────────────────────────────────────────────────
# Typed slices
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Finding:
    """One reviewer finding flattened from a phase attempt."""

    id: str
    severity: str               # "P0" | "P1" | "P2" | "P3"
    title: str
    body: str
    required_fix: str | None
    file: str | None
    line: int | None
    phase: str                  # "validate_plan" / "review" / "final_acceptance" / ...
    attempt: int                # which attempt within the phase emitted this


@dataclass(frozen=True, slots=True)
class PlanSummary:
    """Compact plan projection — short enough for an LLM context window."""

    source: str                 # "json" | "markdown" | "absent"
    short_summary: str
    planning_context: str
    subtask_count: int
    has_contract: bool
    goal: str | None
    acceptance_criteria: tuple[str, ...]
    owned_files: tuple[str, ...]
    commands_to_run: tuple[str, ...]
    risks: tuple[str, ...]
    review_focus: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """One command the pipeline shelled out to."""

    argv_summary: str
    cwd: str
    exit_code: int | None
    duration_s: float
    outcome: str                # "success" | "failure" | …
    source: str = "event"
    identity_digest: str | None = None
    phase: str | None = None
    state: str | None = None
    executable: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    artifact_path: str | None = None
    degraded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One file the run wrote to disk that's not events.jsonl/meta.json."""

    path: str
    kind: str                   # "plan" | "review" | "build_diff" | …
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RecoveryReplacement:
    """One provider-neutral runtime/model replacement candidate (ADR 0101).

    A configured ``(runtime, model)`` pair the operator may switch the failed
    phase to. Each maps to one ``orcho_run_resume`` replace action with args
    ``{run_id, runtime_override: {phase, runtime, model}}``.
    """

    runtime: str
    model: str


@dataclass(frozen=True, slots=True)
class ProviderAccessRecovery:
    """Typed projection of a terminal provider-access failure's recovery record.

    ADR 0101. Built from ``meta.failure`` when ``failure_kind ==
    'provider_access'``. ``retry`` and ``halt`` are always-available recovery
    options; ``halt`` is **meta-only** — it is never projected as an executable
    SDK ``next_actions`` entry (the run is already terminal and there is no
    halt tool), it stays a durable recovery option. ``replacements`` carries
    the configured runtime/model candidates for the failed phase; each maps to
    one ``orcho_run_resume`` replace action.
    """

    failure_kind: str
    recoverable: bool
    recommended_action: str
    failed_phase: str
    runtime: str
    model: str
    replacements: tuple[RecoveryReplacement, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderRuntimeFailure:
    """Typed projection of a recoverable provider/runtime failure (ADR 0118).

    Built from ``meta.failure`` when ``failure_kind == 'provider_runtime'`` — a
    transient provider/runtime condition (rate-limit / connection / timeout /
    local resource) that escalated past the retry budget. Unlike
    :class:`ProviderAccessRecovery` it carries no runtime/model replacement set:
    the safe next action is to resume or retry the same phase once the
    condition clears (``recommended_action == 'resume_or_retry_phase'``), so
    ``recoverable`` is ``True``. ``provider_message`` is the sanitized signature
    (never raw JSONL / secrets / prompts) and is ``""`` when none was captured.
    """

    failure_kind: str
    recoverable: bool
    recommended_action: str
    failed_phase: str
    runtime: str
    model: str
    provider_message: str = ""


@dataclass(frozen=True, slots=True)
class ErrorsAndHalt:
    """Errors collected during the run + halt reason if applicable.

    ``recovery`` is the additive typed projection of a terminal
    provider-access failure's durable recovery record (ADR 0101) — ``None``
    for every other run. ``provider_runtime`` is the parallel additive
    projection of a recoverable provider/runtime failure (ADR 0118) — ``None``
    for every other run. The two are mutually exclusive (distinct
    ``failure_kind`` values). The same fields also remain inline in the
    ``errors`` rollup dicts; these typed fields promote them for clients that
    drive the recovery / resume UX without re-parsing the error dict.
    """

    status: str                 # "done" | "failed" | "halted" | …
    errors: tuple[dict[str, Any], ...]
    halt_reason: str | None
    halted_at: str | None
    error_summary: str | None
    recovery: ProviderAccessRecovery | None = None
    provider_runtime: ProviderRuntimeFailure | None = None


@dataclass(frozen=True, slots=True)
class StalledCommandRecovery:
    """Typed projection of one stalled-command diagnostic.

    Covers both stall sources behind a single shape, discriminated by
    ``source`` / ``terminal``:

    * ``source='terminal'`` (``terminal=True``) — built from ``meta.failure``
      when ``failure_kind == 'stalled_command'`` (the idle-timeout escalation
      that failed the run).
    * ``source='live_non_terminal'`` (``terminal=False``) — built from an
      emitted non-terminal ``agent.command_stalled`` event, observable while
      the phase is still running (the live event-backed source — NOT the
      after-phase finalization snapshot).

    ``recovery_actions`` is the bounded recovery verb set
    (:data:`pipeline.run_state.stalled_command.STALL_RECOVERY_VERBS`), the same
    list the terminal failure record and event payload agree on.
    """

    source: str                 # "terminal" | "live_non_terminal"
    terminal: bool
    phase: str
    reason: str
    elapsed_s: float
    recovery_actions: tuple[str, ...]
    command_preview: str | None = None
    output_tail: str | None = None
    process_group: int | None = None


@dataclass(frozen=True, slots=True)
class SubRunLink:
    """Cross-run child alias linkage for cross-project orchestration."""

    name: str                   # alias e.g. "unity"
    status: str | None
    run_dir: str                # absolute path to <run_dir>/<alias>/


@dataclass(frozen=True, slots=True)
class CriterionReport:
    """One developer claim against a subtask done-criterion (P7 / ADR 0068).

    The ``met`` flag is the developer's explicit self-attestation; ``evidence``
    is a one-sentence claim, not proof. Whether the claim is TRUE is the job of
    the reviewer / final_acceptance / test gates, not this projection.
    """

    index: int                  # 1-based position in the subtask's done_criteria
    criterion: str
    met: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class SubtaskReceipt:
    """Terminal delivery record for one planned subtask (subtask_dag).

    ``state`` is ``"done" | "incomplete" | "failed" | "skipped"`` (ADR 0067 +
    ADR 0068). ``incomplete`` means the invocation succeeded but the typed
    done-criteria self-attestation was missing / malformed / mismatched /
    not-all-met — distinct from a hard ``failed`` execution error.
    ``criteria_report`` / ``attestation_summary`` / ``attestation_error`` carry
    the P7 attestation; they are empty / ``None`` for criteria-less subtasks
    and for runs predating P7.
    """

    subtask_id: str
    state: str
    runtime: str
    model: str
    skill: str | None
    depends_on: tuple[str, ...]
    done_criteria: tuple[str, ...]
    duration: float
    error: str | None
    criteria_report: tuple[CriterionReport, ...]
    attestation_summary: str | None
    attestation_error: str | None
    attestation_repaired: bool = False


@dataclass(frozen=True, slots=True)
class HandoffAdviceUsage:
    """Aggregated advisor usage across a run's advice calls.

    Each field mirrors the corresponding key in ``collect_handoff_advice``'s
    ``summary['usage']`` block and is ``None`` when no call carried that signal —
    never a fabricated zero (an absent cost is meaningful: cost unknown).
    """

    tokens_in: int | None
    tokens_out: int | None
    tokens_cached: int | None
    duration_s: float | None
    cost_usd_equivalent: float | None


@dataclass(frozen=True, slots=True)
class HandoffAdviceCall:
    """One Stage 0/1 advisor invocation, projected from a durable advice artifact.

    The fields are a 1:1 typed view of one entry in ``collect_handoff_advice``'s
    ``calls`` list; the outcome classification (``resolved`` / ``repeated`` /
    ``outcome``) is the normalizer's, copied here verbatim — this projection adds
    no policy. ``advice_artifact`` is the run-relative path to the advice JSON;
    the usage fields (``tokens_*`` / ``duration_s`` / ``cost_usd_equivalent`` /
    ``model``) are ``None`` when the artifact carried no accounting.
    """

    handoff_id: str
    phase: str
    advice_artifact: str
    trigger: str
    verdict: str
    feedback_source: str | None
    recommended_action: str
    applied_action: str | None
    confidence: str
    finding_fingerprint: str
    resolved: bool | None
    repeated: bool
    outcome: str
    severity_counts: dict[str, int]
    tokens_in: int | None
    tokens_out: int | None
    tokens_cached: int | None
    duration_s: float | None
    cost_usd_equivalent: float | None
    model: str | None


@dataclass(frozen=True, slots=True)
class HandoffAdviceSummary:
    """Run-level rollup over the advice calls (mirrors ``summary``)."""

    calls: int
    applied_retries: int
    resolved_retries: int
    repeated: int
    stopped: int
    unknown: int
    usage: HandoffAdviceUsage | None


@dataclass(frozen=True, slots=True)
class HandoffAdviceEvidence:
    """Typed projection of the Stage 0/1 handoff-advice evidence surface."""

    calls: tuple[HandoffAdviceCall, ...]
    summary: HandoffAdviceSummary


# ─────────────────────────────────────────────────────────────────────────────
# Slice functions
# ─────────────────────────────────────────────────────────────────────────────


def list_findings(
    run_id: str | None = None,
    *,
    severity_min: _SeverityLiteral | None = None,
    phases: tuple[str, ...] | None = None,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[Finding]:
    """Flatten findings from all reviewer-phase attempts into one list.

    ``severity_min`` filters by criticality: ``"P0"`` returns only P0,
    ``"P1"`` returns P0+P1, etc. ``None`` returns all severities.

    ``phases`` selects which finding-bearing phases to include. ``None``
    defaults to all four (``validate_plan``, ``review``, ``final_acceptance``,
    ``compliance_check``).
    """
    if severity_min is not None and severity_min not in _SEVERITY_RANK:
        raise ValueError(
            f"severity_min must be one of {tuple(_SEVERITY_RANK)!r}, "
            f"got {severity_min!r}"
        )
    chosen = tuple(phases) if phases is not None else FINDING_BEARING_PHASES
    threshold = _SEVERITY_RANK.get(severity_min) if severity_min else None

    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    meta = load_meta(ref.run_dir)
    phases_meta = meta.get("phases") or {}

    out: list[Finding] = []
    for phase_name in chosen:
        attempts = _phase_attempts(phases_meta.get(phase_name))
        for attempt_idx, attempt in enumerate(attempts, start=1):
            attempt_num = int(attempt.get("attempt") or attempt_idx)
            findings = attempt.get("findings") or []
            if not isinstance(findings, list):
                continue
            for f in findings:
                if not isinstance(f, dict):
                    continue
                sev = str(f.get("severity") or "P3")
                if threshold is not None:
                    rank = _SEVERITY_RANK.get(sev, 99)
                    if rank > threshold:
                        continue
                out.append(
                    Finding(
                        id=str(f.get("id") or ""),
                        severity=sev,
                        title=str(f.get("title") or ""),
                        body=str(f.get("body") or ""),
                        required_fix=_optional_str(f.get("required_fix")),
                        file=_optional_str(f.get("file")),
                        line=_optional_int(f.get("line")),
                        phase=phase_name,
                        attempt=attempt_num,
                    )
                )
    return out


def get_plan_summary(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> PlanSummary:
    """Compact plan projection drawn from the evidence bundle's plan record."""
    bundle = collect_evidence(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )
    plan = bundle.body.get("plan") or {}
    return PlanSummary(
        source=str(plan.get("source") or "absent"),
        short_summary=str(plan.get("short_summary") or ""),
        planning_context=str(plan.get("planning_context") or ""),
        subtask_count=int(plan.get("subtask_count") or 0),
        has_contract=bool(plan.get("has_contract")),
        goal=_optional_str(plan.get("goal")),
        acceptance_criteria=tuple(plan.get("acceptance_criteria") or ()),
        owned_files=tuple(plan.get("owned_files") or ()),
        commands_to_run=tuple(plan.get("commands_to_run") or ()),
        risks=tuple(plan.get("risks") or ()),
        review_focus=tuple(plan.get("review_focus") or ()),
    )


def list_commands(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[CommandRecord]:
    """Commands the pipeline executed (argv summary + outcome)."""
    bundle = collect_evidence(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )
    cmds = bundle.body.get("commands") or []
    return [
        CommandRecord(
            argv_summary=str(c.get("argv_summary") or ""),
            cwd=str(c.get("cwd") or ""),
            exit_code=_optional_int(c.get("exit_code")),
            duration_s=float(c.get("duration_s") or 0.0),
            outcome=str(c.get("outcome") or ""),
            source=str(c.get("source") or "event"),
            identity_digest=_optional_str(c.get("identity_digest")),
            phase=_optional_str(c.get("phase")),
            state=_optional_str(c.get("state")),
            executable=_optional_str(c.get("executable")),
            started_at=_optional_str(c.get("started_at")),
            finished_at=_optional_str(c.get("finished_at")),
            artifact_path=_optional_str(c.get("artifact_path")),
            degraded_reason=_optional_str(c.get("degraded_reason")),
        )
        for c in cmds if isinstance(c, dict)
    ]


def list_artifacts(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[ArtifactRecord]:
    """Files the run wrote (plan, build diff, review artefacts, etc.)."""
    bundle = collect_evidence(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )
    arts = bundle.body.get("artifacts") or []
    return [
        ArtifactRecord(
            path=str(a.get("path") or ""),
            kind=str(a.get("kind") or ""),
            size_bytes=int(a.get("size_bytes") or 0),
        )
        for a in arts if isinstance(a, dict)
    ]


def get_errors_halt(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> ErrorsAndHalt:
    """Errors + halt reason in one typed projection.

    ``status`` / ``halt_reason`` come from the ADR 0104 merge rule
    (:func:`pipeline.run_state.setup_failure.merged_status` /
    :func:`~pipeline.run_state.setup_failure.merged_halt_reason`), which
    reconciles the pipeline-owned ``meta`` fields with the optional launcher
    state file: terminal ``meta`` wins, the launcher is consulted only for an
    empty/``running`` ``meta.status``, and a signal-reaped ``failed`` remaps to
    ``interrupted``. This keeps ``status``/``halt_reason`` consistent with
    ``load_status`` and the launcher integration on the same run dir.
    ``halted_at`` still comes from ``meta.json`` verbatim. ``errors`` comes
    from the evidence bundle's rollup (errors[] + error events), which now
    includes the synthesized ``setup_failed`` breadcrumb (ADR 0104) for an
    otherwise-silent setup/preflight death.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    meta = load_meta(ref.run_dir)
    bundle = collect_evidence(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )

    errors_raw = bundle.body.get("errors") or []
    errors = tuple(e for e in errors_raw if isinstance(e, dict))

    error_summary: str | None = None
    if errors:
        # First-error summary if not surfaced elsewhere — useful for
        # one-line error reporting in MCP clients.
        first = errors[0]
        title = first.get("title") or first.get("kind")
        if title:
            error_summary = str(title)

    merged = merged_status(meta, ref.run_dir)
    return ErrorsAndHalt(
        status=str(merged or bundle.body.get("status") or ""),
        errors=errors,
        halt_reason=_optional_str(merged_halt_reason(meta, ref.run_dir)),
        halted_at=_optional_str(meta.get("halted_at")),
        error_summary=error_summary,
        recovery=_provider_access_recovery_from_meta(meta),
        provider_runtime=_provider_runtime_failure_from_meta(meta),
    )


def _provider_access_recovery_from_meta(
    meta: Mapping[str, Any],
) -> ProviderAccessRecovery | None:
    """Project ``meta.failure`` into a typed recovery record (ADR 0101).

    Returns ``None`` unless the run died on a provider-access failure. Reads
    only ``meta`` — the candidates were persisted into
    ``meta.failure.recovery_actions`` by the run; ``halt`` stays a meta-only
    recovery option and is not promoted into ``replacements``.
    """
    failure = meta.get("failure")
    if not isinstance(failure, Mapping):
        return None
    if failure.get("failure_kind") != "provider_access":
        return None
    raw_actions = failure.get("recovery_actions")
    replacements: tuple[RecoveryReplacement, ...] = ()
    if isinstance(raw_actions, list):
        replacements = tuple(
            RecoveryReplacement(
                runtime=str(entry["runtime"]), model=str(entry["model"]),
            )
            for entry in raw_actions
            if isinstance(entry, Mapping)
            and entry.get("action") == "replace"
            and entry.get("runtime")
            and entry.get("model")
        )
    return ProviderAccessRecovery(
        failure_kind=str(failure.get("failure_kind") or ""),
        recoverable=bool(failure.get("recoverable", False)),
        recommended_action=str(failure.get("recommended_action") or ""),
        failed_phase=str(failure.get("failed_phase") or ""),
        runtime=str(failure.get("runtime") or ""),
        model=str(failure.get("model") or ""),
        replacements=replacements,
    )


def _provider_runtime_failure_from_meta(
    meta: Mapping[str, Any],
) -> ProviderRuntimeFailure | None:
    """Project ``meta.failure`` into a typed provider/runtime record (ADR 0118).

    Returns ``None`` unless the run died on a recoverable provider/runtime
    failure (``failure_kind == 'provider_runtime'``). Reads only ``meta`` — the
    record was written by the run's terminal failure handler. Distinct from
    :func:`_provider_access_recovery_from_meta`: the two ``failure_kind`` values
    never overlap, so exactly one of them is non-``None`` on any given run.
    ``provider_message`` is the sanitized signature; absent → ``""``.
    """
    failure = meta.get("failure")
    if not isinstance(failure, Mapping):
        return None
    if failure.get("failure_kind") != PROVIDER_RUNTIME_FAILURE_KIND:
        return None
    return ProviderRuntimeFailure(
        failure_kind=str(failure.get("failure_kind") or ""),
        recoverable=bool(failure.get("recoverable", False)),
        recommended_action=str(failure.get("recommended_action") or ""),
        failed_phase=str(failure.get("failed_phase") or ""),
        runtime=str(failure.get("runtime") or ""),
        model=str(failure.get("model") or ""),
        provider_message=str(failure.get("provider_message") or ""),
    )


_AGENT_COMMAND_STALLED_KIND = "agent.command_stalled"


def _recovery_verbs_from_meta(raw_actions: Any) -> tuple[str, ...]:
    """Recover the verb tuple from a ``recovery_actions`` list of dicts.

    Falls back to the canonical :data:`STALL_RECOVERY_VERBS` when the durable
    list is missing / malformed so the slice always reports the consistent set.
    """
    if isinstance(raw_actions, list):
        verbs = tuple(
            str(e["action"])
            for e in raw_actions
            if isinstance(e, Mapping) and e.get("action")
        )
        if verbs:
            return verbs
    return tuple(STALL_RECOVERY_VERBS)


def active_stall_diagnostics(
    run_dir: Path | str | None = None,
    *,
    events: list[Event] | None = None,
) -> list[StalledCommandRecovery]:
    """Live non-terminal stall diagnostics from the run event-store.

    Reads emitted ``agent.command_stalled`` events with ``terminal=False`` —
    the write-through diagnostics the stream monitor records the moment it
    detects a non-terminal stall (e.g. unsafe process polling). Because these
    are durable events, they are observable while the phase is still running,
    long before any after-phase bookkeeping. This is the live event-backed
    source of non-terminal observability; the optional finalization snapshot in
    ``meta`` is explicitly NOT read here.

    Pass either ``run_dir`` (the events file is read off disk) or a pre-read
    ``events`` list (tests / callers that already tailed the store). When both
    are given, ``events`` wins.
    """
    if events is None:
        if run_dir is None:
            return []
        events = read_all(Path(run_dir))

    out: list[StalledCommandRecovery] = []
    for evt in events:
        if evt.kind != _AGENT_COMMAND_STALLED_KIND:
            continue
        payload = evt.payload or {}
        if payload.get("terminal") is not False:
            # Only non-terminal events are "active" diagnostics; the terminal
            # escalation event belongs to the failed-run terminal source.
            continue
        out.append(
            StalledCommandRecovery(
                source="live_non_terminal",
                terminal=False,
                phase=str(payload.get("phase") or evt.phase or ""),
                reason=str(payload.get("reason") or ""),
                elapsed_s=float(payload.get("elapsed_s") or 0.0),
                recovery_actions=_recovery_verbs_from_meta(
                    payload.get("recovery_actions"),
                ),
                command_preview=_optional_str(payload.get("command_preview")),
                output_tail=_optional_str(payload.get("output_tail")),
                process_group=_optional_int(payload.get("process_group")),
            )
        )
    return out


def _terminal_stall_recovery_from_meta(
    meta: Mapping[str, Any],
) -> StalledCommandRecovery | None:
    """Project ``meta.failure`` into a terminal stall recovery, or ``None``.

    Returns ``None`` unless the run died on a stalled command. Reads only
    ``meta`` — the durable failure record was written by the run's terminal
    failure handler.
    """
    failure = meta.get("failure")
    if not isinstance(failure, Mapping):
        return None
    if failure.get("failure_kind") != STALLED_COMMAND_FAILURE_KIND:
        return None
    return StalledCommandRecovery(
        source="terminal",
        terminal=True,
        phase=str(failure.get("failed_phase") or failure.get("phase") or ""),
        reason=str(failure.get("reason") or ""),
        elapsed_s=float(failure.get("elapsed_s") or 0.0),
        recovery_actions=_recovery_verbs_from_meta(
            failure.get("recovery_actions"),
        ),
        command_preview=_optional_str(failure.get("command_preview")),
        output_tail=_optional_str(failure.get("output_tail")),
        process_group=_optional_int(failure.get("process_group")),
    )


def list_stall_recovery(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[StalledCommandRecovery]:
    """All stall-recovery diagnostics for a run — terminal and live non-terminal.

    The terminal record (if the run failed on a stalled command) comes first,
    followed by the live non-terminal diagnostics read from the event-store.
    Empty list for runs with no stall of either kind.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    meta = load_meta(ref.run_dir) or {}
    out: list[StalledCommandRecovery] = []
    terminal = _terminal_stall_recovery_from_meta(meta)
    if terminal is not None:
        out.append(terminal)
    out.extend(active_stall_diagnostics(ref.run_dir))
    return out


def list_subtask_receipts(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[SubtaskReceipt]:
    """Per-subtask delivery receipts for a ``subtask_dag`` run.

    Projects the evidence bundle's ``implementation_receipts`` (built from
    ``subtask.receipt`` events) into typed records, including the P7
    done-criteria attestation (``criteria_report`` / ``attestation_summary`` /
    ``attestation_error``). Returns an empty list for ``whole_plan`` runs and
    any run that recorded no subtask receipts.

    This is a self-attestation projection: ``met`` flags and ``state="done"``
    report what the developer CLAIMED, not independently verified truth — the
    reviewer / final_acceptance / test gates remain the verification layer.
    """
    bundle = collect_evidence(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )
    receipts = bundle.body.get("implementation_receipts") or []
    out: list[SubtaskReceipt] = []
    for r in receipts:
        if not isinstance(r, dict):
            continue
        out.append(
            SubtaskReceipt(
                subtask_id=str(r.get("subtask_id") or ""),
                state=str(r.get("state") or ""),
                runtime=str(r.get("runtime") or ""),
                model=str(r.get("model") or ""),
                skill=_optional_str(r.get("skill")),
                depends_on=tuple(
                    str(x) for x in (r.get("depends_on") or ()) if isinstance(x, str)
                ),
                done_criteria=tuple(
                    str(x) for x in (r.get("done_criteria") or ()) if isinstance(x, str)
                ),
                duration=float(r.get("duration") or 0.0),
                error=_optional_str(r.get("error")),
                criteria_report=_criteria_report(r.get("criteria_report")),
                attestation_summary=_optional_str(r.get("attestation_summary")),
                attestation_error=_optional_str(r.get("attestation_error")),
                attestation_repaired=bool(r.get("attestation_repaired")),
            )
        )
    return out


def list_handoff_advice(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> HandoffAdviceEvidence | None:
    """Typed projection of the Stage 0/1 handoff-advice evidence surface.

    Resolves the run the same way the other evidence slices do (``find_run`` +
    ``load_meta``), then wraps
    :func:`pipeline.project.handoff_advice_evidence.collect_handoff_advice` —
    the normalizer that folds ``phase_handoff_advice/`` artifacts,
    ``phase_handoff_decisions/`` provenance, and ``meta['phases']`` verdicts into
    ``{'calls', 'summary'}``. This is a thin typed wrapper: it copies the
    normalizer's values verbatim and adds no eligibility / parse / safety /
    classification policy of its own.

    Returns ``None`` when (and only when) ``collect_handoff_advice`` does — i.e.
    the Stage 0/1 artifact surface is entirely absent (no advice artifacts and no
    decision carrying advice provenance). A run with advice artifacts but no
    matching decision still yields an entry per artifact (``applied_action=None``,
    ``outcome='stopped'``).
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    meta = load_meta(ref.run_dir)
    raw = collect_handoff_advice(ref.run_dir, meta)
    if raw is None:
        return None

    calls = tuple(
        _handoff_advice_call(c)
        for c in (raw.get("calls") or [])
        if isinstance(c, dict)
    )
    return HandoffAdviceEvidence(
        calls=calls,
        summary=_handoff_advice_summary(raw.get("summary") or {}),
    )


def _handoff_advice_call(call: dict[str, Any]) -> HandoffAdviceCall:
    """Project one normalizer call dict into a typed record (values verbatim)."""
    severity = call.get("severity_counts")
    severity_counts: dict[str, int] = (
        {str(k): int(v) for k, v in severity.items()}
        if isinstance(severity, dict)
        else {}
    )
    return HandoffAdviceCall(
        handoff_id=str(call.get("handoff_id") or ""),
        phase=str(call.get("phase") or ""),
        advice_artifact=str(call.get("advice_artifact") or ""),
        trigger=str(call.get("trigger") or ""),
        verdict=str(call.get("verdict") or ""),
        feedback_source=_optional_str(call.get("feedback_source")),
        recommended_action=str(call.get("recommended_action") or ""),
        applied_action=_optional_str(call.get("applied_action")),
        confidence=str(call.get("confidence") or ""),
        finding_fingerprint=str(call.get("finding_fingerprint") or ""),
        resolved=_optional_bool(call.get("resolved")),
        repeated=bool(call.get("repeated")),
        outcome=str(call.get("outcome") or ""),
        severity_counts=severity_counts,
        tokens_in=_optional_int(call.get("tokens_in")),
        tokens_out=_optional_int(call.get("tokens_out")),
        tokens_cached=_optional_int(call.get("tokens_cached")),
        duration_s=_optional_float(call.get("duration_s")),
        cost_usd_equivalent=_optional_float(call.get("cost_usd_equivalent")),
        model=_optional_str(call.get("model")),
    )


def _handoff_advice_summary(summary: dict[str, Any]) -> HandoffAdviceSummary:
    """Project the normalizer summary dict into a typed record."""
    usage_raw = summary.get("usage")
    usage: HandoffAdviceUsage | None = None
    if isinstance(usage_raw, dict):
        usage = HandoffAdviceUsage(
            tokens_in=_optional_int(usage_raw.get("tokens_in")),
            tokens_out=_optional_int(usage_raw.get("tokens_out")),
            tokens_cached=_optional_int(usage_raw.get("tokens_cached")),
            duration_s=_optional_float(usage_raw.get("duration_s")),
            cost_usd_equivalent=_optional_float(usage_raw.get("cost_usd_equivalent")),
        )
    return HandoffAdviceSummary(
        calls=int(summary.get("calls") or 0),
        applied_retries=int(summary.get("applied_retries") or 0),
        resolved_retries=int(summary.get("resolved_retries") or 0),
        repeated=int(summary.get("repeated") or 0),
        stopped=int(summary.get("stopped") or 0),
        unknown=int(summary.get("unknown") or 0),
        usage=usage,
    )


def _criteria_report(value: Any) -> tuple[CriterionReport, ...]:
    """Project a receipt's ``criteria_report`` list into typed records.

    Tolerant: skips malformed entries rather than failing the whole slice, and
    coerces ``index`` defensively (a missing/odd index becomes 0 — the binding
    happened at gate time, this is read-only reporting).
    """
    if not isinstance(value, list):
        return ()
    out: list[CriterionReport] = []
    for c in value:
        if not isinstance(c, dict):
            continue
        out.append(
            CriterionReport(
                index=_optional_int(c.get("index")) or 0,
                criterion=str(c.get("criterion") or ""),
                met=bool(c.get("met")),
                evidence=str(c.get("evidence") or ""),
            )
        )
    return tuple(out)


#: Direct-child directories of a run_dir that are run-owned artifact
#: sinks, NOT per-alias cross sub-runs. A *defensive* exclusion layer
#: for :func:`list_sub_runs` — the primary classifier is positive
#: detection (run marker or declared parent alias, see
#: :func:`_looks_like_subrun`). This denylist names the known sinks
#: explicitly so the intent is greppable; positive detection already
#: excludes them (they carry no run marker and are not declared
#: aliases) and also excludes any *future* artifact dir that is never
#: added here.
_NON_SUBRUN_DIR_NAMES: frozenset[str] = frozenset({
    "commit_decisions",        # commit-delivery decision artifacts
    "worktrees",               # physical worktree checkouts root
    "phase_handoff_decisions", # operator phase-handoff decision artifacts
})

#: Files whose presence in a direct-child dir of run_dir marks it as a
#: pipeline run (a real sub-run), not an artifact sink. The
#: orchestrator writes ``output.log`` + ``events.jsonl`` at child
#: startup (before ``meta.json``), so an early-state sub-run is still
#: detected before it records its own status.
_SUBRUN_MARKER_FILES: tuple[str, ...] = (
    "meta.json", "output.log", "events.jsonl", "progress.log",
)


def _parent_alias_names(parent_meta: dict) -> set[str]:
    """Alias names a cross parent records, across both persisted shapes.

    Cross runs record their per-alias projects under
    ``session["phases"]["projects"]`` (dict keyed by alias) and/or a
    top-level ``projects`` map/list. A dir whose name matches a
    declared alias is a sub-run even when it is otherwise empty (the
    parent spawned the alias dir before the child wrote any marker).
    """
    names: set[str] = set()
    phases_projects = parent_meta.get("phases", {})
    if isinstance(phases_projects, dict):
        proj = phases_projects.get("projects")
        if isinstance(proj, dict):
            names.update(str(k) for k in proj)
    top = parent_meta.get("projects")
    if isinstance(top, dict):
        names.update(str(k) for k in top)
    elif isinstance(top, list):
        names.update(str(a) for a in top if isinstance(a, str))
    return names


def _looks_like_subrun(sd: Path, alias_names: set[str]) -> bool:
    """Positive sub-run detection: the dir is a declared parent alias
    OR carries a pipeline run marker. Artifact sinks
    (``commit_decisions`` etc.) satisfy neither and are excluded —
    including future artifact dirs never added to
    :data:`_NON_SUBRUN_DIR_NAMES`."""
    if sd.name in alias_names:
        return True
    return any((sd / marker).exists() for marker in _SUBRUN_MARKER_FILES)


def list_sub_runs(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> list[SubRunLink]:
    """Cross-run child links — one entry per sub-project alias.

    Empty list for single-project runs. The ``status`` field is
    ``None`` when the sub-run's own meta.json hasn't been written yet
    (e.g. parent spawned the alias dir but the child pipeline isn't
    far enough along to record state).

    A direct-child dir is classified as a sub-run by **positive
    detection** (:func:`_looks_like_subrun`): it is a declared parent
    alias OR carries a pipeline run marker (``meta.json`` /
    ``output.log`` / ``events.jsonl`` / ``progress.log``). Run-owned
    artifact sinks (``commit_decisions``, ``worktrees``,
    ``phase_handoff_decisions``) satisfy neither and are excluded —
    so are any *future* artifact dirs. Without this a single-project
    run reports a spurious ``commit_decisions`` sub-run once worktree
    isolation + commit-delivery are active (they create that dir),
    breaking the "empty list for single-project runs" contract. The
    :data:`_NON_SUBRUN_DIR_NAMES` denylist is a defensive second layer
    naming the known sinks explicitly.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    alias_names = _parent_alias_names(load_meta(ref.run_dir) or {})
    out: list[SubRunLink] = []
    for sd in sorted(
        p for p in ref.run_dir.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and p.name not in _NON_SUBRUN_DIR_NAMES
        and _looks_like_subrun(p, alias_names)
    ):
        sub_meta = load_meta(sd)
        out.append(
            SubRunLink(
                name=sd.name,
                status=_optional_str(sub_meta.get("status")) if sub_meta else None,
                run_dir=str(sd),
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _phase_attempts(value: Any) -> list[dict[str, Any]]:
    """Normalize a ``meta.phases[name]`` slot into a list of attempt
    dicts. Mirrors :func:`pipeline.evidence.collector._phase_attempts`
    so the SDK and the raw evidence bundle see the same set.

    Two persisted shapes today:

      * **Attempt-list** (``validate_plan``, ``review_changes``,
        ``compliance_check``): ``list[dict]`` of per-round attempts.
      * **Singleton-dict** (``final_acceptance``, since ADR 0025
        Phase 1): one ``dict`` — the closing gate runs once.
    """
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    """Preserve the tri-state ``resolved`` flag: ``None`` stays ``None``."""
    if value is None:
        return None
    return bool(value)
