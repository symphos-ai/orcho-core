"""Tests for ``pipeline.project.bootstrap`` workspace helpers.

Covers the shared ``autoderive_workspace_from_cwd`` used by both
``orcho run`` and ``orcho cross`` to override ``$ORCHO_WORKSPACE``
when the operator is standing inside a workspace tree.

Lives in its own module (not ``test_pipeline_runtime.py``) so the
fast bootstrap helpers stay loadable even when unrelated
prompt/contract validation breaks at module import time.
"""

from __future__ import annotations

import os


class TestAutoderiveWorkspaceFromCwd:
    """Used by both ``orcho run`` and ``orcho cross`` to override
    ``$ORCHO_WORKSPACE`` when the operator is standing inside a
    workspace tree. Symmetric semantics across both entry points.
    """

    def test_override_fires_when_env_diverges(self, tmp_path, monkeypatch):
        from core.infra import config
        from pipeline.project.bootstrap import autoderive_workspace_from_cwd
        stale_ws = tmp_path / "stale" / "workspace-orchestrator"
        stale_ws.mkdir(parents=True)
        fresh_root = tmp_path / "fresh"
        fresh_ws = fresh_root / "workspace-orchestrator"
        fresh_proj = fresh_root / "api"
        fresh_ws.mkdir(parents=True)
        fresh_proj.mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(stale_ws))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        # Avoid leaking a cached config from a prior test.
        config._reset_config()
        result = autoderive_workspace_from_cwd(cwd=str(fresh_proj))
        assert result == fresh_ws.resolve()
        assert os.environ["ORCHO_WORKSPACE"] == str(fresh_ws.resolve())
        assert os.environ["ORCHO_RUNSPACE"] == str(
            fresh_ws.resolve() / "runspace",
        )

    def test_no_override_when_env_already_matches(
        self, tmp_path, monkeypatch,
    ):
        from pipeline.project.bootstrap import autoderive_workspace_from_cwd
        root = tmp_path / "ws_root"
        ws = root / "workspace-orchestrator"
        proj = root / "api"
        ws.mkdir(parents=True)
        proj.mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        result = autoderive_workspace_from_cwd(cwd=str(proj))
        # Same workspace — returns it without printing or writing env.
        assert result == ws.resolve()
        assert os.environ["ORCHO_WORKSPACE"] == str(ws)

    def test_returns_none_when_cwd_outside_any_workspace(
        self, tmp_path, monkeypatch,
    ):
        from pipeline.project.bootstrap import autoderive_workspace_from_cwd
        monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path / "anything"))
        assert autoderive_workspace_from_cwd(cwd=str(tmp_path)) is None

    def test_silent_derive_when_no_env_was_set(
        self, tmp_path, monkeypatch, capsys,
    ):
        from pipeline.project.bootstrap import autoderive_workspace_from_cwd
        root = tmp_path / "ws_root"
        ws = root / "workspace-orchestrator"
        proj = root / "api"
        ws.mkdir(parents=True)
        proj.mkdir(parents=True)
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        result = autoderive_workspace_from_cwd(cwd=str(proj))
        assert result == ws.resolve()
        out = capsys.readouterr().out
        assert "auto-derived from cwd" in out
        # No warning colour, no foot-gun text about stale env.
        assert "stale" not in out

    def test_yellow_warning_when_displacing_stale_env(
        self, tmp_path, monkeypatch, capsys,
    ):
        from pipeline.project.bootstrap import autoderive_workspace_from_cwd
        stale_ws = tmp_path / "stale" / "workspace-orchestrator"
        stale_ws.mkdir(parents=True)
        fresh_root = tmp_path / "fresh"
        fresh_ws = fresh_root / "workspace-orchestrator"
        fresh_proj = fresh_root / "api"
        fresh_ws.mkdir(parents=True)
        fresh_proj.mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(stale_ws))
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        autoderive_workspace_from_cwd(cwd=str(fresh_proj))
        out = capsys.readouterr().out
        assert "workspace overridden" in out
        assert "stale $ORCHO_WORKSPACE" in out
        # Yellow + bold ANSI codes flank the override message.
        assert "\033[93m" in out
        assert "\033[1m" in out
