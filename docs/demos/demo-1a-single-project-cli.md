# DEMO-1A — Single-project CLI walkthrough

A plain CLI user runs the pipeline against one Python project, then
inspects what the reviewers said. No MCP, no dashboard, no
cross-project orchestration.

This demo proves that the evidence surface a control-loop client
reaches via the SDK — `sdk.evidence_slices.list_findings()` — is
reachable from the bare `orcho` CLI as well.

## What this demo proves

1. `orcho run --mock` drives the full pipeline against a git-backed
   fixture project and writes a self-contained run directory.
2. The default worktree-isolated run produces a real reviewable diff:
   `review_changes=ok` and `diff.patch` is captured in the run dir.
3. `orcho evidence` renders that run directory into a compact terminal
   summary; `orcho evidence --format md` renders the markdown report whose
   `## Findings` section surfaces reviewer findings the SDK already exposes
   typed.
4. The same evidence shape lands whether reviewers approve cleanly or
   reject — empty findings render an explicit no-findings line, so an
   approved run reads as deliberately clean rather than as a missing
   section.

## Setup

This walkthrough can be started from an installed CLI or from a source
checkout. Prefer the installed CLI path when you want to test the package you
installed with `pipx`.

For an installed CLI:

```bash
pipx install orcho  # skip if already installed
orcho demos bootstrap golden-api
```

`orcho demos install golden-api` is accepted as the same operation. It copies
the packaged `golden-api` fixture into a disposable demo directory, initializes
the workspace, and prints the same `orcho run ... --mock` command shown below.

Use the source-checkout script only when you are evaluating from source or
contributing:

```bash
git clone <orcho-core repo> orcho-core
cd orcho-core
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

If you have more than one `orcho` executable and intentionally want the
source-checkout script to use a specific installed CLI, pin it explicitly:

```bash
ORCHO_DEMO_ORCHO_BIN="$(command -v orcho)" examples/scripts/bootstrap_demo_1a.sh
```

Do not clone `orcho-core` next to a `pipx install orcho` only to obtain the
demo fixture. That creates two Orcho copies and makes it too easy to run source
code when you meant to test the installed CLI, or the reverse.

`ORCHO_DEMO_CORE_PYTHON=/path/to/python` forces the source-checkout Python
path when tests or local scripts need it.

The fixture is an intentionally buggy mini-API plus its tests, sized so a
`--mock` run completes in under two seconds.

## Bootstrap a disposable demo dir

The mock pipeline writes inside the project tree it's pointed at. To keep
`examples/golden-api/` untouched across re-runs, the demo runs against
a *copy* and uses a separate workspace. The copy is initialized and
committed as a tiny git repo so the default worktree-isolated run can
exercise review and diff capture:

```bash
examples/scripts/bootstrap_demo_1a.sh
```

Output:

```
DEMO-1A workspace ready.

  Project (copy):  /tmp/orcho_demo_1a/project
  Workspace:       /tmp/orcho_demo_1a/workspace-orchestrator
  Source fixture:  <repo>/examples/golden-api  (untouched)

Run the pipeline:
  orcho run \
    --task "Fix validation bug in sample API" \
    --project /tmp/orcho_demo_1a/project \
    --workspace /tmp/orcho_demo_1a/workspace-orchestrator \
    --profile feature \
    --mock \
    --mock-validate-plan-reject 1 \
    --max-rounds 2 \
    --stream-output

Inspect the run:
  orcho evidence --workspace /tmp/orcho_demo_1a/workspace-orchestrator
  orcho status --workspace /tmp/orcho_demo_1a/workspace-orchestrator
  orcho diff <run-id> --stat --workspace /tmp/orcho_demo_1a/workspace-orchestrator
  orcho metrics --workspace /tmp/orcho_demo_1a/workspace-orchestrator
```

Re-running the script wipes the previous demo dir and recreates it.
The wipe checks for a sentinel file (`.orcho-demo-1a`) inside the
target dir, so pointing `ORCHO_DEMO_ROOT` at unrelated data refuses
rather than `rm -rf`. Override the location with
`ORCHO_DEMO_ROOT=/path/to/demo examples/scripts/bootstrap_demo_1a.sh`.

## Run the pipeline

The pinned command forces one validate_plan reject so the reviewer output
is non-empty. Without that flag the mock approves on the first
attempt — a fine outcome, but it doesn't exercise the rejection loop
this demo is here to surface.

```bash
orcho run \
  --task "Fix validation bug in sample API" \
  --project /tmp/orcho_demo_1a/project \
  --workspace /tmp/orcho_demo_1a/workspace-orchestrator \
  --profile feature \
  --mock \
  --mock-validate-plan-reject 1 \
  --max-rounds 2 \
  --stream-output
```

(The plan-loop budget is declared in the active profile — ``feature``'s
``LoopStep.max_rounds=2`` — so there is no CLI ``--max-plan-rounds``
flag to pass. With ``mock-validate-plan-reject=1`` the mock reviewer
rejects once, the architect revises, and round 2 approves cleanly
within the profile's budget.)

Final lines of the streamed output:

```
[DONE] Pipeline complete
  ✓ plan=ok | validate_plan=ok | implement=ok | review_changes=ok | repair_changes=skip | final_acceptance=ok
  ✓ Session: /tmp/orcho_demo_1a/workspace-orchestrator/runspace/runs/<RUN_ID>/meta.json
  ✓ Usage:   Tokens: 9,317 (in=7,970 out=1,347) | Time: 1.8s
```

The run produced two plan attempts (one rejected, one approved), one
build round, an LGTM review, and a clean Final-QA — exactly what the
typed contract calls for when validate_plan blocks once and the architect
revises.

## Inspect the run

`orcho evidence` with `--workspace` and no run id resolves to the
latest run in that workspace:

```bash
orcho evidence --workspace /tmp/orcho_demo_1a/workspace-orchestrator
```

For a markdown report, add `--format md`:

```bash
orcho evidence --format md --workspace /tmp/orcho_demo_1a/workspace-orchestrator
```

Curated markdown excerpt: quality gates, then the new findings section, then
commands (the section ordering is stable):

```markdown
## Quality gates

| Gate | Kind | Outcome | Duration |
|------|------|---------|----------|
| `tests` | `computational` | `passed` | 0.08s |

## Findings

### `P2` Missing test coverage for edge case A

**ID:** `F1` · **Phase:** `validate_plan` · **Attempt:** 1

The plan does not specify how edge case A will be covered.

**Required fix:** Add a concrete test case for edge case A with acceptance criteria.

### `P3` Module boundary unclear in section 3

**ID:** `F2` · **Phase:** `validate_plan` · **Attempt:** 1

Section 3 does not state which module owns the new behavior.

**Required fix:** Name the owning module and list the files it touches.

### `P2` Verification step lacks rollback plan

**ID:** `F3` · **Phase:** `validate_plan` · **Attempt:** 1

Verification mentions running tests but does not describe rollback if they fail.

**Required fix:** Document the rollback path and how to revert the change safely.

## Commands
```

Mock-mode gates are deterministic protocol signals. In this run the
`tests` gate is a mock signal, and no shell command was recorded in the
evidence bundle. Real-provider runs use the same evidence surface; when
runtimes emit shell commands, they appear under `## Commands` with exit
code, outcome, and duration.

Findings render in causal order — the chain that produced them
(phase → attempt → source order) — not sorted by severity. The first
finding is the first thing the reviewer flagged, which is usually
what blocked the gate.

In this forced-reject run the findings come from `validate_plan`. That is
the single gate the demo command exercises with non-empty output;
`final_qa` and `review` are also finding-bearing phases, but on this
fixture the mock review approves and the BUILD/REVIEW/FIX loop
collapses into a single LGTM round. DEMO-1A's claim is that the
gate-evidence surface is visible to a CLI user, not that every
phase-bearing review fails.

For machine consumers the same normal evidence view is available as compact
JSON:

```bash
orcho evidence --format json --workspace /tmp/orcho_demo_1a/workspace-orchestrator
```

Add `--debug` when a tool needs the raw schema bundle with full text,
verbose receipts, prompt-render details, and low-level diagnostic records.

`orcho status` and `orcho metrics` accept the same `--workspace` flag
and likewise default to the latest run. `orcho diff` takes an explicit
run id:

```bash
orcho diff <RUN_ID> --stat --workspace /tmp/orcho_demo_1a/workspace-orchestrator
```

Expected stat output:

```text
app/__init__.py   | +1 -0
app/validation.py | +1 -0
tests/__init__.py | +1 -0
```

The JSON document has a top-level `findings` list (additive on the v1
schema — older tooling that ignores unknown keys keeps working) whose
records mirror the `sdk.evidence_slices.Finding` dataclass.

## A note on `--mock` cost

`--mock` is **not** a free real-LLM run. It swaps the agent provider
for a deterministic stub that records the run's *shape* — phase
timings, gate outcomes, reviewer findings, artifact paths — without
talking to a model.

Because mock providers report no per-phase cost, the `metrics.json`
written by a mock run simply omits the `total_cost_usd_equivalent`
field. The CLI's metrics summary correspondingly omits the cost line
entirely rather than printing a misleading `$0.00 — free`.

A real-provider run populates the same metrics + evidence surfaces
from genuine model usage. The shape on disk is identical; the cost
field is the only difference. Billing differs by provider, not by
the engine.

## What this demo does not cover

- MCP control-loop semantics — see DEMO-1B (control loop)
  and [orcho-mcp/docs/control_loop_walkthrough.md](../../../orcho-mcp/docs/control_loop_walkthrough.md).
- Web dashboard — see DEMO-1C.
- Cross-project orchestration — see DEMO-1D / DEMO-1E.
- Real-provider billing.
- Filtering UX (`--severity`, `--phase` on `orcho evidence`).
  Not in DEMO-1A scope; the SDK's `list_findings(severity_min=, phases=)`
  remains the typed slice for filtered reads. Adding CLI flags is a
  separate UX task once a use case demands it.

## Pointers

- Engine architecture: [docs/architecture/overview.md](../architecture/overview.md)
- Evidence bundle schema: [pipeline/evidence/schema.py](../../pipeline/evidence/schema.py)
- Typed evidence slices (SDK): [sdk/evidence_slices.py](../../sdk/evidence_slices.py)
- Fixture: [examples/golden-api/README.md](../../examples/golden-api/README.md)
