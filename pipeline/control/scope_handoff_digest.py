"""pipeline.control.scope_handoff_digest — decision-oriented digest for a
``scope_expansion:*`` phase handoff.

A scope-expansion sanction (ADR 0112 §5) pauses the run with a
``PhaseHandoffRequested`` whose REJECTED verdict is issued by the *engine* —
the reviewer whose transcript rides ``last_output`` may well have said
APPROVED. The signal's ``artifacts`` already carry the full scope delta (the
classified out-of-plan findings, the offending paths, and the declared
in-plan patterns), but the legacy metadata block dropped them: the operator
saw ``verdict: REJECTED`` above an APPROVED reviewer transcript and had to
dig through that transcript to learn *what* went out of scope and what the
declared scope even was.

This module turns those artifacts into a compact, decision-first digest:

* a **classifier** (:func:`classify_scope_expansion`) that reads the signal
  artifacts and returns a frozen :class:`ScopeExpansionDigest` — the
  per-file findings (path / category / status / evidence), the offending
  paths, the declared in-plan patterns, the operating mode, and the
  out-of-set repo for the ``participant_add`` variant; and
* a pure **renderer** (:func:`render_scope_expansion_digest`) that prints a
  ``Why paused`` block: the verdict-provenance note (engine sanction, not
  the reviewer) first, then the out-of-plan changes against the declared
  scope.

Both are pure: primitives in, strings out. No filesystem / SDK I/O, no import
of ``pipeline.project``. Long evidence is collapsed via
:func:`pipeline.control.handoff_banners.sanitize_feedback_preview`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.io.ansi import C, paint
from pipeline.control.handoff_banners import sanitize_feedback_preview

__all__ = [
    "ScopeExpansionDigest",
    "ScopeExpansionFinding",
    "classify_scope_expansion",
    "is_scope_expansion_trigger",
    "render_scope_expansion_digest",
]

# The ``scope_expansion:`` trigger-family prefix. Deliberately duplicated from
# ``pipeline.runtime.handoff._SCOPE_EXPANSION_TRIGGER_PREFIX`` (the same
# conscious-twin convention as the duplicated phase-name guards there) so this
# module keeps the control-layer purity contract: no runtime import.
_SCOPE_EXPANSION_TRIGGER_FAMILY = "scope_expansion:"

_RULE = "─" * 60
_EVIDENCE_MAX_LEN = 200
#: Declared-scope patterns shown before collapsing to ``(+N more)`` — enough
#: to orient the operator without drowning the decision block.
_MAX_SCOPE_PATTERNS = 10


def is_scope_expansion_trigger(trigger: str | None) -> bool:
    """True when ``trigger`` belongs to the ``scope_expansion:*`` family."""
    return bool(trigger) and str(trigger).startswith(
        _SCOPE_EXPANSION_TRIGGER_FAMILY
    )


@dataclass(frozen=True, slots=True)
class ScopeExpansionFinding:
    """One classified out-of-plan change from the signal artifacts."""

    path: str
    category: str
    status: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScopeExpansionDigest:
    """Decision-oriented summary of a ``scope_expansion:*`` handoff.

    All fields are derived purely from the signal's ``artifacts``:

    * ``operating_mode`` — the run's mode the sanction was projected under;
    * ``findings`` — classified out-of-plan changes (``findings`` artifact);
    * ``handoff_paths`` — the offending paths (fallback when a findings entry
      is malformed or absent);
    * ``in_plan_patterns`` — the declared plan scope the changes were judged
      against;
    * ``participant_repo`` — the out-of-set repository for the
      ``participant_add`` variant (empty for ``out_of_plan``).
    """

    operating_mode: str
    findings: tuple[ScopeExpansionFinding, ...]
    handoff_paths: tuple[str, ...]
    in_plan_patterns: tuple[str, ...]
    participant_repo: str


def _as_str_tuple(value: object) -> tuple[str, ...]:
    """Coerce an artifact list field to a tuple of stripped non-empty strings."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _findings(value: object) -> tuple[ScopeExpansionFinding, ...]:
    """Build findings from the ``findings`` artifact; malformed entries drop."""
    if not isinstance(value, (list, tuple)):
        return ()
    findings: list[ScopeExpansionFinding] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        findings.append(
            ScopeExpansionFinding(
                path=path,
                category=str(item.get("category") or ""),
                status=str(item.get("status") or ""),
                evidence=_as_str_tuple(item.get("evidence")),
            )
        )
    return tuple(findings)


def classify_scope_expansion(
    artifacts: Mapping[str, object],
) -> ScopeExpansionDigest:
    """Classify a ``scope_expansion:*`` handoff into a digest.

    Pure: ``artifacts`` is the signal's ``artifacts`` mapping. Missing /
    malformed fields degrade to empty values rather than raising, so the
    digest renders something useful for both trigger variants and for older
    persisted payloads without the enriched keys.
    """
    return ScopeExpansionDigest(
        operating_mode=str(artifacts.get("operating_mode") or ""),
        findings=_findings(artifacts.get("findings")),
        handoff_paths=_as_str_tuple(artifacts.get("handoff_paths")),
        in_plan_patterns=_as_str_tuple(artifacts.get("in_plan_patterns")),
        participant_repo=str(artifacts.get("participant_repo") or ""),
    )


def _shown_findings(
    digest: ScopeExpansionDigest,
) -> tuple[ScopeExpansionFinding, ...]:
    """Findings to render — synthesized from bare paths when absent."""
    if digest.findings:
        return digest.findings
    return tuple(
        ScopeExpansionFinding(path=path, category="", status="", evidence=())
        for path in digest.handoff_paths
    )


def render_scope_expansion_digest(
    digest: ScopeExpansionDigest, *, color: bool | None = None,
) -> str:
    """Render the decision-first ``Why paused`` digest for ``digest``.

    Order is fixed: the engine-gate line and the verdict-provenance note come
    first (the single most confusing byte of the legacy layout was an
    engine-issued REJECTED above an APPROVED reviewer transcript), then each
    out-of-plan change with its classification, then the declared scope it was
    judged against. Pure: returns the block as a string; the caller owns the
    ``print``.
    """
    lines = [f"┌─ {paint('Why paused', C.BOLD, color=color)} {_RULE[:48]}"]
    mode = f" ({digest.operating_mode} mode)" if digest.operating_mode else ""
    gate = (
        f"out-of-set repository discovered mid-run{mode}"
        if digest.participant_repo
        else f"out-of-plan scope expansion{mode}"
    )
    lines.append(f"  {paint('Engine gate', C.CYAN, color=color)}: {gate}")
    lines.append(
        f"  {paint('Verdict', C.YELLOW, color=color)}: "
        + paint(
            "issued by the engine scope-expansion sanction, not the "
            "reviewer — the reviewer transcript below may say APPROVED",
            C.YELLOW, color=color,
        )
    )
    if digest.participant_repo:
        lines.append(
            f"  {paint('Out of set', C.CYAN, color=color)}: "
            + digest.participant_repo
        )
    for finding in _shown_findings(digest):
        tag = " · ".join(t for t in (finding.category, finding.status) if t)
        head = finding.path + (f"  [{tag}]" if tag else "")
        lines.append(f"  {paint('Out of plan', C.CYAN, color=color)}: {head}")
        if finding.evidence:
            preview = sanitize_feedback_preview(
                "; ".join(finding.evidence), max_len=_EVIDENCE_MAX_LEN,
            )
            lines.append(f"      evidence: {preview}")
    if digest.in_plan_patterns:
        shown = digest.in_plan_patterns[:_MAX_SCOPE_PATTERNS]
        more = len(digest.in_plan_patterns) - len(shown)
        scope = ", ".join(shown) + (f" (+{more} more)" if more else "")
    else:
        scope = "(not recorded on this signal)"
    lines.append(f"  {paint('Declared scope', C.CYAN, color=color)}: {scope}")
    lines.append(f"└{_RULE}")
    return "\n".join(lines)
