# Run artifacts on disk

## `cross_execution_graph.json`

An admitted non-dry-run cross plan may create one immutable structural
snapshot in its run directory. Its exact closed JSON shape is:

```json
{
  "schema_version": 1,
  "compile_identity": {"schema_version": 1, "fingerprint": "sha256"},
  "nodes": [{"identity": "opaque", "kind": "project", "dependencies": [], "owner": "project", "executor": {"executor": "project_pipeline", "handler": null, "enabled": true, "run": null, "on_skip": null, "mode": null}, "required": true}]
}
```

The shape is strict: unknown fields, unsupported versions, mutable lifecycle
keys such as `status` or `ready`, invalid topology, and a mismatched fingerprint
make the artifact invalid. The first write uses a flushed, fsynced temporary
file and `os.replace`; an equal second write is a byte-preserving no-op, while
a differing or malformed existing snapshot is never overwritten. It is a C1
structural artifact only, not a scheduler ledger, checkpoint, MCP/XF3 wire
projection, or source of dispatch/resume decisions.

> Reference for what a finished `orcho` run lands in its run directory.
> Pin-point for `meta.json`, `evidence.json`, and `metrics.json` shapes.
> Companion to [ADR 0020](../adr/0020-run-evidence-in-core.md),
> [ADR 0035](../adr/0035-terminal-status-and-resume-observability.md),
> [ADR 0036](../adr/0036-agent-session-persistence-across-subprocess-restart.md).

Every run, regardless of outcome, lives in a single directory under
`<workspace>/runspace/runs/<run_id>/`. The directory's filesystem layout is the
canonical interchange format for post-mortem tooling, the SDK
evidence slices, and the MCP wire surface. This page enumerates what files are
written, what shape they carry, and what changes per terminal status.

## Scheduled-gate ledger

Runs with a verification contract persist `scheduled_gate_ledger.json`. It is
schema version `"1"`, ordered by `(command, hook, phase)`, and contains the
declaration/selection/execution axes plus an append-only identity trail. Each
terminal row is exactly one of `not_selected`, `manual_available`, `suggested`,
`skipped_fresh`, `executed_pass`, `executed_fail`, `residual_missing`,
`residual_stale`, or `residual_failed`; non-selection preserves `paths`,
`task_kind`, or `operator` when applicable. `manual_only` is a hook; `manual`
is the policy. Finalization closes this artifact before evidence and DONE.

Resume reuses the snapshot and epoch decisions. Evidence copies the validated
artifact as `scheduled_gate_ledger`; SDK readers never reconstruct it via a
project plugin.

An exact operator-triggered gate rerun appends a second `execution` event for
the same full identity, rather than replacing its original execution.  That
event has `rerun: true` and the fresh command-receipt evidence path; repeating
the identical event is idempotent.  This is the only persisted rerun trail.

---

## File inventory

```
<run_dir>/
├── meta.json                  # pipeline-owned session + status truth
├── events.jsonl               # event spine; append-only timeline
├── metrics.json               # token / duration / attempt rollups
├── evidence.json              # v1 curated bundle (or REA-0 placeholder)
├── evidence.md                # readable rendering of evidence.json
├── checkpoints.db             # SQLite — phase log + agent_sessions
├── parsed_plan.json           # typed plan (when plan ran)
├── plan_<run_id>_r<N>.{md,json}  # per-round plan artifacts
├── diff.patch                 # captured run diff
├── output.log                 # raw stdout/stderr capture
├── runner.log                 # decolorized banner / phase log
├── progress.log               # live-card snapshots
├── phase_handoff_decisions/   # one file per resolved handoff
└── mcp_supervisor.json        # MCP supervisor handle (only when spawned via MCP)
```

When worktree isolation is enabled, the physical git checkout lives outside
the run directory at `<workspace>/runspace/worktrees/<worktree_id>/checkout/`.
`meta.json` records that path in `worktree.path`.

Pause-time and terminal write contracts differ — see the per-file
sections below.

For the canonical `events.jsonl` kind registry and required payload
keys, see [event registry](event_registry.md).

---

## `meta.json`

**Writer:** `pipeline/engine/session.py:save_session` and `sdk/phase_handoff.py`
direct write on halt. There is exactly one source of truth for
in-flight runs (the pipeline subprocess) and one out-of-band writer
(the SDK halt path).

**Always present (fresh-session shape — `pipeline/project_orchestrator.py:1714-1726`):**

```json
{
  "task": "<truncated to first 500 chars at run.start>",
  "project": "/abs/path/to/project",
  "plugin": "Project",
  "model": "claude-opus-4-8[1m]",
  "profile": "feature",
  "plan_source": "local",
  "change_handoff": "uncommitted",
  "session_mode_requested": "stateless",
  "timestamp": "2026-05-24T01:23:45.678901",
  "status": "running",
  "phases": { }
}
```

### Status field semantics

`meta.status` is the wire-format status. Canonical values:

| Value | Meaning | Where written |
|---|---|---|
| `running` | live in-flight (default after fresh-session save) | `_init_session_with_atexit` |
| `done` | natural finish through `finalize` | `finalize` else-branch (`project_orchestrator.py:1190`) |
| `halted` | terminal halt; see "Halt paths" below for sub-flavours | finalize state.halt branch / SDK halt / interactive halt / resume halt-heal |
| `interrupted` | atexit fired while status was `running` (graceful SIGTERM / KeyboardInterrupt / unhandled exception / parent-process death) | atexit hook (`project_orchestrator.py:1797-1815`) |
| `failed` | structured failure via `_record_phase_failure` | `project_orchestrator.py:1098-1107` |
| `awaiting_phase_handoff` | rc=4 pause waiting on `phase_handoff_decide` | `_apply_phase_handoff_pause` (`project_orchestrator.py:3066-3071`) |
| `awaiting_human_review` | legacy plan-profile tail (retiring) | finalize else-branch |
| `cancelled` | supervisor-side hard cancel (only set by cross-project orchestrator today) | `pipeline/cross_project/orchestrator.py:2110` |
| `awaiting_commit_decision` | post-release commit-decision gate paused — see [ADR 0032](../adr/0032-commit-decision-gate.md) | commit-decision gate handler |

`running` on a finished-run dir means the subprocess was killed
before atexit ran (SIGKILL, OOM kill, host crash, segfault). The
MCP supervisor's `orcho_run_status` merge layer corrects this on
the wire for runs spawned via MCP; raw `meta.status` does not.

### `halt_reason` (top-level)

Stamped on every terminal status except `done` and (for now)
`awaiting_*` states. Canonical values that the SDK + parsers
recognise:

| Value | Set by | When |
|---|---|---|
| `"phase_handoff_halt"` | SDK halt + interactive halt + resume halt-heal | operator chose `halt` in `phase_handoff_decide` |
| `"phase_handoff_unattended_halt"` | CLI unattended phase-handoff policy | `orcho run --no-interactive` reached a handoff that cannot be safely auto-continued |
| `"commit_decision_fix"` | commit-decision correction path | operator chose `fix` after a rejected release verdict; follow-up resume is the intended next step |
| `"commit_decision_halt"` | commit-decision gate halt path | operator rejected the commit decision; see ADR 0032 |
| `"interrupted"` | atexit hook | graceful interrupt while status was `running` |
| `"phase_failure:<ExceptionClass>"` | `_record_phase_failure` | uncaught exception escaped a phase handler |
| free-form string | finalize `state.halt` branch | any `state.stop(reason)` caller — see the [halt-trigger enumeration](#halt-trigger-enumeration) below |

**Caveat — `state.halt` free-form strings.** When the finalize
`state.halt` branch fires, `halt_reason` holds whatever string the
caller passed to `state.stop()`. Real examples that ship today:
`"plan rejected before implement: …"`, `"validate_plan contract
rejected before implement: …"`, `"quality gate '<name>' failed
(on_fail=HALT)"`, `"agent guardrail blocked destructive git command
during implement"`, `"phase handoff requested: <handoff_id>"`. Code
keying on `halt_reason == "phase_handoff_halt"` (e.g.
`pipeline/control/resume_context.py:387` `is_terminal_phase_handoff_halt`)
correctly matches the SDK/interactive/halt-heal paths but does
**not** match a finalize-state.halt termination triggered from a
handoff request. Consumers needing reliable phase-handoff detection
should additionally check `meta.phase_handoff` payload presence or
the decision artifact directory.

### Per-status fields matrix

| Field | `running` | `done` | `halted` (finalize state.halt) | `halted` (SDK / interactive / heal) | `interrupted` | `failed` | `awaiting_phase_handoff` |
|---|---|---|---|---|---|---|---|
| `status` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `halt_reason` (top) | — | — | ✓ (free-form) | `"phase_handoff_halt"` | `"interrupted"` | `"phase_failure:<Ex>"` | — |
| `halt` nested `{reason, phase}` | — | — | ✓ | — | — | — | — |
| `halted_at` (ISO UTC) | — | — | — | ✓ | — | — | — |
| `interrupted_at` (ISO local) | — | — | — | — | ✓ | — | — |
| `failure` block | — | — | — | — | — | ✓ | — |
| `phase_handoff` payload | — | — | — | popped | unchanged | unchanged | ✓ (full) |
| `phases` map | partial | complete | partial | partial | partial | partial | partial |

**The nested `halt = {reason, phase}` block is finalize-only.** ADR 0035
documents this asymmetry — the SDK halt path, interactive halt, and
resume halt-heal write top-level `halt_reason` but do NOT mirror
into the nested block. Consumers should read top-level
`halt_reason` as canonical; the nested block is best-effort
diagnostic and only available on `state.halt`-driven halts.

### `failure` block shape

```json
{
  "phase": "<phase_name>",
  "error": "<first 2000 chars of str(exc)>",
  "type":  "<ExceptionClass>",
  "ts":    "2026-05-24T01:23:45.678901"
}
```

Idempotent — second call to `_record_phase_failure` no-ops, the
first failure wins on both `halt_reason` and `failure` block.

### `phase_handoff` payload shape

Set by `_apply_phase_handoff_pause` (`project_orchestrator.py:3038-3051`):

```json
{
  "id": "validate_plan:plan_round:2",
  "phase": "validate_plan",
  "type": "human_feedback_on_reject",
  "trigger": "rejected",
  "verdict": "REJECTED",
  "approved": false,
  "round_extras_key": "plan_round",
  "round": 2,
  "loop_max_rounds": 2,
  "available_actions": ["continue", "retry_feedback", "halt"],
  "artifacts": {"plan_file": "<abs path>"},
  "last_output": "<verbatim agent output>"
}
```

Popped on `phase_handoff_decide(halt)` and on resume `continue` /
`retry_feedback`. See [ADR 0031](../adr/0031-generic-phase-handoff-contract.md)
for the full lifecycle.

### Byte format note

Two writers produce slightly different on-disk bytes:

* `save_session` writes JSON via `json.dumps(d, indent=2)` (no
  trailing newline).
* SDK `phase_handoff_decide(halt)` writes via `json.dumps(meta,
  indent=2, ensure_ascii=False) + "\n"` (trailing newline).

Parsers tolerate both; binary diffs may surface the difference.

---

## `evidence.json`

**Writer:** `pipeline/evidence/bundle.py:write_bundle` (full v1) or
`write_placeholder` (REA-0 fallback). `write_bundle_or_placeholder`
chooses between them and is the only entry point.

**Trigger points (any of):**

* Normal pipeline finish (`finalize` calls `write_bundle_or_placeholder`).
* `phase_handoff_decide(halt)` (per ADR 0035 — `sdk/phase_handoff.py:301-320`).
* `--from-run-plan` follow-up parent doesn't lose its bundle.

**Schema branching.** Always read `schema_version` first:

```python
import json
bundle = json.load(open(run_dir / "evidence.json"))
if bundle["schema_version"] == "1":
    # full curated bundle — every required key present
    ...
elif bundle["schema_version"] == "0-placeholder":
    # collection failed — only the placeholder envelope is guaranteed
    ...
```

### Placeholder shape (`schema_version: "0-placeholder"`)

Always present keys — the only invariant on a placeholder bundle:

```json
{
  "schema_version": "0-placeholder",
  "run_id": "20260524_012345_abcdef",
  "run_dir": "/abs/path/to/run_dir",
  "status": "halted",
  "created_at": "2026-05-24T01:23:45+00:00"
}
```

A placeholder lands when the v1 collector raises
(`EvidenceSchemaError`, `FileNotFoundError`, `OSError`, `ValueError`)
— typically a partially-written run dir or a schema mismatch on a
synthetic test fixture. Operators seeing a placeholder should
fall back to `events.jsonl` + `meta.json` for diagnostic detail.

### Full v1 shape (`schema_version: "1"`)

16 required top-level keys (defined in
`pipeline/evidence/schema.py:38-59`):

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | `"1"` | branching key — always check first |
| `run_id` | `str` | from `meta.run_id` or `meta.session_ts` or dir basename |
| `run_dir` | `str` | absolute path to run dir |
| `status` | `str` | `meta.status` or inferred from last `run.end` event |
| `created_at` | `str` | ISO UTC timestamp of bundle composition |
| `task` | `str` | from meta or `run.start` payload |
| `profile` | `str` | active profile name |
| `plan` | `dict` | typed plan record (see `REQUIRED_PLAN_KEYS`) |
| `phases` | `list[dict]` | one entry per phase invocation, soft-validated |
| `gates` | `list[dict]` | quality gates fired, soft-validated |
| `commands` | `list[dict]` | recorded commands, soft-validated |
| `artifacts` | `list[dict]` | written artifacts |
| `metrics` | `dict` | rollup; see [`metrics.json`](#metricsjson) below for shape. Carries the additive `subtasks` per-subtask usage breakdown through verbatim when `metrics.json` has it, so the durable bundle alone answers "which implement subtask used the most tokens / cost reference?" |
| `errors` | `list[dict]` | errors from events |
| `prompt_render` | `list[dict]` | **closed-schema** — see [observability_surfaces.md](../architecture/observability_surfaces.md#writer-stamped-attribution-on-prompt_render-adr-0035) for the field table |
| `raw_events_path` | `str` | absolute path to `events.jsonl` |

The required `plan` object may also carry additive `subtasks: list[dict]`,
an embedded task/DAG projection from the `plan.parsed` event when the run was
created by a version that emitted it. Older written bundles may have
`plan.subtask_count` without `plan.subtasks`; readers should then fall back to
the `parsed_plan` artifact path.

**Additional keys the collector always emits** (NOT in the required
set — `validate_bundle` accepts bundles without them; consumers may
rely on them being present in practice):

* `findings: list[dict]` — review/repair findings extracted from meta.
  Each entry carries additive lifecycle fields for evidence readers:
  `status` (`open`, `final_rejected`, `waived`, `fixed`, or `accepted`)
  and `status_reason`. Active findings are `open` or `final_rejected`;
  `waived`, `fixed`, and `accepted` remain in the bundle as historical
  proof rather than current blockers.
* `release_summary: list[dict]` — final_acceptance gate decisions.
* `implementation_receipts: list[dict]` — per-subtask delivery receipts for a
  `subtask_dag` run, built from `subtask.receipt` events. Empty for `whole_plan`
  runs. Each entry: `subtask_id`, `state` (`done` / `incomplete` / `failed` /
  `skipped` — ADR 0067 + [0068](../adr/0068-subtask-done-criteria-attestation.md)
  + [0071](../adr/0071-subtask-attestation-repair-receipts.md)),
  `runtime`, `model`, `skill`, `depends_on`, `done_criteria`, `duration`,
  `error`, and the P7 done-criteria self-attestation: `criteria_report`
  (`[{index, criterion, met, evidence}, …]`), `attestation_summary`, and
  `attestation_error` (the gate reason on an `incomplete` receipt). When
  present, `attestation_repaired=true` means the original machine-readable
  attestation was malformed but a single no-artifact-mutation repair turn
  produced a valid one. The
  `sdk.list_subtask_receipts` accessor projects this key into typed records.
* `worktree: dict | None` — worktree metadata from meta.
* `worktree_projects: dict` — cross-project per-alias worktree map.

### Validator semantics

`pipeline/evidence/schema.py:validate_bundle` runs:

* Top-level `REQUIRED_TOP_LEVEL_KEYS` check (16 keys).
* `_validate_prompt_render` — **closed schema** (rejects extras +
  type-checks every field, rejects `bool` in int slots, rejects
  known leak vectors like `prompt_text`).
* `_validate_list_entries` — **soft schema** for `phases`, `gates`,
  `commands`, `artifacts` (accepts extras).

The asymmetry is deliberate: `prompt_render` carries hashable trace
content where extras smuggle in raw prompt bodies; lists like
`phases` evolve more freely.

### `findings_summary` consumers

Status / dashboard surfaces typically render
`bundle["findings_summary"]` (when present). The full structure is
in `sdk/evidence_slices.py`. This page does not pin that schema —
see the SDK reference for client-facing slices.

---

## `metrics.json`

**Writer:** `core/observability/metrics.py:MetricsCollector.save`.

**Two trigger points:**

* `finalize` (`pipeline/project_orchestrator.py:1224`) — natural
  end-of-run write.
* `_apply_phase_handoff_pause` (`project_orchestrator.py:3081-3082`)
  — pause-time snapshot before rc=4 exit, so the SDK halt path
  finds a populated `metrics.json` to read on bundle finalize, and
  so the resume subprocess can rehydrate via
  `MetricsCollector.load_from_disk` (see [ADR 0035](../adr/0035-terminal-status-and-resume-observability.md)).
* A human-directed verification retry snapshots the FSM-owned
  `repair_changes` attempt before its exact gate rerun, so a fresh failed
  handoff retains the completed repair attempt without adding a second counter.

**No trigger** on generic crashes / `_record_phase_failure` /
SIGKILL — those paths leave `metrics.json` as last-written (which
may be missing or stale on first-phase failures).

### Top-level shape

```json
{
  "total_tokens_in":  2125,
  "total_tokens_out": 136,
  "total_tokens_unknown": 0,
  "total_tokens":     2261,
  "total_duration_s": 0.516,
  "phases":           { "<phase_name>": { ... } },
  "phase_attempts":   [ { ... }, ... ]
}
```

### Conditionally present keys

Three rollup fields are **omitted when zero** (`as_dict` at
`metrics.py:421-430`):

* `total_rounds: int` — only when at least one `add_round()` call fired.
* `total_retries: int` — only when at least one phase recorded a retry.
* `total_cost_usd_equivalent: float` — cost reference, present only
  when at least one phase reported `cost_usd_equivalent`.
* `cost_estimated: bool` — `false` when the cost reference came from
  the active runtime/endpoint; `true` when Orcho estimated it from a
  local pricing table.

Consumers must use `metrics.get("total_rounds", 0)` rather than
`metrics["total_rounds"]`.

### `phase_attempts` entry shape

```json
{
  "phase": "plan",
  "attempt": 1,
  "model": "claude-opus-4-8[1m]",
  "tokens_in": 4,
  "tokens_out": 374,
  "total_tokens": 378,
  "duration_s": 0.204,
  "tokens_exact": true
}
```

Optional per-entry keys (omitted when zero / unset):
`tool_calls`, `tokens_unknown`, `retries`, `cost_usd_equivalent`,
`cost_estimated`.

`tokens_exact` semantics: `true` when the count came from the
provider's API headers / CLI usage trailer; `false` when we
estimated via `estimate_tokens(prompt)`.

### `phases` rollup shape

Per-phase aggregate keyed by phase name; same per-attempt fields
collapsed via summation, plus:

* `attempts: int` — count of attempts merged for this phase.
* `model` becomes `"mixed"` when attempts used different models.
* `tokens_exact` becomes `False` when any contributing attempt was
  estimated.

### Per-subtask usage breakdown (`subtasks`)

Additive top-level key, present **only** for `subtask_dag` implement
runs (and only from the version that introduced it — older runs and
`whole_plan` runs do not carry it). It makes a high-usage implement
phase diagnosable by attributing usage to individual subtasks:

```json
{
  "subtasks": {
    "implement": [
      {
        "subtask_id": "T1-register-reject",
        "runtime": "claude",
        "model": "claude-opus-4-8[1m]",
        "invocations": 1,
        "duration_s": 42.0,
        "tokens_in": 1200000,
        "tokens_out": 8000,
        "total_tokens": 1208000,
        "tool_calls": 31,
        "tokens_exact": true,
        "tokens_in_cache_read": 900000,
        "tokens_in_cache_create": 100000,
        "cost_usd_equivalent": 4.21,
        "cost_estimated": false,
        "state": "done",
        "declared_files": ["pipeline/register.py"]
      }
    ]
  }
}
```

Always-present per-record fields: `subtask_id`, `runtime`, `model`,
`invocations`, `duration_s`, `tokens_in`, `tokens_out`,
`total_tokens`, `tool_calls`, `tokens_exact`. The remaining fields —
`tokens_in_cache_read`, `tokens_in_cache_create`,
`cost_usd_equivalent`, `cost_estimated`, `state`, `declared_files` —
appear **only when known**; an unknown value is omitted.

Three authority/semantics rules a consumer must honor:

* **Phase rollups stay authoritative.** The `phases.implement` total
  (and every `total_*`) is the single source of truth for the
  implement total. The per-subtask records *explain* that total —
  their token/cost sums equal `phases.implement`'s — they are **not**
  added to it. Reading both and summing them double-counts.
* **`declared_files` is plan-declared scope, not observed changes.**
  It is the subtask's `files` from the parsed plan, i.e. the intended
  edit surface — not the set of files Orcho saw change. Do not present
  it as "files this subtask modified".
* **`invocations` aggregates retries.** A subtask re-run by the ADR
  0073 substance-repair path (or an in-subtask attestation-repair turn)
  contributes multiple invocations to one record; `state` reflects the
  latest receipt across all passes.

`cost_usd_equivalent` is a dollar-denominated cost reference, not a
billing receipt. Runtime-reported values come from the active
runtime/endpoint; estimated values use Orcho pricing tables.
Subscription plans may bill differently. The field follows the same
accounting gate as `total_cost_usd_equivalent`: when dollar accounting
is disabled it is scrubbed from every record.

### Cross-subprocess merge (ADR 0035)

`MetricsCollector.load_from_disk(path)` rehydrates the accumulator
from a saved `metrics.json` so a resume subprocess extends rather
than replaces the prior subprocess's work. Triggered automatically
in `run_pipeline` when `resume_from is not None`. Defensive:
missing file, malformed JSON, or malformed individual entries return
`0` and leave the collector empty (resume must never fail on a bad
snapshot). The additive `subtasks` breakdown is rehydrated too, so a
handoff pause → resume → final save preserves it rather than dropping
the pre-pause breakdown. A partial `implement_retry` resume re-emits
only the rerun subtasks; `record_subtask_usage` therefore **merges by
`subtask_id`** (rerun ids accumulate onto the rehydrated record,
untouched ids are kept) rather than replacing the phase list — so the
breakdown stays complete and its sums keep reconciling with the
cumulative `phases.implement` rollup.

### Byte format

`save()` writes via `json.dumps(d, indent=2, ensure_ascii=False)` —
no trailing newline (unlike `phase_handoff_decide`'s meta.json write).

---

## Halt-trigger enumeration

Every place a run can land in a non-`done` terminal status. The
matrix is exhaustive for halt/interrupted/failed paths; cross-project
orchestrator paths are listed separately.

### Pipeline-side stop points (`state.stop(reason)` → finalize state.halt branch)

| Trigger | Location | `state.halt_reason` string |
|---|---|---|
| Plan parse failure round-1 | `pipeline/phases/builtin.py:476` | `"plan rejected before implement: <parse error>"` |
| validate_plan budget exhausted on contract reject | `pipeline/phases/builtin.py:655` | `"validate_plan contract rejected before implement: <error>"` |
| validate_plan contract reject (early-exit) | `pipeline/phases/builtin.py:900` | `"validate_plan contract rejected before implement: <error>"` |
| review contract reject before repair_changes | `pipeline/phases/builtin.py:2225` | `"review contract rejected before repair_changes: <error>"` |
| final_acceptance contract reject | `pipeline/phases/builtin.py:2718` | `"final_acceptance contract rejected: <error>"` |
| Agent guardrail (implement) | `pipeline/phases/builtin.py:2047` | `"agent guardrail blocked destructive git command during implement"` |
| Agent guardrail (repair_changes) | `pipeline/phases/builtin.py:2596` | `"agent guardrail blocked destructive git command during repair_changes"` |
| Quality gate `on_fail=HALT` | `pipeline/quality_gates.py:292` | `"quality gate '<name>' failed (on_fail=HALT)"` |
| Missing parsed_plan (DAG executor) | `pipeline/lifecycle.py:400` | parser-internal text |
| Missing registry handler | `pipeline/lifecycle.py:408` | parser-internal text |
| DAG `stop_on_failure` | `pipeline/lifecycle.py:522` | DAG-internal text |
| Runner outcome reason | `pipeline/runtime/runner.py:430` | `outcome.reason` or `"halt"` |
| Loop runner caught handoff trigger | `pipeline/runtime/runner.py:815` | `"phase handoff requested: <handoff_id>"` |
| Resume retry path caught handoff | `pipeline/project_orchestrator.py:3460` | `"phase handoff requested: <handoff_id>"` |

All of the above flow through `_PipelineRun.finalize state.halt`
branch (`project_orchestrator.py:1156-1175`) → writes
`status="halted"` + top-level `halt_reason=state.halt_reason` +
nested `halt={reason, phase}`.

### Decision-side halt (SDK / supervisor)

| Trigger | Location | `meta.halt_reason` |
|---|---|---|
| `phase_handoff_decide(action="halt")` | `sdk/phase_handoff.py:285-299` | `"phase_handoff_halt"` |
| Interactive in-process halt sync (after decide) | `project_orchestrator.py:2915-2921` | `"phase_handoff_halt"` |
| Resume halt-heal defensive flip | `project_orchestrator.py:3310-3313` | `"phase_handoff_halt"` |
| CLI unattended phase-handoff halt | `pipeline/project/handoff.py` | `"phase_handoff_unattended_halt"` |
| commit-decision halt | see [ADR 0032](../adr/0032-commit-decision-gate.md) | `"commit_decision_halt"` |
| deferred delivery parked (defer mode) | see [ADR 0099](../adr/0099-deferred-delivery-decision-gate.md) | `"commit_delivery_pending"` |
| `decide_delivery(action="halt"/"fix")` | `sdk/run_control/delivery.py` | `"commit_decision_halt"` / `"commit_decision_fix"` |

### Deferred delivery decision gate (ADR 0099)

When `commit.decision_mode='defer'`, a finished non-interactive run does NOT
auto-ship or silently drop its diff. The producer persists the parked decision
to `meta.commit_delivery` with `status="pending"` (the only place `pending`
appears at rest — the `commit_decisions/<id>.json` audit schema never carries
it) and finalizes the run `halted` with
`halt_reason="commit_delivery_pending"` — a recoverable amber halt. The full
delivery context (`source_path`, `project_path`, `baseline_ref`,
`changed_paths`, `untracked_paths`, `release_verdict`, and any
`verification_*` blockers) rides on the persisted gate so it can be replayed.

An operator resolves the parked gate out of band through
`sdk.decide_delivery(run_id, action)` (mirror:
`RunService.decide_delivery`). The executor re-checks the hard guards from the
persisted evidence, recomputes the patch against the held worktree (it never
reads the non-serialised `patch_text`), applies the chosen action, and
finalizes the run: `approve` / `apply` / `skip` settle it `done`; `halt` / `fix`
keep it `halted` (`commit_decision_halt` / `commit_decision_fix`). The read-only
companion `sdk.delivery_decision_state(run_id)` projects which actions are
currently safe to offer.

### Cross-level commit delivery (cross runs only)

After the cross release gate (CFA) approves — or the operator overrides a
REJECTED verdict and chooses `continue` — the cross runner delivers each
alias's worktree diff into its project checkout
(`pipeline/cross_project/cross_delivery.py`). The aggregate maps to a
terminal status in `pipeline/cross_project/finalization._decide_status`:

| Delivery aggregate | `meta.status` | `meta.halt_reason` |
|---|---|---|
| `ok` / `disabled` (all aliases success-like) | `done` | — |
| `partial` (mix of success + failure) | `failed` | `"cross_delivery_partial"` |
| `failed` (no alias delivered) | `failed` | `"cross_delivery_failed"` |
| `halted` (operator stop mid-loop) | `halted` | `"phase_handoff_halt"` |

Per-alias detail lives in `session["phases"]["cross_delivery"]` (see the
shape note below), never a top-level `session["cross_delivery"]`.

### Process-level halt (atexit / failure)

| Trigger | Location | `meta.halt_reason` | `meta.status` |
|---|---|---|---|
| Graceful SIGTERM / Ctrl-C / exception / parent death | `project_orchestrator.py:1797-1815` | `"interrupted"` | `"interrupted"` |
| Uncaught exception escaped a phase handler | `project_orchestrator.py:1098-1107` | `"phase_failure:<ExceptionClass>"` | `"failed"` |

### Out-of-band (no in-pipeline writer)

| Trigger | Effect |
|---|---|
| SIGKILL / OOM kill / host crash | atexit bypassed → `meta.status="running"` stays stale; supervisor-side merge in `orcho_run_status` is the honest source |
| Early-init halt (pre-pipeline-start crash) | Same as SIGKILL — no atexit, no meta update |

---

## `session["phases"]["cross_delivery"]` shape (cross runs only)

Written by the cross delivery loop before `run.end`. Phase-scoped
(under `phases`), never a top-level key. Shape:

```json
{
  "overall": "ok | partial | failed | halted | disabled",
  "disabled_by_config": false,
  "per_alias": {
    "<alias>": {
      "alias": "<alias>",
      "status": "committed | applied_uncommitted | no_diff | skipped | skipped_already_delivered | target_dirty | commit_failed | apply_failed | not_applicable | halted | disabled",
      "commit_sha": "<sha landed in the target checkout, when present>",
      "published_commit_sha": "<sha created on the published delivery branch, when present>",
      "error": "<text, present on failure>",
      "release_override": {
        "original_verdict": "REJECTED",
        "effective_verdict": "APPROVED_FOR_DELIVERY",
        "source": "operator_override"
      },
      "decision": { "<mono commit-delivery decision dict>": "…" }
    }
  }
}
```

`disabled_by_config` is the success contract: a `disabled` overall is
success-like only when the operator turned delivery off
(`commit.enabled=false`). `release_override` is present only on aliases
shipped past a non-APPROVED child verdict via a CFA override-continue —
the original reviewer verdict is preserved there and the persisted child
`final_acceptance.verdict` is never rewritten. Idempotent resume state
lives on the cross checkpoint as `cross_checkpoint.delivery_status`
(`{alias: {status, commit_sha}}`); a resumed run skips aliases already
delivered.

## Related references

* [ADR 0020](../adr/0020-run-evidence-in-core.md) — REA evidence in core (baseline).
* [ADR 0031](../adr/0031-generic-phase-handoff-contract.md) — phase-handoff decision lifecycle.
* [ADR 0032](../adr/0032-commit-decision-gate.md) — commit-decision gate.
* [ADR 0035](../adr/0035-terminal-status-and-resume-observability.md) — terminal-status + resume observability completeness.
* [ADR 0036](../adr/0036-agent-session-persistence-across-subprocess-restart.md) — agent session-id persistence (E1 baseline).
* [SDK API reference](sdk_api.md) — `RunStatus`, `RunMetrics`, `ErrorsAndHalt`, `EvidenceBundle`.
* [Observability surfaces](../architecture/observability_surfaces.md) — `prompt_render` + four sibling per-phase trace surfaces.
* [Event registry](event_registry.md) — canonical `events.jsonl` event kinds and required payload keys.
* [Resume modes](resume_modes.md) — CHECKPOINT vs FOLLOWUP vs FRESH semantics + `--from-run-plan`.
