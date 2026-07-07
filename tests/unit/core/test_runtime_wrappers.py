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
