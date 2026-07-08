"""pipeline.evidence.collector — Compose run evidence from on-disk state.

Single pure function :func:`collect_evidence` that reads the canonical
run-dir surface (``meta.json`` / ``events.jsonl`` / ``metrics.json``)
and folds it into the v1 evidence dict described in
:mod:`pipeline.evidence.schema`.

Design choices:

* **No new I/O channels.** The collector relies only on files orcho
  already writes. If a field needs data orcho doesn't yet record, the
  bundle leaves it empty rather than spawning a new collection path —
  REA-3 v1 stays narrow.
* **Derive rollups from events.** Per-phase / gate / command /
  artifact rollups come from the event spine, not from session shape.
  A consumer reading the bundle gets the same view as one tailing the
  live event stream.
* **Tolerant of missing files.** A run that halted before
  ``metrics.json`` was written still produces a bundle — the missing
  rollup degrades to zero values, never to a crash.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pipeline.evidence.finding_lifecycle import annotate_finding_lifecycle
from pipeline.evidence.schema import EVIDENCE_SCHEMA_VERSION
from pipeline.run_state.setup_failure import detect_setup_preflight_failure

#: Reviewer-phase names whose attempts carry ``findings`` records.
#: Aligned with :data:`sdk.evidence_slices.FINDING_BEARING_PHASES` so the
#: bundle and the typed SDK slice surface the same set.
_FINDING_BEARING_PHASES: tuple[str, ...] = (
    "validate_plan",
    "review_changes",
    "final_acceptance",
    "compliance_check",
    "cross_final_acceptance",
)

#: Phases that carry release-gate signal (ADR 0025). Each persisted
#: attempt may carry ``ship_ready`` / ``release_blockers`` /
#: ``verification_gaps`` / ``contract_status``. Phase 1 added project
#: ``final_acceptance``; Phase 3 adds ``cross_final_acceptance`` —
#: the cross runner's system release gate.
_RELEASE_BEARING_PHASES: tuple[str, ...] = (
    "final_acceptance",
    "cross_final_acceptance",
)


@dataclass(frozen=True)
class _RawEvent:
    seq: int
    ts: str
    kind: str
    phase: str | None
    payload: dict[str, Any]


def collect_evidence(run_dir: Path | str) -> dict[str, Any]:
    """Compose the v1 evidence bundle from a finished run directory.

    Args:
        run_dir: directory containing ``meta.json`` / ``events.jsonl``
            / ``metrics.json``. The directory must exist; missing
            companion files degrade gracefully to empty rollups.

    Returns:
        Bundle dict matching the v1 schema in
        :mod:`pipeline.evidence.schema`. Pass through
        :func:`pipeline.evidence.schema.validate_bundle` to assert
        the contract.
    """
    target = Path(run_dir)
    if not target.is_dir():
        raise FileNotFoundError(f"run_dir does not exist: {target}")

    meta = _load_json(target / "meta.json") or {}
    metrics = _load_json(target / "metrics.json") or {}
    from core.observability.metrics import accounting_enabled, scrub_accounting_fields
    if not accounting_enabled():
        metrics = scrub_accounting_fields(metrics)
    events = _load_events(target / "events.jsonl")

    run_start = _find_event(events, "run.start")
    run_end = _find_event(events, "run.end", reverse=True)

    # M12-C3: ``prompt_render`` is a read-only projection over the
    # session shape persisted in ``meta.json``. ``build_prompt_render_evidence``
    # always returns a list (empty when no covered records exist), so
    # the bundle key is always present.
    #
    # M12-C5: evidence-layer projection lives in
    # ``pipeline.evidence.prompt_render`` (split from
    # ``pipeline.observability.prompt_render``). Observability owns
    # extractor + durable normalization; evidence owns the summary
    # projection that lands in the bundle.
    from pipeline.evidence.prompt_render import build_prompt_render_evidence

    bundle: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": _resolve_run_id(target, meta),
        "run_dir": str(target),
        "status": meta.get("status") or _infer_status(run_end),
        "created_at": _now_iso(),
        "task": meta.get("task") or _payload_get(run_start, "task", ""),
        "profile": meta.get("profile") or _payload_get(run_start, "profile", ""),
        "plan": _build_plan_record(events, meta),
        "phases": _build_phases(events),
        "gates": _build_gates(events),
        "commands": _build_commands(events),
        "artifacts": _build_artifacts(events),
        "metrics": _build_metrics_rollup(metrics),
        "errors": _build_errors(events, meta, target),
        "findings": _build_findings(meta),
        "release_summary": _build_release_summary(meta),
        "implementation_receipts": _build_implementation_receipts(events),
        # ADR 0076: additive durable verification-environment receipt
        # digest (new top-level key — the v1 schema permits adding keys,
        # so this never breaks validation). Empty list when no phase
        # wrote a receipt.
        "verification_receipts": _build_verification_receipts(target),
        # ADR 0082: additive observed-receipt digest (new top-level key —
        # the v1 schema permits adding keys). Deliberately carries NO
        # stale/missing verdict: the collector has neither the declared
        # contract (no required set) nor the live checkout/HEAD (the
        # worktree may be gone after the run), so a deterministic
        # stale/missing call is impossible here — that classification is
        # owned by the prompt layer (pipeline.verification_readiness).
        "verification_readiness": _build_verification_readiness(target),
        "prompt_render": build_prompt_render_evidence(meta),
        "worktree": meta.get("worktree"),
        "worktree_projects": _build_worktree_projects(meta),
        "raw_events_path": str(target / "events.jsonl"),
    }

    # ADR 0093: additive durable handoff-advice digest (new top-level key —
    # the v1 schema permits adding keys, so this never breaks validation).
    # Emitted ONLY when the run actually invoked advice (≥1 advice call): a
    # run with no Stage 0/1 advice surface gets no key at all, so consumers
    # never see a misleading empty section. ``collect_handoff_advice`` reads
    # only durable artifacts under ``run_dir`` + the loaded ``meta``.
    from pipeline.project.handoff_advice_evidence import collect_handoff_advice
    handoff_advice = collect_handoff_advice(target, meta)
    if handoff_advice and handoff_advice.get("calls"):
        bundle["handoff_advice"] = handoff_advice

    # T2: additive durable multi-project delivery disclosure (new top-level key —
    # the v1 schema permits adding keys, so this never breaks validation).
    # Emitted ONLY when the run recorded a companion-repo delivery scope (the
    # ``meta.multi_project_delivery`` block ``run.py`` propagated from the T1
    # disclosure). A clean single-repo mono run records no block, so the key is
    # absent and the bundle stays byte-identical.
    multi_project_delivery = _build_multi_project_delivery(meta)
    if multi_project_delivery is not None:
        bundle["multi_project_delivery"] = multi_project_delivery

    return bundle


def _build_multi_project_delivery(
    meta: dict[str, Any],
) -> dict[str, Any] | None:
    """Project the durable ``meta.multi_project_delivery`` block into the bundle.

    Surfaces the primary delivery status plus per-repo companion disclosure
    (alias / path / state / changed paths) and the convenience ``dirty`` alias
    list, so a post-mortem reads — from the durable bundle alone — that the
    primary shipped while a declared companion repo stayed uncommitted. Returns
    ``None`` (key omitted) when no block was recorded or it carries no usable
    companion entry, keeping single-repo bundles byte-identical. Core-durable
    surface; MCP does not consume it yet.
    """
    block = meta.get("multi_project_delivery")
    if not isinstance(block, dict):
        return None
    companions = block.get("companions")
    if not isinstance(companions, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for entry in companions:
        if not isinstance(entry, dict):
            continue
        alias = str(entry.get("alias") or "")
        if not alias:
            continue
        cleaned.append({
            "alias": alias,
            "path": str(entry.get("path") or ""),
            "state": str(entry.get("state") or ""),
            "changed_paths": [
                str(p) for p in (entry.get("changed_paths") or [])
                if isinstance(p, str)
            ],
        })
    if not cleaned:
        return None
    return {
        "primary_status": str(block.get("primary_status") or ""),
        "companions": cleaned,
        "dirty": [c["alias"] for c in cleaned if c["state"] == "dirty"],
    }


# ── Internals ───────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _resolve_run_id(target: Path, meta: dict[str, Any]) -> str:
    return meta.get("run_id") or meta.get("session_ts") or target.name


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_events(path: Path) -> list[_RawEvent]:
    if not path.is_file():
        return []
    out: list[_RawEvent] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(_RawEvent(
            seq=int(d.get("seq", 0)),
            ts=str(d.get("ts", "")),
            kind=str(d.get("kind", "")),
            phase=d.get("phase"),
            payload=dict(d.get("payload") or {}),
        ))
    return out


def _find_event(
    events: list[_RawEvent], kind: str, *, reverse: bool = False,
) -> _RawEvent | None:
    iterable = reversed(events) if reverse else iter(events)
    return next((e for e in iterable if e.kind == kind), None)


def _payload_get(evt: _RawEvent | None, key: str, default: Any) -> Any:
    if evt is None:
        return default
    return evt.payload.get(key, default)


def _infer_status(run_end: _RawEvent | None) -> str:
    """Status fallback when ``meta.json.status`` is missing."""
    if run_end is None:
        return "unknown"
    return str(run_end.payload.get("status") or "unknown")


# ── Plan record ─────────────────────────────────────────────────────────────

def _build_plan_record(
    events: list[_RawEvent], meta: dict[str, Any],
) -> dict[str, Any]:
    """Compose the embedded plan-contract record.

    Source of truth: the ``plan.parsed`` event (REA-2) — it carries the
    architect's typed contract surface without re-parsing markdown.
    Falls through to empty plan fields when no parse event fired.
    """
    parsed = _find_event(events, "plan.parsed", reverse=True)
    if parsed is None:
        return {
            "source": "absent",
            "short_summary": "",
            "planning_context": "",
            "subtask_count": 0,
            "has_contract": False,
            "goal": None,
            "acceptance_criteria": [],
            "owned_files": [],
            "commands_to_run": [],
            "risks": [],
            "review_focus": [],
            "mcp_context": [],
            "subtasks": [],
        }
    payload = parsed.payload
    return {
        "source": str(payload.get("source", "json")),
        "short_summary": str(payload.get("short_summary") or ""),
        "planning_context": str(payload.get("planning_context") or ""),
        "subtask_count": int(payload.get("subtask_count", 0)),
        "has_contract": bool(payload.get("has_contract", False)),
        "goal": payload.get("goal") or None,
        "acceptance_criteria": _string_list_from_payload(
            payload, "acceptance_criteria", "acceptance_criteria_count",
        ),
        "owned_files": _string_list_from_payload(
            payload, "owned_files", "owned_files_count",
        ),
        "commands_to_run": _string_list_from_payload(
            payload, "commands_to_run", "commands_to_run_count",
        ),
        "risks": _string_list_from_payload(payload, "risks"),
        "review_focus": _string_list_from_payload(payload, "review_focus"),
        "mcp_context": [
            dict(x) for x in payload.get("mcp_context", [])
            if isinstance(x, dict)
        ],
        "subtasks": [
            dict(x) for x in payload.get("subtasks", [])
            if isinstance(x, dict)
        ],
    }


def _string_list_from_payload(
    payload: dict[str, Any],
    key: str,
    count_key: str | None = None,
) -> list[str]:
    """Return a typed contract list from ``plan.parsed`` payload.

    REA-3 stores the real list values when present. The count fallback
    preserves compatibility with REA-2 events written before the full
    contract payload was added.
    """
    value = payload.get(key)
    if isinstance(value, list):
        return [str(x) for x in value if isinstance(x, str)]
    if count_key is None:
        return []
    n = int(payload.get(count_key, 0) or 0)
    return [f"<entry {i + 1}>" for i in range(n)]


def _build_verification_receipts(run_dir: Path) -> list[dict[str, Any]]:
    """Return the additive verification-environment receipt digest (ADR 0076).

    Reads the durable receipts under ``<run_dir>/verification_receipts/``
    and folds them to the brief summary form. Empty list when no phase
    wrote a receipt — additive, never breaks the v1 schema.
    """
    from pipeline.evidence.verification_receipt import (
        summarize_verification_receipts,
    )

    return summarize_verification_receipts(run_dir)


def _build_verification_readiness(run_dir: Path) -> dict[str, Any]:
    """Return the additive observed-receipt readiness digest (ADR 0082).

    ``commands`` is the Stage 3 per-command summary (command / env /
    exit_code / parity / passed / has_baseline) from
    ``verification_command_receipts/``; ``envs`` is the per-env
    ``all_passed`` rollup from ``verification_env_receipts/``. Both lists
    are empty when the directories are absent. Observed facts only — no
    stale/missing verdict (see the collect_evidence comment).

    OBSERVED FACTS, NOT A VERDICT (ADR 0089 / review F2). ``passed`` here is a
    single receipt's own rollup (exit 0 AND every declared assertion passed); it
    is NOT a readiness verdict over the *required* command set, and this digest
    deliberately carries no ``required`` / ``missing`` / ``failed`` / ``stale``
    keys. It reads ONLY the current ``run_dir`` and never searches parent runs.
    The single authoritative required-missing/failed/stale verdict — including
    parent-receipt inheritance and subject/dependency staleness — lives in
    :func:`pipeline.verification_readiness.classify_required_receipts`; no other
    surface recomputes it. The status-surface inventory (review F2) confirmed
    this: ``cli/_formatters.py`` (``format_verify_env`` / ``format_verify_run`` /
    ``format_verify_list``) render the *execution* result of ``orcho verify``
    (PASS/FAIL from exit codes / assertions), not a readiness verdict, and a grep
    of ``cli/``, ``sdk/``, ``pipeline/control/``, and ``pipeline/observability/``
    found no other command-receipt verdict computation. The wire shape of this
    digest is locked by
    ``tests/unit/pipeline/evidence/test_verification_readiness_digest.py`` (evidence
    v1 is a schema-validated wire format; its shape must not drift).
    """
    from pipeline.evidence.verification_receipt import (
        load_env_assertion_receipts,
        summarize_command_receipts,
    )

    return {
        "commands": summarize_command_receipts(run_dir),
        "envs": [
            {
                "env": str(r.get("env", "")),
                "all_passed": bool(r.get("all_passed", False)),
            }
            for r in load_env_assertion_receipts(run_dir)
        ],
    }


def _build_implementation_receipts(events: list[_RawEvent]) -> list[dict[str, Any]]:
    """Return policy-owned per-subtask delivery receipts."""
    out: list[dict[str, Any]] = []
    for event in events:
        if event.kind != "subtask.receipt":
            continue
        payload = event.payload
        subtask_id = payload.get("subtask_id")
        state = payload.get("state")
        if not isinstance(subtask_id, str) or not subtask_id:
            continue
        if not isinstance(state, str) or not state:
            continue
        receipt: dict[str, Any] = {
            "subtask_id": subtask_id,
            "state": state,
            "runtime": str(payload.get("runtime") or ""),
            "model": str(payload.get("model") or ""),
            "skill": payload.get("skill") if isinstance(payload.get("skill"), str) else None,
            "depends_on": [
                str(x) for x in payload.get("depends_on", [])
                if isinstance(x, str)
            ],
            "done_criteria": [
                str(x) for x in payload.get("done_criteria", [])
                if isinstance(x, str)
            ],
            "duration": float(payload.get("duration") or 0.0),
            "error": payload.get("error") if isinstance(payload.get("error"), str) else None,
        }
        # P7: carry the done-criteria self-attestation into the durable bundle
        # when present, so the receipt records WHAT the developer claimed and
        # (on an ``incomplete`` state) WHY the gate did not close.
        criteria_report = payload.get("criteria_report")
        if isinstance(criteria_report, list):
            cleaned = [c for c in criteria_report if isinstance(c, dict)]
            if cleaned:
                receipt["criteria_report"] = cleaned
        summary = payload.get("attestation_summary")
        if isinstance(summary, str) and summary:
            receipt["attestation_summary"] = summary
        att_error = payload.get("attestation_error")
        if isinstance(att_error, str) and att_error:
            receipt["attestation_error"] = att_error
        if payload.get("attestation_repaired") is True:
            receipt["attestation_repaired"] = True
        out.append(receipt)
    return out


# ── Phases / gates / commands / artifacts ──────────────────────────────────

def _build_phases(events: list[_RawEvent]) -> list[dict[str, Any]]:
    """Roll phase.start/phase.end pairs into per-attempt records.

    Each ``phase.start`` opens a new attempt; the matching
    ``phase.end`` closes it. Out-of-order events (a ``phase.end``
    without a preceding ``start``) are still recorded so a partially
    captured run still produces a usable bundle.
    """
    by_phase: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evt in events:
        if evt.kind == "phase.start":
            by_phase[evt.phase or ""].append({
                "name": evt.phase or "",
                "title": str(evt.payload.get("title", "")),
                "phase_kind": evt.payload.get("phase_kind"),
                "attempt": int(evt.payload.get("attempt", 1)),
                "started_at": evt.ts,
                "ended_at": None,
                "outcome": "in_progress",
            })
        elif evt.kind == "phase.end":
            attempts = by_phase.get(evt.phase or "")
            if attempts:
                # Match by attempt number when present, else most recent.
                attempt_num = evt.payload.get("attempt")
                target = next(
                    (
                        a for a in reversed(attempts)
                        if (attempt_num is None or a["attempt"] == attempt_num)
                        and a["ended_at"] is None
                    ),
                    attempts[-1],
                )
                target["ended_at"] = evt.ts
                target["outcome"] = str(evt.payload.get("outcome", "unknown"))
            else:
                # Synthetic record so the rollup still surfaces the phase
                # even without a matching start.
                by_phase[evt.phase or ""].append({
                    "name": evt.phase or "",
                    "title": str(evt.payload.get("title", "")),
                    "phase_kind": evt.payload.get("phase_kind"),
                    "attempt": int(evt.payload.get("attempt", 1)),
                    "started_at": "",
                    "ended_at": evt.ts,
                    "outcome": str(evt.payload.get("outcome", "unknown")),
                })
    flat: list[dict[str, Any]] = []
    for entries in by_phase.values():
        flat.extend(entries)
    flat.sort(key=lambda r: (r.get("started_at") or r.get("ended_at") or ""))
    return flat


def _build_gates(events: list[_RawEvent]) -> list[dict[str, Any]]:
    """Pair gate.start with gate.end by gate name."""
    open_gates: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    for evt in events:
        if evt.kind == "gate.start":
            name = str(evt.payload.get("name", ""))
            open_gates[name] = {
                "name": name,
                "kind": str(evt.payload.get("gate_kind", "computational")),
                "started_at": evt.ts,
                "outcome": "in_progress",
                "duration_s": 0.0,
            }
        elif evt.kind == "gate.end":
            name = str(evt.payload.get("name", ""))
            record = open_gates.pop(name, {
                "name": name,
                "kind": "computational",
                "started_at": "",
            })
            record["ended_at"] = evt.ts
            record["outcome"] = str(evt.payload.get("outcome", "unknown"))
            record["duration_s"] = float(evt.payload.get("duration_s", 0.0))
            error = evt.payload.get("error")
            if error:
                record["error"] = error
            closed.append(record)
    # Surface gates that started but never closed (timeouts, crashes).
    for stuck in open_gates.values():
        stuck["ended_at"] = None
        closed.append(stuck)
    return closed


def _build_commands(events: list[_RawEvent]) -> list[dict[str, Any]]:
    """Pair command.start with command.end. FIFO matching since
    nested shell invocations are rare and pairing by name would
    require argv to be unique."""
    open_commands: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    for evt in events:
        if evt.kind == "command.start":
            open_commands.append({
                "argv_summary": str(evt.payload.get("argv_summary", "")),
                "cwd": str(evt.payload.get("cwd", "")),
                "command_kind": str(evt.payload.get("command_kind", "")),
                "started_at": evt.ts,
                "exit_code": None,
                "duration_s": 0.0,
                "outcome": "in_progress",
            })
        elif evt.kind == "command.end":
            record = open_commands.pop(0) if open_commands else {
                "argv_summary": "",
                "cwd": "",
                "command_kind": "",
                "started_at": "",
            }
            record["ended_at"] = evt.ts
            record["exit_code"] = int(evt.payload.get("exit_code", -1))
            record["duration_s"] = float(evt.payload.get("duration_s", 0.0))
            record["outcome"] = str(evt.payload.get("outcome", "unknown"))
            closed.append(record)
    for stuck in open_commands:
        stuck["ended_at"] = None
        stuck["exit_code"] = stuck.get("exit_code") if stuck.get("exit_code") is not None else -1
        closed.append(stuck)
    return closed


def _build_artifacts(events: list[_RawEvent]) -> list[dict[str, Any]]:
    """Snapshot every ``artifact.created`` payload."""
    artifacts: list[dict[str, Any]] = []
    for evt in events:
        if evt.kind != "artifact.created":
            continue
        artifact = {
            "path": str(evt.payload.get("path", "")),
            "kind": str(evt.payload.get("artifact_kind", "")),
            "size_bytes": int(evt.payload.get("size_bytes", 0)),
            "created_at": evt.ts,
        }
        if "attempt" in evt.payload:
            artifact["attempt"] = int(evt.payload["attempt"])
        if isinstance(evt.payload.get("apply_check"), dict):
            artifact["apply_check"] = evt.payload["apply_check"]
        artifacts.append(artifact)
    return artifacts


# ── Metrics + errors ───────────────────────────────────────────────────────

def _build_metrics_rollup(metrics: dict[str, Any]) -> dict[str, Any]:
    """Project ``metrics.json`` onto the bundle metrics surface.

    Lower-bound contract — missing fields degrade to zero rather than
    crashing the bundle.
    """
    return {
        "total_tokens": int(metrics.get("total_tokens", 0) or 0),
        "total_tokens_in": int(metrics.get("total_tokens_in", 0) or 0),
        "total_tokens_out": int(metrics.get("total_tokens_out", 0) or 0),
        "total_duration_s": float(metrics.get("total_duration_s", 0.0) or 0.0),
        "total_rounds": int(metrics.get("total_rounds", 0) or 0),
        **(
            {"total_retries": int(metrics["total_retries"])}
            if "total_retries" in metrics else {}
        ),
        **(
            {"per_phase": metrics["phases"]}
            if isinstance(metrics.get("phases"), dict) else {}
        ),
        # Additive passthrough of the per-subtask usage breakdown so a
        # post-mortem can answer "which implement subtask produced the most
        # usage?" from the durable bundle alone. Present only when ``metrics.json``
        # carried it (subtask_dag runs); accounting scrub already ran on
        # ``metrics`` in ``collect_evidence`` before this rollup.
        **(
            {"subtasks": metrics["subtasks"]}
            if isinstance(metrics.get("subtasks"), dict) else {}
        ),
    }


def _build_errors(
    events: list[_RawEvent], meta: dict[str, Any], run_dir: Path,
) -> list[dict[str, Any]]:
    """Surface explicit error breadcrumbs from events + session.

    Sources covered today:

    * ``run.end`` events whose payload carries ``error`` / ``error_type``
      (single-project halt, cross-project failure).
    * ``meta.phases.plan[].parse_error`` — REA-1 plan-schema halt.
    * ``phase.handoff_requested`` events + ``meta.phase_handoff`` —
      generic phase-handoff pause record (post-Phase-3 cutover; the
      legacy ``validate_plan.gate_blocked`` event + ``meta.plan_gate``
      payload were replaced).
    * ``meta.phase_handoff_waiver`` — the durable record of a
      ``continue_with_waiver`` decision (kind ``phase_handoff_waiver``):
      a REJECTED verdict the operator accepted with a verdict, injected
      into downstream review gates so findings are not reopened.
    * **Synthesized setup/preflight failure** (ADR 0104) — a single
      ``setup_failed`` breadcrumb for a run that died *before any phase ran*
      and left no other terminal breadcrumb. Appended ONLY when
      :func:`pipeline.run_state.setup_failure.detect_setup_preflight_failure`
      fires; its own gate guarantees it stays silent (and the ``errors`` list
      byte-identical) whenever any richer terminal cause above is present.
    """
    errors: list[dict[str, Any]] = []
    for evt in events:
        if evt.kind != "run.end":
            continue
        if evt.payload.get("status") == "halted":
            errors.append({
                "kind": "run_halted",
                "message": str(evt.payload.get("halt_reason") or "halted"),
                "at": evt.ts,
            })
        if evt.payload.get("error"):
            record = {
                "kind": "run_failed",
                "message": str(evt.payload.get("error")),
                "error_type": str(evt.payload.get("error_type", "")),
                "at": evt.ts,
            }
            for key in (
                "failure_kind",
                "recoverable",
                "recommended_action",
                "failed_phase",
                "runtime",
                "model",
                "recovery_actions",
                # ADR 0118: recoverable provider/runtime failures carry a
                # sanitized provider_message instead of recovery_actions; copy
                # it through so the errors slice surfaces the typed signature.
                "provider_message",
            ):
                if key in evt.payload:
                    record[key] = evt.payload[key]
            errors.append(record)

    plan_attempts = (meta.get("phases") or {}).get("plan") or []
    if isinstance(plan_attempts, list):
        for entry in plan_attempts:
            if not isinstance(entry, dict):
                continue
            parse_err = entry.get("parse_error")
            if parse_err:
                errors.append({
                    "kind": "plan_parse_error",
                    "message": str(parse_err),
                    "attempt": entry.get("attempt"),
                })

    for evt in events:
        if evt.kind == "phase.handoff_requested":
            errors.append({
                "kind":         "phase_handoff_requested",
                "message":      str(evt.payload.get("trigger", "")),
                "phase":        evt.payload.get("phase", ""),
                "handoff_type": evt.payload.get("handoff_type", ""),
                "handoff_id":   evt.payload.get("handoff_id", ""),
                "round":        evt.payload.get("round"),
                "at":           evt.ts,
            })

    handoff = meta.get("phase_handoff")
    if isinstance(handoff, dict) and handoff.get("id"):
        errors.append({
            "kind":         "phase_handoff_requested",
            "message":      str(
                handoff.get("last_output") or handoff.get("trigger") or "",
            ),
            "phase":        str(handoff.get("phase", "")),
            "handoff_type": str(handoff.get("type", "")),
            "handoff_id":   str(handoff.get("id", "")),
            "round":        handoff.get("round"),
        })

    # ``continue_with_waiver`` durable record: the operator accepted a
    # REJECTED verdict and the waived findings are injected into
    # downstream review gates. Surface it as a distinct verdict-exception
    # so a post-mortem shows the run shipped under an operator waiver.
    waiver = meta.get("phase_handoff_waiver")
    if isinstance(waiver, dict) and waiver.get("handoff_id"):
        waiver_text = str(waiver.get("waiver_text", ""))
        errors.append({
            "kind":         "phase_handoff_waiver",
            "message":      waiver_text,
            "waiver_text":  waiver_text,
            "phase":        str(waiver.get("phase", "")),
            "handoff_id":   str(waiver.get("handoff_id", "")),
            "decided_at":   str(waiver.get("decided_at", "")),
            # ADR 0073: applier-set provenance — ``operator`` for a human
            # resume decision, ``auto:on_exhausted`` for the automatic
            # implement substance-repair fallback. ``decided_at`` is kept.
            "decided_by":   str(waiver.get("decided_by", "")),
            "findings":     waiver.get("findings"),
            "note":         waiver.get("note"),
        })

    # Stage 6 delivery-gate waiver provenance: a required verification gate whose
    # failed/missing receipt was excused by an exact durable ``phase_handoff_waiver``
    # (``gate:<command>:<round>``). ``run.py`` persists one record per waived gate
    # under ``meta.commit_delivery_verification_waived`` (a NON-wire delivery
    # evidence key). Surface a distinct breadcrumb per gate so a post-mortem shows
    # delivery shipped a required gate under an operator waiver — accepted, not an
    # active blocker. Absent key → no breadcrumb (byte-identical otherwise).
    waived_gates = meta.get("commit_delivery_verification_waived")
    if isinstance(waived_gates, list):
        for entry in waived_gates:
            if not isinstance(entry, dict):
                continue
            command = str(entry.get("command") or entry.get("gate_name") or "")
            if not command:
                continue
            waiver_preview = str(entry.get("waiver_preview", ""))
            errors.append({
                "kind":           "verification_gate_waived",
                "command":        command,
                "gate_name":      str(entry.get("gate_name") or command),
                "handoff_id":     str(entry.get("handoff_id", "")),
                "message":        waiver_preview,
                "waiver_preview": waiver_preview,
                "status":         str(entry.get("status", "")),
            })

    # ADR 0073: implement substance-repair delivery provenance. The implement
    # phase entry (persisted by ``BuildAdapter``) carries the final
    # ``delivery_status`` (clean | repaired | waived | incomplete) plus the
    # waiver fields and WHICH subtasks blocked delivery. Surface a distinct
    # breadcrumb for any non-clean delivery so a post-mortem sees how implement
    # closed (``action`` distinguishes a bare ``continue`` from
    # ``continue_with_waiver``) and exactly which subtasks were accepted —
    # including missing-receipt ids, which have no ``subtask.receipt`` event of
    # their own and would otherwise be invisible in evidence.
    implement_attempts = _phase_attempts((meta.get("phases") or {}).get("implement"))
    if implement_attempts:
        impl = implement_attempts[-1]
        delivery_status = impl.get("delivery_status")
        if isinstance(delivery_status, str) and delivery_status not in ("", "clean"):
            _incomplete = impl.get("incomplete_subtasks")
            _missing = impl.get("missing_subtask_receipts")
            _att_incomplete = impl.get("attestation_incomplete")
            errors.append({
                "kind":                     "implement_delivery",
                "delivery_status":          delivery_status,
                "delivery_waived":          bool(impl.get("delivery_waived")),
                "waiver_id":                str(impl.get("waiver_id", "")),
                "action":                   str(impl.get("action", "")),
                "incomplete_subtasks":      list(_incomplete) if isinstance(_incomplete, list) else [],
                "missing_subtask_receipts": list(_missing) if isinstance(_missing, list) else [],
                "attestation_incomplete":   dict(_att_incomplete) if isinstance(_att_incomplete, dict) else {},
            })

    # ADR 0103: durable stalled-command breadcrumbs, one per
    # ``agent.command_stalled`` event. The single event kind covers BOTH
    # paths — the terminal idle-timeout escalation (``terminal=True``, emitted
    # by run.py just before ``run.end``) and the live non-terminal
    # unsafe-process-polling diagnostic (``terminal=False``, write-through from
    # the provider-neutral sink during a stream event). The non-terminal
    # record is the durable mirror of a live diagnostic; it does NOT imply the
    # run failed.
    errors.extend(_build_command_stalled_errors(events))

    # ADR 0104: synthesized setup/preflight failure. ``detect_*`` returns a
    # record ONLY when the run died before any phase ran AND none of the
    # terminal breadcrumbs above are present (no run.end error/halt, no
    # meta.failure, no active phase_handoff, no phase attempts) AND the merged
    # terminal state says the run failed. So this is strictly additive: for any
    # run with an existing terminal cause it is a no-op and ``errors`` stays
    # byte-identical.
    setup_failure = detect_setup_preflight_failure(meta, run_dir, events)
    if setup_failure is not None:
        errors.append(setup_failure)
    return errors


def _build_command_stalled_errors(
    events: list[_RawEvent],
) -> list[dict[str, Any]]:
    """One ``command_stalled`` error record per ``agent.command_stalled`` event.

    Provider-neutral and path-agnostic: both the terminal idle-timeout
    escalation and the live non-terminal risk flag emit the same event kind,
    discriminated by the ``terminal`` payload flag. ``recovery_actions`` is
    taken from the payload when present and otherwise filled from the shared
    durable builder (:func:`pipeline.run_state.stalled_command.build_stall_recovery_actions`)
    so terminal failure, live projection, and this evidence record agree on the
    interrupt/resume/halt verb set.
    """
    from pipeline.run_state.stalled_command import build_stall_recovery_actions

    out: list[dict[str, Any]] = []
    for evt in events:
        if evt.kind != "agent.command_stalled":
            continue
        payload = evt.payload
        recovery_actions = payload.get("recovery_actions")
        if not isinstance(recovery_actions, list):
            recovery_actions = build_stall_recovery_actions()
        record: dict[str, Any] = {
            "kind": "command_stalled",
            "phase": str(payload.get("phase") or evt.phase or ""),
            "reason": str(payload.get("reason") or ""),
            "elapsed_s": _coerce_float(payload.get("elapsed_s"), 0.0),
            "terminal": bool(payload.get("terminal", False)),
            "recovery_actions": recovery_actions,
            "at": evt.ts,
        }
        preview = _optional_str(payload.get("command_preview"))
        if preview is not None:
            record["command_preview"] = preview
        tail = _optional_str(payload.get("output_tail"))
        if tail is not None:
            record["output_tail"] = tail
        pgroup = _optional_int(payload.get("process_group"))
        if pgroup is not None:
            record["process_group"] = pgroup
        out.append(record)
    return out


# ── Findings ───────────────────────────────────────────────────────────────

def _build_findings(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten reviewer findings from ``meta.phases`` into one list.

    Each finding-bearing phase (validate_plan / review / final_acceptance /
    compliance_check) stores attempts as a list of dicts; each attempt
    may carry ``findings: list[dict]``. We surface every finding as a
    flat record annotated with its source ``phase`` + ``attempt`` so a
    consumer can render the chain that produced it without re-walking
    meta.

    Order is preserved exactly: phase order matches
    :data:`_FINDING_BEARING_PHASES`, attempt order follows the meta
    list, and within an attempt finding order follows source order.
    No severity sort — the *causal* sequence (what blocked first) is
    more useful than a global priority view.

    Tolerant of missing / non-dict shapes — a phase that's absent or
    stored as something other than a list of dicts contributes no
    findings rather than crashing.
    """
    out: list[dict[str, Any]] = []
    phases_meta = meta.get("phases") or {}
    if not isinstance(phases_meta, dict):
        return out
    phase_attempts = {
        phase_name: _phase_attempts(phases_meta.get(phase_name))
        for phase_name in _FINDING_BEARING_PHASES
    }
    for phase_name in _FINDING_BEARING_PHASES:
        attempts = phase_attempts[phase_name]
        for idx, attempt in enumerate(attempts, start=1):
            attempt_num = _coerce_int(attempt.get("attempt"), idx)
            findings = attempt.get("findings")
            if not isinstance(findings, list):
                continue
            for f in findings:
                if not isinstance(f, dict):
                    continue
                out.append({
                    "id": str(f.get("id") or ""),
                    "severity": str(f.get("severity") or "P3"),
                    "title": str(f.get("title") or ""),
                    "body": str(f.get("body") or ""),
                    "required_fix": _optional_str(f.get("required_fix")),
                    "file": _optional_str(f.get("file")),
                    "line": _optional_int(f.get("line")),
                    "phase": phase_name,
                    "attempt": attempt_num,
                    "source_verdict": str(attempt.get("verdict") or ""),
                    "source_approved": (
                        attempt.get("approved")
                        if isinstance(attempt.get("approved"), bool)
                        else None
                    ),
                    "source_ship_ready": (
                        attempt.get("ship_ready")
                        if isinstance(attempt.get("ship_ready"), bool)
                        else None
                    ),
                })
    waiver = meta.get("phase_handoff_waiver")
    return annotate_finding_lifecycle(
        out,
        phase_attempts,
        waiver=waiver if isinstance(waiver, dict) else None,
    )


def _build_release_summary(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Surface release-gate signal as a top-level evidence section
    (ADR 0025 Phase 1).

    One entry per release-bearing phase (today: ``final_acceptance``;
    Phase 3 will add ``cross_final_acceptance``). The entry pulls
    ``ship_ready`` / ``release_blockers`` / ``verification_gaps`` /
    ``contract_status`` from the persisted session entry written by
    :class:`pipeline.session_adapters.FinalAcceptanceAdapter`. Existing
    ``findings`` consumers keep reading the parallel ``findings``
    section; release-aware consumers read this one for the structured
    release view — including the release-only ``why_blocks_release``
    field on each blocker that the review-shape mirror drops.

    Tolerant of legacy paths: a phase that never wrote the release
    fields (or wrote them as ``None`` on a parse-error path)
    contributes no entry rather than crashing.
    """
    out: list[dict[str, Any]] = []
    phases_meta = meta.get("phases") or {}
    if not isinstance(phases_meta, dict):
        return out
    for phase_name in _RELEASE_BEARING_PHASES:
        attempts = _phase_attempts(phases_meta.get(phase_name))
        if not attempts:
            continue
        # Release surface is currently single-entry per phase (the
        # final_acceptance handler writes one dict). When Phase 3 adds
        # cross_final_acceptance there will still be one entry per
        # phase; if a future change introduces attempts, emit one
        # summary per attempt rather than collapsing.
        for entry in attempts:
            ship_ready = entry.get("ship_ready")
            if ship_ready is None:
                # Phase ran but didn't emit release fields (parse error
                # or legacy review-shape path).
                continue
            contract_status = entry.get("contract_status")
            if not isinstance(contract_status, dict):
                contract_status = None
            release_blockers = entry.get("release_blockers")
            if not isinstance(release_blockers, list):
                release_blockers = []
            verification_gaps = entry.get("verification_gaps")
            if not isinstance(verification_gaps, list):
                verification_gaps = []
            out.append({
                "phase": phase_name,
                "verdict": str(entry.get("verdict") or ""),
                "ship_ready": bool(ship_ready),
                "short_summary": str(entry.get("short_summary") or ""),
                "release_blockers": release_blockers,
                "verification_gaps": verification_gaps,
                "contract_status": contract_status,
            })
    return out


def _build_worktree_projects(meta: dict[str, Any]) -> dict[str, Any]:
    """Extract per-alias worktree contexts from a cross-run meta dict.

    Returns ``{}`` for single-project runs (no ``projects`` key).
    Only aliases that recorded a worktree block are included.
    """
    projects = meta.get("projects")
    if not isinstance(projects, dict):
        return {}
    return {
        alias: proj["worktree"]
        for alias, proj in projects.items()
        if isinstance(proj, dict) and proj.get("worktree") is not None
    }


def _phase_attempts(value: Any) -> list[dict[str, Any]]:
    """Normalize a ``meta.phases[name]`` slot into a list of attempt
    dicts. Two persisted shapes exist today:

      * **Attempt-list shape** (``validate_plan``, ``review_changes``,
        ``compliance_check``): ``list[dict]`` of per-round attempts.
      * **Singleton-dict shape** (``final_acceptance`` since
        ADR 0025 Phase 1, written by
        :class:`pipeline.session_adapters.FinalAcceptanceAdapter`): a
        single ``dict`` — the closing gate runs once, not as a loop.

    Without this normalization, the dict-shape path is silently invisible
    to ``_build_findings`` and ``_build_release_summary``, dropping the
    release blockers projected into the review-shape ``findings`` mirror.
    """
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
