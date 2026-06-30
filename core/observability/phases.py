"""
core/observability/phases.py — canonical pipeline phase_kind constants.

A *phase_kind* is the canonical identifier of a pipeline stage, independent
of the human-readable phase id ("PLAN", "REVIEW_CHANGES", ...). It's the key
the dashboard groups attempts by; it's emitted into the payload of every
`phase.start` / `phase.end` event in events.jsonl.

Compared to the legacy `phase` string field on Event:
    phase           — display label, free-form (e.g. "PLAN", "REVIEW_CHANGES")
    phase_kind      — canonical, one of the constants below
    attempt         — 1-based int. Multiple attempts share the same phase_kind.

The canonical 7 mirror PIPELINE_PHASES on the dashboard rail. They are the
only kinds the dashboard reducer recognizes for grouping; events without
phase_kind are treated as milestones (banners), not phase instances.

Identifiers follow ADR 0022 workflow-semantic phase taxonomy.
"""

from __future__ import annotations

from typing import Final

HYPOTHESIS:       Final[str] = "HYPOTHESIS"
PLAN:             Final[str] = "PLAN"
VALIDATE_PLAN:    Final[str] = "VALIDATE_PLAN"
IMPLEMENT:        Final[str] = "IMPLEMENT"
REVIEW_CHANGES:   Final[str] = "REVIEW_CHANGES"
REPAIR_CHANGES:   Final[str] = "REPAIR_CHANGES"
FINAL_ACCEPTANCE: Final[str] = "FINAL_ACCEPTANCE"
CORRECTION_TRIAGE: Final[str] = "CORRECTION_TRIAGE"


PIPELINE_PHASE_KINDS: Final[tuple[str, ...]] = (
    HYPOTHESIS, PLAN, VALIDATE_PLAN, IMPLEMENT,
    REVIEW_CHANGES, REPAIR_CHANGES, FINAL_ACCEPTANCE,
    CORRECTION_TRIAGE,
)
