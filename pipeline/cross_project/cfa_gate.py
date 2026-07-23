"""cross_final_acceptance (CFA) pause gate — Phase A2.

The CFA gate turns the cross_final_acceptance REJECTED terminal into
an operator pause/decide gate that mirrors the cross_plan pause
(ADR 0038) and the project-proxy pause (ADR 0039 cross parity). On
the fresh path it runs the CFA reviewer; on REJECTED it persists a
pause via :func:`apply_cross_phase_handoff_pause` and returns
``CfaGateOutcome(outcome="paused")`` so the cross runner exits
WITHOUT emitting ``run.end``. On the resume path it consumes the
operator's decision artifact and dispatches the three actions:

* ``halt`` → :func:`finalize_cross_terminal` writes terminal
  ``status="halted"`` + ``halt_reason="phase_handoff_halt"`` and
  the gate returns ``outcome="halted"``. The caller MUST NOT
  invoke ``finalize_cross_run`` after this (no double run.end).
* ``continue`` (operator override) → an APPROVED CFA result is
  synthesised so the existing :func:`finalize_cross_run`
  status-decision tree flows through the APPROVED branch and
  emits ``run.end`` with ``status="done"``. The override marker is
  stamped on ``session["phases"]["cross_final_acceptance"]
  ["override"]`` for evidence/audit. Phase B will additionally
  surface per-alias override on the delivery side.
* ``retry_feedback`` → deferred to Phase A2c (extends the CFA
  reviewer with a ``feedback`` parameter and re-invokes). Today the
  gate raises ``NotImplementedError`` so a stale decision artifact
  cannot silently bypass the recovery contract.

Critical invariants pinned by tests:

* Resume routing dispatches on ``cross_ckpt["phase_handoff_kind"]
  == "cfa"`` — NOT on the id prefix alone. A stale checkpoint with a
  mis-prefixed id never mis-dispatches into the planning loop.
* The fresh REJECT path does NOT emit ``run.end`` (the structural
  completion signal); the pause is persisted via
  :func:`apply_cross_phase_handoff_pause` and the gate returns
  ``paused`` so the cross runner short-circuits.
* The override-continue branch synthesises a verdict-only APPROVED
  CFA result. The original REJECTED verdict + findings stay on
  ``session["phases"]["cross_final_acceptance"]`` (persisted by the
  pause) and the override marker preserves operator audit. The
  finalizer sees the synthesised APPROVED so the existing decision
  tree does not need a new branch.
* All three resume actions clear ``phase_handoff_pending``,
  ``phase_handoff_id``, ``phase_handoff_kind``, and ``cfa_paused_state``
  from the cross checkpoint before returning.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

#: Default upper bound on CFA pause rounds (initial REJECT + retries).
#: Hardcoded for Phase A2; a future ticket may surface this as a
#: configurable knob next to ``LoopStep.max_rounds`` on the cross
#: profile.
CFA_DEFAULT_MAX_ROUNDS: int = 2


@dataclass(frozen=True)
class CfaGateOutcome:
    """Structured outcome returned by :func:`evaluate_cfa_gate`.

    The caller (cross app body) routes on ``outcome``:

    * ``"approved_terminal"`` — fresh CFA returned APPROVED; caller
      proceeds to finalize as ``done``. ``cfa_result`` is the
      reviewer's actual result.
    * ``"paused"`` — fresh CFA REJECTED; pause already persisted via
      :func:`apply_cross_phase_handoff_pause`. Caller MUST return
      without finalizing. ``cfa_result`` is the reviewer's result
      (persisted on session before the pause).
    * ``"override_continue"`` — resumed with action=continue; caller
      proceeds to finalize as ``done`` using the SYNTHESISED
      APPROVED CFA result in ``cfa_result``. ``override_marker``
      carries the dict the caller MUST re-apply to
      ``session["phases"]["cross_final_acceptance"]["override"]``
      AFTER refreshing the phase_log entry from the synthesised
      result (``result_to_phase_log_entry`` rewrites the dict and
      would otherwise clear the marker).
    * ``"halted"`` — resumed with action=halt; terminal halted state
      already persisted via :func:`finalize_cross_terminal`. Caller
      MUST return without finalizing.
    * ``"retry_consumed"`` — resumed with action=retry_feedback and
      the re-invoked CFA reviewer returned APPROVED; caller
      proceeds to finalize as ``done`` using the new reviewer
      result in ``cfa_result``. (Phase A2c — currently unreachable;
      ``retry_feedback`` raises ``NotImplementedError``.)
    """

    outcome: Literal[
        "approved_terminal",
        "cached_terminal",
        "paused",
        "override_continue",
        "halted",
        "retry_consumed",
    ]
    cfa_result: Any | None  # CrossFinalAcceptanceResult when not paused/halted
    override_marker: dict | None = None  # only populated on override_continue


def load_completed_cfa_cache(
    session: dict, run_dir: Path | None = None
) -> Any | None:
    """Return a strictly valid, completed CFA cache or ``None``.

    Only an approved, ship-ready persisted result is terminally reusable.  A
    rejected result needs its handoff route, and absent, pending, or malformed
    entries must remain on the normal fail-closed gate path.
    """
    phases = session.get("phases")
    entry = phases.get("cross_final_acceptance") if isinstance(phases, dict) else None
    if entry is None and run_dir is not None:
        _load_prior_cfa_result_from_disk(run_dir, session)
        phases = session.get("phases")
        entry = phases.get("cross_final_acceptance") if isinstance(phases, dict) else None
    if not _is_completed_cfa_cache_entry(entry):
        return None
    return _load_prior_cfa_result(session)


def has_completed_cfa_phase_entry(entry: Any) -> bool:
    """Return whether ``entry`` is a complete persisted CFA result.

    This answers the graph's historical question (did the gate finish?), not
    the narrower resume-cache question (can an APPROVED result be reused for
    delivery).  A valid REJECTED result remains completed even when its final
    disposition was an operator override or a terminal halt.
    """
    if not isinstance(entry, dict) or entry.get("skipped") is True:
        return False
    required_strings = ("output", "raw_output", "short_summary", "source")
    if not all(isinstance(entry.get(field), str) for field in required_strings):
        return False
    duration = entry.get("duration_s")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        return False
    if not isinstance(entry.get("approved"), bool):
        return False
    if entry.get("approved") is not (entry.get("verdict") == "APPROVED"):
        return False

    from core.contracts.release_schema import ReleaseSchemaError, validate_release_dict

    try:
        validate_release_dict({
            field: entry.get(field)
            for field in (
                "verdict", "ship_ready", "short_summary", "release_blockers",
                "verification_gaps", "contract_status",
            )
        })
    except ReleaseSchemaError:
        return False
    return True


def _is_completed_cfa_cache_entry(entry: Any) -> bool:
    """Validate the persisted CFA phase-log contract without coercion."""
    if not has_completed_cfa_phase_entry(entry) or entry.get("parse_error") is not None:
        return False
    return (
        entry.get("verdict") == "APPROVED"
        and entry.get("approved") is True
        and entry.get("ship_ready") is True
        and entry.get("source") in {"agent", "precondition"}
    )


def _clear_cfa_checkpoint_markers(cross_ckpt: dict) -> None:
    """Strip every CFA-specific checkpoint field on resume completion.

    Called by all three resume action branches (halt / continue /
    retry_feedback success) so a subsequent resume can never re-enter
    the gate on stale markers.

    The shared handoff markers (pending flag + id + kind + project
    siblings) are evicted through the single canonical
    :func:`evict_cross_handoff_markers`. ``cfa_paused_state`` is popped
    locally here, NOT via that helper: it is CFA-specific resume state
    (the verdict/summary the gate renders without re-invoking the
    reviewer), not a shared cross handoff marker, so it stays out of the
    canonical handoff-only set.
    """
    from pipeline.run_state.terminal import evict_cross_handoff_markers

    evict_cross_handoff_markers(cross_ckpt)
    cross_ckpt.pop("cfa_paused_state", None)


def _synthesise_approved_for_override(original: Any, override_note: str | None):
    """Build a verdict-only APPROVED CFA result for the operator
    override path.

    Per Phase A invariant 5 option (a): the cross finalizer's
    ``_decide_status()`` reads ``cfa_result.parsed.approved`` to
    route between done / failed. The simplest closure on the
    override semantic is to hand the finalizer a result whose
    ``parsed.verdict == "APPROVED"`` — the override marker on
    ``session["phases"]["cross_final_acceptance"]["override"]`` is
    what preserves audit / evidence forensics. The original
    REJECTED verdict + findings are also preserved on
    ``session["phases"]["cross_final_acceptance"]`` (the pause
    persisted them before the pause emit), so consumers never lose
    the reviewer's verdict.

    The synthetic ``short_summary`` records the override action so a
    reader of the synthesised result alone can still tell it apart
    from an honestly-approved reviewer result.
    """
    from pipeline.cross_project.final_acceptance import (
        CrossFinalAcceptanceResult,
    )
    from pipeline.release_parser import ParsedRelease

    original_parsed = original.parsed
    synthetic_parsed = ParsedRelease(
        verdict="APPROVED",
        ship_ready=True,
        short_summary=(
            "Operator override (CFA continue): "
            + (override_note or "no operator note")
            + f". Original reviewer verdict: {original_parsed.verdict}."
        ),
        release_blockers=(),
        verification_gaps=original_parsed.verification_gaps,
        contract_status=original_parsed.contract_status,
        source=original_parsed.source,
        parse_warnings=original_parsed.parse_warnings,
    )
    # ``source`` MUST NOT carry the original ``"parse_error"`` value:
    # the finalizer's ``_decide_status`` force-fails on
    # ``cfa.source == "parse_error"`` *independently of the verdict*
    # (finalization.py — ``_cfa_failed = not approved or source ==
    # "parse_error"``). An operator continue-override on a parse_error
    # pause would otherwise still finalize as failed, contradicting the
    # ``continue`` action we offered them. The distinct
    # ``"operator_override"`` source flows through the APPROVED branch
    # (approved=True, source != "parse_error") AND stays honest about
    # what happened — the original source is preserved in the override
    # marker (``preserved_source``) the gate stamps on the session
    # phase entry for audit.
    return CrossFinalAcceptanceResult(
        parsed=synthetic_parsed,
        source="operator_override",
        raw_output=original.raw_output,
        rendered=original.rendered,
        duration_s=original.duration_s,
        parse_error=original.parse_error,
        precondition_blockers=original.precondition_blockers,
        prompt_text=original.prompt_text,
    )


def _persist_cfa_pause(
    *,
    run_dir: Path | None,
    session: dict,
    cross_ckpt: dict,
    cfa_result: Any,
    round_n: int,
    max_rounds: int,
    cross_phase_usage: dict | None,
    terminal: bool,
) -> None:
    """Persist a fresh CFA pause: stamp checkpoint markers, build the
    payload, call :func:`apply_cross_phase_handoff_pause`. The
    session's ``cross_final_acceptance`` phase entry must already be
    populated by the caller BEFORE invoking this helper so the
    persisted ``meta.json`` carries the reviewer result alongside the
    pause payload.
    """
    from pipeline.cross_project.handoff_payloads import (
        apply_cross_phase_handoff_pause,
        build_cfa_handoff_payload,
    )

    payload = build_cfa_handoff_payload(
        round_n=round_n,
        max_rounds=max_rounds,
        cfa_result=cfa_result,
    )

    # cfa_paused_state — minimum resume state per Phase A invariant
    # so the resume path can render the pause UI without re-invoking
    # the reviewer. Mirrors what the artifacts in the payload carry,
    # but lives on the checkpoint so resume-routing has direct access
    # before reading meta.json.
    cross_ckpt["phase_handoff_kind"] = "cfa"
    cross_ckpt["cfa_paused_state"] = {
        "verdict": cfa_result.parsed.verdict,
        "summary": cfa_result.parsed.short_summary or "",
        "source": cfa_result.source,
        "blockers_count": len(cfa_result.parsed.release_blockers),
        "round": round_n,
    }

    apply_cross_phase_handoff_pause(
        run_dir=run_dir,
        session=session,
        cross_ckpt=cross_ckpt,
        payload=payload,
        cross_phase_usage=cross_phase_usage,
        terminal=terminal,
    )


def _resume_cfa_decision(
    *,
    run_dir: Path,
    session: dict,
    cross_ckpt: dict,
    output_dir: bool,
    terminal: bool,
) -> CfaGateOutcome:
    """Apply the operator's decision on a pending CFA handoff.

    Loads the decision artifact, then dispatches halt / continue /
    retry_feedback. ``retry_feedback`` is reserved for Phase A2c;
    today it raises ``NotImplementedError`` rather than silently
    treating it as continue or halt.
    """
    from pipeline.control import (
        HandoffDecisionContext,
        load_handoff_decision,
    )
    from pipeline.cross_project.checkpoint import write_cross_checkpoint
    from pipeline.cross_project.terminal import finalize_cross_terminal

    handoff_id = str(cross_ckpt.get("phase_handoff_id") or "")
    if not handoff_id:
        raise RuntimeError(
            f"Cannot resume cross run {run_dir.name!r}: checkpoint "
            "flags a CFA handoff as pending but the handoff_id field "
            "is missing."
        )

    decision = load_handoff_decision(
        HandoffDecisionContext(
            run_id=run_dir.name,
            handoff_id=handoff_id,
            runs_dir=run_dir.parent,
            cwd=None,
            missing_message=(
                f"Cannot resume cross run {run_dir.name!r}: cross "
                f"checkpoint flags CFA handoff {handoff_id!r} as "
                "pending but no decision artifact found under "
                "phase_handoff_decisions/. Call "
                "orcho_phase_handoff_decide before orcho_run_resume."
            ),
            invalid_message_prefix=(
                f"Cannot resume cross run {run_dir.name!r}: decision "
                f"artifact for CFA handoff {handoff_id!r} failed "
                "strict validation"
            ),
        ),
    )

    if decision.action == "halt":
        finalize_cross_terminal(
            run_dir=run_dir if output_dir else None,
            session=session,
            status="halted",
            halt_reason="phase_handoff_halt",
            cross_ckpt=cross_ckpt,
        )
        _clear_cfa_checkpoint_markers(cross_ckpt)
        write_cross_checkpoint(
            run_dir if output_dir else None,
            cross_ckpt,
        )
        return CfaGateOutcome(outcome="halted", cfa_result=None)

    if decision.action == "continue":
        prior_result = _load_prior_cfa_result(session)
        if prior_result is None:
            # Session was not hydrated from the previous meta.json
            # (direct/internal callers that don't thread
            # ``resumed_meta``; the CLI path does hydrate). Fall back
            # to reading the persisted entry off disk so the override
            # can still preserve the original findings.
            prior_result = _load_prior_cfa_result_from_disk(run_dir, session)
        if prior_result is None:
            raise RuntimeError(
                f"Cannot resume cross run {run_dir.name!r} with CFA "
                "continue action: no prior CFA result found on session "
                "or in meta.json. The pause path must persist the "
                "result before emitting the pause."
            )
        synthetic = _synthesise_approved_for_override(
            prior_result, decision.note,
        )
        # Build the override marker for evidence / audit. Lives under
        # the actual phase entry (per Phase A invariant — finalizer
        # reads session["phases"]["cross_final_acceptance"] for the
        # entry, NOT a top-level key). The caller re-applies this
        # marker AFTER refreshing the phase_log entry from the
        # synthesised result (``result_to_phase_log_entry`` rewrites
        # the dict and would otherwise drop the marker we set here).
        override_marker = {
            "action": "continue",
            "note": decision.note,
            "preserved_verdict": prior_result.parsed.verdict,
            "preserved_source": prior_result.source,
        }
        cfa_entry = session.setdefault("phases", {}).setdefault(
            "cross_final_acceptance", {},
        )
        if isinstance(cfa_entry, dict):
            cfa_entry["override"] = dict(override_marker)
        _clear_cfa_checkpoint_markers(cross_ckpt)
        write_cross_checkpoint(
            run_dir if output_dir else None,
            cross_ckpt,
        )
        return CfaGateOutcome(
            outcome="override_continue",
            cfa_result=synthetic,
            override_marker=override_marker,
        )

    if decision.action == "retry_feedback":
        # Phase A2c — extends the CFA reviewer API with a ``feedback``
        # parameter and re-invokes here. Until that lands, the gate
        # raises rather than silently treating retry_feedback as
        # continue or halt — either would corrupt the operator's
        # intent.
        raise NotImplementedError(
            "CFA retry_feedback is reserved for Phase A2c. The "
            "current build supports only continue / halt on a CFA "
            f"REJECTED pause (handoff {handoff_id!r})."
        )

    # load_handoff_decision narrows to four actions; the CFA gate offers
    # only continue / halt (retry_feedback reserved above), and
    # ``continue_with_waiver`` (ADR 0072) is single-project only — the CFA
    # producer never publishes it. Keep an explicit raise so the excluded
    # actions fail loudly rather than silently falling through.
    raise RuntimeError(
        f"Cannot resume cross run {run_dir.name!r}: unknown CFA "
        f"decision action {decision.action!r}."
    )


def _load_prior_cfa_result_from_disk(run_dir: Path, session: dict) -> Any | None:
    """Disk fallback for :func:`_load_prior_cfa_result`.

    When the resumed session was not hydrated from the previous
    ``meta.json`` (direct/internal callers that don't thread
    ``resumed_meta`` — the CLI path hydrates at the cross app body),
    read the persisted ``cross_final_acceptance`` entry straight off
    ``run_dir/meta.json`` and splice it into ``session["phases"]`` so
    BOTH this reconstruction AND the caller's override-preserve path
    (which keeps the original findings on the session entry) see the
    original result. Returns ``None`` when meta.json is missing /
    unreadable / lacks the entry.
    """
    import json as _json

    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    cfa_entry = meta.get("phases", {}).get("cross_final_acceptance")
    if not isinstance(cfa_entry, dict):
        return None
    # Splice the persisted entry back onto the live session so the
    # override-preserve path in the cross app body keeps the original
    # release_blockers / verification_gaps (not the synthetic empties).
    session.setdefault("phases", {})["cross_final_acceptance"] = cfa_entry
    return _load_prior_cfa_result(session)


def _load_prior_cfa_result(session: dict) -> Any | None:
    """Reconstruct the persisted CFA result from session.

    The pause path runs the reviewer first, persists the phase_log
    entry via ``result_to_phase_log_entry``, then emits the pause.
    On resume we need the original
    :class:`CrossFinalAcceptanceResult` to feed the override
    synthesiser (which preserves verification_gaps, contract_status,
    raw_output, etc.). We reconstruct it from the phase_log entry +
    the persisted ``cfa_paused_state`` checkpoint marker. The two
    sources are consistent at pause time — the phase_log carries
    the full structured rendering; the checkpoint carries the
    summary fields the gate needs to render without re-parsing.

    Returns ``None`` when the session is missing the
    ``cross_final_acceptance`` phase entry — caller raises a
    structural error in that case (the pause invariant was broken).
    """
    cfa_entry = (
        session.get("phases", {}).get("cross_final_acceptance")
    )
    if not isinstance(cfa_entry, dict):
        return None

    from pipeline.cross_project.final_acceptance import (
        CrossFinalAcceptanceResult,
    )
    from pipeline.release_parser import (
        ContractStatus,
        ParsedRelease,
        ReleaseBlocker,
        VerificationGap,
    )

    # Defensive shape coercion — phase_log entries are JSON-typed
    # dicts that may have been round-tripped through disk. We
    # reconstruct the ParsedRelease (verdict, blockers, gaps,
    # contract_status) the override synthesiser + evidence consumers
    # read.
    verdict = str(cfa_entry.get("verdict") or "REJECTED")
    summary = str(cfa_entry.get("short_summary") or "")
    blockers_raw = cfa_entry.get("release_blockers") or []
    blockers: tuple[ReleaseBlocker, ...] = ()
    if isinstance(blockers_raw, list):
        items: list[ReleaseBlocker] = []
        for b in blockers_raw:
            if not isinstance(b, dict):
                continue
            try:
                items.append(ReleaseBlocker(
                    id=str(b.get("id") or ""),
                    severity=str(b.get("severity") or "release"),
                    title=str(b.get("title") or ""),
                    body=str(b.get("body") or ""),
                    required_fix=str(b.get("required_fix") or ""),
                    why_blocks_release=str(
                        b.get("why_blocks_release") or "",
                    ),
                    file=b.get("file"),
                    line=b.get("line"),
                ))
            except (TypeError, ValueError):
                continue
        blockers = tuple(items)

    contract_status_raw = cfa_entry.get("contract_status") or {}
    if isinstance(contract_status_raw, dict):
        contract_status = ContractStatus(
            task_contract=str(
                contract_status_raw.get("task_contract") or "unknown",
            ),
            interfaces=str(
                contract_status_raw.get("interfaces") or "unknown",
            ),
            persistence=str(
                contract_status_raw.get("persistence") or "unknown",
            ),
            tests=str(contract_status_raw.get("tests") or "unknown"),
        )
    else:
        contract_status = ContractStatus(
            task_contract="unknown",
            interfaces="unknown",
            persistence="unknown",
            tests="unknown",
        )

    # Reconstruct verification_gaps from the persisted entry — the
    # phase_log shape carries them (``parsed.gaps_as_dicts()`` writes
    # ``verification_gaps``). Preserving them keeps the synthesised
    # override result faithful for any downstream consumer that reads
    # gaps off the in-memory result (the finalizer ignores gaps for
    # the done/fail decision, but evidence collectors do not).
    gaps_raw = cfa_entry.get("verification_gaps") or []
    gaps: tuple[VerificationGap, ...] = ()
    if isinstance(gaps_raw, list):
        gap_items: list[VerificationGap] = []
        for g in gaps_raw:
            if not isinstance(g, dict):
                continue
            try:
                gap_items.append(VerificationGap(
                    risk=str(g.get("risk") or ""),
                    missing_evidence=str(g.get("missing_evidence") or ""),
                    required_check=str(g.get("required_check") or ""),
                ))
            except (TypeError, ValueError):
                continue
        gaps = tuple(gap_items)

    parsed = ParsedRelease(
        verdict=verdict,
        ship_ready=bool(cfa_entry.get("ship_ready") or False),
        short_summary=summary,
        release_blockers=blockers,
        verification_gaps=gaps,
        contract_status=contract_status,
    )
    return CrossFinalAcceptanceResult(
        parsed=parsed,
        source=str(cfa_entry.get("source") or "agent"),
        raw_output=str(cfa_entry.get("raw_output") or ""),
        # ``result_to_phase_log_entry`` persists the rendered markdown
        # under the ``"output"`` key (NOT ``"rendered"``). Read
        # ``"output"`` first so a resumed override result carries the
        # original verdict markdown — otherwise the resume preview
        # prints an empty "Cross-final-acceptance verdict:" block.
        # ``"rendered"`` kept as a defensive fallback.
        rendered=str(cfa_entry.get("output") or cfa_entry.get("rendered") or ""),
        duration_s=float(cfa_entry.get("duration_s") or 0.0),
        parse_error=cfa_entry.get("parse_error"),
    )


def evaluate_cfa_gate(
    *,
    cfa_ctx: Any,
    codex: Any,
    dry_run: bool,
    run_dir: Path,
    session: dict,
    cross_ckpt: dict,
    cross_phase_usage: dict | None,
    resume_from: str | None,
    output_dir: bool,
    terminal: bool,
    max_rounds: int = CFA_DEFAULT_MAX_ROUNDS,
) -> CfaGateOutcome:
    """Gate the cross_final_acceptance phase with pause/decide semantics.

    Replaces the direct ``run_cross_final_acceptance(...)`` call in
    the cross app body. Routes between three structural branches:

    1. **Resume-decide.** When ``resume_from`` is set AND the cross
       checkpoint flags a pending handoff with
       ``phase_handoff_kind == "cfa"``, load the operator decision
       artifact and dispatch halt / continue / retry_feedback (the
       last is A2c; current build raises ``NotImplementedError``).
    2. **Fresh CFA approved.** Call :func:`run_cross_final_acceptance`;
       if ``parsed.approved`` is True, return ``approved_terminal``
       with the reviewer result.
    3. **Fresh CFA rejected.** Persist the phase_log entry, build the
       CFA handoff payload, stamp checkpoint markers, call
       :func:`apply_cross_phase_handoff_pause`. Return ``paused``;
       the caller MUST exit without finalizing.

    Args:
        cfa_ctx: prepared :class:`CrossFinalAcceptanceContext`.
        codex: cross-level reviewer runtime.
        dry_run: forwarded to :func:`run_cross_final_acceptance`.
        run_dir: cross run dir (per-alias artifacts live under it).
        session: cross session dict (mutated by the gate on
            persistence + override marker stamping).
        cross_ckpt: cross checkpoint dict (mutated with handoff
            markers on pause, cleared on resume completion).
        cross_phase_usage: accumulator passed to
            :func:`apply_cross_phase_handoff_pause` for the
            best-effort metrics.json snapshot.
        resume_from: cross run id when this is a resume (None on
            fresh runs). The gate only looks at the resume-decide
            branch when this is set.
        output_dir: when False, skip every disk write while still
            mutating session + checkpoint (in-memory dry runs).
        terminal: TERMINAL vs SILENT presentation flag forwarded to
            the pause helper (governs the operator warn line).
        max_rounds: configured upper bound on CFA pause rounds. The
            gate surfaces this in the handoff payload as
            ``loop_max_rounds`` so UI / CLI can render ``round N
            of M``.

    Returns:
        :class:`CfaGateOutcome` carrying the dispatch outcome and (on
        approved / override / retry_consumed branches) the
        :class:`CrossFinalAcceptanceResult` the caller feeds into
        :func:`finalize_cross_run`.
    """
    # ── Resume-decide branch ──────────────────────────────────────────
    if (
        resume_from
        and cross_ckpt.get("phase_handoff_pending")
        and cross_ckpt.get("phase_handoff_kind") == "cfa"
    ):
        return _resume_cfa_decision(
            run_dir=run_dir,
            session=session,
            cross_ckpt=cross_ckpt,
            output_dir=output_dir,
            terminal=terminal,
        )

    if resume_from:
        cached = load_completed_cfa_cache(session, run_dir)
        if cached is not None:
            return CfaGateOutcome(outcome="cached_terminal", cfa_result=cached)

    # ── Fresh path: run the reviewer ──────────────────────────────────
    from pipeline.cross_project.final_acceptance import (
        result_to_phase_log_entry,
        run_cross_final_acceptance,
    )

    cfa_result = run_cross_final_acceptance(
        cfa_ctx, codex=codex, dry_run=dry_run,
    )

    if cfa_result.parsed.approved:
        return CfaGateOutcome(
            outcome="approved_terminal", cfa_result=cfa_result,
        )

    # ── Fresh REJECTED → pause ────────────────────────────────────────
    # Persist the phase_log entry BEFORE the pause so meta.json carries
    # both the reviewer result and the pause payload — resume reads the
    # phase_log entry to reconstruct the original result for the
    # override-continue branch.
    session.setdefault("phases", {})["cross_final_acceptance"] = (
        result_to_phase_log_entry(cfa_result)
    )

    _persist_cfa_pause(
        run_dir=run_dir if output_dir else None,
        session=session,
        cross_ckpt=cross_ckpt,
        cfa_result=cfa_result,
        round_n=1,
        max_rounds=max_rounds,
        cross_phase_usage=cross_phase_usage,
        terminal=terminal,
    )
    return CfaGateOutcome(outcome="paused", cfa_result=cfa_result)
