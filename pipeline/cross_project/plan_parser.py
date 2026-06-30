"""
pipeline/cross_project/plan_parser.py — parsing + rendering for cross-project
plans (ADR 0054).

The cross architect emits ONE JSON object (the ``cross_plan_json`` contract,
validated by :mod:`core.contracts.cross_plan_schema`). That normalized object
is the machine source of truth — persisted as ``cross_plan.json``. The
human-readable ``cross_plan.md`` and the validate-reviewer artifact are
*derived renders* of it (:func:`render_cross_plan_markdown`); raw agent text
lives only in the round trace.

``parse_cross_plan`` parses + validates the JSON (one guarded recovery path
for stray prose, via :func:`pipeline.json_contract.parse_json_contract_object`)
and returns a :class:`CrossPlanParse` bundling the normalized data, a
renderer/routing-facing :class:`ParsedCrossPlan`, and any parse warnings. A
malformed or schema-invalid object raises :class:`CrossPlanParseError` — the
caller turns that into a synthetic planning rejection (never a crash).

The ``=== SUBTASK ===`` marker grammar and the ``##``-heading scrape (ADR 0052)
are gone: ``interface_contract`` / ``implementation_order`` / per-alias
``spec`` / cross-alias ``depends_on`` are now first-class schema fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.contracts.cross_plan_schema import (
    CrossPlanSchemaError,
    cross_plan_alias_map,
    validate_cross_plan_dict,
)
from pipeline.json_contract import parse_json_contract_object

# Top-level keys that mark a JSON object as a cross-plan candidate (used by the
# embedded-object recovery path to avoid latching onto an unrelated object).
_CROSS_PLAN_CANDIDATE_KEYS = ("interface_contract", "subtasks")


class CrossPlanParseError(ValueError):
    """Raised when a cross-plan response is not one valid cross-plan JSON object."""


@dataclass(frozen=True)
class ParsedCrossPlan:
    """Structured, renderer/routing-facing view of a cross-project plan.

    ``subtasks`` maps each supplied alias (in supplied order) to its ``spec``
    string (the per-child implement payload). ``dependencies`` carries the
    typed cross-alias ``depends_on`` edges (ADR 0054 data; ADR 0057 consumes
    them for dispatch ordering). ``aliases_missing`` is always empty — the
    schema validator guarantees full alias coverage — but the field is kept so
    ``to_render_dict`` (consumed by ``core.io.transcript.render_cross_plan_block``)
    stays byte-identical.
    """

    interface_contract:   str
    implementation_order: str
    subtasks:             tuple[tuple[str, str | None], ...]
    aliases_missing:      tuple[str, ...]
    dependencies:         tuple[tuple[str, tuple[str, ...]], ...] = ()

    def to_render_dict(self) -> dict[str, Any]:
        """Mapping shape consumed by ``render_cross_plan_block`` (unchanged)."""
        return {
            "interface_contract":   self.interface_contract,
            "implementation_order": self.implementation_order,
            "subtasks":             list(self.subtasks),
            "aliases_missing":      list(self.aliases_missing),
        }

    def subtasks_dict(self) -> dict[str, str | None]:
        """``{alias: spec}`` routing map (supplied-alias order preserved)."""
        return {alias: body for alias, body in self.subtasks}


@dataclass(frozen=True)
class CrossPlanParse:
    """Result of :func:`parse_cross_plan`.

    ``data`` is the NORMALIZED validated cross-plan object — the canonical
    ``cross_plan.json`` source of truth and the input to
    :func:`render_cross_plan_markdown`. ``parsed`` is the structured view for
    routing + CLI rendering. ``parse_warnings`` records any non-fatal recovery
    (e.g. stray prose stripped around the JSON object).
    """

    data:           dict[str, Any]
    parsed:         ParsedCrossPlan
    parse_warnings: tuple[str, ...] = ()


def parse_cross_plan(text: str, aliases: list[str]) -> CrossPlanParse:
    """Parse + validate one cross-plan JSON object for ``aliases``.

    Recovers one schema-valid object from stray prose (recording a warning)
    via the shared JSON-contract recovery. Raises :class:`CrossPlanParseError`
    when the output is not exactly one valid cross-plan object (unparseable,
    multiple objects, schema/coverage/cycle invalid). Never returns ``None``
    and never silently degrades — the marker/markdown fallback is gone.
    """
    try:
        payload = parse_json_contract_object(
            text,
            label="cross_plan",
            parse_error_cls=CrossPlanParseError,
            is_candidate=_is_cross_plan_candidate,
            validate=lambda d: validate_cross_plan_dict(d, aliases),
        )
    except CrossPlanSchemaError as exc:
        # Schema/coverage/cycle defect surfaced through the validate closure.
        raise CrossPlanParseError(str(exc)) from exc

    return CrossPlanParse(
        data=payload.data,
        parsed=_parsed_from_dict(payload.data, aliases),
        parse_warnings=payload.parse_warnings,
    )


def _is_cross_plan_candidate(obj: Any) -> bool:
    return isinstance(obj, dict) and all(
        k in obj for k in _CROSS_PLAN_CANDIDATE_KEYS
    )


def _parsed_from_dict(data: dict[str, Any], aliases: list[str]) -> ParsedCrossPlan:
    """Build the renderer/routing view from a NORMALIZED cross-plan dict.

    Ordered by the supplied ``aliases`` so render/routing order is stable
    regardless of JSON ordering. This is the ONLY place the
    ``implementation_order`` list is joined to a string (the downstream
    ``ParsedCrossPlan`` / ``Handoff`` / renderers are all str-typed).
    """
    alias_map = cross_plan_alias_map(data)
    interface_contract = data["interface_contract"].strip()
    implementation_order = "\n".join(data["implementation_order"])
    subtasks = tuple(
        (alias, str(alias_map[alias]["spec"]).strip()) for alias in aliases
    )
    dependencies = tuple(
        (alias, tuple(alias_map[alias].get("depends_on") or ()))
        for alias in aliases
    )
    return ParsedCrossPlan(
        interface_contract=interface_contract,
        implementation_order=implementation_order,
        subtasks=subtasks,
        aliases_missing=(),
        dependencies=dependencies,
    )


def render_cross_plan_markdown(
    data: dict[str, Any], aliases: list[str] | None = None,
) -> str:
    """Render the human-readable cross plan from a NORMALIZED cross-plan dict.

    This is the derived audit/preview view (``cross_plan.md`` on disk + the
    cross-validate reviewer artifact). The three canonical headings
    (``## Interface Contract`` / ``## Per-Project Subtasks`` /
    ``## Implementation Order``) are preserved verbatim so existing readers
    and acceptance assertions still match. Deterministic, derived purely from
    the typed object (ADR 0050/0052 "JSON is source of truth, markdown is a
    render" pattern).

    When ``aliases`` is supplied the subtasks render in that (supplied) order
    so the human/audit render matches the routing order used by dispatch
    (``ParsedCrossPlan`` orders by supplied aliases); without it they render in
    JSON-array order. Schema validation guarantees exact alias coverage, so the
    reorder is a stable permutation, never a drop.
    """
    lines: list[str] = []

    contract = str(data.get("interface_contract") or "").strip()
    lines.append("## Interface Contract")
    lines.append("")
    lines.append(contract or "(none)")
    lines.append("")

    lines.append("## Per-Project Subtasks")
    lines.append("")
    for st in _ordered_subtasks(data, aliases):
        alias = st.get("alias", "?")
        lines.append(f"### [{alias}]")
        goal = str(st.get("goal") or "").strip()
        if goal:
            lines.append(f"Goal: {goal}")
        lines.append("")
        spec = str(st.get("spec") or "").strip()
        if spec:
            lines.append(spec)
            lines.append("")
        depends_on = list(st.get("depends_on") or ())
        if depends_on:
            lines.append(f"Depends on: {', '.join(depends_on)}")
        files = list(st.get("files") or ())
        if files:
            lines.append("Files:")
            lines.extend(f"- {f}" for f in files)
        produces = str(st.get("produces") or "").strip()
        if produces:
            lines.append(f"Produces: {produces}")
        consumes = str(st.get("consumes") or "").strip()
        if consumes:
            lines.append(f"Consumes: {consumes}")
        lines.append("")

    lines.append("## Implementation Order")
    lines.append("")
    order = list(data.get("implementation_order") or ())
    if order:
        lines.extend(f"{i}. {step}" for i, step in enumerate(order, 1))
    else:
        lines.append("(none)")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _ordered_subtasks(
    data: dict[str, Any], aliases: list[str] | None,
) -> list[dict[str, Any]]:
    """Subtasks in supplied-alias order when ``aliases`` is given, else JSON
    order. Any subtask whose alias is not in ``aliases`` (shouldn't happen
    post-validation) is appended in JSON order so nothing is silently dropped.
    """
    subtasks = list(data.get("subtasks") or ())
    if not aliases:
        return subtasks
    by_alias = {st.get("alias"): st for st in subtasks}
    ordered = [by_alias[a] for a in aliases if a in by_alias]
    seen = set(aliases)
    ordered.extend(st for st in subtasks if st.get("alias") not in seen)
    return ordered


def cross_plan_document(
    data: dict[str, Any], *, task: str, aliases: list[str] | None = None,
) -> str:
    """The full ``cross_plan.md`` document: title + task header + render.

    Single source for the on-disk audit render, the cross-validate reviewer
    artifact, and the dispatch handoff's ``full_cross_plan_markdown`` — so all
    three are byte-identical (ADR 0054).
    """
    return (
        "# Cross-Project Plan\n\n"
        f"Task: {task}\n\n"
        f"{render_cross_plan_markdown(data, aliases)}"
    )


def aliasize_cross_plan(
    parse: CrossPlanParse,
    projects: Mapping[str, Path],
    aliases: list[str],
) -> CrossPlanParse:
    """Rewrite absolute project roots to ``[alias]/`` form across every string
    field of the normalized plan, returning a fully consistent
    :class:`CrossPlanParse` (data + parsed view rebuilt from the leak-clean
    data).

    ADR 0054 persists the normalized object as the canonical ``cross_plan.json``
    that crosses no writer/child boundary, but an architect that emits an
    absolute path in ``files`` (or in ``spec`` prose) would otherwise leave that
    path in the canonical artifact. Aliasizing the DATA (not just the rendered
    markdown) keeps ``cross_plan.json``, the render, and the round-trace
    ``normalized_plan`` byte-consistent and leak-clean. Idempotent: re-running on
    already-aliased data is a no-op.
    """
    norm = _aliasize_plan_data(parse.data, projects)
    return CrossPlanParse(
        data=norm,
        parsed=_parsed_from_dict(norm, aliases),
        parse_warnings=parse.parse_warnings,
    )


def write_cross_plan_artifacts(
    run_dir: Path,
    parse: CrossPlanParse,
    *,
    task: str,
    projects: Mapping[str, Path],
    aliases: list[str],
) -> tuple[CrossPlanParse, str]:
    """Aliasize + persist the canonical ``cross_plan.json`` (leak-clean
    normalized object) and the derived ``cross_plan.md`` (full document).

    Returns ``(normalized_parse, document)``. The returned document is
    byte-identical to ``cross_plan.md`` on disk, so the cross-validate reviewer
    artifact, the audit render, and the dispatch handoff all see the same text.
    Single definition of the on-disk layout for every persist site (ADR 0054).
    """
    import json

    norm = aliasize_cross_plan(parse, projects, aliases)
    run_dir = Path(run_dir)
    (run_dir / "cross_plan.json").write_text(
        json.dumps(norm.data, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    document = cross_plan_document(norm.data, task=task, aliases=aliases)
    (run_dir / "cross_plan.md").write_text(document, encoding="utf-8")
    return norm, document


def _aliasize_plan_data(
    data: dict[str, Any], projects: Mapping[str, Path],
) -> dict[str, Any]:
    from pipeline.cross_project.path_alias import aliasize_plan_paths

    def _az(value: Any) -> Any:
        return (
            aliasize_plan_paths(value, projects)
            if isinstance(value, str) and value
            else value
        )

    out = dict(data)
    out["interface_contract"] = _az(data.get("interface_contract", ""))
    out["implementation_order"] = [
        _az(step) for step in (data.get("implementation_order") or [])
    ]
    subtasks: list[dict[str, Any]] = []
    for st in data.get("subtasks") or []:
        st2 = dict(st)
        for key in ("goal", "spec", "produces", "consumes"):
            if key in st2:
                st2[key] = _az(st2[key])
        if st2.get("files") is not None:
            st2["files"] = [_az(f) for f in st2["files"]]
        subtasks.append(st2)
    out["subtasks"] = subtasks
    return out
