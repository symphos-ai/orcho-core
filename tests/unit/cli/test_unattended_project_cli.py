# SPDX-License-Identifier: Apache-2.0
"""CLI project-run dispatch for unattended request-only fields."""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.project import cli as project_cli


def test_cli_dispatch_threads_unattended_request_only(
    monkeypatch,
    tmp_path,
) -> None:
    captured = {}

    def fake_run_project_pipeline(request):
        captured["request"] = request
        return SimpleNamespace(session={"status": "done"})

    monkeypatch.setattr(
        project_cli,
        "run_project_pipeline",
        fake_run_project_pipeline,
    )

    session = project_cli.run_pipeline(
        task="do it",
        project_dir=str(tmp_path),
        no_interactive=True,
        unattended=True,
    )

    request = captured["request"]
    assert session == {"status": "done"}
    assert request.no_interactive is True
    assert request.unattended is True
