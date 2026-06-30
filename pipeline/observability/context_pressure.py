"""M14.4 — Runtime context-pressure telemetry surface (ADR 0029).

Observe-only telemetry sibling of M14.1 ``context_growth`` and
M14.3 ``context_clearing``. Where context_growth describes
*per-invocation token observables* and context_clearing describes
*per-invocation eligibility evidence*, context_pressure describes
*runtime context fullness*: which source the values came from
(runtime-reported live indicator → provider usage bucket → Orcho's
own estimate → static config → unknown), how full the active
context window is, and which source any future automatic
compaction trigger would have used.

ADR 0029 §"Runtime context source hierarchy" locks the contract:

```text
runtime_reported  -> runtime exposes live used/remaining/window context
provider_usage    -> provider usage buckets + known model window
orcho_estimated   -> Orcho prompt/session estimate
config_static     -> configured model window / trigger token fallback
unknown           -> no automatic lossy action
```

``trigger_tokens`` values from config are fallback hints, not the
source of truth. Future M14.4+ automatic compaction must prefer
``context_fill_ratio`` from a higher-source-hierarchy reading over
a static token threshold.

## M14.4 observe-first scope

M14.4 lands the surface + writer stamp + classifier integration
but does **not** trigger any compaction. The writer resolves the
best available source today (typically ``orcho_estimated`` from
``agent.last_estimated_tokens_in`` when the runtime exposes it,
otherwise ``unknown``) and records both the source label and the
numeric values when meaningful. When a runtime ships a
machine-readable context indicator (Claude/Codex CLI tools may
expose this in future releases), the writer's
``_resolve_pressure`` helper flips to ``runtime_reported`` and
populates ``context_window_tokens`` / ``context_used_tokens`` /
``context_remaining_tokens`` / ``context_fill_ratio``. No
consumer code change required — the surface contract is stable
from M14.4 onwards.

Coverage taxonomy mirrors context_growth / context_clearing:

- ``plan`` / ``replan`` → ``session.phases.plan[].context_pressure``
- ``validate_plan`` → ``session.phases.validate_plan[].context_pressure``
- ``implement`` → ``session.phases.implement.context_pressure``
- ``review_changes`` → ``session.phases.rounds[].context_pressure_review``
- ``repair_changes`` → ``session.phases.rounds[].context_pressure_repair``

The extractor is pure: it never mutates the input session and
never fabricates records for surfaces that did not stamp one.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ContextSource(StrEnum):
    """Source of the context-pressure reading (ADR 0029 hierarchy).

    Ordered from highest authority to lowest:

    - :data:`RUNTIME_REPORTED` — the runtime exposed a live
      used / remaining / window indicator. This is the only
      reading that captures hidden runtime overhead
      (tool-result history, session prefix accounting,
      provider-side compaction state) Orcho cannot derive from
      rendered-prompt bytes.
    - :data:`PROVIDER_USAGE` — the runtime exposed per-call
      usage buckets and a known effective model window;
      pressure derived from accumulated usage / window.
    - :data:`ORCHO_ESTIMATED` — Orcho's own prompt-side
      estimate (``estimate_tokens`` over the wire prompt /
      cumulative session bytes). Conservative — undercounts
      runtime overhead.
    - :data:`CONFIG_STATIC` — the static-config fallback (a
      configured model window + a threshold). The weakest
      authority; should drive automatic action only when no
      higher-authority source is available.
    - :data:`UNKNOWN` — no usable source. Automatic lossy
      actions must not fire when the source is ``UNKNOWN``.
    """

    RUNTIME_REPORTED = "runtime_reported"
    PROVIDER_USAGE = "provider_usage"
    ORCHO_ESTIMATED = "orcho_estimated"
    CONFIG_STATIC = "config_static"
    UNKNOWN = "unknown"


# Ordered from highest to lowest authority. The resolver picks
# the first available source.
SOURCE_PRIORITY: tuple[ContextSource, ...] = (
    ContextSource.RUNTIME_REPORTED,
    ContextSource.PROVIDER_USAGE,
    ContextSource.ORCHO_ESTIMATED,
    ContextSource.CONFIG_STATIC,
    ContextSource.UNKNOWN,
)


# ── M14.4: durable context-pressure shape ────────────────────────────────────
#
# 17 fields total. 3 attribution + 1 source label + 4 numeric
# readings + 1 trigger-source label + 5 session identity slots +
# 3 render correlation slots.
# No reserved cleared-side fields — context_pressure describes
# fullness, not action; M14.3 already owns the action side.
#
# Numeric readings are ``int | None`` (``float | None`` for the
# ratio). ``None`` means "not available from the source the writer
# chose"; a static-config fallback typically returns a window value
# and nothing else. Consumers must handle ``None`` everywhere —
# never substitute a zero or an estimate.
DURABLE_FIELDS: tuple[str, ...] = (
    # Attribution.
    "phase",
    "round",
    "surface_id",
    # Source the writer resolved this reading from.
    "context_source",
    # Numeric readings (may be None when the source did not supply
    # the value).
    "context_window_tokens",
    "context_used_tokens",
    "context_remaining_tokens",
    "context_fill_ratio",
    # The source any future automatic compaction trigger would
    # have used. Typically the same as ``context_source``, but a
    # future writer may resolve a higher-authority reading for
    # measurement while triggering on a different fallback (e.g.
    # context_source=RUNTIME_REPORTED, trigger_source=CONFIG_STATIC
    # if the runtime value is missing for that single field).
    "trigger_source",
    # Physical prompt/context session identity. These fields let run-level
    # summaries group context windows without summing unrelated sessions.
    "session_split",
    "session_key",
    "provider_session_id",
    "runtime",
    "model",
    # Render correlation (back to prompt_render / context_growth /
    # context_clearing).
    "prefix_hash",
    "payload_hash",
    "wire_chars",
)


def normalize_context_pressure(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the source ``context_pressure`` dict into the durable shape.

    Pure function — does not mutate *payload*. Projection rules:

    - ``context_source`` defaults to ``"unknown"``. Unknown
      values fall back to ``"unknown"`` (defense-in-depth:
      a writer drift cannot smuggle a non-canonical label
      through).
    - ``trigger_source`` defaults to the same value as
      ``context_source`` (today they always match; a future
      writer may diverge them).
    - Numeric readings (``context_window_tokens`` /
      ``context_used_tokens`` / ``context_remaining_tokens``)
      pass through; ``None`` when missing.
    - ``context_fill_ratio`` passes through as
      ``float | None``; ``None`` when the source did not
      supply ratio-bearing values.
    - Attribution + render-correlation slots pass through.

    The returned dict's key set is exactly :data:`DURABLE_FIELDS`.
    """
    valid_sources = {c.value for c in ContextSource}

    raw_source = payload.get("context_source", ContextSource.UNKNOWN.value)
    context_source = raw_source if raw_source in valid_sources else (
        ContextSource.UNKNOWN.value
    )
    raw_trigger = payload.get("trigger_source", context_source)
    trigger_source = raw_trigger if raw_trigger in valid_sources else (
        ContextSource.UNKNOWN.value
    )

    return {
        "phase": payload.get("phase"),
        "round": payload.get("round"),
        "surface_id": payload.get("surface_id"),
        "context_source": context_source,
        "context_window_tokens": payload.get("context_window_tokens"),
        "context_used_tokens": payload.get("context_used_tokens"),
        "context_remaining_tokens": payload.get("context_remaining_tokens"),
        "context_fill_ratio": payload.get("context_fill_ratio"),
        "trigger_source": trigger_source,
        "session_split": payload.get("session_split"),
        "session_key": payload.get("session_key"),
        "provider_session_id": payload.get("provider_session_id"),
        "runtime": payload.get("runtime"),
        "model": payload.get("model"),
        "prefix_hash": payload.get("prefix_hash"),
        "payload_hash": payload.get("payload_hash"),
        "wire_chars": payload.get("wire_chars"),
    }


@dataclass(frozen=True)
class PhaseContextPressure:
    """One durable context-pressure trace record.

    Sibling of :class:`PhaseContextGrowth` and
    :class:`PhaseContextClearing`. Consumers join the three
    surfaces by ``(phase, trace_surface, attempt, round)`` to
    align telemetry, growth observables, and eligibility evidence
    for the same invocation.
    """

    phase: str
    trace_surface: str
    attempt: int | None
    round: int | None
    source_path: str
    payload: dict[str, Any]


# ── Pressure-reading resolver (writer-side helper) ───────────────────────────


@dataclass(frozen=True)
class PressureReading:
    """Resolved context-pressure values + the source they came from.

    Returned by :func:`resolve_context_pressure`. Each numeric
    slot is ``int | None`` (``float | None`` for the ratio);
    ``None`` means "this source did not supply the value".
    """

    context_source: ContextSource
    context_window_tokens: int | None
    context_used_tokens: int | None
    context_remaining_tokens: int | None
    context_fill_ratio: float | None
    trigger_source: ContextSource


def resolve_context_pressure(
    agent: Any,
    *,
    config_window_tokens: int | None = None,
) -> PressureReading:
    """Resolve the best available context-pressure reading for *agent*.

    Walks :data:`SOURCE_PRIORITY` from highest authority to
    lowest and returns the first source that yields a usable
    reading. M14.4 ships the lowest two branches today
    (``orcho_estimated`` from
    ``agent.last_estimated_tokens_in``; ``config_static`` from
    *config_window_tokens* when set; otherwise ``unknown``).
    When Claude / Codex CLI expose a live indicator, the
    runtime-reported branch lands here without a contract
    change — consumers already handle every source uniformly via
    the ``context_source`` label.

    The function is pure: it reads *agent* attributes and the
    *config_window_tokens* hint, never mutates them.

    ``trigger_source`` mirrors ``context_source`` today. A future
    writer that uses a higher-authority source for measurement
    while triggering on a fallback may diverge them — the
    contract already accommodates that.
    """
    # Branch 1 — RUNTIME_REPORTED.
    # Reserved for future runtime telemetry attributes such as
    # ``agent.last_context_window`` / ``agent.last_context_used``.
    # No runtime exposes them today; the branch stays inactive
    # until one does, at which point the resolver flips
    # automatically.
    window = getattr(agent, "last_context_window_tokens", None)
    used = getattr(agent, "last_context_used_tokens", None)
    remaining = getattr(agent, "last_context_remaining_tokens", None)
    if isinstance(window, int) and isinstance(used, int):
        ratio: float | None = (
            float(used) / float(window) if window > 0 else None
        )
        return PressureReading(
            context_source=ContextSource.RUNTIME_REPORTED,
            context_window_tokens=window,
            context_used_tokens=used,
            context_remaining_tokens=(
                int(remaining) if isinstance(remaining, int)
                else (window - used if window >= used else None)
            ),
            context_fill_ratio=ratio,
            trigger_source=ContextSource.RUNTIME_REPORTED,
        )

    # Branch 2 — PROVIDER_USAGE.
    # Reserved for runtimes that expose accumulated usage
    # buckets without a live fullness indicator. Not implemented
    # today; falls through to branch 3.
    # (Documented branch — kept in the resolver shape so a
    # future change adds the wiring without altering the
    # signature.)

    # Branch 3 — ORCHO_ESTIMATED.
    # Today's working branch. Reads the runtime's per-call
    # ``last_estimated_tokens_in`` (the prompt token estimate
    # the runtime stamped after invoke). This undercounts hidden
    # runtime overhead (tool-result history, session prefix
    # accounting) but is honest: the writer labels it
    # explicitly as ``orcho_estimated`` so consumers know not to
    # treat it as authoritative.
    last_in = getattr(agent, "last_estimated_tokens_in", None)
    if isinstance(last_in, int) and last_in > 0:
        ratio_o: float | None = None
        remaining_o: int | None = None
        if (
            isinstance(config_window_tokens, int)
            and config_window_tokens > 0
        ):
            # Static-config window is the weakest authority, but
            # combining it with the orcho_estimated used value
            # gives a meaningful fill ratio while keeping the
            # source label honest.
            ratio_o = float(last_in) / float(config_window_tokens)
            remaining_o = max(0, config_window_tokens - last_in)
        return PressureReading(
            context_source=ContextSource.ORCHO_ESTIMATED,
            context_window_tokens=(
                config_window_tokens
                if isinstance(config_window_tokens, int) else None
            ),
            context_used_tokens=last_in,
            context_remaining_tokens=remaining_o,
            context_fill_ratio=ratio_o,
            trigger_source=ContextSource.ORCHO_ESTIMATED,
        )

    # Branch 4 — CONFIG_STATIC.
    # Pure-config fallback: we know the window but the runtime
    # exposed no usage. The reading carries the window only; a
    # future writer with a configured threshold can still
    # trigger on it but the label warns it is the weakest
    # source.
    if isinstance(config_window_tokens, int) and config_window_tokens > 0:
        return PressureReading(
            context_source=ContextSource.CONFIG_STATIC,
            context_window_tokens=config_window_tokens,
            context_used_tokens=None,
            context_remaining_tokens=None,
            context_fill_ratio=None,
            trigger_source=ContextSource.CONFIG_STATIC,
        )

    # Branch 5 — UNKNOWN.
    return PressureReading(
        context_source=ContextSource.UNKNOWN,
        context_window_tokens=None,
        context_used_tokens=None,
        context_remaining_tokens=None,
        context_fill_ratio=None,
        trigger_source=ContextSource.UNKNOWN,
    )


# ── Extractor ────────────────────────────────────────────────────────────────


_PHASES_KEY = "phases"
_CONTEXT_PRESSURE_KEY = "context_pressure"
_CONTEXT_PRESSURE_REVIEW_KEY = "context_pressure_review"
_CONTEXT_PRESSURE_REPAIR_KEY = "context_pressure_repair"


def extract_context_pressure_traces(
    session: dict,
) -> list[PhaseContextPressure]:
    """Walk *session* and return one record per covered
    ``context_pressure`` payload.

    Mirrors ``extract_context_clearing_traces`` coverage:

    - ``plan`` / ``replan`` → ``session.phases.plan[].context_pressure``
    - ``validate_plan`` → ``session.phases.validate_plan[].context_pressure``
    - ``implement`` → ``session.phases.implement.context_pressure``
    - ``review_changes`` → ``session.phases.rounds[].context_pressure_review``
    - ``repair_changes`` → ``session.phases.rounds[].context_pressure_repair``
    """
    phases = _phases_from_session(session)
    if phases is None:
        return []

    traces: list[PhaseContextPressure] = []
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


def _extract_plan(phases: dict) -> list[PhaseContextPressure]:
    entries = phases.get("plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseContextPressure] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_CONTEXT_PRESSURE_KEY)
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
            PhaseContextPressure(
                phase="plan",
                trace_surface=trace_surface,
                attempt=attempt,
                round=None,
                source_path=f"phases.plan[{idx}].context_pressure",
                payload=normalize_context_pressure(payload),
            ),
        )
    return out


def _extract_validate_plan(phases: dict) -> list[PhaseContextPressure]:
    entries = phases.get("validate_plan")
    if not isinstance(entries, list):
        return []
    out: list[PhaseContextPressure] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        payload = entry.get(_CONTEXT_PRESSURE_KEY)
        if not isinstance(payload, dict):
            continue
        attempt_raw = entry.get("attempt", 1)
        attempt = int(attempt_raw) if isinstance(attempt_raw, int) else 1
        out.append(
            PhaseContextPressure(
                phase="validate_plan",
                trace_surface="validate_plan",
                attempt=attempt,
                round=None,
                source_path=f"phases.validate_plan[{idx}].context_pressure",
                payload=normalize_context_pressure(payload),
            ),
        )
    return out


def _extract_implement(phases: dict) -> list[PhaseContextPressure]:
    entry = phases.get("implement")
    if not isinstance(entry, dict):
        return []
    payload = entry.get(_CONTEXT_PRESSURE_KEY)
    if not isinstance(payload, dict):
        return []
    return [
        PhaseContextPressure(
            phase="implement",
            trace_surface="implement",
            attempt=None,
            round=None,
            source_path="phases.implement.context_pressure",
            payload=normalize_context_pressure(payload),
        ),
    ]


def _extract_rounds(phases: dict) -> list[PhaseContextPressure]:
    entries = phases.get("rounds")
    if not isinstance(entries, list):
        return []
    out: list[PhaseContextPressure] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        round_raw = entry.get("round")
        round_n = int(round_raw) if isinstance(round_raw, int) else None
        review_payload = entry.get(_CONTEXT_PRESSURE_REVIEW_KEY)
        if isinstance(review_payload, dict):
            out.append(
                PhaseContextPressure(
                    phase="review_changes",
                    trace_surface="review_changes",
                    attempt=None,
                    round=round_n,
                    source_path=(
                        f"phases.rounds[{idx}].context_pressure_review"
                    ),
                    payload=normalize_context_pressure(review_payload),
                ),
            )
        repair_payload = entry.get(_CONTEXT_PRESSURE_REPAIR_KEY)
        if isinstance(repair_payload, dict):
            out.append(
                PhaseContextPressure(
                    phase="repair_changes",
                    trace_surface="repair_changes",
                    attempt=None,
                    round=round_n,
                    source_path=(
                        f"phases.rounds[{idx}].context_pressure_repair"
                    ),
                    payload=normalize_context_pressure(repair_payload),
                ),
            )
    return out


# ── Human-readable summary (CLI-facing) ──────────────────────────────────────


def _format_token_count(n: int) -> str:
    """Render ``193600 -> '193.6k'``, ``1000000 -> '1.0M'`` etc.

    Matches the Claude Code CLI ``Context window`` line styling so
    Orcho's run summary feels visually consistent for users who jump
    between the two surfaces. No localisation — ``.`` decimal, ``k``
    / ``M`` / ``G`` suffixes; ``< 1000`` shows the raw integer.
    """
    if n < 1_000:
        return str(n)
    for divisor, suffix in ((1_000_000_000, "G"), (1_000_000, "M"), (1_000, "k")):
        if n >= divisor:
            value = n / divisor
            return f"{value:.1f}{suffix}"
    return str(n)


def _pressure_rank(t: PhaseContextPressure) -> tuple[float, int]:
    ratio = t.payload.get("context_fill_ratio")
    used = t.payload.get("context_used_tokens") or 0
    return (
        float(ratio) if isinstance(ratio, (int, float)) else -1.0,
        int(used) if isinstance(used, int) else 0,
    )


def _format_pressure_core(payload: dict[str, Any]) -> str:
    used = payload.get("context_used_tokens")
    window = payload.get("context_window_tokens")
    ratio = payload.get("context_fill_ratio")
    if isinstance(window, int) and isinstance(used, int) and window > 0:
        pct_value = ratio if isinstance(ratio, (int, float)) else used / window
        pct = int(round(pct_value * 100))
        return (
            f"{_format_token_count(used)} / {_format_token_count(window)} "
            f"({pct}%)"
        )
    if isinstance(used, int) and used > 0:
        return f"{_format_token_count(used)} used"
    if isinstance(window, int) and window > 0:
        return f"/ {_format_token_count(window)} (window known)"
    return "(no numeric reading)"


def _runtime_label(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "unknown-runtime"
    return value.rsplit(".", 1)[-1]


def _session_group_key(t: PhaseContextPressure) -> tuple[object, ...]:
    payload = t.payload
    provider_session_id = payload.get("provider_session_id")
    if isinstance(provider_session_id, str) and provider_session_id:
        return ("provider", provider_session_id)
    session_key = payload.get("session_key")
    if isinstance(session_key, dict):
        return (
            "session_key",
            session_key.get("runtime"),
            session_key.get("model_key"),
            session_key.get("scope"),
        )
    if payload.get("session_split") == "stateless":
        return ("stateless", t.source_path)
    return ("trace", t.source_path)


def _session_label(t: PhaseContextPressure) -> str:
    payload = t.payload
    split = payload.get("session_split") or "unknown"
    session_key = payload.get("session_key")
    scope = None
    model = payload.get("model")
    runtime = payload.get("runtime")
    if isinstance(session_key, dict):
        scope = session_key.get("scope")
        model = session_key.get("model_key") or model
        runtime = session_key.get("runtime") or runtime
    scope_text = str(scope or split)
    model_text = str(model or "unknown-model")
    return f"{scope_text} {_runtime_label(runtime)}/{model_text}"


def format_context_summary(session: dict) -> str | None:
    """Return a one-line CLI summary of peak context fullness, or
    ``None`` when the session carries no context_pressure evidence.

    Walks every :func:`extract_context_pressure_traces` record and
    picks the **peak** by fill ratio (or by used tokens when the
    ratio is missing — e.g. when an `orcho_estimated` reading had
    no `config_window_tokens` to compute the ratio). Format:

        Context window: 193.6k / 1.0M (19%) [runtime_reported plan]

    The trailing ``[<source> <phase>]`` annotates which source the
    reading came from (ADR 0029 hierarchy: ``runtime_reported`` >
    ``provider_usage`` > ``orcho_estimated`` > ``config_static`` >
    ``unknown``) and which phase saw the peak — both load-bearing
    when a user is debugging why the line says what it says.

    Returns ``None`` for sessions without any pressure record, or
    when every record's source is :data:`ContextSource.UNKNOWN`
    (no signal worth surfacing — the writer chose ``unknown``
    explicitly, suppressing the line is more honest than printing
    "Context window: ? / ? (?%)").
    """
    traces = extract_context_pressure_traces(session)
    if not traces:
        return None

    # Filter out unknown-source records — they carry no signal.
    informative = [
        t for t in traces
        if t.payload.get("context_source") != ContextSource.UNKNOWN.value
    ]
    if not informative:
        return None

    peak = max(informative, key=_pressure_rank)
    source = peak.payload.get("context_source") or ContextSource.UNKNOWN.value
    phase = peak.phase
    core = _format_pressure_core(peak.payload)

    groups: dict[tuple[object, ...], list[PhaseContextPressure]] = {}
    for trace in informative:
        groups.setdefault(_session_group_key(trace), []).append(trace)
    if len(groups) <= 1:
        return f"Context window: {core} [{source} {phase}]"

    lines = [
        "Context windows:",
        f"           ↳ Peak: {core} [{source} {phase}]",
        f"           ↳ Sessions: {len(groups)}",
    ]
    for traces in groups.values():
        current = traces[-1]
        group_peak = max(traces, key=_pressure_rank)
        phases = "→".join(dict.fromkeys(t.phase for t in traces))
        lines.append(
            "             - "
            f"{_session_label(current)}: "
            f"current {_format_pressure_core(current.payload)}, "
            f"peak {_format_pressure_core(group_peak.payload)}, "
            f"phases {phases}"
        )
    return "\n".join(lines)


__all__ = [
    "DURABLE_FIELDS",
    "SOURCE_PRIORITY",
    "ContextSource",
    "PhaseContextPressure",
    "PressureReading",
    "extract_context_pressure_traces",
    "format_context_summary",
    "normalize_context_pressure",
    "resolve_context_pressure",
]
