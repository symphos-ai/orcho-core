"""Contract tests for the read-only ``orcho workspace mcp`` command."""

from __future__ import annotations

from pathlib import Path

from sdk.workspace import init_workspace


def _run_cli(argv: list[str]) -> int:
    from cli.orcho import build_parser

    args = build_parser().parse_args(argv)
    return args.func(args)


def test_parser_accepts_workspace_mcp_options() -> None:
    from cli.orcho import build_parser

    args = build_parser().parse_args(
        [
            "workspace",
            "mcp",
            "--workspace",
            "/ws",
            "--mcp-server-name",
            "orcho-custom",
            "--orcho-mcp-command",
            "/opt/orcho-mcp",
        ]
    )

    assert args.workspace == "/ws"
    assert args.mcp_server_name == "orcho-custom"
    assert args.orcho_mcp_command == "/opt/orcho-mcp"
    assert args.func.__name__ == "cmd_workspace_mcp"


def test_explicit_workspace_renders_full_setup_without_writing(
    tmp_path: Path,
    capsys,
) -> None:
    result = init_workspace(tmp_path / "group")
    workspace = Path(result.workspace_dir)
    before = {
        path.relative_to(workspace): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }

    rc = _run_cli(
        [
            "workspace",
            "mcp",
            "--workspace",
            str(workspace),
            "--mcp-server-name",
            "orcho-custom",
            "--orcho-mcp-command",
            "/opt/orcho-mcp",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "codex mcp add orcho-custom" in out
    assert "/opt/orcho-mcp" in out
    assert "Claude app / JSON clients — mcpServers shape:" in out
    assert "Antigravity app — User/mcp.json servers shape:" in out
    assert out.count("Done when:") == 5
    assert "After client restart — verify:" in out
    after = {
        path.relative_to(workspace): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_project_bound_workspace_wins_over_ambient_workspace(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='project'\n")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    managed = init_workspace(project)

    ambient = tmp_path / "unrelated-workspace"
    (ambient / "runspace" / "runs").mkdir(parents=True)
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ambient))
    monkeypatch.chdir(project)

    assert _run_cli(["workspace", "mcp"]) == 0
    out = capsys.readouterr().out
    assert managed.workspace_dir in out
    assert str(ambient) not in out


def test_shared_workspace_resolves_from_registered_project(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    group = tmp_path / "group"
    project = group / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='project'\n")
    shared = init_workspace(group)
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    monkeypatch.chdir(project)

    assert _run_cli(["workspace", "mcp"]) == 0
    assert shared.workspace_dir in capsys.readouterr().out


def test_missing_workspace_returns_typed_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)

    assert _run_cli(["workspace", "mcp"]) == 1
    assert "Could not resolve an active workspace" in capsys.readouterr().err


def test_init_summary_is_short_when_identity_is_stored(tmp_path: Path) -> None:
    from cli._formatters import format_workspace_init

    result = init_workspace(
        tmp_path / "group",
        mcp_server_name="orcho-custom",
        orcho_mcp_command="/opt/orcho-mcp",
    )
    out = format_workspace_init(result)

    assert result.mcp_identity_stored is True
    assert "Full setup: orcho workspace mcp" in out
    assert "Full setup: orcho workspace mcp --workspace" not in out
    assert "--mcp-server-name" not in out
    assert "--orcho-mcp-command" not in out
    assert "```json" not in out
    assert "mcpServers shape" not in out
    assert "Antigravity app" not in out


def test_dry_run_init_summary_keeps_explicit_replay_flags(tmp_path: Path) -> None:
    from cli._formatters import format_workspace_init

    result = init_workspace(
        tmp_path / "group",
        mcp_server_name="orcho-custom",
        orcho_mcp_command="/opt/orcho-mcp",
        dry_run=True,
    )
    out = format_workspace_init(result)

    assert result.mcp_identity_stored is False
    assert "Full setup: orcho workspace mcp --workspace" in out
    assert "--mcp-server-name orcho-custom" in out
    assert "--orcho-mcp-command /opt/orcho-mcp" in out


def test_bare_workspace_mcp_replays_identity_stored_at_init(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    group = tmp_path / "group"
    project = group / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='project'\n")
    init_workspace(
        group,
        mcp_server_name="orcho-custom",
        orcho_mcp_command="/opt/orcho-mcp",
    )
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    monkeypatch.chdir(group)

    assert _run_cli(["workspace", "mcp"]) == 0
    out = capsys.readouterr().out
    assert "codex mcp add orcho-custom" in out
    assert "/opt/orcho-mcp" in out


def test_workspace_mcp_flags_override_stored_identity(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    group = tmp_path / "group"
    init_workspace(
        group,
        mcp_server_name="orcho-custom",
        orcho_mcp_command="/opt/orcho-mcp",
    )
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    monkeypatch.chdir(group)

    rc = _run_cli(
        [
            "workspace",
            "mcp",
            "--mcp-server-name",
            "orcho-override",
            "--orcho-mcp-command",
            "/override/orcho-mcp",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "codex mcp add orcho-override" in out
    assert "/override/orcho-mcp" in out
    assert "orcho-custom" not in out


def test_repeat_init_without_flags_keeps_stored_identity(tmp_path: Path) -> None:
    group = tmp_path / "group"
    init_workspace(
        group,
        mcp_server_name="orcho-custom",
        orcho_mcp_command="/opt/orcho-mcp",
    )

    repeat = init_workspace(group)

    assert repeat.mcp_server_name == "orcho-custom"
    entry = repeat.mcp_snippet["mcpServers"]["orcho-custom"]
    assert entry["command"] == "/opt/orcho-mcp"
