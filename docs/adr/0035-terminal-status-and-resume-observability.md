# ADR 0035 — Terminal-status and resume observability completeness

- **Status:** Accepted. Five-commit observability suite shipped on
  ``main`` on 2026-05-24 atop the E1 session-continuity work
  (`ed81779`, ADR 0036).
- **Date:** 2026-05-24
- **Deciders:** project owner
- **Builds on:**
  [ADR 0020](0020-run-evidence-in-core.md) — the typed evidence
  bundle lives in core,
  [ADR 0031](0031-generic-phase-handoff-contract.md) — phase-handoff
  decision lifecycle (`continue` / `retry_feedback` / `halt`),
  [ADR 0032](0032-commit-decision-gate.md) — separate halt path
  (`commit_decision_halt`).

## Context

Three observability gaps surfaced while validating E1 (subprocess-
restart session continuity) end-to-end through MCP:

1. **`prompt_render` evidence had null attribution slots.** The
   durable trace declared `round` and the evidence summary projected
   it, but the writer (`_session_aware_invoke`) never populated it —
   every entry surfaced as `round=None` regardless of which plan or
   validate_plan iteration the invocation belonged to. Likewise
   `continue_session` was absent from the schema, so post-mortem of a
   multi-round or resumed run had to cross-reference `runner.log` to
   tell which render belonged to which round and whether the provider
   session was resumed.

2. **`evidence.json` and `metrics.json` were missing on halted runs.**
   ADR 0031 specifies that `halt` "writes the decision artifact, then
   synchronously flips `meta.status` to `halted` and clears
   `meta.phase_handoff`." It does not specify what happens to the
   curated bundle. In practice the pipeline subprocess exited rc=4 at
   the phase-handoff pause without reaching its `finalize` writer,
   and the SDK halt path (which runs in a different process) had no
   hook into the bundle collector — so post-halt the run dir carried
   only raw `events.jsonl` + `meta.json` + per-phase artifacts, never
   the curated `evidence.json`. Post-mortem tooling had to special-case
   halted runs.

3. **`metrics.json` lost attempts across subprocess boundary.** A run
   that paused on phase-handoff, then resumed via
   `retry_feedback`, ended with `metrics.json` containing only the
   post-resume attempts. The pre-pause plan / validate_plan rounds
   disappeared from the rollup. The ground truth was in
   `events.jsonl` but the curated metrics were wrong. Root cause:
   `MetricsCollector` had `save()` but no inverse — every fresh
   subprocess started with an empty accumulator and `finalize`
   overwrote whatever was on disk.

4. **`meta.halt_reason` was null on most halt paths.** ADR 0031's
   halt path stamped `meta.halt_reason="phase_handoff_halt"` at the
   top level. The ~10 other halt paths — `state.halt` from quality-
   gate HALT, plan / validate_plan / review / final_acceptance
   contract rejections, lifecycle stops, runner halts, agent guardrail
   blocks — only wrote `meta.halt.reason` (nested) and left top-level
   `halt_reason=None`. Downstream consumers (SDK resume-gate at
   `project_orchestrator.py:1763`, MCP wire, dashboards) that key off
   `meta.halt_reason` silently missed the cause on every non-handoff
   halt. The atexit hook on graceful exits (SIGTERM,
   KeyboardInterrupt, unhandled exception) was in the same shape —
   `status="interrupted"` with no reason.

## Decision

Close all four gaps in a coherent suite without changing the existing
ADR 0020 / 0031 / 0032 contracts. The new behaviour layers on top.

### 1. `prompt_render` writer-stamped attribution

`_session_aware_invoke` now stamps three additional fields into the
`prompt_render` payload alongside the existing trace metadata:

| Field | Source | Meaning |
|---|---|---|
| `phase_key` | the `phase` argument | session-key phase; differs from `trace_surface` for CHAIN `repair_changes` (uses `phase_key="implement"` because the repair reuses the implement physical session) |
| `round` | `state.extras[_active_loop_round_key]` with phase-name fallback | the loop counter at invoke time — plan rounds 1/2/3, repair rounds 1/2 |
| `continue_session` | the bool caller forwarded to the runtime | distinguishes round-1 fresh provider sessions from round-N resumed sessions without cross-referencing `runner.log` |

`pipeline/observability/prompt_render.py` adds them to
`DURABLE_FIELDS` and lifts them onto `PhaseRenderTrace`. The evidence
summary projection (`pipeline/evidence/prompt_render.py`) prefers
the writer-stamped `round` over the dataclass fallback, so plan /
validate_plan / implement rounds finally surface non-null in
`evidence.json`. The strict schema (`pipeline/evidence/schema.py`)
adds `phase_key` and `continue_session` to
`REQUIRED_PROMPT_RENDER_KEYS` with a new
`_PROMPT_RENDER_OPT_BOOL_FIELDS` slot that type-checks bools
explicitly (rejecting ints disguised as bools), consistent with the
existing int / str / opt-int / opt-str slot conventions.

### 2. `evidence.json` and `metrics.json` finalized on halt

Two writers cooperate so the run dir lands a complete curated bundle
on every halt path:

- **Pause-time snapshot.** `_apply_phase_handoff_pause` snapshots
  `metrics.json` alongside the existing `session.json` write before
  the rc=4 exit. The in-memory accumulator is the only source of
  token / duration / attempt rollups, and the SDK halt path runs in a
  different process — without this snapshot the subsequent bundle
  write would degrade to zero metrics for every operator-halted run.
  Best-effort: `OSError` here must not break the pause path.

- **Halt-time finalize.** `phase_handoff_decide(action="halt")` calls
  `write_bundle_or_placeholder` after the meta.status flip succeeds.
  The collector reads the pause-time `metrics.json` +
  `meta.json` + `events.jsonl` and lands a full v1 bundle (or the
  `0-placeholder` stub on collection failure). Best-effort with
  `contextlib.suppress(OSError)`: the halt transition has already
  succeeded (meta flipped, decision artifact persisted), a bundle
  write failure must not poison the result.

**Post-suite contract:** any terminal `meta.status`
(`done` / `halted` / `failed`) is accompanied by `evidence.json` +
`metrics.json` in the run dir. Post-mortem tooling no longer needs to
special-case halted runs.

### 3. Cross-subprocess metrics aggregation

`MetricsCollector.load_from_disk(path)` rehydrates `_phases` /
`_rounds` / `_total_retries` from a previously-saved `metrics.json`.
Defensive: missing file, malformed JSON, missing `phase_attempts`
key, or individually malformed entries all return `0` rather than
raising — a resume must never fail on a bad snapshot. Per-entry
int / float coercion guards against wrong-typed source fields.

`run_pipeline` calls `_metrics.load_from_disk(output_dir / "metrics.json")`
immediately after constructing the collector, gated on
`resume_from is not None`. Pairs with the pause-snapshot writer in
gap (2) so the snapshot pause writes is the snapshot resume reads.

**Post-suite invariant:** after a pause / resume cycle,
`metrics.json` `phase_attempts[]` includes every attempt from every
subprocess, matching `events.jsonl` exactly.

### 4. Top-level `meta.halt_reason` on every halt path

Two writers stamp `halt_reason` at the top level so downstream
consumers no longer see null on non-handoff halts:

- **`_PipelineRun.finalize` `state.halt` branch.** Stamps
  `self.session["halt_reason"] = self.state.halt_reason` alongside
  the existing nested `halt` block. Covers all ~10 in-pipeline stop
  points (quality gate HALT, contract rejections, lifecycle stops,
  runner halts, agent guardrail blocks). The nested `halt` block
  stays for backwards compatibility with consumers that already read
  `halt.phase`.
- **`_init_session_with_atexit` atexit hook.** Stamps
  `halt_reason="interrupted"` alongside the existing
  `status="interrupted"` / `interrupted_at` flip. Covers graceful
  SIGTERM, KeyboardInterrupt, unhandled exceptions, parent-process
  death — anything that triggers atexit while `status="running"`.

Taxonomy choice. `"interrupted"` is the honest minimal label:
atexit fires on multiple causes (signal vs exception vs parent
death) and without a process-level signal handler we cannot
distinguish them — a more specific tag (e.g. `"cancelled_sigterm"`)
would over-claim. Richer taxonomy can layer on later via a signal
handler; that is a separate ADR if and when it ships.

## Out of scope

- **SIGKILL / early-init halts** where atexit never runs. `meta.status`
  stays stale at `running` and the supervisor-side merge in
  `orcho_run_status` is the honest source. Extending the supervisor
  reap mapping to stamp `halt_reason` alongside the merged `status` is
  a separate change — tracked as a future ADR if the SIGKILL case
  becomes common enough to matter for post-mortem.
- **Process-level signal handler for fine-grained interrupt
  taxonomy.** Distinguishing SIGTERM vs SIGINT vs unhandled-exception
  in `halt_reason` requires `signal.signal(...)` installation at
  pipeline init — invasive enough to warrant its own ADR plus
  validation against nested cross-project subprocess scenarios.

## Consequences

### Wire-format additions

The closed evidence schema grew by two fields and one type slot —
strict consumers depending on `REQUIRED_PROMPT_RENDER_KEYS` get the
two new keys + opt-bool validation discipline. Golden fixtures
under `tests/fixtures/golden/{full_mode_single_round,task_mode}.json`
were regenerated via `ORCHO_REGEN_SNAPSHOTS=1 pytest`. ``sdk_schema.json``
did not change (the SDK surface was already complete; only the
writer was incomplete).

### Backwards-compatible meta shape

`meta.halt_reason` was previously `null` on most halt paths. It is
now a non-null string on every halt path. Consumers that previously
treated `halt_reason==None` as "no specific reason" will now see real
values — they should switch to status-based detection
(`status in {"halted","interrupted"}`) for the "was this a clean
finish" question. The nested `meta.halt.{reason,phase}` block stays
present and unchanged for consumers that already read it.

### Coverage

- L1: 5 unit tests for `MetricsCollector.load_from_disk` (roundtrip,
  load-then-record extends, missing file, malformed JSON, malformed
  entry).
- L1: 2 unit tests for the atexit hook (status-running flip writes
  reason; no-op when status already terminal).
- L2: schema strict-validation extended with `_PROMPT_RENDER_OPT_BOOL_FIELDS`.
- L4: acceptance tests for halt-finalizes-bundle, cross-subprocess
  metrics aggregation, and state.halt halt_reason.
- Total: 4049 passing / 3 skipped on `pytest`, ruff clean.

### Commit trail

- `4a96f72` — feat(observability): stamp phase_key / round / continue_session
- `46596c3` — feat(evidence): finalize bundle on phase-handoff halt + snapshot metrics on pause
- `e2ac261` — feat(metrics): merge prior subprocess metrics on resume
- `559c8a9` — fix(observability): write top-level halt_reason on state.halt and atexit paths
- `d7e8cc4` — chore(ruff): pre-existing UP037 / SIM105 cleanup (collateral)

All on top of `ed81779`, now formally documented as
[ADR 0036](0036-agent-session-persistence-across-subprocess-restart.md)
(written retroactively 2026-05-24 to close the "ADR-less baseline"
reference the original draft of this ADR carried).

## Clarifications (2026-05-24)

Surfaced by a code/docs audit run shortly after this ADR landed.
Original decision unchanged; the points below sharpen the contract
where the prose was ambiguous or under-specified.

### Nested `meta.halt` block is finalize-only, not a universal compat shim

The Decision §4 paragraph "Nested `halt` block stays for backwards
compatibility with consumers that already read `halt.phase`" reads as
if every halt writer maintains the nested block. **It does not.** The
nested `meta.halt = {reason, phase}` block is written by exactly one
path:

* `_PipelineRun.finalize state.halt` branch (`pipeline/project_orchestrator.py:1156-1175`).

The three halt paths that stamp top-level `halt_reason="phase_handoff_halt"`
do **not** write the nested block:

* SDK `phase_handoff_decide(action="halt")` (`sdk/phase_handoff.py:285-299`).
* In-process interactive halt sync (`pipeline/project_orchestrator.py:2915-2921`).
* Resume halt-heal defensive flip (`pipeline/project_orchestrator.py:3310-3313`).

Consequence for embedders: the nested block is present on
`state.halt`-driven halts (quality gate HALT, plan / validate_plan /
review / final_acceptance contract rejections, lifecycle stops,
runner halts, agent guardrail blocks) and absent on every
phase-handoff halt. Read `meta.halt_reason` (top-level) as the
canonical signal; treat `meta.halt.phase` as a best-effort
diagnostic available only on the `state.halt` finalize path. A new
ADR can pull the block to parity if downstream consumers need it
uniformly — for now the asymmetry is intentional (the SDK halt path
does not have access to the current phase name).

### `halted_at` timestamp is decision-side only

Stamped by SDK halt (`sdk/phase_handoff.py:286`) and resume halt-heal
(`project_orchestrator.py:3311`) — both use the decision artifact's
`decided_at`. Finalize `state.halt` and in-process interactive halt
do **not** stamp it (no decision artifact to read from). Consumers
treating `halted_at` as a universal "when did this halt" timestamp
need to tolerate `None` on the `state.halt` path; the `interrupted_at`
slot is its sibling for the atexit path.

### `state.halt_reason` is free-form, not from a closed taxonomy

The `state.halt` finalize path writes whatever string the caller
passed to `state.stop(reason)`. Real values include:

* `"phase handoff requested: validate_plan:plan_round:2"` (runner.py:815)
* `"plan rejected before implement: …"` (builtin.py:476)
* `"quality gate <name> failed (on_fail=HALT)"` (quality_gates.py:292)
* `"agent guardrail blocked destructive git command during implement"` (builtin.py:2047)
* free-form text from any future `state.stop()` caller.

Consumers comparing `meta.halt_reason == "phase_handoff_halt"` (e.g.
`pipeline/control/resume_context.py:387` `is_terminal_phase_handoff_halt`)
will not match `state.halt`-driven halts even when the cause was a
handoff trigger — the runner code path takes
`state.stop("phase handoff requested: …")` which goes through finalize
and lands a different string than the SDK halt path's canonical
`"phase_handoff_halt"`. Consumers needing reliable phase-handoff
detection should also check `meta.phase_handoff` payload presence or
the decision artifact directory.

### `failed` status carries `halt_reason` too

The Decision section's invariant ("any non-`done` terminal status
carries a non-null reason") was originally illustrated only via the
halt and atexit paths. The `_record_phase_failure` path
(`project_orchestrator.py:1084-1107`) also satisfies the invariant
as of follow-up commit `<see commit trail below>`: it stamps
`meta.halt_reason = f"phase_failure:{ExceptionClass}"` alongside the
existing structured `failure` block. The full diagnostic detail
remains in `meta.failure.{phase, error, type, ts}`; `halt_reason` is
the short rollup tag for status/dashboard consumers.

### `_PROMPT_RENDER_OPT_BOOL_FIELDS` semantics

The schema slot rejects `int` values in the optional-bool slot.
`isinstance(True, int)` is `True` in Python, so accepting an int
would let a wrong-typed flag (e.g. `continue_session=1`) slip
through. The slot allows `None` (field present, value not stamped —
legacy / synthetic payloads); rejects `int`, `str`, anything else.

### Halt-trigger enumeration (replaces "~10 stop points" prose)

The Decision section enumerated halt triggers as "the ~10 other halt
paths" without citing them. The exact list as of this writing — all
flow through `_PipelineRun.finalize state.halt` branch:

| Trigger | Location | `state.halt_reason` string |
|---|---|---|
| Plan parse failure (round 1) | `pipeline/phases/builtin.py:476` | `"plan rejected before implement: <error>"` |
| validate_plan budget exhausted on contract reject | `pipeline/phases/builtin.py:655` | `"validate_plan contract rejected before implement: <error>"` |
| validate_plan contract reject (early-exit) | `pipeline/phases/builtin.py:900` | `"validate_plan contract rejected before implement: <error>"` |
| review contract reject before repair_changes | `pipeline/phases/builtin.py:2225` | `"review contract rejected before repair_changes: <error>"` |
| final_acceptance contract reject | `pipeline/phases/builtin.py:2718` | `"final_acceptance contract rejected: <error>"` |
| Agent guardrail (implement phase) | `pipeline/phases/builtin.py:2047` | `"agent guardrail blocked destructive git command during implement"` |
| Agent guardrail (repair_changes phase) | `pipeline/phases/builtin.py:2596` | `"agent guardrail blocked destructive git command during repair_changes"` |
| Quality gate `on_fail=HALT` | `pipeline/quality_gates.py:292` | `"quality gate '<name>' failed (on_fail=HALT)"` |
| Missing parsed_plan (DAG executor) | `pipeline/lifecycle.py:400` | parser-internal text |
| Missing registry handler | `pipeline/lifecycle.py:408` | parser-internal text |
| DAG `stop_on_failure` | `pipeline/lifecycle.py:522` | DAG-internal text |
| Runner outcome reason | `pipeline/runtime/runner.py:430` | `outcome.reason` or `"halt"` |
| Loop runner caught handoff trigger | `pipeline/runtime/runner.py:815` | `"phase handoff requested: <handoff_id>"` |
| Resume retry path caught handoff | `pipeline/project_orchestrator.py:3460` | `"phase handoff requested: <handoff_id>"` |

That is 14 distinct sites, not "~10" — but the prose intent is
preserved: this enumeration covers every pipeline-side trigger that
ends up in `state.halt_reason` and consequently in
`meta.halt_reason` via the finalize state.halt branch.

For decision-side halt paths (SDK / interactive / heal) and
process-level halts (atexit / failure), see the corresponding
matrices in [docs/reference/run_artifacts.md](../reference/run_artifacts.md#halt-trigger-enumeration).

### Follow-up commit trail

* (this commit) — docs(adr): clarification appended to ADR 0035.
* (this commit) — fix(observability): stamp halt_reason on
  ``_record_phase_failure``; honour ADR 0035 invariant on ``failed``
  status.
* (this commit) — docs(adr): ADR 0036 retroactively documents E1
  session-id persistence baseline.
