# ADR 0143 — Provider-owned child lifecycle

- **Status:** Accepted
- **Date:** 2026-07-20
- **Extends:** ADR 0034
- **Related:** ADR 0103, ADR 0129

## Context

A provider invocation can fail after a stream or transport boundary becomes
ambiguous. Retrying by command name, host process discovery, or a blind delay
can create a second provider child while the first remains live. Those
mechanisms are also unable to establish ownership safely in the presence of
PID reuse or unrelated host activity.

The stream layer already receives the exact `Popen` it starts, and sandbox
launchers may own a process group or Windows Job Object that must remain alive
until that child is settled. That is the only authoritative lifecycle seam.

## Decision

Each built-in runtime owns an in-memory `OwnedChildRegistry` from construction.
After spawn, `_stream_run` registers the exact `Popen`, the sandbox launcher,
the confirmed group-ownership fact, and the existing `agent_call_id`. The
public immutable handle exposes only an opaque registration id, PID, and start
identity; it never contains argv, prompt, environment, stdout, stderr, or
secrets.

The registry privately retains the `Popen` and launcher and reports only three
observations:

- `running` — the exact registered child has not reached a terminal result;
- `exited(exit_code)` — a terminal result has been observed and memoized; and
- `unavailable` — ownership or observation cannot be confirmed safely.

`poll` is non-blocking. `wait` takes an explicit timeout and returns `running`
on timeout; it has no sleep loop or host-global discovery. The first terminal
observation performs the single settlement/reap and records the exit code;
later poll/wait calls return the memoized result.

Retry admission runs only after normal retry-budget classification and before a
retry event, backoff, or next spawn. No preceding handle and `exited` admit the
existing retry policy. `running` and `unavailable` fail closed and re-raise the
original typed runtime error, so no false retry/start event pair is created.

Cancellation validates the handle against the registry and signals only that
exact `Popen`, or a confirmed owned process group. POSIX group cancellation
checks that the group differs from Orcho's parent group before `killpg`; it
falls back to the registered PID otherwise. The registry retains the Windows
launcher/Job Object through settlement.

## Boundary and non-goals

Ownership is limited to the directly spawned `Popen` and, where the launcher
confirmed it, that child's process-group boundary. It does **not** provide
per-grandchild observation. A nested provider tool subprocess without an exact
PID/API is not an owned child and cannot be made observable by name matching;
that is a stop condition for any requirement demanding that granularity.

Handles and observations are in-memory invocation control state. They are not
written to run-state, evidence, SDK, MCP, or other persisted schemas. Existing
free-text `pgrep`/`pkill` detection remains a non-terminal safety diagnostic;
owned-handle poll/wait is structured lifecycle observation and never passes
through that text guard.

## Consequences

- Retry cannot overlap a live or unobservable prior provider child.
- Stream cancellation and final reaping share one scoped implementation.
- Runtime constructors remain side-effect free; constructing a registry does
  not resolve a provider binary or start a process.
- Operators retain existing stall diagnostics without conflating them with
  direct-Popen lifecycle observation.
