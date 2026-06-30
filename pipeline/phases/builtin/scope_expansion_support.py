# SPDX-License-Identifier: Apache-2.0
"""Durable-input gathering for the final_acceptance scope-expansion gate.

The pure classifier lives in :mod:`pipeline.engine.scope_expansion` (no I/O).
This focused support module is the **I/O-facing facade** that reads the named
durable artefacts of a run, builds the per-file signals, and calls the pure T1
functions — the same split as ``verification_readiness`` (pure gaps) vs the
``review_support`` readiness/backstop facades.

It is extracted into its own module rather than appended to ``review_support``
so that file does not cross the architecture-fitness size limit: the new
responsibility (durable-artefact gathering for scope expansion) gets a focused
home, and ``review_support`` keeps only the thin facade aliases the handler and
tests import.

Every durable source degrades softly: a missing git repo, unreadable plan
artefact, or absent receipt collapses to the conservative signal (``verified``
False, ``has_explanation`` False, …) so a gap never silently upgrades an
out-of-plan file toward ``notice``, and a failure never breaks delivery.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.io.git_helpers import _run_git, git_changed_files
from pipeline.engine.scope_expansion import (
    CATEGORY_BUILD,
    CATEGORY_FIXTURE,
    CATEGORY_IMPORT_WIRING,
    CATEGORY_PERSISTENCE,
    CATEGORY_PUBLIC_WIRE,
    CATEGORY_SCHEMA,
    CATEGORY_SECURITY,
    ScopeExpansionAssessment,
    ScopeExpansionItem,
    build_scope_expansion_assessment,
    build_scope_expansion_signals,
    categorize_file,
    derive_in_plan_patterns,
    render_scope_expansion_lines,
)
from pipeline.phases.builtin.lifecycle import _agent_project_dir
from pipeline.runtime.roles import ScopeExpansionSanction
from pipeline.runtime.run_shape import OperatingMode
from pipeline.runtime.scope_expansion_sanction import decide as _decide_sanction

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState

_GATE_GREEN = "green"
_PHASE_HANDOFF_WAIVER_KEY = "phase_handoff_waiver"


def _operator_waiver_active(state: PipelineState) -> bool:
    """True when an active operator waiver carries a non-empty verdict.

    The waiver is the explicit human decision to ship despite open findings;
    while it is active the scope-expansion gate is fully disarmed so the
    final_acceptance prompt/readiness and ``phase_log`` stay byte-identical to
    the pre-feature waiver behaviour.
    """
    waiver = state.extras.get(_PHASE_HANDOFF_WAIVER_KEY)
    return isinstance(waiver, Mapping) and bool(
        str(waiver.get("waiver_text") or "").strip()
    )

# Map a required-delivery command (rendered string) to the scope-expansion
# categories whose verification it substantiates. A category is marked
# ``green`` only when at least one relevant required receipt is present and none
# of its relevant receipts are missing/failed/stale.
_BUILD_GATE_TOKENS = ("ruff", "lint", "flake", "mypy", "build", "compile", "make")


def _command_categories(command: str) -> set[str]:
    """Categories a required-delivery command verifies (pure keyword map)."""
    c = command.lower()
    cats: set[str] = set()
    if "schema" in c or "snapshot" in c:
        cats.update((CATEGORY_SCHEMA, CATEGORY_PUBLIC_WIRE))
    if "pytest" in c or "test" in c:
        cats.add(CATEGORY_FIXTURE)
    if any(tok in c for tok in _BUILD_GATE_TOKENS):
        cats.update((CATEGORY_BUILD, CATEGORY_IMPORT_WIRING))
    return cats


def _gate_status_by_category(
    state: PipelineState, contract: Any, output_dir: Path,
) -> dict[str, str]:
    """Per-category gate status from the final-acceptance readiness summary.

    A category is ``green`` only when one of its relevant required receipts is
    present AND none of its relevant receipts are missing/failed/stale. Unknown
    bindings are absent from the map → the per-file ``verified`` stays False
    (conservative). Soft-degrades to ``{}`` on any readiness error.
    """
    from pipeline.verification_contract import PlaceholderContext
    from pipeline.verification_readiness import build_final_acceptance_readiness

    ctx = state.extras.get("verification_placeholders") or PlaceholderContext()
    try:
        summary = build_final_acceptance_readiness(
            contract, output_dir, ctx, extras=state.extras,
        )
    except Exception:  # noqa: BLE001 — readiness must never break the gate
        return {}

    present_cats: set[str] = set()
    bad_cats: set[str] = set()
    for cmd in summary.required_present:
        present_cats |= _command_categories(str(cmd))
    for cmd in (
        *summary.required_missing,
        *summary.required_failed,
        *summary.required_stale,
    ):
        bad_cats |= _command_categories(str(cmd))

    status: dict[str, str] = {}
    for cat in present_cats | bad_cats:
        status[cat] = _GATE_GREEN if (cat in present_cats and cat not in bad_cats) else "red"
    return status


def _changed_diffs(cwd: str) -> list[Any]:
    """Parsed per-file working-tree diffs, soft-degrading to ``[]``."""
    from pipeline.engine.run_diff import parse_unified_diff

    _rc, out, _err = _run_git(["diff", "--no-color"], cwd=cwd)
    if not out:
        return []
    try:
        return parse_unified_diff(out)
    except Exception:  # noqa: BLE001
        return []


def _diff_stats_by_file(diffs: list[Any]) -> dict[str, dict[str, Any]]:
    """``{path: {added, removed, is_deletion}}`` from parsed diff sections."""
    from pipeline.engine.run_diff import file_stats

    stats: dict[str, dict[str, Any]] = {}
    for d in diffs:
        added, removed = file_stats(d)
        is_deletion = (
            any(line.startswith("deleted file mode") for line in d.raw_lines)
            or d.new_path is None
        )
        path = d.path
        if path and path != "(unknown)":
            stats[path] = {
                "added": added,
                "removed": removed,
                "is_deletion": is_deletion,
            }
    return stats


def _is_new_export_line(added: str) -> bool:
    """True when an added line introduces a new public name/field/export.

    A ``@dataclass(...)`` decorator line is NOT an export (it restores the
    frozen/slots invariant); a new ``def`` / ``class`` / ``__all__`` entry or a
    new top-level ``name: type`` / ``name = ...`` binding is.
    """
    s = added.strip()
    if not s:
        return False
    if s.startswith("@"):
        return False  # decorator change (e.g. dataclass frozen/slots) is not an export
    if s.startswith(("def ", "class ", "async def ")):
        return True
    if s.startswith("__all__"):
        return True
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*[:=]", s))


def _sdk_flags_by_file(
    diffs: list[Any], gate_status: Mapping[str, str],
) -> dict[str, dict[str, bool]]:
    """Conservative SDK-reconciliation flags for schema/public-wire files.

    Flags are set ONLY when the category's snapshot-guard gate is green and the
    diff restores an invariant (``frozen=True`` / ``slots=True``) without
    introducing any new export/field. Anything unprovable → no entry (all flags
    default False in T1).
    """
    flags: dict[str, dict[str, bool]] = {}
    for d in diffs:
        category = categorize_file(d.path)
        if category not in (CATEGORY_SCHEMA, CATEGORY_PUBLIC_WIRE):
            continue
        if gate_status.get(category) != _GATE_GREEN:
            continue  # snapshot-guard not green → unprovable
        added = [
            line[1:] for line in d.body_lines
            if line.startswith("+") and not line.startswith("+++")
        ]
        removed = [
            line for line in d.body_lines
            if line.startswith("-") and not line.startswith("---")
        ]
        if not added and not removed:
            continue
        if any(_is_new_export_line(line) for line in added):
            continue
        restores_invariant = any(
            "frozen=true" in line.lower() or "slots=true" in line.lower()
            for line in added
        )
        if not restores_invariant:
            continue
        flags[d.path] = {
            "already_public": True,
            "no_new_exports": True,
            "restores_invariant": True,
        }
    return flags


def _in_plan_patterns(state: PipelineState, output_dir: Path) -> tuple[str, ...]:
    """Glob patterns the durable plan + project allowed-modifications declare."""
    from pipeline.plan_artifacts import load_parsed_plan_artifact

    plan: Any = None
    try:
        plan = load_parsed_plan_artifact(Path(output_dir))
    except Exception:  # noqa: BLE001 — fall back to the in-memory plan
        plan = getattr(state, "parsed_plan", None)
    project_allowed = getattr(getattr(state, "plugin", None), "allowed_modifications", None)
    return derive_in_plan_patterns(plan, project_allowed or ())


def _explained_files(state: PipelineState, changed: list[str]) -> set[str]:
    """Out-of-plan files whose change is explained by implement evidence."""
    explained: set[str] = set()
    impl = state.phase_log.get("implement")
    if not isinstance(impl, Mapping):
        return explained

    declared: set[str] = set()
    receipts = impl.get("implementation_receipts")
    if isinstance(receipts, list):
        for rec in receipts:
            if isinstance(rec, Mapping):
                for f in rec.get("declared_files") or ():
                    if isinstance(f, str):
                        declared.add(f)

    text_parts: list[str] = []
    for key in ("output", "attestation_summary"):
        value = impl.get(key)
        if isinstance(value, str):
            text_parts.append(value)
    blob = "\n".join(text_parts)

    for path in changed:
        base = path.rsplit("/", 1)[-1]
        in_declared = path in declared or (base and base in declared)
        in_blob = path in blob or (base and base in blob)
        if in_declared or in_blob:
            explained.add(path)
    return explained


def _repeated_paths(state: PipelineState) -> set[str]:
    """Out-of-plan paths repeated across corrections (soft, best-effort)."""
    paths: set[str] = set()
    candidates: list[Any] = []
    block = state.extras.get("correction_fixed_point")
    if isinstance(block, Mapping):
        candidates.extend(block.get("repeated") or [])
    triage = state.phase_log.get("correction_triage")
    if isinstance(triage, Mapping):
        candidates.extend(triage.get("blockers") or [])
    for item in candidates:
        if isinstance(item, str) and ("/" in item or "." in item):
            paths.add(item)
        elif isinstance(item, Mapping):
            for key in ("path", "file"):
                value = item.get(key)
                if isinstance(value, str):
                    paths.add(value)
    return paths


def scope_expansion_assessment(state: PipelineState) -> ScopeExpansionAssessment:
    """Build the scope-expansion assessment from the run's durable artefacts.

    Empty (no items) under dry-run, without a verification contract or run dir,
    when an operator waiver is active, or when the working tree has no
    out-of-plan change — so the prompt and verdict stay byte-identical for an
    ordinary in-scope diff and for the pre-feature waiver path. Never raises.
    """
    if getattr(state, "dry_run", False):
        return ScopeExpansionAssessment()
    # An active operator waiver is the explicit human ship decision: it gates
    # the whole gate (assessment → render → durable write), not only the
    # blocker backstop, so prompt/readiness/phase_log stay byte-identical.
    if _operator_waiver_active(state):
        return ScopeExpansionAssessment()
    output_dir = getattr(state, "output_dir", None)
    if output_dir is None:
        return ScopeExpansionAssessment()
    contract = state.extras.get("verification_contract")
    if contract is None:
        return ScopeExpansionAssessment()

    cwd = _agent_project_dir(state)
    try:
        changed = [f for f in git_changed_files(cwd) if f]
    except Exception:  # noqa: BLE001
        changed = []
    if not changed:
        return ScopeExpansionAssessment()

    diffs = _changed_diffs(cwd)
    gate_status = _gate_status_by_category(state, contract, Path(output_dir))
    signals = build_scope_expansion_signals(
        changed_files=changed,
        changed_file_set=changed,
        in_plan_patterns=_in_plan_patterns(state, Path(output_dir)),
        diff_stats_by_file=_diff_stats_by_file(diffs),
        gate_status_by_category=gate_status,
        explained_files=_explained_files(state, changed),
        repeated_paths=_repeated_paths(state),
        sdk_flags_by_file=_sdk_flags_by_file(diffs, gate_status),
    )
    return build_scope_expansion_assessment(signals)


def render_scope_expansion_text(assessment: ScopeExpansionAssessment) -> str:
    """Render the compact scope-expansion block; ``""`` for an empty assessment."""
    return "\n".join(render_scope_expansion_lines(assessment.to_dict()))


def scope_expansion_text(state: PipelineState) -> str:
    """Facade: gather + render the scope-expansion block for the prompt."""
    return render_scope_expansion_text(scope_expansion_assessment(state))


def _is_russian(language: str | None) -> bool:
    return bool(language and str(language).strip().lower().startswith("rus"))


def _scope_gap_dict(item: Any, *, language: str | None) -> dict[str, str]:
    evidence = "; ".join(item.evidence) or "no supporting evidence"
    if _is_russian(language):
        return {
            "risk": (
                f"Out-of-plan {item.category} файл '{item.path}' — "
                "scope-expansion blocker."
            ),
            "missing_evidence": f"Сигналы scope-expansion: {evidence}.",
            "required_check": (
                f"Обоснуй парным alignment/тестом или откати out-of-plan "
                f"изменение {item.path}."
            ),
        }
    return {
        "risk": (
            f"Out-of-plan {item.category} change '{item.path}' is a "
            "scope-expansion blocker."
        ),
        "missing_evidence": f"Scope-expansion signals: {evidence}.",
        "required_check": (
            f"Justify with paired alignment/tests or revert the out-of-plan "
            f"change to {item.path}."
        ),
    }


# Genuine-safety classes (ADR 0112 §5): the only ones whose sanction stays hard
# (default halt + waiver) in every mode. ``security`` / ``persistence`` are
# categories; ``destructive_delete`` surfaces as a per-file evidence token.
_GENUINE_SAFETY_CATEGORIES = frozenset({CATEGORY_SECURITY, CATEGORY_PERSISTENCE})
_DESTRUCTIVE_DELETE_EVIDENCE = "destructive-delete"


def _item_is_genuine_safety(item: ScopeExpansionItem) -> bool:
    """True for a genuine-safety item (security / persistence / destructive delete).

    Derived from the classifier's durable fact only — the category and the
    pure-fact evidence tokens — so the classifier is not re-run or re-coupled to
    a verdict here.
    """
    return (
        item.category in _GENUINE_SAFETY_CATEGORIES
        or _DESTRUCTIVE_DELETE_EVIDENCE in item.evidence
    )


@dataclass(frozen=True)
class ScopeExpansionSanctionRouting:
    """Mode-projected routing of a scope-expansion assessment (ADR 0112 §5).

    The classifier status is a pure fact; this is the *route* each fact takes
    under the run's ``OperatingMode``, computed via the T1 projection
    :func:`pipeline.runtime.scope_expansion_sanction.decide`:

    - ``halt_items`` — sanction ``HALT_WAIVER`` (a genuine-safety class with no
      active waiver). These are the **only** items that emit a release gap and
      force a REJECTED verdict, in every mode.
    - ``handoff_items`` — sanction ``HANDOFF`` (a ``pro`` blocker or any
      ``governed`` expansion). These do **not** force REJECTED; they mark the
      need for a phase-handoff (the lifecycle wiring is a later increment).
    - ``alert_items`` — sanction ``AUTO_ALERT`` (a ``pro`` risk): continue with
      an operator-visible alert, no release gap.

    ``AUTO_CONTINUE`` items carry no route entry (record → continue).
    """

    operating_mode: OperatingMode
    halt_items: tuple[ScopeExpansionItem, ...] = ()
    handoff_items: tuple[ScopeExpansionItem, ...] = ()
    alert_items: tuple[ScopeExpansionItem, ...] = ()

    @property
    def forces_rejected(self) -> bool:
        """True iff a genuine-safety halt forces a REJECTED verdict."""
        return bool(self.halt_items)

    @property
    def needs_phase_handoff(self) -> bool:
        """True iff at least one item routes through phase-handoff (T3)."""
        return bool(self.handoff_items)

    def release_gaps(self, *, language: str | None = None) -> list[dict[str, str]]:
        """Release-gap dicts (``required_receipt_gaps`` shape) for halt items only."""
        return [_scope_gap_dict(item, language=language) for item in self.halt_items]

    def to_dict(self) -> dict[str, Any]:
        """Durable, JSON-safe view of the routing decision."""
        return {
            "operating_mode": self.operating_mode.value,
            "forces_rejected": self.forces_rejected,
            "needs_phase_handoff": self.needs_phase_handoff,
            "halt_paths": [item.path for item in self.halt_items],
            "handoff_paths": [item.path for item in self.handoff_items],
            "alert_paths": [item.path for item in self.alert_items],
        }


def route_scope_expansion_sanction(
    assessment: ScopeExpansionAssessment,
    *,
    operating_mode: OperatingMode,
    has_active_waiver: bool,
) -> ScopeExpansionSanctionRouting:
    """Project each classified item onto its ADR 0112 §5 sanction route.

    Replaces the old unconditional ``blockers → release gaps`` coupling (the
    "prison rule"): the route now depends on the run's ``OperatingMode``,
    whether the item is a genuine-safety class, and whether an operator
    ``continue_with_waiver`` is active. Pure routing — the classifier is left
    untouched; only :func:`pipeline.runtime.scope_expansion_sanction.decide`
    decides the route per item.
    """
    halt: list[ScopeExpansionItem] = []
    handoff: list[ScopeExpansionItem] = []
    alerts: list[ScopeExpansionItem] = []
    for item in assessment.items:
        disposition = _decide_sanction(
            status=item.status.value,
            category_is_genuine_safety=_item_is_genuine_safety(item),
            operating_mode=operating_mode,
            has_active_waiver=has_active_waiver,
        )
        sanction = disposition.sanction
        if sanction is ScopeExpansionSanction.HALT_WAIVER:
            halt.append(item)
        elif sanction is ScopeExpansionSanction.HANDOFF:
            handoff.append(item)
        elif sanction is ScopeExpansionSanction.AUTO_ALERT:
            alerts.append(item)
        # AUTO_CONTINUE: record → continue, no route entry.
    return ScopeExpansionSanctionRouting(
        operating_mode=operating_mode,
        halt_items=tuple(halt),
        handoff_items=tuple(handoff),
        alert_items=tuple(alerts),
    )


def _handoff_findings(routing: ScopeExpansionSanctionRouting) -> list[dict[str, Any]]:
    """Audit-grade per-item view of the HANDOFF-routed scope expansions."""
    return [
        {
            "path": item.path,
            "category": item.category,
            "status": item.status.value,
            "evidence": list(item.evidence),
        }
        for item in routing.handoff_items
    ]


def raise_scope_expansion_handoff(
    state: PipelineState,
    routing: ScopeExpansionSanctionRouting,
    *,
    last_output: str = "",
) -> Any:
    """Open the out-of-plan phase-handoff pause for a HANDOFF-routed sanction.

    ADR 0112 §5 (T3 lifecycle wiring): :func:`route_scope_expansion_sanction`
    only *classifies* the route — it records that a ``pro`` blocker or any
    ``governed`` expansion ``needs_phase_handoff``. Recording the need is not
    enough: the run must actually pause for operator sanction, otherwise the
    HANDOFF route silently degrades to "log and continue finalizing" (the F1
    review finding). This builds the ``scope_expansion:out_of_plan`` signal and
    raises it on ``state.phase_handoff_request`` so the runner breaks out of the
    phase walk and the orchestrator's pause tail
    (:func:`pipeline.project.handoff.apply_phase_handoff_pause`) persists
    ``meta.phase_handoff`` + ``meta.status='awaiting_phase_handoff'`` and exits
    rc=4 — the same ADR 0038 lifecycle the loop-driven handoffs ride.

    No-op (returns ``None``) when there is no handoff need, or when a pause is
    already pending — genuine-safety HALT items reject via the release-gap path,
    and an earlier request (e.g. the implement substance-repair handoff) is never
    clobbered. Idempotent across resume: the final_acceptance phase is recorded
    completed before the runner breaks, so a resumed ``continue`` short-circuits
    the completed phase rather than re-raising the same handoff.
    """
    if not routing.needs_phase_handoff:
        return None
    if getattr(state, "phase_handoff_request", None) is not None:
        return None

    from pipeline.runtime.handoff import (
        SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER,
        build_scope_expansion_handoff_signal,
    )

    paths = [item.path for item in routing.handoff_items]
    signal = build_scope_expansion_handoff_signal(
        trigger=SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER,
        artifacts={
            "operating_mode": routing.operating_mode.value,
            "handoff_paths": paths,
            "findings": _handoff_findings(routing),
        },
        last_output=last_output or (
            "Out-of-plan scope expansion routed to phase-handoff for operator "
            f"sanction: {', '.join(paths)}"
        ),
    )
    if signal is not None:
        state.phase_handoff_request = signal
    return signal


__all__ = [
    "ScopeExpansionSanctionRouting",
    "raise_scope_expansion_handoff",
    "render_scope_expansion_text",
    "route_scope_expansion_sanction",
    "scope_expansion_assessment",
    "scope_expansion_text",
]
