"""
agents/stream.py — PTY-based subprocess streaming + optional live log.

Extracted from agents.py.

Public surface:
    set_agent_log(path)        — redirect live stdout to a tail-able file
    _stream_run(cmd, ...)      — run *cmd* over a PTY; returns (stdout, rc, stderr, dur)

The leading underscore on _stream_run is preserved for backward compat with
existing callers (tests patch ``agents._stream_run``).

Sandbox integration (ADR 0034): when a ``sandbox_policy`` is passed in,
the streamer uses :func:`pipeline.sandbox.select_launcher` to compute
env + preexec_fn + creationflags before ``Popen`` and applies a
:class:`pipeline.sandbox.TokenMasker` to log / echo / stderr output.
Returned stdout stays raw unless a runtime opts into ``return_filter`` for
its own retained protocol buffer.
"""

import contextlib
import errno
import os
import pty
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from agents.pty_diagnostics import (
    is_pty_exhaustion,
    render_pty_exhaustion_diagnostic,
)

if TYPE_CHECKING:
    from agents.stall_protocol import StallDiagnosticSink
    from pipeline.sandbox import SandboxPolicy, TokenMasker


class StreamAbort(RuntimeError):
    """Raised by an ``on_line`` callback to abort the running subprocess.

    This is intentionally narrow: ordinary parser failures are still swallowed,
    but safety guards can stop a write-capable agent before a dangerous tool
    call continues.
    """


# ---- Live-output log (optional) --------------------------------------------
_agent_log: Path | None = None
_stdout_echo: bool = False


def set_agent_log(path: Path | None) -> None:
    """Point streaming output to *path*. Pass None to disable."""
    global _agent_log
    _agent_log = path


def set_stdout_echo(enabled: bool) -> None:
    """Echo streamed agent output to stdout in addition to ``output.log``."""
    global _stdout_echo
    _stdout_echo = bool(enabled)


def write_agent_log_section(
    label: str,
    content: str = "",
    *,
    duration_s: float | None = None,
    label_codes: tuple[str, ...] = (),
    content_key_codes: tuple[str, ...] = (),
    separator_codes: tuple[str, ...] = (),
    exit_codes: tuple[str, ...] = (),
) -> None:
    """Append a structured diagnostic section to live agent output.

    Runtime subprocess output already streams through this module, but
    orchestrator-owned milestones (for example "subtask X started") do not
    come from a child process. This helper writes those milestones to the same
    tail-able log and optional stdout echo without pretending they are model
    output.
    """
    echo_payload = _render_agent_log_section(
        label,
        content,
        duration_s=duration_s,
        label_codes=label_codes,
        content_key_codes=content_key_codes,
        separator_codes=separator_codes,
        exit_codes=exit_codes,
        color=None,
    )
    file_payload = _render_agent_log_section(
        label,
        content,
        duration_s=duration_s,
        label_codes=(),
        content_key_codes=(),
        separator_codes=(),
        exit_codes=(),
        color=False,
    )
    _echo_stdout(echo_payload)
    if _agent_log is None:
        return
    try:
        _agent_log.parent.mkdir(parents=True, exist_ok=True)
        with _agent_log.open("a", encoding="utf-8", buffering=1) as fh:
            fh.write(file_payload)
            fh.flush()
    except Exception:
        pass


def append_agent_log_section(label: str, content: str = "") -> None:
    """Append a plain structured section to ``output.log`` without stdout echo.

    Some orchestrator-owned milestones already render directly to the terminal
    via local presentation helpers. They still need durable parity in the
    tail-able operator log, but routing them through ``write_agent_log_section``
    would double-print when stdout echo is enabled. This helper writes the same
    section shape to the configured agent log only.
    """
    if _agent_log is None:
        return
    payload = _render_agent_log_section(
        label,
        content,
        duration_s=None,
        label_codes=(),
        content_key_codes=(),
        separator_codes=(),
        exit_codes=(),
        color=False,
    )
    try:
        _agent_log.parent.mkdir(parents=True, exist_ok=True)
        with _agent_log.open("a", encoding="utf-8", buffering=1) as fh:
            fh.write(payload)
            fh.flush()
    except Exception:
        pass


def _render_agent_log_section(
    label: str,
    content: str,
    *,
    duration_s: float | None,
    label_codes: tuple[str, ...],
    content_key_codes: tuple[str, ...],
    separator_codes: tuple[str, ...],
    exit_codes: tuple[str, ...],
    color: bool | None,
) -> str:
    from core.io.ansi import paint

    sep = paint("-" * 60, *separator_codes, color=color, stream=sys.stdout)
    label_text = paint(label, *label_codes, color=color, stream=sys.stdout)
    header = f"\n{sep}\n{label_text}\n{sep}\n"
    body = ""
    if content:
        body = _paint_section_content_keys(
            content if content.endswith("\n") else f"{content}\n",
            codes=content_key_codes,
            color=color,
        )
    exit_line = ""
    if duration_s is not None:
        exit_text = f"[EXIT code=0 duration={duration_s:.2f}s]"
        exit_line = f"{paint(exit_text, *exit_codes, color=color, stream=sys.stdout)}\n"
    return f"{header}{body}{exit_line}"


def _paint_section_content_keys(
    content: str,
    *,
    codes: tuple[str, ...],
    color: bool | None,
) -> str:
    if not codes:
        return content
    from core.io.ansi import paint

    rendered: list[str] = []
    for line in content.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        key, sep, rest = body.partition(":")
        if sep and key and all(ch.isalnum() or ch == "_" for ch in key):
            rendered.append(
                f"{paint(key, *codes, color=color, stream=sys.stdout)}:"
                f"{rest}{newline}"
            )
        else:
            rendered.append(line)
    return "".join(rendered)


def _echo_stdout(text: str) -> None:
    global _stdout_echo
    if not _stdout_echo:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # Agent logging is diagnostic; a closed stdout pipe must not fail the
        # pipeline or prevent output.log from receiving the same content.
        _stdout_echo = False


def _spawn_with_sandbox(
    cmd: list[str],
    cwd: str | None,
    slave_fd: int,
    sandbox_policy: "SandboxPolicy | None",
) -> tuple[subprocess.Popen, "TokenMasker | None", int, object | None]:
    """Construct the agent Popen, applying sandbox policy when provided.

    Returns ``(proc, masker, env_stripped_count, launcher)``. The
    launcher reference is returned so the caller can hold it alive
    for the duration of ``proc``. On Windows the launcher owns the
    Job Object handle and the kernel closes the job (killing the
    assigned process) as soon as the handle is garbage-collected.
    A ``None`` launcher means no sandbox was applied — the legacy
    pre-L1 spawn path.
    """
    if sandbox_policy is None or not sandbox_policy.isolation_active:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        return proc, None, 0, None

    # Lazy import: sandbox depends on stdlib only, but keeping the
    # import local lets stream.py stay loadable in stripped-down
    # test contexts that don't build pipeline.sandbox.
    from pipeline.sandbox import select_launcher
    from pipeline.sandbox.resolver import materialize_masker

    launcher = select_launcher(sandbox_policy)
    prepared = launcher.prepare(
        cmd=cmd, cwd=cwd, parent_env=dict(os.environ)
    )

    popen_kwargs = dict(
        cwd=cwd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=subprocess.PIPE,
        close_fds=True,
        env=prepared.env,
    )
    # preexec_fn is Unix-only; on Windows the launcher returns None.
    if prepared.preexec_fn is not None:
        popen_kwargs["preexec_fn"] = prepared.preexec_fn
    # creationflags is Windows-only; on Unix the launcher returns 0.
    if prepared.creationflags:
        popen_kwargs["creationflags"] = prepared.creationflags

    proc = subprocess.Popen(prepared.cmd, **popen_kwargs)

    # Windows: assign the live child to the Job Object. No-op on Unix
    # (post_spawn is None).
    if prepared.post_spawn is not None:
        prepared.post_spawn(proc)

    masker = materialize_masker(sandbox_policy)
    return proc, masker, prepared.env_stripped_count, launcher


def _kill_subprocess_tree(
    proc: subprocess.Popen, *, group_owned: bool,
) -> None:
    """Kill the agent subprocess plus any descendants it spawned.

    When the sandbox launcher placed the child in its own process
    group (``setpgrp()`` in ``preexec_fn`` on Unix), a plain
    ``proc.kill()`` only terminates the leader — grandchildren keep
    running. ADR 0034 commits to killing the whole subtree, so we
    SIGKILL the group instead. ``proc.kill()`` is used as a fallback
    when ``group_owned`` is False (NullLauncher / pre-L1 callers).

    The ``group_owned`` flag is **declarative** — the launcher
    requested ``setpgrp`` in ``preexec_fn``, but the preexec swallows
    ``OSError`` so the spawn never fails on that primitive. If
    ``setpgrp`` actually failed, the child inherits the parent's
    process group. Sending SIGKILL to *that* group would tear down
    the orcho parent. Before issuing ``killpg``, we runtime-check
    that the child's effective pgid is distinct from ours and fall
    back to ``proc.kill()`` when they match — losing grandchild
    cleanup is a smaller failure than orchestrator suicide.

    On Windows the Job Object's ``KILL_ON_JOB_CLOSE`` flag handles
    descendant cleanup at the kernel level when the handle drops;
    ``proc.kill()`` here is a parallel safety net.
    """
    if not group_owned:
        with contextlib.suppress(OSError):
            proc.kill()
        return

    try:
        child_pgid = os.getpgid(proc.pid)
    except OSError:
        # Race: child already exited and was reaped. Nothing to do.
        return

    if child_pgid == os.getpgrp():
        # setpgrp failed inside preexec_fn (rare — EPERM in exotic
        # contexts). The child is in our own process group, so a
        # killpg here would SIGKILL orcho itself. Fall back to a
        # single-PID kill; grandchildren survive but the orchestrator
        # stays alive.
        with contextlib.suppress(OSError):
            proc.kill()
        return

    try:
        os.killpg(child_pgid, signal.SIGKILL)
    except OSError:
        # killpg can fail if the group is gone already (after natural
        # exit). proc.kill is idempotent and safe in that case.
        with contextlib.suppress(OSError):
            proc.kill()


def _own_child_pgid(proc: subprocess.Popen, *, group_owned: bool) -> int | None:
    """Return the run's OWN child process group id, or ``None``.

    Only meaningful when the launcher placed the child in its own group
    (``group_owned``). Returns ``None`` when not group-owned or when the child
    already exited — the carrier records ``process_group=None`` rather than
    guessing. This never inspects any process other than our own child.
    """
    if not group_owned:
        return None
    try:
        return os.getpgid(proc.pid)
    except OSError:
        return None


def _escalate_idle_stall(
    *,
    proc: subprocess.Popen,
    monitor: "object",
    group_owned: bool,
    elapsed_s: float,
    idle_timeout: int,
    log_fh,
    echo: Callable[[str], None],
) -> None:
    """Terminal idle-timeout escalation: scoped-kill → raise.

    The single auto-kill trigger. Builds the bounded terminal carrier scoped to
    our OWN child process group (no ``pgrep`` / no by-name matching), kills the
    child subtree via :func:`_kill_subprocess_tree`, reaps it, then raises
    :class:`agents.stall_protocol.AgentCommandStalledError`. Never writes the
    run session. Always raises — it never returns normally.

    It does NOT emit the terminal ``agent.command_stalled`` event: the run-level
    failure handler (``pipeline.project.run._record_phase_failure``) is the
    single authoritative emit-site, firing the terminal event next to
    ``session['failure']`` and ``run.end`` once the raised error propagates up.
    Emitting here too would double the record in the evidence bundle (F2). The
    non-terminal write-through path keeps emitting from the stream (via the
    sink) because no pipeline handler catches that — it never raises.
    """
    from agents.stall_protocol import AgentCommandStalledError

    stalled = monitor.idle_stall(  # type: ignore[attr-defined]
        elapsed_s=elapsed_s,
        process_group=_own_child_pgid(proc, group_owned=group_owned),
    )
    _kill_subprocess_tree(proc, group_owned=group_owned)
    with contextlib.suppress(Exception):
        proc.wait()
    reason = f"[IDLE TIMEOUT after {idle_timeout}s without output]"
    if log_fh:
        with contextlib.suppress(Exception):
            log_fh.write(f"\n{reason}\n")
            log_fh.flush()
    echo(f"\n{reason}\n")
    raise AgentCommandStalledError(stalled)


def _stream_run(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int | None = None,
    idle_timeout: int | None = None,
    label: str = "",
    on_line: Callable[[str], None] | None = None,
    stdout_filter: Callable[[str], str | None] | None = None,
    log_filter: Callable[[str], str | None] | None = None,
    return_filter: Callable[[str], str] | None = None,
    sandbox_policy: "SandboxPolicy | None" = None,
    stall_sink: "StallDiagnosticSink | None" = None,
    stall_phase: str = "",
) -> tuple[str, int, str, float]:
    """
    Run *cmd* via a PTY so the child sees a real terminal and flushes
    stdout line-by-line. Stream output to _agent_log (if set).

    Args:
        timeout: Optional hard deadline in seconds. Default None = no wall
                 clock cap; healthy autonomous agent runs can take hours.
        idle_timeout: Optional watchdog in seconds. When set, the child is
                      killed only if it stays alive while emitting no stdout
                      for this long. Any output chunk resets the timer, so a
                      long but active agent is not interrupted just because
                      it exceeds a fixed duration.
        on_line: Optional callback invoked for every complete line read
                 from the child. Used by providers (e.g. ClaudeAgent with
                 --output-format stream-json) to parse JSON events into
                 the event-store in real time. Exceptions in on_line are
                 swallowed so a misbehaving parser cannot kill the run.
        stdout_filter: Optional line-level formatter used only for live
                 stdout echo. ``output.log`` and the returned stdout are
                 separate surfaces. Returning None suppresses the line from
                 stdout.
        log_filter: Optional line-level formatter used only for ``output.log``.
                 When omitted, ``output.log`` keeps raw child stdout. The
                 returned stdout stays raw unless ``return_filter`` is set.
        return_filter: Optional line-level formatter used only for the
                 returned stdout retained by the runtime. Live stdout,
                 ``output.log``, and parser callbacks still see their
                 own surfaces.
        stall_sink: Optional provider-neutral diagnostic sink. When supplied,
                 a :class:`agents.stream_stall.StreamStallMonitor` is wired in:
                 unsafe free-text process polling detected on a stream line is
                 written through to the sink AT DETECTION (non-terminal, no
                 kill/raise), and the existing idle-timeout escalates with a
                 scoped kill + :class:`AgentCommandStalledError` (the terminal
                 ``agent.command_stalled`` event is emitted once by the
                 pipeline failure handler, not here). ``None`` (default) keeps the
                 historical behaviour: idle-timeout kills and returns normally,
                 with no stall escalation. The sink/monitor never touches the
                 run session.
        stall_phase: Phase label stamped onto the bounded ``StalledCommand``
                 carriers the monitor builds. Only meaningful with ``stall_sink``.

    Returns (stdout_text, returncode, stderr_text, duration_seconds).

    Raises:
        AgentCommandStalledError: only when ``stall_sink`` is wired AND the
            existing idle-timeout fires — the single auto-kill trigger.
    """
    _t0 = time.monotonic()
    if timeout is not None and timeout <= 0:
        timeout = None
    if idle_timeout is not None and idle_timeout <= 0:
        idle_timeout = None

    lines: list[str] = []
    returncode = -1
    stderr_text = ""
    termination_reason: str | None = None

    log_fh = None
    if _agent_log:
        _agent_log.parent.mkdir(parents=True, exist_ok=True)
        log_fh = _agent_log.open("a", encoding="utf-8", buffering=1)
    if label:
        # Stream framing — the agent's caller already printed the
        # ``-> {label}`` line, so this header opens a Transcript block
        # without repeating the command. ``output.log`` keeps the
        # legacy heavy ``--- label ---`` framing so post-mortem
        # tooling (less, grep -A) stays readable.
        from core.io.transcript import render_transcript_open
        sep = "-" * 60
        log_header = f"\n{sep}\n{label}\n{sep}\n"
        if log_fh:
            log_fh.write(log_header)
            log_fh.flush()
        _echo_stdout(render_transcript_open() + "\n")

    # Sandbox masker is materialised by _spawn_with_sandbox below.
    # We hold the reference here so the inner closures can apply it
    # without re-deriving it from policy per chunk.
    masker_ref: list[TokenMasker | None] = [None]

    def _mask(text: str) -> str:
        """Apply token masking when a masker is wired in.

        Called on every log write, echo, and stderr capture. Returned
        stdout (the value the caller uses to parse JSON events /
        session ids) stays raw — masking lives only on the
        display / persistence path per ADR 0034.
        """
        m = masker_ref[0]
        if m is None or not m.active:
            return text
        return m.mask(text)

    def _echo_agent_line(text: str) -> None:
        masked = _mask(text)
        if stdout_filter is None:
            from core.io.output_elision import elide_tool_result_for_transcript
            _echo_stdout(elide_tool_result_for_transcript(masked))
            return
        try:
            formatted = stdout_filter(masked)
        except Exception:
            formatted = masked
        if formatted:
            from core.io.output_elision import elide_tool_result_for_transcript
            formatted = elide_tool_result_for_transcript(formatted)
            _echo_stdout(formatted)

    def _format_log_line(text: str) -> str | None:
        masked = _mask(text)
        if log_filter is None:
            formatted = masked
        else:
            try:
                formatted = log_filter(masked)
            except Exception:
                formatted = masked
        if formatted is None:
            return None
        from core.io.output_elision import elide_tool_result_for_transcript
        return elide_tool_result_for_transcript(formatted)

    def _format_return_line(text: str) -> str:
        if return_filter is None:
            return text
        try:
            return return_filter(text)
        except Exception:
            return text

    def _write_log_line(text: str) -> None:
        if not log_fh:
            return
        formatted = _format_log_line(text)
        if not formatted:
            return
        log_fh.write(formatted)
        log_fh.flush()

    def _return_startup_failure(stderr_text: str) -> tuple[str, int, str, float]:
        duration = time.monotonic() - _t0
        returncode = 126
        if log_fh:
            log_fh.write(f"\n{stderr_text}\n")
            log_fh.write(f"\n[EXIT code={returncode} duration={duration:.1f}s]\n")
            log_fh.flush()
            log_fh.close()
        _echo_stdout(f"\n{stderr_text}\n")
        from core.io.transcript import render_result, render_transcript_close
        if label:
            _echo_stdout(render_transcript_close() + "\n")
        _echo_stdout(render_result(returncode, duration) + "\n")
        return "", returncode, stderr_text, duration

    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        if is_pty_exhaustion(exc):
            return _return_startup_failure(render_pty_exhaustion_diagnostic(exc))
        raise
    try:
        proc, masker, env_stripped, sandbox_launcher = _spawn_with_sandbox(
            cmd, cwd, slave_fd, sandbox_policy,
        )
        # Hold the launcher in this local for the duration of the
        # streamed process. On Windows the launcher owns the Job
        # Object handle; releasing it before ``proc.wait()`` returns
        # would close the job and (per
        # ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``) kill the assigned
        # child mid-flight. On Unix this reference is harmless.
        _sandbox_launcher_alive = sandbox_launcher  # noqa: F841 — keep alive
        masker_ref[0] = masker
        # Process-group ownership: the Unix env backend wraps the
        # child in its own pgrp via ``setpgrp`` in ``preexec_fn``.
        # When this flag is True, kill helpers send SIGKILL to the
        # whole group so grandchildren die too (ADR 0034).
        _group_owned = (
            sandbox_policy is not None
            and sandbox_policy.isolation_active
            and sys.platform != "win32"
        )
        if log_fh and sandbox_policy is not None and sandbox_policy.isolation_active:
            log_fh.write(
                f"[SANDBOX mode={sandbox_policy.mode.value} "
                f"env_stripped={env_stripped}]\n"
            )
            log_fh.flush()
        # Stall monitor (opt-in via ``stall_sink``): monitoring only. It drives
        # the non-terminal write-through and builds the terminal idle carrier;
        # the kill + raise stay in this function (the seam below).
        stall_monitor = None
        if stall_sink is not None:
            from agents.stream_stall import StreamStallMonitor
            stall_monitor = StreamStallMonitor(
                phase=stall_phase, sink=stall_sink,
                command_preview=" ".join(cmd),
            )
        os.close(slave_fd)  # parent doesn't need slave end

        def _handle_line_callback(line: str) -> bool:
            """Run ``on_line``; return False when it requested abort."""
            nonlocal termination_reason
            if on_line is None:
                return True
            try:
                on_line(line)
            except StreamAbort as exc:
                termination_reason = f"[ABORTED by stream guard: {exc}]"
                _kill_subprocess_tree(proc, group_owned=_group_owned)
                return False
            except Exception:
                # Parser failure must never kill the streaming loop. Errors
                # are visible via missing events.
                pass
            return True

        buf = b""
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        last_output_at = time.monotonic()
        abort_requested = False
        while True:
            now = time.monotonic()
            if deadline is not None:
                remaining = deadline - now
                if remaining <= 0:
                    termination_reason = f"[TIMEOUT after {timeout}s]"
                    _kill_subprocess_tree(proc, group_owned=_group_owned)
                    if log_fh:
                        log_fh.write(f"\n{termination_reason}\n")
                        log_fh.flush()
                    _echo_stdout(f"\n{termination_reason}\n")
                    break
                wait_for = min(remaining, 1.0)
            else:
                # No deadline: poll the pty for up to 1s, then loop. Liveness
                # comes from proc.poll() below, not a wall clock.
                wait_for = 1.0
            try:
                rlist, _, _ = select.select([master_fd], [], [], wait_for)
            except (ValueError, OSError):
                break  # master_fd closed (child exited)
            if not rlist:
                if proc.poll() is not None:
                    break
                if (
                    idle_timeout is not None
                    and time.monotonic() - last_output_at >= idle_timeout
                ):
                    termination_reason = (
                        f"[IDLE TIMEOUT after {idle_timeout}s without output]"
                    )
                    if stall_monitor is not None:
                        # Single auto-kill trigger: the existing idle-timeout.
                        # Build the terminal carrier scoped to OUR OWN child
                        # process group (no pgrep / no by-name matching), emit
                        # the terminal event, scoped-kill, reap, then escalate.
                        _escalate_idle_stall(
                            proc=proc,
                            monitor=stall_monitor,
                            group_owned=_group_owned,
                            elapsed_s=time.monotonic() - _t0,
                            idle_timeout=idle_timeout,
                            log_fh=log_fh,
                            echo=_echo_stdout,
                        )
                    _kill_subprocess_tree(proc, group_owned=_group_owned)
                    if log_fh:
                        log_fh.write(f"\n{termination_reason}\n")
                        log_fh.flush()
                    _echo_stdout(f"\n{termination_reason}\n")
                    break
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EBADF):
                    break  # EOF / child closed pty
                raise
            if not chunk:
                break
            last_output_at = time.monotonic()
            if stall_monitor is not None:
                # Output resets the idle window; record the new bytes so an
                # idle-timeout can classify silent-vs-inactive and carry a tail.
                stall_monitor.note_output(chunk.decode("utf-8", errors="replace"))
            buf += chunk
            text = buf.decode("utf-8", errors="replace")
            while "\n" in text:
                line, text = text.split("\n", 1)
                line += "\n"
                lines.append(_format_return_line(line))
                _write_log_line(line)
                _echo_agent_line(line)
                if stall_monitor is not None:
                    # Write-through non-terminal diagnostic AT DETECTION — never
                    # kills, never raises, never writes the session.
                    stall_monitor.inspect_line(
                        line, elapsed_s=time.monotonic() - _t0,
                    )
                if not _handle_line_callback(line):
                    abort_requested = True
                    break
            buf = text.encode("utf-8")  # keep incomplete line
            if abort_requested:
                break
        if abort_requested:
            proc.wait()

        if buf and not abort_requested:
            tail = buf.decode("utf-8", errors="replace")
            lines.append(_format_return_line(tail))
            _write_log_line(tail)
            _echo_agent_line(tail)
            _handle_line_callback(tail)

        proc.wait()
        returncode = proc.returncode
        stderr_raw = (
            proc.stderr.read().decode("utf-8", errors="replace")
            if proc.stderr else ""
        )
        # Mask stderr too — secrets that leak via stderr (e.g. the
        # provider CLI echoing an env-derived API key in an error
        # message) must not survive into the run transcript or the
        # caller's error path.
        stderr_text = _mask(stderr_raw)
        if termination_reason:
            stderr_text = f"{stderr_text.rstrip()}\n{termination_reason}".strip()
    finally:
        with contextlib.suppress(OSError):
            os.close(master_fd)
        duration = time.monotonic() - _t0
        # ``output.log`` keeps the legacy ``[EXIT …]`` line for
        # post-mortem grep tooling. The CLI gets the friendlier
        # ``Result  exit N  Ns`` line via the transcript renderer.
        log_exit = f"\n[EXIT code={returncode} duration={duration:.1f}s]\n"
        if log_fh:
            log_fh.write(log_exit)
            log_fh.flush()
            log_fh.close()
        from core.io.transcript import render_result, render_transcript_close
        if label:
            _echo_stdout(render_transcript_close() + "\n")
        _echo_stdout(render_result(returncode, duration) + "\n")

    return "".join(lines).strip(), returncode, stderr_text, duration
