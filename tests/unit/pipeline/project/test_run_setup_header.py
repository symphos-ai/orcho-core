from __future__ import annotations

from pathlib import Path

from pipeline.plugins import PluginConfig
from pipeline.project.run_setup import _skills_header_line
from pipeline.skills.types import SkillPackage


def _skill(name: str) -> SkillPackage:
    return SkillPackage(
        name=name,
        description=f"{name} skill",
        root_dir=Path(f"/skills/{name}"),
        skill_md_path=Path(f"/skills/{name}/SKILL.md"),
        body="body",
        frontmatter={"name": name, "description": f"{name} skill"},
    )


def test_skills_header_line_lists_discovered_skill_names_sorted() -> None:
    plugin = PluginConfig()
    plugin.skill_registry = {
        "quant-analytics-theory": _skill("quant-analytics-theory"),
        "quant-analytics-atas": _skill("quant-analytics-atas"),
    }

    assert (
        _skills_header_line(plugin)
        == "2: quant-analytics-atas, quant-analytics-theory"
    )


def test_skills_header_line_omits_empty_registry() -> None:
    assert _skills_header_line(PluginConfig()) is None
