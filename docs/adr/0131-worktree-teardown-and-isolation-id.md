# ADR 0131 — Isolated-run external-resource lifecycle: `worktree_teardown` + `ORCHO_ISOLATION_ID`

Status: Accepted

## Context

Worktree isolation (ADR 0033) gives each run its own git worktree so the agent
never mutates the user's checkout. `worktree_bootstrap` lets a plugin prepare
that worktree (copy deps, `npm ci`, …) before the phases run.

This works for self-contained checkouts but breaks for projects whose
verification runs against **live external infrastructure** — most commonly a
Docker Compose stack (app container + test database + volumes). Two gaps:

1. **No per-worktree isolation namespace.** A Compose project name defaults to
   the directory basename. Two runs of the same repo in different worktrees
   either collide on one shared stack (fighting over the same test DB, ports,
   and memory — the observed `backend_test` collisions and OOM-137) or the gate
   cannot find the stack `worktree_bootstrap` brought up. `worktree_bootstrap`
   and gate commands already both inherit `ORCHO_RUN_ID` (it is set before the
   pipeline and is not stripped by `RUN_SCOPED_ENV_CHANNELS`), but `ORCHO_RUN_ID`
   is a run-identity implementation detail, not a documented isolation contract.

2. **No teardown hook.** `worktree_bootstrap` is a one-shot subprocess: it brings
   a stack up and exits. Nothing tears it down. `worktree_bootstrap` cannot
   self-arrange cleanup at run-end — there is no live process to trap, and on a
   terminal **halt** (e.g. an implement-attestation halt) no in-band agent step
   ever runs. So a per-worktree Compose stack leaks its containers and volumes
   exactly on the abnormal exits where cleanup matters most.

The practical outcome is that Dockerized projects are pushed to
`--no-worktree-isolation`, which defeats the point of isolation (parallel,
non-colliding runs). Isolation should *work* for these projects, not warn users
off it.

## Decision

Add a first-class external-resource lifecycle to isolated runs. Core owns the
namespace and the teardown lifecycle guarantee; the plugin owns the
provider-specific commands (`orcho-core` owns the protocol; plugins own provider
behavior).

### `ORCHO_ISOLATION_ID`

A stable, per-worktree identifier exported into the environment alongside
`ORCHO_RUN_ID`, and — like `ORCHO_RUN_ID` — **not** stripped from gate command
environments (`RUN_SCOPED_ENV_CHANNELS`). v1 isolation is per-run (ADR 0033), so
the value is run-scoped; the dedicated name is the documented contract a project
keys external-resource isolation on, decoupled from run-identity plumbing.

Documented recipe: a Compose project sets
`COMPOSE_PROJECT_NAME=orcho_${ORCHO_ISOLATION_ID}` and binds ephemeral host ports
(or none) for its test stack, in both `worktree_bootstrap` (bring up) and its
verification gate commands (run against the same stack).

### `worktree_teardown`

A new plugin key symmetric to `worktree_bootstrap` — the same step shapes
(`run` / `shell` / `python`), declared as what to clean up:

```python
PLUGIN = {
    "worktree_bootstrap": [{"run": ["docker", "compose", "up", "-d", "--wait"]}],
    "worktree_teardown":  [{"run": ["docker", "compose", "down", "-v"]}],
}
```

The engine guarantees **when**: `run_worktree_teardown` is invoked at run
finalization, in the worktree cwd, immediately before the git worktree is
released, and only for a **terminal** run (`done` / `halted` / `failed`). A run
that **pauses** (`awaiting_phase_handoff`, rc=4) retains its worktree for resume
and MUST NOT tear down — its stack stays up for the resumed run. Teardown is
**best-effort**: a failing teardown step is recorded and surfaced but never
raises, because the run is already terminal and cleanup failure must not mask the
run's real outcome.

Teardown fires at run-terminal even though `teardown_worktree(retain=True)`
retains the worktree *directory* for `orcho gc` (7-day retention, ADR 0033): the
retained directory is for post-mortem/diff inspection, which does not need the
external stack running.

## Consequences

- Dockerized projects run under worktree isolation without collision: each
  worktree gets its own namespaced stack, brought up in bootstrap and torn down
  at run-terminal. Parallel isolated runs on one repo stop fighting over a shared
  test DB.
- Core stays provider-neutral: it owns `ORCHO_ISOLATION_ID` and the teardown
  lifecycle guarantee; the plugin owns `docker compose` (or any other external
  resource) commands.
- Teardown is off-by-default (empty key) and additive; existing plugins are
  unaffected.
- Not covered here: per-phase isolation (still v1-rejected, ADR 0033) and a
  teardown at `orcho gc` time — for ephemeral runtime infra, run-terminal is the
  correct teardown point; the retained directory needs no live stack.
