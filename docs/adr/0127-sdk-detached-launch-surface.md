# ADR 0127 — SDK detached-launch surface: a framework-neutral run-launch primitive

- **Status:** Proposed
- **Date:** 2026-07-05
- **Deciders:** project owner
- **Related:**
  - [ADR 0021](0021-public-sdk-boundary.md) — the public SDK
    boundary: typed dataclasses, no `print` / `sys.exit` / env mutation
    in SDK APIs
  - [ADR 0033](0033-worktree-foundation.md) — worktree isolation
    (GWT-1); a launched run's `project_dir` must be a real git repo
  - [ADR 0120](0120-unattended-no-interactive-phase-handoffs.md) —
    non-interactive phase handoffs; the pause a detached run parks on and
    that `resume_run` continues past

## Context

### Two launch paths, neither reusable

Today there are two ways to start an Orcho pipeline run, and neither can
be embedded by a new caller without dragging in machinery it does not
want:

1. **Blocking, in-process.** `pipeline.project_orchestrator.run_pipeline`
   (and the `sdk.run_control.service` start path over it) runs the whole
   pipeline synchronously in the caller's process. It returns only when
   the run reaches a terminal or paused status. A caller that wants to
   start a run and keep doing other work — a dashboard, a supervisor, a
   TUI event loop — cannot use it without a thread or a process of its
   own, and it offers no handle to signal or cancel the work once
   started.

2. **Detached, but asyncio- and MCP-bound.** The MCP server's
   `RunsSupervisor` (`orcho-mcp`, `supervisor/{spawn,resume,cancel,
   state,paths,process}.py`) already spawns the orchestrator as a
   detached subprocess in its own session, persists an
   `mcp_supervisor.json` state file, and cancels via `os.killpg`. But
   that surface is welded to `asyncio` (per-project `asyncio.Lock`,
   `asyncio.create_task` reaping) and lives in the MCP package. Its
   state file is named `mcp_supervisor.json` and its in-memory handle
   carries a live `Popen`. Any other embedder — a TUI, a plain CLI
   wrapper, a test harness — would have to import the MCP server and an
   event loop to get detached-launch mechanics that are, at their core,
   provider- and framework-neutral.

The detached spawn mechanics (mint a run id, build the orchestrator
argv, launch a session leader so `killpg` reaches the whole tree,
persist the spawn facts, signal the process group to cancel) are pure
core mechanism. The concurrency policy layered on top of them — locks,
background reaping, capacity limits, client notification — is embedder
policy. The two are currently fused in a place only one embedder can
reach.

This mirrors the boundary the workspace already draws elsewhere:
`orcho-core` owns the protocol and the durable artifact; downstream
packages own their behavior. The launch mechanism belongs in core; the
event loop and the run registry belong to whoever embeds it.

## Decision

Add a synchronous, framework-neutral launch primitive to `orcho-core` at
`sdk/run_control/launch.py`. It is neutral **by construction**: it imports
no `asyncio`, no terminal-UI framework, and nothing from the MCP package.
A unit guard (`tests/unit/sdk/run_control/test_launch_neutrality.py`)
asserts this via AST so the boundary cannot silently rot.

### Public surface

Four frozen dataclasses and three functions, exported from
`sdk.run_control`:

- **`LaunchSpec`** — an immutable description of a run to launch:
  `project_dir` (resolved once to an absolute path so the subprocess
  `cwd` and the `--project` argv agree — the workspace-relative doubling
  trap), `task` / `task_file`, `workspace` / `runs_dir`, `profile`,
  `mock`, `max_rounds`, `mock_validate_plan_reject`, `output_mode`,
  `session_mode`, the `attach*` inputs, and `from_run_plan`.
- **`LaunchedRun`** — the framework-neutral, serialisable record of a
  launched run: `run_id`, `pid`, `pgid`, `run_dir`, `project_dir`,
  `command`, `started_at`, `mock`, `output_mode`, `status`. It carries
  **no** `Popen` and no event-loop object — it can be written to disk,
  handed across a process boundary, or wrapped by any embedder.
- **`LaunchResult`** — `run: LaunchedRun` plus `popen:
  subprocess.Popen`. The live process object rides here, and only here,
  for owners that reap through `Popen.wait()`.
- **`CancelResult`** — `run_id` plus a `status` string
  (`signal_sent(<mode>)`, `already_done`, or `already_dead`).

- **`launch_run(spec, *, run_id=None) -> LaunchResult`** — mints a run id
  when not supplied, builds the orchestrator argv, and launches
  `python -m pipeline.project_orchestrator` as a detached session leader
  (`start_new_session=True`, so `pgid == pid` and `killpg` reaches the
  whole tree). The active-profile / `auto-detect` selector env handling
  matches the reference exactly (the `auto-detect` token routes only
  through argv and drops any inherited `ORCHO_PIPELINE`). Raises
  `LaunchError` on a spawn failure (`OSError` / `FileNotFoundError`) or an
  invalid `project_dir` / `task_file`.
- **`resume_run(run_id, *, runs_dir=None, profile=None) -> LaunchResult`**
  — continues an existing run from its checkpoint via the orchestrator's
  `--resume` flag. It deliberately omits `--task` so core classifies the
  spawn as a `CHECKPOINT` continuation (re-using the existing run dir)
  rather than a follow-up. It inherits `mock` / `output_mode` from the
  persisted state so a paused mock run does not silently switch
  providers, and resolves the profile explicit → `meta.profile` →
  `"feature"`. A missing run / state / recorded task raises
  `RunNotFound`.
- **`cancel_run(run_id, *, runs_dir=None, mode='graceful') -> CancelResult`**
  — sends `SIGTERM` (`graceful`) or `SIGKILL` (`hard`) to the run's
  process group. It is **state-file driven**: it reads `pid` / `pgid`
  back from the state file on disk, so it works even for an embedder that
  never held (or has lost) the live `Popen`. It is idempotent — a
  terminal `meta.json` returns `already_done`, a dead pid returns
  `already_dead` (and settles the state file), and a `ProcessLookupError`
  on `killpg` returns `already_dead`. It never raises on a dead or
  finished run; only `RunNotFound` (missing state) and `ValueError` (bad
  `mode`) propagate.

`LaunchError(OrchoError)` is added to `sdk/errors.py` with `exit_code=1`
for the spawn-failure case; the missing-run case on resume / cancel reuses
the existing `RunNotFound` rather than minting a parallel error.

### The `run_supervisor.json` durable contract

`launch_run` / `resume_run` write a **fresh, framework-neutral** state
file at `<run_dir>/run_supervisor.json`. It is deliberately **not**
`mcp_supervisor.json`: this is a new artifact with no MCP coupling and no
back-compat shim (per the No Backcompat Ceremony rule — internal plumbing
does not carry a dual-path migration). Keys:

| Key | Meaning |
|---|---|
| `run_id` | the run id (equals `run_dir` name) |
| `pid` | the detached process pid |
| `pgid` | the process-group id (equals `pid`; session leader) |
| `command` | the full argv the run was spawned with |
| `project_dir` | the resolved absolute project directory |
| `started_at` | ISO-8601 UTC spawn timestamp |
| `status` | `running` at spawn; settled to a terminal/interrupted value on the orphan-cancel path |
| `mock` | whether the run was spawned `--mock` |
| `output_mode` | the transcript output mode |

On the orphan-cancel path (a dead pid behind a non-terminal `meta.json`)
`cancel_run` rewrites the file with a settled `status` and a
`halt_reason` of `interrupted_orphan`, so a subsequent probe sees a
finished run.

**Terminal-status semantics.** Whether a run "finished" is decided from
the pipeline's own `meta.json:status`, not from the state file — meta is
the authoritative completion signal. The terminal set is
`{done, failed, halted, interrupted, orphaned}`. `awaiting_phase_handoff`
is **not** terminal: it is a *paused* state (the run parked on a phase
handoff per ADR 0120), so cancelling a paused run is a legitimate action
and `resume_run` is the way to continue past it. This matches the
reference supervisor's `META_TERMINAL_STATUSES` exactly.

`mcp_supervisor.json` and the MCP `RunsSupervisor` are unchanged by this
ADR and remain the MCP server's own artifact until the adaptation work
(C1, below) folds the MCP path onto this primitive.

### Responsibility split — core owns spawn, the embedder owns concurrency

The dividing line is explicit and load-bearing:

- **Core (`launch.py`) owns the spawn mechanism**: run-id minting, argv
  construction, single-resolve of `project_dir`, the detached
  session-leader launch, the neutral `run_supervisor.json`, and the
  signal-the-process-group cancel. Nothing here knows about an event
  loop.
- **The embedder owns concurrency and lifecycle policy.** There is no
  lock, no background reaping, no capacity gate, and no client
  notification in `launch.py`. Specifically:
  - **Locks / serialization** (e.g. serialising concurrent spawns on a
    shared `project_dir`) are the embedder's policy.
  - **Reaping and notification** — waiting on the returned `Popen`,
    updating a registry, and telling a client the run ended — are the
    embedder's. Owners that reap do so via `LaunchResult.popen.wait()`.
  - **Capacity limits** (a max-concurrent-runs gate) are the embedder's.

This keeps the primitive a mechanism, not a policy: an embedder composes
its own locks, reaper, and limits around these calls without core taking
a position on any of them.

## Consequences

- A single neutral primitive can be wrapped by any embedder without
  pulling in an event loop or a terminal framework. The AST neutrality
  guard makes the boundary enforceable in CI.
- The public wire surface gains a new durable artifact
  (`run_supervisor.json`) with a documented, stable key set and
  terminal-status contract.
- Two consumers still run their own launch code and should later be
  reduced to thin wrappers over this primitive. That reduction is
  **out of scope for this ADR** and is tracked as separate work:
  - **C1 — MCP adaptation.** Rewrite the MCP `RunsSupervisor`
    spawn / resume / cancel bodies as thin `asyncio` wrappers over
    `launch_run` / `resume_run` / `cancel_run`, leaving only the
    embedder policy (locks, `_reap`, `_max_runs`, MCP-error mapping) in
    the MCP package. The success shape is *deletion of duplicated
    mechanism*, not re-plumbing. Until C1 lands, `mcp_supervisor.json`
    and the MCP supervisor stay as they are.
  - **C2 / C3 — TUI consumption.** Have the TUI drive detached runs
    through this primitive rather than any bespoke launch path. Also a
    later task, and likewise expected to remove duplicate launch code
    rather than add a parallel one.

This ADR is append-only; any future change to the launch surface or the
`run_supervisor.json` contract supersedes it with a new ADR rather than
editing this one. The delivering commit references ADR 0127.
