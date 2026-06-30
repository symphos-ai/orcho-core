"""M14.1 durable context-growth trace extractor (ADR 0029).

Observe-only sibling of :mod:`pipeline.observability.prompt_render`.
Where ``prompt_render`` describes **what bytes went on the wire**,
``context_growth`` describes **how the context window grew** as a
result of that invocation: estimated input/output token volume,
correlation back to the prompt-render record, and reserved slots
for the lifecycle primitives (clearing, compaction, tool counts)
that later M14 slices will populate.

ADR 0029 §"Evidence Model" defines the full candidate field set.
M14.1 lands the observe-only subset; clearing, compaction, and
memory fields are placeholders that stay at safe defaults until
M14.3 / M14.4 / M14.5 enable the underlying primitives.

Coverage taxonomy mirrors :mod:`prompt_render`:

- ``plan`` / ``replan`` → ``session.phases.plan[].context_growth``
- ``validate_plan`` → ``session.phases.validate_plan[].context_growth``
- ``implement`` → ``session.phases.implement.context_growth``
- (rounds-side review/repair are reserved for M14.3 when per-side
  tool clearing actually happens; M14.1 keeps the round entry
  shape stable but does not split context_growth per side)

The extractor is pure: it never mutates the input session and
never fabricates records for surfaces that did not stamp one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── M14.1: durable context-growth shape ──────────────────────────────────────
#
# The 15-field durable shape is the M14.1 normalization target.
# Every projected record carries exactly these keys so downstream
# consumers (future evidence collector, dashboards, lab probes) read
# one stable surface regardless of which lifecycle primitive
# eventually populates each placeholder.
#
# Placeholder semantics (always safe / zero-equivalent until the
# matching primitive lands):
#
# - ``tool_use_count`` — count of tool/function invocations the
#   provider reported during the call. M14.1 cannot read this from
#   the runtime yet; default ``0``. M14.3 wires the real count
#   when the clearing primitive lands.
# - ``cleared_tokens`` — estimated removed tool-result tokens.
#   M14.1 never clears, so always ``0``. M14.3 populates.
# - ``summary_tokens`` — tokens in a compaction summary. M14.1
#   never compacts, so always ``0``. M14.4 populates.
# - ``artifact_refs`` — paths/digests that make clearing
#   recoverable. M14.1 stamps no clearing, so always empty.
#   M14.3 populates.
#
# ``input_tokens_estimate`` and ``output_tokens_estimate`` are the
# load-bearing M14.1 observables — per-invocation estimates the
# runtime exposes via ``last_estimated_tokens_in`` /
# ``last_estimated_tokens_out``. They are NOT a cumulative
# session total; later slices can add a separate
# ``cumulative_input_tokens`` slot when runtimes expose it without
# requiring a re-walk of the session for every consumer.
DURABLE_FIELDS: tuple[str, ...] = (
    # Event identity.
    "kind",
    "trigger",
    # Attribution.
    "phase",
    "round",
    "surface_id",
    # Render-side correlation (back to prompt_render).
    "render_mode",
    "prefix_hash",
    "payload_hash",
    "wire_chars",
    # Observed token deltas (M14.1 core observable).
    "input_tokens_estimate",
    "output_tokens_estimate",
    # Reserved lifecycle placeholders (zero until M14.3 / M14.4).
    "tool_use_count",
    "cleared_tokens",
    "summary_tokens",
    "artifact_refs",
)

# Initial event kind. M14.3 will add ``clear_tool_results``;
# M14.4 will add ``compact``; M14.5 will add ``memory_*``.
_DEFAULT_KIND = "phase_invocation"
_DEFAULT_TRIGGER = "phase_invocation"
_DEFAULT_TOOL_USE_COUNT = 0
_DEFAULT_CLEARED_TOKENS = 0
_DEFAULT_SUMMARY_TOKENS = 0


def normalize_context_growth(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the source ``context_growth`` dict into the durable shape.

    Pure function — does not mutate *payload*. The projection rules:

    - ``kind`` / ``trigger`` pass through; default to
      ``"phase_invocation"`` when absent (M14.1 fallback).
    - ``phase`` / ``round`` / ``surface_id`` are attribution slots
      stamped by the writer. ``surface_id`` is always ``None``
      until ADR 0027 fanout lands.
    - ``render_mode`` / ``prefix_hash`` / ``payload_hash`` /
      ``wire_chars`` mirror the sibling ``prompt_render`` record so
      consumers can correlate by hash without re-walking the
      session.
    - ``input_tokens_estimate`` / ``output_tokens_estimate`` pass
      through; default to ``None`` when the runtime did not expose
      them (some non-instrumented mock paths).
    - ``tool_use_count`` / ``cleared_tokens`` / ``summary_tokens``
      default to ``0``; ``artifact_refs`` defaults to ``[]``.
      These four are reserved for later M14 slices and stay at
      safe defaults under M14.1.

    The returned dict's key set is exactly :data:`DURABLE_FIELDS`.
    """
    return {
        "kind": payload.get("kind", _DEFAULT_KIND),
        "trigger": payload.get("trigger", _DEFAULT_TRIGGER),
        "phase": payload.get("phase"),
        "round": payload.get("round"),
        "surface_id": payload.get("surface_id"),
        "render_mode": payload.get("render_mode"),
        "prefix_hash": payload.get("prefix_hash"),
        "payload_hash": payload.get("payload_hash"),
        "wire_chars": payload.get("wire_chars"),
        "input_tokens_estimate": payload.get("input_tokens_estimate"),
        "output_tokens_estimate": payload.get("output_tokens_estimate"),
        "tool_use_count": payload.get(
            "tool_use_count", _DEFAULT_TOOL_USE_COUNT,
        ),
        "cleared_tokens": payload.get(
            "cleared_tokens", _DEFAULT_CLEARED_TOKENS,
        ),
        "summary_tokens": payload.get(
            "summary_tokens", _DEFAULT_SUMMARY_TOKENS,
        ),
        "artifact_refs": list(payload.get("artifact_refs") or []),
    }


@dataclass(frozen=True)
class PhaseContextGrowth:
    """One durable context-growth trace record.

    ``trace_surface`` mirrors the same field on
    :class:`pipeline.observability.prompt_render.PhaseRenderTrace`
    so a downstream consumer can join records by
    ``(phase, trace_surface, attempt, round)`` to a sibling
    ``prompt_render`` trace.
    """

    phase: str
    trace_surface: str
    attempt: int | None
    round: int | None
    source_path: str
    payload: dict[str, Any]


_PHASES_KEY = "phases"
_CONTEXT_GROWTH_KEY = "context_growth"


def extract_context_growth_traces(session: dict) -> list[PhaseContextGrowth]:
    """Walk *session* and return one record per covered
    ``context_growth`` payload.

    Coverage tracks the prompt_render extractor's plan / replan /
    validate_plan / implement surfaces. Round-side review / repair
    split is reserved for M14.3 when per-side tool clearing
    actually happens.
    """
    phases = _phases_from_session(session)
    if phases is None:
        return []

    traces: list[PhaseContextGrowth] = []
    traces.extend(_extract_plan(phases))
    traces.extend(_extract_validate_plan(phases))
    traces.extend(_extract_implement(phases))
    return traces


def _phases_from_session(session: dict) -> dict | None:
    if not isinstance(session, dict):
        return None
    phases = session.get(_PHASES_KEY)
    return phases if isinstance(phases, dict) else None


def _extract_plan(phases: dict) -> list[PhaseContextGrowth]:
    entries = phases.get("plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseContextGrowth] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_CONTEXT_GROWTH_KEY)
        if not isinstance(payload, dict):
            continue
        attempt_raw = entry.get("attempt", 1)
        attempt = int(attempt_raw) if isinstance(attempt_raw, int) else 1
        replan_critique = entry.get("replan_critique")
        human_feedback = entry.get("human_feedback")
        trace_surface = (
            "plan"
            if attempt == 1 and not replan_critique and not human_feedback
            else "replan"
        )
        out.append(
            PhaseContextGrowth(
                phase="plan",
                trace_surface=trace_surface,
                attempt=attempt,
                round=None,
                source_path=f"phases.plan[{idx}].context_growth",
                payload=normalize_context_growth(payload),
            ),
        )
    return out


def _extract_validate_plan(phases: dict) -> list[PhaseContextGrowth]:
    entries = phases.get("validate_plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseContextGrowth] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_CONTEXT_GROWTH_KEY)
        if not isinstance(payload, dict):
            continue
        attempt_raw = entry.get("attempt", 1)
        attempt = int(attempt_raw) if isinstance(attempt_raw, int) else 1
        out.append(
            PhaseContextGrowth(
                phase="validate_plan",
                trace_surface="validate_plan",
                attempt=attempt,
                round=None,
                source_path=f"phases.validate_plan[{idx}].context_growth",
                payload=normalize_context_growth(payload),
            ),
        )
    return out


def _extract_implement(phases: dict) -> list[PhaseContextGrowth]:
    entry = phases.get("implement")
    if not isinstance(entry, dict):
        return []
    payload = entry.get(_CONTEXT_GROWTH_KEY)
    if not isinstance(payload, dict):
        return []
    return [
        PhaseContextGrowth(
            phase="implement",
            trace_surface="implement",
            attempt=None,
            round=None,
            source_path="phases.implement.context_growth",
            payload=normalize_context_growth(payload),
        ),
    ]


__all__ = [
    "DURABLE_FIELDS",
    "PhaseContextGrowth",
    "extract_context_growth_traces",
    "normalize_context_growth",
]
