"""End-to-end: ``_stream_run`` filters env and masks output when a policy is supplied."""
from __future__ import annotations

import platform
import sys

import pytest

from agents.stream import _stream_run
from pipeline.sandbox.policy import SandboxMode, SandboxPolicy


@pytest.mark.skipif(
    platform.system().lower() == "windows",
    reason="PTY streamer is Unix-only; Windows is covered by a separate path",
)
def test_stream_run_strips_env_when_policy_active(tmp_path) -> None:
    # Inject a fake secret into the parent env and verify the child
    # cannot see it. We use this Python helper script that dumps env
    # keys to stdout.
    import os

    os.environ["BOGUS_SECRET_VAR"] = "must-not-leak"
    try:
        policy = SandboxPolicy(mode=SandboxMode.ENV)
        stdout, rc, _stderr, _dur = _stream_run(
            [sys.executable, "-c",
             "import os; print('\\n'.join(sorted(os.environ.keys())))"],
            sandbox_policy=policy,
        )
        assert rc == 0
        assert "PATH" in stdout
        assert "BOGUS_SECRET_VAR" not in stdout
    finally:
        os.environ.pop("BOGUS_SECRET_VAR", None)


@pytest.mark.skipif(
    platform.system().lower() == "windows",
    reason="PTY streamer is Unix-only",
)
def test_stream_run_masks_tokens_in_live_log(tmp_path) -> None:
    from agents.stream import set_agent_log
    log_path = tmp_path / "output.log"
    set_agent_log(log_path)
    try:
        policy = SandboxPolicy(mode=SandboxMode.ENV)
        # Have the child print a known token shape — the masker
        # should rewrite it before the log file is written.
        token = "sk-ant-api01-" + "x" * 40
        _stream_run(
            [sys.executable, "-c", f"print('here is the key: {token}')"],
            sandbox_policy=policy,
        )
        log_text = log_path.read_text(encoding="utf-8")
        assert token not in log_text
        assert "***MASKED***" in log_text
    finally:
        set_agent_log(None)


@pytest.mark.skipif(
    platform.system().lower() == "windows",
    reason="PTY streamer is Unix-only",
)
def test_stream_run_returns_raw_stdout_even_when_masked(tmp_path) -> None:
    """Runtime parsers depend on raw stdout — masking lives on display path only."""
    policy = SandboxPolicy(mode=SandboxMode.ENV)
    token = "sk-ant-api01-" + "y" * 40
    stdout, rc, _stderr, _dur = _stream_run(
        [sys.executable, "-c", f"print('{token}')"],
        sandbox_policy=policy,
    )
    assert rc == 0
    # ADR 0034 contract: returned stdout stays raw so JSON parsers
    # (Claude session-id extraction etc.) keep working.
    assert token in stdout


def test_stream_run_with_none_policy_is_pre_l1_behaviour() -> None:
    """sandbox_policy=None must keep the legacy behaviour intact."""
    stdout, rc, _stderr, _dur = _stream_run(
        [sys.executable, "-c", "print('hello')"],
        sandbox_policy=None,
    )
    assert rc == 0
    assert "hello" in stdout
