# golden-api — orcho REA-0 golden scenario

Tiny fixture project used by the **Run Evidence and Audit (REA)** roadmap
as a deterministic, mock-only end-to-end run.

It deliberately contains a known bug:

* `app/validation.py` — `validate_payload(...)` returns `200` for every
  payload, including ones missing required fields.
* `tests/test_validation.py` — encodes the desired behaviour. Two tests
  fail until the bug is fixed (missing-name and missing-email cases).

This combination resembles a real fix (failing test → implementation
change → review → test command) without requiring real model APIs.

## One-command run

```bash
cd /path/to/orcho-core
ORCHO_WORKSPACE=/tmp/orcho-golden \
  python -m pipeline.project_orchestrator \
    --project examples/golden-api \
    --task "Fix validation bug in sample API" \
    --mock \
    --profile advanced \
    --mock-validate-plan-reject 1 \
    --run-id GOLDEN_REA0
```

Outputs land under `${ORCHO_WORKSPACE}/runspace/runs/GOLDEN_REA0/`:

* `meta.json` — session shape (status, per-phase outputs, rounds)
* `events.jsonl` — append-only event spine
* `metrics.json` — token / latency rollup
* `progress.log` — human-readable phase timeline
* `evidence.json` — REA-0 placeholder; REA-3 will enrich it

The `--mock-validate-plan-reject 1` knob forces one validate_plan rejection
followed by an approval, so the run exercises the full advanced-profile loop:

```text
hypothesis → plan → validate_plan(reject) → plan(replan) → validate_plan(approve)
           → implement → review_changes → repair_changes → final_acceptance → done
```

Add `--stream-output` to see each phase's mock content live in stdout.

## CI / acceptance test

`tests/acceptance/test_golden_scenario.py` runs this exact scenario as a
subprocess and asserts:

1. Every advanced-profile phase fires (`hypothesis` … `final_acceptance`).
2. The validate_plan reject + replan loop executes (two verdicts, false then true).
3. Event-spine `seq` is strictly monotonic with no gaps.
4. `run.start` / `run.end` frame the timeline.
5. The `evidence.json` placeholder is written with the locked
   `schema_version="0-placeholder"`.

This is the acceptance baseline for every future REA milestone — any
regression in the golden loop must fail this test in CI.

## Why mock-only?

REA-0's whole point is determinism without network. The
`MockAgentProvider` returns canned responses, so the run finishes in
under a second on a warm Python interpreter and produces the same event
stream every time. Real Claude / Codex runs follow the same pipeline
shape but add latency, non-determinism, and API costs — appropriate for
manual verification, not CI gate.
