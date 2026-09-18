# Resume modes

> Reference for what happens when a CLI invocation carries `--resume`.
> Pin-point for the `ResumeMode` enum, `--from-run-plan` follow-up,
> and the matrix of "what survives subprocess restart".
> Companion to [ADR 0031](../adr/0031-generic-phase-handoff-contract.md)
> (phase-handoff lifecycle) and [ADR 0036](../adr/0036-agent-session-persistence-across-subprocess-restart.md)
> (agent session-id persistence).

`orcho run` has three knobs that interact: `--resume <run_id>`,
`--task <text>` / `--task-file <path>`, and `--from-run-plan <run_id>`.
Their combination resolves to one of four flows. This page pins the
flows by name, what each rehydrates, and how parent-status guards
affect the dispatch.

---

## The `ResumeMode` enum

Defined at `pipeline/control/resume_context.py:31-44` as a `StrEnum`:

```python
class ResumeMode(StrEnum):
    FRESH      = "fresh"
    CHECKPOINT = "checkpoint"
    FOLLOWUP   = "followup"
```

Classified at `pipeline/control/resume_context.py:399-416` by a
**pure function** (no I/O):

```python
def classify_resume_mode(*, resume, explicit_task, explicit_task_file):
    if resume is None:
        return ResumeMode.FRESH
    if explicit_task or explicit_task_file:
        return ResumeMode.FOLLOWUP
    return ResumeMode.CHECKPOINT
```

### Mode matrix

| CLI invocation | Mode | What it means |
|---|---|---|
| `orcho run --task "do X"` | FRESH | brand-new run, no parent context |
| `orcho run --resume <id>` (no task) | CHECKPOINT | continue the existing run in the same dir |
| `orcho run --resume <id> --task "new direction"` | FOLLOWUP | new run with parent's context + new directive |
| `orcho run --from-run-plan <id> --task ...` | FRESH (with parent slot) | see [`--from-run-plan`](#from-run-plan-follow-up) below |

`--from-run-plan` does **not** map onto `ResumeMode` — it's a
fourth flavour that uses FRESH semantics for the new run dir but
hydrates the parent's `parsed_plan.json`. Tracked separately below.

### Interactive override (TTY only)

When stdin is a TTY and bare `--resume` lacks a task,
`_prompt_resume_intent` (`pipeline/control/resume_prompt.py`) offers
a menu: continue-as-checkpoint vs follow-up-with-new-task. If the
user picks follow-up, `args.task` is mutated and the next call to
`classify_resume_mode` returns `FOLLOWUP`. Non-interactive transports
(MCP, CI, piped invocations) skip this prompt — bare `--resume` stays
`CHECKPOINT`.

The menu derivation lives in
`pipeline/control/resume_context.py:419-491` `get_resume_intent_options`.
It blocks `CHECKPOINT` when the parent meta is in a terminal state
(`done`, `phase_handoff_halt`, `commit_decision_halt`) — those
require a follow-up by definition.

### Unattended phase-handoff re-arm

ADR 0154 makes one halted parent checkpoint-resumable:
`halt_reason="phase_handoff_unattended_halt"`. It is not an operator's
terminal `phase_handoff_halt`. On the first bare CHECKPOINT resume, Orcho
validates `meta.phase_handoff_unattended.phase_handoff`, restores that exact
payload (including `available_actions` and artifacts), and returns with
`status="awaiting_phase_handoff"`. It does not call `on_resume`, replay a
decision, or execute phases on that invocation. Record a decision through the
existing `phase_handoff_decide` API, then resume again for normal continuation.

If the persisted unattended block is legacy or malformed, or a typed ledger
resume check refuses execution, the run remains `halted` and writes
`meta.resume_refusal`; the original halt reason is retained when known. This
is a durable refusal, not an `interrupted` outcome.

### Parent-status guard (non-interactive)

`pipeline/project_orchestrator.py:4175-4189` rejects bare `--resume`
on a terminal parent:

```
Run <id> cannot be resumed from checkpoint (status: <status>);
pass --task with --resume to create a follow-up.
```

Exit code 0 with a hint on stderr. Pre-empts a dispatch that would
fail later with confusing diagnostics.

---

## CHECKPOINT mode

Continues an existing run **in the same directory**, reusing the
checkpoint store. The new subprocess is the second half of the same
logical run.

### What rehydrates on CHECKPOINT

| State | Lives in | Rehydrated by | Reference |
|---|---|---|---|
| Completed phase log entries | `checkpoints.db` `checkpoints` table | `_ckpt.load(resume_from)` → `session["phases"].setdefault(...)` | `project_orchestrator.py:1854-1862` |
| Per-role agent provider session ids | `checkpoints.db` `agent_sessions` table | `_ckpt.get_agent_sessions()` → role-attr→role translation → `_apply_followup_session_seeds` | [ADR 0036](../adr/0036-agent-session-persistence-across-subprocess-restart.md) |
| Metrics accumulator | `metrics.json` (pause snapshot) | `_metrics.load_from_disk(output_dir / "metrics.json")` (gated on `resume_from is not None`) | [ADR 0035](../adr/0035-terminal-status-and-resume-observability.md) |
| Active phase-handoff payload | `meta.phase_handoff` | `_init_session_with_atexit` carry-forward | `project_orchestrator.py:1789-1791` |
| Profile | `meta.profile` | resume profile resolution (defaults to `"feature"` for runs without recorded profile) | project resume setup |
| Task / project | `meta.task` / `meta.project` | `_resolve_task` / `_resolve_project` | `project_orchestrator.py:4180-4188` |
| Decision artifact | `<run_dir>/phase_handoff_decisions/<safe_id>.json` | `_apply_phase_handoff_resume` (strict reader) | `project_orchestrator.py:3232+` |
| Run dir + run id | The dir itself | CLI passes `--resume RUN_ID`; output_dir resolves to parent dir | `project_orchestrator.py:4209-4212` |

### Per-action handling (after `phase_handoff_decide`)

A CHECKPOINT resume after a phase-handoff pause reads the decision
artifact and dispatches per the recorded action:

| Action | Effect on resumed run |
|---|---|
| `continue` | Injects `state.extras["phase_handoff_override"]`; loop runner exits without rewriting machine verdict; dispatch proceeds past `validate_plan` |
| `retry_feedback` | Injects `state.last_critique = feedback` + `state.extras["human_feedback"]`; runs exactly **one** extra `plan → validate_plan` round (separate `human_directed_rounds` counter); `LoopStep.max_rounds` unchanged |
| `retry_verification` | Re-executes the persisted blocking gate set on the retained verification subject, in the same run dir, with **no agent round and no feedback**; routed to its own owner *before* route classification and before the ledger is read ([ADR 0195](../adr/0195-same-run-environment-gate-retry.md)). All gates green → the decision is consumed and the run continues after the phase the gates were scheduled on — phases behind it are skipped without firing the resume-skip trace callbacks, a raising phase inside a loop is positioned by a validated loop cursor built from the pause (an unlocatable loop round blocks before any gate runs), and a loop member reached with the round already satisfied dispatches without announcing itself, so no `phase.start` advertises a round this action may not take; a still-red gate → a fresh gate handoff; any evidence defect → a `:retry_blocked` re-park with zero gates executed |
| `halt` | **Cannot resume — terminal.** Refused at `project_orchestrator.py:1782-1788` with `PhaseHandoffHaltedError` (CLI rc=2) |

#### Scheduled-gate ledger handling on a `retry_verification` resume

Whether a CHECKPOINT resume is an env retry is decided **once**, in
`pipeline/project/session_run.py` before the header prints, from the prior
`meta.json` plus the tolerantly-read `phase_handoff_decisions/` artifacts. Both
pre-router setup steps that touch `scheduled_gate_ledger.json` read that one
copy, so they cannot disagree about which resume they are setting up:

| Setup step | Ordinary resume | `retry_verification` resume |
|---|---|---|
| Run-header gate matrix (decorative read) | strict: a corrupt ledger raises | tolerant: a failed read renders exactly as an unwritten ledger; nothing is printed about it |
| Ledger initialization (`initialize(state, resume=True)`, authoritative) | raises out of setup | same call; a store/OS error is recorded as a typed marker in `state.extras` and the run reaches the router |

The env-retry owner then reads that marker, executes no gate, and re-parks the
pause with the recorded reason — fail-closed is preserved (the marker is what
blocks re-execution), but the operator gets a decidable pause instead of a
refused run. The tolerance is scoped to this action only.

The retained worktree is also mandatory for this resume: like an active review
retry (ADR 0088), a missing, unregistered, or reclaimed `meta.worktree.path`
blocks before any checkout is materialised rather than minting a fresh one.

Both setup steps and the worktree guard key off the *claim*, not the record's
validity: a decision artifact addressed to the active pause that records
`retry_verification` puts the resume inside this boundary even when its own
persisted ids are corrupted. The strict decision reader still refuses such an
artifact — it runs later, inside the resume router — but by then the retained
subject is safe, and the refusal lands as a `:retry_blocked` re-park naming the
corrupted audit record. For every other action a corrupt decision artifact
stays a hard resume failure.

### Refusal paths

CHECKPOINT mode refuses early in three scenarios:

* Prior `meta.status="halted"` + `halt_reason="phase_handoff_halt"`
  → `PhaseHandoffHaltedError` ("halt is terminal — start a new run").
* Prior `meta.status="halted"` + `halt_reason="commit_decision_halt"`
  → analogous refusal via the commit-decision gate guard.
* Parent meta in terminal-success or terminal-halt state without a
  follow-up task → exit 0 with the "pass --task" hint (see
  [parent-status guard](#parent-status-guard-non-interactive)).

---

## FOLLOWUP mode

Mints a **new run directory** with a fresh timestamp `run_id`, but
attaches parent context as historical-only references. The new run
has its own checkpoint store, its own evidence bundle, its own
metrics — the parent is read-only context.

### What gets attached from parent

Stored on the new run's session dict at
`project_orchestrator.py:4327-4346`:

* `_followup_parent_run_id: str` — the parent run_id.
* `_followup_parent_run_dir: str` — absolute path to parent run dir.
* `_followup_parent_status: str` — parent's terminal status snapshot.
* `_followup_base_task: str` — parent's original `meta.task` (used by
  cross-run prompt context).
* `_followup_session_seeds: dict[str, str] | None` — per-role
  `session_id` values extracted from parent's persisted
  `meta.phases` (read via
  `pipeline/control/resume_context.py:extract_followup_session_seeds`).

### What rehydrates on FOLLOWUP

Different rules than CHECKPOINT — FOLLOWUP is a new run, not a
continuation:

| State | Rehydrated? |
|---|---|
| Parent's `agent_sessions` checkpoint | ❌ (different dir; cross-run goes via `meta.phases` snapshot) |
| Parent's `metrics.json` | ❌ (different dir; new accumulator) |
| Parent's `phase_handoff` payload | ❌ (parent terminal by definition) |
| Parent's per-role session_ids (from `meta.phases`) | ✅ via `_followup_session_seeds` (when present and compatible) |
| Parent's parsed plan | ❌ (FOLLOWUP does not hydrate plan — use `--from-run-plan` for that) |
| Profile | ✅ inherited via `_resolve_resume_profile` unless explicit `--profile` |

Hypothesis pre-PLAN is force-disabled on both CHECKPOINT and
FOLLOWUP (`project_orchestrator.py:4356-4366`).

---

## FRESH mode

The default when no `--resume` flag is present. Brand-new run dir,
no parent context, hypothesis configurable.

`--from-run-plan` is a special case of FRESH (see below) — same
mode classification, but the new run hydrates the parent's
`parsed_plan.json` and projects the profile to skip plan-producing
phases.

---

## `--from-run-plan` follow-up

Not a `ResumeMode` value — orthogonal flag that injects parent
context into a FRESH run.

**Resolved at:** `pipeline/project_orchestrator.py:4044-4103`.

### What it does

* New run dir (FRESH semantics).
* Loads parent's `parsed_plan.json` and hydrates the new run's
  `PipelineState.parsed_plan` (skips the plan handler — the parent
  already produced a typed plan).
* Projects the active profile to drop plan-producing phases (the
  child run starts at `implement`).
* Stamps `_followup_parent_run_id` / `_followup_parent_run_dir`
  slots like FOLLOWUP, so meta records the parent linkage.
* Continues the parent's physical worktree when parent `meta.worktree`
  is present. This keeps implementation retries on the same change
  journey; if the parent worktree is missing or no longer registered,
  the child fails fast instead of silently forking a fresh checkout.
* Provider session seeds: nullified for Phase 1 MVP scope
  (`project_orchestrator.py:4363`) — does **not** inherit parent's
  provider sessions. ADR roadmap mentions an opt-in
  `--from-run-plan-session` flag for a later phase.

### Use case

"I want a fresh implementation run for a plan that's already
approved." Useful for re-running implement-review-repair on a
known-good plan without paying for re-planning.

---

## What survives subprocess restart (cheat sheet)

A flat enumeration of everything that crosses the subprocess
boundary on CHECKPOINT resume. See [ADR 0036](../adr/0036-agent-session-persistence-across-subprocess-restart.md)
for the full contract on per-role session ids and [ADR 0035](../adr/0035-terminal-status-and-resume-observability.md)
for the metrics / evidence bundle continuity.

1. **`meta.json`** — full session dict (status, phases, halt_reason,
   phase_handoff payload, all attribution metadata).
2. **`events.jsonl`** — append-only; resume appends; sequence
   numbers continue.
3. **`metrics.json`** — pause-snapshot for handoff-pause resumes;
   load_from_disk rehydrates `_phases` / `_rounds` / `_total_retries`.
4. **`checkpoints.db`** — phase log (completed phases skipped on
   re-execution) + `agent_sessions` table (per-role provider
   session ids).
5. **`parsed_plan.json`** — typed plan from prior round; consumed
   by `--from-run-plan` or carried in session.
6. **`plan_<run_id>_r<N>.{md,json}`** — per-round plan artifacts.
7. **`phase_handoff_decisions/<safe_id>.json`** — decision
   artifacts; strict reader on resume.
8. **`evidence.json` / `evidence.md`** — finalised on halt by SDK
   path (ADR 0035), otherwise written by pipeline finalize.
9. **`meta.worktree.path`** — physical worktree checkout path when
   `isolation: per_run`; branch ref is recorded alongside it.

Out-of-band:

* **MCP supervisor truth** (`mcp_supervisor.json`) — written by the
  supervisor, not by the pipeline; reflects exit code / process
  state. Read by `orcho_run_status` to merge a corrected status
  when meta is stale (SIGKILL case).

---

## Related references

* [ADR 0031](../adr/0031-generic-phase-handoff-contract.md) — phase-handoff lifecycle.
* [ADR 0035](../adr/0035-terminal-status-and-resume-observability.md) — terminal-status + resume observability.
* [ADR 0036](../adr/0036-agent-session-persistence-across-subprocess-restart.md) — agent session-id persistence (E1 baseline).
* [Run artifacts](run_artifacts.md) — `meta.json` / `evidence.json` / `metrics.json` shapes.
* [SDK API reference](sdk_api.md) — `RunStatus` typed projection of `meta.json`.
