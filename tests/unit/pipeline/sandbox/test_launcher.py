"""Launcher selection + null backend + EnvUnix prepare contract."""
from __future__ import annotations

import platform
import sys

import pytest

from pipeline.sandbox.backends.null_backend import NullLauncher
from pipeline.sandbox.launcher import select_launcher
from pipeline.sandbox.policy import (
    SandboxLimits,
    SandboxMode,
    SandboxPolicy,
)


@pytest.fixture
def env_policy() -> SandboxPolicy:
    return SandboxPolicy(mode=SandboxMode.ENV)


@pytest.fixture
def off_policy() -> SandboxPolicy:
    return SandboxPolicy(mode=SandboxMode.OFF)


def test_select_off_returns_null_launcher(off_policy: SandboxPolicy) -> None:
    launcher = select_launcher(off_policy)
    assert isinstance(launcher, NullLauncher)


def test_null_launcher_preserves_env_verbatim(off_policy: SandboxPolicy) -> None:
    launcher = NullLauncher(off_policy)
    parent = {"PATH": "/bin", "SECRET": "shh", "ORCHO_X": "1"}
    prep = launcher.prepare(cmd=["true"], cwd=None, parent_env=parent)
    assert prep.env == parent
    assert prep.preexec_fn is None
    assert prep.creationflags == 0
    assert prep.post_spawn is None
    assert prep.env_stripped_count == 0


@pytest.mark.skipif(
    platform.system().lower() == "windows",
    reason="EnvUnixLauncher is Unix-only; Windows uses EnvWindowsLauncher",
)
def test_env_unix_launcher_filters_env_and_attaches_preexec(env_policy: SandboxPolicy) -> None:
    launcher = select_launcher(env_policy)
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "ANTHROPIC_API_KEY": "sk-ant-…",
        "AWS_SECRET_ACCESS_KEY": "leak",
        "ORCHO_RUN_ID": "r1",
    }
    prep = launcher.prepare(cmd=["true"], cwd=None, parent_env=parent)
    assert "AWS_SECRET_ACCESS_KEY" not in prep.env
    assert "ANTHROPIC_API_KEY" in prep.env
    assert "ORCHO_RUN_ID" in prep.env
    assert prep.env_stripped_count == 1
    # preexec_fn is always set for env mode on Unix (process-group +
    # optional pdeathsig); even with no limits, setpgrp still runs.
    assert callable(prep.preexec_fn)
    assert prep.creationflags == 0
    assert prep.post_spawn is None


@pytest.mark.skipif(
    platform.system().lower() == "windows",
    reason="Unix-only spawn smoke",
)
def test_env_unix_launcher_actually_spawns_under_rlimit() -> None:
    """End-to-end: child should run with env scrubbed and exit cleanly."""
    import subprocess

    policy = SandboxPolicy(
        mode=SandboxMode.ENV,
        limits=SandboxLimits(cpu_seconds=60, open_files=512),
    )
    launcher = select_launcher(policy)
    parent_env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LEAKED": "no-go"}
    prep = launcher.prepare(cmd=[sys.executable, "-c", "import os; print(sorted(os.environ.keys()))"],
                            cwd=None, parent_env=parent_env)
    proc = subprocess.Popen(
        prep.cmd, env=prep.env, preexec_fn=prep.preexec_fn,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    keys = out.decode()
    assert "PATH" in keys
    assert "LEAKED" not in keys
