# ADR 0179 — Bounded service process trees

Status: Accepted

Supersedes the platform-specific detached-launch and cancellation mechanics in
[ADR 0127](0127-sdk-detached-launch-surface.md). ADR 0127 remains historical;
this decision changes neither its public SDK types nor its original record.

## Context

A direct child exiting is not proof that a command is settled: a descendant can
retain inherited stdout or stderr handles indefinitely. Likewise, durable run
cancellation must own the recorded tree on both POSIX and Windows rather than
depending on a live `Popen` or a host-wide process-name search.

## Decision

`core.io.process_tree` is the sole platform boundary. It creates detached
POSIX sessions or Windows process groups, probes liveness, and owns group/tree
termination. Where pywin32 is available it assigns a Job Object; if Job Object
creation, assignment, or termination is unavailable, the adapter invokes
`taskkill /PID <pid> /T /F` and waits only for the remaining deadline.

`core.io.bounded_proc` is separate from that platform adapter. It starts pipe
readers before waiting, drains both streams without a pipe-buffer output cap,
and waits for both the direct process and reader EOF. On timeout it terminates
the owned tree, then spends a distinct `reap_budget` waiting for process and
reader settlement. Its typed timeout result retains partial stdout/stderr and
states whether that reap budget was exhausted. Reader threads left after the
budget are daemon threads, so an inherited handle cannot stall interpreter
shutdown. The wall-clock bound is timeout plus reap budget, including tree
termination and the second reap.

Detached SDK launches write an additive `process_tree` object in
`run_supervisor.json`:

```json
{"platform":"posix|windows","root_pid":123,"group_id":123,"group_owned":true}
```

The existing `pid` and `pgid` keys remain. Readers of old state files safely
project missing `process_tree` data from those keys. `cancel_run` delegates
liveness and termination to the adapter: POSIX retains graceful/hard group
signals; Windows asks a `CREATE_NEW_PROCESS_GROUP` tree to stop with
`CTRL_BREAK_EVENT` and falls back to the hard tree kill when no console is
shared. The adapter returns the mode it actually delivered, and
`signal_sent(<mode>)` reports that rather than the mode requested: a hard kill
announced as `graceful` would promise a checkpoint the pipeline never got to
write.

Liveness is a read-only question. `os.kill(pid, 0)` is not a probe on Windows —
for every signal except the two console events CPython calls
`TerminateProcess`, so the "probe" would kill the run it inspects. The adapter
uses `OpenProcess` + `GetExitCodeProcess` there, and an unusable probe reports
*alive*, which keeps the caller on the terminate path instead of skipping a
live tree.

Boundedness is not a licence to answer. `git_changed_file_records` keeps its
documented degrade-to-empty contract, but `has_uncommitted` and `git_diff_stat`
answer a question *about the working tree*, and "git could not be consulted" is
not the answer "clean": downstream that reads as "no file changes were
produced" and lets final acceptance approve an empty diff surface. Those two
raise on an unusable cwd, a missing binary, and a stalled git — bounded, but
never silently clean.

## Consequences

- Git and other bounded command callers receive a typed completion, spawn
  failure, or timeout result instead of hanging on inherited pipes.
- Callers of `has_uncommitted` / `git_diff_stat` must handle `OSError` /
  `TimeoutError` rather than reading a failure as a clean tree.
- Windows proof is a dedicated CI smoke slice for the real inherited-pipe
  timeout and recorded-tree cancel tests; it is not replaced by POSIX coverage.
- The hosted Windows result is deferred evidence until that configured
  `windows-smoke` job executes; this POSIX development environment cannot
  substitute a Windows result.
- This changes no public SDK dataclass/signature, MCP wire format, profile, or
  observability payload. `docs/sdk_schema.json` therefore remains unchanged.
