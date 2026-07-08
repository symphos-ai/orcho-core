# Orcho commands

Orcho is a production harness and control plane for agentic software delivery:
one task becomes an observable workflow with typed plans, gates, evidence, run
state, and cross-project coordination when you need it.

## Quick start

```bash
# First safe run: mock agents, no real API calls
orcho run --task "Add health endpoint" --project ./api --mock

# One coordinated task across several projects
orcho cross --task "Add telemetry" --projects api:./api web:./web --mock
```

`orcho help` prints a short starting map. `orcho help --verbose` prints
the full argparse dump for every subcommand.

## Command map

| Command | What it does |
|---------|-----------|
| `orcho run` | One project: plan → implement → review/repair → final QA |
| `orcho cross` | One task across several projects |
| `orcho status` | What is happening / what should I do next? |
| `orcho history` | List recent runs |
| `orcho evidence` | What happened / what proves it? |
| `orcho diff` | What changed? |
| `orcho metrics` | How much did it consume? Tokens and time |
| `orcho cost` | How much did it consume? Cost reference |
| `orcho profiles list` | List execution profiles with their phase topology |
| `orcho workflows` | List workflow profiles |
| `orcho prompts` | Inspect the resolution chain for a prompt template |
| `orcho pricing` | Inspect / refresh the pricing data used by `cost` |
| `orcho verify` | Execute declared verification-contract checks for a run |
| `orcho runtimes` | Install helper wrappers for agent runtimes |
| `orcho workspace` | Initialise and manage Orcho workspaces |
| `orcho repair-state` | Inspect and safely apply known run-state repairs |

---

## Inspection surfaces

Use the inspection commands by question, not by file shape:

| Question | Command | Leads with |
|----------|---------|------------|
| What is happening / what should I do next? | `orcho status` | current state, phase progress, attention signals, delivery state, paths |
| What happened / what proves it? | `orcho evidence` | plan contract, phase timeline, gate receipts, commands, findings, artifacts |
| How much did it consume? | `orcho metrics`, `orcho cost` | tokens, time, retries, cost-reference usage |
| What changed? | `orcho diff` | captured patch, preview, stats, path filtering |

`status` may summarize gates or delivery because they affect the next operator
move. `evidence` owns the proof record. `metrics` and `cost` own consumption.
`diff` owns the changed files.

---

## `orcho run` — single project

```bash
orcho run --task "Task description" --project /path/to/project

# Core options:
--task "..."          # task as text
--task-file task.md   # task from a file (bare NAME.md resolves from .orcho/.task-files)
--project /path       # project directory
--profile feature     # feature | complex_feature | small_task | planning | research | delivery_audit | code_review | refactor | migration | task
--mock                # simulation without API calls; can create a mock artifact for the review loop
--dry-run             # print what would happen, change nothing
--max-rounds 2        # how many implement/review/repair rounds (default: 1)
--workspace /path     # explicit workspace (default: $ORCHO_WORKSPACE / cwd discovery)
--output summary      # summary (default) | live | debug — transcript mode
--stream-output       # alias for --output live
--verbose / -v        # alias for --output debug
```

Profiles decide which phases run. `orcho profiles list` shows each
profile's exact phase topology and intent; the short version:

- `feature` — full delivery cycle with plan validation, implementation,
  review/repair, and final acceptance. The default work kind for shipped work.
- `complex_feature` — `feature` plus an extension-point compliance gate.
- `small_task` — plan → validate_plan → implement for a small direct change;
  no terminal QA loop, ship-readiness is your call.
- `planning` — produce a plan and stop for a human verdict.
- `research` — exploratory plan-only workflow.
- `delivery_audit` — review the current delivery surface and run final acceptance.
- `code_review` — focused review of the current working tree.
- `refactor` / `migration` — full-cycle recipes tuned for those work kinds.
- `task` — internal/follow-up profile that implements against an existing plan.

### Resume and plan reuse

```bash
orcho run --resume                  # resume the most recent run
orcho run --resume 20260610_144938  # resume a specific run by id
orcho run --from-run-plan 20260610_144938 --project ./api
```

- `--resume` continues an interrupted or paused run from its checkpoint,
  skipping phases that already completed.
- `--from-run-plan` starts a **new** run that inherits the parsed plan
  of a parent run: the profile skips its leading plan + validate_plan
  block and starts at implement. Mutually exclusive with `--resume`.

### Operator decisions (pauses and gates)

Some phases pause the run and wait for a human verdict — for example
the `planning` profile always pauses after `validate_plan`, and a failed
gate can pause a `feature` run. In a terminal Orcho prompts you
interactively. For non-interactive transports there are explicit flags:

```bash
--decision TARGET=DECISION   # answer a pending decision (may repeat)
--decision-feedback TEXT     # free-form feedback for a single --decision
--no-interactive             # never prompt on stdin; leave a resumable
                             # pending-decision state for MCP / CI / UI
```

### Routing models and runtimes per phase

```bash
--model MODEL                        # default implementation model
--model-plan / --model-implement / --model-review-changes / --model-repair-changes
--runtime-plan RUNTIME               # which registered agent runtime owns the phase
--runtime-implement / --runtime-review-changes / --runtime-repair-changes
```

The same routing is available permanently through environment
variables:

```bash
export MODEL_PLAN='claude-opus-4-8[1m]'
export MODEL_IMPLEMENT='claude-opus-4-8[1m]'
export MODEL_REVIEW_CHANGES=gpt-5.5
export RUNTIME_REVIEW_CHANGES=codex
```

Runtime ids include the built-ins `claude`, `claude-glm`, `codex`, and
`gemini`, plus any plugin-provided runtime registered in the environment. See
[../guides/claude_glm_runtime.md](../guides/claude_glm_runtime.md) for the GLM
wrapper setup.

Example: keep planning on Claude, then route implementation through the
Claude-compatible GLM wrapper under Codex review:

```bash
orcho run \
  --task "Implement the approved plan" \
  --project ./api \
  --runtime-plan claude \
  --runtime-implement claude-glm \
  --model-implement 'glm-5.2[1m]' \
  --runtime-review-changes codex
```

### Attachments and session control

```bash
--attach PATH          # attach a file to the task (type auto-detected)
--attach-text PATH     # force text attachment
--attach-image PATH    # force image attachment
--attach-binary PATH   # force binary attachment
--session-mode {auto,stateless,chain,hybrid}
--session-split PHASE=SPLIT   # override a phase's prompt-session split
                              # (stateless, per_phase, per_role, common)
```

See [../reference/attachments.md](../reference/attachments.md) for the
attachment model and [../reference/resume_modes.md](../reference/resume_modes.md)
for session/resume semantics.

### Transcript modes

`--output` is the single transcript-mode knob, a monotonic stack:

- `summary` (default) — phase banners, structured plan/review blocks,
  final outcome. Enough for a normal "start it and move on" run.
- `live` — `summary` plus the live agent transcript on stdout (what
  `--stream-output` used to do). `output.log` is written as usual.
- `debug` — `live` plus stderr `[TRACE]` diagnostics and untruncated
  phase previews (what `--verbose` used to do).

The `--stream-output` and `--verbose` aliases are kept for
compatibility; the canonical form is `--output {level}`. **Behavioral
change:** `--verbose` now includes the live agent transcript
(`debug ⊃ live`). Before the normalization `--verbose` did not show the
live stream; the old "trace without agent echo" combination no longer
exists (see `docs/migration/run-output-mode-flag.md`).

The `--output` default comes from `cli.output_mode` in the workspace
config (`.orcho/config.json`) when the flag is not passed. **An
explicitly passed flag always beats the config:** `orcho run --output
summary` honestly gives `summary` even when the config says
`live`/`debug`.

**Examples:**
```bash
# Normal run
orcho run --task "Add rate limiting to /api/login" --project ~/www/my-api

# Plan only (no implementation)
orcho run --task "Refactor auth module" --project ~/www/my-api --profile planning

# Review the current working tree only
orcho run --project ~/www/my-api --profile delivery_audit

# Live agent transcript on stdout
orcho run --task-file ./tasks/sprint-42.md --project ~/www/my-api --output live

# Task from a file + 2 repair rounds
orcho run --task-file ./tasks/sprint-42.md --project ~/www/my-api --max-rounds 2
```

---

## `orcho cross` — several projects

For tasks that touch several repositories at once.

```bash
orcho cross \
  --task "Add rate limiting: update API endpoint + Unity client" \
  --projects api:~/www/api unity:~/www/unity-client
```

```bash
# Options (same as run, plus):
--projects alias:/path alias2:/path2   # project list (alias:path)
--mode plan                            # stop after the cross plan
--mode full                            # full run (default)
--plan-file cross_plan.json            # start from an existing cross plan
```

---

## `orcho status` — what is happening / what should I do next?

```bash
orcho status              # latest run
orcho status <run-id>     # a specific run by id
```

Output:
```
Run: 20260503_104135
Status: DONE ✓
Phases: plan ✓  implement ✓  review_changes ✓  final_acceptance ✓
Gates: passed x2  skipped x1
Duration: 4m 32s
```

---

## `orcho history` — list of runs

```bash
orcho history             # last 10 runs
orcho history --last 25   # last 25
```

---

## `orcho metrics` and `orcho cost` — how much did it consume?

```bash
orcho metrics             # latest run
orcho metrics --last 5    # aggregated over 5 runs

orcho cost                # cost-reference usage report
orcho pricing             # inspect / refresh the pricing data behind cost
```

`orcho cost` is a **cost reference / usage accounting** view over a window of
runs — not a billing receipt. Runtime-reported dollar values come from the
active runtime/endpoint; token-only phases are priced locally and marked as
estimated.

The report groups spend two ways:

- **By phase** — cost per pipeline phase (`plan`, `implement`, …).
- **By runtime/provider** — cost summed across phases per agent. The label is
  the resolved runtime id when a run recorded one (e.g. `claude`, `claude-glm`),
  and otherwise falls back to a model→provider mapping for older runs
  (`claude` / `codex` / `gemini` / `other`).

Percentages in each breakdown are **share of that breakdown** — a row's cost
over the sum of the rows shown, so they never exceed 100%. They are not a share
of the report total (phase and runtime views sum the same money along different
axes, so a total-based percentage would look like a broken pie).

The footer names only the **estimated** entries and where their prices came
from (`~/.orcho/pricing.local.toml`, or the bundled snapshot). When that
snapshot is stale it prints an age warning suggesting `orcho pricing refresh`.

---

## `orcho diff` — what changed?

Every run writes `<run-dir>/diff.patch`. `orcho diff` renders that
artifact — it never recomputes a git diff.

```bash
orcho diff <run-id>                       # grouped per-file overview (default)
orcho diff <run-id> --preview             # same grouped overview, explicit
orcho diff <run-id> --stat                # +A -R table per file
orcho diff <run-id> --full                # raw patch for git apply
orcho diff <run-id> --path api/payload.py # filter by file
orcho diff <run-id> --path api/           # prefix filter (api/*)
orcho diff <run-id> --max-bytes 200000    # truncate output
orcho diff <run-id> --no-color            # no ANSI colors
```

`--preview` (default) is the operator-readable grouped view. `--full`
is the byte-for-byte raw patch (pipable into
`git apply`). With a `--path` filter the raw patch is reassembled from
the matching sections, keeping `diff --git` / `index` / `---`/`+++` /
hunks intact — it stays valid.

`--preview` / `--stat` support color in a TTY; with `--full` color is
disabled on principle so the output never breaks downstream tools.

When the artifact is missing: exit 0 plus the message
`No diff artifact recorded.` ("the command worked, there is just no
artifact" — e.g. a clean run or a pre-artifact one). A nonexistent
`run-id` exits nonzero through the standard SDK error mapping.

`--path` tries an exact match first, then prefix. Matching runs over
the union of `{display path, old path, new path}`, so renames and
deletions are found under any of their names.

`run-id` is required: showing the diff of the wrong run is a common
mistake.

## `orcho evidence` — what happened / what proves it?

Plain `orcho evidence <run-id>` renders the normal evidence view as a compact
terminal summary. Use `--format=md` for the markdown report, or
`--format=json` for machine consumers. The normal JSON keeps run state and
actionable sections readable: long text fields are previewed, verbose
receipt/prompt details are summarized, and low-level live diagnostics are
counted instead of expanded. Add `--debug` to print the raw schema bundle.
The `--diff[=mode]` flag changes what goes to stdout:

```bash
orcho evidence <run-id>
orcho evidence <run-id> --diff            # = --diff=preview
orcho evidence <run-id> --diff=stat
orcho evidence <run-id> --diff=full
orcho evidence <run-id> --format=md --diff
orcho evidence <run-id> --format=json --debug
```

- `--format cli` (default): an operator-friendly terminal summary. With
  `--diff`, a `## Diff` section (stat table + preview/full) is appended after
  the summary. When the artifact is missing: `_No diff artifact recorded._`.
- `--format md`: the markdown evidence report. With `--diff`, the same
  `## Diff` section is appended after the bundle markdown.
- `--format json`: the output is wrapped as
  `{"evidence": <normal evidence view>, "diff": <record>}`. Use
  `--debug` for the raw schema bundle with full text, verbose receipts,
  prompt-render details, and every low-level diagnostic record. Disk output
  via `--out` always writes the canonical raw `evidence.json` /
  `evidence.md` bundle.

`--out PATH` (write to disk) is not affected by `--diff`: the file on
disk is the canonical schema-validated bundle without diff additions.
To get the diff as its own artifact, use `orcho diff`.

---

## `orcho verify` — verification contract

Run the project's declared verification contract against a run:

```bash
orcho verify list   # show declared verification commands (resolved, not executed)
orcho verify run    # execute the declared commands and persist receipts
orcho verify env    # execute one verification_env's assertions (writes an env receipt)
```

Receipts land in the run directory; see
[04_results.md](04_results.md).

---

## `orcho prompts` — inspect prompts

```bash
# Show which prompt template the BUILD step resolves to
orcho prompts tasks/build --project ~/www/my-project

# List all available prompts
orcho prompts --list
```

---

## `orcho repair-state` — run-state maintenance

Inspect and safely apply known repairs to run state (for example after
an interrupted process). Read `orcho repair-state --help` before using
it; repairs are explicit and listed, never guessed.
