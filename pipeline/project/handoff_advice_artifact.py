# SPDX-License-Identifier: Apache-2.0
"""Durable advice-artifact persistence + provenance note (focused helper).

Extracted from ``pipeline.project.handoff_advice`` (Architecture Fitness Gate):
the artifact read/write surface and the deterministic provenance note live here
so the advisor module stays focused on context/prompt/parse/safety. This module
is a leaf — it imports no other ``pipeline.project`` module at runtime — so the
advisor module can re-export these names without an import cycle.

The advice artifact is written to ``<run_dir>/phase_handoff_advice/`` and
**never** touches ``phase_handoff_decisions/``. Provenance flows ONLY through
the returned relative path: :func:`build_provenance_note` is built from the path
:func:`write_advice_artifact` actually returned, never from a recomputed name,
so a durable decision always references the exact advice object its feedback was
generated from.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sdk.phase_handoff import safe_handoff_id

if TYPE_CHECKING:
    from pipeline.project.handoff_advice import AdviceContext, HandoffAdvice

#: Durable advice artifacts live here. Never ``phase_handoff_decisions/``.
_ADVICE_DIRNAME = "phase_handoff_advice"


def _jsonable(value: Any) -> Any:
    """Normalise dataclass tuples to their durable JSON representation."""
    return json.loads(json.dumps(value, ensure_ascii=False))


def _advice_payload(advice: HandoffAdvice) -> dict[str, Any]:
    """Decision-relevant advice fields used for the idempotency comparison
    (excludes ``raw_output`` so cosmetically-different identical decisions stay
    idempotent)."""
    return {
        "recommended_action": advice.recommended_action,
        "confidence": advice.confidence,
        "rationale": advice.rationale,
        "retry_feedback": advice.retry_feedback,
        "risks": list(advice.risks),
        "expected_files": list(advice.expected_files),
        "operator_note": advice.operator_note,
        "parse_warnings": list(advice.parse_warnings),
        "intent": asdict(advice.intent),
    }


def _advice_artifact_dict(
    advice: HandoffAdvice,
    ctx: AdviceContext,
    *,
    created_at: str,
    usage: Mapping[str, Any] | None,
    assessment: Any | None,
) -> dict[str, Any]:
    return {
        "run_id": ctx.run_id,
        "handoff_id": ctx.handoff_id,
        "phase": ctx.phase,
        # The handoff trigger that prompted this advice, persisted verbatim so
        # the durable advice artifact records exactly which handoff it answered
        # (e.g. an ADR 0112 §5 ``scope_expansion:*`` trigger).
        "trigger": ctx.trigger,
        "created_at": created_at,
        "response_language": ctx.response_language,
        "advice": _advice_payload(advice),
        "contract_snapshot": _jsonable(asdict(ctx.contract_snapshot)) if ctx.contract_snapshot else None,
        "proposed_operations": [asdict(item) for item in advice.intent.proposed_operations],
        "contract_effects": [asdict(item) for item in advice.intent.contract_effects],
        "disposition": getattr(assessment, "disposition", ""),
        "blocked_reason": getattr(assessment, "blocked_reason", ""),
        "conflict_details": list(getattr(assessment, "conflict_details", ())),
        "raw_output": advice.raw_output,
        "usage": dict(usage) if usage else {},
    }


def write_advice_artifact(
    run_dir: Path,
    handoff_id: str,
    advice: HandoffAdvice,
    context: AdviceContext,
    *,
    usage: Mapping[str, Any] | None = None,
    created_at: str | None = None,
    assessment: Any | None = None,
) -> str:
    """Persist the advice object and return its actual relative path.

    Writes ``<run_dir>/phase_handoff_advice/<safe_handoff_id>.json``; NEVER
    touches ``phase_handoff_decisions/``. Idempotent + divergence-safe: a repeat
    write with identical advice returns the same path without rewriting; a
    divergent advice for the same handoff goes to a new attempt-suffixed file
    (``<safe_id>_2.json`` …) whose path is returned — prior/human artifacts are
    never overwritten. The returned relative path is the only value
    :func:`build_provenance_note` may use.
    """
    created_at = created_at or datetime.now(UTC).isoformat(timespec="seconds")
    safe_id = safe_handoff_id(handoff_id)
    advice_dir = run_dir / _ADVICE_DIRNAME
    new_payload = _jsonable(_advice_payload(advice))
    identity = {
        "advice": new_payload,
        "contract_snapshot": _jsonable(asdict(context.contract_snapshot)) if context.contract_snapshot else None,
        "disposition": getattr(assessment, "disposition", ""),
        "blocked_reason": getattr(assessment, "blocked_reason", ""),
        "conflict_details": list(getattr(assessment, "conflict_details", ())),
    }
    # Probe the base name then attempt-suffixed names: first free name wins
    # (write + return); an occupant holding identical advice is an idempotent
    # return; a divergent occupant is stepped over.
    attempt = 1
    while True:
        name = f"{safe_id}.json" if attempt == 1 else f"{safe_id}_{attempt}.json"
        candidate = advice_dir / name
        relpath = f"{_ADVICE_DIRNAME}/{name}"
        existing = load_advice_artifact(candidate)
        if existing is None:
            advice_dir.mkdir(parents=True, exist_ok=True)
            candidate.write_text(
                json.dumps(
                    _advice_artifact_dict(
                        advice, context, created_at=created_at, usage=usage, assessment=assessment,
                    ),
                    indent=2,
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )
            return relpath
        if {key: existing.get(key) for key in identity} == identity:
            return relpath
        attempt += 1


def load_advice_artifact(path: Path) -> dict[str, Any] | None:
    """Lenient reader: parsed advice artifact dict, or ``None`` on any error."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


#: Valid provenance sources. ``agent_advice`` is the operator-driven advisory
#: retry; ``ci_agent`` is the non-interactive CI auto-retry. Kept explicit so a
#: typo can never silently mislabel a durable decision's provenance.
_PROVENANCE_SOURCES: frozenset[str] = frozenset({"agent_advice", "ci_agent"})


def build_provenance_note(
    artifact_relpath: str, *, source: str = "agent_advice",
) -> str:
    """Decision ``note`` built ONLY from the written artifact path (never a
    recomputed deterministic name) — so the durable decision always references
    the exact advice object whose ``retry_feedback`` was applied, including the
    divergent-advice case with an attempt suffix.

    ``source`` records the retry provenance: ``agent_advice`` (default, the
    operator-driven advisory retry) or ``ci_agent`` (the non-interactive CI
    auto-retry). Any other value is rejected so provenance stays auditable.
    """
    if source not in _PROVENANCE_SOURCES:
        raise ValueError(
            f"unknown provenance source {source!r}; "
            f"expected one of {sorted(_PROVENANCE_SOURCES)}"
        )
    return f"feedback_source={source}; advice_artifact={artifact_relpath}"


__all__ = [
    "build_provenance_note",
    "load_advice_artifact",
    "write_advice_artifact",
]
