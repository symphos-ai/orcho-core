"""Tests for ``pipeline.project.workspace_picker``.

Covers the bare-run project resolution introduced to fix the
multi-repo workspace bug where ``orcho run`` from a non-project cwd
silently degraded to in-tree edits.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from pipeline.project.workspace_picker import (
    WorkspaceProjectPickError,
    pick_project_for_fresh_run,
)


def _write_workspace_map(
    workspace: Path, projects: dict[str, str],
) -> None:
    config_dir = workspace / ".orcho"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.local.json").write_text(
        json.dumps({"projects": projects}),
        encoding="utf-8",
    )


class _TtyStream(io.StringIO):
    def __init__(self, payload: str = "") -> None:
        super().__init__(payload)

    def isatty(self) -> bool:  # noqa: D401 - matches TextIO contract
        return True


class _PipeStream(io.StringIO):
    def isatty(self) -> bool:
        return False


class TestEmptyOrMissingMap:
    def test_missing_workspace_config_raises_with_init_hint(
        self, tmp_path: Path,
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(WorkspaceProjectPickError) as exc_info:
            pick_project_for_fresh_run(
                cwd=tmp_path,
                workspace=ws,
                stdin=_TtyStream(),
                stdout=io.StringIO(),
            )
        assert "no registered projects" in exc_info.value.message
        assert "orcho workspace init" in exc_info.value.hint

    def test_empty_projects_map_raises_with_init_hint(
        self, tmp_path: Path,
    ) -> None:
        ws = tmp_path / "ws"
        _write_workspace_map(ws, {})
        with pytest.raises(WorkspaceProjectPickError) as exc_info:
            pick_project_for_fresh_run(
                cwd=tmp_path,
                workspace=ws,
                stdin=_TtyStream(),
                stdout=io.StringIO(),
            )
        assert "orcho workspace init" in exc_info.value.hint


class TestAutoPick:
    def test_cwd_inside_registered_project_auto_picks(
        self, tmp_path: Path,
    ) -> None:
        ws = tmp_path / "ws"
        api = tmp_path / "api"
        api.mkdir()
        (api / "src").mkdir()
        _write_workspace_map(ws, {"api": str(api)})

        picked = pick_project_for_fresh_run(
            cwd=api / "src",
            workspace=ws,
            stdin=_TtyStream(),
            stdout=io.StringIO(),
        )
        assert picked == api.resolve()

    def test_cwd_equals_project_path_auto_picks(
        self, tmp_path: Path,
    ) -> None:
        ws = tmp_path / "ws"
        api = tmp_path / "api"
        api.mkdir()
        _write_workspace_map(ws, {"api": str(api)})

        picked = pick_project_for_fresh_run(
            cwd=api,
            workspace=ws,
            stdin=_TtyStream(),
            stdout=io.StringIO(),
        )
        assert picked == api.resolve()

    def test_nested_projects_pick_innermost(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        _write_workspace_map(
            ws,
            {"outer": str(outer), "inner": str(inner)},
        )

        picked = pick_project_for_fresh_run(
            cwd=inner,
            workspace=ws,
            stdin=_TtyStream(),
            stdout=io.StringIO(),
        )
        assert picked == inner.resolve()


class TestInteractivePrompt:
    def test_tty_picks_first_via_default(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        api = tmp_path / "api"
        web = tmp_path / "web"
        api.mkdir()
        web.mkdir()
        _write_workspace_map(ws, {"api": str(api), "web": str(web)})

        stdin = _TtyStream("\n")  # empty line → default
        stdout = io.StringIO()
        picked = pick_project_for_fresh_run(
            cwd=tmp_path,
            workspace=ws,
            stdin=stdin,
            stdout=stdout,
        )
        # Alphabetical sort: api is first.
        assert picked == api.resolve()
        assert "Pick a project to run in" in stdout.getvalue()

    def test_tty_picks_explicit_choice(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        api = tmp_path / "api"
        web = tmp_path / "web"
        api.mkdir()
        web.mkdir()
        _write_workspace_map(ws, {"api": str(api), "web": str(web)})

        stdin = _TtyStream("2\n")
        picked = pick_project_for_fresh_run(
            cwd=tmp_path,
            workspace=ws,
            stdin=stdin,
            stdout=io.StringIO(),
        )
        assert picked == web.resolve()

    def test_tty_exit_choice_raises_no_selection(
        self, tmp_path: Path,
    ) -> None:
        ws = tmp_path / "ws"
        api = tmp_path / "api"
        api.mkdir()
        _write_workspace_map(ws, {"api": str(api)})

        # 1 entry + exit at index 2.
        stdin = _TtyStream("2\n")
        with pytest.raises(WorkspaceProjectPickError) as exc_info:
            pick_project_for_fresh_run(
                cwd=tmp_path,
                workspace=ws,
                stdin=stdin,
                stdout=io.StringIO(),
            )
        assert "No project selected" in exc_info.value.message

    def test_tty_invalid_then_valid_reprompts(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        api = tmp_path / "api"
        api.mkdir()
        _write_workspace_map(ws, {"api": str(api)})

        stdin = _TtyStream("99\n1\n")
        stdout = io.StringIO()
        picked = pick_project_for_fresh_run(
            cwd=tmp_path,
            workspace=ws,
            stdin=stdin,
            stdout=stdout,
        )
        assert picked == api.resolve()
        assert "Please answer one of" in stdout.getvalue()


class TestNonInteractive:
    def test_non_tty_stdin_raises_with_project_list(
        self, tmp_path: Path,
    ) -> None:
        ws = tmp_path / "ws"
        api = tmp_path / "api"
        web = tmp_path / "web"
        api.mkdir()
        web.mkdir()
        _write_workspace_map(ws, {"api": str(api), "web": str(web)})

        with pytest.raises(WorkspaceProjectPickError) as exc_info:
            pick_project_for_fresh_run(
                cwd=tmp_path,
                workspace=ws,
                stdin=_PipeStream(),
                stdout=io.StringIO(),
            )
        assert "not inside any registered project" in exc_info.value.message
        assert "api" in exc_info.value.hint
        assert "web" in exc_info.value.hint

    def test_no_interactive_flag_raises_even_on_tty(
        self, tmp_path: Path,
    ) -> None:
        ws = tmp_path / "ws"
        api = tmp_path / "api"
        api.mkdir()
        _write_workspace_map(ws, {"api": str(api)})

        with pytest.raises(WorkspaceProjectPickError):
            pick_project_for_fresh_run(
                cwd=tmp_path,
                workspace=ws,
                stdin=_TtyStream(),
                stdout=io.StringIO(),
                no_interactive=True,
            )
