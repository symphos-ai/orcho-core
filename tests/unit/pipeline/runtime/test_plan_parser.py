"""
Parser for the team-lead PLAN output.

Covers JSON-fence happy path, markdown fallback, DAG validation
(duplicates, dangling refs, cycles, self-loops), and topological waves.
"""

from __future__ import annotations

import textwrap

import pytest

from agents.entities import SubTask
from pipeline.plan_parser import (
    PlanParseError,
    parse_json_fence,
    parse_json_object,
    parse_markdown_sections,
    parse_plan,
    topological_waves,
    validate_dag,
)

# ── JSON happy paths ──────────────────────────────────────────────────────────

def _wrap_with_json(payload: str) -> str:
    return f"## Task 1: foo\n\n```json\n{payload}\n```\n"


def test_parse_json_object_extracts_basic_plan() -> None:
    payload = """
    {
      "short_summary": "do stuff",
      "planning_context": "do stuff in two steps",
      "tasks": [
        {"id": "a", "goal": "first thing"},
        {"id": "b", "goal": "second thing", "depends_on": ["a"]}
      ]
    }
    """
    plan = parse_json_object(payload)
    assert plan.source == "json"
    assert plan.short_summary == "do stuff"
    assert plan.planning_context == "do stuff in two steps"
    assert len(plan.subtasks) == 2
    assert plan.subtasks[1].depends_on == ("a",)


def test_parse_json_fence_extracts_basic_plan() -> None:
    payload = """
    {
      "short_summary": "do stuff",
      "planning_context": "do stuff in two steps",
      "tasks": [
        {"id": "a", "goal": "first thing"},
        {"id": "b", "goal": "second thing", "depends_on": ["a"]}
      ]
    }
    """
    plan = parse_json_fence(_wrap_with_json(payload))
    assert plan.source == "json"
    assert plan.short_summary == "do stuff"
    assert plan.planning_context == "do stuff in two steps"
    assert len(plan.subtasks) == 2
    assert plan.subtasks[1].depends_on == ("a",)


def test_parse_json_fence_takes_last_fence_when_multiple_present() -> None:
    text = textwrap.dedent("""
        Some prose with example JSON inside:

        ```json
        {"this": "is just an example"}
        ```

        Real plan below:

        ```json
        {
          "short_summary": "real",
          "planning_context": "real planning context",
          "tasks": [{"id": "x", "goal": "do x"}]
        }
        ```
    """)
    plan = parse_json_fence(text)
    assert plan.short_summary == "real"
    assert plan.planning_context == "real planning context"
    assert plan.subtasks[0].id == "x"


def test_parse_json_object_long_summary_is_soft_trimmed() -> None:
    # `short_summary` is a display-only field (CLI / dashboards / markdown).
    # Overflow must not abort the PLAN phase — the parser trims with a
    # trailing ellipsis and emits a non-fatal `parse_warnings` entry.
    from core.contracts.plan_schema import PLAN_SHORT_SUMMARY_MAX_CHARS
    overflow = "x" * (PLAN_SHORT_SUMMARY_MAX_CHARS + 7)
    payload = (
        '{"short_summary": "' + overflow + '", '
        '"planning_context": "ok", '
        '"tasks": [{"id": "a", "goal": "first"}]}'
    )
    plan = parse_json_object(payload)
    assert len(plan.short_summary) == PLAN_SHORT_SUMMARY_MAX_CHARS
    assert plan.short_summary.endswith("…")
    assert len(plan.parse_warnings) == 1
    assert "short_summary" in plan.parse_warnings[0]


def test_parse_json_fence_raises_when_no_fence() -> None:
    with pytest.raises(PlanParseError, match="no ```json"):
        parse_json_fence("just some text, no fence here")


def test_parse_json_fence_raises_on_invalid_json() -> None:
    with pytest.raises(PlanParseError, match="not valid JSON"):
        parse_json_fence("```json\n{not json,}\n```")


# ── Markdown fallback ─────────────────────────────────────────────────────────

def test_parse_markdown_basic_sections() -> None:
    text = textwrap.dedent("""
        Plan overview prose.

        ## Task T1: Add endpoint
        **Goal:** add /foo
        **Skill:** backend-endpoint
        **Depends on:** none
        **Files:**
        - src/Controller/FooController.php
        **Done Criteria:**
        - returns 200
        - tests pass

        ## Task T2: Add client
        **Goal:** consume /foo
        **Depends on:** T1
        **Files:** web/foo.ts
    """)
    plan = parse_markdown_sections(text)
    assert plan.source == "markdown"
    assert len(plan.subtasks) == 2

    t1, t2 = plan.subtasks
    assert t1.id == "T1"
    assert t1.goal == "add /foo"
    assert t1.skill == "backend-endpoint"
    assert t1.depends_on == ()
    assert "src/Controller/FooController.php" in t1.files
    assert "returns 200" in t1.done_criteria

    assert t2.id == "T2"
    assert t2.depends_on == ("T1",)
    assert t2.files == ("web/foo.ts",)


def test_parse_markdown_no_sections_raises() -> None:
    with pytest.raises(PlanParseError, match="no '## Task"):
        parse_markdown_sections("just prose, no headings")


# ── parse_plan: combined entry point ──────────────────────────────────────────

def test_parse_plan_prefers_json_fence_when_both_present() -> None:
    text = textwrap.dedent("""
        ## Task md1: ignore me
        **Goal:** ignored

        ```json
        {"short_summary": "via json", "planning_context": "via json context", "tasks": [{"id": "j1", "goal": "from json"}]}
        ```
    """)
    plan = parse_plan(text)
    assert plan.source == "json"
    assert plan.subtasks[0].id == "j1"


def test_parse_plan_accepts_raw_json_object() -> None:
    text = """
    {
      "short_summary": "raw json",
      "planning_context": "raw json context",
      "tasks": [{"id": "j1", "goal": "from raw json"}]
    }
    """
    plan = parse_plan(text)
    assert plan.source == "json"
    assert plan.short_summary == "raw json"
    assert plan.planning_context == "raw json context"
    assert plan.subtasks[0].id == "j1"


def test_parse_plan_recovers_embedded_json_with_warning() -> None:
    text = """
    I inspected PUT /api/users/{id} and found the plan.

    {
      "short_summary": "raw json",
      "planning_context": "raw json context",
      "tasks": [{"id": "j1", "goal": "from raw json"}]
    }
    """
    plan = parse_plan(text)
    assert plan.source == "json"
    assert plan.short_summary == "raw json"
    assert plan.subtasks[0].id == "j1"
    assert len(plan.parse_warnings) == 1
    assert "stripped non-JSON text around plan JSON" in plan.parse_warnings[0]


def test_parse_plan_raw_json_error_does_not_silently_fallback() -> None:
    text = """
    {
      "short_summary": "broken",
      "planning_context": "broken context",
      "tasks": [
        {"id": "a", "goal": "do a", "depends_on": ["b"]},
        {"id": "b", "goal": "do b", "depends_on": ["a"]}
      ]
    }
    """
    with pytest.raises(PlanParseError, match="cycle"):
        parse_plan(text)


def test_parse_plan_falls_back_to_markdown_when_json_missing() -> None:
    text = textwrap.dedent("""
        ## Task A: do thing
        **Goal:** thing
    """)
    plan = parse_plan(text)
    assert plan.source == "markdown"
    assert plan.subtasks[0].id == "A"


def test_parse_plan_raises_when_both_paths_fail() -> None:
    with pytest.raises(PlanParseError):
        parse_plan("totally unstructured text")


def test_parse_plan_invalid_dag_in_json_fence_does_not_silently_fallback() -> None:
    # JSON fence parses cleanly but contains a dependency cycle. The bug
    # was: parse_plan caught the validation error and fell through to
    # markdown, masking the planning error from DECOMPOSE_QA.
    text = textwrap.dedent("""
        ## Task A: alpha
        **Goal:** alpha goal

        ## Task B: beta
        **Goal:** beta goal

        ```json
        {
          "short_summary": "cycle here",
          "planning_context": "cycle context",
          "tasks": [
            {"id": "a", "goal": "do a", "depends_on": ["b"]},
            {"id": "b", "goal": "do b", "depends_on": ["a"]}
          ]
        }
        ```
    """)
    with pytest.raises(PlanParseError, match="cycle"):
        parse_plan(text)


def test_parse_plan_invalid_dag_in_json_fence_unknown_dep() -> None:
    text = textwrap.dedent("""
        ```json
        {
          "short_summary": "dangling",
          "planning_context": "dangling context",
          "tasks": [
            {"id": "a", "goal": "do a", "depends_on": ["does-not-exist"]}
          ]
        }
        ```
    """)
    with pytest.raises(PlanParseError, match="unknown"):
        parse_plan(text)


# ── DAG validation ────────────────────────────────────────────────────────────

def _st(id: str, deps: tuple[str, ...] = ()) -> SubTask:
    return SubTask(id=id, goal=f"goal-{id}", depends_on=deps)


def test_validate_dag_accepts_valid_chain() -> None:
    validate_dag((_st("a"), _st("b", ("a",)), _st("c", ("b",))))


def test_validate_dag_rejects_duplicate_ids() -> None:
    with pytest.raises(PlanParseError, match="duplicate"):
        validate_dag((_st("a"), _st("a")))


def test_validate_dag_rejects_dangling_dependency() -> None:
    with pytest.raises(PlanParseError, match="unknown"):
        validate_dag((_st("a", ("b",)),))


def test_validate_dag_rejects_self_loop() -> None:
    with pytest.raises(PlanParseError, match="depends on itself"):
        validate_dag((_st("a", ("a",)),))


def test_validate_dag_rejects_cycle() -> None:
    with pytest.raises(PlanParseError, match="cycle"):
        validate_dag((_st("a", ("b",)), _st("b", ("a",))))


def test_validate_dag_rejects_empty_plan() -> None:
    with pytest.raises(PlanParseError, match="no subtasks"):
        validate_dag(())


# ── Topological waves ─────────────────────────────────────────────────────────

def test_topological_waves_groups_independent_tasks() -> None:
    subs = (
        _st("a"),
        _st("b"),
        _st("c", ("a", "b")),
        _st("d", ("c",)),
    )
    waves = topological_waves(subs)
    assert [sorted(s.id for s in w) for w in waves] == [["a", "b"], ["c"], ["d"]]


def test_topological_waves_single_chain() -> None:
    subs = (_st("a"), _st("b", ("a",)), _st("c", ("b",)))
    waves = topological_waves(subs)
    assert [[s.id for s in w] for w in waves] == [["a"], ["b"], ["c"]]


def test_topological_waves_diamond() -> None:
    subs = (
        _st("root"),
        _st("left", ("root",)),
        _st("right", ("root",)),
        _st("merge", ("left", "right")),
    )
    waves = topological_waves(subs)
    assert [sorted(s.id for s in w) for w in waves] == [
        ["root"],
        ["left", "right"],
        ["merge"],
    ]


def test_topological_waves_satisfied_ids_schedules_node_with_satisfied_dep() -> None:
    # ``b`` depends only on ``a``; ``a`` is not in the scheduling set but is
    # marked satisfied (a done node from a repair pass), so ``b`` schedules in
    # the first wave.
    subs = (_st("b", ("a",)),)
    waves = topological_waves(subs, satisfied_ids={"a"})
    assert [[s.id for s in w] for w in waves] == [["b"]]


def test_topological_waves_satisfied_ids_partial_deps() -> None:
    # ``c`` depends on a done node ``a`` and an in-scope node ``b``: the done
    # dep is dropped, so ``c`` waits only on ``b``.
    subs = (_st("b"), _st("c", ("a", "b")))
    waves = topological_waves(subs, satisfied_ids={"a"})
    assert [sorted(s.id for s in w) for w in waves] == [["b"], ["c"]]


def test_topological_waves_default_unchanged() -> None:
    # satisfied_ids=None preserves the original behaviour exactly.
    subs = (_st("a"), _st("b", ("a",)))
    none_ids = [[s.id for s in w] for w in topological_waves(subs, satisfied_ids=None)]
    default_ids = [[s.id for s in w] for w in topological_waves(subs)]
    assert default_ids == none_ids == [["a"], ["b"]]
