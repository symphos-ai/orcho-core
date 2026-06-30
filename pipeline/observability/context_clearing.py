"""M14.3 — Tool-result clearing event evidence (ADR 0029).

Observe-only sibling of :mod:`pipeline.observability.context_growth`.
Where ``context_growth`` answers *"how did the context window grow
during this invocation?"*, ``context_clearing`` answers *"what
parts of that context would be safe to clear, why, and how many
tokens / artifacts are involved?"* — without actually clearing
anything.

The M14.3 scope is deliberately observe-only. Orcho's agent
runtimes do not expose a clearing API today (no
``agent.clear_tool_results()`` or equivalent on
``IAgentRuntime``), so this milestone records **eligibility**
evidence and leaves runtime context intact. When a runtime
clearing API lands later (or M14.5 memory persists summaries),
the same evidence shape extends to ``kind="clear_tool_results"``
without churning consumers.

The eligibility judgment uses the M14.2
:class:`pipeline.observability.output_class.OutputClass` taxonomy:

- ``RE_FETCHABLE`` and ``PERSISTED_ARTIFACT`` → **eligible** to
  clear (the payload is recoverable from disk / code / repo or
  from an on-disk artefact).
- ``DECISION_BEARING`` and ``EPHEMERAL`` → **retained** (decision
  content must never drop silently; ephemeral content has no
  recovery path).

## Evidence model (ADR 0029 §"Evidence Model")

The durable shape projects the source ``context_clearing`` dict
into 15 fields covering identity + attribution + correlation +
the eligibility counts and class breakdown M14.3 actually
populates. Cleared-side fields (``cleared_tokens``,
``artifact_refs``, ``cache_effect``) stay at safe defaults until a
clearing primitive ships.

Coverage taxonomy mirrors :mod:`context_growth`:

- ``plan`` / ``replan`` → ``session.phases.plan[].context_clearing``
- ``validate_plan`` → ``session.phases.validate_plan[].context_clearing``
- ``implement`` → ``session.phases.implement.context_clearing``
- ``review_changes`` → ``session.phases.rounds[].context_clearing_review``
- ``repair_changes`` → ``session.phases.rounds[].context_clearing_repair``

The round-side split is new in M14.3 (M14.1 deferred it; this
milestone catches up by promoting both ``context_growth_review``
/ ``context_growth_repair`` and ``context_clearing_review`` /
``context_clearing_repair`` through ``RoundAdapter`` — see
:mod:`pipeline.session_adapters`).

The extractor is pure: it never mutates the input session and
never fabricates records for surfaces that did not stamp one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── M14.3: durable context-clearing shape ────────────────────────────────────
#
# 15 fields total. Mirror of M14.1 ``context_growth`` but counts
# eligibility instead of growth observables. Reserved cleared-side
# fields stay at safe defaults under M14.3 observe-only mode and
# populate when a runtime clearing API lands (or M14.5 memory
# persists summaries).
#
# Load-bearing field semantics:
#
# - ``kind`` defaults to ``"eligible_tool_results"`` — the
#   observe-only event class. ADR 0029 reserves
#   ``"clear_tool_results"`` for the post-clearing event; the
#   normalizer accepts either value when a future writer ships
#   the actual clearing primitive.
#
# - ``clearable_tokens`` is the summed estimated-token count of
#   every part whose M14.2 classification is in
#   ``CLEARABLE_CLASSES`` (RE_FETCHABLE + PERSISTED_ARTIFACT).
#   The writer computes this from
#   ``core.observability.metrics.estimate_tokens(part.body)`` for
#   each eligible part — same estimator the runtime uses for
#   per-call token accounting.
#
# - ``clearable_part_ids`` / ``retained_part_ids`` carry the
#   ``part_session_key`` strings (``"kind:name@version"``) so a
#   downstream consumer joins them to the sibling
#   ``prompt_render.part_ids`` without re-running the M14.2
#   classifier.
#
# - ``class_counts`` records the count per
#   :class:`OutputClass` value over the full ``envelope.parts``
#   set. Useful for at-a-glance "how decision-heavy is this
#   invocation" charts; downstream consumers can compute it
#   themselves from ``part_ids`` + the classifier, but stamping
#   it durably saves the round-trip.
#
# - ``cleared_tokens`` / ``artifact_refs`` / ``cache_effect``
#   stay at safe defaults under observe-only mode. M14.3 does not
#   clear, so no tokens leave the runtime context, no artefact
#   references are recorded, and no provider cache is invalidated.
#
# TODO(artifact_refs): once ``PromptPart.artifact_path`` is in
# place (introduced with the path-leak fix that moved on-disk
# pointers out of artifact wire bodies), this writer should
# populate ``artifact_refs`` with the ``artifact_path`` values of
# parts classified as PERSISTED_ARTIFACT — currently the field
# stays empty even when an artifact part *is* present, which makes
# evidence say "clearable because persisted artifact" without
# pointing at the artifact. Out of scope for the initial
# path-separation PR (no clearing primitive yet → no semantic
# coupling broken by leaving it empty), but a natural follow-up
# when a future PR touches artifact lifecycle / context clearing.
DURABLE_FIELDS: tuple[str, ...] = (
    # Event identity.
    "kind",
    "trigger",
    # Attribution (matches context_growth).
    "phase",
    "round",
    "surface_id",
    # Render-side correlation (back to prompt_render / context_growth).
    "render_mode",
    "prefix_hash",
    "payload_hash",
    "wire_chars",
    # Eligibility (M14.3 core observables).
    "clearable_tokens",
    "clearable_part_ids",
    "retained_part_ids",
    "class_counts",
    # Reserved cleared-side fields (stay at safe defaults until
    # an actual clearing primitive lands).
    "cleared_tokens",
    "artifact_refs",
    "cache_effect",
)

# Initial event kind under observe-only mode. The normalizer
# accepts the future "clear_tool_results" value too so a later
# writer can flip without breaking consumers.
_DEFAULT_KIND_ELIGIBLE = "eligible_tool_results"
_DEFAULT_KIND_CLEARED = "clear_tool_results"
_VALID_KINDS: frozenset[str] = frozenset({
    _DEFAULT_KIND_ELIGIBLE,
    _DEFAULT_KIND_CLEARED,
})

_DEFAULT_TRIGGER = "phase_invocation"
_DEFAULT_CLEARED_TOKENS = 0
_DEFAULT_CACHE_EFFECT = "none"


def normalize_context_clearing(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the source ``context_clearing`` dict into the durable shape.

    Pure function — does not mutate *payload*. Projection rules:

    - ``kind`` defaults to ``"eligible_tool_results"`` (M14.3
      observe-only). The normalizer also accepts
      ``"clear_tool_results"`` for the future clearing primitive
      without churning the contract.
    - ``trigger`` defaults to ``"phase_invocation"``.
    - Attribution slots (``phase`` / ``round`` / ``surface_id``)
      pass through. ``surface_id`` stays ``None`` until ADR 0027
      fanout lands.
    - Render correlation slots (``render_mode`` / ``prefix_hash``
      / ``payload_hash`` / ``wire_chars``) mirror the sibling
      ``context_growth`` record.
    - ``clearable_tokens`` defaults to ``0`` (no eligible parts).
    - ``clearable_part_ids`` / ``retained_part_ids`` default to
      empty lists; the normalizer copies the source lists into
      fresh ``list`` instances so consumer mutation does not
      bleed into the source payload.
    - ``class_counts`` defaults to a fresh dict keyed by every
      :class:`OutputClass` value with zero counts — never
      ``None`` so consumers can index directly.
    - ``cleared_tokens`` / ``artifact_refs`` / ``cache_effect``
      stay at safe defaults under observe-only mode.

    The returned dict's key set is exactly :data:`DURABLE_FIELDS`.
    """
    from pipeline.observability.output_class import OutputClass

    kind = payload.get("kind", _DEFAULT_KIND_ELIGIBLE)
    if kind not in _VALID_KINDS:
        kind = _DEFAULT_KIND_ELIGIBLE

    raw_class_counts = payload.get("class_counts")
    if isinstance(raw_class_counts, dict):
        class_counts = {
            c.value: int(raw_class_counts.get(c.value, 0)) for c in OutputClass
        }
    else:
        class_counts = {c.value: 0 for c in OutputClass}

    return {
        "kind": kind,
        "trigger": payload.get("trigger", _DEFAULT_TRIGGER),
        "phase": payload.get("phase"),
        "round": payload.get("round"),
        "surface_id": payload.get("surface_id"),
        "render_mode": payload.get("render_mode"),
        "prefix_hash": payload.get("prefix_hash"),
        "payload_hash": payload.get("payload_hash"),
        "wire_chars": payload.get("wire_chars"),
        "clearable_tokens": payload.get("clearable_tokens", 0),
        "clearable_part_ids": list(payload.get("clearable_part_ids") or []),
        "retained_part_ids": list(payload.get("retained_part_ids") or []),
        "class_counts": class_counts,
        "cleared_tokens": payload.get(
            "cleared_tokens", _DEFAULT_CLEARED_TOKENS,
        ),
        "artifact_refs": list(payload.get("artifact_refs") or []),
        "cache_effect": payload.get("cache_effect", _DEFAULT_CACHE_EFFECT),
    }


@dataclass(frozen=True)
class PhaseContextClearing:
    """One durable context-clearing trace record.

    Sibling of
    :class:`pipeline.observability.context_growth.PhaseContextGrowth`.
    Consumers join the two by ``(phase, trace_surface, attempt,
    round)`` to correlate eligibility evidence with growth
    observables for the same invocation.
    """

    phase: str
    trace_surface: str
    attempt: int | None
    round: int | None
    source_path: str
    payload: dict[str, Any]


_PHASES_KEY = "phases"
_CONTEXT_CLEARING_KEY = "context_clearing"
_CONTEXT_CLEARING_REVIEW_KEY = "context_clearing_review"
_CONTEXT_CLEARING_REPAIR_KEY = "context_clearing_repair"


def extract_context_clearing_traces(
    session: dict,
) -> list[PhaseContextClearing]:
    """Walk *session* and return one record per covered
    ``context_clearing`` payload.

    Coverage mirrors :func:`context_growth.extract_context_growth_traces`
    plus the M14.3 round-side split:

    - ``plan`` / ``replan`` → ``session.phases.plan[].context_clearing``
    - ``validate_plan`` → ``session.phases.validate_plan[].context_clearing``
    - ``implement`` → ``session.phases.implement.context_clearing``
    - ``review_changes`` → ``session.phases.rounds[].context_clearing_review``
    - ``repair_changes`` → ``session.phases.rounds[].context_clearing_repair``
    """
    phases = _phases_from_session(session)
    if phases is None:
        return []

    traces: list[PhaseContextClearing] = []
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


def _extract_plan(phases: dict) -> list[PhaseContextClearing]:
    entries = phases.get("plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseContextClearing] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_CONTEXT_CLEARING_KEY)
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
            PhaseContextClearing(
                phase="plan",
                trace_surface=trace_surface,
                attempt=attempt,
                round=None,
                source_path=f"phases.plan[{idx}].context_clearing",
                payload=normalize_context_clearing(payload),
            ),
        )
    return out


def _extract_validate_plan(phases: dict) -> list[PhaseContextClearing]:
    entries = phases.get("validate_plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseContextClearing] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_CONTEXT_CLEARING_KEY)
        if not isinstance(payload, dict):
            continue
        attempt_raw = entry.get("attempt", 1)
        attempt = int(attempt_raw) if isinstance(attempt_raw, int) else 1
        out.append(
            PhaseContextClearing(
                phase="validate_plan",
                trace_surface="validate_plan",
                attempt=attempt,
                round=None,
                source_path=f"phases.validate_plan[{idx}].context_clearing",
                payload=normalize_context_clearing(payload),
            ),
        )
    return out


def _extract_implement(phases: dict) -> list[PhaseContextClearing]:
    entry = phases.get("implement")
    if not isinstance(entry, dict):
        return []
    payload = entry.get(_CONTEXT_CLEARING_KEY)
    if not isinstance(payload, dict):
        return []
    return [
        PhaseContextClearing(
            phase="implement",
            trace_surface="implement",
            attempt=None,
            round=None,
            source_path="phases.implement.context_clearing",
            payload=normalize_context_clearing(payload),
        ),
    ]


def _extract_rounds(phases: dict) -> list[PhaseContextClearing]:
    """Walk ``phases.rounds[]``. Each round entry may carry two
    independent context-clearing records — ``context_clearing_review``
    (reviewer side) and ``context_clearing_repair`` (CHAIN-attributed
    repair side). Both surface as separate trace records when present.
    """
    entries = phases.get("rounds")
    if not isinstance(entries, list):
        return []
    out: list[PhaseContextClearing] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        round_raw = entry.get("round")
        round_n = int(round_raw) if isinstance(round_raw, int) else None
        review_payload = entry.get(_CONTEXT_CLEARING_REVIEW_KEY)
        if isinstance(review_payload, dict):
            out.append(
                PhaseContextClearing(
                    phase="review_changes",
                    trace_surface="review_changes",
                    attempt=None,
                    round=round_n,
                    source_path=(
                        f"phases.rounds[{idx}].context_clearing_review"
                    ),
                    payload=normalize_context_clearing(review_payload),
                ),
            )
        repair_payload = entry.get(_CONTEXT_CLEARING_REPAIR_KEY)
        if isinstance(repair_payload, dict):
            out.append(
                PhaseContextClearing(
                    phase="repair_changes",
                    trace_surface="repair_changes",
                    attempt=None,
                    round=round_n,
                    source_path=(
                        f"phases.rounds[{idx}].context_clearing_repair"
                    ),
                    payload=normalize_context_clearing(repair_payload),
                ),
            )
    return out


__all__ = [
    "DURABLE_FIELDS",
    "PhaseContextClearing",
    "extract_context_clearing_traces",
    "normalize_context_clearing",
]
