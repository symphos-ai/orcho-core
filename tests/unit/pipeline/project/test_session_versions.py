"""meta.json carries the versions of the Orcho packages that wrote the run."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.protocols import SessionMode
from pipeline.plugins import PluginConfig
from pipeline.project import bootstrap


def test_fresh_session_meta_records_installed_orcho_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap, "installed_orcho_versions",
        lambda: {"orcho-core": "1.2.3", "orcho-mcp": "4.5.6"},
    )

    session = bootstrap.init_session_with_atexit(
        task="t", project_path=tmp_path, plugin=PluginConfig(), model="m",
        profile_name="small_task", session_mode=SessionMode.AUTO,
        change_handoff="", output_dir=tmp_path,
    )
    session["status"] = "done"  # keep the test atexit-safe

    assert session["versions"] == {"orcho-core": "1.2.3", "orcho-mcp": "4.5.6"}
    persisted = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert persisted["versions"] == {"orcho-core": "1.2.3", "orcho-mcp": "4.5.6"}
