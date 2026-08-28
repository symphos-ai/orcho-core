"""Decision-boundary tests for project plugin materialisation at init."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.plugins import PLUGIN_RELATIVE_PATH, describe_plugin, load_plugin


def _args(root: Path, **overrides) -> SimpleNamespace:
    values = {
        "project_group_root": str(root),
        "workspace_dir": None,
        "workspace_name": None,
        "mcp_config": None,
        "mcp_server_name": None,
        "orcho_mcp_command": "orcho-mcp",
        "force": False,
        "dry_run": False,
        "no_interactive": False,
        "no_scaffold": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _registered_python_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "group"
    project = root / "project"
    package = project / "demo_pkg"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (package / "__init__.py").write_text("\n")
    return root, project


def _tty_input(text: str) -> io.StringIO:
    stdin = io.StringIO(text)
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    return stdin


@pytest.fixture(autouse=True)
def _runtimes_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sdk.runtimes.shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"codex", "claude", "gemini"} else None,
    )


def test_tty_yes_materializes_each_registered_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    from cli.orcho import cmd_workspace_init
    from pipeline.project import project_discovery_prompt
    from sdk.workspace import ExtraProject

    root, project = _registered_python_project(tmp_path)
    confirmed = root / "confirmed"
    confirmed.mkdir()
    monkeypatch.setattr(
        project_discovery_prompt,
        "prompt_for_extra_projects",
        lambda _: [ExtraProject(name="confirmed", path=str(confirmed))],
    )
    monkeypatch.setattr("sys.stdin", _tty_input("y\n"))

    assert cmd_workspace_init(_args(root)) == 0

    destination = project / PLUGIN_RELATIVE_PATH
    confirmed_destination = confirmed / PLUGIN_RELATIVE_PATH
    out = capsys.readouterr().out
    assert destination.is_file()
    assert confirmed_destination.is_file()
    assert "Project plugin configuration" in out
    assert "gates, route repairable failures, and retain readiness evidence" in out
    assert "https://docs.orcho.dev/extend/project-instructions/" in out
    assert f"created {destination}" in out
    assert f"created {confirmed_destination}" in out
    # The markerless project is disclosed before the question and its
    # created line is differentiated; the marker-backed project stays green.
    assert (
        "no repo markers detected in confirmed — a skeleton will be created; "
        "fill lint/test commands yourself" in out
    )
    assert "no repo markers detected in project" not in out
    assert f"created {confirmed_destination} (empty — fill commands)" in out
    assert f"created {destination} (empty" not in out
    assert "Next: review the plugin, then: orcho run --task '...' --mock" in out
    assert "generic mode" not in describe_plugin(load_plugin(str(project)))


@pytest.mark.parametrize("answer", ["n\n", ""])
def test_tty_decline_or_eof_leaves_project_tree_unmodified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, answer: str,
) -> None:
    from cli.orcho import cmd_workspace_init

    root, project = _registered_python_project(tmp_path)
    monkeypatch.setattr("sys.stdin", _tty_input(answer))

    assert cmd_workspace_init(_args(root)) == 0

    captured = capsys.readouterr()
    assert not (project / PLUGIN_RELATIVE_PATH).exists()
    assert "failed" not in captured.out
    assert captured.err == ""


def test_tty_ctrl_c_declines_without_false_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    from cli.orcho import cmd_workspace_init

    class InterruptingTty:
        def isatty(self) -> bool:
            return True

        def readline(self) -> str:
            raise KeyboardInterrupt

    root, project = _registered_python_project(tmp_path)
    monkeypatch.setattr("sys.stdin", InterruptingTty())

    assert cmd_workspace_init(_args(root)) == 0

    captured = capsys.readouterr()
    assert not (project / PLUGIN_RELATIVE_PATH).exists()
    assert "failed" not in captured.out
    assert captured.err == ""


def test_existing_plugin_is_skipped_without_byte_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    from cli.orcho import cmd_workspace_init

    root, project = _registered_python_project(tmp_path)
    destination = project / PLUGIN_RELATIVE_PATH
    destination.parent.mkdir(parents=True)
    before = b"PLUGIN = {'name': 'keep'}\n"
    destination.write_bytes(before)
    monkeypatch.setattr("sys.stdin", _tty_input("y\n"))

    assert cmd_workspace_init(_args(root)) == 0

    assert destination.read_bytes() == before
    assert f"skipped {destination}" in capsys.readouterr().out


def test_tty_with_no_registered_projects_does_not_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    from cli.orcho import cmd_workspace_init

    root = tmp_path / "empty-group"
    root.mkdir()
    monkeypatch.setattr("sys.stdin", _tty_input("y\n"))

    assert cmd_workspace_init(_args(root)) == 0

    assert "Project plugin configuration" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("overrides", "stdin"),
    [
        ({"no_interactive": True}, _tty_input("y\n")),
        ({}, io.StringIO("y\n")),
        ({"dry_run": True}, _tty_input("y\n")),
    ],
)
def test_non_eligible_modes_do_not_prompt_or_materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    overrides: dict,
    stdin: io.StringIO,
) -> None:
    import cli._workspace_init as workspace_init_module
    from cli.orcho import cmd_workspace_init

    root, project = _registered_python_project(tmp_path)
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(
        workspace_init_module,
        "materialize_project_plugins",
        lambda _: pytest.fail("materializer must not run"),
    )

    assert cmd_workspace_init(_args(root, **overrides)) == 0

    out = capsys.readouterr().out
    assert "Project plugin configuration" not in out
    assert not (project / PLUGIN_RELATIVE_PATH).exists()


def test_facade_delegates_to_focused_workspace_init_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli._workspace_init as workspace_init_module
    from cli.orcho import cmd_workspace_init

    sentinel = object()
    monkeypatch.setattr(workspace_init_module, "run_workspace_init", lambda args: args is sentinel)

    assert cmd_workspace_init(sentinel) is True
