"""
Unit tests for plugin_loader.py.
Uses real filesystem (tempdir) — no subprocess.
"""

import textwrap
from pathlib import Path
from unittest.mock import patch

from pipeline.plugins import PluginConfig, describe_plugin, load_plugin
from pipeline.skills import SkillTrustPolicy


def _write_skill(root: Path, name: str, description: str | None = None) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description or f'{name} skill'}\n"
        "---\n\n"
        f"Body for {name}.\n",
        encoding="utf-8",
    )
    return skill_dir


class TestLoadPlugin:
    def test_missing_directory_returns_defaults(self) -> None:
        cfg = load_plugin("/nonexistent/path/that/does/not/exist")
        assert cfg.name == "Project"
        assert cfg.language == ""
        assert cfg.ma_artifacts_dir == ".orcho/artifacts"

    def test_valid_plugin_loaded(self, project_dir_with_plugin: str) -> None:
        cfg = load_plugin(project_dir_with_plugin)
        assert cfg.name == "Test Project"
        assert cfg.language == "Python"
        assert cfg.file_hints == ["src/", "tests/"]
        assert cfg.loaded_plugin_path.endswith(".orcho/multiagent/plugin.py")

    def test_plugin_load_does_not_write_bytecode_cache(self, project_dir: str) -> None:
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(
            "PLUGIN = {'name': 'No Cache'}",
            encoding="utf-8",
        )

        cfg = load_plugin(project_dir)

        assert cfg.name == "No Cache"
        assert not (plugin_path / "__pycache__").exists()

    def test_unknown_keys_ignored(self, project_dir: str) -> None:
        plugin_code = textwrap.dedent("""
            PLUGIN = {
                "name": "Generic Project",
                "unknown_future_field": "should be silently dropped",
            }
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)  # must not raise
        assert cfg.name == "Generic Project"

    def test_unknown_keys_emit_structured_warning(
        self, project_dir: str, capsys,
    ) -> None:
        # The loader is a normalising validator (see docstring). Unknown
        # keys are dropped, but the operator must see WHICH keys were
        # ignored — silent normalisation would mask plugin authoring
        # bugs after a field rename or deletion in core.
        plugin_code = textwrap.dedent("""
            PLUGIN = {
                "name": "Demo",
                "tech_stack": "stale field, deleted in M11.5",
                "testing": {"run_command": "pytest"},
            }
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        load_plugin(project_dir)
        captured = capsys.readouterr().out
        # Structured warning shape — uses ``warn`` from
        # core.observability.logging, not raw ``print``.
        assert "unknown PLUGIN keys" in captured
        assert "tech_stack" in captured
        assert "testing" in captured

    def test_syntax_error_returns_defaults(self, project_dir: str) -> None:
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text("this is !!! not valid python")

        cfg = load_plugin(project_dir)
        assert cfg.name == "Project"  # graceful fallback

    def test_plugin_is_not_dict_returns_defaults(self, project_dir: str) -> None:
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text("PLUGIN = 'a string, not a dict'")

        cfg = load_plugin(project_dir)
        assert cfg.name == "Project"

    def test_empty_plugin_dict_returns_defaults(self, project_dir: str) -> None:
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text("PLUGIN = {}")

        cfg = load_plugin(project_dir)
        assert cfg.name == "Project"  # all defaults
        assert cfg.loaded_plugin_path.endswith(".orcho/multiagent/plugin.py")

    def test_partial_plugin_dict(self, project_dir: str) -> None:
        """Only supplied keys should be overridden; rest stay default."""
        plugin_code = textwrap.dedent("""
            PLUGIN = {"language": "Go"}
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)
        assert cfg.language == "Go"
        assert cfg.name == "Project"   # still default
        assert cfg.ma_artifacts_dir == ".orcho/artifacts" # still default

    def test_worktree_bootstrap_key_loaded(self, project_dir: str) -> None:
        plugin_code = textwrap.dedent("""
            PLUGIN = {
                "worktree_bootstrap": [
                    {"copy": "libs"},
                    {"run": ["composer", "install"]},
                ],
            }
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)

        assert cfg.worktree_bootstrap == [
            {"copy": "libs"},
            {"run": ["composer", "install"]},
        ]

    def test_allowed_modifications_list_loaded(self, project_dir: str) -> None:
        plugin_code = textwrap.dedent("""
            PLUGIN = {
                "allowed_modifications": [
                    "package-lock.json — derived from package.json",
                    "tests/golden/*.snap — regenerated golden",
                ],
            }
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)

        assert cfg.allowed_modifications == [
            "package-lock.json — derived from package.json",
            "tests/golden/*.snap — regenerated golden",
        ]

    def test_allowed_modifications_non_list_dropped(
        self, project_dir: str, capsys,
    ) -> None:
        plugin_code = textwrap.dedent("""
            PLUGIN = {"allowed_modifications": "package-lock.json"}
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)  # must not raise

        assert cfg.allowed_modifications == []
        assert "allowed_modifications must be a list" in capsys.readouterr().out

    def test_allowed_modifications_mixed_types_keep_strings(
        self, project_dir: str, capsys,
    ) -> None:
        plugin_code = textwrap.dedent("""
            PLUGIN = {
                "allowed_modifications": [
                    "package-lock.json — derived",
                    123,
                    None,
                    "yarn.lock — derived",
                ],
            }
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)  # must not raise

        assert cfg.allowed_modifications == [
            "package-lock.json — derived",
            "yarn.lock — derived",
        ]
        assert "entries must be" in capsys.readouterr().out

    def test_allowed_modifications_missing_is_empty_list(
        self, project_dir: str,
    ) -> None:
        plugin_code = textwrap.dedent("""
            PLUGIN = {"name": "No Allowed Mods"}
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)

        assert cfg.allowed_modifications == []

    def test_verification_contract_fields_loaded(self, project_dir: str) -> None:
        plugin_code = textwrap.dedent("""
            PLUGIN = {
                "work_mode": "governed",
                "dependency_repos": {
                    "shared": {"path": "../shared", "ref": "main"},
                },
                "verification_envs": {
                    "ci": {"image": "python:3.12"},
                },
                "verification": {
                    "default_env": "ci",
                    "required": True,
                    "commands": {"lint": "ruff check .", "test": "pytest -q"},
                    "schedule": "on_phase_end",
                },
            }
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)

        assert cfg.work_mode == "governed"
        assert cfg.dependency_repos == {
            "shared": {"path": "../shared", "ref": "main"},
        }
        assert cfg.verification_envs == {"ci": {"image": "python:3.12"}}
        assert cfg.verification == {
            "default_env": "ci",
            "required": True,
            "commands": {"lint": "ruff check .", "test": "pytest -q"},
            "schedule": "on_phase_end",
        }

    def test_verification_contract_fields_default_empty(self, project_dir: str) -> None:
        plugin_code = textwrap.dedent("""
            PLUGIN = {"name": "No Contract"}
        """)
        plugin_path = Path(project_dir) / ".orcho" / "multiagent"
        plugin_path.mkdir(parents=True)
        (plugin_path / "plugin.py").write_text(plugin_code)

        cfg = load_plugin(project_dir)

        assert cfg.work_mode == ""
        assert cfg.dependency_repos == {}
        assert cfg.verification_envs == {}
        assert cfg.verification == {}

    def test_missing_plugin_has_empty_contract_defaults(self) -> None:
        cfg = load_plugin("/nonexistent/path/that/does/not/exist")
        assert cfg.work_mode == ""
        assert cfg.dependency_repos == {}
        assert cfg.verification_envs == {}
        assert cfg.verification == {}

    def test_workspace_agents_skills_discovered_from_worktree_project(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "runspace" / "worktrees" / "api"
        project.mkdir(parents=True)
        _write_skill(workspace / ".agents" / "skills", "workspace-skill")

        # Isolate the user-level layer (``~/.agents/skills``) so real skills
        # installed in the developer's home do not leak into the exact-set
        # assertion below.
        (tmp_path / "home").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        with patch("importlib.metadata.entry_points", return_value=[]):
            cfg = load_plugin(str(project))

        assert set(cfg.skill_registry) == {"workspace-skill"}
        assert cfg.skill_registry["workspace-skill"].source == "workspace"

    def test_trusted_project_agents_skills_populate_registry(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "api"
        plugin_dir = project / ".orcho" / "multiagent"
        plugin_dir.mkdir(parents=True)
        _write_skill(project / ".agents" / "skills", "project-skill")
        (plugin_dir / "plugin.py").write_text(
            "from pipeline.skills import SkillTrustPolicy\n"
            "PLUGIN = {'skill_trust': SkillTrustPolicy(trust_project=True)}\n",
            encoding="utf-8",
        )

        # Isolate the user-level layer (``~/.agents/skills``) so real skills
        # installed in the developer's home do not leak into the exact-set
        # assertion below.
        (tmp_path / "home").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        with patch("importlib.metadata.entry_points", return_value=[]):
            cfg = load_plugin(str(project))

        assert set(cfg.skill_registry) == {"project-skill"}
        assert cfg.skill_registry["project-skill"].source == "project"

    def test_loaded_orcho_skill_registry_reaches_decompose_prompt(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from pipeline import prompts

        workspace = tmp_path / "workspace"
        project = workspace / "api"
        plugin_dir = project / ".orcho" / "multiagent"
        plugin_dir.mkdir(parents=True)
        _write_skill(
            project / ".agents" / "skills",
            "architect-skill",
            description="Shapes the task DAG.",
        )
        (plugin_dir / "plugin.py").write_text(
            "from pipeline.skills import SkillTrustPolicy\n"
            "PLUGIN = {'skill_trust': SkillTrustPolicy(trust_project=True)}\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("ORCHO_WORKSPACE", str(workspace))
        with patch("importlib.metadata.entry_points", return_value=[]):
            cfg = load_plugin(str(project))

        out = prompts.decompose_plan_prompt("Split the work", str(project), cfg).text
        assert "AVAILABLE SKILLS" in out
        assert "`architect-skill`" in out
        assert "Shapes the task DAG." in out


class TestDescribePlugin:
    def test_no_plugin_message(self) -> None:
        desc = describe_plugin(PluginConfig())
        assert "no plugin" in desc

    def test_loaded_default_named_plugin_is_not_described_as_missing(self) -> None:
        cfg = PluginConfig(
            skill_trust=SkillTrustPolicy(trust_project=True),
            loaded_plugin_path="/repo/.orcho/multiagent/plugin.py",
        )

        desc = describe_plugin(cfg)

        assert "no plugin" not in desc
        assert "Plugin: Project" in desc
        assert "Plugin file: /repo/.orcho/multiagent/plugin.py" in desc
        assert "Skill trust: enabled project" in desc

    def test_named_plugin_shown(self) -> None:
        desc = describe_plugin(PluginConfig(name="My API", language="PHP"))
        assert "My API" in desc
        assert "PHP" in desc

    def test_file_hints_shown(self) -> None:
        cfg = PluginConfig(name="Svc", file_hints=["src/", "lib/"])
        desc = describe_plugin(cfg)
        assert "src/" in desc
        assert "lib/" in desc

    def test_architecture_shown(self) -> None:
        cfg = PluginConfig(name="Svc", architecture="Hexagonal")
        desc = describe_plugin(cfg)
        assert "Hexagonal" in desc
