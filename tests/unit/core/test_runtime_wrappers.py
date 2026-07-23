from __future__ import annotations

import os

import pytest

from core.infra.runtime_wrappers import (
    RuntimeWrapperError,
    install_runtime_wrapper,
    runtime_wrapper_default_path,
    runtime_wrapper_env_var,
    runtime_wrapper_names,
    runtime_wrapper_script,
)


def test_claude_glm_wrapper_template_is_packaged() -> None:
    script = runtime_wrapper_script("claude-glm")

    assert "ANTHROPIC_BASE_URL" in script
    assert "exec claude" in script
    assert runtime_wrapper_env_var("claude-glm") == "CLAUDE_GLM_BIN"
    assert runtime_wrapper_names() == ("claude-glm",)


def test_install_runtime_wrapper_writes_executable(tmp_path) -> None:
    destination = tmp_path / "bin" / "claude-glm"
    result = install_runtime_wrapper("claude-glm", destination=destination)

    assert result.path == destination
    assert destination.read_text() == runtime_wrapper_script("claude-glm")
    assert os.access(destination, os.X_OK)
    assert result.already_current is False


def test_install_runtime_wrapper_is_idempotent(tmp_path) -> None:
    destination = tmp_path / "claude-glm"
    install_runtime_wrapper("claude-glm", destination=destination)

    result = install_runtime_wrapper("claude-glm", destination=destination)

    assert result.already_current is True


def test_install_runtime_wrapper_refuses_overwrite_without_force(tmp_path) -> None:
    destination = tmp_path / "claude-glm"
    destination.write_text("custom\n")

    with pytest.raises(RuntimeWrapperError, match="pass --force"):
        install_runtime_wrapper("claude-glm", destination=destination)

    assert destination.read_text() == "custom\n"


def test_install_runtime_wrapper_can_force_overwrite(tmp_path) -> None:
    destination = tmp_path / "claude-glm"
    destination.write_text("custom\n")

    install_runtime_wrapper("claude-glm", destination=destination, force=True)

    assert destination.read_text() == runtime_wrapper_script("claude-glm")


def test_unknown_runtime_wrapper_is_rejected() -> None:
    with pytest.raises(RuntimeWrapperError, match="unknown runtime wrapper"):
        runtime_wrapper_default_path("not-real")


def test_claude_glm_missing_key_message_is_cross_platform() -> None:
    script = runtime_wrapper_script("claude-glm")

    # (a) what is missing
    assert "GLM Coding Plan key" in script
    # (b) auth source precedence wording, naming the env var
    assert "precedence" in script
    assert "ANTHROPIC_AUTH_TOKEN" in script
    # (c) one-line smoke test after setup
    assert "claude-glm --print --model 'glm-5.2[1m]' 'Reply OK only.'" in script
    # (d) the Claude Code connectors warning is documented as expected
    assert "connectors" in script
    assert "expected" in script
    # never a real secret value, only the placeholder
    assert "<GLM Coding Plan key>" in script


def test_runtime_wrapper_script_defaults_to_host_platform() -> None:
    """No platform override resolves to the host. On the POSIX gate runner
    that is the .sh twin, so a regression that flipped the default to the
    .cmd would break the existing ``exec claude`` expectation."""
    posix_script = runtime_wrapper_script("claude-glm")
    cmd_script = runtime_wrapper_script("claude-glm", platform="win32")
    assert posix_script != cmd_script
    assert "exec claude" in posix_script
    assert "claude %*" in cmd_script


def test_claude_glm_windows_cmd_template_contract() -> None:
    cmd = runtime_wrapper_script("claude-glm", platform="win32")

    # delegation to claude (exec-like handoff) + GLM base URL present
    assert "claude %*" in cmd
    assert "ANTHROPIC_BASE_URL" in cmd
    # current-process PowerShell setup
    assert "powershell -Command" in cmd
    assert "$env:ANTHROPIC_AUTH_TOKEN" in cmd
    # persistent option + explicit restart caveat
    assert "SetEnvironmentVariable" in cmd
    assert "restart your shell" in cmd
    # the Claude Code connectors warning is documented as expected
    assert "connectors" in cmd
    assert "expected" in cmd
    # placeholder, never a real key value
    assert "GLM Coding Plan key" in cmd


def test_install_runtime_wrapper_writes_windows_cmd(tmp_path) -> None:
    destination = tmp_path / "claude-glm.cmd"
    result = install_runtime_wrapper(
        "claude-glm", destination=destination, platform="win32",
    )

    assert result.path == destination
    assert destination.read_text() == runtime_wrapper_script(
        "claude-glm", platform="win32",
    )
    assert result.already_current is False


def test_install_runtime_wrapper_windows_cmd_is_idempotent(tmp_path) -> None:
    destination = tmp_path / "claude-glm.cmd"
    install_runtime_wrapper(
        "claude-glm", destination=destination, platform="win32",
    )

    result = install_runtime_wrapper(
        "claude-glm", destination=destination, platform="win32",
    )

    assert result.already_current is True


def test_install_runtime_wrapper_windows_cmd_refuses_overwrite(tmp_path) -> None:
    destination = tmp_path / "claude-glm.cmd"
    destination.write_text("custom\n")

    with pytest.raises(RuntimeWrapperError, match="pass --force"):
        install_runtime_wrapper(
            "claude-glm", destination=destination, platform="win32",
        )

    assert destination.read_text() == "custom\n"


def test_runtime_wrapper_default_path_windows(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))

    path = runtime_wrapper_default_path("claude-glm", platform="win32")

    assert path == appdata / "npm" / "claude-glm.cmd"
    assert str(path).endswith("claude-glm.cmd")
