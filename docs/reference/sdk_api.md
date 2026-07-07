# `sdk` — public SDK API reference

The headless library boundary every embedder calls. Returns typed
dataclasses, raises typed errors, never prints, never calls `sys.exit`.
JSON-friendly through `to_jsonable` for IPC consumers.

## Conventions

Every read/report call accepts the same explicit-context triple:

```python
def some_call(
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None = None,
) -> Result: ...
```

Resolution order in `find_runs_dir` (used by every read/report call):

1. explicit `runs_dir`
2. explicit `workspace` → `workspace/runspace/runs`
3. `$ORCHO_RUNSPACE/runs`
4. `$ORCHO_WORKSPACE/runspace/runs` (engine resolver)
5. walk-up from `cwd` (only when `cwd` is not `None`)

Pass `cwd=None` together with an explicit `workspace=` or `runs_dir=`
to opt out of walk-up. The default `cwd` resolves to `Path.cwd()` at
call time.

## Errors

| Class               | `exit_code` | Raised when                                                   |
| ------------------- | ----------- | ------------------------------------------------------------- |
| `OrchoError`        | 1           | base — every SDK error subclasses it                          |
| `NoWorkspace`       | 1           | no runs directory could be resolved                           |
| `RunNotFound`       | 1           | requested run id has no directory                             |
| `PricingFetchError` | 2           | `refresh_pricing` scrape or network fetch failed              |
| `ProfileCustomizeError` | 2       | `customize_profile` request or overlay validation failed      |
| `PromptNotFound`    | 1           | requested prompt name has no resolution                       |
| `EvidenceInvalid`   | 1           | evidence bundle composition failed (file missing/unreadable)  |

## Modules

### `sdk.runs`

```python
find_runs_dir(*, workspace=None, runs_dir=None, cwd=None) -> Path
find_run(run_id=None, *, workspace=None, runs_dir=None, cwd=None) -> RunRef
load_meta(run_dir: Path) -> dict
```

`find_run` returns the requested run, or the newest one when `run_id`
is `None`. Raises `NoWorkspace` (no runs directory) or `RunNotFound`
(requested id absent or directory empty).

`load_meta` is intentionally tolerant: returns `{}` when `meta.json` is
missing or unreadable. Used internally by every reader; exposed for
embedders building their own row projections.

### `sdk.status`

```python
load_status(run_id=None, *, workspace=None, runs_dir=None, cwd=None) -> RunStatus
```

`RunStatus` carries the typed projection plus `raw_meta` / `raw_metrics`
for fields the SDK hasn't promoted. `sub_projects` is the cross-run
sub-project list (status per alias).

### `sdk.history`

```python
list_history(last=None, *, workspace=None, runs_dir=None, cwd=None) -> list[RunSummary]
```

Newest-first. `last=None` returns all runs. Returns `[]` when the runs
directory exists but is empty; raises `NoWorkspace` only when the
directory itself can't be resolved.

`RunSummary.cross_aliases` is non-empty only for cross runs;
`RunSummary.project` is the raw `meta["project"]` for single-project
runs.

### `sdk.metrics`

```python
get_run_metrics(run_id, *, workspace=None, runs_dir=None, cwd=None) -> RunMetrics
list_metrics(last, *, workspace=None, runs_dir=None, cwd=None) -> list[RunMetrics]
```

`RunMetrics.raw` carries the full `metrics.json` for embedders that
need fields the SDK hasn't promoted yet. Wraps
`core.observability.metrics.load_historical_runs` for the historical
scan.

### `sdk.evidence`

```python
collect_evidence(run_id=None, *, workspace=None, runs_dir=None, cwd=None) -> EvidenceBundle
render_evidence_md(bundle: EvidenceBundle) -> str
write_evidence_bundle(bundle: EvidenceBundle, out_dir: Path | str) -> list[Path]
```

`collect_evidence` reads run artifacts and runs the schema validator;
the returned bundle carries `valid` / `validation_errors`. Schema-only
soft failures stay in the bundle; only composition failures raise
`EvidenceInvalid`.

**`write_evidence_bundle` is side-effecting.** It writes
`<out_dir>/<run_id>/evidence.json` and `evidence.md` and returns the
list of written paths.

**`bundle["prompt_render"]` (M12).** Every evidence bundle carries a
top-level `prompt_render` list — durable per-phase render-trace
summaries projected from the session's `prompt_render` records via
the M12 extractor. The section is always present; empty list when
the run produced no covered records. Each entry has a closed schema
(reject extras + typed fields) with the keys: `phase`,
`trace_surface` (`"plan" | "replan" | "validate_plan" | "implement" |
"review_changes" | "repair_changes"`), `attempt`, `round`,
`source_path`, `render_mode`, `session_split`, `execution_mode`
(`"linear"` for current pre-fanout sessions; future writer-side
stamps pass through), `surface_id` / `surface_count` (reserved for
the planned fanout milestone), `session_scope` / `session_run_id` /
`session_runtime` / `session_model` / `provider_session_id`,
`selected_count` / `omitted_count` (NEVER the raw part-key arrays),
`prefix_hash`, `payload_hash`, `wire_chars`. **Counts only, no raw
prompt text** — the strict schema rejects the leak vectors
(`prompt`, `prompt_text`, `wire_prompt`, `body`, the part-key arrays).
Documented exceptions never appear: `hypothesis`,
`validate_hypothesis`, `final_acceptance`.

**Typed evidence slices.** Alongside the full bundle, `sdk` exports narrow
typed projections (frozen dataclasses) for control-loop clients that want one
question answered without scanning the bundle:

```python
get_plan_summary(run_id=None, ...) -> PlanSummary
list_findings(run_id=None, *, severity_min=None, phases=None, ...) -> list[Finding]
list_evidence_commands(run_id=None, ...) -> list[CommandRecord]
list_evidence_artifacts(run_id=None, ...) -> list[ArtifactRecord]
get_errors_halt(run_id=None, ...) -> ErrorsAndHalt
list_sub_runs(run_id=None, ...) -> list[SubRunLink]
list_subtask_receipts(run_id=None, ...) -> list[SubtaskReceipt]
```

All take the same optional `run_id` (newest when `None`) plus keyword-only
`workspace` / `runs_dir` / `cwd`, and raise `RunNotFound` / `NoWorkspace`.

`list_subtask_receipts` (ADR 0067 + 0068 + 0071) projects the evidence bundle's
`implementation_receipts` (built from `subtask.receipt` events) into per-subtask
delivery records. Empty for `whole_plan` runs and any run with no subtask
receipts. It is a self-attestation projection — `met` flags and `state="done"`
report what the developer *claimed*, not independently verified truth (the
reviewer / final_acceptance / test gates are the verification layer):

```python
@dataclass(frozen=True, slots=True)
class CriterionReport:
    index: int          # 1-based position in the subtask's done_criteria
    criterion: str
    met: bool
    evidence: str       # one-sentence claim, not proof

@dataclass(frozen=True, slots=True)
class SubtaskReceipt:
    subtask_id: str
    state: str          # "done" | "incomplete" | "failed" | "skipped"
    runtime: str
    model: str
    skill: str | None
    depends_on: tuple[str, ...]
    done_criteria: tuple[str, ...]
    duration: float
    error: str | None
    criteria_report: tuple[CriterionReport, ...]
    attestation_summary: str | None
    attestation_error: str | None   # gate reason when state == "incomplete"
    attestation_repaired: bool      # true when a malformed attestation was repaired
```

`state="incomplete"` means the subtask's invocation succeeded but its typed
done-criteria self-attestation was missing / malformed / mismatched /
not-all-met — distinct from a hard `failed` exec error.

### `sdk.run_diff`

```python
get_run_diff(
    run_id=None,
    *,
    workspace=None,
    runs_dir=None,
    cwd=None,
    mode: Literal["preview", "stat", "full"] = "preview",
    path: str | None = None,
    phase: str | None = None,
    max_bytes: int | None = None,
    color: bool = False,
) -> RunDiffRecord
```

Reads a captured `diff.patch` artifact and renders it for viewing.
**Capture and read are separate concerns**: this helper never
recomputes git state; it only renders the captured artifact.

With `phase=None` (default) it reads `<run_dir>/diff.patch` — the
run-level cumulative diff. With `phase="<name>"` it reads the
per-phase artifact `<run_dir>/phases/<name>/diff.patch` written by the
pipeline during that phase. The two surfaces are otherwise identical:
all `mode` / `path` / `max_bytes` / `color` semantics apply unchanged.

`mode`:

- `"preview"` (default) — Claude-style grouped view with per-file
  `+A -R` headers; intended for compact LLM context windows.
- `"stat"` — per-file `+A -R` table only, no hunk content.
- `"full"` — raw unified patch text, byte-identical to the artifact
  when no `path` filter is active (suitable for piping to `git apply`).

`path` (optional): filter to files at this path. Matches the union of
`{display path, old path, new path}` for each section, so renames and
deletes are findable by either name. Exact match first; falls back to
prefix match (`api/` returns every `api/*` file). Stripped before use;
empty after strip raises `ValueError`.

`phase` (optional): artifact key. `None` (default) reads the
run-level cumulative diff. A non-empty string reads
`<run_dir>/phases/<phase>/diff.patch`. Stripped before use; empty
after strip, or values containing `/`, `\`, or `..`, raise
`ValueError`. Phase names are simple artifact keys — not a
filesystem-path API.

`max_bytes`: cap on `content` bytes. `None` = unlimited. `<= 0` raises
`ValueError`. UTF-8 safe — partial trailing multibyte sequences are
dropped, never an exception.

`color`: forwarded to `preview`/`stat` renderers; ignored for `full`
(raw patches are byte-faithful artifacts — embedding ANSI would corrupt
`git apply` consumers).

**Missing artifact is not an error.** The returned record has
`found=False`, `files=()`, `content=""`, and `message` set (run vs
phase wording distinguishes "no diff captured for this run" from "you
asked about a phase that produced none"). Callers distinguish
"clean / pre-artifact" from "unknown run id" by catching `RunNotFound`
only. **No silent fallback**: a missing per-phase artifact never
returns the run-level cumulative diff.

`RunDiffRecord` fields: `run_id`, `found`, `mode`, `diff_path` (string
or `None`), `files` (tuple of `RunDiffFileRecord(path, added, removed)`),
`content`, `truncated`, `max_bytes` (echo of cap, or `None`), `message`,
`scope` (`"run"` or `"phase"`, echoes which artifact was asked for),
`phase` (normalized phase name on phase calls, `None` on run calls).

### `sdk.prompts`

```python
list_prompts() -> list[str]
resolve_prompt(name: str, *, project_dir=None) -> PromptResolution
```

Resolution chain ordered project → workspace → core. `winner` is the
first existing path; `body` is the rendered prompt content (or `None`
on read failure). Raises `PromptNotFound` when no level resolves.

### `sdk.pricing`

```python
show_pricing() -> PricingTable
refresh_pricing(provider: str = "openai", *, dry_run: bool = False) -> RefreshResult
```

`show_pricing` is read-only.

**`refresh_pricing` is side-effecting.** Writes the path indicated by
`core.observability.pricing._user_pricing_path()` (default
`~/.orcho/pricing.local.toml`). Tests **must** monkeypatch
`core.observability.pricing._user_pricing_path` so CI never writes the
developer's real `~/.orcho/`.

Raises `PricingFetchError` (with `exit_code=2`) on any scrape or
network failure.

### `sdk.profile_customize`

```python
customize_profile(profile: str, *, ..., dry_run: bool = False) -> ProfileCustomizeResult
```

Writes a validated `profiles_v2` overlay for a built-in profile into a local
`config.local.json`. The default scope is workspace-local
(`$ORCHO_WORKSPACE/.orcho/config.local.json`); `scope="user"` writes
`~/.orcho/config.local.json`. `dry_run=True` validates and returns the target
path without writing.

Raises `ProfileCustomizeError` (with `exit_code=2`) when the request cannot be
resolved or the resulting overlay does not pass the v2 profile schema.

### `sdk.cost`

```python
aggregate_cost(*, workspace=None, runs_dir=None, cwd=None,
               window: str = "30d", top_n: int = 5) -> CostReport
```

Pure aggregation across runs whose timestamps fall within `window`
(`"30d"` / `"7d"` / `"all"`). The dollar fields are cost references,
not billing receipts. Runtime-reported values come from the active
runtime/endpoint; token-only phases can trigger pricing fallback through
`core.observability.pricing.estimate_cost_from_total`;
`CostReport.priced_entries_count` records how many entries got priced.

`CostReport.top_runs` is sorted by `(cost desc, tokens desc)`;
`phase_breakdown` and `agent_breakdown` by cost descending. `provider`
in `agent_breakdown` is one of `"claude"` / `"codex"` / `"gemini"` /
`"other"` (best-effort prefix match on the model string).

### `sdk.runner`

```python
run_pipeline = pipeline.project_orchestrator.run_pipeline
run_cross_pipeline = pipeline.cross_project.orchestrator.run_cross_pipeline
build_orch_argv = pipeline.argv.build_orch_argv

run_pipeline_from_args(args) -> int
run_cross_from_args(args) -> int
```

The first three are stable re-exports — already library-shaped in
their original modules.

`run_pipeline_from_args` / `run_cross_from_args` translate an argparse
`Namespace` into argv and invoke the orchestrator's `main()`. They keep
the `sys.argv`+`main()` path because the orchestrator's argparse
handling owns load-bearing CLI plumbing (task-file, workspace
inference, mock provider, trace, collision checks). Retiring that path
is out of scope for the SDK boundary.

### `sdk.run_control`

Client-neutral read/command model for a run's control state (Stage 4).
Reads durable artifacts and expresses operator decisions as typed values;
never prints, renders, or depends on a terminal layer. Self-contained —
import as `from sdk.run_control import ...` (not re-exported from the
top-level `sdk` namespace).

```python
load_run_snapshot(run_id=None, *, workspace=None, runs_dir=None, cwd=None) -> RunSnapshot
read_run_events(run_id, *, workspace=None, runs_dir=None, cwd=None) -> tuple[RunEvent, ...]
tail_run_events(run_id, *, since_seq=0, poll=0.3, stop_predicate=None,
                workspace=None, runs_dir=None, cwd=None) -> Iterator[RunEvent]
build_decision_command(pending: PendingOperatorAction, action, *,
                       feedback=None, note=None) -> PhaseHandoffDecisionCommand
```

**`load_run_snapshot`** composes the existing read-only helpers
(`find_run` / `load_meta` / `load_active_phase_handoff` /
`read_cross_checkpoint`) into a focused control projection. It never
parses durable files by hand and writes nothing. `RunSnapshot` carries
`run_id`, `run_dir`, `status`, `task`, `project`, `profile`, `phases`,
`sub_runs` (tuple of `PhaseStatus`), `worktree`, `pending_action`, and a
full-fidelity `raw_meta` escape hatch. Raises `NoWorkspace` /
`RunNotFound` through `find_run`.

**`pending_action`** is at most one `PendingOperatorAction`, covering all
five pending forms. For a cross run the cross checkpoint is authoritative
at the cross level, so checkpoint-derived forms resolve before the
single-project `meta.phase_handoff` form:

| Form | `kind` | `handoff_kind` | Source | Key fields |
| ---- | ------ | -------------- | ------ | ---------- |
| Project / off-band child handoff | `phase_handoff` | `None` | `meta.phase_handoff` | `handoff_id` (payload `id`), `phase`, `available_actions` (verbatim) |
| Cross plan-reject pause | `phase_handoff` | `plan` | `meta.phase_handoff` + checkpoint | `handoff_id`, `available_actions` (verbatim) |
| Cross child-project pause | `phase_handoff` | `project` | `meta.phase_handoff` + checkpoint | `project_alias`, child id in `raw`, `available_actions` (verbatim) |
| Cross final-acceptance pause | `phase_handoff` | `cfa` | `meta.phase_handoff` + checkpoint | `cfa_paused_state` in `raw`, `available_actions` (verbatim) |
| Gate pause | `gate` | `None` | `pending_gate` | `choices` / `on_skip` in `raw` (observable only) |

`kind` and `handoff_kind` come from explicit payload fields
(`phase_handoff_kind` from the cross checkpoint is the dispatch authority)
— never inferred from an id prefix. The handoff `id` and
`available_actions` come from the active `meta.phase_handoff` payload,
which cross pauses persist in full alongside the checkpoint; the checkpoint
contributes only the dispatch fields (kind, project alias, child id,
`cfa_paused_state`), preserved in `raw`. `available_actions` carries the
runtime-published handoff verbs verbatim and is the only sanctioned source
of allowed actions; it is empty only for gate (whose `run` / `skip` choices
live in `raw`, never reinterpreted as handoff verbs). `raw` preserves the
originating payload so no field is dropped.

**`read_run_events` / `tail_run_events`** delegate to `sdk.events` and
`core.observability.events`; they own no JSONL parser. Each `RunEvent`
(`seq`, `ts`, `kind`, `phase`, `payload`) keeps `payload` as an open dict,
so unknown fields survive (forward-compatible). `tail_run_events`
forwards `since_seq` / `poll` / `stop_predicate` and yields only events
with `seq > since_seq`.

**`build_decision_command`** validates an observed phase-handoff
`PendingOperatorAction` and returns a pure `PhaseHandoffDecisionCommand`
(`run_id`, `handoff_id`, `action`, `feedback`, `note`). It checks that the
pause is a phase handoff, that `action` is in `pending.available_actions`,
and that feedback-required actions (`retry_feedback`,
`continue_with_waiver`) carry non-empty feedback — reusing the canonical
`sdk.phase_handoff` rule, not a local copy. The command is data only: it
never executes the decision or writes to disk. `to_decide_kwargs()` adapts
it to `sdk.phase_handoff.phase_handoff_decide`, the sole executor.

**Gate command boundary (Stage 4, first half).** Gate decisions resolve
through `core.resolve_gate_decision` with `run` / `skip` choices and do
not reduce to `phase_handoff_decide`. A typed gate command is
intentionally out of scope here and gets its own adapter later;
`build_decision_command` rejects gate inputs with a clear error. The
pending gate stays observable in the read model (`PendingOperatorAction`
with `kind='gate'`, choices in `raw`).

## Serialisation

```python
to_jsonable(value: Any) -> Any
```

Recursive projection that walks dataclasses, lists/tuples/sets, dicts,
`Path`, `datetime`/`date` (ISO-formatted), `Enum` (its `.value`), and
primitives. Falls back to `str(value)` for anything else.

The result round-trips through `json.dumps`. This is the contract a
future out-of-process embedder would speak over IPC; in-process
embedders use the dataclass values directly.

```python
from sdk import to_jsonable, list_history
import json

rows = list_history(last=5, workspace="/path/to/workspace")
json_payload = json.dumps(to_jsonable(rows))
```

## Side-effecting calls

Only these public SDK calls write to disk:

| Call                                | Writes                                        |
| ----------------------------------- | --------------------------------------------- |
| `refresh_pricing(provider, ...)`    | `~/.orcho/pricing.local.toml` (configurable)  |
| `write_evidence_bundle(b, out_dir)` | `<out_dir>/<run_id>/evidence.{json,md}`       |
| `customize_profile(profile, ...)`   | local `config.local.json` `profiles_v2` block |

Every other public function is read-only (no filesystem writes, no
network, no env mutation). The CLI's `_run_cli(call, formatter)`
adapter routes only the read-only handlers.

## Machine-readable schema

A deterministic JSON snapshot of every public export — name, kind
(callable / dataclass / exception), signature, dataclass fields,
exit codes — lives at [`docs/sdk_schema.json`](sdk_schema.json). It
is the SDK analogue of an OpenAPI snapshot: not a runtime contract
but a *committed* fingerprint that fails CI on accidental drift.

Regenerate after intentional surface changes:

```bash
python tools/dump_sdk_schema.py
```

`--check` mode (used by the drift test) compares the live SDK
introspection against the committed snapshot:

```bash
python tools/dump_sdk_schema.py --check   # exit 1 on drift
```

The drift guard `tests/sdk/test_schema_snapshot.py` runs on every
`pytest` invocation and asserts:

- Snapshot exists and matches `--check` output.
- Snapshot's export list equals `sdk.__all__` exactly.
- Every `OrchoError` subclass declares a numeric `exit_code`.
- Every public dataclass is `frozen=True, slots=True`.
- Every read/report call accepts the standard `(workspace,
  runs_dir, cwd)` triple.
- Exit codes are pinned per error class.

Embedders building wire bridges (MCP server, out-of-process
runners) read the snapshot to derive their own typed clients
without re-introspecting the live module.

## Versioning and stability

The SDK lives inside `orcho-core` and follows its release cadence. The
contracts above are stable: breaking changes ship with an announced
deprecation cycle, not silent drift. New fields on dataclasses are
non-breaking; field removal or rename is breaking.
