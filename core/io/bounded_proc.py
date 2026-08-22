"""A bounded Popen runner which owns both a child and its pipe readers."""
from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.io.process_tree import spawn_process, terminate_tree

Output = str | bytes


@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int
    stdout: Output
    stderr: Output


@dataclass(frozen=True, slots=True)
class SpawnFailure:
    error: str
    stdout: Output
    stderr: Output
    exception: OSError | ValueError | None = None


@dataclass(frozen=True, slots=True)
class TimedOut:
    stdout: Output
    stderr: Output
    returncode: int | None
    reap_exhausted: bool
    error: str = "command timed out"


CommandOutcome = Completed | SpawnFailure | TimedOut


def _reader(stream: object, sink: bytearray, done: threading.Event) -> None:
    try:
        # BufferedReader.read(n) is allowed to wait for *n* bytes.  read1
        # returns promptly with whatever a pipe has, which is essential when
        # a grandchild inherited the write end and will never produce EOF.
        read = getattr(stream, "read1", None) or stream.read  # type: ignore[attr-defined]
        while chunk := read(65536):
            sink.extend(chunk)
    finally:
        done.set()


def _convert(value: bytearray, *, text: bool, encoding: str, errors: str) -> Output:
    raw = bytes(value)
    return raw.decode(encoding, errors) if text else raw


def run_bounded(
    args: Sequence[str] | str,
    *,
    timeout_s: float,
    reap_budget_s: float = 1.0,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    input_data: str | bytes | None = None,
    text: bool = False,
    encoding: str = "utf-8",
    errors: str = "replace",
    shell: bool = False,
) -> CommandOutcome:
    """Run exactly ``args`` and return by timeout plus the reap budget.

    Pipe readers are daemon threads so an inherited pipe held by a surviving
    descendant cannot keep the interpreter alive after this function returns.
    """
    started = time.monotonic()
    deadline = started + max(0.0, timeout_s)
    stdout, stderr = bytearray(), bytearray()
    empty: Output = "" if text else b""
    try:
        tree = spawn_process(
            args, cwd=cwd, env=dict(env) if env is not None else None, shell=shell,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        return SpawnFailure(str(exc), empty, empty, exc)

    process = tree.process
    out_done, err_done = threading.Event(), threading.Event()
    threading.Thread(target=_reader, args=(process.stdout, stdout, out_done), daemon=True).start()
    threading.Thread(target=_reader, args=(process.stderr, stderr, err_done), daemon=True).start()
    if input_data is not None:
        payload = input_data.encode(encoding) if isinstance(input_data, str) else input_data
        def write_input() -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(payload)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=write_input, daemon=True).start()

    while time.monotonic() < deadline:
        if process.poll() is not None and out_done.is_set() and err_done.is_set():
            return Completed(process.returncode, _convert(stdout, text=text, encoding=encoding, errors=errors), _convert(stderr, text=text, encoding=encoding, errors=errors))
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    reap_deadline = time.monotonic() + max(0.0, reap_budget_s)
    terminate_tree(tree, deadline=reap_deadline)
    while time.monotonic() < reap_deadline:
        if process.poll() is not None and out_done.is_set() and err_done.is_set():
            return TimedOut(_convert(stdout, text=text, encoding=encoding, errors=errors), _convert(stderr, text=text, encoding=encoding, errors=errors), process.returncode, False)
        time.sleep(min(0.01, max(0.0, reap_deadline - time.monotonic())))
    return TimedOut(_convert(stdout, text=text, encoding=encoding, errors=errors), _convert(stderr, text=text, encoding=encoding, errors=errors), process.poll(), not (out_done.is_set() and err_done.is_set()))


__all__ = ["CommandOutcome", "Completed", "SpawnFailure", "TimedOut", "run_bounded"]
