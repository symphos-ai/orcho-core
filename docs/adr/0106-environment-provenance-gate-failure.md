# ADR 0106 — Environment-provenance failure downgrades its phase gate to FAIL

Status: Accepted

## Context

A developer-side phase (`implement` / `repair_changes`) records a durable
**verification-environment receipt** (ADR 0076) under
`<run_dir>/verification_receipts/<phase>_round<N>.json`. Its `checks[]` carry the
environment-provenance invariants the phase actually ran — most importantly
`pipeline_import`, which proves the interpreter imported the codebase from the
*checkout* (cwd) and not from a stale install. Each check carries
`expected` / `actual` / `passed`; the receipt deliberately stores **no**
`all_passed` rollup, so pass/fail must be derived from `checks[].passed`.

A separate surface answers "what do this run's official verification gates look
like": the typed SDK projection `sdk.get_verification_timeline`
(`GateProjection` per gate) and the live DONE/HALTED render in
`pipeline.project.verification_timeline`. Both classify a gate from its **command
receipt** (`verification_command_receipts/`, ADR 0089/0095): present → PASS, or
FRESH when an auto-run reported it `skipped_fresh`.

The dogfood run **20260625_174403_06c642** exposed a gap between *freshness* and
*outcome*. A required gate scheduled at `after_phase(implement)` had a fresh,
exit-0 command receipt, so the timeline reported it **FRESH / PASS** — green.
But the `implement` phase's `verification_environment` receipt recorded
`pipeline_import.passed = false`: the interpreter that produced that "passing"
work imported `pipeline` from outside the checkout. The receipt that proved the
environment was wrong existed and was durable, yet **no gate surface read it**.
A green-looking gate was therefore resting on a broken environment — exactly the
incident the provenance receipt was introduced (ADR 0076) to make impossible to
hide. FRESH was masking a real failure.

Two constraints framed the fix:

- **No new status vocabulary.** `GateStatus` is exactly the six values
  `PASS` / `FAIL` / `MISSING` / `STALE` / `SKIPPED` / `FRESH` (no `MANUAL`); a
  provenance break must map onto the existing `FAIL`, not introduce a seventh
  value or a parallel `freshness` field.
- **One rule, two surfaces.** The typed SDK projection and the live render must
  apply an identical downgrade rule, or the cockpit (SDK/MCP) and the terminal
  DONE block would disagree about the same run.

## Decision

A failed environment-provenance check is a **blocking provenance signal** for
the gate(s) scheduled at that phase: the gate is reported `FAIL` regardless of
its own command-receipt state (including `PASS` / `FRESH`), and it joins the
run's failed residual. The downgrade fires **only** when the phase receipt of a
gate's scheduled phase actually failed a check — a present/healthy/missing/stale
gate with no provenance break keeps its prior semantics, and `FRESH` keeps its
meaning of "successful, fresh, present + skipped_fresh".

### One read-only reader, one shared downgrade rule

- `pipeline/evidence/verification_receipt.py` gains the frozen dataclass
  `EnvProvenanceFailure` (`phase`, `round`, `check`, `expected`, `actual`,
  `receipt_path`) and a pure reader `environment_provenance_failures(run_dir)`.
  It reads the same `load_verification_receipts(run_dir)` receipts, emits one
  record per check whose `passed` is not truthy (failure derived from
  `checks[]`, since the receipt stores no `all_passed`), and degrades any IO /
  JSON error to `()` — projections must never raise.
- `pipeline/verification_readiness.py` gains the single downgrade rule both
  surfaces call: `command_phase_schedule(contract)` builds the `command → phase`
  map **only** from `contract.schedule` entries whose hook is `before_phase` /
  `after_phase` with a named phase (so `before_delivery` and `manual_only` gates
  get no provenance link and are untouched), and
  `environment_provenance_gate_failures(command_phases, failures)` maps each
  phase-scheduled gate to its phase's failure, returning a
  `ProvenanceGateFailure` carrying the operator-evidence and the human-readable
  `detail` string (`"<check>: expected <X> actual <Y>"`). It is pure: it neither
  reads receipts nor raises.

Keeping the rule in one place is load-bearing: if the SDK projection and the
live render each re-derived "which gate failed provenance", the typed projection
and the DONE block could drift. They call the same helper instead.

### Typed SDK projection

`GateProjection` gains an additive field **`detail: str = ""`** — a
human-readable operator note, empty for an ordinary gate, populated when a
provenance break downgrades the gate (e.g.
`"pipeline_import: expected <X> actual <Y>"`). In
`sdk.verification_timeline._project_with_contract`, a gate whose scheduled phase
has a provenance failure (and is not manual) is forced to `status = "FAIL"`
over whatever `_gate_status` returned (including `FRESH` / `PASS`), its
`receipt_path` is repointed at the failing `verification_environment` phase
receipt, `detail` is filled, a `rerun_hint` is produced via the existing
non-present-gate path (`suggested_verify_commands` scoped to the one command),
and the command is added to the failed bucket so it appears in
`residual_failed`. The six-value `GateStatus` enum is unchanged. The additive
field is reflected in the regenerated `docs/sdk_schema.json`
(`python tools/dump_sdk_schema.py`).

### Live DONE/HALTED projection

`pipeline.project.verification_timeline._run_level_projection` calls the same
reader + shared helper inside its existing `suppress(Exception)` block, so a
provenance-failed required gate joins `residual_failed`, becomes part of
`blocking_residual` under a `require` policy, and feeds the shared `fix` hint —
rendered by `render_verification_gate_done_block` with no render change. The
projection's never-raise contract is preserved.

### What is deliberately preserved

- The six `GateStatus` values, and the meaning of `FRESH` (successful, fresh,
  present + `skipped_fresh`). The downgrade applies **only** when the gate's
  phase receipt actually failed a check.
- `manual_only` / `before_delivery` gates (no phase schedule), and `inherited` /
  `present` / `missing` / `stale` semantics for gates with no provenance break.

## Consequences

- A required gate scheduled at a phase whose `verification_environment` receipt
  failed a check can no longer read as FRESH/PASS: it is `FAIL`, lands in
  `residual_failed` / `blocking_residual`, and carries self-sufficient
  operator-evidence (`receipt_path` at the phase receipt, the failing check name
  with `expected` / `actual` in `detail`, and a non-empty `rerun_hint`) — no raw
  log reading required.
- The typed cockpit projection and the terminal DONE/HALTED block agree on the
  downgrade because both call the one shared rule in
  `pipeline/verification_readiness.py`.
- `GateProjection.detail` is a **public wire change** (it changes
  `docs/sdk_schema.json` and the MCP-visible payload shape, ADR 0021).

### MCP companion (mandatory, same delivery)

Per the repo's MCP Validation rule, this wire change does **not** ship without
its `orcho-mcp` companion in the **same delivery**: the cockpit Pydantic model
mirrors the new `GateProjection.detail` field and re-projects the `FAIL`
downgrade, `orcho-mcp/docs/mcp_schema.json` is regenerated to match the live
tool schema, and an E2E mock-smoke pins the mirrored field + FAIL projection.
A core-only delivery of the changed public schema is not acceptable; if
`orcho-mcp` is physically unavailable, the delivery halts as externally blocked
rather than being accepted core-only.

The companion E2E mock-smoke
(`orcho-mcp/tests/acceptance/test_verification_cockpit_provenance_smoke.py`)
must run against the **matching** core — the checkout that actually carries
`GateProjection.detail` — or it is meaningless. Because a workspace `orcho-mcp`
commonly resolves `sdk` from a stale editable/stable install that predates the
field, the smoke would otherwise skip and silently mask the mandatory check. The
smoke therefore reads two env hooks: `ORCHO_MCP_CORE_SRC=<checkout>` installs a
meta-path finder that rebinds only the engine packages (`sdk` / `pipeline` /
`core` / `cli` / `agents`) to the in-development checkout (never its `tests`
package), so `import sdk` resolves to the matching core without a global
reinstall; and `ORCHO_MCP_REQUIRE_COMPANION=1` makes the companion mandatory —
if the resolved core still predates the field the smoke **fails as externally
blocked** instead of skipping. Delivery verification runs the smoke with both
set; a bare local `pytest` keeps the benign skip for unrelated work.

This ADR builds on ADR 0076 (durable verification-environment receipt — the
source of the provenance signal), ADR 0095 (verification-gate timeline durable
trails — the surface being corrected), ADR 0089/0097/0090 (delivery receipt
continuity, delivery verification policy, and require-gate no-silent-green — the
residual / policy buckets this downgrade feeds), and ADR 0021 (public SDK
boundary — why the additive field needs the MCP companion). It is append-only
and supersedes none of them.
