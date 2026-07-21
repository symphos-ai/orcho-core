# ADR 0147 — Run-scoped managed-command admission

- **Status:** Accepted
- **Date:** 2026-07-21
- **Supersedes:** the nested-command non-goal in ADR 0143
- **Related:** ADR 0143

## Context

ADR 0143 made retry admission authoritative for the provider CLI process that
Orcho starts directly. It intentionally did not claim ownership of commands
started later by provider tools. That boundary is insufficient for a long
nested command: a tool can lose its handle or report wrapper completion while
the operating-system child is still live. Starting the same expensive command
again then creates overlapping work inside one phase.

Provider lifecycle messages cannot close this gap. Partial output, elapsed
time, disappearance of a tool handle, and a provider-level `completed` event
do not prove that the nested command exited.

## Decision

Orcho provides a run-scoped managed-command boundary:

```text
orcho command run --run-dir RUN --phase PHASE --cwd CHECKOUT -- COMMAND ARGS...
```

Before it creates the exact child, the boundary atomically creates a lease
keyed by run id, phase, resolved working directory, and complete argument
vector. An existing equivalent lease rejects admission. A fresh observer
projects such a lease as `unknown`, not `running`, because it does not own the
original child object and must not infer liveness from a recorded PID.

The admitted wrapper starts one exact child, inherits ordinary stdio, and
waits synchronously. Only the observed child exit writes a durable receipt
with its exit code. The receipt is written before the lease is removed. This
ordering may conservatively reject after a crash, but it cannot admit an
overlapping duplicate. Once exact terminal settlement removes the lease, a
later explicit repeat is allowed as a new attempt.

Cancellation is scoped to the exact child object held by the admitted wrapper.
There is no command-name scan, host-wide process discovery, blind delay, or PID
reconstruction. If cancellation cannot observe a terminal exit, the lease is
retained and later admission fails closed.

Write-phase prompts distinguish three cases:

1. configured broad verification gates remain engine-owned;
2. targeted and diff-scoped development checks run normally; and
3. other repo-wide or expected-long commands use the managed boundary.

The same prompt part is carried by whole-plan implement, subtask-DAG implement,
and repair work. A refusal is a blocker to report, never permission to bypass
the boundary with a direct duplicate.

## Consequences

- ADR 0143 remains authoritative for top-level provider-process retries.
- Nested long-command admission now has durable terminal truth independent of
  provider tool handles.
- A stale lease is intentionally fail-closed. Operator recovery for abandoned
  leases is a separate, explicit action; automatic process-name recovery is
  prohibited.
- This is a local run protocol, not a general process supervisor and not a new
  remote-control wire contract.
