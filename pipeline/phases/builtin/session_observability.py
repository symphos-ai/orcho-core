# SPDX-License-Identifier: Apache-2.0
"""Post-invoke observability stampers for session-aware invocation.

ADR 0029 (M12 / M14.x) writes a family of observe-only trace records to
``state.phase_log[trace_slot]`` after every ``agent.invoke``: the M12
``prompt_render`` record plus the M14.1/M14.3/M14.4/M14.4+ context
siblings (growth, clearing eligibility, pressure, runtime compaction) and
the M14.4.4 live card. Each stamper here owns exactly one record and is
independently testable.

The caller (``session_invoke._session_aware_invoke``) computes the shared
inputs once and hands them over as a private :class:`_TraceInputs`. This
module never imports from ``session_invoke`` — the dependency is strictly
one-way — so the session-key dict is resolved by the caller and passed in
(``_TraceInputs.session_key``) rather than recomputed here.

Asymmetry preserved verbatim from the monolithic handler: the M12
``prompt_render`` record stamps ``round`` from the resolved loop-round
counter (``round_n``); the ADR-0029 context siblings stamp ``round`` from
``state.extras['loop_round']`` (``loop_round``). The two can differ and
must not be unified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipeline.observability.invocation_outcome import AgentInvocationOutcome


@dataclass(frozen=True, slots=True)
class _TraceInputs:
    """Shared writer-side inputs for the post-invoke stampers.

    Private and constructed only inside ``session_invoke``; not a public
    contract and never re-exported from the package facade.
    """

    trace_slot: str
    phase_key: str
    round_n: int | None
    loop_round: int | None
    render_mode: str
    session_split: str
    session_key: dict[str, str] | None
    provider_session_id: str | None
    prefix_hash: str
    payload_hash: str
    wire_chars: int


def stamp_prompt_render(
    entry: dict[str, Any],
    trace: _TraceInputs,
    *,
    part_ids: tuple[str, ...],
    selected: tuple[str, ...],
    omitted: tuple[str, ...],
    delta_dropped: tuple[str, ...],
    continue_session: bool,
) -> None:
    """M12 render record: what the selector resolved and what hit the wire."""
    entry["prompt_render"] = {
        "render_mode": trace.render_mode,
        "session_split": trace.session_split,
        "session_key": trace.session_key,
        "provider_session_id": trace.provider_session_id,
        "part_ids": list(part_ids),
        "selected_part_keys": list(selected),
        "omitted_part_keys": list(omitted),
        # ADR 0026 delta drop: parts the caller asked to omit from the
        # wire on a resumed turn because the runtime already holds them in
        # history (e.g. the original task on replan). On the source side
        # ``part_ids`` still lists them; they are absent from
        # ``selected_part_keys`` (not on the wire). Completeness invariant:
        # part_ids ⊇ selected ∪ omitted ∪ delta_dropped.
        "delta_dropped_part_keys": list(delta_dropped),
        "prefix_hash": trace.prefix_hash,
        "payload_hash": trace.payload_hash,
        "wire_chars": trace.wire_chars,
        # Writer-stamped attribution: ``phase_key`` is the session-key
        # phase, which differs from the trace slot for CHAIN
        # repair_changes (phase="implement", trace_phase="repair_changes").
        # ``round`` is the loop counter at invoke time. ``continue_session``
        # reflects whether this call resumed the provider session — needed
        # to distinguish round-1 fresh sessions from round-N resumed
        # sessions without cross-referencing runner.log.
        "phase_key": trace.phase_key,
        "round": trace.round_n,
        "continue_session": bool(continue_session),
    }


def stamp_context_growth(
    entry: dict[str, Any],
    trace: _TraceInputs,
    *,
    agent: Any,
) -> None:
    """ADR 0029 / M14.1: observe-only per-invocation context-growth record.

    Lifecycle primitive fields (cleared_tokens, summary_tokens,
    artifact_refs) stay at safe defaults — M14.3+ populate them when
    clearing / compaction / memory actually run. Stamped after invoke so
    the runtime has updated its ``last_estimated_tokens_*`` slots.
    """
    entry["context_growth"] = {
        "kind": "phase_invocation",
        "trigger": "phase_invocation",
        "phase": trace.trace_slot,
        "round": trace.loop_round,
        "surface_id": None,
        "render_mode": trace.render_mode,
        "prefix_hash": trace.prefix_hash,
        "payload_hash": trace.payload_hash,
        "wire_chars": trace.wire_chars,
        "input_tokens_estimate": getattr(
            agent, "last_estimated_tokens_in", None,
        ),
        "output_tokens_estimate": getattr(
            agent, "last_estimated_tokens_out", None,
        ),
        "tool_use_count": int(
            getattr(agent, "last_tool_use_count", 0) or 0,
        ),
        "cleared_tokens": 0,
        "summary_tokens": 0,
        "artifact_refs": [],
    }


def stamp_context_clearing(
    entry: dict[str, Any],
    trace: _TraceInputs,
    *,
    source_envelope: Any,
) -> None:
    """ADR 0029 / M14.3: observe-only tool-result clearing eligibility.

    Classifies every envelope part through the M14.2 taxonomy, sums token
    estimates for the clearable classes (RE_FETCHABLE + PERSISTED_ARTIFACT),
    and records which part ids would be eligible if a runtime clearing API
    were active. Nothing is actually cleared — Orcho's runtimes do not
    expose a clearing surface yet.
    """
    from core.observability.metrics import estimate_tokens as _estimate
    from pipeline.observability.output_class import (
        OutputClass,
        classify_prompt_part,
    )
    from pipeline.prompts.session import part_session_key as _part_key

    _clearable_classes = {
        OutputClass.RE_FETCHABLE,
        OutputClass.PERSISTED_ARTIFACT,
    }
    _clearable_part_ids: list[str] = []
    _retained_part_ids: list[str] = []
    _clearable_tokens = 0
    _class_counts = {c.value: 0 for c in OutputClass}
    for part in source_envelope.parts:
        klass = classify_prompt_part(
            kind=part.kind, name=part.name, source=part.source,
        )
        _class_counts[klass.value] += 1
        part_id = _part_key(part)
        if klass in _clearable_classes:
            _clearable_part_ids.append(part_id)
            # Per-part token estimate via the same estimator the runtime
            # uses for ``last_estimated_tokens_*``.
            _clearable_tokens += _estimate(part.body)
        else:
            _retained_part_ids.append(part_id)
    entry["context_clearing"] = {
        "kind": "eligible_tool_results",
        "trigger": "phase_invocation",
        "phase": trace.trace_slot,
        "round": trace.loop_round,
        "surface_id": None,
        "render_mode": trace.render_mode,
        "prefix_hash": trace.prefix_hash,
        "payload_hash": trace.payload_hash,
        "wire_chars": trace.wire_chars,
        "clearable_tokens": _clearable_tokens,
        "clearable_part_ids": _clearable_part_ids,
        "retained_part_ids": _retained_part_ids,
        "class_counts": _class_counts,
        # Observe-only mode: nothing is actually cleared and no provider
        # cache is touched.
        "cleared_tokens": 0,
        "artifact_refs": [],
        "cache_effect": "none",
    }


def stamp_context_pressure(
    entry: dict[str, Any],
    trace: _TraceInputs,
    *,
    agent: Any,
    runtime_id: str,
    model_key: str,
) -> Any:
    """ADR 0029 / M14.4: runtime context-pressure telemetry.

    Resolves the best available source via ``resolve_context_pressure`` —
    runtime-reported if the runtime exposes a live indicator, otherwise the
    next source down the hierarchy. Returns the resolved pressure object so
    the live card can reuse it without a second probe.
    """
    from pipeline.observability.context_pressure import (
        resolve_context_pressure,
    )

    _pressure = resolve_context_pressure(agent)
    entry["context_pressure"] = {
        "phase": trace.trace_slot,
        "round": trace.loop_round,
        "surface_id": None,
        "context_source": _pressure.context_source.value,
        "context_window_tokens": _pressure.context_window_tokens,
        "context_used_tokens": _pressure.context_used_tokens,
        "context_remaining_tokens": _pressure.context_remaining_tokens,
        "context_fill_ratio": _pressure.context_fill_ratio,
        "trigger_source": _pressure.trigger_source.value,
        "session_split": trace.session_split,
        "session_key": trace.session_key,
        "provider_session_id": trace.provider_session_id,
        "runtime": runtime_id,
        "model": model_key,
        "prefix_hash": trace.prefix_hash,
        "payload_hash": trace.payload_hash,
        "wire_chars": trace.wire_chars,
    }
    return _pressure


def stamp_runtime_compaction(
    entry: dict[str, Any],
    trace: _TraceInputs,
    *,
    agent: Any,
) -> None:
    """ADR 0029 / M14.4+: runtime auto-compaction evidence.

    Only stamps when the runtime exposes ``last_runtime_compaction_event``.
    No runtime exposes it today; the branch activates automatically when one
    does. Observe-only: records that the runtime compacted itself, not
    anything Orcho did.
    """
    from pipeline.observability.runtime_compaction import (
        resolve_runtime_compaction_event,
    )

    _compaction = resolve_runtime_compaction_event(agent)
    if _compaction is not None:
        entry["runtime_compaction"] = {
            "kind": _compaction.kind,
            "trigger": _compaction.trigger,
            "phase": trace.trace_slot,
            "round": trace.loop_round,
            "surface_id": None,
            "pre_used_tokens": _compaction.pre_used_tokens,
            "post_used_tokens": _compaction.post_used_tokens,
            "summary_tokens": _compaction.summary_tokens,
            "prefix_hash": trace.prefix_hash,
            "payload_hash": trace.payload_hash,
            "wire_chars": trace.wire_chars,
            "preserved_slots": list(_compaction.preserved_slots),
            "artifact_refs": list(_compaction.artifact_refs),
        }


def render_live_card(
    *,
    agent: Any,
    outcome: AgentInvocationOutcome,
    pressure: Any,
    duration_s: float,
    trace_slot: str,
    loop_round: int | None,
) -> None:
    """M14.4.4 — per-call live card.

    Gated on ``--output live / debug`` so summary-mode runs stay quiet;
    under live mode the card prints once per ``agent.invoke`` with a stable
    1-4 line shape. Numeric fields come from the normalized
    :class:`AgentInvocationOutcome` (built once by the caller); context
    fields come from the resolved ``pressure``. No extra runtime probes.
    Wrapped in try/except so a formatting bug never breaks an
    otherwise-successful call.
    """
    try:
        from core.observability.logging import get_output_mode
        if get_output_mode() in {"live", "debug"}:
            from core.infra.config import accounting_enabled
            from core.observability.live_card import (
                LiveCardData,
                format_live_card,
            )
            _model = outcome.model or str(getattr(agent, "model", "") or "")
            _cost_usd = None
            _cost_estimated = False
            if accounting_enabled():
                from core.observability.metrics import (
                    _resolve_phase_cost_usd_equivalent,
                )
                _tokens_in = int(outcome.tokens_in or 0)
                _tokens_out = int(outcome.tokens_out or 0)
                _tokens_total = int(outcome.tokens_total or 0)
                _tokens_unknown = max(0, _tokens_total - _tokens_in - _tokens_out)
                _cost_usd, _cost_estimated = _resolve_phase_cost_usd_equivalent(
                    cost_usd=outcome.cost_usd_equivalent,
                    model=_model,
                    tokens_in=_tokens_in,
                    tokens_out=_tokens_out,
                    tokens_unknown=_tokens_unknown,
                    tokens_in_cache_read=int(outcome.tokens_in_cache_read or 0),
                    tokens_exact=outcome.tokens_exact,
                )
            _card = LiveCardData(
                phase=trace_slot,
                duration_s=duration_s,
                round=loop_round,
                cost_usd=_cost_usd,
                cost_estimated=_cost_estimated,
                model=_model,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                reasoning_output_tokens=outcome.tokens_out_reasoning,
                tokens_total=outcome.tokens_total,
                prompt_tokens=outcome.wire_tokens_estimate,
                tool_calls=outcome.tool_calls,
                cache_read_tokens=outcome.tokens_in_cache_read,
                cache_creation_tokens=outcome.tokens_in_cache_create,
                context_used_tokens=pressure.context_used_tokens,
                context_window_tokens=pressure.context_window_tokens,
                context_source=pressure.context_source.value,
            )
            print(format_live_card(_card))
    except Exception:  # noqa: BLE001 - card is print-only, never fail invoke
        pass
