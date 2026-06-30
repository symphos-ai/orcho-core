"""M12 evidence-layer projection of durable prompt-render traces.

The :mod:`pipeline.observability.prompt_render` module owns the read-
only extractor and the durable 12-field normalization — observability
concerns end there. This module belongs to the evidence layer and
owns the SUMMARY projection that lands inside the evidence bundle:

- Drop list-valued part keys (counts only — never raw identifiers).
- Flatten ``physical_session_key`` into top-level ``session_*`` keys
  for easier downstream filtering / SQL projection.
- Add ``provider_session_id`` as a top-level field for the same
  reason.

Separating observability from evidence keeps the layering clean:
observability is reusable beyond evidence (a dashboard or MCP tool
can read durable traces directly), evidence owns the summary
projection it persists.
"""
from __future__ import annotations

from typing import Any

from pipeline.observability.prompt_render import (
    PhaseRenderTrace,
    extract_prompt_render_traces,
)

#: Required keys on every entry of ``evidence["prompt_render"]``.
#: The strict schema lets downstream consumers treat absence as a bug
#: rather than as backward-compat tolerance.
EVIDENCE_SUMMARY_FIELDS: tuple[str, ...] = (
    # Attribution (M12-C1 surface taxonomy).
    "phase",
    "phase_key",
    "trace_surface",
    "attempt",
    "round",
    "continue_session",
    "source_path",
    # Render-side metadata (durable shape passthrough).
    "render_mode",
    "session_split",
    "execution_mode",
    "surface_id",
    "surface_count",
    # Session-key dimensions (flattened from physical_session_key for
    # easy SQL / dashboard projection; the original nested dict stays
    # in the durable trace surface for callers that prefer it).
    "session_scope",
    "session_run_id",
    "session_runtime",
    "session_model",
    "provider_session_id",
    # Render-shape summary — counts only, never the part-key arrays.
    "selected_count",
    "omitted_count",
    "delta_dropped_count",
    "prefix_hash",
    "payload_hash",
    "wire_chars",
)


def summarize_trace_for_evidence(trace: PhaseRenderTrace) -> dict[str, Any]:
    """Project a :class:`PhaseRenderTrace` into the evidence summary shape.

    Counts-only by design: the durable trace carries
    ``selected_part_keys`` / ``omitted_part_keys`` lists, but the
    evidence surface exposes only ``selected_count`` /
    ``omitted_count`` so consumers cannot accidentally read part-key
    arrays as if they were raw prompt content. Hashes (``prefix_hash``,
    ``payload_hash``) and ``wire_chars`` carry forward unchanged so
    drift detection still works.

    Never includes raw prompt text. The durable trace shape already
    excludes it (M12-C1 contract); this projection asserts the same
    rule on the evidence-facing surface.
    """
    p = trace.payload
    session_key = p.get("physical_session_key") or {}
    if not isinstance(session_key, dict):
        session_key = {}
    selected = p.get("selected_part_keys") or []
    omitted = p.get("omitted_part_keys") or []
    dropped = p.get("delta_dropped_part_keys") or []
    # Writer-stamped attribution fields (E1 follow-up). ``phase_key``
    # is the session-key phase (differs from ``trace.phase`` only for
    # CHAIN repair_changes, where ``phase_key="implement"`` because
    # the repair reuses the implement physical session). The extractor
    # already populated ``trace.phase_key`` / ``trace.continue_session``
    # / ``trace.round`` with the writer-stamped values (or sensible
    # structural fallbacks for legacy / synthetic sources), so the
    # summary reads them off the trace directly.
    return {
        "phase": trace.phase,
        "phase_key": trace.phase_key if trace.phase_key is not None else trace.phase,
        "trace_surface": trace.trace_surface,
        "attempt": trace.attempt,
        "round": trace.round,
        "continue_session": trace.continue_session,
        "source_path": trace.source_path,
        "render_mode": p.get("render_mode"),
        "session_split": p.get("session_split"),
        "execution_mode": p.get("execution_mode"),
        "surface_id": p.get("surface_id"),
        "surface_count": p.get("surface_count"),
        "session_scope": session_key.get("scope"),
        "session_run_id": session_key.get("run_id"),
        "session_runtime": session_key.get("runtime"),
        "session_model": session_key.get("model_key"),
        "provider_session_id": p.get("provider_session_id"),
        "selected_count": len(selected) if isinstance(selected, list) else 0,
        "omitted_count": len(omitted) if isinstance(omitted, list) else 0,
        "delta_dropped_count": len(dropped) if isinstance(dropped, list) else 0,
        "prefix_hash": p.get("prefix_hash"),
        "payload_hash": p.get("payload_hash"),
        "wire_chars": p.get("wire_chars"),
    }


def build_prompt_render_evidence(session: dict) -> list[dict[str, Any]]:
    """Compose the ``evidence["prompt_render"]`` section from *session*.

    Always returns a list — empty when *session* has no covered
    ``prompt_render`` records. M12-C3 makes the section always-present
    so downstream consumers can rely on the key existing.
    """
    traces = extract_prompt_render_traces(session)
    return [summarize_trace_for_evidence(t) for t in traces]


__all__ = [
    "EVIDENCE_SUMMARY_FIELDS",
    "build_prompt_render_evidence",
    "summarize_trace_for_evidence",
]
