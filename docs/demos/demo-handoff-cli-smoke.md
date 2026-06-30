# Phase-handoff CLI smoke — Live mock E2E

A copy-pasteable smoke that exercises the
[generic phase-handoff contract](../adr/0031-generic-phase-handoff-contract.md)
end-to-end via the bare `orcho` CLI + `sdk` API, with the
`MockAgentProvider` driving deterministic verdicts. Under 30 seconds
on a laptop. No MCP, no Web, no real-provider tokens.

This is the **executable proof** companion to ADR 0031 — every
contract invariant the ADR claims is verified live by the commands
below. If any step diverges from the expected output, the slice has
regressed.

## What this smoke proves

The four load-bearing invariants from ADR 0031:

1. **`decide ≠ resume`** — `phase_handoff_decide` writes the
   decision artifact but does not spawn a subprocess; `orcho run
   --resume` is the actual continuation.
2. **Exact-payload idempotency** — replaying the same decision is a
   silent no-op (`decided_at` not refreshed); a different `note` or
   `action` raises `InvalidPhaseHandoffState`.
3. **`meta.phase_handoff` is canonical** — the active payload reads
   off `meta.json`, decision artifacts persist under
   `phase_handoff_decisions/`, and the
   `phase.handoff_requested` event records the emission moment.
4. **Halt clears the active payload synchronously** —
   `meta.status="halted"` + `meta.halt_reason="phase_handoff_halt"`
   land in the same `decide` call; `meta.phase_handoff` is no longer
   active.

## Prerequisites

```bash
cd /path/to/orcho-core
pip install -e ".[dev]"             # editable install + pytest
which python                        # 3.12+
```

## Scenario A — `continue` action (manual override on rejected plan)

### A1. Bootstrap workspace + project copy

```bash
rm -rf /tmp/orcho_handoff_smoke
mkdir -p /tmp/orcho_handoff_smoke
cp -R examples/golden-api /tmp/orcho_handoff_smoke/project
python -m cli.orcho workspace init /tmp/orcho_handoff_smoke
```

### A2. Run under `advanced` profile — pause on phase handoff

```bash
ORCHO_WORKSPACE=/tmp/orcho_handoff_smoke/workspace-orchestrator \
python -m cli.orcho run \
  --task "Smoke: drive phase-handoff slice end-to-end" \
  --project /tmp/orcho_handoff_smoke/project \
  --profile advanced \
  --mock \
  --mock-validate-plan-reject 3 \
  --max-rounds 1 \
  --output summary
echo "rc=$?"
```

Expected: subprocess exits **rc=4**. The `advanced` profile declares
`human_feedback_on_reject` on `validate_plan` with
`LoopStep.max_rounds=2`. `mock-validate-plan-reject=3` forces all
plan rounds to reject, so the handoff fires on the final automatic
round.

Console tail:

```
[VALIDATE_PLAN] VALIDATE PLAN -- reviewer audits the plan (round 2)
  Plan validation
    verdict  REJECTED
    summary  P2: Mock validate_plan round 2 flagged missing coverage and rollback plan.
  ⚠ Phase handoff requested for 'validate_plan' (round 2/2): trigger='rejected'. Pausing for human decision.
```

### A3. Inspect the canonical payload

```bash
RUN_ID=$(ls /tmp/orcho_handoff_smoke/workspace-orchestrator/runspace/runs/)
RUN_DIR=/tmp/orcho_handoff_smoke/workspace-orchestrator/runspace/runs/$RUN_ID

python - <<PY
import json
meta = json.load(open("$RUN_DIR/meta.json"))
print("status =", meta["status"])
ph = meta["phase_handoff"]
print("phase_handoff.id =", ph["id"])
print("phase_handoff.type =", ph["type"])
print("phase_handoff.trigger =", ph["trigger"])
print("phase_handoff.round =", ph["round"], "/", ph["loop_max_rounds"])
print("phase_handoff.available_actions =", ph["available_actions"])
PY
```

Expected output:

```
status = awaiting_phase_handoff
phase_handoff.id = validate_plan:plan_round:2
phase_handoff.type = human_feedback_on_reject
phase_handoff.trigger = rejected
phase_handoff.round = 2 / 2
phase_handoff.available_actions = ['continue', 'retry_feedback', 'halt']
```

The `phase.handoff_requested` event records the same data:

```bash
grep "phase.handoff_requested" "$RUN_DIR/events.jsonl"
```

### A4. Decide `continue` via SDK + verify idempotency + conflict

```bash
python - <<PY
from sdk import phase_handoff_decide, InvalidPhaseHandoffState
RID = "$RUN_ID"
HID = "validate_plan:plan_round:2"
WS = "/tmp/orcho_handoff_smoke/workspace-orchestrator"

# Decide
d = phase_handoff_decide(RID, HID, "continue",
                         note="Smoke: manual override, accept rejected plan",
                         workspace=WS)
print("decision.action =", d.action)
print("decision.decided_at =", d.decided_at)

# Exact-payload idempotent replay — decided_at must NOT refresh.
replay = phase_handoff_decide(RID, HID, "continue",
                              note="Smoke: manual override, accept rejected plan",
                              workspace=WS)
assert replay.decided_at == d.decided_at, "decided_at refreshed on replay"
print("idempotent replay OK")

# Different note for the same handoff_id → conflict.
try:
    phase_handoff_decide(RID, HID, "continue", note="different",
                         workspace=WS)
    raise AssertionError("expected InvalidPhaseHandoffState")
except InvalidPhaseHandoffState:
    print("conflict-on-different-note OK")
PY
```

Note that this step never spawns a subprocess — `meta.status` is
still `awaiting_phase_handoff` after the decision, and a new
`phase_handoff_decisions/validate_plan_plan_round_2_<hash>.json`
file is on disk.

```bash
ls "$RUN_DIR/phase_handoff_decisions/"
cat "$RUN_DIR/phase_handoff_decisions/"*.json
```

### A5. Resume — core applies the override, runs remaining phases

```bash
ORCHO_WORKSPACE=/tmp/orcho_handoff_smoke/workspace-orchestrator \
python -m cli.orcho run \
  --resume "$RUN_ID" \
  --project /tmp/orcho_handoff_smoke/project \
  --profile advanced \
  --mock \
  --output summary
echo "rc=$?"
```

Expected: subprocess exits **rc=0**. Tail of the resumed run:

```
[DONE] Pipeline complete
  ✓ plan=skip | validate_plan=skip | implement=ok | review_changes=skip | repair_changes=skip | final_acceptance=ok
```

`plan` and `validate_plan` are **skipped** because the core resume
mechanics applied the `phase_handoff_override` marker — the loop
runner exited without rewriting the machine verdict (which would
re-enter the loop forever). Downstream phases ran under the
**original** `advanced` profile, not silently rewritten to `task`
or `advanced` mid-resume.

Final state:

```bash
python - <<PY
import json
meta = json.load(open("$RUN_DIR/meta.json"))
print("status =", meta["status"])
print("phase_handoff (cleared by finalize) =", meta.get("phase_handoff"))
PY
```

Expected:

```
status = done
phase_handoff (cleared by finalize) = None
```

The decision artifact under `phase_handoff_decisions/` survives —
it's audit log, not active state.

## Scenario B — `halt` action (terminate the paused run)

Tests the halt-finalisation invariant (artifact first, then
`meta.status` flip, then `meta.phase_handoff` cleared, atomically
inside the `decide` call) and halt-after-halt replay idempotency.

```bash
rm -rf /tmp/orcho_halt_smoke
mkdir -p /tmp/orcho_halt_smoke
cp -R examples/golden-api /tmp/orcho_halt_smoke/project
python -m cli.orcho workspace init /tmp/orcho_halt_smoke

ORCHO_WORKSPACE=/tmp/orcho_halt_smoke/workspace-orchestrator \
python -m cli.orcho run \
  --task "Smoke halt" --project /tmp/orcho_halt_smoke/project \
  --profile advanced --mock --mock-validate-plan-reject 3 \
  --max-rounds 1 --output summary
# rc=4 (paused)

RUN_ID=$(ls /tmp/orcho_halt_smoke/workspace-orchestrator/runspace/runs/)
python - <<PY
from sdk import phase_handoff_decide
import json
RID = "$RUN_ID"
HID = "validate_plan:plan_round:2"
WS = "/tmp/orcho_halt_smoke/workspace-orchestrator"

d = phase_handoff_decide(RID, HID, "halt",
                         note="Smoke: plan rejected, terminate run",
                         workspace=WS)
print("decision.action =", d.action)

# Halt is synchronous: meta.status already flipped before decide returns.
meta = json.load(open(
    f"/tmp/orcho_halt_smoke/workspace-orchestrator/runspace/runs/{RID}/meta.json"
))
print("status after halt =", meta["status"])
print("halt_reason =", meta["halt_reason"])
print("phase_handoff cleared:", meta.get("phase_handoff") is None)

# Halt-after-halt replay: artifact-only path (meta.phase_handoff is gone,
# but the decision artifact still gates idempotency).
d2 = phase_handoff_decide(RID, HID, "halt",
                          note="Smoke: plan rejected, terminate run",
                          workspace=WS)
assert d2.decided_at == d.decided_at, "halt-after-halt refreshed decided_at"
print("halt-after-halt idempotent OK")
PY
```

Expected:

```
decision.action = halt
status after halt = halted
halt_reason = phase_handoff_halt
phase_handoff cleared: True
halt-after-halt idempotent OK
```

## Scenario C — TTY interactive decision (in-process, no rc=4)

When `orcho run` is attached to a real TTY and `--no-interactive`
is not set, the phase handoff is resolved **in-process**: the
subprocess prints the action menu, reads the operator's choice at
the keyboard, records the decision artifact through the same SDK
function the other transports use (audit-trail invariant), and
either continues or terminates inside the same `orcho run`
invocation — no rc=4 exit, no separate `--resume` call.

```bash
# Run directly in a terminal (real TTY required).
orcho run \
  --task "TTY smoke: interactive continue" \
  --project /tmp/orcho_handoff_smoke/project \
  --profile advanced \
  --mock \
  --mock-validate-plan-reject 3 \
  --max-rounds 1 \
  --output summary
```

When the plan loop exhausts its budget the menu appears:

```
════════════════════════════════════════════════════════════════════
  Phase handoff — validate_plan (round 2/2)
════════════════════════════════════════════════════════════════════
  handoff_id : validate_plan:plan_round:2
  policy     : human_feedback_on_reject
  trigger    : rejected
  verdict    : REJECTED

  Last reviewer output:
    …critique…

  Choose action:
    1) ✅ continue       — accept the verdict, run remaining phases
    2) 🔁 retry_feedback — one extra plan round with human feedback
    3) 🛑 halt           — terminate the run synchronously

  Action [1/2/3 or continue/retry/halt]: 1

  Audit note (optional, press Enter for default): _
```

Pressing `1` + Enter + Enter writes the decision artifact through
`sdk.phase_handoff_decide(action="continue")`, applies the override
marker in-process, and continues into the remaining `advanced`
profile phases:

```
[DONE] Pipeline complete
  ✓ plan=ok | validate_plan=ok | implement=ok | review_changes=skip | repair_changes=skip | final_acceptance=ok
```

`rc=0`, `meta.status="done"`. The decision artifact under
`<run_dir>/phase_handoff_decisions/` looks identical to the
non-interactive Scenario A — every transport writes through the
same SDK function, so the audit trail does not distinguish
"TTY operator pressed 1" from "MCP client called
`orcho_phase_handoff_decide(action='continue')`". That's the
contract: three transports, one audit shape.

### Gating

The interactive prompt fires only when **all** of:

* `--no-interactive` flag is **not** set (CLI-level opt-out for CI /
  MCP / scripted callers — same flag the resume-intent prompter
  honours).
* `sys.stdin.isatty()` returns `True`.
* `sys.stdout.isatty()` returns `True` (piping `orcho run >run.log`
  is non-interactive by intent).

If any condition fails the orchestrator falls back to the non-
interactive path (persist + rc=4) and the operator decides later
via SDK / MCP / Web. This is the same persist-first path scripts
and CI already rely on; TTY interactivity is strictly additive.

### retry_feedback over TTY

Picking `2` opens a multi-line feedback prompt that closes on an
empty line:

```
  Action [1/2/3 or continue/retry/halt]: 2

  Feedback for the next plan round (required for retry_feedback). End with an empty line:
Add the auth-migration step before deployment.
Cover edge case A explicitly with a test.

  Audit note (optional, press Enter for default): _
```

Empty feedback is rejected (the SDK contract requires a non-empty
string for `retry_feedback`). Ctrl-D, Ctrl-C, or three malformed
inputs surface as "leave the run paused" — the operator can come
back later via SDK / MCP / Web. The prompt **never** silently picks
a default action.

### halt over TTY

Picking `3` records `action="halt"` through the SDK, which
synchronously flips `meta.status="halted"` and clears the active
`meta.phase_handoff`. `rc=0` (clean exit, not crash). The run is
terminal — no resume.

## Scenario D — `retry_feedback` action (not pinned in this smoke)

`retry_feedback` runs **one** extra human-directed
`plan → validate_plan` round and either closes the loop on
approval or chains a fresh handoff with `handoff_id` round+1.
The chain semantics are pinned in
[`tests/acceptance/test_full_mock_flow.py::TestStage5_PhaseHandoffResume`](../../tests/acceptance/test_full_mock_flow.py).
Run that test class directly for the equivalent of a Scenario C
smoke:

```bash
pytest tests/acceptance/test_full_mock_flow.py::TestStage5_PhaseHandoffResume -v
```

## What the smoke does NOT cover

- **Real-provider runs.** Mock provider only. Real-provider phase
  handoff has the same shape — only the verdict text and tokens
  differ.
- **Cross-project orchestration.** This single-project smoke does not
  exercise cross runs. Cross-plan handoff parity is covered by ADR 0038
  tests; proxied child handoff parity is covered by ADR 0039 tests. The
  cross CLI TTY prompt currently handles only `cross_plan:*` pauses in
  process; `project:<alias>:...` child-proxy pauses remain resumable
  off-band pauses (`rc=4`) so the child-owned decision is routed through
  SDK / MCP / child-run resume.
- **MCP transport.** Same contract, different transport — see
  [orcho-mcp DEMO-1B](../../../orcho-mcp/docs/demos/demo-1b-single-project-mcp.md)
  for the MCP-driven equivalent.
- **Web transport.** Same contract — exercised manually through
  the `phase_handoff_review` Streamlit state.

## Pointers

- Contract: [ADR 0031](../adr/0031-generic-phase-handoff-contract.md)
- Migration: [validate-plan-gate-retired.md](../migration/validate-plan-gate-retired.md)
- SDK: [`sdk/phase_handoff.py`](../../sdk/phase_handoff.py)
- Pinned tests:
  [`tests/sdk/test_phase_handoff.py`](../../tests/sdk/test_phase_handoff.py),
  [`tests/unit/pipeline/runtime/test_handoff_trigger.py`](../../tests/unit/pipeline/runtime/test_handoff_trigger.py),
  [`tests/acceptance/test_full_mock_flow.py::TestStage5_PhaseHandoffResume`](../../tests/acceptance/test_full_mock_flow.py)
