"""Skill registry discovery for transcript telemetry."""
from __future__ import annotations

from agents.stream_parsers.skill_registry import discover_registered_skill_names


def test_discover_registered_skill_names_finds_project_child_skill(tmp_path) -> None:
    skill = tmp_path / "web" / ".agents" / "skills" / "frontend-qa"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Frontend QA\n", encoding="utf-8")

    assert "frontend-qa" in discover_registered_skill_names(str(tmp_path))
