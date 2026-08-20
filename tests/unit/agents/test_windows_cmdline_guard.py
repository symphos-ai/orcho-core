"""Fail-fast guard for Windows command-line length limits.

Phase prompts ride argv, and ``CreateProcessW`` caps the whole command line at
32767 characters (a ``.cmd``/``.bat`` shim is re-parsed by cmd.exe with an
~8k budget). Without the guard the overflow surfaces as ``WinError 206``
("filename or extension is too long"), which reads like a missing binary and
names neither the prompt nor the limit.
"""
from __future__ import annotations

import sys

import pytest

from agents.stream import (
    _WIN_CMD_SHIM_CMDLINE_MAX,
    _WIN_CREATEPROCESS_CMDLINE_MAX,
    _windows_cmdline_overflow,
)


def _cmd_of_length(total: int, argv0: str = "claude") -> list[str]:
    prompt = "x" * (total - len(argv0) - 1)
    return [argv0, prompt]


class TestWindowsCmdlineOverflow:
    def test_posix_never_guards(self) -> None:
        if sys.platform == "win32":
            pytest.skip("POSIX-only assertion")
        huge = _cmd_of_length(_WIN_CREATEPROCESS_CMDLINE_MAX * 2)
        assert _windows_cmdline_overflow(huge) is None

    def test_windows_within_limit_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert _windows_cmdline_overflow(["claude", "short prompt"]) is None

    def test_windows_overflow_names_length_and_limit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        overflow = _windows_cmdline_overflow(
            _cmd_of_length(_WIN_CREATEPROCESS_CMDLINE_MAX + 100),
        )
        assert overflow is not None
        assert str(_WIN_CREATEPROCESS_CMDLINE_MAX) in overflow
        assert "argv" in overflow

    def test_windows_stdin_delivery_does_not_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert _windows_cmdline_overflow(
            _cmd_of_length(_WIN_CREATEPROCESS_CMDLINE_MAX + 100), "stdin",
        ) is None

    def test_cmd_shim_uses_the_lower_cmd_exe_budget(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        length = _WIN_CMD_SHIM_CMDLINE_MAX + 100
        assert length < _WIN_CREATEPROCESS_CMDLINE_MAX
        overflow = _windows_cmdline_overflow(
            _cmd_of_length(length, argv0=r"C:\bin\claude-glm.cmd"),
        )
        assert overflow is not None
        assert str(_WIN_CMD_SHIM_CMDLINE_MAX) in overflow
        assert "claude-glm.cmd" in overflow
