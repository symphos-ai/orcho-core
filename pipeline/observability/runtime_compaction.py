"""M14.4+ — Runtime auto-compaction evidence (ADR 0029).

Observe-only sibling of M14.4
:mod:`pipeline.observability.context_pressure`. Where
``context_pressure`` records *how full the active context window is*,
``runtime_compaction`` records *the fact that the runtime compacted
itself* — a discrete event surfaced by Claude CLI / Codex CLI / any
future provider that exposes an auto-compact signal (event hook,
response-header field, log line — whichever surfaces first).

No runtime exposes this signal today. The module ships the durable
event shape, the normalizer, the resolver branch that reads
``agent.last_runtime_compaction_event`` if/when one appears, and the
recovery-contract validator that joins the event back to the
protected ``coding_agent_compaction`` preserve-list. When a runtime
plumbs the signal through, the writer's stamp flips on automatically
— no consumer code change required, same forward-compat normalizer
pattern as M14.1 / M14.3.

## Observe-only invariants (carried from M14.4)

* No auto-compaction is triggered by Orcho. The event is *observed
  evidence* — the runtime decided to compact, Orcho records that it
  happened.
* No runtime mutation. The resolver reads agent attributes only.
* No prompt-wire change. The compaction contract template lives in
  ``pipeline.prompts.contract_templates``; this module never renders
  prompt bytes.

## Evidence model

The durable shape carries the event identity + attribution +
pre/post token observables + a recovery hint listing which preserve
slots the runtime claims to have covered via artifacts.

* ``kind`` defaults to ``"runtime_auto_compacted"``. The normalizer
  accepts the value as-is when present so a future runtime can
  introduce sub-kinds (``"runtime_manual_compacted"``,
  ``"runtime_summary_only"``) without churning the contract;
  unrecognised values stay verbatim rather than being clamped, so a
  consumer can still inspect them.
* ``trigger`` records *which surface emitted the signal* —
  ``"event_hook"``, ``"response_header"``, ``"log_line"``, or
  ``"unknown"``. Useful for debugging when two surfaces disagree.
* ``pre_used_tokens`` / ``post_used_tokens`` are the runtime's
  best-effort fullness readings *just before* and *just after* the
  compaction. Either may be ``None`` when the source did not supply
  it; consumers must handle ``None`` everywhere.
* ``summary_tokens`` is the size of the compacted summary itself,
  if the runtime exposed it.
* ``prefix_hash`` / ``payload_hash`` / ``wire_chars`` correlate
  the event back to the M2 render envelope of the invocation that
  observed it (same shape as ``context_pressure`` /
  ``context_clearing`` / ``context_growth``).
* ``preserved_slots`` is the list of
  :data:`REQUIRED_COMPACTION_PRESERVE_FIELDS` slot names the runtime
  claims its summary preserves via artifact refs. The recovery
  validator joins this list against session-derived evidence and
  reports missing slots — it does not raise.
* ``artifact_refs`` is a list of dicts (or bare strings) pointing to
  the on-disk artifacts the runtime emitted alongside the summary.
  The normalizer copies the list defensively but does not validate
  its shape — a future runtime may stamp a richer record.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.prompts.contract_templates import (
    REQUIRED_COMPACTION_PRESERVE_FIELDS,
)

# ── M14.4+: durable runtime-compaction shape ─────────────────────────────────
#
# 13 fields total. Forward-compat normalizer (M14.x style): unknown
# / missing inputs project to safe defaults; unknown keys on the
# input are dropped silently (the durable shape is the contract).
DURABLE_FIELDS: tuple[str, ...] = (
    # Event identity.
    "kind",
    "trigger",
    # Attribution.
    "phase",
    "round",
    "surface_id",
    # Pre/post fullness observables.
    "pre_used_tokens",
    "post_used_tokens",
    "summary_tokens",
    # Correlation (back to prompt_render / context_growth /
    # context_clearing / context_pressure on the same invocation).
    "prefix_hash",
    "payload_hash",
    "wire_chars",
    # Recovery-contract hint + artifact pointers.
    "preserved_slots",
    "artifact_refs",
)


_DEFAULT_KIND = "runtime_auto_compacted"
_DEFAULT_TRIGGER = "unknown"
_VALID_TRIGGERS: frozenset[str] = frozenset({
    "event_hook",
    "response_header",
    "log_line",
    "unknown",
})


def normalize_runtime_compaction_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the source runtime-compaction dict into the durable shape.

    Pure function — does not mutate *payload*. Projection rules:

    - ``kind`` defaults to ``"runtime_auto_compacted"``. Non-string
      values fall back to the default; recognised string values pass
      through so a future runtime can introduce sub-kinds without
      churning the contract.
    - ``trigger`` defaults to ``"unknown"``. Values outside the
      canonical surface set fall back to ``"unknown"``
      (defense-in-depth: a runtime drift cannot smuggle a noisy
      label into the trace).
    - Attribution slots (``phase`` / ``round`` / ``surface_id``)
      pass through; ``None`` when missing.
    - Numeric observables (``pre_used_tokens`` / ``post_used_tokens``
      / ``summary_tokens``) pass through; ``None`` when the source
      did not supply the value. Negative or non-int values clamp to
      ``None`` (defensive — the surface is observe-only, a bogus
      reading is worse than no reading).
    - Render-correlation slots (``prefix_hash`` / ``payload_hash`` /
      ``wire_chars``) mirror the sibling records.
    - ``preserved_slots`` defaults to an empty list. The normalizer
      filters to canonical
      :data:`REQUIRED_COMPACTION_PRESERVE_FIELDS` names so a runtime
      cannot inject phantom slot names.
    - ``artifact_refs`` defaults to an empty list. Copied into a
      fresh list instance so consumer mutation does not bleed into
      the source payload.

    The returned dict's key set is exactly :data:`DURABLE_FIELDS`.
    """
    raw_kind = payload.get("kind", _DEFAULT_KIND)
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind else _DEFAULT_KIND

    raw_trigger = payload.get("trigger", _DEFAULT_TRIGGER)
    trigger = (
        raw_trigger
        if isinstance(raw_trigger, str) and raw_trigger in _VALID_TRIGGERS
        else _DEFAULT_TRIGGER
    )

    raw_preserved = payload.get("preserved_slots") or []
    canonical = set(REQUIRED_COMPACTION_PRESERVE_FIELDS)
    preserved_slots: list[str] = [
        s for s in raw_preserved if isinstance(s, str) and s in canonical
    ]

    return {
        "kind": kind,
        "trigger": trigger,
        "phase": payload.get("phase"),
        "round": payload.get("round"),
        "surface_id": payload.get("surface_id"),
        "pre_used_tokens": _coerce_token_count(payload.get("pre_used_tokens")),
        "post_used_tokens": _coerce_token_count(payload.get("post_used_tokens")),
        "summary_tokens": _coerce_token_count(payload.get("summary_tokens")),
        "prefix_hash": payload.get("prefix_hash"),
        "payload_hash": payload.get("payload_hash"),
        "wire_chars": payload.get("wire_chars"),
        "preserved_slots": preserved_slots,
        "artifact_refs": list(payload.get("artifact_refs") or []),
    }


def _coerce_token_count(value: Any) -> int | None:
    """Clamp a token-count reading to ``int >= 0`` or ``None``.

    Booleans are ints in Python; reject them explicitly so a stray
    ``True`` does not project to ``1``. Negative ints clamp to
    ``None`` — a negative reading is a runtime bug, not signal.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


# ── Resolver branch (writer-side helper) ─────────────────────────────────────


@dataclass(frozen=True)
class RuntimeCompactionEvent:
    """Resolved runtime-compaction event the writer will stamp.

    Returned by :func:`resolve_runtime_compaction_event` when the
    agent exposes ``last_runtime_compaction_event``. Each numeric
    slot is ``int | None``; ``None`` means "this source did not
    supply the value".
    """

    kind: str
    trigger: str
    pre_used_tokens: int | None
    post_used_tokens: int | None
    summary_tokens: int | None
    preserved_slots: tuple[str, ...]
    artifact_refs: tuple[Any, ...]


def resolve_runtime_compaction_event(
    agent: Any,
) -> RuntimeCompactionEvent | None:
    """Resolve the runtime's last auto-compaction event if exposed.

    The agent attribute ``last_runtime_compaction_event`` is the
    seam. When non-``None`` and dict-shaped, the writer stamps a
    ``runtime_compaction`` record alongside the existing
    ``context_pressure`` record. When absent or shaped unexpectedly,
    the resolver returns ``None`` and the writer omits the record.

    The function is pure: it reads the agent attribute only, never
    mutates it. Forward-compat — no runtime exposes the attribute
    today; the branch wakes up automatically when one does.
    """
    raw = getattr(agent, "last_runtime_compaction_event", None)
    if not isinstance(raw, dict):
        return None
    normalized = normalize_runtime_compaction_event(raw)
    return RuntimeCompactionEvent(
        kind=normalized["kind"],
        trigger=normalized["trigger"],
        pre_used_tokens=normalized["pre_used_tokens"],
        post_used_tokens=normalized["post_used_tokens"],
        summary_tokens=normalized["summary_tokens"],
        preserved_slots=tuple(normalized["preserved_slots"]),
        artifact_refs=tuple(normalized["artifact_refs"]),
    )


# ── Recovery-contract validator ──────────────────────────────────────────────
#
# Maps each :data:`REQUIRED_COMPACTION_PRESERVE_FIELDS` slot to the
# session keys that, if present and truthy, satisfy the slot
# without an artifact ref. The map is intentionally loose — a future
# session shape change adds keys here rather than changing the
# validator's structure.
_SESSION_SLOT_SOURCES: dict[str, tuple[str, ...]] = {
    "task_and_acceptance": ("task", "acceptance_criteria"),
    "approved_plan_and_non_goals": (
        "plan_markdown",
        "approved_plan",
        "non_goals",
    ),
    "files_read_and_changed": (
        "files_read",
        "files_changed",
        "change_handoff",
    ),
    "code_identifiers_and_schemas": (
        "code_identifiers",
        "schemas",
    ),
    "commands_tests_and_outcomes": (
        "commands_run",
        "tests_run",
        "phase_log",
        "phases",
    ),
    "errors_that_still_matter": (
        "errors",
        "blockers",
        "errors_that_still_matter",
    ),
    "review_findings_and_blockers": (
        "review_findings",
        "release_blockers",
    ),
    "verification_gaps": ("verification_gaps",),
    "assumptions_and_open_questions": (
        "assumptions",
        "open_questions",
    ),
    "risks": ("risks",),
    "phase_round_surface_session_metadata": (
        "run_id",
        "phases",
        "extras",
    ),
}


@dataclass(frozen=True)
class RecoveryValidationResult:
    """Outcome of the runtime-compaction recovery-contract check.

    Observe-only: missing slots are reported, never raised. The
    caller decides what to do (log, surface in dashboard, gate a
    future automatic-resume action).
    """

    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    by_artifact: tuple[str, ...]
    by_session: tuple[str, ...]


def validate_compaction_recovery(
    session: dict,
    event: dict | RuntimeCompactionEvent | None,
) -> RecoveryValidationResult:
    """Check that the compaction event preserves every required slot.

    For each slot in
    :data:`REQUIRED_COMPACTION_PRESERVE_FIELDS`, the slot is
    satisfied when **either**:

    - the event lists the slot under ``preserved_slots`` (the
      runtime claims to have stamped an artifact for it), or
    - the session carries a truthy value at one of the
      :data:`_SESSION_SLOT_SOURCES` paths for that slot.

    Missing slots are returned, not raised. ``event=None`` is
    accepted — every slot then falls back to session-derived
    evidence only.

    The function is pure: it reads *session* and *event* only.
    """
    if isinstance(event, RuntimeCompactionEvent):
        preserved = set(event.preserved_slots)
    elif isinstance(event, dict):
        preserved = set(
            normalize_runtime_compaction_event(event)["preserved_slots"],
        )
    else:
        preserved = set()

    session_dict = session if isinstance(session, dict) else {}

    satisfied: list[str] = []
    missing: list[str] = []
    by_artifact: list[str] = []
    by_session: list[str] = []

    for slot in REQUIRED_COMPACTION_PRESERVE_FIELDS:
        if slot in preserved:
            satisfied.append(slot)
            by_artifact.append(slot)
            continue
        if _session_satisfies_slot(session_dict, slot):
            satisfied.append(slot)
            by_session.append(slot)
            continue
        missing.append(slot)

    return RecoveryValidationResult(
        satisfied=tuple(satisfied),
        missing=tuple(missing),
        by_artifact=tuple(by_artifact),
        by_session=tuple(by_session),
    )


def _session_satisfies_slot(session: dict, slot: str) -> bool:
    sources = _SESSION_SLOT_SOURCES.get(slot, ())
    for key in sources:
        value = session.get(key)
        if value:  # truthy: non-empty dict / list / non-empty string / non-zero
            return True
    return False


# ── Extractor (session-walk) ─────────────────────────────────────────────────
#
# Mirror of context_pressure's extractor for the same coverage
# taxonomy. Records appear under ``state.phase_log[phase]
# ["runtime_compaction"]`` only when the writer's resolver branch
# fired (i.e. the agent exposed ``last_runtime_compaction_event``),
# so the extractor's record count tracks observed compactions.


@dataclass(frozen=True)
class PhaseRuntimeCompaction:
    """One durable runtime-compaction trace record."""

    phase: str
    trace_surface: str
    attempt: int | None
    round: int | None
    source_path: str
    payload: dict[str, Any]


_PHASES_KEY = "phases"
_RUNTIME_COMPACTION_KEY = "runtime_compaction"
_RUNTIME_COMPACTION_REVIEW_KEY = "runtime_compaction_review"
_RUNTIME_COMPACTION_REPAIR_KEY = "runtime_compaction_repair"


def extract_runtime_compaction_traces(
    session: dict,
) -> list[PhaseRuntimeCompaction]:
    """Walk *session* and return one record per stamped event."""
    phases = _phases_from_session(session)
    if phases is None:
        return []

    traces: list[PhaseRuntimeCompaction] = []
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


def _extract_plan(phases: dict) -> list[PhaseRuntimeCompaction]:
    entries = phases.get("plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseRuntimeCompaction] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_RUNTIME_COMPACTION_KEY)
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
            PhaseRuntimeCompaction(
                phase="plan",
                trace_surface=trace_surface,
                attempt=attempt,
                round=None,
                source_path=f"phases.plan[{idx}].runtime_compaction",
                payload=normalize_runtime_compaction_event(payload),
            ),
        )
    return out


def _extract_validate_plan(phases: dict) -> list[PhaseRuntimeCompaction]:
    entries = phases.get("validate_plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseRuntimeCompaction] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_RUNTIME_COMPACTION_KEY)
        if not isinstance(payload, dict):
            continue
        attempt_raw = entry.get("attempt", 1)
        attempt = int(attempt_raw) if isinstance(attempt_raw, int) else 1
        out.append(
            PhaseRuntimeCompaction(
                phase="validate_plan",
                trace_surface="validate_plan",
                attempt=attempt,
                round=None,
                source_path=(
                    f"phases.validate_plan[{idx}].runtime_compaction"
                ),
                payload=normalize_runtime_compaction_event(payload),
            ),
        )
    return out


def _extract_implement(phases: dict) -> list[PhaseRuntimeCompaction]:
    entry = phases.get("implement")
    if not isinstance(entry, dict):
        return []
    payload = entry.get(_RUNTIME_COMPACTION_KEY)
    if not isinstance(payload, dict):
        return []
    return [
        PhaseRuntimeCompaction(
            phase="implement",
            trace_surface="implement",
            attempt=None,
            round=None,
            source_path="phases.implement.runtime_compaction",
            payload=normalize_runtime_compaction_event(payload),
        ),
    ]


def _extract_rounds(phases: dict) -> list[PhaseRuntimeCompaction]:
    entries = phases.get("rounds")
    if not isinstance(entries, list):
        return []
    out: list[PhaseRuntimeCompaction] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        round_raw = entry.get("round")
        round_n = int(round_raw) if isinstance(round_raw, int) else None
        review_payload = entry.get(_RUNTIME_COMPACTION_REVIEW_KEY)
        if isinstance(review_payload, dict):
            out.append(
                PhaseRuntimeCompaction(
                    phase="review_changes",
                    trace_surface="review_changes",
                    attempt=None,
                    round=round_n,
                    source_path=(
                        f"phases.rounds[{idx}].runtime_compaction_review"
                    ),
                    payload=normalize_runtime_compaction_event(review_payload),
                ),
            )
        repair_payload = entry.get(_RUNTIME_COMPACTION_REPAIR_KEY)
        if isinstance(repair_payload, dict):
            out.append(
                PhaseRuntimeCompaction(
                    phase="repair_changes",
                    trace_surface="repair_changes",
                    attempt=None,
                    round=round_n,
                    source_path=(
                        f"phases.rounds[{idx}].runtime_compaction_repair"
                    ),
                    payload=normalize_runtime_compaction_event(repair_payload),
                ),
            )
    return out


def latest_runtime_compaction_event(session: dict) -> dict | None:
    """Return the most-recent normalised compaction event in *session*.

    "Most recent" follows extractor order: plan → validate_plan →
    implement → rounds. Rounds are walked in order; round entries
    later in the list are considered more recent. Within a round
    entry the repair side is more recent than the review side
    (repair always follows review in the loop).

    Returns ``None`` when no compaction event is stamped anywhere
    in the session.
    """
    traces = extract_runtime_compaction_traces(session)
    if not traces:
        return None
    return traces[-1].payload


__all__ = [
    "DURABLE_FIELDS",
    "PhaseRuntimeCompaction",
    "RecoveryValidationResult",
    "RuntimeCompactionEvent",
    "extract_runtime_compaction_traces",
    "latest_runtime_compaction_event",
    "normalize_runtime_compaction_event",
    "resolve_runtime_compaction_event",
    "validate_compaction_recovery",
]
