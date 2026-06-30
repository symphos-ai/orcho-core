"""Unit tests for sdk.runtimes — CLI agentic runtime detection."""
from __future__ import annotations

import sdk.runtimes as runtimes
from sdk.runtimes import CLI_RUNTIMES, DetectedRuntime, detect_cli_runtimes


def test_detects_each_known_runtime_in_order(monkeypatch) -> None:
    installed = {"codex": "/usr/bin/codex", "claude": "/usr/local/bin/claude"}
    monkeypatch.setattr(runtimes.shutil, "which", lambda cmd: installed.get(cmd))

    result = detect_cli_runtimes()

    # One entry per catalogue row, in the declared display order.
    assert [rt.command for rt in result] == [cmd for _, cmd in CLI_RUNTIMES]
    by_command = {rt.command: rt for rt in result}
    assert by_command["codex"].path == "/usr/bin/codex"
    assert by_command["codex"].installed is True
    # Missing runtime is reported with a None path, not omitted.
    assert by_command["gemini"].path is None
    assert by_command["gemini"].installed is False


def test_none_installed_returns_full_catalogue(monkeypatch) -> None:
    monkeypatch.setattr(runtimes.shutil, "which", lambda cmd: None)

    result = detect_cli_runtimes()

    assert len(result) == len(CLI_RUNTIMES)
    assert all(not rt.installed for rt in result)
    assert all(isinstance(rt, DetectedRuntime) for rt in result)


def test_detect_is_side_effect_free_and_typed(monkeypatch) -> None:
    monkeypatch.setattr(runtimes.shutil, "which", lambda cmd: "/x/" + cmd)

    result = detect_cli_runtimes()

    assert isinstance(result, tuple)
    assert all(rt.installed for rt in result)
