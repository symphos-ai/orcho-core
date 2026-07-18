"""Unit tests for sdk.runtimes — CLI agentic runtime detection."""
from __future__ import annotations

import sdk.runtimes as runtimes
from sdk.runtimes import (
    CLI_RUNTIMES,
    DetectedRuntime,
    RuntimeAvailability,
    assess_runtime_availability,
    detect_cli_runtimes,
    runtime_command,
    runtime_installed,
)


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


# ─── runtime_command / runtime_installed ────────────────────────────────────


def test_runtime_command_maps_aliases_and_passes_through() -> None:
    assert runtime_command("claude-glm") == "claude"
    assert runtime_command("claude") == "claude"
    assert runtime_command("codex") == "codex"
    # Unknown (plugin) runtime ids probe an executable of the same name.
    assert runtime_command("my-plugin-runtime") == "my-plugin-runtime"


def test_runtime_installed_respects_alias(monkeypatch) -> None:
    monkeypatch.setattr(
        runtimes.shutil, "which",
        lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
    )

    assert runtime_installed("claude") is True
    assert runtime_installed("claude-glm") is True
    assert runtime_installed("codex") is False


# ─── assess_runtime_availability ────────────────────────────────────────────


def test_assess_none_installed(monkeypatch) -> None:
    monkeypatch.setattr(runtimes.shutil, "which", lambda cmd: None)

    result = assess_runtime_availability(["claude", "codex", "claude"])

    assert isinstance(result, RuntimeAvailability)
    assert result.installed_runtimes == ()
    assert result.any_installed is False
    assert result.fallback_runtime is None
    # First-seen order, duplicates collapsed.
    assert result.missing_runtimes == ("claude", "codex")


def test_assess_partial_gap_offers_installed_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        runtimes.shutil, "which",
        lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
    )

    result = assess_runtime_availability(["claude", "codex"])

    assert result.installed_runtimes == ("claude",)
    assert result.any_installed is True
    assert result.missing_runtimes == ("codex",)
    assert result.fallback_runtime == "claude"


def test_assess_fallback_prefers_claude_over_catalogue_order(monkeypatch) -> None:
    monkeypatch.setattr(runtimes.shutil, "which", lambda cmd: "/x/" + cmd)

    result = assess_runtime_availability(["gemini"])

    assert result.missing_runtimes == ()
    # `codex` comes first in the display catalogue, but the engine-wide
    # default runtime wins as the offered replacement.
    assert result.fallback_runtime == "claude"


def test_assess_fallback_uses_first_installed_outside_preference(
    monkeypatch,
) -> None:
    """A future catalogue entry outside the preference list still wins
    as fallback when it is the only installed runtime."""
    monkeypatch.setattr(
        runtimes.shutil, "which",
        lambda cmd: "/usr/bin/gemini" if cmd == "gemini" else None,
    )
    monkeypatch.setattr(runtimes, "_FALLBACK_PREFERENCE", ("claude", "codex"))

    result = assess_runtime_availability(["claude"])

    assert result.installed_runtimes == ("gemini",)
    assert result.fallback_runtime == "gemini"


def test_assess_unknown_runtime_probed_by_own_name(monkeypatch) -> None:
    monkeypatch.setattr(
        runtimes.shutil, "which",
        lambda cmd: "/opt/bin/" + cmd if cmd in ("gemini", "my-rt") else None,
    )

    result = assess_runtime_availability(["my-rt", "codex"])

    assert result.missing_runtimes == ("codex",)
    assert result.fallback_runtime == "gemini"
