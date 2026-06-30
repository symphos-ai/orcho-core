"""
Per-subtask prompt rendering.

Verifies the composition order (project context → skill content block →
subtask block → project execution rules), the ``(text, SkillBinding)``
return tuple, and that optional fields drop out cleanly when empty.
"""

from __future__ import annotations

from pathlib import Path

from agents.entities import SubTask
from pipeline.plugins import PluginConfig
from pipeline.prompts.subtask import build_subtask_prompt as _build_subtask_turn
from pipeline.skills import SkillBinding, SkillPackage


def build_subtask_prompt(*args, **kwargs):
    """P1: ``build_subtask_prompt`` now returns ``(PromptTurn, binding)``.

    These content tests assert on the wire text, so read ``turn.text``.
    Byte-parity / part-shape / return-type are pinned in
    ``tests/unit/pipeline/phases/test_subtask_dag_session.py``.
    """
    turn, binding = _build_subtask_turn(*args, **kwargs)
    return turn.text, binding


def _pkg(
    name: str = "backend",
    description: str = "Adds REST endpoints to the PHP backend.",
    body: str = "Long-form specialist guidance about REST controllers.",
) -> SkillPackage:
    return SkillPackage(
        name=name,
        description=description,
        root_dir=Path("/tmp/skills") / name,
        skill_md_path=Path("/tmp/skills") / name / "SKILL.md",
        body=body,
        frontmatter={"name": name, "description": description},
        source="project",
        checksum=f"sha256:{name}",
    )


def _binding_for(pkg: SkillPackage, subtask_id: str) -> SkillBinding:
    return SkillBinding(
        skill_name=pkg.name,
        activation="architect_selected",
        source=pkg.source,
        checksum=pkg.checksum,
        subtask_id=subtask_id,
    )


def test_minimal_subtask_renders_only_subtask_block() -> None:
    sub = SubTask(id="t1", goal="say hello")
    text, binding = build_subtask_prompt(sub, PluginConfig())
    assert "## Current Executable Subtask `t1`" in text
    assert "**Goal:** say hello" in text
    assert "Change handoff mode: uncommitted" in text
    # No project context section when plugin has no language/architecture/file_hints
    assert "Project context" not in text
    assert "<skill_content" not in text
    assert binding is None


def test_includes_project_context_when_plugin_populated() -> None:
    plugin = PluginConfig(
        language="Python",
        architecture="Async API",
        file_hints=["src/", "tests/"],
    )
    sub = SubTask(id="t1", goal="g")
    text, _ = build_subtask_prompt(sub, plugin)
    assert "Project context" in text
    assert "Language: Python" in text
    assert "Architecture: Async API" in text
    assert "src/" in text


def test_skill_block_inserted_with_metadata_when_skill_provided() -> None:
    plugin = PluginConfig()
    pkg = _pkg()
    sub = SubTask(id="t1", goal="g")
    binding_in = _binding_for(pkg, sub.id)

    text, binding_out = build_subtask_prompt(
        sub, plugin, skill=pkg, binding=binding_in,
    )

    assert "<skill_content" in text
    assert 'name="backend"' in text
    assert 'source="project"' in text
    assert 'checksum="sha256:backend"' in text
    assert 'subtask_id="t1"' in text
    assert "REST controllers" in text
    # Binding flows through unchanged so the dag runner can record it.
    assert binding_out is binding_in


def test_subtask_fields_render_files_and_done_criteria_as_bullets() -> None:
    sub = SubTask(
        id="t1",
        goal="add endpoint",
        spec="POST /foo returns 201 on valid body.",
        files=("src/Controller/Foo.php", "tests/FooTest.php"),
        done_criteria=("test_foo passes", "swagger updated"),
        depends_on=("setup",),
    )
    text, _ = build_subtask_prompt(sub, PluginConfig())
    assert "**Spec:**" in text
    assert "POST /foo returns 201" in text
    assert "**Files in scope:**" in text
    assert "- src/Controller/Foo.php" in text
    assert "- tests/FooTest.php" in text
    assert "**Done criteria" in text
    assert "- test_foo passes" in text
    assert "Upstream subtasks already completed" in text
    assert "`setup`" in text


def test_plan_contract_and_dag_map_land_before_focused_subtask() -> None:
    sub = SubTask(id="apply-fix", goal="apply the patch")
    text, _ = build_subtask_prompt(
        sub,
        PluginConfig(),
        plan_contract="## Plan Contract\n\n**Goal:** Ship safely.",
        dag_map=(
            "## Execution Plan Context\n\n"
            "Background only — navigation, not instructions.\n\n"
            "- inspect-target — inspect (depends_on: none)\n"
            "- apply-fix — apply the patch (depends_on: inspect-target)\n"
        ),
    )

    # P2 layout: plan contract → compact DAG map → current executable subtask.
    assert "## Plan Contract" in text
    assert "## Execution Plan Context" in text
    assert "## Current Executable Subtask `apply-fix`" in text
    assert (
        text.index("## Plan Contract")
        < text.index("## Execution Plan Context")
        < text.index("## Current Executable Subtask `apply-fix`")
    )
    # The compact map carries the sibling as navigation only — no full spec.
    assert "- inspect-target — inspect (depends_on: none)" in text


def test_build_prompt_extra_appended_when_set() -> None:
    plugin = PluginConfig(build_prompt_extra="Run `make lint` after changes.")
    sub = SubTask(id="t1", goal="g")
    text, _ = build_subtask_prompt(sub, plugin)
    assert "Project rules for execution:" in text
    assert "make lint" in text


def test_commit_handoff_strategy_appended() -> None:
    sub = SubTask(id="t1", goal="g")
    text, _ = build_subtask_prompt(
        sub,
        PluginConfig(),
        change_handoff="commit_set",
    )
    assert "Change handoff mode: commit_set" in text


def test_project_dir_hint_emitted_when_provided() -> None:
    text, _ = build_subtask_prompt(
        SubTask(id="t1", goal="g"),
        PluginConfig(),
        project_dir="/tmp/proj",
    )
    assert "Working directory: /tmp/proj" in text


def test_composition_order_is_stable() -> None:
    plugin = PluginConfig(
        language="Python",
        build_prompt_extra="Lint after.",
    )
    pkg = _pkg(name="s", description="s", body="BODY")
    sub = SubTask(id="t1", goal="g")
    binding_in = _binding_for(pkg, sub.id)
    text, _ = build_subtask_prompt(
        sub, plugin, skill=pkg, binding=binding_in, project_dir="/p",
    )

    pos_ctx     = text.index("Project context")
    pos_skill   = text.index("<skill_content")
    pos_subtask = text.index("## Current Executable Subtask")
    pos_exec    = text.index("Project rules for execution")

    assert pos_ctx < pos_skill < pos_subtask < pos_exec
