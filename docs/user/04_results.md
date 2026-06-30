# Run results

## Where the artifacts live

Every run creates a folder:
```
runspace/runs/20260610_144938/
├── meta.json                ← run config, per-phase status, delivery decisions
├── events.jsonl             ← the event spine: every phase/gate/handoff event
├── evidence.json            ← schema-validated evidence bundle
├── evidence.md              ← the same bundle, human-readable
├── metrics.json             ← tokens and time per phase
├── diff.patch               ← the run's captured diff (rendered by `orcho diff`)
├── plan_<run>_r1.md / .json ← plan artifact per planning round
├── parsed_plan.json         ← the typed plan the pipeline executed
├── progress.log             ← live log (tail -f while the run is going)
├── output.log               ← full agent output
├── checkpoints.db           ← SQLite state for --resume
├── phases/                  ← per-phase working artifacts
├── commit_decisions/        ← delivery decision records
└── verification_receipts/   ← receipts written by verification checks
```

Long full-cycle runs also persist `session.json` — the per-phase
observability record (prompt rendering, context growth and clearing,
runtime compaction), checkpointed after every review/repair round. The
detailed navigator over these surfaces is
[observability_surfaces.md](../architecture/observability_surfaces.md).

---

## meta.json — run status

```json
{
  "run_id": "20260503_104135",
  "status": "done",
  "task": "Add input validation...",
  "phases": {
    "plan":             { "status": "ok", "duration_s": 45.2 },
    "validate_plan":    { "status": "ok", "duration_s": 12.4 },
    "implement":        { "status": "ok", "duration_s": 187.0 },
    "review_changes":   { "status": "ok", "duration_s": 23.1 },
    "repair_changes":   { "status": "ok", "duration_s": 94.5 },
    "final_acceptance": { "status": "ok", "duration_s": 31.7 }
  }
}
```

The phase set depends on the profile (`--profile feature` shown above;
`small_task` keeps only `plan` / `validate_plan` / `implement`). Run
`orcho profiles list` for every profile's exact topology.

---

## Live monitoring

```bash
# Watch progress while the run is going
tail -f runspace/runs/$(ls -t runspace/runs | head -1)/progress.log
```

---

## Resume an interrupted run

```bash
# Continue from the last checkpoint
orcho run --resume 20260503_104135

# Or simply resume the most recent run
orcho run --resume
```

---

## Check the outcome quickly

```bash
orcho status           # status of the latest run
orcho metrics          # tokens and time
orcho evidence --format md   # the full evidence bundle, readable
```

---

## Diff delivery after the run

After `final_acceptance`, Orcho delivers the run-owned diff and
non-ignored untracked files from the isolated checkout into the
project's working checkout. Four actions are available:

| Action | What happens |
|----------|----------------|
| `apply` | Move the diff into the project checkout and leave it uncommitted for manual review or a batch commit. |
| `approve` | Move the diff, run `git add`, then `git commit` with a message from the release summary. |
| `skip` | Do not deliver — the run finishes as **DONE** (success, just without delivery). A recovery copy stays in run artifacts / the retained checkout; you can deliver manually later. |
| `halt` | Do not deliver and mark the run **HALTED** (non-success) with `halt_reason="commit_decision_halt"` — for when something is off and the run should be flagged, not counted as finished. |

`skip` and `halt` both "do not deliver" — the difference is only the
final run status (DONE vs HALTED).

The interactive default is `apply`: the next run will see the dirty
checkout through pre-run dirty intake and can continue on top of it via
`include`. For unattended runs the default stays `approve`, so the
project repo must have `user.name`, `user.email`, and working commit
hooks configured.

### Delivery on REJECTED (ADR 0069)

When `final_acceptance` returns **REJECTED** (`ship_ready: no`):

- **Interactive (TTY):** the delivery dialog still appears — with a
  warning that acceptance did not approve the change, and the safe
  default `skip` (a bare Enter delivers nothing). You may explicitly
  choose `approve`/`apply` and deliver the diff into your checkout —
  that is your conscious override (your repo, your decision).
- **Non-interactive (CI / no TTY):** delivery is hard-blocked
  (`not_applicable`) — a rejected change is never delivered
  automatically.

### Target-dirty guard

Before every `apply`/`approve`, Orcho checks whether `project_dir` is
clean (`git status --porcelain`). If the checkout already holds
parallel unsaved work — a separate change journey unrelated to this
run — delivery pauses so two change histories never mix in one commit:

| Mode | Behavior |
|-------|-----------|
| Interactive (TTY) | A prompt with three actions: `retry` (re-check the checkout after manual cleanup), `skip` (leave the diff in run artifacts), `halt` (`halt_reason="commit_decision_halt"`). The original action is preserved for `retry`. |
| Non-interactive (CI, MCP, no TTY) | Delivery records `commit_status="target_dirty"` with the porcelain list of dirty files in `target_dirty_paths`. The session halts with `halt_reason="commit_delivery_target_dirty"` — distinct from an operator halt and from an executor failure. The project checkout is not modified. |

If the parallel work is already finished and you want to commit it
first and then deliver the run — `git add -A && git commit`, then
resume or re-run.

Independently of the guard, staging is **path-scoped**: `git add --
<run-owned paths>` instead of `git add -A`. That closes the residual
race: even if parallel dirty work appears after the guard's check but
before `git add`, it cannot end up in the run's commit.
