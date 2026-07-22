# Verification Contract

> Architecture reference. As of **Stage 1 (read-only projection,
> [ADR 0077](../adr/0077-verification-contract-read-only-projection.md))** core
> loads, validates, and *projects* the contract — into the run header and
> bounded per-phase prompt blocks. **Stage 2
> ([ADR 0078](../adr/0078-verification-contract-env-assertions.md))** adds
> *execution of one env's declared assertions* on demand via
> `orcho verify env`, persists an env-assertion receipt in its own directory,
> and adds `orcho workspace fine-tune --dry-run`. **Stage 3
> ([ADR 0080](../adr/0080-verification-contract-command-receipts.md))** adds
> *native execution of declared `verification.commands`* on demand via
> `orcho verify list` / `orcho verify run`, persisting a per-command
> command-receipt in its own directory. Stages 2 and 3 are still
> non-blocking: they do **not** block transitions or run repair. **Stage 4
> ([ADR 0081](../adr/0081-verification-contract-scheduling-and-repair-routing.md))**
> makes the contract *executable as policy*: it adds the gate-set / selection
> model, a deterministic policy algebra (absence-vs-explicit, work_mode-derived
> policy **and** action, defaults merge), and repair routing — a failed required
> `after_phase(implement)` gate enters `repair_changes` minus a reviewer pass,
> every other hook degrades `repair_loop` to a handoff. Stage 4 is the first
> *blocking* stage; it does **not** change the MCP wire (see
> [Stage 4](#stage-4-scheduling-policy-algebra-and-repair-routing)). **Stage 5
> ([ADR 0082](../adr/0082-verification-contract-final-acceptance-readiness.md))**
> gives the `final_acceptance` reviewer a *read-only readiness summary*
> (present/missing/failed/stale required receipts) — awareness only, no
> execution and no block. **Stage 6
> ([ADR 0083](../adr/0083-verification-contract-delivery-gate-awareness.md))**
> reads the contract at the *delivery* boundary: a `delivery_policy`
> (`manual|suggest|warn|require`, default `warn` when a contract is declared,
> `require` only by explicit opt-in) that warns or — non-interactively under
> `require` — blocks delivery on missing/failed/stale required receipts or
> detected generated garbage, classifying that garbage separately from the
> product diff. Stage 6 executes nothing and does **not** change the MCP wire
> (see [Stage 6](#stage-6-delivery-gate-awareness)). **Stage 7
> ([ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md))**
> records **cross-repo dependency provenance** on each command-receipt (one
> entry per declared `dependency_repos` — name/path/HEAD/dirty-summary/`depends_on`,
> receipt schema bumped 1 → 2) and marks a depended-on receipt **stale** when
> that dependency's HEAD moves — narrowly (HEAD-only, `depends_on` only),
> degrading rather than raising. Stage 7 writes nothing into dependency repos and
> does **not** change the evidence v1 bundle or the MCP wire (see
> [Stage 7](#stage-7-cross-repo-receipt-graph)). **Stage 8
> ([ADR 0089](../adr/0089-delivery-receipt-continuity.md))** lets a correction
> follow-up **inherit** the parent run's command-receipts for the *same* diff: the
> shared classifier searches the current run **then** the parent run(s) (one
> `state.extras` key, so readiness and the delivery gate cannot disagree),
> inheriting a valid parent receipt while never letting an older parent pass mask
> a fresh same-diff failure, and printing the searched dirs + exact `orcho verify`
> hints when proof is genuinely absent. Stage 8 rewrites no receipt and does
> **not** change the evidence v1 bundle or the MCP wire (see
> [Stage 8](#stage-8-parent-receipt-continuity)).
> `worktree_bootstrap` ([ADR 0074](../adr/0074-worktree-bootstrap.md)), the
> verification-environment receipt
> ([ADR 0076](../adr/0076-durable-verification-environment-receipt.md)), the
> Stage 2 env-assertion execution, and the Stage 3 command execution are the
> fields with execution behavior. See
> [Implemented today vs proposed](#implemented-today-vs-proposed) for the exact
> per-field status before treating any field below as more than read-only.

A *verification contract* is project-level configuration that tells Orcho **what
counts as proof that a change is ready, and in which environment that proof is
valid**. It exists to make correct verification easier than ad-hoc
verification — not to add another layer of mandatory checks.

The core rule the contract encodes:

```text
Agents may run any native tools while debugging.
Readiness is proven only by declared verification commands executed in the
declared verification environment.
```

This preserves agent autonomy during implementation while giving Orcho a
durable, reviewable record of *what was actually tested and against which
subject*.

## Current ADR 0132 foundation

This section is the normative vocabulary for the current contract foundation;
historical Stage descriptions below do not override it. Policy tiers are
`manual`, `suggest`, `warn`, and `require`, in increasing strictness. An absent
verification contract remains `None`; it is not a `manual` contract. A declared
contract whose `delivery_policy` is absent continues to resolve to `warn`.

`schedule` assigns a hook, phase, and policy to an identity. `selection`
answers whether a named gate set is selected; it does not schedule it and does
not select an executor. An automatic schedule that names a gate set with no
selection rule is rejected. A directly scheduled command has an explicit
`always` activation binding and no synthetic gate-set attribution. A selected
command with no applicable schedule gets `manual_only` with policy `manual`,
even when it appears in `verification.required`.

| Policy | Selected execution eligibility | Base consequence |
|---|---|---|
| `manual` / `suggest` | operator | none |
| `warn` on an executable hook | engine | warning |
| `require` on an executable hook | engine | required action |

`manual_only` accepts only `manual` or `suggest`; declared actions are allowed
only for `require`. Selection, execution policy, base consequence, and terminal
disposition are distinct facts. `pipeline.verification_execution` currently
provides the pure eligibility resolver only: scheduled-gates task 2 will adopt
  it in executors. The durable ledger and SDK projection are the authoritative
implemented by this foundation.

## Typed receipt outcomes and hygiene handoff

## Human-directed verification retry observability (ADR 0150)

The owner of verification retry accounting is the immutable
`VerificationHandoffRetryContext` in
`pipeline.project.verification_handoff_retry`.  It is built once from the
active handoff and the exact `(command, hook, phase)` gate identity.  A human
retry advances the round but never increases the original automatic maximum:
an exhausted `2/2` retry is round `3/2`, labelled `human retry 1 after
REJECTED verdict`.

`repair_changes` is dispatched through the normal lifecycle FSM, which writes
one phase-metrics attempt; the retry owner does not add another counter.  The
only durable gate record is `scheduled_gate_ledger.json`: its execution trail
stores the exact rerun as `rerun: true` with receipt evidence.  Evidence and
the SDK read that same ledger; no SDK or MCP wire schema changes are involved.
The same execution rule covers automatic repair-loop rechecks: every execution
after the initial exact gate identity is a rerun. Each trail event references a
different immutable receipt copy, while the flat command receipt remains the
single latest result classified by readiness and delivery.

## Verification subject identity (ADR 0140, I4-R1)

Command receipts now use schema **v3**. Their only authoritative freshness
proof is the top-level `subject` union:

```json
{"status":"available","identity":{"version":1,"object_format":"sha1","tree_oid":"…","observed_head_oid":"…","baseline_oid":null}}
```

or `{"status":"unavailable","reason":"…"}`. `tree_oid` is a temporary
index snapshot of the complete Git-visible checkout: tracked edits, additions,
deletions, renames, modes, symlink targets, and non-ignored untracked content.
Ignored untracked content is excluded. Capture never changes the real index,
refs, HEAD, or working tree.

`object_format` and `tree_oid` identify content. `observed_head_oid` and
`baseline_oid` are provenance: direct freshness additionally requires the same
observed HEAD, while baseline is not content equality. An unavailable or
malformed subject is `unverifiable`, never `present`; old v2 receipts remain
readable but cannot prove freshness. Each effective (`depends_on: true`)
dependency carries the same subject union. A dirty submodule must have its own
usable capture; its unchanged superproject gitlink is not proof.

The compact evidence v1 and MCP projections deliberately omit both `subject`
and `unverifiable`; they do not inspect Git themselves. R1 does **not** permit
exact-commit or apply carry-over after a changed HEAD. That delivery-transition
continuity is R2 work, and terminal/DONE projection is R3 work.

ADR 0130 retains the receipt status vocabulary (`present`, `missing`, `failed`,
`stale`) while adding `failure_kind`: no receipt is `missing`; a nonzero exit is
`test_failure`; no exit code/execution detail is `env_failure`; and an exit-0
failed assertion is `provenance_failure` for `import_path_*` or `env_failure`
otherwise. Fingerprint, checkout HEAD, and depended-on dependency movement are
`stale` after execution/assertion classification.

| Effective declared policy | `test_failure` / `missing` / `stale` | `provenance_failure` / `env_failure` |
| --- | --- | --- |
| `require` | blocking readiness, release gap, and delivery | visible `warn`, not an engine gap or delivery blocker |
| `warn` / `suggest` | visible warning | visible warning |
| `manual` | visible operator-owned item, never blocking | visible operator-owned item, never blocking |

A hygiene failure is not a source-code repair request. Its phase handoff uses
existing `artifacts.findings`, `artifacts.short_summary`, and `last_output`, and
offers only `continue_with_waiver` or `halt`. A waiver remains an explicit
operator action; test failures retain repair-loop and `retry_feedback` behavior.

No top-level handoff wire field is added. Core consumers use existing artifacts;
an exact MCP `findings_summary` or
`default_action=continue_with_waiver` requirement needs a separate `orcho-mcp`
wire change and smoke, because the current adapter reads top-level findings and
chooses its own default action.

## Why this exists

Three recurring pains motivate the contract:

1. **Repeated manual environment setup in every task.** Task files grow with
   copy-pasted interpreter / dependency recipes ("activate this venv", "set
   `PYTHONPATH`", "copy `libs/` first"). The setup is easy to forget and easy to
   get subtly wrong.

2. **Implementer / reviewer disputes about which local command is
   authoritative.** The implementer runs one command and calls it proof; the
   reviewer reruns a different host command, sees a different result, and
   rejects readiness. Both are locally correct because they tested *different
   subjects*. The operator becomes the real state machine for "what counts as
   proof".

3. **The `orcho-mcp` / `orcho-core` incident.** `orcho-mcp` depends on
   `orcho-core`. Orcho itself is launched from a stable install, so a bare host
   command such as:

   ```bash
   python -c "import pipeline; print(pipeline.__file__)"
   ```

   can import `pipeline` from the **stable install** instead of the canonical
   workspace `orcho-core` checkout. The implementer "passed" against canonical
   core, the reviewer reran a plain host command and saw stable core, and the
   run got stuck in handoff even when the code change was unrelated to the
   dispute. The contract removes this ambiguity by naming the subject under
   test explicitly.

This is not MCP-specific: C# projects may need ignored `libs/` copied before a
build, PHP projects require `vendor/bin/phpunit` rather than a global one, and
cross-repo tasks must prove which dependency checkout was under test.

## Default policy

The contract is opt-in and progressive. The default behavior is:

```text
No verification contract -> Orcho behaves as today.
Verification contract present -> Orcho may surface and use it.
Blocking behavior -> explicit project/profile/operator opt-in only.
```

The contract should feel like "Orcho remembers the boring setup for me", not
"Orcho demands a CI platform before I can run my first task". See
[First-run UX](#first-run-ux).

## The gate / environment / receipt triad

The contract deliberately keeps three concepts distinct. They are often shown
together in the same UI, but must never be collapsed into one:

```text
Quality gate = what must be true.
Verification environment = where and against what it is valid.
Receipt = proof that the native command ran in that environment.
```

A [quality gate](quality_gates.md) defines a pass/fail condition. A
verification environment defines the subject and command context that makes a
gate result meaningful. A receipt is the durable artifact proving the native
command actually ran in that environment. Each has a single owner; see
[quality_gates.md](quality_gates.md) for the gate side of this relationship.

## Reserved placeholders

Command and environment fields are templated. Orcho resolves these placeholders
at run time, so the contract never hard-codes a worktree path or a stable
install path:

```text
{checkout}          current Orcho-provided checkout/worktree (default command cwd)
{project}           canonical target repo metadata / source contract
{workspace}         Orcho workspace root
{run_dir}           current run artifact directory
{dependency:name}   a declared dependency repo (see dependency_repos)
```

The default command `cwd` is `{checkout}` — the current Orcho-provided worktree
where the agent edits. Do **not** write `cwd` explicitly unless a command must
run from another declared repo.

> **Stage 1 resolution (read-only).** Placeholder substitution is **purely
> syntactic** and never raises: each known token is replaced from the available
> run context, and any unknown or unavailable token is left **literal**. In the
> per-phase prompt blocks specifically, `{run_dir}` may not be resolvable at the
> point the block is built — when it is unavailable it stays literal (`{run_dir}`)
> rather than erroring. In Stage 1 projection `{checkout}` / `{project}` both
> resolve to the run's project checkout, `{workspace}` is inferred from the
> environment, and `{dependency:name}` resolves from `dependency_repos`. See
> [ADR 0077](../adr/0077-verification-contract-read-only-projection.md).

> **Stage 3 resolution (command execution).** When
> [`orcho verify run`](#cli-orcho-verify-list--orcho-verify-run-stage-3) executes
> a command, the subjects diverge: `{checkout}` resolves to the **recorded run
> subject**. An isolated run requires its exact readable
> `meta['worktree']['path']`; only `meta['worktree']['isolation'] == 'off'`
> resolves `{checkout}` to `{project}`. `{project}` stays the
> **canonical** target repo. The contract is loaded from `{project}` but commands
> run against `{checkout}`. See
> [ADR 0080](../adr/0080-verification-contract-command-receipts.md).

The split between `{checkout}` and `{project}` is what prevents the
canonical-vs-worktree confusion from the original incident:

- `{checkout}` is the **Orcho-provided checkout** — the per-run worktree, the
  code-under-test the agent is editing. It is transient and run-scoped.
- `{project}` is the **canonical target repo** — the durable source-of-truth
  repo and its metadata contract.
- `{dependency:name}` names a *different* canonical repo the subject depends on
  (e.g. `orcho-core` for an `orcho-mcp` task), so a receipt can record exactly
  which dependency checkout the proof ran against.

Because the subject under test is named by placeholder rather than left to
whatever `python`/`pipeline` resolves to on the host, the implementer and
reviewer can no longer be "both right against different subjects".

## Terms

### Canonical target repo vs Orcho-provided checkout

The single most important distinction in the contract:

| | Canonical target repo (`{project}`) | Orcho-provided checkout (`{checkout}`) |
|---|---|---|
| What it is | Durable source-of-truth repo + its contract | Per-run worktree, the code-under-test |
| Lifetime | Persistent | Run-scoped, transient |
| Role | Reference / dependency source | Where the agent edits and Orcho verifies |
| Confusion risk | Importing it when you meant the checkout (the MCP incident) | Treating it as the durable repo |

A verification environment exists precisely to assert *which* of these a given
import / binary / path actually resolves to.

### dependency_repos

Declares the other checkouts that are part of the subject under test. Each entry
maps a name to a path (and optional flags like `required`). Names become
`{dependency:name}` placeholders usable in environments and commands.

> Implemented (read-only Stage 1 projection,
> [ADR 0077](../adr/0077-verification-contract-read-only-projection.md)) via
> `PluginConfig.dependency_repos`. Loaded, carried, and used to resolve
> `{dependency:name}` placeholders during projection. No execution.

### worktree_bootstrap

Preparation steps run after an isolated worktree exists (and after any pre-run
dirty seed is applied) but **before** agent phases. Bootstrap makes the
checkout *usable*; it is not itself proof that the change is ready. Supported
portable actions are `{"copy": <path>}` and `{"run": [argv...]}`.

> Implemented today via `PluginConfig.worktree_bootstrap`
> ([ADR 0074](../adr/0074-worktree-bootstrap.md)).

### worktree_teardown

Cleanup steps symmetric to `worktree_bootstrap` — same step shapes — declared as
*what* to tear down. The engine guarantees *when*: at run finalization, in the
worktree cwd, immediately before the git worktree is released, and only for a
**terminal** run. A run paused awaiting a phase-handoff decision keeps its
worktree (and its external stack) for resume and is not torn down. Teardown is
best-effort: a failing step is recorded but never raises, so cleanup cannot mask
the run's real outcome.

> Implemented via `PluginConfig.worktree_teardown`
> ([ADR 0131](../adr/0131-worktree-teardown-and-isolation-id.md)).

#### Isolated runs against a Docker Compose stack

A project whose gates run against a live Compose stack can run under worktree
isolation without collision by keying the stack on `ORCHO_ISOLATION_ID` — a
stable, per-worktree namespace Orcho exports into the environment (alongside
`ORCHO_RUN_ID`, and — like it — not stripped from gate command environments).
Bring the stack up in `worktree_bootstrap`, run gates against it, and tear it
down in `worktree_teardown`:

```python
PLUGIN = {
    # bring up an isolated, per-worktree stack (unique project name + ephemeral
    # ports so parallel runs on the same repo never collide)
    "worktree_bootstrap": [
        {"run": ["docker", "compose", "up", "-d", "--wait"]},
        {"run": ["make", "prepare-test-env"]},
    ],
    "worktree_teardown": [
        {"run": ["docker", "compose", "down", "-v"]},
    ],
}
```

The Compose file (a full copy lives in each worktree) should read
`COMPOSE_PROJECT_NAME=orcho_${ORCHO_ISOLATION_ID}` and bind ephemeral (or no)
host ports for its test services. Because both `worktree_bootstrap` and gate
commands inherit `ORCHO_ISOLATION_ID`, they target the same stack; the teardown
hook removes it at run-terminal even if the run halted.

### verification_envs

Named environments that define the subject under test and command context.
Each answers: *when Orcho says "test passed", which executable, cwd, env, paths,
dependency checkouts, and runtime assertions made that statement valid?* For
Python this is typically a declared interpreter plus import assertions; for
other stacks it is path / binary / version / file / service / command
assertions.

> Implemented. Stage 1 projection
> ([ADR 0077](../adr/0077-verification-contract-read-only-projection.md)) loads
> and validates them and surfaces names. **Stage 2
> ([ADR 0078](../adr/0078-verification-contract-env-assertions.md))** *executes*
> a single env's declared `assertions` on demand via `orcho verify env`, using a
> generic, key-dispatched vocabulary (import `path_equals`/`path_under`,
> `path_exists`, `file_exists`, `command_exists`, and the single
> `{"version": [...], "contains": ...}` form). The effective cwd defaults to
> `{checkout}`, so import/version subprocesses prove the
> declared checkout, not the CLI's host cwd. Execution never raises (subprocess
> failure/timeout → `passed=false`); unknown keys are failed checks. The result
> is written as an env-assertion receipt (see
> [The env-assertion receipt](#the-env-assertion-receipt-stage-2)).

### verification.commands

Named native commands that count as readiness proof. They are not a new test
framework or DSL — they are argv + env + assertions, and Orcho's durable record
that a native tool was invoked. A `python` token in a command means "the
environment's declared python", not whatever bare `python` resolves to on the
host.

> Implemented. Stage 1 projection
> ([ADR 0077](../adr/0077-verification-contract-read-only-projection.md)) loads
> the commands and projects each name + (placeholder-resolved) `run` text into
> the header summary and per-phase prompt blocks. **Stage 3
> ([ADR 0080](../adr/0080-verification-contract-command-receipts.md))** *executes*
> a command's native argv on demand via `orcho verify run`, with the default
> command `cwd` = `{checkout}` (the run worktree) and a bare `python` token
> resolved to the declared env interpreter. The execution writes a durable
> [command-receipt](#the-command-receipt-stage-3). The receipt's **git
> provenance is always taken from the subject checkout (`{checkout}` = run
> worktree), independent of the command's `cwd`** — see
> [Command cwd vs git subject checkout](#command-cwd-vs-git-subject-checkout).
> Each command may declare `parity: absolute | differential` (validated as an
> enum; default `absolute`). Commands are still **not** scheduled or blocking;
> `verify run` is operator-invoked.

#### verification.required

`verification.required` is a list (tuple) of declared command **names** — the
set that forms the required gate. It is validated against `commands` when the
contract loads (an unknown name, or a non-list shape, is a
`VerificationContractError`); an undeclared contract stays `None`.
`orcho verify run --required` executes exactly this set and aggregates pass/fail;
an empty or missing required set is a resolution error (exit 2).

> Implemented (Stage 3,
> [ADR 0080](../adr/0080-verification-contract-command-receipts.md)). Earlier
> drafts modelled `required` as a boolean; it is now a list of command names.

### verification.schedule

Declarative, phase-aware policy for *when* commands are suggested, run, checked,
or made blocking. Scheduling hooks start small:

```text
before_phase
after_phase
before_delivery
on_resume
manual_only
```

Per-entry policy:

```text
manual   operator-owned; no automatic execution
suggest  operator-visible suggestion; no automatic execution
warn     executable warning policy
require  executable policy with a required action on failure
```

Per-entry action (optional; what happens when a `require` gate fails):

```text
continue_warn   warn and proceed (never blocks)
repair_loop     route into repair (see the repair_loop-by-hook matrix)
handoff         pause for a human decision
abort           stop the run
```

`policy` and `action` are both **optional** (Stage 4). Their *absence* is
deliberately distinct from any explicit value — see
[absence vs explicit](#absence-vs-explicit-the-load-bearing-distinction). A
schedule entry may also carry an optional `gate_sets` list that narrows the
defaults-merge source.

> Implemented. Stage 1 projection
> ([ADR 0077](../adr/0077-verification-contract-read-only-projection.md)) loads,
> validates, and surfaces hooks/policies into each phase's prompt block. **Stage 4
> ([ADR 0081](../adr/0081-verification-contract-scheduling-and-repair-routing.md))**
> ADR 0132 foundation validates and normalizes schedule/selection identities.
> It does not adopt the new eligibility resolver in transition executors.

### work_mode

The user-facing strictness control. Gate policies are derived defaults, not
something the operator tunes per gate:

```text
fast      move quickly, use gates as hints and cheap feedback
pro       balanced default, run important gates and repair obvious failures
governed  strict delivery discipline, require declared proof before key transitions
```

Gate policy is derived from the declared **tier** (`manual` / `suggest` / `warn` /
`require`) and the work_mode — **never** from cost. The declared tier `T` is the
explicit base policy when set, else `require` for a required command and
`suggest` for an advisory one. The mode × tier projection (cost is not an input):

| Mode | `manual` tier | `suggest` tier | `warn` tier | `require` tier |
|---|---|---|---|---|
| `fast` | `manual` | `manual` | `warn` | `require` |
| `pro` | `manual` | `suggest` | `warn` | `require` |
| `governed` | `manual` | `suggest` | `require` | `require` |
| unset | `manual` | `suggest` | `warn` | `require` |

`require` is honored in every mode (the must-block tier — mode-independent);
`governed` escalates `warn → require`; `fast` relaxes only the advisory
`suggest → manual` for speed; `pro` and unset honor the declared tier as-is. So an
expensive `require` gate (e.g. the broad suite) still blocks; the lever to make a
gate advisory is choosing a lower tier, not its cost (see
[ADR 0117](../adr/0117-verification-blocking-tier-independent-of-cost.md)).

The default for a newly fine-tuned project is `pro` with mostly `warn`
behavior, never immediate `governed` blocking.

> Implemented. Stage 1 validated `work_mode` and showed it in the header.
> **Stage 4 ([ADR 0081](../adr/0081-verification-contract-scheduling-and-repair-routing.md))**
> makes it *derive policy and action*: when a schedule entry omits `policy` /
> `action`, the gate-set base is fed through the work_mode transform (table
> above for policy; the [deterministic action table](#deterministic-work_mode-derived-action)
> for action). It is still **not** surfaced on the MCP wire.

### prompt_policy

How Orcho turns the contract's *facts* (which envs exist, which commands are
authoritative, when gates are scheduled, where receipts live) into
*phase-specific prompt blocks*. Prompt policy is orchestration behavior and does
**not** live inside `PLUGIN["verification"]`; the plugin supplies source data,
Orcho owns the policy.

Orcho ships a default prompt policy. Overrides merge in a fixed order:

```text
Orcho default prompt policy
  -> workspace override
  -> project override
  -> run/profile override
```

Each phase receives only the bounded subset of contract facts it can use; the
whole plugin config is never dumped into a prompt.

> Partially implemented. The **Orcho default prompt policy** is live. In Stage 1
> ([ADR 0077](../adr/0077-verification-contract-read-only-projection.md)) each
> phase got its relevant raw schedule entries. **Stage 4
> ([ADR 0081](../adr/0081-verification-contract-scheduling-and-repair-routing.md))**
> projects from the resolved `ScheduledGatePlan` instead when the contract
> declares `gate_sets` / `selection`: the per-phase block carries the *effective*
> `policy -> action` (post work_mode transform) and the gate source via
> `primary_gate_set`, still as a bounded RUN-scoped `PromptPart`. The **override
> merge chain** (workspace → project → run/profile) is **still deferred** — both
> stages ship only the code-owned default.

## Stage 4: scheduling, policy algebra, and repair routing

Stage 4
([ADR 0081](../adr/0081-verification-contract-scheduling-and-repair-routing.md))
turns the read-only schedule/work_mode into an executable, deterministic policy
algebra. It adds a gate-set / selection model, resolves an *effective policy* and
*effective action* per command and hook, and routes a failed required gate into
the right transition. This is the first **blocking** stage. It does **not** move
the MCP wire (see the falsifier in ADR 0081).

### Selection model

```text
gate_sets:  name -> { commands: [...] (required),
                      default_policy?, default_action?, default_cheap? }
selection:  ordered rules, each with exactly one type key:
              { always:    [sets] }
              { task_kind:  <str>, include: [sets] }
              { paths:      [glob...], include: [sets] }
              { operator:   [sets] }
```

A run resolves a `ScheduledGatePlan`: selection runs in the fixed order
`baseline(always)` → `task_kind` → `subsystem(paths ∩ touched_paths)` →
`operator` (deduped, stable). The selected commands are the union of the selected
sets' commands; each records `contributing_gate_sets` (all selected sets that
contain it) and `primary_gate_set` (the first by that fixed order).

The four steps consume run-level **selection inputs**. Their production source
is the declared contract; a per-run operator/CLI request may override them via
`state.extras` (keys `verification_task_kind` / `verification_operator_sets`).
The resolved inputs feed executable `ScheduledGatePlan`s cached **by lifecycle
position** (`hook:phase`) in `state.extras["verification_gate_routing_plans"]`.
Keying by position — not merely by hook — matters because `after_phase` fires
after *every* phase: `after_phase(plan)` / `after_phase(validate_plan)` run
before `implement` (no implement changes yet), while `after_phase(implement)`
runs after and so builds its plan from the post-implement changed files for
path-based subsystem selection. Each distinct `hook:phase` position builds its
plan once, at that point, and reuses it on later invocations of the same
position (deterministic, no per-hook recompute); an early plan (e.g.
`after_phase:plan` or `before_phase:implement`) is **never** reused for
`after_phase:implement`. The per-phase prompt blocks use a **separate advisory
preview** (`state.extras["verification_gate_prompt_preview"]`) that routing never
reads — the prompt may be built before `implement` mutates the tree, so its
path-based selection can be incomplete, but it cannot suppress a gate that
becomes relevant only after `implement`.

At the pre-final boundary, the current run-worktree context resolves one
authoritative `before_delivery:` epoch. Its selection trail is written to the
scheduled-gate ledger before the identical plan is published in the routing-plan
cache or any command is materialized. Readiness, the final delivery backstop, and
the materializer consume that published plan; they do not rebuild path selection
for an epoch already recorded. A repeated hook or checkpoint resume reconstructs
the recorded identities from the ledger, so later git changes or plugin selection
rules cannot rewrite the epoch. Prompt preview remains advisory and never enters
this replay path.

- `task_kind` — the project/profile-declared task class
  (`verification.task_kind`, override `state.extras["verification_task_kind"]`).
  A `task_kind` rule activates only when it equals this value. Auto-inference
  from the task is a TODO; absent → no `task_kind` gates.
- `touched_paths` — the run worktree's changed files, captured once when the
  per-run plan is built.
- `operator_sets` — gate sets the project/profile/run opted into
  (`verification.operator_sets`, override
  `state.extras["verification_operator_sets"]`). An `operator` rule is
  **opt-in**: its sets activate *only* when named here. With no request, no
  operator gate is selected — expensive opt-in gates never fire by default, so
  first-run usage stays non-hostile.

### Absence vs explicit (the load-bearing distinction)

`schedule.policy`, `schedule.action`, and the `gate_set.default_*` fields are all
optional, and **absence (`None`) is not an explicit value**:

- **Absent** → flows through the merged gate-set base + the work_mode transform.
- **Explicit** policy supplies the declared tier; it is still projected through
  the selected work mode. An explicit action is valid only with `require`.

This is why omitting `policy` differs from writing `policy: suggest`; both are
then subject to the same exact mode matrix.

### Effective policy resolution

```text
1. explicit schedule.policy (not None, incl. "suggest")  -> declared base tier
2. else merged gate_set default_policy (may be None)      -> base_policy
3. else derive the tier from base_policy / required-ness  -> per the work_mode table
   + work_mode
```

The work_mode policy table (same as the [work_mode](#work_mode) section) keys on
the declared **tier** and **never** on cost. The declared tier `T` is the
explicit base policy when set, else `require` for a required command and
`suggest` for an advisory one. `fast` relaxes only the advisory `suggest` tier to
`manual`; `warn` and `require` are honored. `pro` honors the declared tier exactly.
`governed` escalates `warn → require`; `require` and `suggest` are honored. An
unset work_mode honors the declared tier. So a `gate_set.default_policy: require`
blocks under **every** mode regardless of cost — an expensive `require` gate
still blocks; the lever to make a gate advisory is choosing a lower tier, not its
cost ([ADR 0117](../adr/0117-verification-blocking-tier-independent-of-cost.md)).

### Deterministic work_mode-derived action

When neither an explicit `schedule.action` nor a merged `default_action` is set,
the action is derived **strictly deterministically** from `(hook, phase,
work_mode)`:

| Hook / phase | `fast` | `pro` | `governed` | unset |
|---|---|---|---|---|
| `after_phase(implement)` | `continue_warn` | `repair_loop` | `repair_loop` | `continue_warn` |
| `before_delivery` | `continue_warn` | `handoff` | **`handoff`** | `continue_warn` |
| anything else | `continue_warn` | `continue_warn` | `continue_warn` | `continue_warn` |

Explicitly: **`governed` + `before_delivery` + `require` with no explicit action
⇒ `handoff`** (never `abort`). `abort` is only ever reached through an *explicit*
`schedule.action`/`default_action` or a separate terminal/system path — the
algebra never derives it.

### Defaults merge and attribution

When a selected command belongs to several gate sets, their defaults merge
deterministically:

```text
merged_default_policy = max strictness (manual < suggest < warn < require) among
                        contributing sets that declare one  (None if none)
merged_default_action = max strictness (continue_warn < repair_loop < handoff
                        < abort) among contributing sets that declare one
merged_cheap          = command.cheap OR any contributing default_cheap
```

A schedule entry's optional `gate_sets` narrows the merge **source** only — it
does not change `contributing_gate_sets` / `primary_gate_set` attribution. The
resolved gate identity is `(command, hook, phase)`: phase is part of the
identity for `before_phase` / `after_phase`, so the same command scheduled under
one hook for two phases yields two distinct gates (it never collapses). When two
schedule entries target the *same* `(command, hook, phase)`, a tie-breaker keeps
the max-strictness pair; a `None` policy is treated as *absent* (an explicit
entry participates; all-`None` stays `None`). A selected command with no
applicable schedule entry becomes `manual_only` with policy `manual`.

### The repair_loop-by-hook matrix (user-visible)

`repair_loop` is a *real* repair flow **only** for `after_phase(implement)` (and
only when the profile has a `repair_changes` step). For every other hook —
`before_phase`, `before_delivery`, `on_resume`, or `after_phase` of any
non-implement phase — `repair_loop` **deterministically degrades to `handoff`**,
with a logged note. This degradation is intentional, user-visible behavior: a
gate cannot "repair" where there is no implement→repair pair to drive.

### The critical flow: implement → repair, minus review

A failed required `after_phase(implement)` gate whose effective action is
`repair_loop`:

1. synthesizes the critique from the failed command receipt
   (`state.last_critique` / `state.last_test_output`) — the failing command
   output *is* the critique;
2. dispatches `repair_changes` through the lifecycle FSM **without** a preceding
   `review_changes` pass (token economy — no reviewer turn);
3. **re-executes the same gate command** as the exit condition;
4. repeats up to the repair budget (`--max-rounds` / the profile's
   `repair_round` loop); a passing re-check closes the flow, budget exhaustion
   escalates to a handoff.

`continue_warn` warns without blocking; `handoff` pauses; `abort` stops the run.
The ADR 0132 foundation does not change executor routing; `manual` and `suggest`
are operator-owned in the eligibility contract, while `warn` has consequence
`warning` and `require` has `required_action`.

Every gate command the Stage 4 router executes is run with the **run worktree
checkout** as the `{checkout}` subject and its receipt is **persisted** under
`<run_dir>/verification_command_receipts/` (latest execution per command wins),
so Stage 5 readiness, the Stage 6 delivery gate, and the evidence bundle see
the same proof routing acted on (ADR 0090 — the silent-skip incident ran gates
against the original project directory and dropped the receipts).

### Handoff retry: fresh subject, exact identity (ADR 0149)

When a required verification gate publishes a
`trigger="verification_gate_failed"` handoff, routing is trigger-first rather
than phase-first.  It therefore remains a verification retry if the pause is
at `final_acceptance`; it is not scope expansion unless the trigger explicitly
states `scope_expansion:*`.  The recovery owner accepts only an exact durable
gate identity `(command, hook, phase)`, obtained from the handoff or an
unambiguous scheduled-gate ledger record.

After all preconditions succeed, the owner consumes the active decision once,
runs exactly one `repair_changes` step against the retained worktree, and
reruns only the identified gate against that freshly repaired subject.  A
passing rerun continues; a failing rerun creates a new handoff.  Missing
feedback, ambiguous identity, stale handoff id, unproven repair subject, or a
missing repair step is a **blocker**: the original handoff remains available
and no decision is consumed.  A provider/process exception is a **crash** and
uses the normal interrupted/failed lifecycle rather than being converted into
a blocker or a second retry.

This is verification-routing behavior only.  It does not specify MCP handler
implementation and does not make final-acceptance recovery part of core.

<!-- TODO(orcho-verification-stage4): expand the full authoring guide (worked
gate_set/selection recipes per stack, override-chain examples) once the
workspace → project → run prompt-policy override chain lands. -->

### Authoritative receipt

The durable artifact proving a *declared verification command ran in a declared
environment*. It is the unit of review: reviewers read receipts, not arbitrary
re-runs. A claim is authoritative only when backed by a receipt; narrative
("tests pass") is not. See
[Implemented today vs proposed](#implemented-today-vs-proposed) for the exact
on-disk shape that exists in core today.

### Exploratory command

Any native command an agent runs while debugging:

```bash
pytest -q tests/unit/foo.py
dotnet test MyProject.Tests --filter SomeCase
npm test -- Button.spec.ts
vendor/bin/phpunit --filter UserService
```

Exploratory commands are useful but do **not** prove readiness unless they match
a declared verification command or are explicitly promoted into a receipt. See
[Exploratory commands vs authoritative receipts](#exploratory-commands-vs-authoritative-receipts).

## Stage 5: final acceptance readiness awareness

Stage 5 ([ADR 0082](../adr/0082-verification-contract-final-acceptance-readiness.md),
`pipeline/verification_readiness.py`) gives the `final_acceptance` reviewer a
**read-only readiness summary** of the declared verification contract, so the
closing gate reasons from official proof instead of re-running ad-hoc host
commands. When a contract is declared, the final-acceptance prompt carries a
`verification_readiness` block with:

- **Environments** — per-env `all_passed` status from the Stage 2
  env-assertion receipts (`verification_env_receipts/`).
- **Scheduled gates** — the delivery-relevant gates
  (`after_phase(implement)` + `before_delivery`) with their effective
  policy/action and per-receipt status.
- **Required receipts** — the required delivery command set
  (`verification.required` plus the commands scheduled at the delivery
  positions), classified **present / missing / failed / stale**:
  - *missing* — no command-receipt on disk;
  - *failed* — non-zero exit, a failed declared assertion, or an execution
    `detail`;
  - *stale* — the receipt's `git.changed_files_fingerprint` /
    `git.checkout_head` no longer match the current checkout (recomputed with
    the same `changed_files_fingerprint` helper the receipt writer used — now
    homed in `pipeline/verification_dependencies.py`, [ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md);
    with no usable checkout, staleness is not asserted), **or** (Stage 7) a
    depended-on dependency's HEAD moved since the receipt was written — see
    [Stage 7](#stage-7-cross-repo-receipt-graph). The stale reason (subject
    drift, or the dependency name + both SHAs) is surfaced in the render. A
    receipt is searched for in the **current run first, then the follow-up's
    parent run(s)**; a valid parent receipt for the same diff is *inherited*
    (with provenance) — see [Stage 8](#stage-8-parent-receipt-continuity);
- **Exploratory commands** — a count of observed ad-hoc `command.end` events,
  labelled explicitly as not authoritative.
- **Remaining before ready** — only the **effective `require`** missing/failed/stale
  gaps (worded `missing required: …`); when the only open gaps are
  `warn`/`suggest`/`manual_only` it reads `(none blocking — … shipping allowed by
  policy)`, and `(none — declared proof complete)` when the declared proof is
  complete.
- The verbatim reviewer policy: `Readiness blockers should be based on
  missing/failed/stale/invalid declared receipts, not only an ad-hoc host
  command mismatch.`

**Per-gate policy awareness ([ADR 0097](../adr/0097-delivery-verification-policy-ux.md)).**
Each missing/failed/stale gap is annotated with its **effective per-gate
delivery policy** — resolved once through the single pure source
`pipeline/verification_policy.py` (`effective_delivery_policy_by_command` +
`partition_gaps`, over the same delivery plan and the `resolve_delivery_policy`
boundary value, which is unchanged). Consequently:

- the phrase **`missing required`** is used only for an effective `require` gap
  (and only those count toward "Remaining before ready");
- a `warn` / `suggest` gap is rendered with its policy and the explicit note
  **`shipping allowed by policy`** — surfaced, not a blocker;
- a `manual_only` / operator-only required command is shown on its own
  `Manual-only receipts:` line (`not auto-run`) and is **excluded** from
  `required_missing` and from "Remaining before ready" — it is intentional
  non-auto work, never a missing-required receipt. The no-blocker (fully-proven)
  block stays byte-identical.

The release backstop (`required_receipt_gaps`) likewise emits a
`verification_gaps` entry only for effective `require` gaps; `warn`/`suggest` are
shipping-allowed and `manual_only` is never a gap — so reclassifying a command as
manual can never hide a real auto-required gap (the ADR 0090 "never falsely
green" invariant holds).

**Authoritative selection input.** The gate plan feeding the summary is the
*delivery* selection plan: the executable routing plan cached for the
`before_delivery` epoch when the Stage 4 hook already built one, otherwise a
fresh plan built at readiness time from the current checkout's changed files.
The advisory prompt-preview plan is never consulted — it may be memoized before
`implement` mutates the tree, which would hide a path-selected gate that became
delivery-relevant afterwards.

**Engine backstop (ADR 0090).** The readiness *summary* is advisory, but the
classification it is built from is also load-bearing: after parsing the
release verdict, the `final_acceptance` handler merges engine-computed
`verification_gaps` (one per required delivery command classified
missing/failed/stale, via `required_receipt_gaps`) and forces
`approved=False / ship_ready=False` — a reviewer model that omits an unproven
required gate cannot produce a green acceptance. The backstop is inert under
dry-run, without a contract, and when an operator waiver
(`continue_with_waiver`) is active.

Boundaries, stated explicitly:

- **Read-only awareness.** Stage 5 executes nothing, writes no receipt, and
  blocks no transition; it only renders what is already on disk (the
  ADR 0090 backstop above reuses the classification but is owned by the
  `final_acceptance` handler, not by this module).
- **No wire change.** No schema, mode flag, or gate primitive moves; the
  prompt block and the evidence key below are additive.
- **Dry-run reads nothing.** `state.dry_run` short-circuits before any receipt
  loader runs; the no-contract prompt stays byte-identical.
- **Evidence digest carries observed facts only.** The evidence v1 bundle
  gains an additive `verification_readiness` key (command summaries
  passed/failed + env `all_passed`) — deliberately **without** stale/missing
  verdicts, because the collector has neither the declared contract (no
  required set) nor the live checkout/HEAD after the run; that classification
  is owned by the prompt layer.

### Scope expansion evidence (read-only projection)

Alongside the readiness summary, Stage 5 classifies the files a run changed
**outside its declared plan scope** into `notice` / `risk` / `blocker` with
per-file evidence ([ADR 0110](../adr/0110-scope-expansion-notice.md),
`pipeline/engine/scope_expansion.py` — a pure, deterministic classifier). This is
a **read-only evidence projection, not a new gate**: it executes nothing and
moves no schema, mode flag, or gate primitive.

- **Default is `notice`, not handoff.** A small, verified, explained out-of-plan
  companion edit (a regenerated lockfile, refreshed fixture/snapshot) is a
  `notice` — surfaced, never an auto-reject. `risk` is also surfaced without
  blocking.
- **`verified` is per-file / per-category.** A category is cleared only by its
  *relevant* required gate (test → `fixture_snapshot`; schema/snapshot-guard →
  `generated_schema` / `public_wire`; lint/build → `build` / `import_wiring`),
  computed from the same required-receipt partition above. A would-be `notice`
  without a green relevant gate is downgraded to at least `risk`.
- **Blocker tier reuses the ADR 0090 backstop; it does not add a gate.** Only a
  `blocker` (unaligned public wire/schema, persistence/state, security/secret,
  destructive/mass-delete, large diff, repeated-across-corrections) forces
  REJECTED — merged into the same `verification_gaps` list as a *parallel* engine
  gap source, inert under dry-run / no contract / active operator waiver. The
  verification gates keep their full authority; scope expansion only adds
  rejection reasons for the blocker tier and never softens a required gate.
- **Single canonical durable path.** The handler writes
  `assessment.to_dict()` to `phase_log['final_acceptance']['scope_expansion']`
  (only when there are out-of-plan items, so an in-scope run is byte-identical),
  and the `FinalAcceptanceAdapter` projects it verbatim to
  `session['phases']['final_acceptance']['scope_expansion']`. The DONE/Evidence
  summary reads **only** that session path. The JSON-safe shape is
  `{ items: [{ path, category, status, evidence }], has_blocker, counts }`; the
  `status` values are `scope_expansion_notice` / `scope_expansion_risk` /
  `scope_expansion_blocker`. See [ADR 0110](../adr/0110-scope-expansion-notice.md)
  for the full status matrix, signal→source map, and the additive MCP follow-up
  contract over this same shape.

## Stage 6: delivery gate awareness

Stage 6 ([ADR 0083](../adr/0083-verification-contract-delivery-gate-awareness.md),
`pipeline/verification_delivery.py`) reads the contract at the **commit
delivery boundary** — the post-acceptance step that transports the run-owned
diff into the project checkout and commits it. It is the last boundary and the
only one that acts on the final tree, so it is where declared proof can
actually gate the change leaving the run.

A new optional contract field `verification.delivery_policy` (validated against
the canonical `manual | suggest | warn | require` vocabulary — no new policy
constants) selects the behaviour. Defaults are conservative:

| Situation                                                          | Effective delivery policy |
| ------------------------------------------------------------------ | ------------------------- |
| No verification contract                                            | `None` (no gate)          |
| Contract declared, `delivery_policy` unset, no scheduled require    | `warn`                    |
| Schedule entry at `before_delivery` with explicit `policy: require` | `require` (ADR 0090)      |
| `delivery_policy` declared explicitly                               | that value                |

`require` is reachable by an explicit `delivery_policy: "require"` **or** by
explicitly scheduling a `policy: "require"` gate at the `before_delivery` hook
— declaring a required delivery gate IS the opt-in (ADR 0090 superseded the
original explicit-field-only rule after the silent-skip incident: a skipped
scheduled hook must not degrade the boundary to a warning). `work_mode`
(including `governed`) never escalates the delivery policy — unlike the Stage 4
action algebra, the hard delivery block remains an intentional opt-in.

What counts as a **blocker**:

- a required delivery receipt that is **missing / failed / stale** — classified
  by the *same* Stage 5 function (`classify_required_receipts`), called with the
  delivery subject checkout as an explicit `checkout` argument, so staleness is
  computed identically to Stage 5 and a receipt written against that checkout is
  never falsely stale. Because both surfaces classify through that one function
  reading the **same** parent-run search path ([Stage 8](#stage-8-parent-receipt-continuity)),
  `final_acceptance` readiness and the delivery gate are constructively guaranteed
  to agree on the missing/failed/stale partition;
- **generated environment garbage** staged in the subject checkout —
  `.venv/`, `__pycache__/`, `*.pyc`/`*.pyo`, `.pytest_cache/`, `.ruff_cache/`,
  `.mypy_cache/`, `.tox/`, `node_modules/`, `*.egg-info/`, and the three
  verification-receipt directories. Garbage is detected component-by-component
  (so `src/venv_utils.py` is product, `.venv/lib/...` is garbage) and rendered
  in its **own** prompt section, distinct from the product `M` / `??` diff.

Behaviour by policy: `suggest` adds one hint line; `warn` adds a warning block
(interactive) or a `warn` log line (non-interactive) and **delivery proceeds**;
`require` with blockers returns the new delivery decision status
`verification_blocked` non-interactively (run halts with
`halt_reason="commit_delivery_verification_blocked"`, an amber banner) and, at a
TTY, defaults the correction prompt to `fix` so a bare Enter never delivers.

**Per-gate policy awareness ([ADR 0097](../adr/0097-delivery-verification-policy-ux.md)).**
The delivery boundary still has the single effective policy above
(`resolve_delivery_policy`, unchanged), but each gap is now classified by its
**effective per-gate policy** through the shared pure source
`pipeline/verification_policy.py`, so the assessment and the prompt no longer
over-state the contract:

- `DeliveryVerificationAssessment` partitions gaps into `blocking` (effective
  `require`), `warning` (`warn`/`suggest`), and `manual_only`. `required_missing`
  / `_failed` / `_stale` exclude `manual_only` (it goes to a separate
  `manual_only_gaps` field); `blocking` is true iff a `require` gap exists, so a
  per-gate `require` gate blocks even under a `warn` boundary while `warn` /
  `suggest` never block.
- The blocker lines are policy-aware: an effective `require` gap reads
  `missing/failed/stale required receipts: …`; a `warn` / `suggest` gap reads
  `missing receipts: <cmd> (<policy>) — shipping allowed by policy`.
- The interactive/non-interactive prompt frames `require` as a hard block
  (`blocked by required verification` / `delivery blocked until receipt or
  waiver`, default `fix`) and `warn` / `suggest` as shipping-allowed (default
  `apply`/`approve`). When an operator chooses `approve` / `apply` at a `require`
  block, the choice is marked an explicit **operator override / waiver**.
- The persisted `commit_decision` field names are unchanged — the per-gate
  policy and the blocking/warning split are render-only.

**The single orchestration interface** is
`assess_delivery_verification(contract, run_dir, ctx, extras, diff_cwd,
baseline_ref="HEAD")`: the caller passes only the subject checkout (`diff_cwd`)
and the baseline; the pure module reads untracked (`git ls-files --others
--exclude-standard`) and changed (`git diff --name-only <baseline_ref>`) paths
and the checkout identity itself. `run.py` contains no git logic for this.

**Difference from the Stage 4 `before_delivery` hook.** The Stage 4
`before_delivery` schedule hook *executes* declared commands (writing receipts)
*before* `final_acceptance`, as part of the gate-routing algebra. Stage 6 runs
*after* acceptance, at the actual approve/apply boundary, and **executes
nothing** — it only reads the receipts already on disk and classifies the
subject tree. The two are complementary: Stage 4 produces the proof; Stage 6
checks it is present and current at the moment of delivery.

Boundaries, stated explicitly:

- **Read-only, no transport change.** Stage 6 detects and gates only; it never
  silently excludes paths from delivery, and the delivered content composition
  is unchanged.
- **No wire change.** No mode flag, schema, or gate primitive moves; the
  `commit_decision` schema's required keys and status enum are untouched
  (`verification_blocked` is a resolve-time return with `action="none"`, never
  persisted through the schema-validated artifact). Only additive keys land on
  the decision artifact / `session['commit_delivery']`
  (`verification_policy`, `verification_missing` / `_failed` / `_stale`,
  `generated_garbage_paths`), present only when non-empty and accepted by the
  validator as optional audit fields.

## Stage 7: cross-repo receipt graph

Stage 7 ([ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md),
`pipeline/verification_dependencies.py`) gives a command-receipt a memory of the
**cross-repo subjects** it was tested against. Through Stage 6 a receipt recorded
only the run worktree's git provenance (`{checkout}` HEAD / baseline /
fingerprint); it could not name *which commit* of a `{dependency:name}` checkout
a command ran against, so the cross-repo summary in [example (e)](#e-cross-repo-dependency-graph)
was only a future goal.

What Stage 7 does:

- **Per-dependency provenance.** `run_command` captures, for **each declared
  `dependency_repos` entry**, a record `{name, path, head, dirty,
  changed_files_count, changed_files_fingerprint, depends_on}` and writes it as a
  top-level `dependencies` array on the command-receipt — a **sibling** of `git`
  (which stays the subject checkout's own differential lens). The receipt schema
  is bumped **1 → 2**. See
  [The command-receipt (Stage 3)](#the-command-receipt-stage-3) for the v2 shape.
- **`depends_on` per command.** A dependency is marked `depends_on: true` exactly
  when its resolved path is a path-prefix (with an `os.sep` boundary — not a bare
  substring, so `/repo/dep` matches `/repo/dep/bin` but not `/repo/department`)
  of a resolved `argv` token, the effective cwd, the interpreter, or an
  `env_overrides` value. A declared dependency a command never references is
  recorded with `depends_on: false` and never drives staleness.
- **Bounded HEAD-only staleness.** When a `depends_on: true` dependency's HEAD
  has moved since the receipt was written, the shared `classify_required_receipts`
  marks that receipt **stale** with a reason naming the dependency and both SHAs
  (`dependency orcho-core HEAD moved <old> -> <new>`). The status vocabulary is
  unchanged (`present/missing/failed/stale`); the reason is additive and shows in
  the Stage 5 render and the Stage 6 delivery `lines`.
- **CLI / readiness surface.** `orcho verify run` adds an `against:
  <name>@<short-head>` line (with `+dirty`) per depended-on dependency, and the
  Stage 5 readiness block shows a *Tested dependency commits* line for present
  receipts.

Boundaries, stated explicitly:

- **No writes into dependency repos.** Capture is read-only git (`rev-parse` /
  `status`) over the declared dependency paths; receipts are written only under
  `run_dir`, never into a dependency checkout or the source tree.
- **Narrow invalidation.** Only a **HEAD move** on a **`depends_on: true`**
  dependency makes a receipt stale. A `depends_on: false` HEAD move, an old v1
  receipt with no `dependencies` block, an unreadable current HEAD (no path / not
  git), and a **dirty-only** change all stay `present` — dirty is informational,
  never a stale trigger.
- **No dependency file paths recorded.** The dirty summary is bool / count /
  fingerprint only; a dependency's changed file *names* never enter this repo's
  receipts.
- **Never-raise.** Every git/IO failure degrades (a field becomes `null`,
  staleness is not asserted), as in every Stage 2–6 module. Cost is
  `O(declared dependencies)` git calls — no recursion, no workspace scan.
- **One-directional import graph.** `changed_files_fingerprint` moves into the
  low-level `pipeline/verification_dependencies.py` (stdlib + `core.io.git_helpers`
  + `PlaceholderContext` only); `verification_command` and `verification_readiness`
  import *from* it and it never imports *them*, top-level or lazily.
- **No wire change.** The `dependencies` block is out-of-wire by physical
  location, and `summarize_command_receipts` (the evidence digest) deliberately
  omits it — pinned by a falsifier test. The audit keys on the commit-decision
  artifact keep using command names only. See the MCP-wire falsifier in
  [ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md#mcp-wire-falsifier).

## Stage 8: parent-receipt continuity

Stage 8 ([ADR 0089](../adr/0089-delivery-receipt-continuity.md),
`pipeline/verification_receipt_index.py`) lets a **correction follow-up** inherit
the parent run's verification proof for the *same* diff. A follow-up reuses the
parent's retained worktree (ADR 0088), so it delivers the parent's exact
changed-files set; but through Stage 7, readiness and the delivery gate read
command-receipts from **one** run dir — the current run's. When the follow-up
re-runs nothing (the proof already exists in the parent), its own
`verification_command_receipts/` is empty, and both surfaces falsely report the
required receipts **missing** — the `20260612_213530 → 20260612_225347` incident.

**Receipt search path.** `classify_required_receipts` now searches an ordered
path: the **current run first**, then the follow-up's parent run(s) (closest
parent first). Parent sources travel under a single documented `state.extras` key
`verification_parent_runs` — an ordered tuple of `(run_id, run_dir)` pairs stamped
by `build_pipeline_state` from the follow-up's parent run. Both consumers —
`build_final_acceptance_readiness` (Stage 5, via `review_support`) and
`assess_delivery_verification` (Stage 6, via `run.py`) — read the **same** key, so
the two surfaces are *constructively guaranteed* to agree on the
missing/failed/stale partition (closed by an agreement test).

**Candidate-priority rule** (per required command, all classified against the
*current* subject identity):

1. **Current present wins** — a present current-run receipt is chosen.
2. **A fresh same-diff failure blocks inheritance** — a current-run `failed`
   receipt whose fingerprint matches the current diff (or is unrecorded) is
   reported, and **no parent is consulted**. A follow-up exists because the
   parent's work needed fixing; an older parent `pass` must never mask a fresh
   failure on the same diff — the **"never falsely green"** invariant.
3. **Otherwise inherit a valid parent** — when the current receipt is absent,
   stale, or `failed` against a *different* fingerprint, the first env-eligible
   parent that classifies `present` against the current subject is inherited
   (with parent provenance).
4. **Most informative fallback** — else report the current receipt's
   classification, or the first eligible parent's (e.g. `stale` naming the
   fingerprint move).
5. **Missing** — no usable receipt anywhere.

**Five conditions to accept a parent receipt** (all required): the command id
matches; the command's declared `env` (when non-empty) equals the receipt's
`env`; `exit_code == 0` with clean assertions/`detail`; the receipt's
changed-files fingerprint and the current subject fingerprint are **both known
and equal** — a stricter rule than the current-run classifier, so a parent that
recorded no fingerprint, or one evaluated when the current fingerprint is
unavailable, is reported `stale` rather than inherited (never falsely green); and
no depended-on dependency HEAD drift (`dependency_stale_reason` empty). An env
mismatch disqualifies the candidate outright.

**Provenance.** Every classification carries `{command, source_run_id, path,
status}` (additive `ReceiptClassification.source_run_id` / `.path`). Stage 5
renders an *Inherited receipt provenance* line (`<command>: <status> from run
<source_run_id> (<path>)`) only for receipts inherited from a parent, so a
no-parent block is byte-identical.

**Diagnostics.** When proof is genuinely missing/stale/failed, both the readiness
block and the delivery banner print the **searched run dirs** (current + parents)
and the exact, copy-paste hints
`orcho verify env --env <env> --run-id <run_id> --project <project>` and
`orcho verify run --required --run-id <run_id> --project <project>` (one shared
`suggested_verify_commands` helper over the union `missing + stale + failed`, so
they never diverge). A bare `missing required receipts` banner with no next step
is no longer possible. The DONE/HALTED `Verification gates` block ([ADR
0095](../adr/0095-verification-gate-timeline-durable-trails.md) §4) reuses the
**same** helper for its `searched run dirs` / `fix` lines and adds a `failed=`
residual segment, a `manual-only` line, and an `inherited` line — an additive,
render-only run-level projection that changes no policy, schema, or readiness
verdict ([ADR
0096](../adr/0096-verification-gate-timeline-operator-ux.md)).

Boundaries, stated explicitly:

- **Read-only, no writes into retained worktrees or incident run dirs.** Parent
  runs are read-only sources; receipts are never copied, rewritten, or moved, and
  no verify command is auto-run.
- **Never-raise.** Candidate loading and classification degrade on any git/IO
  failure (a source contributes nothing, staleness is not asserted), as in every
  Stage 2–7 module.
- **One verdict home.** The required-missing/failed/stale verdict exists **only**
  in `classify_required_receipts`. The status-surface inventory (ADR 0089, review
  F2) confirmed it: the evidence digest (`_build_verification_readiness`) is
  observed-facts-only (locked by a test), the `orcho verify` CLI formatters render
  execution PASS/FAIL, and `cli/` / `sdk/` / `pipeline/control/` /
  `pipeline/observability/` compute no second verdict. The receipt-index module
  loads candidates but renders no verdict.
- **No wire change.** `COMMAND_RECEIPT_SCHEMA_VERSION` stays **2** — provenance
  and inheritance are computed at *read* time, never written into receipts; the
  evidence v1 bundle keeps its shape; `verification_parent_runs` is an in-process
  `state.extras` key, not a serialized contract field. The
  `searched_run_dirs` / `suggested_commands` / `receipt_provenance` fields are
  render-only and never persisted to the schema-validated `commit_decision`
  artifact. See the MCP-wire falsifier in
  [ADR 0089](../adr/0089-delivery-receipt-continuity.md#mcp-wire-falsifier).

## Stage 9: auto-run required receipts before final acceptance

Stage 9 ([ADR 0094](../adr/0094-verification-auto-run-required-receipts.md),
`pipeline/project/verification_autorun.py`) closes the last manual detour. Stage
5/6/8 guarantee a run never goes *falsely* green, but when required receipts are
**missing or stale** the gate's only remedy was the copy-paste `orcho verify …`
hint — and `final_acceptance` is a model that cannot run shell, so that hint
leaked the work back to a **human operator** (incidents `20260613_104716` /
`20260613_125608`). Stage 9 makes the engine materialise that evidence itself.

**Overview.** Before a final phase runs, Orcho first durably selects the current
`before_delivery:` plan and then regenerates the run's missing/stale
required delivery receipts through a single shared executor
(`materialize_required_receipts`), so the `before_delivery` gate, the Stage 5
readiness render, and the Stage 6 delivery gate all read **fresh on-disk
receipts**. The executor is reused by the correction `gate_rerun` route (Stage
4.1 / ADR 0091), which now delegates to it instead of running its own pair — one
runner, two callers.

**Auto-run policy.** Targets are exactly the **delivery-selected** required
commands that classify `missing` or `stale` (via the same
`classify_required_receipts` the gates use). The executor threads the run's
**full** `state.extras` into classification, so `delivery_gate_plan` reuses the
**cached `before_delivery` routing plan** and selection context that Stage 5
readiness and the Stage 6 delivery gate read — *not* a fresh plan rebuilt from
the live worktree, and *not* `verification.required` alone. This is what makes
**path-selected delivery gates** (e.g. `cli-sdk-unit`, scheduled only because the
diff touched its paths) materialise automatically. (An earlier revision passed a
stripped `state.extras` and saw only `verification.required`, under-targeting
path-selected gates and re-leaking them to a manual `orcho verify run`; threading
the full extras fixed that, keeping auto-run targeting in lockstep with what the
gates enforce.) Explicitly **not** auto-run:

- **manual_only / operator-only** — never auto-run, even when also `required`;
  recorded in `skipped_manual` and left as an explicit operator escape-hatch. The
  raw manual set comes from `manual_or_operator_only_commands` *before* the
  `verify run` subtraction of `required`, so a `required` + `manual_only` command
  stays manual.
- **fresh / present** — left untouched, recorded in `skipped_fresh`, never
  executed (incl. an inherited valid parent receipt, Stage 8).
- **failed** — remains authoritative for its recorded subject. The sole narrow
  exception is a failed **current-run** official receipt whose usable recorded
  typed subject is proved `STALE` against the usable current checkout subject;
  that selected engine-owned command may join this one target pass ([ADR
  0141](../adr/0141-subject-aware-refresh-of-failed-verification-receipts.md)).
  Same, legacy, malformed, unavailable, absent, or inherited subjects never
  qualify. A failure produced by the pass remains `failed` and is reported in
  `result.failed`; execution-first receipt reporting does not change.
- **dry-run / no contract / empty resolved delivery-required set** — strict no-op
  (`attempted=False`, nothing executes). The empty-set check fires *after* the
  delivery plan resolves, so an empty static `verification.required` with a
  non-empty path-selected delivery set still materialises.

Execution is strictly through `sdk.verify.verify_env` / `verify_run` — one env
pass per needed env, one command pass over the targets, **no retry loop**. Any
executor failure degrades into `errors` and is never raised: `final_acceptance` /
delivery remain the authoritative verdict.

**Integration point.** `pipeline/project/run.py::_on_phase_pre` resolves the
durable `before_delivery:` epoch and calls the thin run-adapter
`auto_run_required_receipts(self, name, reason=…, delivery_plan=…)` when
`name in FINAL_PHASES` (`final_acceptance` / `compliance_check`), **after** the
correction-route skip check and **before** `evaluate_pre_phase_gates`. The adapter
resolves `ctx` from `state.extras['verification_placeholders']` (else builds it
via `placeholder_context_for`) and parent sources from `verification_parent_runs`
— the same `state.extras` keys Stage 8 uses — and keys classification off
`state.extras['verification_contract']` while `sdk.verify` canonically reloads the
contract from the project's `plugin.py` (the accepted provenance invariant: the
executor passes a `project_dir` matching the run's `meta['project']`, so both
resolve the same contract).

The ledger write precedes plan publication and `sdk.verify` execution. The later
`before_delivery` hook replays the same selected identities and reuses fresh
receipts; it never recomputes selection or executes an already materialized
engine-owned command a second time. Correction pre-review uses the materializer
without selecting this delivery epoch.

**Durable evidence.** `auto_run_required_receipts` records an **append-only**
list at `state.extras['verification_autorun']` (one
`ReceiptAutoRunResult.to_evidence()` entry per triggering final phase) and mirrors
the same entry per-phase at `session['phase_log'][phase]['verification_autorun']`.
Guard no-ops record nothing. No wire change: this is an in-process `state.extras`
key, not a serialized contract field, and `COMMAND_RECEIPT_SCHEMA_VERSION` stays
**2**.

**Manual commands are now a fallback.** With Stage 9, the auto-run is the normal
route to green required evidence. The manual `orcho verify env` / `orcho verify
run` CLI (below) remains a **fallback / escape-hatch** — for operator-only
commands the engine will not auto-run, and for out-of-band debugging — not the
happy path it used to be when missing receipts forced an operator into the loop.

## Plugin shape draft

The following `PLUGIN` dict is **illustrative syntax**, not the final schema.
It shows where each field lives and what it owns:

```python
PLUGIN = {
    "dependency_repos": {
        "orcho-core": {
            "path": "/path/to/orcho/orcho-core",
            "required": True,
        },
    },
    "worktree_bootstrap": [
        {
            "run": [
                "{dependency:orcho-core}/.venv/bin/python",
                "-m", "pip", "install", "-e", ".[dev]",
            ],
            "when": {"missing_import": "orcho_mcp"},
        },
    ],
    "verification_envs": {
        "canonical-core": {
            "python": "{dependency:orcho-core}/.venv/bin/python",
            "env": {"PYTHONPATH": "src:."},
            "assertions": [
                {"import": "pipeline", "path_equals": "{dependency:orcho-core}/pipeline/__init__.py"},
                {"import": "sdk", "path_equals": "{dependency:orcho-core}/sdk/__init__.py"},
                {"import": "orcho_mcp", "path_under": "{checkout}/src"},
                {"import": "tests.fixtures.mcp_workspace", "path_under": "{checkout}/tests"},
            ],
        },
    },
    "verification": {
        "default_env": "canonical-core",
        "required": ["lint", "architecture", "mcp-smoke"],
        # Stage 6 delivery gate (ADR 0083). Optional; manual|suggest|warn|require.
        # Omitted with a contract declared → effective `warn`. `require` (a hard
        # non-interactive block on missing/failed/stale receipts or generated
        # garbage) is only ever reached by this explicit value — work_mode never
        # escalates it.
        "delivery_policy": "warn",
        "commands": {
            "lint": {"env": "canonical-core", "run": ["python", "-m", "ruff", "check", "."]},
            "architecture": {
                "env": "canonical-core",
                "run": ["python", "-m", "pytest", "-q", "tests/unit/architecture"],
            },
            "mcp-smoke": {
                "env": "canonical-core",
                "run": [
                    "python", "-m", "pytest", "-q",
                    "tests/acceptance/mock_pipeline/test_smoke_matrix.py",
                    "-m", "mcp_integration", "-o", "addopts=",
                ],
            },
        },
        "schedule": [
            {"after_phase": "implement", "commands": ["lint"], "policy": "warn"},
            {"before_phase": "final_acceptance", "commands": ["lint", "architecture"], "policy": "warn"},
            {"before_delivery": True, "commands": ["mcp-smoke"], "policy": "warn"},
        ],
    },
}
```

The five contract fields above (`dependency_repos`, `verification_envs`,
`verification.commands`, `verification.schedule`, `work_mode`) are read by core
today as a **read-only Stage 1 projection**
([ADR 0077](../adr/0077-verification-contract-read-only-projection.md)): loaded,
validated when declared, and surfaced in the run header and per-phase prompt
blocks. The remaining detail (env `assertions`, command `env`/`run` execution,
`when` bootstrap predicates) is illustrative and not executed.

Stage 2 ([ADR 0078](../adr/0078-verification-contract-env-assertions.md)) closes
the env-`assertions` execution gap: a single env's assertions are executed on
demand via `orcho verify env`. Stage 3
([ADR 0080](../adr/0080-verification-contract-command-receipts.md)) closes the
command-`run` execution gap: declared `verification.commands` are executed on
demand via `orcho verify run`, each writing a command-receipt. Stage 4
([ADR 0081](../adr/0081-verification-contract-scheduling-and-repair-routing.md))
closes the *scheduled / blocking* gap: the schedule's `require` gates now block
and route via the policy algebra and the repair_loop matrix (see
[Stage 4](#stage-4-scheduling-policy-algebra-and-repair-routing)). Still open: the
`prompt_policy` **override chain** (workspace → project → run/profile) — both
stages ship only the code-owned default.

<!-- TODO(orcho-verification-stage4): document the prompt-policy override chain
once a later stage adds it. Scheduling/gating execution is done (ADR 0081). -->

The product split of the fields:

```text
dependency_repos:      what other checkouts are part of the subject
worktree_bootstrap:    how to make the checkout runnable
verification_envs:     valid command contexts (where + against what)
verification.commands: authoritative native invocations (what must be true)
verification.schedule: when commands are suggested, run, checked, or blocking
work_mode:             user-facing default for gate strictness and loops
prompt_policy:         how Orcho shapes phase prompts from the setup (Orcho-owned)
receipts:              durable proof of what ran
```

## Examples

### (a) Python cross-repo: orcho-mcp against canonical orcho-core

The motivating case. `orcho-mcp` is the checkout; `orcho-core` is a canonical
dependency. The `canonical-core` environment asserts that `pipeline` / `sdk`
import from the canonical core checkout while `orcho_mcp` / test fixtures import
from `{checkout}`:

```python
PLUGIN = {
    "dependency_repos": {
        "orcho-core": {"path": "/path/to/orcho/orcho-core"},
    },
    "verification_envs": {
        "canonical-core": {
            "python": "{dependency:orcho-core}/.venv/bin/python",
            "env": {"PYTHONPATH": "src:."},
            "assertions": [
                {"import": "pipeline", "path_equals": "{dependency:orcho-core}/pipeline/__init__.py"},
                {"import": "sdk", "path_equals": "{dependency:orcho-core}/sdk/__init__.py"},
                {"import": "orcho_mcp", "path_under": "{checkout}/src"},
                {"import": "tests.fixtures.mcp_workspace", "path_under": "{checkout}/tests"},
            ],
        },
    },
    "verification": {
        "default_env": "canonical-core",
        "commands": {
            "lint": {"env": "canonical-core", "run": ["python", "-m", "ruff", "check", "."]},
            "mcp-smoke": {
                "env": "canonical-core",
                "run": [
                    "python", "-m", "pytest", "-q",
                    "tests/acceptance/mock_pipeline/test_smoke_matrix.py",
                    "-m", "mcp_integration", "-o", "addopts=",
                ],
            },
        },
    },
}
```

A bare host `python -c "import pipeline"` resolving to the stable install is
exploratory and expected; the authoritative proof is the `canonical-core`
environment with its import assertions.

### (b) C# / ATAS with ignored `libs/`

The build needs gitignored native libraries copied into the worktree before
`dotnet restore`. `worktree_bootstrap` (implemented today) handles the copy;
verification commands wrap the native `dotnet` invocations:

```python
PLUGIN = {
    "worktree_bootstrap": [
        {"copy": "libs"},
        {"run": ["dotnet", "restore"]},
    ],
    "verification": {
        "commands": {
            "build": {"run": ["dotnet", "build"]},
            "test": {"run": ["dotnet", "test"]},
        },
    },
}
```

The ignored `libs/` never appears in the canonical repo's tracked tree; bootstrap
restores it into `{checkout}` so the build is reproducible.

### (c) PHP with composer install and vendor/bin/phpunit

The project pins its test runner to a local `vendor/bin/phpunit`, not a global
`phpunit`. Bootstrap installs dependencies; the test command uses the local
binary:

```python
PLUGIN = {
    "worktree_bootstrap": [
        {"run": ["composer", "install"]},
    ],
    "verification": {
        "commands": {
            "install": {"run": ["composer", "install"]},
            "test": {"run": ["vendor/bin/phpunit"]},
        },
    },
}
```

### (d) Node with a local package manager

The project requires a pinned package manager and a local `node_modules`, not a
globally installed CLI. Bootstrap restores `node_modules`; the test command runs
through the local install:

```python
PLUGIN = {
    "worktree_bootstrap": [
        {"run": ["npm", "ci"]},
    ],
    "verification": {
        "commands": {
            "test": {"run": ["npm", "test"]},
            "typecheck": {"run": ["npm", "run", "typecheck"]},
        },
    },
}
```

### (e) Cross-repo dependency graph

A multi-repo change spanning `api`, `web`, and `shared`. Each repo gets its own
environment, and receipts record which dependency checkout each command tested:

```python
PLUGIN = {
    "dependency_repos": {
        "api": {"path": "../api"},
        "web": {"path": "../web"},
        "shared": {"path": "../shared"},
    },
    "verification_envs": {
        "api": {"cwd": "{dependency:api}", "env": {"SHARED_DEV": "{dependency:shared}"}},
        "web": {"cwd": "{dependency:web}"},
    },
    "verification": {
        "commands": {
            "api-test": {"env": "api", "run": ["dotnet", "test"]},
            "web-test": {"env": "web", "run": ["npm", "test"]},
            "contract": {"env": "api", "run": ["dotnet", "test", "--filter", "Contract"]},
        },
    },
}
```

Orcho can then present a cross-repo receipt summary that names the dependency
HEADs each command ran against:

```text
Cross-repo verification:
  api-test    passed against api@abc123 + shared@def456
  web-test    passed against web@aaa111 + shared@def456
  contract    missing
```

No agent has to remember which dependency was under test; the receipt says it.
**Implemented (Stage 7, [ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md)):**
each command-receipt now records a per-dependency `dependencies` block, and
`orcho verify run` prints the `against: <name>@<short-head>` line for the
dependencies a command actually used. A depended-on dependency's HEAD move marks
the receipt stale — see [Stage 7](#stage-7-cross-repo-receipt-graph).

## Exploratory commands vs authoritative receipts

The reviewer policy that resolves the original dispute:

```text
A readiness finding must be based on a missing, failed, or invalid declared
verification receipt.

An ad-hoc command mismatch is a note unless it proves that a declared receipt
is invalid.
```

Concretely: a reviewer running `python -c "import pipeline"` and seeing the
stable install is **not** a blocker — that is an exploratory host command using
a different environment. The blocker is `verification env canonical-core failed`
or `mcp-smoke receipt missing / failed / stale`. This stops the loop where a
reviewer rejects readiness because a host command "sees a different world" than
the declared subject under test.

## Implemented today vs proposed

As of **Stage 1** ([ADR 0077](../adr/0077-verification-contract-read-only-projection.md))
core loads, validates, and *projects* the contract read-only. Be honest about
the boundary: "read-only Stage 1 projection" means the field is loaded and
surfaced (header + per-phase prompt blocks), **not** that any command runs, any
receipt is written, or any transition is blocked.

| Concept | Status |
|---|---|
| `worktree_bootstrap` | **Implemented (execution)** — `PluginConfig.worktree_bootstrap`, [ADR 0074](../adr/0074-worktree-bootstrap.md) |
| `worktree_teardown` + `ORCHO_ISOLATION_ID` | **Implemented (execution)** — `PluginConfig.worktree_teardown`, [ADR 0131](../adr/0131-worktree-teardown-and-isolation-id.md) |
| verification-environment receipt | **Implemented (execution)** — `pipeline/evidence/verification_receipt.py`, [ADR 0076](../adr/0076-durable-verification-environment-receipt.md) |
| env-assertion execution + receipt | **Implemented (execution, Stage 2)** — `pipeline/verification_env.py` + `verification_env_receipts/`, [ADR 0078](../adr/0078-verification-contract-env-assertions.md) |
| command execution + command-receipt | **Implemented (execution, Stage 3)** — `pipeline/verification_command.py` + `verification_command_receipts/`, [ADR 0080](../adr/0080-verification-contract-command-receipts.md) |
| `dependency_repos` | **Implemented (read-only Stage 1 projection)** — [ADR 0077](../adr/0077-verification-contract-read-only-projection.md) |
| `verification_envs` | **Implemented (read-only Stage 1 projection)** — [ADR 0077](../adr/0077-verification-contract-read-only-projection.md) |
| `verification.commands` | **Implemented (execution, Stage 3)** — projected in Stage 1, executed via `orcho verify run`; [ADR 0080](../adr/0080-verification-contract-command-receipts.md) |
| `verification.required` (list of command names) | **Implemented (Stage 3)** — validated list of declared names; drives `verify run --required`; [ADR 0080](../adr/0080-verification-contract-command-receipts.md) |
| `parity` (absolute/differential) | **Implemented (Stage 3)** — validated enum per command; differential lens on the receipt; [ADR 0080](../adr/0080-verification-contract-command-receipts.md) |
| `verification.schedule` (+ optional `policy`/`action`/`gate_sets`) | **Implemented (ADR 0132 foundation)** — validated normalized identities; executor adoption remains scheduled-gates task 2. |
| `verification.gate_sets` / `verification.selection` | **Implemented (ADR 0132 foundation)** — deterministic selection and defaults merge feed `ScheduledGatePlan`; durable disposition migration remains task 3. |
| `work_mode` (fast/pro/governed) | **Implemented (ADR 0132 foundation)** — exact policy projection; action/executor adoption is not part of this foundation. |
| final-acceptance readiness summary | **Implemented (read-only, Stage 5)** — `pipeline/verification_readiness.py` prompt block (present/missing/failed/stale required receipts, env status, exploratory count) + additive evidence `verification_readiness` digest; [ADR 0082](../adr/0082-verification-contract-final-acceptance-readiness.md) |
| `verification.delivery_policy` (manual/suggest/warn/require) | **Implemented (foundation vocabulary)** — absent contract stays `None`; declared contract defaults to `warn`; executor/durable migration is out of scope. |
| delivery gate awareness | **Implemented (Stage 6)** — `pipeline/verification_delivery.py` warns/blocks delivery on missing/failed/stale required receipts + generated garbage (classified separately from the product diff); decision status `verification_blocked`, halt `commit_delivery_verification_blocked`; reuses Stage 5 `classify_required_receipts`; [ADR 0083](../adr/0083-verification-contract-delivery-gate-awareness.md) |
| cross-repo dependency provenance + stale | **Implemented (Stage 7)** — `pipeline/verification_dependencies.py`; command-receipt schema v2 records a per-`dependency_repos` `dependencies` block (name/path/HEAD/dirty-summary/`depends_on`), and a depended-on dependency's HEAD move marks the receipt stale at Stage 5/6 (HEAD-only, `depends_on` only; degrades, never raises). Evidence v1 / MCP wire unchanged (falsifier); [ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md) |
| `prompt_policy` | Partially implemented — Orcho default projection live (Stage 1 raw schedule / Stage 4 resolved plan); override chain still proposed ([ADR 0081](../adr/0081-verification-contract-scheduling-and-repair-routing.md)) |
| CLI `orcho verify env` | **Implemented (Stage 2)** — executes one env's assertions, writes an env-assertion receipt; [ADR 0078](../adr/0078-verification-contract-env-assertions.md) |
| CLI `orcho verify list` / `orcho verify run` | **Implemented (Stage 3)** — list projects declared commands; run executes them and writes command-receipts; [ADR 0080](../adr/0080-verification-contract-command-receipts.md) |
| CLI `orcho workspace fine-tune --dry-run` | **Implemented (Stage 2)** — prints a candidate contract, writes nothing; [ADR 0078](../adr/0078-verification-contract-env-assertions.md) |

### The receipt that exists today

The authoritative receipt as **actually written** by
`pipeline/evidence/verification_receipt.py` is a flat JSON object with this
shape:

```json
{
  "phase": "repair_changes",
  "round": 1,
  "kind": "verification_environment",
  "cwd": "/abs/path",
  "python": "3.12.4 (/abs/.../python)",
  "checks": [
    {"name": "pipeline_import", "expected": "/abs/.../pipeline/__init__.py", "actual": "/abs/.../pipeline/__init__.py", "passed": true}
  ],
  "commands": [
    {"argv": ["/abs/.../python", "-c", "import pipeline, sys; sys.stdout.write(pipeline.__file__)"], "exit_code": 0}
  ],
  "temp_env_outside_checkout": true
}
```

Field contract as implemented:

- `phase` — the writing phase (`implement` or `repair_changes`).
- `round` — 1-based phase round.
- `kind` — the literal `"verification_environment"`.
- `cwd` — absolute working directory the checks ran in.
- `python` — interpreter identity (`<version> (<executable>)`).
- `checks` — list of `{name, expected, actual, passed}`.
- `commands` — list of `{argv, exit_code}`.
- `temp_env_outside_checkout` — boolean; the throwaway environment lived outside
  the source checkout.

Receipts are written to:

```text
<run_dir>/verification_receipts/<phase>_round<N>.json
```

> Note: [ADR 0076](../adr/0076-durable-verification-environment-receipt.md)
> illustrates `checks` as `{name, ok, detail}` and `commands` as raw strings.
> The shipped writer evolved to the `{name, expected, actual, passed}` /
> `{argv, exit_code}` form documented above; treat the code as authoritative.

### The env-assertion receipt (Stage 2)

`orcho verify env` ([ADR 0078](../adr/0078-verification-contract-env-assertions.md))
writes a **second, distinct** receipt kind when it executes one env's declared
assertions. It is written by `write_env_assertion_receipt` to:

```text
<run_dir>/verification_env_receipts/verify_env_<env>.json
```

This directory is **deliberately separate** from the ADR 0076
`verification_receipts/` directory. The evidence collector reads only
`verification_receipts/`, so the env-assertion receipt
(`kind: "verification_env_assertions"`) is kept out of the schema-validated
evidence v1 bundle by *physical location*, not by filtering. The shape:

```json
{
  "kind": "verification_env_assertions",
  "env": "ci",
  "subject": {"checkout": "/abs/checkout", "project": "/abs/project"},
  "cwd": "/abs/effective-cwd",
  "interpreter": "3.12.4 (/abs/.../python)",
  "env_overrides": {"PYTHONPATH": "src:."},
  "assertions": [
    {"name": "pipeline", "kind": "import_path_equals",
     "expected": "/abs/.../pipeline/__init__.py",
     "actual": "/abs/.../pipeline/__init__.py", "passed": true, "detail": ""}
  ],
  "all_passed": true,
  "temp_env_outside_checkout": true
}
```

Like the ADR 0076 receipt, it is written **only** under `run_dir`, never the
checkout. The `cwd` field records the *effective* cwd the assertions ran in
(the declared checkout by default), so the receipt proves the declared subject.

### The command-receipt (Stage 3)

`orcho verify run` ([ADR 0080](../adr/0080-verification-contract-command-receipts.md))
writes a **third, distinct** receipt kind when it executes a declared command.
It is written by `write_command_receipt` to:

```text
<run_dir>/verification_command_receipts/<command>.json
```

This directory is **deliberately separate** again from both
`verification_receipts/` (ADR 0076, the evidence-collected kind) and
`verification_env_receipts/` (ADR 0078). The evidence collector reads only
`verification_receipts/`, so the command-receipt (`kind:
"verification_command"`) is kept out of the schema-validated evidence v1 bundle
by *physical location*, not by filtering (see
[the MCP-wire falsifier in ADR 0080](../adr/0080-verification-contract-command-receipts.md#mcp-wire-falsifier-t9)).
The shape, carrying a `schema_version` (now **2** — Stage 7 added the
`dependencies` block; [ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md)):

```json
{
  "schema_version": 2,
  "kind": "verification_command",
  "command": "lint",
  "env": "canonical-core",
  "cwd": "/abs/eff-cwd",
  "placeholders": {"checkout": "/abs/run-worktree", "project": "/abs/canonical"},
  "argv": ["/abs/.../python", "-m", "ruff", "check", "."],
  "env_overrides": {"PYTHONPATH": "src:."},
  "assertions": [{"name": "...", "kind": "...", "passed": true, "detail": ""}],
  "exit_code": 0,
  "duration_s": 0.42,
  "stdout_tail": "…last N chars…",
  "stderr_tail": "",
  "log_path": "/abs/.../verification_command_receipts/lint.log",
  "parity": "absolute",
  "detail": "",
  "git": {
    "checkout_head": "<HEAD of {checkout} = run worktree>",
    "baseline_head": "<meta.worktree.base_ref, differential gate>",
    "changed_files_fingerprint": "<sha256[:16] of sorted changed files in {checkout}>"
  },
  "dependencies": [
    {
      "name": "orcho-core",
      "path": "/abs/canonical/orcho-core",
      "head": "<HEAD of the dependency checkout, or null>",
      "dirty": false,
      "changed_files_count": 0,
      "changed_files_fingerprint": "<sha256[:16], or null>",
      "depends_on": true
    }
  ]
}
```

Field notes:

- `cwd` is the command's effective working directory (`eff_cwd`) — the declared
  env `cwd`, defaulting to `{checkout}`. It is **only** where the subprocess ran.
- `placeholders` records the two subjects: `checkout` (run worktree) and
  `project` (canonical repo).
- `parity` is the validated enum (`absolute` | `differential`, default
  `absolute`). For a `differential` command the `git` block carries both
  `checkout_head` and `baseline_head` so a gate can compare the run worktree's
  HEAD against its base ref.
- `git.*` is the **differential lens**, always taken from the subject checkout
  (`{checkout}`), never from `cwd`.
- `dependencies` (Stage 7, schema v2) is a **sibling** of `git`: one entry per
  declared `dependency_repos`, in name order. `head` is the dependency
  checkout's HEAD (`null` when not a git repo / git failed); the dirty summary
  (`dirty` / `changed_files_count` / `changed_files_fingerprint`) is bool / count
  / fingerprint only — **never** the dependency's file *paths* — and all three
  are `null` when `head` is `null`. `depends_on` is `true` exactly when the
  dependency's resolved path is a path-prefix (with an `os.sep` boundary) of a
  resolved `argv` token, `eff_cwd`, the interpreter, or an `env_overrides` value.
  A `depends_on: true` dependency whose HEAD later moves makes the receipt stale
  (HEAD-only; dirty never triggers it) — see
  [Stage 7](#stage-7-cross-repo-receipt-graph).

#### Command cwd vs git subject checkout

The single subtlety of Stage 3, mirroring the canonical/checkout split:

| | Command `cwd` (`eff_cwd`) | Git subject checkout (`{checkout}`) |
|---|---|---|
| What it is | Declared env `cwd` (may be `{project}`, a dependency dir, or a subdir) | The run worktree under test |
| Recorded as | `receipt.cwd` | `git.checkout_head` / `git.changed_files_fingerprint` |
| Role | Where the subprocess actually ran | Which checkout the proof is *about* |

A command may run from `{project}` or a dependency dir while still proving a
change made in the run worktree. Taking git provenance from `cwd` would let a
differential receipt attribute a baseline diff to the wrong subject; core
therefore always computes `checkout_head` / `changed_files_fingerprint` from
`{checkout}` and draws `baseline_head` from `meta['worktree']['base_ref']` — the
same subject.

### Cross-repo dependency provenance (Stage 7)

Recorded **dependency repo HEADs** in a command-receipt — so a cross-repo
receipt names exactly which `{dependency:name}` checkout each command ran against
(the `api@abc123 + shared@def456` summary above) — are **implemented as of
Stage 7** ([ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md)).
The receipt schema is now **v2**: alongside the single-subject `git` block, the
receipt carries a `dependencies` array with one entry per declared
`dependency_repos` (`name` / `path` / `head` / dirty-summary / `depends_on`) —
see the v2 shape in [The command-receipt (Stage 3)](#the-command-receipt-stage-3).
A depended-on dependency's HEAD move marks the receipt stale at the Stage 5
readiness and Stage 6 delivery surfaces (HEAD-only, `depends_on` only; dirty and
absent-block receipts never become stale), and `orcho verify run` prints the
`against: <name>@<short-head>` line — see
[Stage 7](#stage-7-cross-repo-receipt-graph).

## CLI: `orcho verify list` / `orcho verify run` (Stage 3)

Stage 3 ([ADR 0080](../adr/0080-verification-contract-command-receipts.md)) adds
two subcommands alongside `orcho verify env`. Both resolve a run, confirm it
belongs to the project, and load the contract from the canonical project; the
declared commands then resolve against the **recorded physical subject**. An
isolated subject must be its recorded readable worktree; missing or ambiguous
metadata fails closed rather than falling back to the canonical project.
For a correction child observed before its own reused-worktree block is
persisted, the resolver follows the durable correction parent lineage to the
nearest recorded retained identity. That lineage must be acyclic and stay
within the same canonical project; otherwise resolution still fails before any
command or receipt write ([ADR 0135](../adr/0135-sdk-profile-authority-and-correction-verification-lineage.md)).

```bash
orcho verify list [-p PROJECT] [--run-id ID] [-w WORKSPACE]
orcho verify run [names...] [--required] [-p PROJECT] [--run-id ID] [-w WORKSPACE]
```

- **`verify list`** is a pure projection: it prints each declared command's
  name, env, required marker, and placeholder-resolved `run` text. It executes
  nothing and writes no receipt. Exit 0 on success, 2 on a resolution error.
- **`verify run`** executes declared commands natively (argv via `subprocess`,
  no shell, no wrapper-bin) in the run worktree and writes one
  [command-receipt](#the-command-receipt-stage-3) per command under
  `<run_dir>/verification_command_receipts/`. With no `names` every declared
  command runs; positional `names` select explicit commands; `--required` runs
  exactly `verification.required`. **There is no `--env` flag** — a command's
  env is its declared `env` (else `default_env`).

Exit codes: **0** when every command exited 0, **1** when a command exited
non-zero, **2** for a resolution error (project↔run mismatch, missing contract,
unknown command name, empty/missing `required` set) — and on a `2` nothing is
written. The `verify run` formatter labels its output as official declared
command-receipts and, for a `differential` command, shows `checkout_head` vs
`baseline_head`.

Since [Stage 9](#stage-9-auto-run-required-receipts-before-final-acceptance) the
engine auto-materialises missing/stale **required** receipts before final
acceptance, so these subcommands are now a **fallback / escape-hatch** — for
operator-only commands the engine deliberately does not auto-run, and for
out-of-band debugging — rather than the normal route to a green run.

## CLI: `orcho quality-gates`

`orcho quality-gates` is a **strictly read-only** inspector that prints a
project's *declared* verification gate matrix — the same matrix the run-header
banner shows at run start. It never starts a run, executes a gate command, or
writes a receipt: it only loads the project plugin, validates the declared
contract, projects it through the shared gate ledger
(`pipeline.verification_ledger.build_gate_ledger`), and renders it with the
**same** formatter the banner uses (`render_gate_matrix`). Because both surfaces
render through that one helper, the printed matrix is identical to the banner —
there is no second projection to drift.

```bash
orcho quality-gates [--profile WORK_KIND] [--paths GLOBS...] [--project PROJECT]
```

- **`--profile <work_kind>`** resolves the named profile from the shipped
  catalogue and derives whether that profile has a final delivery phase (its
  phases ∩ the `FINAL_PHASES` set). This decides the `when` axis for
  non-required gates (see below). An unknown profile name is an error on stderr
  with the known names listed (exit 2); it is never guessed.
- **`--paths <globs/files>`** feeds the paths to the ledger as `changed_files`,
  so a trailing *Resolution for given paths* section reports each gate's
  identity-resolved selection — `selected` or `not_selected` with a durable
  `paths`, `task_kind`, or `operator` reason; terminal state is one of the nine
  for these paths), or `manual` (an operator/manual gate). The path-matching is
  delegated wholesale to the selection engine; the command re-implements none of
  it.
- **No `--profile`** renders the declared matrix with the profile deliberately
  unknown, so non-required (`warn` / `manual`) gates read `profile-dependent`
  rather than a guessed stage. Required gates still show their own timing hook.

Exit codes: **0** on success (including the *no verification contract declared*
case — absent-but-worked), **2** on an unknown `--profile` or an invalid
declared contract. Nothing is executed and nothing is written in any mode.

### The three axes

Each gate row separates the command's identity from three orthogonal,
operator-facing axes (rendered as columns alongside `run` = auto/manual and
`kind` = declared cost):

- **`when`** — the stage the gate *actually runs at*, which the raw schedule
  hook alone cannot express. It is a pure derivation of the gate's effective
  policy, its hook/phase, and whether the profile has a final delivery phase:
  - a **`require`** gate runs inline at its timing hook, e.g.
    `after_implement` (an `after_phase(implement)` gate) or `delivery` (a
    `before_delivery` gate) — a required gate is enforced right where it is
    scheduled;
  - a **`suggest`** policy, or a `manual_only` / `on_resume` hook → `operator`:
    a human runs it, it is never part of the automatic flow;
  - a **`warn`** / **`manual`** (or otherwise non-required auto) gate is not
    enforced inline, so it surfaces only near delivery: **`pre-final`** when the
    resolved profile has a final delivery phase, **`not auto-run`** when it
    provably does not (a profile such as `fast` or `small_task` with no final
    phase — the matrix says so honestly rather than implying the gate fires),
    and **`profile-dependent`** when no `--profile` was given so the stage
    genuinely cannot be known.
- **`policy`** — the effective *declared* receipt-enforcement policy for the
  gate: `manual` / `suggest` / `warn` / `require`, or `unknown` when it would only
  resolve after the `work_mode` transform. This is the declared strictness tier,
  read from the schedule entry (else the strictest backing gate-set default) —
  distinct from `when` and from activation.
- **`activation`** — *when the gate is selected* at all, read straight from the
  contract's declared `selection` rules: `always` (an `always` rule includes a
  backing set), `on-path: <globs>` (a `paths` rule — the gate is selected only
  when the changed files match), `manual` (the `manual` execution policy; the
  rule), or `task_kind`. This keeps a path-gated gate from reading as an
  unconditional `require`: a gate can be `require` on the `policy` axis yet only
  `on-path` on the `activation` axis.

The axes are deliberately independent: `policy` says *how strict* the receipt
requirement is, `activation` says *whether the gate is selected*, and `when`
says *at which stage a selected gate is exercised*. Reading them together is how
an operator sees, for example, that a `warn`/`pre-final` lint gate and a
`require`/`after_implement` unit gate differ in both strictness and timing.

## First-run UX

The contract is optional and gradual. A new user can start with:

```bash
orcho run "fix this"
```

without writing a plugin, declaring dependency repos, naming verification
environments, or learning a new configuration surface. A project with no
verification contract behaves exactly as it does today. Verification contracts
are advanced project tuning for repos with real repeatability problems (ignored
binary dependencies, cross-repo source-under-test requirements, pinned local
tools, expensive tests, recurring reviewer disputes). Adding a contract never
flips a project to blocking behavior — that requires an explicit
project/profile/operator opt-in.

## See also

- [Quality gates](quality_gates.md) — what must be true; the gate / env /
  receipt relationship from the gate side
- [ADR 0074](../adr/0074-worktree-bootstrap.md) — worktree bootstrap (implemented)
- [ADR 0076](../adr/0076-durable-verification-environment-receipt.md) —
  durable verification-environment receipt (implemented)
- [ADR 0077](../adr/0077-verification-contract-read-only-projection.md) —
  verification contract read-only Stage 1 projection (implemented)
- [ADR 0078](../adr/0078-verification-contract-env-assertions.md) —
  Stage 2 env-assertion execution, env-assertion receipt, and the
  `orcho verify env` / `orcho workspace fine-tune` CLI (implemented)
- [ADR 0080](../adr/0080-verification-contract-command-receipts.md) —
  Stage 3 native command execution, the command-receipt (command cwd vs git
  subject checkout, `parity` enum, differential lens), and the
  `orcho verify list` / `orcho verify run` CLI (implemented)
- [ADR 0081](../adr/0081-verification-contract-scheduling-and-repair-routing.md) —
  Stage 4 scheduling, the gate-set / selection policy algebra
  (absence-vs-explicit, work_mode-derived policy and action, defaults merge),
  the repair_loop-by-hook matrix, and the implement→repair critical flow;
  with the MCP-wire-unchanged falsifier (implemented)
- [ADR 0084](../adr/0084-verification-contract-cross-repo-receipt-graph.md) —
  Stage 7 cross-repo receipt graph: the v2 command-receipt `dependencies` block,
  the `depends_on` path-prefix rule, HEAD-only bounded staleness, the
  `changed_files_fingerprint` move to `verification_dependencies.py`, and the
  evidence/MCP-wire-unchanged falsifier (implemented)
- [ADR 0094](../adr/0094-verification-auto-run-required-receipts.md) —
  Stage 9 auto-run of missing/stale required receipts before final acceptance:
  the shared `materialize_required_receipts` executor (reused by correction
  `gate_rerun`), the auto-run policy (manual/fresh/failed/dry-run/no-contract),
  the `_on_phase_pre` integration point, and the append-only
  `verification_autorun` evidence — manual `orcho verify` is now a fallback
- [ADR 0097](../adr/0097-delivery-verification-policy-ux.md) —
  policy-aware UX for the delivery/readiness verification gate: the pure
  `pipeline/verification_policy.py` per-gate effective-policy source, the
  require/warn/suggest/manual_only matrix (gap meaning + delivery default
  action), `missing required` reserved for effective `require`, warn/suggest
  shown as `shipping allowed by policy`, `manual_only` excluded from
  missing-required, and the ADR 0090 "never falsely green" invariant — with
  `resolve_delivery_policy` unchanged (render-only)
- [CLI: `orcho quality-gates`](#cli-orcho-quality-gates) — read-only inspector
  for the declared gate matrix (`--profile` / `--paths` / no-profile), rendering
  the same `render_gate_matrix` matrix the banner shows and the three
  `when` / `policy` / `activation` axes
- [Run state](run_state.md) — run-scoped state and artifact directory layout
- [Profile JSON schema](../reference/profile_schema.md) — profile authoring
  surface that a future contract projection would extend
