"""
pipeline/cross_project/task_plan.py — typed cross execution plan adapter.

This is a typed *view* over an already-validated :class:`CrossPlanParse`. It
does NOT parse JSON, compute gates, or add/remove schema fields: it reads the
normalized validated dict (``parse.data``) and the schema's
``cross_plan_alias_map`` to project the cross plan into a single typed object
that ``project_dispatch`` can consume instead of re-parsing ``plan_output``.

See the prep-stage design in the cross-task-plan-model planning record
(internal) for the target shape, the
``cross_plan`` field mapping, and the artifacts that must stay unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.contracts.cross_plan_schema import cross_plan_alias_map
from pipeline.cross_project.plan_parser import CrossPlanParse


@dataclass(frozen=True)
class CrossTaskUnit:
    """One per-alias execution unit of a cross plan.

    ``unit_id`` currently equals ``alias`` (the executable identity in the
    cross runner); it is kept as a distinct field so a future model can
    decouple identity from project alias without reshaping consumers.
    """

    unit_id:    str
    alias:      str
    goal:       str
    spec:       str
    depends_on: tuple[str, ...]
    files:      tuple[str, ...]
    produces:   str
    consumes:   str


@dataclass(frozen=True)
class CrossTaskPlan:
    """Typed view over a validated cross plan (mirrors single-project ``ParsedPlan``).

    ``implementation_order`` is the typed tuple of narrative steps. It is
    descriptive/human-facing ordering, NOT a routing input — routing order
    derives from per-unit ``depends_on`` (ADR 0057).
    """

    short_summary:        str
    interface_contract:   str
    implementation_order: tuple[str, ...]
    units:                tuple[CrossTaskUnit, ...]

    def units_by_alias(self) -> dict[str, CrossTaskUnit]:
        """``{alias: unit}`` map (supplied-alias order preserved)."""
        return {unit.alias: unit for unit in self.units}


def normalize_cross_task_plan(
    parse: CrossPlanParse, aliases: list[str],
) -> CrossTaskPlan:
    """Adapt an already-validated :class:`CrossPlanParse` into a typed plan.

    Reads only ``parse.data`` (the normalized, schema-valid dict) and reuses
    ``cross_plan_alias_map`` to resolve per-alias subtasks in supplied
    ``aliases`` order. Does NOT parse JSON and does NOT compute gates.

    Strip semantics match ``plan_parser._parsed_from_dict``:
    ``interface_contract`` and per-unit ``spec`` are stripped;
    ``implementation_order`` is kept element-wise unstripped so
    ``"\\n".join(...)`` reproduces the existing parsed view.
    """
    data = parse.data
    alias_map = cross_plan_alias_map(data)
    units = tuple(
        _unit_from_subtask(alias, alias_map[alias]) for alias in aliases
    )
    return CrossTaskPlan(
        short_summary=str(data.get("short_summary") or "").strip(),
        interface_contract=str(data.get("interface_contract") or "").strip(),
        implementation_order=tuple(data.get("implementation_order") or ()),
        units=units,
    )


def _unit_from_subtask(alias: str, st: dict) -> CrossTaskUnit:
    return CrossTaskUnit(
        unit_id=alias,
        alias=alias,
        goal=str(st.get("goal") or "").strip(),
        spec=str(st.get("spec") or "").strip(),
        depends_on=tuple(st.get("depends_on") or ()),
        files=tuple(st.get("files") or ()),
        produces=str(st.get("produces") or "").strip(),
        consumes=str(st.get("consumes") or "").strip(),
    )
