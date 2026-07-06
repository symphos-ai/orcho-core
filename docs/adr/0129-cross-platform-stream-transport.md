# ADR 0129 — Cross-platform streamed-subprocess transport

- **Status:** Proposed
- **Date:** 2026-07-06
- **Deciders:** project owner
- **Related:**
  - [ADR 0034](0034-sandbox-isolation-layered.md) — layered sandbox
    isolation; the launcher's `preexec_fn` / `creationflags` /
    `post_spawn` seam that `_spawn_with_sandbox` applies, and the
    process-group kill semantics the streamer preserves
  - [ADR 0123](0123-stream-first-session-capture.md) — stream-first
    session capture that rides on the same `_stream_run` output path

## Context

The agent runtime streams every child process (Claude, Codex, Gemini, and
`--mock`) through `agents.stream._stream_run`. Historically that function
allocated a pseudo-terminal with `pty.openpty()`, wired the child's stdin and
stdout to the PTY slave, and read the master end with `select` + `os.read`. The
PTY gives agent CLIs a real TTY so they line-buffer and emit live output.

`pty` is POSIX-only: CPython ships no `pty` module on native Windows. The import
was unconditional at module load (`import pty` in `agents/stream.py`), and that
module is on the import path of `agents/__init__.py`. So `import agents` — and
therefore `orcho --help`, `orcho run`, and every CLI/pipeline entry point —
raised `ModuleNotFoundError` on native Windows before doing any work. The
project advertised native Windows support (README, the `Microsoft :: Windows`
classifier, `docs/expert/05_windows.md`), but nothing exercised Windows in CI,
so the contradiction went unnoticed. `select` compounds the problem: on Windows
it cannot wait on pipe handles, only sockets, so even a lazy `pty` import would
not make the existing read loop work there.

## Decision

Introduce a transport seam that owns the platform-specific byte source, and keep
`_stream_run` transport-agnostic (line buffering, log fan-out, masking, and the
idle/hard watchdogs stay in `agents/stream.py`).

`agents/stream_transport.py` defines `StreamTransport` and two implementations:

- **`PtyTransport` (POSIX).** Allocates a pseudo-terminal, wires the child's
  stdin/stdout to the slave, and reads the master with `select` + `os.read` —
  the historical behaviour verbatim. The `pty` import is lazy, inside the
  constructor, so it is never touched on a host without it. PTY-pool exhaustion
  still surfaces as an `OSError` at construction and is rendered by
  `agents.pty_diagnostics` instead of a traceback.
- **`PipeTransport` (native Windows, and any host without `os.openpty`).** The
  child's stdout is an ordinary pipe drained by a daemon reader thread into a
  queue, because `select` cannot wait on pipe handles on Windows. The parent's
  `read()` waits on the queue with a timeout, preserving the same
  `bytes` / `None` (no output yet) / `b""` (EOF) contract as the PTY path. The
  child receives no controlling terminal: its stdout is a plain pipe, so a CLI
  probing `sys.stdout.isatty()` sees a non-interactive stream, exactly as under
  any pipe. (stdin is `DEVNULL`; `sys.stdin.isatty()` is not a portable signal —
  on Windows the `NUL` device behind `DEVNULL` is itself a character device.)

`select_transport()` picks `PtyTransport` when `os.openpty` is available and
`PipeTransport` otherwise. `_spawn_with_sandbox` now takes the transport's stdio
wiring (`stdin`/`stdout` kwargs) rather than a raw slave fd; `stderr` remains a
pipe read after exit on both paths. The ADR 0034 sandbox launcher seam
(`preexec_fn`, `creationflags`, `post_spawn`, Job Object ownership) is unchanged.

A bounded post-exit drain covers a race unique to the threaded pipe path: the
read loop can stop on `proc.poll()` before the reader thread has surfaced its
final chunks and EOF. After the loop, the streamer pulls any remaining bytes
through the same line-processing path, bounded by a short grace that resets on
each chunk and ends immediately on a clean EOF.

`PipeTransport` is pure stdlib (`subprocess` + `threading` + `queue`) and runs
on POSIX too, so the Windows path is exercised on every CI runner, not only
`windows-latest`.

### What does not change

- POSIX behaviour is byte-for-byte the historical PTY path.
- The `_stream_run` signature, return tuple, filters, watchdogs, stall
  protocol, and sandbox kill semantics are untouched.
- Structured agent-log sections moved to `agents/stream_log.py` in the same
  change (a separate responsibility from subprocess streaming); this is a pure
  extraction with no behavioural change.

### CI proof

A `windows-latest` job imports the engine and CLI, runs the streaming transport
tests, and runs an end-to-end `orcho run --mock`. A `macos-latest` job runs the
full suite. The native-Windows claim is now backed by a green Windows job rather
than an unverified classifier.

## Consequences

- `import agents` succeeds on native Windows; the CLI and mock pipeline run
  there.
- On Windows, the agent child has no controlling terminal and its stdout is a
  plain pipe. CLIs that change output when `sys.stdout.isatty()` is false behave
  as they do under any non-interactive pipe. This is documented in
  `docs/expert/05_windows.md`.
- The threaded reader adds one daemon thread per streamed child on the pipe
  path; it exits on EOF and is joined briefly at teardown.
- Third-party runtime adapters and tests that constructed spawns directly should
  go through `select_transport()` / the transport's `popen_stdio()` rather than
  assuming a PTY.
- The `windows-latest` job surfaced a second latent blocker: `orcho run` prints
  emoji/box-drawing glyphs, and a legacy Windows console code page (cp1252)
  raises `UnicodeEncodeError` on them. `core/io/encoding.py:ensure_utf8_stdio`
  reconfigures the standard streams to UTF-8 when they are not already, and is
  called from each CLI entry point (`cli.orcho`, `pipeline.project.cli`,
  `pipeline.cross_project.cli`). It is a no-op on already-UTF-8 hosts.
