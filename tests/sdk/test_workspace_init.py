"""SDK tests for :func:`sdk.init_workspace`.

Pins the user-facing bootstrap contract: filesystem layout, refusal
rules, idempotency, dry-run safety, and MCP config merge semantics.
"""
from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

from sdk import (
    DetectedProject,
    DetectedRuntime,
    ExtraProject,
    WorkspaceInitError,
    WorkspaceInitResult,
    discover_undetected_candidates,
    init_workspace,
)
from sdk.workspace import _find_nested_git_dirs

# ─── Layout & filesystem effects ────────────────────────────────────────────


def test_creates_workspace_layout(tmp_path: Path) -> None:
    root = tmp_path / "group"
    r = init_workspace(root)

    assert isinstance(r, WorkspaceInitResult)
    assert Path(r.workspace_dir).is_dir()
    assert Path(r.runs_dir).is_dir()
    assert Path(r.env_file).is_file()
    assert Path(r.local_config_file).is_file()
    ws = Path(r.workspace_dir)
    shared_config = ws / ".orcho" / "config.json"
    gitignore = ws / ".orcho" / ".gitignore"
    assert json.loads(shared_config.read_text(encoding="utf-8")) == {
        "_comment": (
            "Team-shared workspace configuration. Add active settings "
            "deliberately; personal overrides belong in config.local.json."
        ),
    }
    active_ignore_patterns = [
        line for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert active_ignore_patterns == ["config.local.json"]
    assert "config.json" not in active_ignore_patterns
    # .orcho/multiagent/prompts/ is part of the visible extension rails.
    assert (ws / ".orcho" / "multiagent" / "prompts").is_dir()
    assert (ws / ".orcho" / "multiagent" / "prompts" / "roles" / "README.md").is_file()
    assert (ws / ".orcho" / "multiagent" / "prompts" / "tasks" / "README.md").is_file()
    assert (ws / ".orcho" / "multiagent" / "prompts" / "formats" / "README.md").is_file()
    assert (ws / ".orcho" / "multiagent" / "plugin.py").is_file()
    assert (ws / ".orcho" / ".task-files" / "README.md").is_file()
    assert (ws / ".orcho" / "multiagent" / "AGENTS.md").is_file()
    assert (
        ws / ".orcho" / "multiagent" / "CLAUDE.md"
    ).read_text(encoding="utf-8") == "@./AGENTS.md\n"
    assert r.extension_points == (
        str(ws / ".orcho" / "multiagent" / "plugin.py"),
        str(ws / ".orcho" / "multiagent" / "prompts"),
        str(ws / ".orcho" / ".task-files"),
        str(ws / ".orcho" / "multiagent" / "AGENTS.md"),
        str(ws / ".orcho" / "multiagent" / "CLAUDE.md"),
    )


def test_task_files_readme_includes_authoring_guidance(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g")
    readme = (
        Path(r.workspace_dir) / ".orcho" / ".task-files" / "README.md"
    ).read_text(encoding="utf-8")

    assert "## Writing a good task file" in readme
    assert "Verification is the engine's job" in readme
    assert "targeted tests" in readme
    assert "direct `--task` input" in readme
    assert "manual-only, declared but unscheduled" in readme
    assert "lint on changed files" in readme
    assert "orcho quality-gates --project" in readme
    assert "Do not copy `orcho verify run ...` into the task" in readme
    assert (
        "https://github.com/symphos-ai/orcho-core/blob/main/"
        "docs/authoring-task-files.md"
    ) in readme


def test_workspace_agent_rules_define_gate_ownership_for_all_task_inputs(
    tmp_path: Path,
) -> None:
    result = init_workspace(tmp_path / "g")
    agents = (
        Path(result.workspace_dir) / ".orcho" / "multiagent" / "AGENTS.md"
    ).read_text(encoding="utf-8")

    assert "template belongs with the adjacent `plugin.py`" in agents
    assert "root `AGENTS.md`" in agents
    assert "When asked to configure Orcho" in agents
    assert "manifests, package-manager scripts" in agents
    assert "Do not call a command\n   cheap" in agents
    assert "do not default every new gate to `warn`" in agents
    assert "Use `require` immediately" in agents
    assert "make the delivery boundary `require` as well" in agents
    assert "Keep unproven, broad" in agents
    assert "operator handoff" in agents
    assert "orcho quality-gates --project ." in agents
    assert "empty generated verification skeleton" in agents
    assert "`--task`, `--task-file`, a\nfollow-up" in agents
    assert "the Orcho engine owns its official execution" in agents
    assert "focused tests, lint on changed files" in agents
    assert "manual-only, declared but unscheduled" in agents
    assert "Never invoke `orcho verify` from an implement subtask" in agents
    assert "Work in the checkout supplied by Orcho" in agents


def test_workspace_plugin_scaffold_includes_validation_safe_gate_pattern(
    tmp_path: Path,
) -> None:
    result = init_workspace(tmp_path / "g")
    plugin = (
        Path(result.workspace_dir) / ".orcho" / "multiagent" / "plugin.py"
    ).read_text(encoding="utf-8")

    assert "delivery_policy" not in plugin
    assert '"commands": {}' in plugin
    assert '"gate_sets": {}' in plugin
    assert '"selection": []' in plugin
    assert '"schedule": []' in plugin
    assert "ruff" not in plugin
    assert "pyproject.toml" not in plugin

    lines = plugin.splitlines()
    start = lines.index("    # BEGIN ORCHO VERIFICATION EXAMPLE") + 1
    end = lines.index("    # END ORCHO VERIFICATION EXAMPLE")
    active_example = "\n".join(
        line.removeprefix("    # ")
        for line in lines[start:end]
    )
    project = tmp_path / "project"
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        f"PLUGIN = {{\n{active_example}\n}}\n",
        encoding="utf-8",
    )

    from pipeline.plugins import load_plugin
    from pipeline.verification_contract import VerificationContract

    contract = VerificationContract.from_plugin(load_plugin(str(project)))

    assert contract is not None
    # The empty skeleton remains validation-safe. Agents must select a policy
    # from project evidence rather than copying one from the template.
    assert contract.delivery_policy is None
    assert tuple(contract.commands) == ()


def test_workspace_plugin_scaffold_is_importable_empty_plugin(
    tmp_path: Path,
) -> None:
    r = init_workspace(tmp_path / "g")
    plugin_path = Path(r.workspace_dir) / ".orcho" / "multiagent" / "plugin.py"
    spec = importlib.util.spec_from_file_location("orcho_scaffold", plugin_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.PLUGIN == {}


def test_creates_group_root_if_missing(tmp_path: Path) -> None:
    root = tmp_path / "does-not-exist-yet" / "group"
    assert not root.exists()
    r = init_workspace(root)
    assert Path(r.group_root) == root.resolve()


def test_env_file_is_executable(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g")
    mode = Path(r.env_file).stat().st_mode
    assert mode & stat.S_IXUSR, "env script must be executable for the user"


def test_env_file_exports_workspace_and_worktree(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g")
    body = Path(r.env_file).read_text(encoding="utf-8")
    assert "export ORCHO_WORKSPACE=" in body
    assert "export ORCHO_RUNSPACE=" in body
    # Must derive ORCHO_WORKSPACE from the script's own location so
    # sourcing it from any cwd still points at the right dir. zsh
    # leaves BASH_SOURCE empty under default settings, so the script
    # falls back to $0 which both shells set to the sourced path.
    assert "BASH_SOURCE" in body
    assert "$0" in body, "env script must fall back to $0 for zsh"


def test_env_file_resolves_to_workspace_under_bash(tmp_path: Path) -> None:
    """Source it via `bash -c` and confirm ORCHO_WORKSPACE points home."""
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    r = init_workspace(tmp_path / "g")
    out = subprocess.run(
        [bash, "-c", f'source "{r.env_file}" && echo "$ORCHO_WORKSPACE"'],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == r.workspace_dir


def test_env_file_resolves_to_workspace_under_zsh(tmp_path: Path) -> None:
    """Source it via `zsh -c` (macOS default) and confirm the same."""
    import shutil
    import subprocess
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh not available")
    r = init_workspace(tmp_path / "g")
    # Run from a different cwd to catch relative-path regressions.
    out = subprocess.run(
        [zsh, "-c", f'cd /tmp && source "{r.env_file}" && echo "$ORCHO_WORKSPACE"'],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == r.workspace_dir


def test_result_paths_align_with_layout(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g")
    ws = Path(r.workspace_dir)
    assert r.runs_dir == str(ws / "runspace" / "runs")
    assert r.env_file == str(ws / "orcho-env.sh")
    assert r.local_config_file == str(ws / ".orcho" / "config.local.json")
    assert r.workspace_dir == str(ws)


def test_workspace_local_config_snapshot_contains_override_surface(
    tmp_path: Path,
) -> None:
    root = tmp_path / "g"
    api = _make_repo(root, "api", "pyproject.toml")
    web = _make_repo(root, "web", "package.json")

    r = init_workspace(root)
    data = json.loads(Path(r.local_config_file).read_text(encoding="utf-8"))

    assert "$ORCHO_WORKSPACE/.orcho/config.local.json" in data["_comment"]
    assert "workspace config.json" in data["_comment"]
    assert "Environment variables still win" in data["_comment"]
    assert "phases" in data
    assert "plan" in data["phases"]
    assert "implement" in data["phases"]
    assert {"runtime", "model", "effort"} <= set(data["phases"]["implement"])
    assert data["phases"]["implement"]["runtime"]
    assert data["phases"]["implement"]["model"]
    assert data["phases"]["implement"]["effort"]
    for section in (
        "timeouts",
        "session",
        "codemap",
        "hypothesis",
        "pipeline",
        "language",
        "artifacts",
    ):
        assert section in data
    assert data["projects"] == {
        "api": str(api.resolve()),
        "web": str(web.resolve()),
    }


def test_existing_workspace_local_config_is_not_overwritten(
    tmp_path: Path,
) -> None:
    root = tmp_path / "g"
    existing = root / "workspace-orchestrator" / ".orcho" / "config.local.json"
    existing.parent.mkdir(parents=True)
    sentinel = {"phases": {"implement": {"effort": "xhigh"}}}
    existing.write_text(json.dumps(sentinel), encoding="utf-8")

    r = init_workspace(root)

    assert json.loads(existing.read_text(encoding="utf-8")) == sentinel
    assert str(existing) in r.skipped_paths


def test_existing_workspace_local_config_gets_missing_project_aliases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "g"
    api = _make_repo(root, "api", "pyproject.toml")
    web = _make_repo(root, "web", "package.json")
    existing = root / "workspace-orchestrator" / ".orcho" / "config.local.json"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        json.dumps({
            "phases": {"implement": {"effort": "xhigh"}},
            "projects": {"api": "/custom/api"},
        }),
        encoding="utf-8",
    )

    init_workspace(root)

    data = json.loads(existing.read_text(encoding="utf-8"))
    assert data["phases"] == {"implement": {"effort": "xhigh"}}
    assert data["projects"] == {
        "api": "/custom/api",
        "web": str(web.resolve()),
    }
    assert str(api.resolve()) != data["projects"]["api"]


# ─── Detected projects ──────────────────────────────────────────────────────


def _make_repo(parent: Path, name: str, marker: str = ".git") -> Path:
    repo = parent / name
    repo.mkdir(parents=True)
    if marker == ".git":
        (repo / marker).mkdir()
    else:
        (repo / marker).write_text("# marker", encoding="utf-8")
    return repo


def test_detects_child_repos_one_level_deep(tmp_path: Path) -> None:
    root = tmp_path / "group"
    root.mkdir()
    _make_repo(root, "proj-a", ".git")
    _make_repo(root, "proj-b", "pyproject.toml")
    _make_repo(root, "proj-c", "package.json")
    (root / "scratch").mkdir()  # not a repo — no marker

    r = init_workspace(root)
    names = sorted(p.name for p in r.detected_projects)
    assert names == ["proj-a", "proj-b", "proj-c"]


def test_excludes_workspace_and_caches(tmp_path: Path) -> None:
    root = tmp_path / "group"
    root.mkdir()
    _make_repo(root, "proj-a", ".git")
    # Things we explicitly never report.
    for noise in ("workspace-orchestrator", "node_modules",
                  ".venv", "__pycache__", ".idea"):
        _make_repo(root, noise, ".git")

    r = init_workspace(root)
    names = [p.name for p in r.detected_projects]
    assert names == ["proj-a"]


def test_skips_deeper_nested_repos(tmp_path: Path) -> None:
    root = tmp_path / "group"
    root.mkdir()
    _make_repo(root, "lvl1", ".git")
    deep = root / "lvl1" / "nested-repo"
    deep.mkdir()
    (deep / ".git").mkdir()

    r = init_workspace(root)
    names = [p.name for p in r.detected_projects]
    assert names == ["lvl1"]


# ─── CLI runtime detection (wiring; unit tests live in test_runtimes) ─────────


def test_init_attaches_detected_runtimes(tmp_path: Path, monkeypatch) -> None:
    import sdk.runtimes as runtimes

    installed = {"codex": "/usr/bin/codex", "claude": "/usr/local/bin/claude"}
    monkeypatch.setattr(runtimes.shutil, "which", lambda cmd: installed.get(cmd))

    r = init_workspace(tmp_path / "g")

    by_command = {rt.command: rt for rt in r.detected_runtimes}
    assert by_command["codex"].path == "/usr/bin/codex"
    assert by_command["gemini"].path is None
    assert isinstance(by_command["gemini"], DetectedRuntime)


# ─── Runtime availability & switch ──────────────────────────────────────────


def _which_only(*installed: str):
    return lambda cmd: f"/usr/bin/{cmd}" if cmd in installed else None


def test_init_records_missing_runtimes(tmp_path: Path, monkeypatch) -> None:
    import sdk.runtimes as runtimes

    monkeypatch.setattr(runtimes.shutil, "which", _which_only("claude"))

    r = init_workspace(tmp_path / "g")

    assert "codex" in r.missing_runtimes
    assert "claude" not in r.missing_runtimes
    assert r.runtime_override is None
    # Without an override the written config keeps the seeded runtimes.
    data = json.loads(Path(r.local_config_file).read_text(encoding="utf-8"))
    assert any(
        spec.get("runtime") == "codex" for spec in data["phases"].values()
    )


def test_runtime_override_remaps_fresh_config(
    tmp_path: Path, monkeypatch,
) -> None:
    import sdk.runtimes as runtimes

    monkeypatch.setattr(runtimes.shutil, "which", _which_only("claude"))

    r = init_workspace(tmp_path / "g", runtime_override="claude")

    assert r.runtime_override == "claude"
    data = json.loads(Path(r.local_config_file).read_text(encoding="utf-8"))
    phases = data["phases"]
    # Every phase now points at an installed runtime…
    assert all(spec["runtime"] == "claude" for spec in phases.values())
    # …and a switched phase borrows the model of a phase that was
    # already configured for the override runtime (models are
    # runtime-specific — keeping the old one would be invalid).
    assert phases["validate_plan"]["model"] == phases["plan"]["model"]


def test_runtime_override_noop_when_nothing_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    import sdk.runtimes as runtimes

    monkeypatch.setattr(runtimes.shutil, "which", lambda cmd: "/x/" + cmd)

    r = init_workspace(tmp_path / "g", runtime_override="claude")

    assert r.missing_runtimes == ()
    assert r.runtime_override is None
    data = json.loads(Path(r.local_config_file).read_text(encoding="utf-8"))
    assert any(
        spec.get("runtime") == "codex" for spec in data["phases"].values()
    )


def test_runtime_override_updates_existing_config(
    tmp_path: Path, monkeypatch,
) -> None:
    import sdk.runtimes as runtimes

    root = tmp_path / "g"
    existing = root / "workspace-orchestrator" / ".orcho" / "config.local.json"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        json.dumps({
            "phases": {
                "plan": {"runtime": "codex", "model": "gpt-x"},
                "implement": {"runtime": "claude", "model": "claude-y"},
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtimes.shutil, "which", _which_only("claude"))

    r = init_workspace(root, runtime_override="claude")

    assert r.runtime_override == "claude"
    data = json.loads(existing.read_text(encoding="utf-8"))
    assert data["phases"]["plan"]["runtime"] == "claude"
    # Donor model comes from a phase already on the override runtime.
    assert data["phases"]["plan"]["model"] == "claude-y"
    # Untouched phase keeps its spec verbatim.
    assert data["phases"]["implement"] == {
        "runtime": "claude", "model": "claude-y",
    }


def test_planned_phase_runtimes_merges_workspace_layer(
    tmp_path: Path,
) -> None:
    from sdk.workspace import planned_phase_runtimes

    root = tmp_path / "g"
    planned_before = planned_phase_runtimes(root)
    assert planned_before  # seeded from package defaults
    existing = root / "workspace-orchestrator" / ".orcho" / "config.local.json"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        json.dumps({"phases": {"plan": {"runtime": "gemini"}}}),
        encoding="utf-8",
    )

    planned = planned_phase_runtimes(root)

    assert planned["plan"] == "gemini"
    # Other phases still come from the seed layers.
    for phase, runtime in planned_before.items():
        if phase != "plan":
            assert planned[phase] == runtime


def test_planned_phase_runtimes_ignores_corrupt_workspace_file(
    tmp_path: Path,
) -> None:
    from sdk.workspace import planned_phase_runtimes

    root = tmp_path / "g"
    existing = root / "workspace-orchestrator" / ".orcho" / "config.local.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("{not json", encoding="utf-8")

    assert planned_phase_runtimes(root) == planned_phase_runtimes(tmp_path / "other")


def test_apply_runtime_override_skips_non_dict_specs(monkeypatch) -> None:
    import sdk.runtimes as runtimes
    from sdk.workspace import _apply_runtime_override

    monkeypatch.setattr(runtimes.shutil, "which", _which_only("claude"))
    phases = {
        "plan": {"runtime": "codex", "model": "gpt-x"},
        "bogus": "not-a-dict",
    }

    changed = _apply_runtime_override(phases, "claude")

    assert changed == ("plan",)
    assert phases["bogus"] == "not-a-dict"


def test_override_runtimes_in_file_tolerates_bad_content(
    tmp_path: Path, monkeypatch,
) -> None:
    import sdk.runtimes as runtimes
    from sdk.workspace import _override_runtimes_in_file

    monkeypatch.setattr(runtimes.shutil, "which", _which_only("claude"))

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert _override_runtimes_in_file(corrupt, "claude", dry_run=False) is False

    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    assert _override_runtimes_in_file(non_object, "claude", dry_run=False) is False


def test_override_runtimes_in_file_noop_when_nothing_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    import sdk.runtimes as runtimes
    from sdk.workspace import _override_runtimes_in_file

    monkeypatch.setattr(runtimes.shutil, "which", lambda cmd: "/x/" + cmd)
    cfg = tmp_path / "config.local.json"
    cfg.write_text(
        json.dumps({"phases": {"plan": {"runtime": "codex", "model": "m"}}}),
        encoding="utf-8",
    )

    assert _override_runtimes_in_file(cfg, "claude", dry_run=False) is False
    # File untouched.
    assert json.loads(cfg.read_text(encoding="utf-8"))["phases"]["plan"][
        "runtime"
    ] == "codex"


def test_override_runtimes_in_file_replaces_non_dict_phases(
    tmp_path: Path, monkeypatch,
) -> None:
    import sdk.runtimes as runtimes
    from sdk.workspace import _override_runtimes_in_file

    monkeypatch.setattr(runtimes.shutil, "which", _which_only("claude"))
    cfg = tmp_path / "config.local.json"
    cfg.write_text(json.dumps({"phases": "bogus"}), encoding="utf-8")

    changed = _override_runtimes_in_file(cfg, "claude", dry_run=False)

    assert changed is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert isinstance(data["phases"], dict)
    assert all(
        spec["runtime"] == "claude" for spec in data["phases"].values()
    )


# ─── Idempotency ────────────────────────────────────────────────────────────


def test_idempotent_second_run_creates_nothing(tmp_path: Path) -> None:
    r1 = init_workspace(tmp_path / "g")
    r2 = init_workspace(tmp_path / "g")

    assert r1.created_paths  # first run did create things
    assert r2.created_paths == ()
    # Everything skipped on the repeat run.
    assert set(r1.created_paths) <= set(r2.skipped_paths)


def test_scaffold_does_not_overwrite_existing_files(tmp_path: Path) -> None:
    r1 = init_workspace(tmp_path / "g")
    plugin = Path(r1.workspace_dir) / ".orcho" / "multiagent" / "plugin.py"
    plugin.write_text("PLUGIN = {'name': 'Custom'}\n", encoding="utf-8")

    r2 = init_workspace(tmp_path / "g")

    assert plugin.read_text(encoding="utf-8") == "PLUGIN = {'name': 'Custom'}\n"
    assert str(plugin) in r2.skipped_paths


def test_scaffold_preserves_custom_workspace_config_files(tmp_path: Path) -> None:
    first = init_workspace(tmp_path / "g")
    config_dir = Path(first.workspace_dir) / ".orcho"
    shared_config = config_dir / "config.json"
    gitignore = config_dir / ".gitignore"
    personal_config = Path(first.local_config_file)
    expected = {
        shared_config: '{"team": "custom"}\n',
        gitignore: "custom.local.json\n",
        personal_config: '{"phases": {"implement": {"effort": "high"}}}\n',
    }
    for path, body in expected.items():
        path.write_text(body, encoding="utf-8")

    second = init_workspace(tmp_path / "g")

    for path, body in expected.items():
        assert path.read_text(encoding="utf-8") == body
        assert str(path) in second.skipped_paths


def test_scaffold_does_not_overwrite_existing_task_files_readme(
    tmp_path: Path,
) -> None:
    r1 = init_workspace(tmp_path / "g")
    readme = Path(r1.workspace_dir) / ".orcho" / ".task-files" / "README.md"
    readme.write_text("# Custom task-file guidance\n", encoding="utf-8")

    r2 = init_workspace(tmp_path / "g")

    assert readme.read_text(encoding="utf-8") == "# Custom task-file guidance\n"
    assert str(readme) in r2.skipped_paths


def test_scaffold_does_not_overwrite_existing_agent_rules(
    tmp_path: Path,
) -> None:
    first = init_workspace(tmp_path / "g")
    agents = (
        Path(first.workspace_dir) / ".orcho" / "multiagent" / "AGENTS.md"
    )
    agents.write_text("# Custom project-group rules\n", encoding="utf-8")

    second = init_workspace(tmp_path / "g")

    assert agents.read_text(encoding="utf-8") == "# Custom project-group rules\n"
    assert str(agents) in second.skipped_paths


def test_no_scaffold_skips_extension_templates(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g", no_scaffold=True)
    ws = Path(r.workspace_dir)

    assert Path(r.local_config_file).is_file()
    assert not (ws / ".orcho" / "config.json").exists()
    assert not (ws / ".orcho" / ".gitignore").exists()
    assert not (ws / ".orcho" / "multiagent").exists()
    assert not (ws / ".orcho" / ".task-files").exists()
    assert r.extension_points == ()


# ─── Dry run ────────────────────────────────────────────────────────────────


def test_dry_run_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "g"
    r = init_workspace(root, dry_run=True)

    assert r.dry_run is True
    assert not root.exists(), "dry-run must not create the group root"
    assert r.created_paths, "result should still list what would be created"
    assert any(path.endswith(".orcho/multiagent/plugin.py") for path in r.created_paths)
    assert any(path.endswith("workspace-orchestrator/.orcho/config.json") for path in r.created_paths)
    assert any(path.endswith("workspace-orchestrator/.orcho/.gitignore") for path in r.created_paths)
    assert any(
        path.endswith("workspace-orchestrator/.orcho/multiagent/AGENTS.md")
        for path in r.created_paths
    )
    assert any(
        path.endswith("workspace-orchestrator/.orcho/multiagent/CLAUDE.md")
        for path in r.created_paths
    )


def test_dry_run_still_produces_snippet_and_detection(tmp_path: Path) -> None:
    root = tmp_path / "g"
    root.mkdir()
    _make_repo(root, "proj-a", "pyproject.toml")
    r = init_workspace(root, dry_run=True)

    assert r.detected_projects == (
        DetectedProject(name="proj-a", path=str(root / "proj-a")),
    )
    assert r.mcp_snippet["mcpServers"], "snippet must be populated in dry-run too"


# ─── Refusal rules ──────────────────────────────────────────────────────────


def test_refuses_filesystem_root() -> None:
    with pytest.raises(WorkspaceInitError, match="filesystem root"):
        init_workspace("/")


def test_refuses_home_directory(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    with pytest.raises(WorkspaceInitError, match="home directory"):
        init_workspace(fake_home)


def test_refuses_repo_looking_root_without_force(tmp_path: Path) -> None:
    root = tmp_path / "g"
    root.mkdir()
    (root / "pyproject.toml").write_text("# repo", encoding="utf-8")
    with pytest.raises(WorkspaceInitError, match="individual project repo"):
        init_workspace(root)


def test_force_allows_repo_looking_root(tmp_path: Path) -> None:
    root = tmp_path / "g"
    root.mkdir()
    (root / "pyproject.toml").write_text("# repo", encoding="utf-8")
    r = init_workspace(root, force=True)
    assert Path(r.workspace_dir).is_dir()


# ─── Existing env file ──────────────────────────────────────────────────────


def test_existing_env_file_different_is_not_overwritten(tmp_path: Path) -> None:
    # Pre-seed an env file with non-matching content.
    root = tmp_path / "g"
    (root / "workspace-orchestrator").mkdir(parents=True)
    env_path = root / "workspace-orchestrator" / "orcho-env.sh"
    sentinel = "# user-edited content do not touch\n"
    env_path.write_text(sentinel, encoding="utf-8")

    r = init_workspace(root)
    assert env_path.read_text(encoding="utf-8") == sentinel
    assert any("differs" in w for w in r.warnings)


# ─── MCP snippet generation ─────────────────────────────────────────────────


def test_snippet_default_server_name_slugged(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "My Cool Org")
    assert r.mcp_server_name == "orcho-my-cool-org"


def test_snippet_does_not_double_prefix_orcho(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "orcho_demo")
    assert r.mcp_server_name == "orcho-demo"


def test_mcp_server_name_override(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g", mcp_server_name="my-srv")
    assert r.mcp_server_name == "my-srv"
    assert "my-srv" in r.mcp_snippet["mcpServers"]


def test_orcho_mcp_command_override(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g", orcho_mcp_command="/abs/bin/orcho-mcp")
    entry = r.mcp_snippet["mcpServers"][r.mcp_server_name]
    assert entry["command"] == "/abs/bin/orcho-mcp"


# ─── MCP config file: write / merge / no-op / conflict ──────────────────────


def test_mcp_config_absent_writes_nothing(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g")
    assert r.mcp_config_path is None
    assert r.mcp_config_action == "printed"


def test_mcp_config_new_file(tmp_path: Path) -> None:
    cfg = tmp_path / "out" / ".mcp.json"
    cfg.parent.mkdir()
    r = init_workspace(tmp_path / "g", mcp_config=cfg)
    assert r.mcp_config_action == "wrote"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert r.mcp_server_name in data["mcpServers"]


def test_mcp_config_merge_preserves_existing_servers(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "other": {"command": "other-mcp", "args": ["--flag"]},
        },
    }), encoding="utf-8")

    r = init_workspace(tmp_path / "g", mcp_config=cfg)
    assert r.mcp_config_action == "merged"

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert set(data["mcpServers"].keys()) == {"other", r.mcp_server_name}
    assert data["mcpServers"]["other"]["command"] == "other-mcp"


def test_mcp_config_identical_server_is_noop(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    r1 = init_workspace(tmp_path / "g", mcp_config=cfg)
    before = cfg.read_text(encoding="utf-8")

    r2 = init_workspace(tmp_path / "g", mcp_config=cfg)
    assert r2.mcp_config_action == "no-op"
    assert cfg.read_text(encoding="utf-8") == before
    assert r1.mcp_server_name == r2.mcp_server_name


def test_mcp_config_conflicting_entry_errors_without_force(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / ".mcp.json"
    init_workspace(tmp_path / "g", mcp_config=cfg)
    # Mutate the entry so the next init sees a diff.
    data = json.loads(cfg.read_text(encoding="utf-8"))
    server_name = next(iter(data["mcpServers"]))
    data["mcpServers"][server_name]["command"] = "tampered"
    cfg.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(WorkspaceInitError, match="already exists"):
        init_workspace(tmp_path / "g", mcp_config=cfg)


def test_mcp_config_conflict_replaced_with_force(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    init_workspace(tmp_path / "g", mcp_config=cfg)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    server_name = next(iter(data["mcpServers"]))
    data["mcpServers"][server_name]["command"] = "tampered"
    cfg.write_text(json.dumps(data), encoding="utf-8")

    r = init_workspace(tmp_path / "g", mcp_config=cfg, force=True)
    assert r.mcp_config_action == "replaced"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    # Replaced — back to the canonical command.
    assert data["mcpServers"][server_name]["command"] == "orcho-mcp"


def test_mcp_config_invalid_json_errors_clearly(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    cfg.write_text("not valid json {", encoding="utf-8")
    with pytest.raises(WorkspaceInitError, match="could not parse"):
        init_workspace(tmp_path / "g", mcp_config=cfg)


def test_mcp_config_missing_parent_errors(tmp_path: Path) -> None:
    cfg = tmp_path / "no" / "such" / "dir" / ".mcp.json"
    with pytest.raises(WorkspaceInitError, match="parent directory"):
        init_workspace(tmp_path / "g", mcp_config=cfg)


def test_mcp_config_dry_run_does_not_write(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    r = init_workspace(tmp_path / "g", mcp_config=cfg, dry_run=True)
    assert r.mcp_config_action == "wrote"
    assert not cfg.exists(), "dry-run must not create the config"


# ─── _find_nested_git_dirs ───────────────────────────────────────────────────


def test_find_nested_git_dirs_finds_git_directory(tmp_path: Path) -> None:
    folder = tmp_path / "mono"
    folder.mkdir()
    nested = folder / "SubProject"
    nested.mkdir()
    (nested / ".git").mkdir()
    result = _find_nested_git_dirs(folder)
    assert result == ["SubProject"]


def test_find_nested_git_dirs_finds_git_file(tmp_path: Path) -> None:
    """Gitlinks (.git as a file) are recognised."""
    folder = tmp_path / "mono"
    folder.mkdir()
    sub = folder / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/worktrees/sub", encoding="utf-8")
    result = _find_nested_git_dirs(folder)
    assert result == ["sub"]


def test_find_nested_git_dirs_shallowest_first(tmp_path: Path) -> None:
    folder = tmp_path / "mono"
    folder.mkdir()
    (folder / "deep" / "nested").mkdir(parents=True)
    (folder / "deep" / "nested" / ".git").mkdir()
    (folder / "shallow").mkdir()
    (folder / "shallow" / ".git").mkdir()
    result = _find_nested_git_dirs(folder)
    assert result[0] == "shallow"
    assert "deep/nested" in result


def test_find_nested_git_dirs_respects_max_depth(tmp_path: Path) -> None:
    folder = tmp_path / "mono"
    folder.mkdir()
    deep = folder / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / ".git").mkdir()
    result = _find_nested_git_dirs(folder, max_depth=2)
    assert result == []


def test_find_nested_git_dirs_prunes_node_modules(tmp_path: Path) -> None:
    folder = tmp_path / "mono"
    folder.mkdir()
    nm = folder / "node_modules" / "some-pkg"
    nm.mkdir(parents=True)
    (nm / ".git").mkdir()
    result = _find_nested_git_dirs(folder)
    assert result == []


def test_find_nested_git_dirs_does_not_include_root_git(tmp_path: Path) -> None:
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / ".git").mkdir()
    result = _find_nested_git_dirs(folder)
    assert result == []


# ─── discover_undetected_candidates ─────────────────────────────────────────


def test_discover_undetected_candidates_finds_no_marker_folders(
    tmp_path: Path,
) -> None:
    root = tmp_path / "group"
    root.mkdir()
    (root / "has-git" / ".git").mkdir(parents=True)  # auto-detected
    no_marker = root / "no-marker"
    no_marker.mkdir()
    (no_marker / "SubProject").mkdir()
    (no_marker / "SubProject" / ".git").mkdir()

    candidates = discover_undetected_candidates(root)
    names = [c.name for c in candidates]
    assert "no-marker" in names
    assert "has-git" not in names


def test_discover_undetected_candidates_nested_git_dirs_populated(
    tmp_path: Path,
) -> None:
    root = tmp_path / "group"
    root.mkdir()
    folder = root / "mono"
    folder.mkdir()
    (folder / "SubProject").mkdir()
    (folder / "SubProject" / ".git").mkdir()

    candidates = discover_undetected_candidates(root)
    assert len(candidates) == 1
    assert candidates[0].nested_git_dirs == ("SubProject",)


def test_discover_undetected_candidates_excludes_workspace(tmp_path: Path) -> None:
    root = tmp_path / "group"
    root.mkdir()
    ws = root / "workspace-orchestrator"
    ws.mkdir()

    candidates = discover_undetected_candidates(root)
    assert not any(c.name == "workspace-orchestrator" for c in candidates)


# ─── init_workspace with extra_projects ─────────────────────────────────────


def test_init_workspace_extra_project_written_as_object_form(
    tmp_path: Path,
) -> None:
    root = tmp_path / "group"
    extra = ExtraProject(name="mono", path=str(tmp_path / "mono"), git_dir="src")
    r = init_workspace(root, extra_projects=[extra])
    config = json.loads(Path(r.local_config_file).read_text(encoding="utf-8"))
    entry = config["projects"]["mono"]
    assert isinstance(entry, dict)
    assert entry["git_dir"] == "src"
    assert entry["path"] == str(tmp_path / "mono")


def test_init_workspace_extra_project_empty_git_dir_stays_string(
    tmp_path: Path,
) -> None:
    root = tmp_path / "group"
    extra = ExtraProject(name="plain", path=str(tmp_path / "plain"), git_dir="")
    r = init_workspace(root, extra_projects=[extra])
    config = json.loads(Path(r.local_config_file).read_text(encoding="utf-8"))
    entry = config["projects"]["plain"]
    assert isinstance(entry, str)


def test_init_workspace_merge_does_not_overwrite_existing(tmp_path: Path) -> None:
    root = tmp_path / "group"
    r1 = init_workspace(root)
    config_path = Path(r1.local_config_file)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    data.setdefault("projects", {})
    data["projects"]["existing"] = "/old/path"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    extra = ExtraProject(name="existing", path="/new/path", git_dir="")
    init_workspace(root, extra_projects=[extra])

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["projects"]["existing"] == "/old/path"


def test_workspace_init_result_has_default_extra_fields(tmp_path: Path) -> None:
    r = init_workspace(tmp_path / "g")
    assert r.extra_projects == ()
    assert r.undetected_count == 0
    assert r.interactive is False


def test_workspace_init_result_carries_extra_projects(tmp_path: Path) -> None:
    extra = ExtraProject(name="x", path="/tmp/x", git_dir="")
    r = init_workspace(
        tmp_path / "g",
        extra_projects=[extra],
        undetected_count=1,
        interactive=True,
    )
    assert len(r.extra_projects) == 1
    assert r.undetected_count == 1
    assert r.interactive is True
