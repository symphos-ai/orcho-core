"""Codex-native skill-scope projection tests."""

from __future__ import annotations

from pathlib import Path

from agents.runtimes.codex import CodexAgent
from agents.runtimes.codex_skills import CodexSkillScope


def _skill(home: Path, name: str) -> Path:
    skill_file = home / ".agents" / "skills" / name / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(f"---\nname: {name}\ndescription: test\n---\n")
    return skill_file


def test_default_scope_disables_user_skills_deterministically(
    tmp_path: Path,
) -> None:
    second = _skill(tmp_path, "zeta")
    first = _skill(tmp_path, "alpha")
    (tmp_path / ".agents" / "skills" / "not-a-skill").mkdir()

    args = CodexSkillScope().config_args(home_dir=tmp_path)

    assert args[0] == "-c"
    override = args[1]
    assert override.startswith("skills.config=[")
    assert f'path="{first}"' in override
    assert f'path="{second}"' in override
    assert override.index(str(first)) < override.index(str(second))
    assert "not-a-skill" not in override
    assert override.count("enabled=false") == 2


def test_default_scope_is_noop_without_user_skills(tmp_path: Path) -> None:
    assert CodexSkillScope().config_args(home_dir=tmp_path) == []


def test_explicit_user_scope_emits_no_disables(tmp_path: Path) -> None:
    _skill(tmp_path, "alpha")

    args = CodexSkillScope(include_user_skills=True).config_args(
        home_dir=tmp_path,
    )

    assert args == []


def test_codex_agent_projects_scope_into_exec_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _skill(tmp_path, "alpha")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    agent = CodexAgent(model="gpt-test")
    agent.bin = "/usr/bin/codex"

    default_cmd = agent._exec_cmd(mutates_artifacts=False)
    assert any("skills.config=" in arg for arg in default_cmd)

    agent.configure_skill_scope(include_user_skills=True)
    opted_in_cmd = agent._exec_cmd(mutates_artifacts=False)
    assert not any("skills.config=" in arg for arg in opted_in_cmd)
