# ADR 0033 — Git Worktree Foundation (GWT-1)

- **Status:** Accepted (PR1 substrate; sync-back and per-phase opt-in are follow-ups)
- **Date:** 2026-05-22
- **Deciders:** project owner
- **Companion to:** [ADR 0032](0032-commit-decision-gate.md) — the
  commit-decision gate becomes the natural sync-back surface once
  worktree isolation lands. The two ADRs share architectural ground
  (run-owned worktree → operator-aware sync-back).
- **Supersedes (intent only):** the original GWT-1 milestone described
  in the run-evidence audit roadmap (internal planning record).
  The 2026-05-07 plan stayed at "make isolated worktrees the safe
  default substrate" without concrete granularity, sync-back, or
  guardrail-interaction decisions. This ADR fills those in.

## Context

Today every run mutates the user's source checkout directly. The
agent and the user share one git working tree, one git index, and
one HEAD. The pipeline tolerates this because:

1. A runtime guardrail
   ([`agents/command_guard.py`](../../agents/command_guard.py)) blocks
   destructive git from agents — `git checkout`, `git reset`,
   `git restore`, `git clean`, `git revert`, `git switch`.
2. The orchestrator captures a `diff.patch` artefact at run
   lifecycle end so the user can see what changed.
3. Prompt-level guidance ("preserve user-owned working-tree changes",
   "do not run destructive git") backs up the guardrail.

This was acceptable while runs were small, sequential, and operator-
supervised. Three pressures broke that:

- **Verification anxiety on long implement phases.** In a real
  run, deep into a large implement phase, the agent attempted to
  verify its change against a baseline commit via
  `git stash && git checkout <base-ref> -- <file>`.
  The guardrail correctly blocked the `git checkout` segment, the
  implement subprocess was killed (rc=-9), and the run halted. The
  *underlying* desire — "run a test against the unmodified file
  to check my change" — is legitimate; the agent had no sanctioned
  way to do it without destructive git.

- **Multi-agent and parallel-DAG roadmaps.** Parallel DAG waves
  explicitly depend on per-subtask isolation. Hosted-PR context
  integration needs a sandbox to apply third-party diffs without
  contaminating the user's checkout. Cross-project topology work
  has cross-alias contention that worktree-per-alias mitigates.

- **Safety of uncommitted user work.** Agents writing directly
  into the user's checkout invite the immediate "what if I had
  uncommitted work?" question. Today the answer is "stash first or
  live with the conflict" — a safe default should require neither.

The shared-worktree architecture is the root cause of all three.
GWT-1 introduces a sanctioned, run-owned worktree that decouples
agent mutation from user state.

## Decision

**Per-run isolated git worktrees become the default execution
substrate** for `orcho run` and `orcho cross`. The user's source
checkout becomes read-only context for the run; the agent's cwd is
an orcho-managed worktree under
`<workspace>/runspace/worktrees/<worktree_id>/checkout/` on branch
`orcho/run/<run_id>`.

### Locked-in design decisions

1. **Granularity — per-run by default, per-phase opt-in.** v1 ships
   per-run. A `worktree_isolation: per_phase` profile flag is
   schema-valid (so parallel-DAG callers can declare intent without
   code changes) but runtime-rejected with a clear error. Per-phase
   isolation lands with parallel DAG waves once that work is
   justified.
2. **Guardrail behaviour — relaxed inside the isolated worktree.**
   The `destructive_git` policy still applies to commands whose
   effective cwd is the user's checkout, but inside the orcho
   worktree the agent has full git freedom. Rationale: destructive
   ops in an isolated worktree cannot harm user-owned work;
   restricting them removes legitimate verification patterns for
   no safety gain. The guardrail's command-detection stays
   unchanged; only the cwd predicate is added.
3. **Sync-back is operator-aware**, not implicit. v1 PR1 ships the
   substrate only — the run writes into the orcho worktree, run
   completion leaves it as an artefact, the user is responsible
   for inspecting and applying. PR2 wires sync-back into the
   commit-decision gate (ADR 0032) so the operator decides via
   one structured gate how to deliver the diff (apply patch,
   merge branch, cherry-pick range, leave as artefact, halt).

### Lifecycle

For `mode="per_run"`:

1. **Run init.** `_PipelineRun.__init__` calls
   `pipeline.engine.worktree.resolve_worktree_for_run(run_id,
   project_dir, run_dir, worktree_config, profile_isolation)`. The
   resolver:
   - Reads the effective isolation mode (profile override > config
     > default `per_run`).
   - `worktree.enabled=false` short-circuits to `mode="off"`.
   - Resolves the user's source checkout HEAD as `base_ref`.
   - Calls
     [`core.io.git_helpers.create_worktree`](../../core/io/git_helpers.py)
     to materialise the physical checkout under
     `<workspace>/runspace/worktrees/<worktree_id>/checkout/` on
     `orcho/run/<run_id>`.
   - Returns a frozen `WorktreeContext` with `mode`, `path`,
     `base_ref`, `branch`, `retention_until`.
2. **Agent dispatch.** Every `agent.invoke(prompt, cwd=...)` call
   reads `ctx.path` for `cwd`. The runtime adapters
   (`agents/runtimes/claude.py`, `agents/runtimes/codex.py`) pass
   the cwd through to subprocess; the command guard receives it
   to decide whether destructive-git checks are active.
3. **Run completion.** The orchestrator persists
   `ctx.to_dict()` under `meta.worktree` and into the evidence
   bundle. `teardown_worktree(ctx, retain=True)` skips removal so
   the worktree survives past run-end for `retention_until`-bounded
   inspection. `orcho gc` (added in PR4) sweeps retained worktrees
   past their TTL.
4. **Sync-back (PR2).** After approve on the commit-decision gate,
   the chosen sync-method runs against the worktree's diff (apply
   patch → user checkout, or merge branch into user branch, etc.).

For `mode="off"`:

The resolver returns `WorktreeContext(mode="off", path=
project_dir, ...)` — every downstream consumer reads `ctx.path`
identically. Today's "agent mutates user's checkout" behaviour is
preserved verbatim under this mode. Set via
`worktree.enabled=false` config or via the CLI escape valve
`--no-worktree-isolation`.

For degraded paths the resolver splits cases by severity:

- **Non-fatal degraded** (e.g. a pre-existing rogue dir at the
  target worktree path the resolver cannot safely reuse):
  returns `mode="off"` with a populated `degraded_reason`.
  The run proceeds in the user's checkout; the reason is
  captured in `meta.worktree.degraded_reason` and in evidence
  so the operator can see why isolation didn't apply.
- **Fatal misconfiguration** (project not a git repo, repo
  has no commits yet, git binary unavailable): raises
  `WorktreeConfigError` and aborts the run before any phase
  executes. Silently degrading these would let later phases
  edit the user's tree while the review gates `git diff` an
  empty target and pass an unreviewed change. The error
  message names the specific failure and the appropriate
  remediation (`git init`, initial commit, install git, or
  `worktree.enabled = false` for explicit opt-out).

### Sync-back matrix (PR2 — for reference, not in this PR)

| `change_handoff` mode | Sync-back action options |
|---|---|
| `uncommitted`         | `apply_patch` (default), `copy_files`, `branch_handoff`, `none`, `halt` |
| `commit`              | `merge`, `cherry_pick`, `branch_handoff`, `halt` |
| `commit_set`          | `cherry_pick_range`, `merge`, `branch_handoff`, `halt` |

These extend, do not replace, the existing PR1 commit-decision
gate (`approve` / `skip` / `halt`). PR1 lands first; the
extension lands after GWT-1 PR1 is in.

### Wire shape

`meta.json` gains:

```jsonc
{
  "worktree": {
    "isolation": "per_run" | "per_phase" | "off",
    "path": "/abs/path/to/orcho-managed/checkout",
    "base_ref": "abcdef1234",
    "branch_ref": "orcho/run/<run_id>",
    "retention_until": "2026-05-29T00:00:00+00:00" | null,
    "degraded_reason": null | "<short reason>"
  }
}
```

This block is run-level, not phase-level — `PhaseMetrics` is
unchanged. The SDK exposes it via the existing run-status surface;
MCP `orcho_run_status` mirrors per the standard "MCP per-phase
validation" rule (matching schema update + L4 smoke in the same
commit set).

### Config

A new `worktree` section in `config.defaults.json`:

```jsonc
{
  "worktree": {
    "enabled": true,
    "isolation": "per_run",
    "retention_days": 7,
    "allow_destructive_inside": true
  }
}
```

The existing layered config overlay (local-config layered lookup)
applies normally.

### Profile schema

`pipeline_profiles_v2.json` gains an optional
`worktree_isolation: "per_run" | "per_phase"` per profile. v1
rejects `per_phase` at runtime; profile JSON validates either way.

## Consequences

**Adds** an architectural primitive that closes the verification-
anxiety failure mode, unblocks parallel DAG waves, hosted-PR
context integration, and per-alias cross-project control, and
guarantees the user's uncommitted work is never at risk.

**Couples** the orchestrators (single-project and cross) to a
new lifecycle step. Both must call `resolve_worktree_for_run` at
init and `teardown_worktree` at completion. The integration is
single-source-of-truth — both orchestrators consume the same
[`pipeline/engine/worktree.py`](../../pipeline/engine/worktree.py)
helper, mirroring the resume-profile auto-detect pattern from
commit `6f43130`.

**Expects** disk-aware operators. Each isolated run materialises
its working tree (objects shared via `.git/objects`, files
duplicated). For repos in the hundreds of MB, retention TTL +
`orcho gc` keeps the floor bounded; for tight-disk environments
the escape valve `--no-worktree-isolation` or
`worktree.enabled=false` flips back to pre-GWT-1 behaviour.

**Surfaces a new precondition** for PR2 sync-back: the commit-
decision gate's `pending_payload` becomes a two-axis decision
(approve/skip/halt × sync-method). PR1 of commit-decision (already
shipped under ADR 0032) is forward-compatible: the existing
shape stays valid for `mode="off"` runs; with `mode="per_run"`
the new sync-method axis lights up additively.

## Out of scope (PR1)

- **Sync-back action wiring.** PR1 of GWT-1 ships the substrate
  only — the run writes into the orcho worktree, completion leaves
  it as an artefact, the operator sync-backs manually. PR2
  threads it into the commit-decision gate.
- **Per-phase isolation.** Schema parses; runtime rejects.
  Parallel-DAG follow-up.
- **Worktree GC.** Retention timestamp is captured in
  `meta.worktree.retention_until`; sweeping comes with the
  `orcho gc` command in PR4.
- **Push / tag / branch publishing.** Worktrees commit to
  `orcho/run/<run_id>` locally. Pushing the branch upstream, or
  letting the operator merge it, is a separate UX layer above
  the orcho lifecycle.
- **Worktrees for runs OUTSIDE a git project.** Originally PR1
  silently degraded to `mode="off"` for a non-git `project_dir`.
  That turned out to be a foot-gun: later phases edit the user's
  tree while the review gates `git diff` an empty target, so the
  run passes an unreviewed change. The resolver now raises
  `WorktreeConfigError` for the three fatal isolation
  prerequisites (non-git dir, no-commits repo, missing git
  binary). The operator either fixes the prerequisite or sets
  `worktree.enabled = false` to opt out explicitly. Adding a
  non-git equivalent (e.g. tar-snapshot isolation) is a separate
  primitive entirely.

## References

- [`core/io/git_helpers.py`](../../core/io/git_helpers.py) —
  worktree primitives (`create_worktree`, `remove_worktree`,
  `worktree_diff_against_base`, `apply_patch_to_checkout`,
  `GitOpResult`).
- [`pipeline/engine/worktree.py`](../../pipeline/engine/worktree.py) —
  `WorktreeContext` + `resolve_worktree_for_run` +
  `teardown_worktree`.
- [ADR 0032](0032-commit-decision-gate.md) — commit-decision gate
  (sync-back partner in PR2).
- Post-mortem trigger: a real run halted by the destructive-git
  guardrail during a legitimate verification attempt (see Context).
