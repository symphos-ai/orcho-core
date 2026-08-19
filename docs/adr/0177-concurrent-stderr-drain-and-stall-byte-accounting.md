# ADR 0177 — Concurrent stderr drain and stall byte accounting

Status: Accepted

Extends [ADR 0103](0103-stalled-command-diagnostics-and-recovery.md).

## Context

The streaming runner formerly collected stderr only after stdout processing and
child settlement. A noisy stderr pipe can fill before a child emits its next
stdout line, preventing progress and making the stdout idle watchdog report a
misleading stall. Operators also could not tell how much output each stream had
actually produced when a stall diagnostic was written.

## Decision

`agents/stream_stderr.py` owns a focused concurrent reader. It starts as soon
as `_stream_run` has spawned a child, drains raw stderr on its own thread, and
retains only the final 4 MiB of raw bytes. It records all bytes read and all
dropped bytes under a lock. After child settlement, the retained tail is
decoded once and, when bounded, gains exactly one dropped-byte note. Stderr is
never merged into stdout, preserving runtime adapters' structured stdout
protocol.

`agents/stream.py` counts raw stdout bytes at the transport boundary, including
the final drain, and snapshots that count with the reader's thread-safe stderr
total whenever it creates a `StalledCommand`. Stderr activity is diagnostic
only: it does not reset, alter, or classify the stdout-based idle watchdog.

`stdout_bytes_read` and `stderr_bytes_read` are additive optional fields on the
bounded carrier. New stream-generated terminal and live events, terminal
failure records, finalization snapshots, evidence records, and SDK
`StalledCommandRecovery` projections carry the snapshot. Old persisted events
and failure records may omit either field; projectors return `None` rather than
rejecting them. `AgentCommandStalledError` renders both values for operator
diagnosis.

MCP needs no dedicated schema change: it relays event and evidence payloads by
the existing generic pass-through contract, so these additive fields remain
visible without a new MCP-specific projection.

## Consequences

- A full stderr pipe cannot block a child from reaching slow stdout or a normal
  exit merely because stdout is streamed separately.
- Diagnostic counts describe raw bytes observed, not the retained stderr tail;
  truncation never lowers `stderr_bytes_read`.
- The existing idle policy remains intentionally stdout-only, including its
  `silent_child_command` versus `output_inactivity` classification.
- Persisted run-state remains backward-readable because the new fields are
  additive and optional.
