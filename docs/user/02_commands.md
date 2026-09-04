# Orcho commands

Orcho is a production harness and control plane for agentic software delivery:
one task becomes an observable workflow with typed plans, gates, evidence, run
state, and cross-project coordination when you need it.

## Verification gates

`orcho quality-gates --paths …` shows each scheduled identity and its durable
selection result: `selected`, or `not_selected (paths|task_kind|operator)`. Run
header, live output, and DONE use the same rows. Manual/suggest operator gates
remain visible as intentional non-execution and never block delivery solely for
an absent receipt.

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
| `orcho workspace init` | Connect a project or initialise a shared workspace; interactive terminals may offer starter project plugin-configs |
| `orcho workspace mcp` | Print the complete read-only MCP client setup for a resolved workspace |
| `orcho repair-state` | Inspect and safely apply known run-state repairs |
| `orcho update` | Upgrade Orcho via the manager that installed it |

---

## Inspection surfaces

Use the inspection commands by question, not by file shape:

| Question | Command | Leads with |
|----------|---------|------------|
| What is happening / what should I do next? | `orcho status` | current state, phase progress, attention signals, delivery state, paths |
| What happened / what proves it? | `orcho evidence` | proof summary; use `--view full` for the plan, task/DAG shape, phase timeline, receipts, findings, and acceptance |
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
--mock-review-reject 1 # mock-only: reject one review, then repair and approve
--dry-run             # print what would happen, change nothing
--max-rounds 2        # how many implement/review/repair rounds (default: 1)
--workspace /path     # explicit workspace (default: $ORCHO_WORKSPACE / cwd discovery)
--output summary      # summary (default) | live | debug — transcript mode
--stream-output       # alias for --output live
--verbose / -v        # alias for --output debug
```

Use `--mock-review-reject N` with `--mock` when you need a deterministic
review/repair harness for CLI or SDK regression tests, release-candidate
smokes, observability debugging, or recordings. It exercises real Orcho
lifecycle and artifact surfaces with synthetic worker output; it does not
measure model quality. See the
[deterministic mock harness guide](../guides/deterministic_mock_harness.md).

Profiles decide which phases run. `orcho profiles list` shows a compact
catalogue with each profile's default mode, recipe, worktree posture, and
phase topology. Use `orcho profiles list --verbose` when you also want the
full profile descriptions.

The short version:

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

Four flags cover every phase, because two of them govern a group rather than
the single phase they are named after:

| Flag | Phases it sets |
|------|----------------|
| `--runtime-plan` / `--model-plan` | `plan` |
| `--runtime-implement` / `--model-implement` | `implement` |
| `--runtime-repair-changes` / `--model-repair-changes` | `repair_changes`, `repair_escalation` |
| `--runtime-review-changes` / `--model-review-changes` | `validate_plan`, `review_changes`, `final_acceptance` |

So passing all four does route the whole pipeline to one vendor — but passing
only some leaves the rest on their configured defaults, which is easy to
mistake for a phase having no flag at all. To set a grouped phase
independently of its siblings, use the `phases.*` configuration block, which
is per-phase.

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
adapter setup.

Example: keep planning on Claude, then route implementation through the
Claude-compatible GLM runtime under Codex review:

```bash
orcho run \
  --task "Implement the approved plan" \
  --project ./api \
  --runtime-plan claude \
  --runtime-implement claude-glm \
  --model-implement 'glm-5.3' \
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

`cross` has a deliberate subset of `run` options. Its cross-specific controls
are:

```bash
--projects alias:/path alias2:/path2   # project list (alias:path)
--mode plan                            # stop after the cross plan
--mode full                            # full run (default)
--plan-file cross_plan.json            # use an existing cross plan
--hypothesis / --no-hypothesis          # override the profile's cross hypothesis step
```

It accepts `--model` and canonical phase routing flags such as
`--model-implement`, `--model-repair-changes`, `--model-review-changes`, and
the matching `--runtime-*` flags. `orcho cross` adapts these to the direct
engine's historical names; use the canonical names with the public facade.

`--mock` is sticky for a cross run: after a mock run has been started,
`orcho cross --resume ...` inherits mock mode even without another `--mock`.
The CLI labels this inherited mode. To preserve a safe and coherent cross
surface, `--from-run-plan`, `--no-worktree-isolation`, `--attach`,
`--attach-text`, `--attach-image`, and `--attach-binary` are **mono-only**
`orcho run` options; `cross` rejects them before execution.

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
orcho history                 # last 10 runs
orcho history --last COUNT    # most recent COUNT runs, for example 25
```

Use history to choose a run id, then open the right inspection surface:
`orcho status <run-id>`, `orcho evidence <run-id>`, or
`orcho diff <run-id> --preview`.

---

## `orcho metrics` and `orcho cost` — how much did it consume?

```bash
orcho metrics                 # latest run
orcho metrics --last COUNT    # aggregated over COUNT runs, for example 5

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
terminal summary. Use `--view full` when you want the run dossier on stdout:
the full plan contract, task/DAG shape when captured, phase timeline,
implementation receipts, findings, artifacts, and final acceptance. Use
`--format=md` for the markdown report, or
`--format=json` for machine consumers. The normal JSON keeps run state and
actionable sections readable: long text fields are previewed, verbose
receipt/prompt details are summarized, and low-level live diagnostics are
counted instead of expanded. Add `--debug` to print the raw schema bundle.
The `--diff[=mode]` flag changes what goes to stdout:

```bash
orcho evidence <run-id>
orcho evidence <run-id> --view full
orcho evidence <run-id> --diff            # = --diff=preview
orcho evidence <run-id> --diff=stat
orcho evidence <run-id> --diff=full
orcho evidence <run-id> --format=md --diff
orcho evidence <run-id> --format=json --debug
```

- `--format cli` (default): an operator-friendly terminal summary. Add
  `--view full` for a full terminal dossier. With
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
orcho verify                 # show the verification command map
orcho verify env             # check the declared environment and write an env receipt
orcho verify list            # preview declared commands; execute nothing
orcho verify run --required  # run the required commands and write receipts
orcho verify run lint        # run one declared command by name
```

Receipts land in the run directory; see
[04_results.md](04_results.md).

With `--run-id`, `--project` names the canonical contract owner while the run
metadata names the physical subject. `env`, `list`, and `run` display the same
effective checkout and provenance source. A missing recorded isolated checkout
is an error (exit 2); it never falls back to the current or canonical directory.

---

## `orcho prompts` — inspect prompts

```bash
# Show the prompt catalog summary
orcho prompts

# List every prompt part
orcho prompts --list

# Show which prompt part wins after project/workspace overrides
orcho prompts tasks/plan --project ~/www/my-project

# Print the resolved prompt body
orcho prompts tasks/plan --verbose
```

---

## `orcho repair-state` — run-state maintenance

Inspect and safely apply known repairs to run state (for example after
an interrupted process). Read `orcho repair-state --help` before using
it; repairs are explicit and listed, never guessed.

---

## `orcho update` — upgrade the installed CLI

Orcho ships as an ordinary Python distribution, so the correct upgrade command
depends on which installer owns the environment the CLI runs from. `orcho
update` resolves that ownership from on-disk evidence and delegates to the
detected manager.

```bash
orcho update            # detect the install, then upgrade through its manager
orcho update --dry-run  # report the install and the command, change nothing
```

| Detected install | Upgrade command |
|---|---|
| pipx venv | `pipx upgrade <package>` |
| `uv tool` venv | `uv tool upgrade <package>` |
| virtualenv or system pip | `<that venv's python> -m pip install --upgrade <package>` |

A pip install is always upgraded with its **own** interpreter, never with
whatever `python` happens to be first on `PATH`.

Three cases are reported instead of upgraded, because upgrading would be the
wrong action:

- **Source checkout** — no installed distribution owns the running code; update
  the checkout itself.
- **Editable install** (`pip install -e`) — the checkout is the upgrade unit, so
  a package-manager upgrade would fight it.
- **Locally built install** — the environment was built from a local path rather
  than a package index, so an upgrade would silently replace that code with the
  published release. The command is printed so you can do it deliberately.

A missing manager binary is also reported rather than run. In every reported
case the command is printed and the exit code is `0`: the report is the
deliverable.
