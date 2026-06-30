"""M12 durable prompt-render trace extractor.

Read-only projection over ``session["phases"]`` per the coverage
contract in
``tests/unit/pipeline/observability/test_prompt_render_coverage.py``.

Covered surfaces:

- ``plan`` → ``session.phases.plan[].prompt_render``
- ``replan`` → ``session.phases.plan[].prompt_render`` (same array,
  surface disambiguated by attempt + presence of ``replan_critique``
  or ``human_feedback`` — any attempt that consumed reviewer findings
  or operator instruction reads as ``replan``)
- ``validate_plan`` → ``session.phases.validate_plan[].prompt_render``
- ``implement`` → ``session.phases.implement.prompt_render``
- ``review_changes`` → ``session.phases.rounds[].prompt_render_review``
- ``repair_changes`` → ``session.phases.rounds[].prompt_render_repair``

Documented exceptions (never synthesized, even if a stray
``prompt_render`` key sits in the session entry):

- ``hypothesis`` — direct ``plan_agent.invoke`` call (M8 deferral)
- ``validate_hypothesis`` — direct ``qa_agent.invoke`` call
- ``final_acceptance`` — intentional full-render verdict isolation

The extractor is pure: it never mutates the input session and never
fabricates records for surfaces not in the coverage contract. M12-C2
adds normalization to the durable trace shape; M12-C1 keeps the
``payload`` field byte-identical to the source ``prompt_render`` dict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── M12-C2: durable trace shape ───────────────────────────────────────────────
#
# The 12-field durable shape is the M12 normalization target. Each
# trace ``payload`` is projected from the source ``prompt_render``
# dict into this shape so downstream consumers (evidence collector,
# dashboards, M12 persistence) read one stable surface regardless
# of session-shape evolution.
#
# Two field semantics are load-bearing:
#
# - ``execution_mode`` is a documented M12 fallback set to
#   ``"linear"`` because the source ``prompt_render`` record does
#   not persist execution-mode metadata, and every covered surface
#   today is a pre-fanout session-adapter trace. It is NOT
#   runtime-derived. When fanout (ADR 0027) lands, the writer must
#   stamp the real value and this fallback retires.
#
# - ``provider_session_id`` is ``None`` because the source
#   ``prompt_render`` record does not persist the provider's
#   session id today. A future writer-side change can stamp it;
#   until then the M12 extractor reports ``None`` rather than
#   fabricating a value.
DURABLE_FIELDS: tuple[str, ...] = (
    "render_mode",
    "session_split",
    "physical_session_key",
    "provider_session_id",
    "part_ids",
    "selected_part_keys",
    "omitted_part_keys",
    "delta_dropped_part_keys",
    "prefix_hash",
    "payload_hash",
    "wire_chars",
    "execution_mode",
    "surface_id",
    "surface_count",
    # Writer-stamped attribution (E1 follow-up). ``phase_key`` is the
    # session-key phase, distinct from ``trace_surface`` for CHAIN
    # repair_changes. ``round`` is the loop counter at invoke time.
    # ``continue_session`` distinguishes fresh from resumed provider
    # sessions without cross-referencing runner.log.
    "phase_key",
    "round",
    "continue_session",
)
# Per-part fields (``body``, ``artifact_path``, etc.) are intentionally
# omitted from the durable shape. This surface is per-render summary
# evidence: it identifies the source parts (``part_ids``) and how they
# were routed — on the wire (``selected_part_keys``), cached-omitted
# (``omitted_part_keys``), or dropped as already-in-history
# (``delta_dropped_part_keys``) — plus the hashes / wire size, but never
# the raw content of any single part. Adding a
# per-part body or trace-metadata field here would explode the durable
# payload size and re-couple render summary to part content schema.
# If a future feature needs durable per-part metadata (e.g. an
# ``artifact_refs`` list summarizing ``artifact_path`` across parts),
# it should land as its own observability sibling, not inside
# ``prompt_render``.

_EXECUTION_MODE_FALLBACK = "linear"
_SURFACE_COUNT_FALLBACK = 1


def normalize_prompt_render(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the source ``prompt_render`` dict into the M12 durable shape.

    Pure function — does not mutate *payload*. The projection rules:

    - ``render_mode`` / ``session_split`` / ``selected_part_keys`` /
      ``omitted_part_keys`` / ``prefix_hash`` / ``payload_hash`` /
      ``wire_chars`` pass through unchanged.
    - The source ``session_key`` field is renamed to
      ``physical_session_key`` on the durable surface; the value
      (dict or ``None``) is preserved.
    - ``provider_session_id`` passes the source value through if
      the writer stamped one; otherwise ``None``. The source
      session does not persist it today, but a future writer-side
      change can stamp it without churning the durable contract.
    - ``execution_mode`` passes the source value through if the
      writer stamped one; otherwise the documented M12 fallback
      ``"linear"`` — NOT runtime-derived (see module-level
      docstring). Every covered surface today is a pre-fanout
      session-adapter trace, so the linear fallback is correct
      for current sessions. ADR 0027 fanout will retire the
      fallback by stamping the real value at the writer; this
      projection already honours that future shape.
    - ``surface_id`` passes the source value through (``None`` if
      absent). Reserved for ADR 0027 fanout.
    - ``surface_count`` passes the source value through; defaults
      to ``1`` (pre-fanout traces describe one linear surface
      invocation). Same pass-through future-proofing as the other
      reserved fields.

    The returned dict's key set is exactly :data:`DURABLE_FIELDS`.

    M12 closing: ``part_ids`` passes the source value through —
    the writer (``_session_aware_invoke``) stamps the full ordered
    set of envelope part ids (union of selected + omitted in render
    order) so downstream consumers can audit "what could have been
    sent" without re-running the M6 selector. Defaults to an empty
    list when the writer did not stamp one (legacy / synthetic
    fixtures); the writer always stamps on every modern invocation.
    """
    return {
        "render_mode": payload.get("render_mode"),
        "session_split": payload.get("session_split"),
        "physical_session_key": payload.get("session_key"),
        "provider_session_id": payload.get("provider_session_id"),
        "part_ids": payload.get("part_ids", []),
        "selected_part_keys": payload.get("selected_part_keys"),
        "omitted_part_keys": payload.get("omitted_part_keys"),
        # ADR 0026 delta drop: parts omitted from the wire on a resumed
        # turn because the runtime already holds them in history. Defaults
        # to [] for legacy/synthetic sources that predate the field.
        "delta_dropped_part_keys": payload.get("delta_dropped_part_keys", []),
        "prefix_hash": payload.get("prefix_hash"),
        "payload_hash": payload.get("payload_hash"),
        "wire_chars": payload.get("wire_chars"),
        "execution_mode": payload.get(
            "execution_mode", _EXECUTION_MODE_FALLBACK,
        ),
        "surface_id": payload.get("surface_id"),
        "surface_count": payload.get(
            "surface_count", _SURFACE_COUNT_FALLBACK,
        ),
        # Writer-stamped attribution: pass-through if the writer
        # stamped one (modern ``_session_aware_invoke`` always does),
        # otherwise ``None`` — synthetic fixtures and legacy sources
        # surface as null so structural fallbacks in the extractor
        # can still derive useful values.
        "phase_key": payload.get("phase_key"),
        "round": payload.get("round"),
        "continue_session": payload.get("continue_session"),
    }


@dataclass(frozen=True)
class PhaseRenderTrace:
    """One durable prompt-render trace record.

    ``trace_surface`` is the rendered persona/task instance (``plan``
    vs ``replan`` distinguish two surfaces that share
    ``phase="plan"``). It is intentionally named ``trace_surface`` to
    avoid collision with ADR 0027's future ``surface_id`` /
    ``surface_count`` fanout reservations on the durable payload.

    ``source_path`` is the dotted path into the source session
    where this trace was extracted from. Examples:

    - ``phases.plan[0].prompt_render``
    - ``phases.implement.prompt_render``
    - ``phases.rounds[2].prompt_render_repair``

    ``payload`` is the source ``prompt_render`` dict, unchanged in
    M12-C1. M12-C2 replaces it with a normalized durable shape.

    ``phase_key`` and ``continue_session`` are writer-stamped
    attribution fields (E1 follow-up). ``phase_key`` is the
    session-key phase passed to ``_session_aware_invoke`` — equal
    to ``phase`` for most surfaces but differs for CHAIN
    repair_changes where phase_key="implement" because the repair
    reuses the implement physical session. ``continue_session``
    reflects whether the invoke resumed the provider session.
    """

    phase: str
    trace_surface: str
    attempt: int | None
    round: int | None
    source_path: str
    payload: dict[str, Any]
    phase_key: str | None = None
    continue_session: bool | None = None


_PHASES_KEY = "phases"
_PROMPT_RENDER_KEY = "prompt_render"


def extract_prompt_render_traces(session: dict) -> list[PhaseRenderTrace]:
    """Walk *session* and return one :class:`PhaseRenderTrace` per
    covered prompt-render record.

    The walk visits only the covered surfaces named in the M12
    coverage contract. Missing ``prompt_render`` keys are skipped
    gracefully. Documented exceptions (hypothesis,
    validate_hypothesis, final_acceptance) are never visited — a
    stray ``prompt_render`` key under those phase entries does not
    surface in the output.

    The extractor does not mutate *session*. Callers can pass the
    same dict to multiple extractors without copy precautions.
    """
    phases = _phases_from_session(session)
    if phases is None:
        return []

    traces: list[PhaseRenderTrace] = []
    traces.extend(_extract_plan(phases))
    traces.extend(_extract_validate_plan(phases))
    traces.extend(_extract_implement(phases))
    traces.extend(_extract_rounds(phases))
    return traces


def _phases_from_session(session: dict) -> dict | None:
    if not isinstance(session, dict):
        return None
    phases = session.get(_PHASES_KEY)
    return phases if isinstance(phases, dict) else None


def _writer_round(payload: dict) -> int | None:
    """Read the writer-stamped round (if any) from a source payload.

    Returns the int when present and well-typed, ``None`` otherwise
    so callers can fall back to a structural source.
    """
    v = payload.get("round")
    if isinstance(v, bool):
        return None
    return v if isinstance(v, int) else None


def _writer_phase_key(payload: dict, default: str) -> str:
    v = payload.get("phase_key")
    return v if isinstance(v, str) and v else default


def _writer_continue_session(payload: dict) -> bool | None:
    v = payload.get("continue_session")
    return v if isinstance(v, bool) else None


def _extract_plan(phases: dict) -> list[PhaseRenderTrace]:
    """Walk ``phases.plan[]``. Surface is ``plan`` for attempt 1
    without a ``replan_critique`` or ``human_feedback``; every other
    entry is ``replan`` (reviewer-driven, operator-driven, or both).
    """
    entries = phases.get("plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseRenderTrace] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_PROMPT_RENDER_KEY)
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
        # Writer-stamped round preferred; fall back to attempt so
        # plan rounds 1/2/3 surface even on legacy / synthetic
        # sources where the writer did not stamp ``round``.
        round_n = _writer_round(payload)
        if round_n is None:
            round_n = attempt
        out.append(
            PhaseRenderTrace(
                phase="plan",
                trace_surface=trace_surface,
                attempt=attempt,
                round=round_n,
                source_path=f"phases.plan[{idx}].prompt_render",
                payload=normalize_prompt_render(payload),
                phase_key=_writer_phase_key(payload, "plan"),
                continue_session=_writer_continue_session(payload),
            ),
        )
    return out


def _extract_validate_plan(phases: dict) -> list[PhaseRenderTrace]:
    entries = phases.get("validate_plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseRenderTrace] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_PROMPT_RENDER_KEY)
        if not isinstance(payload, dict):
            continue
        attempt_raw = entry.get("attempt", 1)
        attempt = int(attempt_raw) if isinstance(attempt_raw, int) else 1
        round_n = _writer_round(payload)
        if round_n is None:
            round_n = attempt
        out.append(
            PhaseRenderTrace(
                phase="validate_plan",
                trace_surface="validate_plan",
                attempt=attempt,
                round=round_n,
                source_path=f"phases.validate_plan[{idx}].prompt_render",
                payload=normalize_prompt_render(payload),
                phase_key=_writer_phase_key(payload, "validate_plan"),
                continue_session=_writer_continue_session(payload),
            ),
        )
    return out


def _extract_implement(phases: dict) -> list[PhaseRenderTrace]:
    entry = phases.get("implement")
    if not isinstance(entry, dict):
        return []
    payload = entry.get(_PROMPT_RENDER_KEY)
    if not isinstance(payload, dict):
        return []
    return [
        PhaseRenderTrace(
            phase="implement",
            trace_surface="implement",
            attempt=None,
            round=_writer_round(payload),
            source_path="phases.implement.prompt_render",
            payload=normalize_prompt_render(payload),
            phase_key=_writer_phase_key(payload, "implement"),
            continue_session=_writer_continue_session(payload),
        ),
    ]


def _extract_rounds(phases: dict) -> list[PhaseRenderTrace]:
    """Walk ``phases.rounds[]``. Each round entry may carry two
    independent prompt-render records — ``prompt_render_review``
    (reviewer side) and ``prompt_render_repair`` (CHAIN-attributed
    repair side per M11.5 Fix 2). Both surface as separate trace
    records when present.
    """
    entries = phases.get("rounds")
    if not isinstance(entries, list):
        return []
    out: list[PhaseRenderTrace] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        round_raw = entry.get("round")
        structural_round = int(round_raw) if isinstance(round_raw, int) else None
        review_payload = entry.get("prompt_render_review")
        if isinstance(review_payload, dict):
            round_n = _writer_round(review_payload)
            if round_n is None:
                round_n = structural_round
            out.append(
                PhaseRenderTrace(
                    phase="review_changes",
                    trace_surface="review_changes",
                    attempt=None,
                    round=round_n,
                    source_path=f"phases.rounds[{idx}].prompt_render_review",
                    payload=normalize_prompt_render(review_payload),
                    phase_key=_writer_phase_key(review_payload, "review_changes"),
                    continue_session=_writer_continue_session(review_payload),
                ),
            )
        repair_payload = entry.get("prompt_render_repair")
        if isinstance(repair_payload, dict):
            round_n = _writer_round(repair_payload)
            if round_n is None:
                round_n = structural_round
            out.append(
                PhaseRenderTrace(
                    phase="repair_changes",
                    trace_surface="repair_changes",
                    attempt=None,
                    round=round_n,
                    source_path=f"phases.rounds[{idx}].prompt_render_repair",
                    payload=normalize_prompt_render(repair_payload),
                    # repair_changes reuses the implement physical
                    # session key (CHAIN mode), so a missing writer
                    # stamp falls back to "implement" rather than
                    # to the trace slot.
                    phase_key=_writer_phase_key(repair_payload, "implement"),
                    continue_session=_writer_continue_session(repair_payload),
                ),
            )
    return out


__all__ = [
    "DURABLE_FIELDS",
    "PhaseRenderTrace",
    "extract_prompt_render_traces",
    "normalize_prompt_render",
]
