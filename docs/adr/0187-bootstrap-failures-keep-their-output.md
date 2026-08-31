# ADR 0187 — A failed worktree bootstrap step keeps its output

- **Status:** Accepted
- **Date:** 2026-08-31
- **Supersedes:** the "stdout/stderr is not persisted" consequence of
  [ADR 0074](0074-worktree-bootstrap.md) (that ADR otherwise stands)
- **Related:** [ADR 0090](0090-require-gate-no-silent-green.md),
  [ADR 0104](0104-setup-preflight-terminal-state-projection.md),
  [ADR 0131](0131-worktree-teardown-and-isolation-id.md)

## Context

ADR 0074 deliberately kept bootstrap subprocess output out of the durable
record: "Subprocess stdout/stderr is not persisted in the session result,
avoiding a new accidental secret-capture surface. The error stores the failing
step and exit code."

That trade was tested in production and lost. An attempt of run
`20260831_170837_de791f` halted `worktree_bootstrap_failed` on step 2
(`npx nuxt prepare`, after a `{"copy": "node_modules"}` step). What the run dir
retained was:

- `runner.log`: one line — `worktree_bootstrap run step 2 failed with exit code 1`
- `output.log`: empty
- `meta.json`: `{"status": "failed", "error": "…failed with exit code 1"}`

The exit code is not a diagnosis. The step's own stderr was the diagnosis, it
existed in memory inside `subprocess.run`, and it was discarded. The halt was
only reconstructible by re-running the command by hand against a worktree that
the halt itself had already made unreachable.

The secret-capture concern is real but was priced wrong. Bootstrap steps are
project-declared commands of exactly the same class as declared verification
commands (`npm ci`, `composer install`, `nuxt prepare`) — and verification
command receipts **already** persist `stdout_tail` / `stderr_tail` into the same
run dir under `verification_command_receipts/`. Bootstrap was the one command
class in the engine whose failure output vanished, so the policy bought no
containment while costing every bootstrap halt its diagnosis.

## Decision

**A failing bootstrap step's captured output is persisted in the run dir.**

`WorktreeBootstrapError` carries a typed `failure` record — index, action,
argv/command, cwd, exit code, failure reason (`exit_code`,
`command_not_found`, `timeout`), and the stdout/stderr tails. The isolation
setup that owns the halt writes it to
`<run_dir>/worktree_bootstrap/step-<NNNN>-<action>.json`, embeds it in the
durable session as `worktree_bootstrap.failed_step`, and quotes it on the
terminal (so it also reaches `runner.log`).

Scope and containment:

- **Failures only.** A successful step records what it did, not what it said.
- **Bounded.** Each stream is capped at `OUTPUT_TAIL_CHARS` (8000), the same
  shape as verification receipt tails: enough to diagnose, bounded so a runaway
  build log cannot grow the durable run record without limit.
- **Same trust boundary as receipts.** The evidence lands in the run dir
  alongside `verification_command_receipts/`, which already holds command
  output tails; the bootstrap directory is a sibling, not a new surface.
- **Never fatal.** Persisting evidence is best-effort: a write failure must not
  replace the bootstrap failure the operator actually needs to see. A run
  without an output dir still gets the record in the session and on the
  terminal.

## Consequences

- `worktree_bootstrap_failed` becomes diagnosable from the run dir alone, which
  is the property `orcho run diagnose` and every post-mortem depend on.
- Bootstrap step output tails are now durable for failing steps. Anything a
  project's bootstrap command prints on failure — including anything it should
  not print — is retained under the run dir. This is the accepted cost, and it
  is the same exposure declared verification commands already carry.
- `meta.json` grows by at most two bounded tails per failed run, and only on the
  bootstrap-halt path.
- ADR 0074's other decisions (portable action contract, core owns the lifecycle
  point, plugins own the commands, failures halt before agent phases) are
  unchanged.
