# ADR 0101 — Provider-access recovery projection and runtime/model override

Status: Accepted

## Context

A run can die terminally when the configured agent runtime has valid
credentials but no access to the requested provider surface — an expired or
disabled subscription, a model the plan does not include, an org-level access
toggle. `core.io.retry` already classifies these as `AgentAccessError`
(ADR-adjacent work, `_ACCESS_PATTERNS`) and never retries them: blind retry
cannot restore access.

Until now the failure record only told the operator *that* the run failed.
There was no durable, provider-neutral description of *what the operator can do
next*, and two hazards lurked in the failure path:

1. **Raw payload leakage.** A provider CLI emits its init/handshake as JSONL on
   stdout/stderr. If that text reaches `session['failure']['error']` or the
   `run.end` error verbatim, an operator-visible field leaks the raw provider
   init payload.
2. **Provider coupling.** Any "switch runtime" hint risks hard-coding a
   provider name, violating the core's provider-neutral contract
   (`orcho-core` owns the protocol; plugins own provider behavior).

## Decision

### Recovery protocol (this ADR's T1 surface)

On a terminal `AgentAccessError`, the run persists a **provider-neutral
recovery projection** into both `session['failure']` and the `run.end` event,
merged via `**failure_meta`. The projection is built by the pure functions in
`pipeline/project/provider_recovery.py`:

```
{
  "failure_kind": "provider_access",
  "recoverable": False,
  "recommended_action": "switch_runtime_or_restore_access",
  "failed_phase": <phase>,
  "runtime": <configured runtime of the failed phase>,
  "model": <resolved model of the failed phase>,
  "recovery_actions": [ ... ],
}
```

`recovery_actions` is the **durable evidence list**:

- `{"action": "retry"}` and `{"action": "halt"}` are **always** present and
  provider-neutral.
- One `{"action": "replace", "runtime": ..., "model": ...}` is appended per
  replacement candidate, and only when candidates exist.

### Candidate source (provider-neutrality)

Replacement candidates are derived **only** from configured `(runtime, model)`
pairs — `AppConfig.phase_runtime_map` + `AppConfig.phase_model_map`, walked per
phase, deduplicated, with the failed pair excluded. No provider name is
hard-coded. An `AgentRegistry.names()`-style set may be passed solely to
**validate** that a candidate runtime still exists; it is never the *source* of
pairs. When no configured alternative exists, `recovery_actions` carries only
`retry`/`halt`.

### Sanitary boundary

For `AgentAccessError`, every operator-visible text field
(`session['failure']['error']`, the `run.end` `error`, and any recovery text)
draws from the sanitized provider-access channel
(`core.io.retry.provider_access_detail`, layered on `_provider_access_detail`),
which strips provider JSON/JSONL plumbing and keeps only human-readable error
lines plus a provider-neutral next step. Other exception classes keep their
historical raw-text source unchanged.

### Execution path and SDK Action contract (forward references)

The durable recovery record is the substrate for an executable override flow
delivered in later subtasks of this plan:

- The operator's runtime/model override is persisted into durable `meta`
  **before** a `ProjectRunRequest` is built, threaded through the canonical
  `orcho_run_resume` executor (`RunService.resume`), re-applied on resume, and
  isolated to the failed phase — no silent fallback. (T2)
- The SDK `next_actions` projection reads **only** `meta`. It projects `retry`
  and each `replace` candidate as an `orcho_run_resume` Action, and **every**
  such Action carries a `run_id` so a default-latest resolution cannot target a
  foreign run. `halt` has no executable tool: it stays a durable recovery
  option in `recovery_actions`/`meta` and is **not** projected into a SDK
  Action. (T3)

## Runtime/model override execution path (T2)

The operator's chosen replacement is delivered as an additive frozen DTO,
`ResumeCommand.runtime_override = RuntimeOverride{phase, runtime, model}`
(default `None`). The canonical resume tool and its arg shape are fixed:

```
orcho_run_resume(run_id, runtime_override={phase, runtime, model})
```

`run_id` is mandatory — it addresses the specific run so a default-latest
resolution cannot target a foreign run.

`RunService.resume` (`sdk/run_control/service.py`) handles the override **before
building the `ProjectRunRequest`**: it calls
`sdk.run_control.runtime_override.persist_runtime_override`, which

1. validates `(runtime, model)` against the configured replacement candidates
   for `phase` (the same T1 candidate set — `phase_runtime_map` +
   `phase_model_map`; `AgentRegistry.names()` only validates runtime
   existence); a non-candidate pair raises and aborts resume (no silent
   fallback);
2. writes `meta['runtime_override'] = {phase, runtime, model, decided_at,
   note}` idempotently on the `(phase, runtime, model, note)` payload —
   re-persisting the same decision is a no-op; a divergent decision is a
   conflict (raised), mirroring the phase-handoff / waiver model.

On resume the pipeline reads the persisted record at the top of
`pipeline/project/session_run.py::_resolve_profile_runtime` — ahead of
`setup_run_id` rewriting `meta.json` — and threads it into
`runtime_setup.setup_runtime → _synthesize_phase_config`, which rebuilds **only
the named phase's** slot from the override pair via the existing
`provider.resolve` path. No other phase is touched; with no record the resume
is byte-identical.

### MCP visibility

The override is *applied* from **durable `meta`**: every resume transport that
re-enters `run_project_pipeline` — the SDK `RunService.resume`, the CLI
`orcho-run --resume`, and the MCP `orcho_run_resume` subprocess (which spawns
`orcho-run --resume`) — reads and applies the persisted record, and the persisted
record also survives the resume itself (`init_session_with_atexit` carries
`meta['runtime_override']` forward into the fresh session before it rewrites
`meta.json`, alongside the `phase_handoff` carry-forward), so a later resume /
SDK / evidence read still sees the operator's decision.

But *persisting* the override is the job of whichever transport receives the
operator's choice, and the SDK `next_actions` **replace** Action delivers that
choice through the MCP `orcho_run_resume` tool. The strict MCP tool schema would
reject (or drop) an unknown `runtime_override` arg, so the tool signature is
**updated synchronously** (cross-repo companion in `orcho-mcp`, separate git
history): `orcho_run_resume(run_id, profile=None, runtime_override=None)` accepts
a typed `RuntimeOverrideArg{phase, runtime, model}` and, after the pre-flight
resume guard clears and **before** the supervisor spawns the subprocess, calls
orcho-core's `sdk.run_control.runtime_override.persist_runtime_override` for the
resolved run dir. orcho-core stays the single validation + persistence authority
(non-candidate pair → `RuntimeOverrideError`, divergent re-decision →
`RuntimeOverrideConflict`, both surfaced as the MCP `InvalidPlanError`); the MCP
layer only resolves the run dir and forwards the pair. The in-process SDK
`RunService.resume` path persists the same way for non-MCP callers (CLI /
library).

#### Verifiable `orcho-mcp` companion (resolves the MCP-compatibility review gate)

The companion is **not** a future promise — it is delivered as working-tree
changes in the sibling `orcho-mcp` repo (separate git history; left uncommitted
under this change's handoff mode). Because that repo is outside the orcho-core
review subject, the companion is **attached to this subject** as two in-tree
proof artifacts a reviewer can open and re-run without leaving orcho-core:

- [`0101-orcho-mcp-companion.patch`](0101-orcho-mcp-companion.patch) — the full
  unified diff of the companion (schema, tool, executor, regenerated
  `mcp_schema.json`, and the new mock-smoke), appliable from the `orcho-mcp`
  root.
- [`0101-mcp-companion-proof.md`](0101-mcp-companion-proof.md) — captured live
  machine output proving the strict tool schema accepts `runtime_override`, the
  executor's persist-before-spawn ordering, the orcho-core signature match, and
  the green `schema-check` / mock-smoke receipts, each with a reproduction
  command.

Concrete, reviewable artifacts:

- `src/orcho_mcp/schemas/run_control.py` — adds the typed `RuntimeOverrideArg`
  (`extra="forbid"`; required `phase` / `runtime` / `model`), mirroring this
  ADR's arg shape; exported from `src/orcho_mcp/schemas/__init__.py`.
- `src/orcho_mcp/tools.py` — the public `orcho_run_resume` tool gains the
  `runtime_override: RuntimeOverrideArg | None = None` parameter and forwards it.
- `src/orcho_mcp/run_control/lifecycle.py` — `resume_run(...)` calls the new
  `_persist_runtime_override`, which resolves the run dir and invokes orcho-core's
  `persist_runtime_override` **after** the pre-flight guard and **before**
  `supervisor.resume(...)` spawns the subprocess; `RuntimeOverrideError` /
  `RuntimeOverrideConflict` map to the typed `InvalidPlanError`.
- `docs/mcp_schema.json` — the regenerated structural snapshot now carries
  `$defs.RuntimeOverrideArg` and the `runtime_override` property on the
  `orcho_run_resume` tool. This file is gated by `make schema-check`
  (`tools/dump_mcp_schema.py --check`), which dumps the **live** registered tool
  schema and fails on drift — so a green `schema-check` is machine proof that the
  strict, in-process `orcho_run_resume` tool actually exposes `runtime_override`
  (introspection of the live `mcp.list_tools()` confirms: `runtime_override` is a
  property and `$defs.RuntimeOverrideArg` has `required = [phase, runtime, model]`,
  `additionalProperties = false`). The arg is therefore accepted and forwarded,
  not silently dropped.
- `tests/unit/run_control/test_resume_runtime_override.py` — three passing
  mock-smokes pin the wire end-to-end: (1) the override lands in `meta.json`
  **before** the supervisor is asked to spawn; (2) a plain resume writes no
  `runtime_override`; (3) a non-candidate pair raises `InvalidPlanError` with no
  spawn and no write. orcho-core's own
  `tests/unit/pipeline/project/test_runtime_override_resume.py` remains the
  authority for `persist_runtime_override`'s candidate validation, idempotency,
  conflict, and resume carry-forward.

## SDK next_actions projection (T3)

`sdk/actions.py::compute_next_actions` projects the durable recovery record
into typed `Action`s for a run whose `status` is a resumable terminal state and
whose `meta.failure.failure_kind == 'provider_access'`. It reads **only `meta`**
— the candidates were persisted into `meta.failure.recovery_actions` (T1); the
SDK layer does not import the agent registry or recompute candidates.

The projection emits, all with `tool = orcho_run_resume`:

- **retry** — args `{run_id}` (reusing the existing `_resume_action`, which
  already carries `run_id`; the provider-access intent clarifies that retry only
  helps after access is restored), `optional=True`;
- **replace** — one per configured replacement candidate, args **exactly**
  `{run_id, runtime_override: {phase, runtime, model}}` — the shape
  `RunService.resume` validates + persists (T2). `run_id` is **mandatory** on
  every replace Action so a default-latest resolution cannot target a foreign
  run.

`halt` is **not** projected as an SDK Action: it has no executable tool and the
run is already terminal, so it stays a durable recovery option in
`meta.failure.recovery_actions` (and the typed `ProviderAccessRecovery`) and
never reaches `next_actions`. No invented halt tool is introduced. When there
are no replacement candidates, only the retry Action is emitted. The
provider-access branch **replaces** the flat resume Action (they are mutually
exclusive), so a provider-access run never emits a duplicate plain
`orcho_run_resume {run_id}`.

The recovery record is also promoted to a typed public surface,
`sdk.ProviderAccessRecovery` (with `sdk.RecoveryReplacement`), exposed as the
additive `ErrorsAndHalt.recovery` field — `None` for every non-provider-access
run. `docs/sdk_schema.json` is the structural snapshot of these additions
(regenerated via `tools/dump_sdk_schema.py`); `halt`-is-meta-only and the
replace-Action arg shape are documented here (this ADR), the prose home, since
the schema snapshot is structural.

## Consequences

- The failure record gains a stable, provider-neutral recovery contract that
  evidence collection (`pipeline/evidence/collector.py`) propagates verbatim.
- Operators never see raw provider init payloads in visible fields.
- Adding a provider is a config/plugin concern; the recovery surface needs no
  change because candidates come from configured pairs, not from core code.
- The runtime override is delivered through durable meta and isolated per
  phase; resume transports converge on one application point.
- This ADR is append-only; supersede rather than edit.
