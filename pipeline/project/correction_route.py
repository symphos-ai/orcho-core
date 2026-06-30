# SPDX-License-Identifier: Apache-2.0
"""Correction-route derivation from the persisted triage record.

This module turns the durable ``correction_triage`` verdict (see
``pipeline.phases.builtin.handlers.correction_triage``) into a
:class:`CorrectionRoute` — the small, declarative decision that the
orchestrator consults to know whether the correction follow-up should
halt before any code change, skip straight past ``implement`` /
``review_changes`` / ``repair_changes`` into ``final_acceptance``, or
behave exactly like a normal code-change run.

It is deliberately correction-specific. This is **not** a general routing
engine: it understands only the four triage ``kind`` values and exists to
keep the route decision a pure function of the recorded record. There is
no I/O here and no import from the runtime or orchestrator layers, so the
derivation can be unit-tested in isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Phases a shortcut correction route skips. When triage decides the
#: blockers do not call for a code change (``gate_rerun`` /
#: ``contract_ack``), these phases are marked skipped and the run proceeds
#: straight to ``final_acceptance``.
SHORTCUT_SKIP_PHASES: frozenset[str] = frozenset(
    {"implement", "review_changes", "repair_changes"}
)


@dataclass(frozen=True)
class CorrectionRoute:
    """Resolved correction route derived from a triage record.

    ``kind`` mirrors the triage classification. ``skip_phases`` names the
    phases the orchestrator must skip (empty for ``code_fix``). ``halt``
    is true only when the run must stop in triage before any code change.
    ``reason`` is the operator-facing explanation of why phases were
    skipped or the run halted, and always embeds the triage summary.
    """

    kind: str
    skip_phases: frozenset[str]
    halt: bool
    reason: str

    def to_evidence(self) -> dict[str, Any]:
        """Serialize to a flat dict for the evidence chain."""
        return {
            "kind": self.kind,
            "skip_phases": sorted(self.skip_phases),
            "halt": self.halt,
            "reason": self.reason,
        }


def _summary_of(record: Mapping[str, Any]) -> str:
    """Best-effort triage summary text for the operator-facing reason."""
    summary = str(record.get("summary") or "").strip()
    return summary or "(no triage summary recorded)"


def _blocked_route(record: Mapping[str, Any], kind: str) -> CorrectionRoute:
    """Build a halting route, folding any recorded blockers into the reason."""
    summary = _summary_of(record)
    blockers = record.get("blockers")
    blocker_text = ""
    if isinstance(blockers, (list, tuple)):
        named = [str(b).strip() for b in blockers if str(b).strip()]
        if named:
            blocker_text = " blockers: " + "; ".join(named)
    reason = (
        f"correction route '{kind}' halts the run before implement: "
        f"{summary}{blocker_text}"
    )
    return CorrectionRoute(
        kind=kind, skip_phases=frozenset(), halt=True, reason=reason
    )


def derive_correction_route(
    record: Mapping[str, Any] | None,
) -> CorrectionRoute | None:
    """Derive the correction route from the persisted triage record.

    Returns ``None`` when there is no triage record (``None`` or a
    non-mapping) — a profile without triage is not correction-routed and
    the orchestrator must not alter its behavior.

    Otherwise maps the triage ``kind``:

    - ``code_fix`` — no skips, no halt; behaves like a normal run.
    - ``gate_rerun`` / ``contract_ack`` — skip the shortcut phases and run
      straight to ``final_acceptance``; no halt.
    - ``blocked`` — halt before any code change, reason carries blockers.
    - unknown ``kind`` — defensively treated as ``blocked`` (mirrors the
      normalization in ``correction_triage._normalize_triage``).
    """
    if not isinstance(record, Mapping):
        return None

    kind = str(record.get("kind") or "").strip().lower()
    summary = _summary_of(record)

    if kind == "code_fix":
        return CorrectionRoute(
            kind="code_fix",
            skip_phases=frozenset(),
            halt=False,
            reason=f"correction route 'code_fix': narrow code change — {summary}",
        )

    if kind in ("gate_rerun", "contract_ack"):
        return CorrectionRoute(
            kind=kind,
            skip_phases=SHORTCUT_SKIP_PHASES,
            halt=False,
            reason=(
                f"not applicable for correction route '{kind}': {summary}"
            ),
        )

    if kind == "blocked":
        return _blocked_route(record, "blocked")

    # Unknown / empty kind: defensive normalization to a blocked-equivalent
    # halt, mirroring ``_normalize_triage``'s out-of-set handling.
    return _blocked_route(record, kind or "unknown")
