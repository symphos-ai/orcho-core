"""pipeline/session_adapters.py — Per-phase session-dict shape writers (Phase 3).

M12 trace foundation: adapters surface ``state.phase_log[phase]
["prompt_render"]`` (the M7+ session-aware render metadata —
render_mode, session_key, selected/omitted part keys,
prefix/payload hashes, wire char count) into the persisted
session entry. M12 observability persistence can build durable
storage on top of this without re-deriving wire-shape information.

The closing-gate ``FinalAcceptanceAdapter`` intentionally does NOT
carry ``prompt_render`` because ``_phase_final_acceptance`` does
not route through ``_session_aware_invoke`` — the M9 design pinned
verdict isolation over prompt-size win on the closing gate, so no
trace is emitted there to forward.


Decouple **handler logic** from **session serialization shape**.
Handlers stay naive (just stuff data into ``state.phase_log[name]``).
Adapters know the canonical session shape and translate state →
``session["phases"][name]``.

Per-phase append helpers live behind a registry-keyed Protocol so
customer plugins can override the session shape per phase via the
``orcho.session_adapters`` entry_points group.

NB on scope: this is **session-dict shape normalization** only — NOT a
universal external-runner integration bus (see
``docs/adr/0006-session-adapter-not-runner-bus.md``). If we ever need
to import forge-session / codex-session / claude-session transcript
shapes for resume / audit, that will be a separate concept
(``ExternalRuntimeAdapter`` / ``TranscriptImporter``), not a
SessionAdapter scope expansion.

Phase 3 acceptance: zero diff in ``session.json`` final shape vs the
legacy ``_append_*`` path. Snapshot tests pin the contract.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pipeline.runtime import PipelineState

# ── Protocol ─────────────────────────────────────────────────────────────────

@runtime_checkable
class SessionAdapter(Protocol):
    """Per-phase session-dict shape writer.

    Reads ``state.phase_log[name]`` (plus other ``state`` fields the
    handler doesn't put into phase_log: ``last_critique``,
    ``parsed_plan``, etc.), writes the canonical shape into
    ``session["phases"][name]``.

    ``round_n`` (optional) is the 1-based loop iteration when the phase
    runs inside a LoopStep — orchestrator passes it explicitly.
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None: ...


# ── Registry ─────────────────────────────────────────────────────────────────

class SessionAdapterRegistry:
    """Map phase name → SessionAdapter. Plugin extension via the future
    ``orcho.session_adapters`` entry_points group (Phase 7).

    Lookup is permissive: ``get_or_none`` returns None for unregistered
    phases so the orchestrator can choose to fall through silently
    (legacy phases that never had a session shape contract).
    """

    def __init__(self) -> None:
        self._handlers: dict[str, SessionAdapter] = {}

    def register(self, name: str, adapter: SessionAdapter) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("session adapter name must be a non-empty string")
        self._handlers[name.strip()] = adapter

    def get(self, name: str) -> SessionAdapter:
        if name not in self._handlers:
            raise KeyError(
                f"Unknown session adapter {name!r}. "
                f"Registered: {sorted(self._handlers)}"
            )
        return self._handlers[name]

    def get_or_none(self, name: str) -> SessionAdapter | None:
        return self._handlers.get(name)

    def has(self, name: str) -> bool:
        return name in self._handlers

    def names(self) -> list[str]:
        return sorted(self._handlers)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _copy_prompt_render(log: dict[str, Any], entry: dict[str, Any]) -> None:
    """Copy M7+ render metadata from a phase_log entry into the
    session entry when the handler recorded it.

    The phase_log key ``prompt_render`` is stamped by
    :func:`pipeline.phases.builtin._session_aware_invoke` and holds:
    ``render_mode``, ``session_split``, ``session_key`` (dict view of
    :class:`pipeline.prompts.session.PhysicalSessionKey`),
    ``selected_part_keys``, ``omitted_part_keys``, ``prefix_hash``,
    ``payload_hash``, ``wire_chars``. Adapters copy it verbatim so
    M12 persistence and operator-facing trace consumers see the same
    shape across every phase that participates in session-aware
    rendering.
    """
    pr = log.get("prompt_render")
    if pr is not None:
        entry["prompt_render"] = pr


def _copy_context_growth(log: dict[str, Any], entry: dict[str, Any]) -> None:
    """Copy M14.1 context-growth metadata from a phase_log entry.

    ADR 0029 §"Evidence Model" sibling of ``prompt_render``. The
    phase_log key ``context_growth`` is stamped by
    :func:`pipeline.phases.builtin._session_aware_invoke` and holds
    per-invocation token estimates, lifecycle attribution
    (``kind`` / ``trigger`` / ``phase`` / ``round`` /
    ``surface_id``), correlation back to the prompt_render record
    (``render_mode`` / ``prefix_hash`` / ``payload_hash`` /
    ``wire_chars``), and reserved placeholders for later M14
    slices (``tool_use_count`` / ``cleared_tokens`` /
    ``summary_tokens`` / ``artifact_refs``). M14.1 is
    observe-only — the placeholders stay at safe defaults until
    M14.3+ enable the matching primitive.
    """
    cg = log.get("context_growth")
    if cg is not None:
        entry["context_growth"] = cg


def _copy_context_clearing(log: dict[str, Any], entry: dict[str, Any]) -> None:
    """Copy M14.3 context-clearing eligibility evidence from a
    phase_log entry.

    ADR 0029 §"Tool-result clearing" sibling of ``context_growth``.
    The phase_log key ``context_clearing`` is stamped by
    :func:`pipeline.phases.builtin._session_aware_invoke` and holds
    eligibility evidence: per-part classification via the M14.2
    taxonomy, ``clearable_tokens`` sum, ``clearable_part_ids`` /
    ``retained_part_ids`` lists, and ``class_counts`` breakdown.
    M14.3 is observe-only — ``kind`` is ``"eligible_tool_results"``
    and ``cleared_tokens`` stays at zero until a runtime clearing
    API ships.
    """
    cc = log.get("context_clearing")
    if cc is not None:
        entry["context_clearing"] = cc


def _copy_context_pressure(log: dict[str, Any], entry: dict[str, Any]) -> None:
    """Copy M14.4 context-pressure telemetry from a phase_log entry.

    ADR 0029 §"Context pressure" sibling of ``context_growth`` and
    ``context_clearing``. The phase_log key ``context_pressure`` is
    stamped by :func:`pipeline.phases.builtin._session_aware_invoke`
    and holds the source-attributed window-fill snapshot
    (``context_source``, ``context_window_tokens``,
    ``context_used_tokens``, ``context_remaining_tokens``,
    ``context_fill_ratio``, ``trigger_source``) plus the
    ``prefix_hash`` / ``payload_hash`` / ``wire_chars`` correlation
    back to the matching ``prompt_render`` record. M14.4 is
    observe-only — no auto-compaction; the protected
    ``coding_agent_compaction`` contract defines what the agent
    must preserve when an external trigger fires.
    """
    cp = log.get("context_pressure")
    if cp is not None:
        entry["context_pressure"] = cp


def _copy_runtime_session(log: dict[str, Any], entry: dict[str, Any]) -> None:
    """Copy per-phase runtime session ids from a phase_log entry.

    Promotes the agent's resume forensic surface — ``session_id`` (post-
    invoke captured id), ``continue_session`` (did a ``--resume`` flag
    actually go out — derived from ``_last_resumed_session_id`` upstream),
    and ``followup_parent_session_id`` (when the invoke consumed a
    follow-up seed, the parent run's sid that was passed to ``--resume``).

    Phase handlers (`pipeline/phases/builtin/` / `phases/adapters.py`)
    stuff these into either ``log["meta"]`` (plan / implement /
    final_acceptance / hypothesis) or the top of ``log`` itself
    (validate_plan / review_changes / repair_changes). We accept both
    shapes — ``meta`` wins if present.

    Optional fields: any key that is ``None`` or absent is skipped so
    fresh runs and pre-follow-up runs keep a stable session.json shape
    (no stray ``followup_parent_session_id: null`` rows).
    """
    runtime_meta = log.get("meta") if isinstance(log.get("meta"), dict) else None
    source: dict[str, Any] = runtime_meta if runtime_meta else log
    for key in ("session_id", "continue_session", "followup_parent_session_id"):
        value = source.get(key)
        if value is None:
            continue
        entry[key] = value


def _copy_runtime_compaction(log: dict[str, Any], entry: dict[str, Any]) -> None:
    """Copy M14.4+ runtime auto-compaction evidence from a phase_log entry.

    ADR 0029 §"Evidence Model" sibling of ``context_pressure``. The
    phase_log key ``runtime_compaction`` is stamped by
    :func:`pipeline.phases.builtin._session_aware_invoke` **only when**
    the agent exposes ``last_runtime_compaction_event`` — no runtime
    exposes it today, so under typical runs this key is absent and
    the adapter is a no-op. When a runtime ships the signal (event
    hook / response-header / log line — whichever surfaces first),
    the writer records the event and this helper promotes it into
    the session entry so
    :func:`pipeline.observability.runtime_compaction.extract_runtime_compaction_traces`
    can read it.

    Observe-only invariants: the record describes what the runtime
    claims to have compacted (kind / trigger / pre/post tokens /
    summary tokens / preserved slots / artifact refs). Orcho never
    triggers compaction itself; the recovery-contract validator
    joins this record against the protected
    ``coding_agent_compaction`` preserve-list.
    """
    rc = log.get("runtime_compaction")
    if rc is not None:
        entry["runtime_compaction"] = rc


# ── Built-in adapters ────────────────────────────────────────────────────────

class PlanAdapter:
    """Promote ``state.phase_log['plan']`` into ``session['phases']['plan']``
    (a list of per-round attempts).

    Reads:
      * output, meta — handler-set
      * attempt — explicit round number (or use ``round_n``)
      * state.parsed_plan.file_paths / total_atomic_tasks
      * existing_files, missing_files — handler-set path validation result
      * codemap_injected, hypothesis_injected
      * replan_critique (optional — only when round > 1)
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None:
        log = state.phase_log.get("plan", {}) or {}
        attempt = log.get("attempt", round_n if round_n is not None else 1)
        parsed_plan = getattr(state, "parsed_plan", None)
        parsed_file_paths = (
            list(getattr(parsed_plan, "file_paths", []) or [])
            if parsed_plan is not None
            else []
        )
        total_atomic_tasks = (
            int(getattr(parsed_plan, "total_atomic_tasks", 0) or 0)
            if parsed_plan is not None
            else 0
        )
        entry: dict[str, Any] = {
            "attempt":             attempt,
            "output":              log.get("output", ""),
            "codemap_injected":    bool(log.get("codemap_injected", False)),
            "hypothesis_injected": bool(log.get("hypothesis_injected", False)),
            "parsed_file_paths":   parsed_file_paths,
            "existing_files":      list(log.get("existing_files", []) or []),
            "missing_files":       list(log.get("missing_files", []) or []),
            "total_atomic_tasks":  total_atomic_tasks,
        }
        replan_critique = log.get("replan_critique")
        if replan_critique:
            entry["replan_critique"] = replan_critique
        human_feedback = log.get("human_feedback")
        if human_feedback:
            entry["human_feedback"] = human_feedback
        meta = log.get("meta") or {}
        if isinstance(meta, dict) and meta.get("human_directed"):
            entry.setdefault("meta", {})["human_directed"] = True
        # REA-1: when the PLAN handler halts on a malformed typed
        # contract or unparseable plan, surface the parse error in the
        # session shape so ``orcho evidence`` (REA-3) can render *why*
        # the run stopped before implement. The handler stuffs
        # ``parse_error`` into ``state.phase_log['plan']`` right before
        # ``state.stop()``.
        parse_error = log.get("parse_error") or log.get("schema_error")
        if parse_error:
            entry["parse_error"] = parse_error
        _copy_runtime_session(log, entry)
        _copy_prompt_render(log, entry)
        _copy_context_growth(log, entry)
        _copy_context_clearing(log, entry)
        _copy_context_pressure(log, entry)
        _copy_runtime_compaction(log, entry)
        session.setdefault("phases", {}).setdefault("plan", []).append(entry)


class ValidatePlanAdapter:
    """Promote ``state.phase_log['validate_plan']`` into the per-attempt list.

    Reads:
      * output, approved — handler-set
      * plan_file, reviewer_provider — orchestrator-stuffed
      * state.last_critique — falls through into ``critique`` field
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None:
        log = state.phase_log.get("validate_plan", {}) or {}
        attempt = log.get("attempt", round_n if round_n is not None else 1)
        # ``output`` is the Orcho-rendered review markdown; the model's raw
        # JSON contract is captured separately as ``raw_output``. Surface the
        # raw model response in ``raw_response`` so MCP / dashboard consumers
        # can re-validate the contract if needed (REA-3.5).
        raw_output = log.get("raw_output") or log.get("output", "")
        entry: dict[str, Any] = {
            "attempt":      attempt,
            "plan_file":    log.get("plan_file", ""),
            "raw_response": raw_output,
            "critique":     (
                log.get("critique") or state.last_critique or log.get("output", "")
            ),
            "approved":     bool(log.get("approved", False)),
            "reviewer_provider":  log.get("reviewer_provider", ""),
        }
        # Additive structured contract fields (REA-3.5). Optional so legacy
        # consumers / fixtures without the contract still parse cleanly.
        verdict = log.get("verdict")
        if verdict is not None:
            entry["verdict"] = verdict
        short_summary = log.get("short_summary")
        if short_summary is not None:
            entry["short_summary"] = short_summary
        findings = log.get("findings")
        if findings is not None:
            entry["findings"] = findings
        parse_error = log.get("parse_error")
        if parse_error:
            entry["parse_error"] = parse_error
        _copy_runtime_session(log, entry)
        _copy_prompt_render(log, entry)
        _copy_context_growth(log, entry)
        _copy_context_clearing(log, entry)
        _copy_context_pressure(log, entry)
        _copy_runtime_compaction(log, entry)
        session.setdefault("phases", {}).setdefault("validate_plan", []).append(entry)


class BuildAdapter:
    """Promote ``state.phase_log['implement']`` into the implement dict.

    Reads:
      * output — handler-set
      * progress (optional dict {completed, total}) — handler-stuffed
        when parsed_plan exists
      * test_result (dict) — orchestrator-stuffed from QualityGateRunner
        (Phase 4) / legacy ``last_test_result`` consumption
      * meta (optional dict) — public executor metadata, including runtime
        session forensics such as ``session_id`` / ``continue_session``.
      * dag_result (optional dict) — legacy Phase 5e-5 substep 6d: forwarded
        when present so dag-build profiles surface the per-subtask
        completion / failure / skip lists
      * implementation_receipts (optional list) — policy-owned
        `subtask_dag` delivery receipts.
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None:
        log = state.phase_log.get("implement", {}) or {}
        entry: dict[str, Any] = {"output": log.get("output", "")}
        progress = log.get("progress")
        if progress is not None:
            entry["progress"] = progress
        test_result = log.get("test_result")
        if test_result is not None:
            entry["test_result"] = test_result
        # Surface executor telemetry. The implement handler writes
        # session plumbing keys into ``meta``; legacy versions filtered
        # ``session_id`` / ``continue_session`` out, but the follow-up
        # feature (resume runtime session from parent) needs the agent's
        # post-invoke session_id and a forensic record of any seeded
        # parent sid. Keep ``meta`` as-is; nullish values are stripped
        # by the writers upstream.
        meta = log.get("meta") or {}
        if isinstance(meta, dict) and meta:
            entry["meta"] = dict(meta)
        dag_result = log.get("dag_result")
        if dag_result is not None:
            entry["dag_result"] = dag_result
        implementation_receipts = log.get("implementation_receipts")
        if implementation_receipts is not None:
            entry["implementation_receipts"] = implementation_receipts
        # ADR 0073: implement substance-repair / handoff delivery provenance.
        # ``delivery_status`` (clean | repaired | waived | incomplete) is the
        # authoritative outcome; ``delivery_waived`` / ``waiver_id`` / ``action``
        # describe an auto- or operator-waiver. ``delivery_clean`` is the legacy
        # boolean preserved for existing readers. ``incomplete_subtasks`` /
        # ``missing_subtask_receipts`` / ``attestation_incomplete`` are the
        # durable record of WHICH subtasks blocked delivery — without them a
        # waived (incl. missing-receipt) delivery would lose which ids were
        # accepted. Each is copied only when the implement handler recorded it,
        # mirroring the conditional copies above.
        for _delivery_key in (
            "delivery_status",
            "delivery_waived",
            "waiver_id",
            "action",
            "delivery_clean",
            "incomplete_subtasks",
            "missing_subtask_receipts",
            "attestation_incomplete",
        ):
            _delivery_value = log.get(_delivery_key)
            if _delivery_value is not None:
                entry[_delivery_key] = _delivery_value
        _copy_prompt_render(log, entry)
        _copy_context_growth(log, entry)
        _copy_context_clearing(log, entry)
        _copy_context_pressure(log, entry)
        _copy_runtime_compaction(log, entry)
        session.setdefault("phases", {})["implement"] = entry


class RoundAdapter:
    """Append a round dict to ``session['phases']['rounds']``.

    Drives the review_changes ↔ repair_changes loop session shape.
    ``round_n`` is required (the loop-driver explicitly passes it). Two
    execution paths:

    1. Critique-empty short-circuit (review passed clean): only
       ``round`` + ``critique`` are written, no repair_changes entry. The
       orchestrator passes ``round_n`` and ``state.last_critique``,
       stuffing ``state.phase_log['rounds_pending']`` with just the
       critique.
    2. Full round (review_changes + repair_changes done): orchestrator stuffs
       the full per-round dict into ``state.phase_log['rounds_pending']``
       with repair_output, repair_model, session_mode, split review/repair
       session ids, legacy session_id alias, and test_result.

    The contract: optional fields (repair_output, repair_model, session_mode,
    split session ids, session_id, test_result) are **omitted** when None to keep
    ``"repair_output" not in rounds[0]`` for clean early-exits.
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None:
        if round_n is None:
            raise ValueError(
                "RoundAdapter requires explicit round_n (loop step number)"
            )
        pending = state.phase_log.get("rounds_pending", {}) or {}
        # Phase 5d step 1: review handler signals "no uncommitted changes"
        # (or any other no-round-to-record condition) by stuffing
        # ``_skip_adapter: True``. Mirrors legacy ``run_review_fix_loop``
        # early-break behaviour where the loop body never executed →
        # no round entry was appended. Ensure ``rounds`` key exists
        # (legacy pre-initialised it before the loop, even when the loop
        # was a no-op).
        session.setdefault("phases", {}).setdefault("rounds", [])
        if pending.get("_skip_adapter"):
            return
        critique = pending.get("critique", state.last_critique or "")
        entry: dict[str, Any] = {"round": round_n, "critique": critique}
        optionals = {
            "repair_output":   pending.get("repair_output"),
            "repair_model":    pending.get("repair_model"),
            "session_mode":    pending.get("session_mode"),
            "session_mode_reason": pending.get("session_mode_reason"),
            "session_mode_context_pressure": (
                pending.get("session_mode_context_pressure")
            ),
            # Split reviewer / repair runtime session ids so a follow-up
            # extractor can pull the right side per role. Old callers
            # that wrote a single ``session_id`` (de facto the repair
            # side) keep working via the alias entry below.
            "review_session_id":       pending.get("review_session_id"),
            "review_continue_session": pending.get("review_continue_session"),
            "followup_parent_review_session_id":
                pending.get("followup_parent_review_session_id"),
            "repair_session_id":       pending.get("repair_session_id"),
            "repair_continue_session": pending.get("repair_continue_session"),
            "followup_parent_repair_session_id":
                pending.get("followup_parent_repair_session_id"),
            "repair_receipt": pending.get("repair_receipt"),
            # Backcompat alias: pre-split readers expect a single
            # ``session_id``. Prefer the explicit repair_session_id
            # (matches legacy semantics — the repair side was what
            # callers stuffed), fall back to whatever the caller put in
            # the legacy slot.
            "session_id":      (
                pending.get("repair_session_id")
                or pending.get("session_id")
            ),
            "test_result":  pending.get("test_result"),
        }
        for k, v in optionals.items():
            if v is not None:
                entry[k] = v
        # M12 trace foundation: surface the M11.5-attributed
        # ``prompt_render`` from each side of the loop. The reviewer
        # writes under ``phase_log["review_changes"]["prompt_render"]``;
        # CHAIN repair attributes its trace to
        # ``phase_log["repair_changes"]["prompt_render"]`` per the
        # M11.5 ``trace_phase`` fix. Both copies are namespaced under
        # ``prompt_render_*`` so M12 persistence can attribute cost
        # and wire shape per side of the round.
        review_log = state.phase_log.get("review_changes", {}) or {}
        repair_log = state.phase_log.get("repair_changes", {}) or {}
        # M12: prompt_render per side.
        review_pr = review_log.get("prompt_render") if isinstance(review_log, dict) else None
        repair_pr = repair_log.get("prompt_render") if isinstance(repair_log, dict) else None
        if review_pr is not None:
            entry["prompt_render_review"] = review_pr
        if repair_pr is not None:
            entry["prompt_render_repair"] = repair_pr
        # ADR 0029 / M14.1 catch-up: round-side context_growth split.
        # M14.1 deferred the round-side promotion to M14.3 because
        # the per-side context evidence becomes useful once
        # eligibility / clearing also runs per side. Surface both
        # sides here under the same ``_review`` / ``_repair`` suffix
        # convention prompt_render uses.
        review_cg = review_log.get("context_growth") if isinstance(review_log, dict) else None
        repair_cg = repair_log.get("context_growth") if isinstance(repair_log, dict) else None
        if review_cg is not None:
            entry["context_growth_review"] = review_cg
        if repair_cg is not None:
            entry["context_growth_repair"] = repair_cg
        # ADR 0029 / M14.3: round-side context_clearing split.
        # Eligibility evidence per round side. Observe-only — no
        # actual clearing happens; the records describe what would
        # be safe to clear if a runtime clearing API were active.
        review_cc = review_log.get("context_clearing") if isinstance(review_log, dict) else None
        repair_cc = repair_log.get("context_clearing") if isinstance(repair_log, dict) else None
        if review_cc is not None:
            entry["context_clearing_review"] = review_cc
        if repair_cc is not None:
            entry["context_clearing_repair"] = repair_cc
        # ADR 0029 / M14.4: round-side context_pressure split.
        # Window-fill telemetry per round side. Observe-only — the
        # protected ``coding_agent_compaction`` contract is the
        # response surface; no auto-compaction runs from this signal.
        review_cp = review_log.get("context_pressure") if isinstance(review_log, dict) else None
        repair_cp = repair_log.get("context_pressure") if isinstance(repair_log, dict) else None
        if review_cp is not None:
            entry["context_pressure_review"] = review_cp
        if repair_cp is not None:
            entry["context_pressure_repair"] = repair_cp
        # ADR 0029 / M14.4+: round-side runtime auto-compaction
        # split. Only present when the runtime exposed an
        # ``last_runtime_compaction_event`` on the corresponding
        # side of the loop. Observe-only — the round entry records
        # that the runtime compacted itself, not anything Orcho
        # did.
        review_rc = review_log.get("runtime_compaction") if isinstance(review_log, dict) else None
        repair_rc = repair_log.get("runtime_compaction") if isinstance(repair_log, dict) else None
        if review_rc is not None:
            entry["runtime_compaction_review"] = review_rc
        if repair_rc is not None:
            entry["runtime_compaction_repair"] = repair_rc
        session["phases"]["rounds"].append(entry)


class FinalAcceptanceAdapter:
    """Promote ``state.phase_log['final_acceptance']`` into the final
    acceptance dict.

    ADR 0025 Phase 1: ``final_acceptance`` now emits the release-gate
    contract; the handler writes a dual-shape phase_log entry (review-
    shape mirror + release fields). This adapter persists BOTH shapes
    into ``session["phases"]["final_acceptance"]`` so:

      * Existing consumers (Web phase card, MCP ``orcho_run_evidence``,
        acceptance fixtures, golden snapshots) keep reading
        ``verdict`` / ``short_summary`` / ``findings``.
      * Release-aware consumers (evidence collector ``release_summary``
        section, future SDK surfaces) read ``ship_ready`` /
        ``release_blockers`` / ``verification_gaps`` /
        ``contract_status``.

    Every field is optional in the phase_log so legacy paths that wrote
    only the review-shape mirror still produce a valid session entry.
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None:
        log = state.phase_log.get("final_acceptance", {}) or {}
        rendered = log.get("output", "")
        entry: dict[str, Any] = {"critique": rendered}
        raw_output = log.get("raw_output")
        if raw_output:
            entry["raw_response"] = raw_output
        # Review-shape mirror + release-shape fields. Each is copied
        # only when present so the adapter degrades cleanly for legacy
        # phase_logs that don't carry the release surface.
        for key in (
            # Review-shape mirror (compat surface).
            "approved", "verdict", "short_summary", "findings", "parse_error",
            # Release-shape (ADR 0025 Phase 1).
            "ship_ready", "release_blockers", "verification_gaps",
            "contract_status",
            # F2 scope-expansion durable evidence: the canonical
            # phase_log['final_acceptance']['scope_expansion'] dict is projected
            # under the same key so the DONE/evidence summary reads it from the
            # single session path. Copied only when present (legacy-safe).
            "scope_expansion",
            # ADR 0112 §5 (increment D): the mode-projected sanction route
            # (forces_rejected / needs_phase_handoff / alert_paths) rides
            # alongside the classifier fact so persisted session/meta keep the
            # route an operator/DONE/evidence surface reads after phase-end, not
            # just the in-memory phase_log. Copied only when present (legacy-safe).
            "scope_expansion_sanction",
            # No-diff release gate surface.
            "skipped", "review_target", "diff", "no_change_outcome",
        ):
            value = log.get(key)
            if value is not None:
                entry[key] = value
        _copy_runtime_session(log, entry)
        session.setdefault("phases", {})["final_acceptance"] = entry


class CorrectionTriageAdapter:
    """Promote ``state.phase_log['correction_triage']`` into the session.

    ADR 0085: the correction profile opens with a read-only triage pass
    that classifies how to close the recorded release blockers. The
    handler writes the structured record ``{kind, summary, allowed_scope,
    required_checks, blockers}`` into ``phase_log``; this adapter persists
    it (plus the fail-fast ``halted`` / ``reason`` markers and any
    ``parse_warnings``) into ``session['phases']['correction_triage']`` so
    evidence and logs can read the triage verdict. Each field is copied
    only when present so a fresh phase_log degrades cleanly.
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None:
        log = state.phase_log.get("correction_triage", {}) or {}
        entry: dict[str, Any] = {
            "kind":            log.get("kind", "blocked"),
            "summary":         log.get("summary", ""),
            "allowed_scope":   list(log.get("allowed_scope", []) or []),
            "required_checks": list(log.get("required_checks", []) or []),
            "blockers":        list(log.get("blockers", []) or []),
        }
        for key in ("halted", "reason", "parse_warnings", "raw_output"):
            value = log.get(key)
            if value is not None:
                entry[key] = value
        _copy_runtime_session(log, entry)
        session.setdefault("phases", {})["correction_triage"] = entry


class HypothesisAdapter:
    """Promote hypothesis loop result into ``session['phases']['hypothesis']``.

    Reads:
      * state.research_hypothesis (bool) — orchestrator-set after
        maybe_run_hypothesis returns
      * state.phase_log['hypothesis']['attempts'] — orchestrator-stuffed
    """

    def write(
        self,
        phase_name: str,
        state: PipelineState,
        session: dict,
        *,
        round_n: int | None = None,
    ) -> None:
        log = state.phase_log.get("hypothesis", {}) or {}
        attempts = log.get("attempts", [])
        if not attempts:
            return  # no hypothesis loop ran (legacy: skip session entry)
        # Read approved verdict from phase_log (orchestrator stuffs it
        # alongside attempts). Falls back to state.research_hypothesis
        # for the legacy attribute path if a caller still wires it that
        # way — keeps the adapter robust to either entry point.
        approved = log.get("approved")
        if approved is None:
            approved = bool(getattr(state, "research_hypothesis", None))
        session.setdefault("phases", {})["hypothesis"] = {
            "enabled":  True,
            "approved": bool(approved),
            "attempts": list(attempts),
        }


# ── Default registry singleton ───────────────────────────────────────────────

_DEFAULT_REGISTRY: SessionAdapterRegistry | None = None


def default_session_adapter_registry() -> SessionAdapterRegistry:
    """Singleton built-in registry with all six adapters wired.

    Phase 7c grows this through the ``orcho.session_adapters``
    entry_points group when third-party plugins ship custom
    session-shape writers for phases they introduce. Plugin entries
    with names matching built-ins replace them.

    NB: ``RoundAdapter`` is registered under TWO names:
      * ``"rounds"`` — the legacy explicit-call path used by
        ``_PipelineRun.run_review_fix_loop``.
      * ``"repair_changes"`` — the v2 dispatch path, where
        ``_on_phase_end`` auto-fires the adapter after the
        repair_changes phase completes (review_changes precedes
        repair_changes in the loop, and the round entry composes data
        from both review's critique and the repair output).
    Same instance shared so adapter state — if any — stays consistent
    across both paths. RoundAdapter is currently stateless so the dual
    registration is purely a name alias.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        reg = SessionAdapterRegistry()
        round_adapter = RoundAdapter()
        reg.register("plan",             PlanAdapter())
        reg.register("validate_plan",    ValidatePlanAdapter())
        reg.register("implement",        BuildAdapter())
        reg.register("rounds",           round_adapter)
        reg.register("repair_changes",   round_adapter)  # v2 dispatch alias
        reg.register("final_acceptance", FinalAcceptanceAdapter())
        reg.register("correction_triage", CorrectionTriageAdapter())
        reg.register("hypothesis",       HypothesisAdapter())
        # Phase 7c: customer plugins ship additional adapters via
        # ``orcho.session_adapters`` entry_points (e.g. a
        # ``compliance_check`` adapter that promotes audit-trail data
        # into the session shape). Load failures are logged per-entry.
        from pipeline.entry_points import register_entry_points
        register_entry_points(reg, "orcho.session_adapters")
        _DEFAULT_REGISTRY = reg
    return _DEFAULT_REGISTRY
