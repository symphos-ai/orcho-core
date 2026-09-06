"""Interruption settles the invocation before unwinding to its caller."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agents.owned_child import OwnedChildRegistry, OwnedChildState
from agents.stream import _stream_run
from agents.stream_transport import PipeTransport


@pytest.mark.parametrize("error", [SystemExit(143), KeyboardInterrupt(), RuntimeError("read failed")])
def test_transport_exception_cancels_owned_child(monkeypatch, error) -> None:
    class InterruptedTransport(PipeTransport):
        def read(self, timeout):
            raise error

    monkeypatch.setattr("agents.stream.select_transport", InterruptedTransport)
    owner = OwnedChildRegistry()
    with pytest.raises(type(error)) as caught:
        _stream_run(
            [sys.executable, "-c", "import time; time.sleep(4)"],
            owned_child_owner=owner,
        )

    assert caught.value is error
    assert owner.last_handle is not None
    observed = owner.poll(owner.last_handle)
    assert observed.state is OwnedChildState.EXITED
    assert observed.exit_code != 0, "interruption must cancel, not await natural exit"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM handler")
@pytest.mark.parametrize("sandbox", [False, True])
def test_sigterm_during_stream_preserves_interruption_and_stops_child(tmp_path, sandbox) -> None:
    code = '''
import os, signal, sys
from pathlib import Path
from agents.stream import _stream_run
from pipeline.project.interruption import install_interrupt_handlers
from pipeline.sandbox.policy import SandboxPolicy
run_dir = Path(sys.argv[1])
install_interrupt_handlers(run_dir, {"status": "running"})
def interrupt(line):
    os.kill(os.getpid(), signal.SIGTERM)
child = "import time; from pathlib import Path; print('ready', flush=True); time.sleep(4); Path('child-finished').touch()"
policy = None
if sys.argv[2] == "True":
    # A descendant inherits the pipes and the sandbox-owned process group.
    child = "import subprocess, sys; subprocess.run([sys.executable, '-c', " + repr(child) + "])"
    policy = SandboxPolicy()
_stream_run([sys.executable, "-c", child], cwd=str(run_dir), on_line=interrupt, sandbox_policy=policy)
(run_dir / "next-invocation").touch()
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(sandbox)],
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 143, result.stderr
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["status"] == "interrupted"
    assert meta["halt_reason"] == "signal:15"
    assert not (tmp_path / "child-finished").exists()
    assert not (tmp_path / "next-invocation").exists()
