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
        max_rounds=1,
        profile_name="small_task", session_mode=SessionMode.AUTO,
        change_handoff="", output_dir=tmp_path,
    )
    session["status"] = "done"  # keep the test atexit-safe

    assert session["versions"] == {"orcho-core": "1.2.3", "orcho-mcp": "4.5.6"}
    persisted = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert persisted["versions"] == {"orcho-core": "1.2.3", "orcho-mcp": "4.5.6"}


def test_fresh_session_meta_records_effective_max_rounds(tmp_path: Path) -> None:
    """meta.json pins the effective budget the run was handed.

    The value is the frontend-resolved budget (inherited on a resume), stamped
    once on the ``versions`` seam as a read-only audit projection.
    ``checkpoints.db`` ``run_meta.config_json`` stays the resume authority.
    """
    session = bootstrap.init_session_with_atexit(
        task="t", project_path=tmp_path, plugin=PluginConfig(), model="m",
        max_rounds=4,
        profile_name="small_task", session_mode=SessionMode.AUTO,
        change_handoff="", output_dir=tmp_path,
    )
    session["status"] = "done"  # keep the test atexit-safe

    assert session["max_rounds"] == 4
    persisted = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert persisted["max_rounds"] == 4
    # ``bool`` is an ``int`` subclass; the budget must survive as a real int.
    assert type(persisted["max_rounds"]) is int
