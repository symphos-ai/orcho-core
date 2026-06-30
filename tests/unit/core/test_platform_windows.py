"""Platform-specific path and binary tests.

These tests use monkeypatching to simulate Windows (sys.platform = "win32") and
Unix environments without requiring a real OS switch. They verify:
claude_candidates() / codex_candidates() return correct paths per platform
_find_binary() expands %ENV_VARS% on Windows (os.path.expandvars)
_wrap_windows_cmd() wraps.cmd binaries correctly on Windows, no-ops on Unix
default_engine_home() points to the right base directory per platform
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ── claude_candidates / codex_candidates ────────────────────────────────────

@pytest.mark.parametrize("platform,expected_suffix", [
    ("win32",  ".cmd"),
    ("darwin", "/claude"),
    ("linux",  "/claude"),
])
def test_claude_candidates_by_platform(monkeypatch, platform, expected_suffix):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")

    # Re-import so _IS_WINDOWS is re-evaluated with patched sys.platform.
    import importlib

    import core.infra.platform as plat_mod
    importlib.reload(plat_mod)

    candidates = plat_mod.claude_candidates()
    assert any(expected_suffix in c for c in candidates), (
        f"Expected suffix {expected_suffix!r} in candidates={candidates!r}"
    )


@pytest.mark.parametrize("platform,expected_suffix", [
    ("win32",  ".cmd"),
    ("darwin", "/codex"),
    ("linux",  "/codex"),
])
def test_codex_candidates_by_platform(monkeypatch, platform, expected_suffix):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")

    import importlib

    import core.infra.platform as plat_mod
    importlib.reload(plat_mod)

    candidates = plat_mod.codex_candidates()
    assert any(expected_suffix in c for c in candidates)


# ── _find_binary: expandvars ─────────────────────────────────────────────────

def test_find_binary_expands_env_vars_unix(tmp_path, monkeypatch):
    """On Unix, $VAR or ${VAR} style candidates are expanded via expandvars."""
    fake_bin = tmp_path / "mytool"
    fake_bin.touch()

    monkeypatch.setenv("MY_TOOL_DIR", str(tmp_path))

    from core.infra.config import _find_binary
    result = _find_binary("mytool", ["$MY_TOOL_DIR/mytool"])
    assert Path(result) == fake_bin


@pytest.mark.skipif(sys.platform != "win32", reason="%%VAR%% expansion is Windows-only")
def test_find_binary_expands_env_vars_windows(tmp_path, monkeypatch):
    """On Windows, %VAR% style env vars in candidate paths are expanded."""
    fake_bin = tmp_path / "mytool.exe"
    fake_bin.touch()

    monkeypatch.setenv("MY_TOOL_DIR", str(tmp_path))

    from core.infra.config import _find_binary
    result = _find_binary("mytool", [r"%MY_TOOL_DIR%\mytool.exe"])
    assert Path(result) == fake_bin


def test_find_binary_expands_tilde(tmp_path, monkeypatch):
    """expanduser must resolve ~/ style candidates on Unix."""
    from core.infra.config import _find_binary

    fake_bin = tmp_path / "mytool"
    fake_bin.touch()

    # Pretend $HOME is tmp_path
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _find_binary("mytool", ["~/mytool"])
    assert Path(result) == fake_bin


def test_find_binary_raises_when_not_found():
    from core.infra.config import _find_binary
    with pytest.raises(RuntimeError, match="Cannot find"):
        _find_binary("nonexistent_xyz_binary_9182736", ["/definitely/does/not/exist"])


# ── _wrap_windows_cmd ────────────────────────────────────────────────────────

@pytest.mark.parametrize("platform,bin_path,expected", [
    # Windows.cmd → wraps with cmd /c
    ("win32",  r"C:\Users\test\AppData\Roaming\npm\claude.cmd",
               ["cmd", "/c", r"C:\Users\test\AppData\Roaming\npm\claude.cmd"]),
    # Windows.exe → no wrap
    ("win32",  r"C:\Program Files\claude\claude.exe",
               [r"C:\Program Files\claude\claude.exe"]),
    # Unix → always no wrap
    ("darwin", "/usr/local/bin/claude",
               ["/usr/local/bin/claude"]),
    ("linux",  "~/.local/bin/claude",
               ["~/.local/bin/claude"]),
])
def test_wrap_windows_cmd(monkeypatch, platform, bin_path, expected):
    monkeypatch.setattr(sys, "platform", platform)
    from core.infra.config import _wrap_windows_cmd
    assert _wrap_windows_cmd(bin_path) == expected


# ── default_engine_home ──────────────────────────────────────────────────────

def test_default_engine_home_unix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    import importlib

    import core.infra.platform as plat_mod
    importlib.reload(plat_mod)

    home = plat_mod.default_engine_home()
    assert home.name == "orcho-core"
    assert ".local" in str(home) or "share" in str(home)


def test_default_engine_home_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    import importlib

    import core.infra.platform as plat_mod
    importlib.reload(plat_mod)

    home = plat_mod.default_engine_home()
    assert home == tmp_path / "orcho-core"
