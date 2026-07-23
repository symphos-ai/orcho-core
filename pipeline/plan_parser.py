"""
pipeline/plan_parser.py — Extract a SubTask DAG from architect output.

Architect agents are expected to emit one machine-readable JSON object matching
``core.contracts.plan_schema``. The JSON object is the ground truth; Orcho then
renders human markdown deterministically from the parsed plan.

Backcompat paths remain:

  * a final ``json`` code-fence is still accepted for older/custom prompts;
  * markdown ``## Task N`` sections are a legacy fallback only when no JSON
    object/fence is present.

Schema or DAG violations in structured JSON are hard errors before implement. After
extraction we validate the DAG (unique ids, closed refs, acyclic).

This module is deliberately stateless and dependency-free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agents.entities import SubTask
from core.contracts.plan_schema import (
    PLAN_SHORT_SUMMARY_MAX_CHARS,
    PlanSchemaError,
    validate_plan_dict,
)
from pipeline.json_contract import parse_json_contract_object

# ── Public API ────────────────────────────────────────────────────────────────

class PlanParseError(ValueError):
    """Raised when neither JSON-fence nor markdown parsing yields a usable plan."""


@dataclass(frozen=True)
class ParsedPlan:
    """Result of parsing an architect's plan document.

    REA-1 added the typed plan contract slots (``goal`` / ``acceptance_criteria`` /
    ``owned_files`` / ``commands_to_run`` / ``risks`` / ``review_focus`` /
    ``mcp_context``). All default to empty so plans authored before REA-1 still
    construct cleanly. When the architect emits them, they propagate to implement /
    review_changes / repair_changes / final_acceptance prompts via
    :func:`pipeline.plan_contract.render_plan_contract`.
    """
    subtasks: tuple[SubTask, ...]
    source: str  # "json" | "markdown" — useful for telemetry
    short_summary: str = ""
    planning_context: str = ""
    # REA-1 typed contract — optional, default empty for backcompat.
    goal: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    owned_files: tuple[str, ...] = ()
    # Plan-level companion modifications allowed beyond ``owned_files`` in
    # every task — lockfiles, regenerated snapshots, derived artifacts.
    # Informational for review gates; not a write-scope primitive.
    allowed_modifications: tuple[str, ...] = ()
    commands_to_run: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    review_focus: tuple[str, ...] = ()
    mcp_context: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # Non-fatal advisories raised while parsing (e.g. short_summary auto-trimmed).
    parse_warnings: tuple[str, ...] = ()

    @property
    def has_contract(self) -> bool:
        """True when at least one REA-1 contract field is populated.

        ``allowed_modifications`` counts at both levels — a plan that only
        declares companion modifications (plan-level or on any subtask)
        still has a typed contract worth rendering, so the
        ``## Plan Contract`` block (and its companion-modifications
        section) is emitted for it.
        """
        return bool(
            self.goal
            or self.acceptance_criteria
            or self.owned_files
            or self.allowed_modifications
            or self.commands_to_run
            or self.risks
            or self.review_focus
            or self.mcp_context
            or any(st.allowed_modifications for st in self.subtasks)
        )

    @property
    def file_paths(self) -> list[str]:
        """Planned file paths, flattened from subtask ``files``.

        This is the progress/path-validation surface expected by session
        adapters.
        """
        seen: set[str] = set()
        paths: list[str] = []
        for subtask in self.subtasks:
            for path in subtask.files:
                if path and path not in seen:
                    seen.add(path)
                    paths.append(path)
        return paths

    @property
    def total_atomic_tasks(self) -> int:
        """Compatibility progress count: one atomic unit per subtask."""
        return len(self.subtasks)

    @property
    def has_dag(self) -> bool:
        """True when this plan has executable subtasks."""
        return bool(self.subtasks)


def parse_plan(text: str) -> ParsedPlan:
    """Parse architect output into a structured plan with a validated DAG.

    Order of attempts:
      1. Raw JSON object — strict, schema-validated. This is the preferred
         PLAN contract and never falls back to markdown when malformed.
      2. ``parse_json_fence`` — strict, schema-validated. If the fence exists
         and parses but its DAG is invalid (cycle, dangling ref, dup id), we
         re-raise immediately — that is a planning error that must reach
         DECOMPOSE_QA, *not* a reason to silently try markdown which might
         yield a different, possibly working plan and mask the real bug.
         REA-1: same logic for schema violations — when a JSON fence is
         present but its typed-contract fields are malformed, surfacing
         the :class:`PlanSchemaError` is the whole point ("fail clearly
         on malformed structured plans"). Markdown fallback would mask
         the architect's structural error.
      3. ``parse_markdown_sections`` — legacy fallback only when JSON is
         absent or syntactically broken. Schema violations and DAG violations
         bubble up unchanged.
    """
    json_err: PlanParseError | None = None
    stripped = text.strip()
    if stripped.startswith("{"):
        payload = parse_json_contract_object(
            text,
            label="plan",
            parse_error_cls=PlanParseError,
            is_candidate=_is_plan_json_shape,
            validate=validate_plan_dict,
        )
        plan = _plan_from_dict(
            payload.data,
            original_data=payload.original_data,
            parse_warnings=payload.parse_warnings,
        )
        validate_dag(plan.subtasks)
        return plan

    try:
        plan = parse_json_fence(text)
    except PlanSchemaError:
        # Fence present and parsed; its typed contract is malformed.
        # REA-1: surface the schema error verbatim — markdown fallback
        # would silently degrade to an unvalidated plan.
        raise
    except PlanParseError as e:
        # No fence at all, or the fence is not valid JSON. Fall through
        # to markdown so legacy prose-only plans keep working.
        json_err = e
    else:
        # JSON fence present and well-formed: its DAG is the ground truth.
        # A bad DAG here surfaces upstream — do not retry via markdown.
        validate_dag(plan.subtasks)
        return plan

    if "{" in stripped:
        try:
            payload = parse_json_contract_object(
                text,
                label="plan",
                parse_error_cls=PlanParseError,
                is_candidate=_is_plan_json_shape,
                validate=validate_plan_dict,
            )
        except PlanParseError as e:
            if "multiple embedded JSON contract objects" in str(e):
                raise
            json_err = e
        else:
            plan = _plan_from_dict(
                payload.data,
                original_data=payload.original_data,
                parse_warnings=payload.parse_warnings,
            )
            validate_dag(plan.subtasks)
            return plan

    try:
        plan = parse_markdown_sections(text)
    except PlanParseError as md_err:
        raise PlanParseError(
            f"plan parse failed; json: {json_err}; markdown: {md_err}"
        ) from md_err

    validate_dag(plan.subtasks)
    return plan


# ── JSON paths ────────────────────────────────────────────────────────────────

# Matches a fenced ``json`` block. Greedy on the inner body so embedded braces
# stay intact; non-greedy on the closing fence to stop at the first ```.
_JSON_FENCE_RE = re.compile(
    r"```(?:json|JSON)\s*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def parse_json_object(text: str) -> ParsedPlan:
    """Parse a raw JSON object and validate it against the plan schema."""
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise PlanParseError(f"plan JSON is not valid JSON: {e}") from e
    return _plan_from_dict(data, source="json")


def parse_json_fence(text: str) -> ParsedPlan:
    """Extract the last ``json`` fence and validate it against the plan schema."""
    matches = list(_JSON_FENCE_RE.finditer(text))
    if not matches:
        raise PlanParseError("no ```json``` code-fence found in plan output")

    # Architects sometimes include illustrative JSON earlier in the text (e.g.
    # in the spec). The contract says the *final* fence is the ground truth.
    body = matches[-1].group("body").strip()

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise PlanParseError(f"json fence is not valid JSON: {e}") from e

    return _plan_from_dict(data, source="json")


def _is_plan_json_shape(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("tasks"), list)
        and "short_summary" in data
        and "planning_context" in data
    )


def _plan_from_dict(
    data: Any,
    *,
    source: str = "json",
    original_data: Any | None = None,
    parse_warnings: tuple[str, ...] = (),
) -> ParsedPlan:
    # Capture pre-validation short_summary length so we can surface the
    # auto-trim as a non-fatal warning. The validator silently truncates
    # over-long summaries (display-only field — not worth aborting the run).
    original_summary_len = (
        len(original_data["short_summary"])
        if isinstance(original_data, dict)
        and isinstance(original_data.get("short_summary"), str)
        else len(data["short_summary"])
        if isinstance(data, dict) and isinstance(data.get("short_summary"), str)
        else 0
    )

    validate_plan_dict(data)  # raises PlanSchemaError for structural issues

    warnings: list[str] = list(parse_warnings)
    if original_summary_len > PLAN_SHORT_SUMMARY_MAX_CHARS:
        warnings.append(
            f"short_summary was {original_summary_len} chars; "
            f"auto-trimmed to {PLAN_SHORT_SUMMARY_MAX_CHARS} "
            f"(target ≤ {PLAN_SHORT_SUMMARY_MAX_CHARS})."
        )

    subtasks = tuple(_subtask_from_dict(t) for t in data["tasks"])
    return ParsedPlan(
        short_summary=data["short_summary"],
        planning_context=data["planning_context"],
        subtasks=subtasks,
        source=source,
        goal=(data.get("goal") or None),
        acceptance_criteria=tuple(data.get("acceptance_criteria") or ()),
        owned_files=tuple(data.get("owned_files") or ()),
        allowed_modifications=tuple(data.get("allowed_modifications") or ()),
        commands_to_run=tuple(data.get("commands_to_run") or ()),
        risks=tuple(data.get("risks") or ()),
        review_focus=tuple(data.get("review_focus") or ()),
        mcp_context=tuple(data.get("mcp_context") or ()),
        parse_warnings=tuple(warnings),
    )


def _subtask_from_dict(t: dict) -> SubTask:
    return SubTask(
        id=t["id"].strip(),
        goal=t["goal"].strip(),
        spec=(t.get("spec") or "").strip(),
        files=tuple(t.get("files") or ()),
        skill=(t.get("skill") or None),
        model=(t.get("model") or None),
        depends_on=tuple(t.get("depends_on") or ()),
        done_criteria=tuple(t.get("done_criteria") or ()),
        owned_files=tuple(t.get("owned_files") or ()),
        allowed_modifications=tuple(t.get("allowed_modifications") or ()),
    )


# ── Markdown fallback ─────────────────────────────────────────────────────────

# Each task is a level-2 heading like "## Task 1: Add endpoint" or
# "## Task T1 — Add endpoint". The id is whatever follows "Task " up to the
# first ":" or "—" / "-" / end-of-line.
_TASK_HEADING_RE = re.compile(
    r"^##\s+Task\s+(?P<id>[^\s:\-—]+)\s*[:\-—]?\s*(?P<title>.*)$",
    re.MULTILINE,
)

# Field lines inside a task block: "**Goal:** ..." or "Goal: ...".
_FIELD_RE = re.compile(
    r"^\s*\**\s*(?P<key>Skill|Model|Depends on|Files|Goal|Spec|Done Criteria)\s*\**\s*:\s*\**\s*(?P<val>.*?)\s*\**\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Bullet items under multi-line fields like Files / Done Criteria.
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.MULTILINE)


def parse_markdown_sections(text: str) -> ParsedPlan:
    """Best-effort markdown fallback when the JSON fence is missing.

    Splits on ``## Task ...`` headings, then for each block extracts known
    fields. Missing fields default to empty. ``Depends on`` is split on
    commas; the literal "none" / "-" is treated as no dependencies.
    """
    headings = list(_TASK_HEADING_RE.finditer(text))
    if not headings:
        raise PlanParseError("no '## Task N' sections found in plan markdown")

    summary = text[: headings[0].start()].strip() or "Plan parsed from markdown"

    subtasks: list[SubTask] = []
    for i, m in enumerate(headings):
        block_start = m.end()
        block_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        block = text[block_start:block_end]
        raw_id = m.group("id").strip()
        title = m.group("title").strip()
        subtasks.append(_subtask_from_markdown_block(raw_id, title, block))

    return ParsedPlan(
        short_summary=summary.splitlines()[0][:280],
        planning_context=summary,
        subtasks=tuple(subtasks),
        source="markdown",
    )


def _subtask_from_markdown_block(raw_id: str, title: str, block: str) -> SubTask:
    fields: dict[str, str] = {}
    for fm in _FIELD_RE.finditer(block):
        fields[fm.group("key").lower()] = fm.group("val").strip()

    files = _collect_list(block, fields.get("files", ""), key="files")
    deps = _split_deps(fields.get("depends on", ""))
    done = _collect_list(block, fields.get("done criteria", ""), key="done criteria")

    return SubTask(
        id=raw_id,
        goal=fields.get("goal", title),
        spec=fields.get("spec", ""),
        files=tuple(files),
        skill=(fields.get("skill") or None),
        model=(fields.get("model") or None),
        depends_on=tuple(deps),
        done_criteria=tuple(done),
    )


def _split_deps(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw or raw.lower() in {"none", "-", "—", "n/a"}:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _collect_list(block: str, inline: str, *, key: str) -> list[str]:
    """Collect a list-valued field. Prefer the inline value if it already
    contains commas; otherwise pull bullet lines that follow the key."""
    inline = inline.strip()
    if inline and "," in inline:
        return [p.strip() for p in inline.split(",") if p.strip()]

    # Find bullets after the field heading. The heading may be wrapped in
    # `**` in any of the common spots: `**Key:**`, `**Key**:`, `Key:`. Match
    # them all by treating asterisks as decoration around the key/colon.
    pattern = re.compile(
        rf"\**\s*{re.escape(key)}\s*\**\s*:\s*\**\s*\n((?:\s*[-*]\s+.+\n?)+)",
        re.IGNORECASE,
    )
    m = pattern.search(block)
    if not m:
        return [inline] if inline else []
    return [b.group(1).strip() for b in _BULLET_RE.finditer(m.group(1))]


# ── DAG validation ────────────────────────────────────────────────────────────

def validate_dag(subtasks: tuple[SubTask, ...]) -> None:
    """Validate uniqueness, closed references, and acyclicity.

    Raises ``PlanParseError`` with a message naming the offending ids so the
    error can be surfaced to the architect during DECOMPOSE_QA.
    """
    if not subtasks:
        raise PlanParseError("plan has no subtasks")

    ids = [s.id for s in subtasks]
    seen: set[str] = set()
    duplicates = [i for i in ids if i in seen or seen.add(i)]  # type: ignore[func-returns-value]
    if duplicates:
        raise PlanParseError(f"duplicate subtask ids: {sorted(set(duplicates))}")

    id_set = set(ids)
    for s in subtasks:
        unknown = [d for d in s.depends_on if d not in id_set]
        if unknown:
            raise PlanParseError(
                f"subtask '{s.id}' depends on unknown ids: {unknown}"
            )
        if s.id in s.depends_on:
            raise PlanParseError(f"subtask '{s.id}' depends on itself")

    # Kahn's algorithm — detect cycles by checking that every node is reachable
    # from the set of nodes with no remaining incoming edges.
    indeg = {s.id: 0 for s in subtasks}
    edges: dict[str, list[str]] = {s.id: [] for s in subtasks}
    for s in subtasks:
        for d in s.depends_on:
            indeg[s.id] += 1
            edges[d].append(s.id)

    queue = [n for n, d in indeg.items() if d == 0]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for nxt in edges[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)

    if visited != len(subtasks):
        unresolved = [n for n, d in indeg.items() if d > 0]
        raise PlanParseError(f"plan contains a dependency cycle involving: {unresolved}")


def topological_waves(
    subtasks: tuple[SubTask, ...],
    satisfied_ids: Iterable[str] | None = None,
) -> list[list[SubTask]]:
    """Group subtasks into execution waves.

    A wave is the set of all subtasks whose dependencies are satisfied by the
    union of preceding waves. Two subtasks in the same wave have no path
    between them and are safe to run in parallel.

    ``satisfied_ids`` names subtasks already completed outside this scheduling
    pass (e.g. done nodes pre-filled on a repair/resume run). Their ids are
    treated as already-satisfied dependencies, so a node whose only dependency
    is satisfied schedules in the first wave even though that dependency is not
    itself part of ``subtasks``. Passing ``None`` (the default) preserves the
    original behaviour exactly.

    The function assumes ``validate_dag`` has been called (no cycles, no
    dangling refs). Returns waves in execution order; ids inside a wave are
    sorted for deterministic test output.
    """
    satisfied = set(satisfied_ids or ())
    by_id = {s.id: s for s in subtasks}
    remaining = {s.id: set(s.depends_on) - satisfied for s in subtasks}
    waves: list[list[SubTask]] = []

    while remaining:
        ready_ids = sorted(i for i, deps in remaining.items() if not deps)
        if not ready_ids:
            # validate_dag should have caught this; defensive guard.
            raise PlanParseError(
                f"cannot schedule remaining subtasks (cycle?): {sorted(remaining)}"
            )
        waves.append([by_id[i] for i in ready_ids])
        for i in ready_ids:
            remaining.pop(i)
        for deps in remaining.values():
            deps.difference_update(ready_ids)

    return waves
