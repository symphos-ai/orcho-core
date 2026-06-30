# ADR 0030 — Runtime Context Autonomy and Contract Recovery

Date: 2026-05-19
Status: Accepted
Extends: ADR 0029 (context lifecycle) — refines the rollout rule from
"observe-first, act on telemetry later" to "observe-first, act on
**contract failure** later"
Related: ADR 0020 (run evidence), ADR 0026 (session-aware prompt parts),
ADR 0027 (execution surfaces), ADR 0028 (cache-first physical wire layout),
ADR 0029 (context lifecycle)

## Context

ADR 0029 introduced the context-lifecycle observability stack
(`context_growth`, `context_clearing`, `context_pressure`,
`runtime_compaction`, the protected `coding_agent_compaction`
contract) under an explicit rollout rule: "observe-only first,
enable automatic action only after evidence justifies it".

After implementing the M14.4.x surfaces through M14.4.5 and probing
real provider CLIs (see
`docs/research/2026-05-18-runtime-context-telemetry-probe.md`),
three forces became visible:

1. **Runtimes own their internal context.** Claude Code may
   auto-compact, summarise prior turns, rotate cache, or run its own
   recovery passes — entirely outside Orcho's observability. Codex
   CLI does the same with no live telemetry exposed at all. A
   future runtime may behave differently again. Orcho cannot
   reliably mirror or control any of this.
2. **Telemetry is asymmetric and partial.** Claude exposes
   `modelUsage[model].contextWindow` per call; Codex exposes only
   post-call `turn.completed.usage`; Gemini surface unknown. An
   automation that depends on uniform telemetry can run honestly
   on one runtime and degrade silently on another.
3. **Phase contracts are the only thing Orcho can verify
   universally.** Every phase emits a machine-parseable result
   (`plan_json`, `review_json`, `release_json`, …). Whether the
   runtime preserved its internal context or compacted it away is
   irrelevant if the parser succeeds. If the parser fails, the
   runtime broke the contract — and that we can react to without
   knowing why.

The previous draft direction (Context Pressure Policy: detect
fill ratio crossing a threshold, then proactively hand off via the
compaction contract) was built on telemetry-as-correctness-input.
That coupling is too tight. A runtime that auto-compacts behind
our back invalidates our running fill estimate. A runtime with no
window telemetry cannot participate. The same proactive policy
either fails silently or refuses to fire on half the supported
runtimes.

This ADR establishes a different boundary.

## Decision

**The correctness boundary is the phase contract, not the runtime's
internal context state.**

```text
agent runtime     →  owns its internal context lifecycle entirely
                     (compact, summarise, rotate cache, expose
                      telemetry — provider-specific, opaque)

orcho             →  owns phase contracts, artifacts, recovery,
                     and cross-phase handoff
```

If the runtime's internal compaction preserves the phase contract,
Orcho does nothing. If it breaks the contract, Orcho reacts.

Two corollary rules:

* **Telemetry is observe-only in Phase 1.** `context_pressure`,
  `context_growth`, `context_clearing`, `runtime_compaction` feed
  the live card, evidence bundle, and future analytics. They never
  trigger restart, handoff, or any other correctness-affecting
  action in Phase 1.
* **Primary automation is contract-failure recovery.** When the
  runtime exits successfully but its output cannot satisfy the
  phase contract (parser failure, missing required field, empty
  assistant body after successful return, output unrelated to
  current contract, phase artifact missing after claimed
  completion), Orcho retries the same phase once with a fresh
  runtime session and an explicit recovery bundle.

## What Runtimes May Do (And We Do Not Care)

A runtime may, at any time and without informing Orcho:

* auto-compact its own context;
* summarise prior turns into a shorter form;
* preserve or drop its cache;
* expose rich telemetry, partial telemetry, or none;
* keep one physical session for an entire conversation or rotate
  it internally;
* return a successful exit on a malformed payload it generated.

Orcho's correctness contract is:

```text
orcho prompt + phase state + artifacts
        →
parseable phase result satisfying the phase contract
```

If that contract holds, runtime internals are out of scope. If it
breaks, recovery (below) handles the break.

## Telemetry Semantics

Telemetry surfaces from ADR 0029 remain valuable and stay shipped:

* `context_pressure` — observability for the live card, dashboard,
  evidence bundle, and future advisory policy.
* `runtime_compaction` — evidence for debugging and audit; records
  observed events when a runtime exposes them.
* `context_growth` / `context_clearing` — sibling surfaces that
  describe per-call observables and clearing eligibility under the
  M14.2 taxonomy.

What this ADR explicitly forbids in Phase 1:

* using `context_pressure.context_fill_ratio` as a restart trigger;
* using `runtime_compaction` event detection as a restart trigger;
* hand-off via the `coding_agent_compaction` contract in the
  absence of a contract failure;
* any correctness path that depends on a specific runtime's
  telemetry being present.

What a future Phase 2 may add (out of scope here):

* repeated-contract-failure-after-compaction → prefer fresh
  sessions earlier;
* sustained-high-pressure → warn in UI / dashboard;
* a separate session-policy layer for profiles that opt into
  proactive handoff explicitly.

## Recovery Triggers (In Scope)

Contract recovery fires when the runtime exits successfully enough
to produce output, but Orcho cannot accept the phase result. These
are all **content-level** failures:

* plan parser failed;
* review JSON contract failed;
* final-acceptance contract failed;
* required contract field missing;
* empty assistant output after successful runtime exit;
* output clearly unrelated to the current phase contract (e.g.
  reviewer narrating implementation steps);
* phase artifact missing after the runtime claimed completion.

## Recovery Triggers (Explicitly Out Of Scope)

Contract recovery does **not** fire on:

* runtime authentication failure;
* CLI crash, sandbox failure, or tool error;
* guardrail abort;
* user cancellation;
* test-suite failure that is itself a valid phase result.

These remain ordinary runtime errors and quality-gate failures.
Treating them as contract failures would trigger spurious retries
on conditions that fresh-session retry cannot fix.

## Recovery Action

On contract failure:

1. Call `agent.reset_session()` if the runtime exposes it.
2. Render the recovery bundle (Orcho-owned, runtime-agnostic).
3. Re-invoke the same phase once with the recovery bundle.
4. Parse the result again.
5. Record recovery evidence — both the original parser error and
   the retry's result — into `phase_log` and the run evidence
   bundle.

Default policy lives in `core/_config/config.defaults.json` and
`core/infra/config.py`, not in profile JSON unless the profile
schema is explicitly extended for it:

```json
{
  "runtime_recovery": {
    "enabled": true,
    "max_contract_retries": 1,
    "fresh_session_on_contract_failure": true
  }
}
```

`max_contract_retries: 1` is intentional. A single fresh-session
retry handles the typical compaction-broke-the-contract case. More
retries multiply cost without proportional payoff; persistent
failure after one retry indicates a real prompt / model / contract
mismatch that needs human attention, not blind looping.

## Recovery Bundle

The recovery prompt is Orcho-owned and runtime-agnostic. It should
be rendered by existing prompt infrastructure (the typed-part
composer from ADR 0026 + the cache-first assembler from ADR 0028),
not hand-built inside a runtime adapter.

Contents:

* original task;
* current phase name;
* current phase machine contract (the JSON schema or grammar the
  parser expects);
* approved plan or current plan attempt (when relevant);
* relevant phase history (prior rounds in the same loop);
* `git diff` summary / changed files (when relevant);
* quality-gate output (when relevant);
* prior invalid model output verbatim;
* exact parser error text;
* explicit instruction to return only the required contract shape.

All runtimes receive the same recovery-bundle semantics. The
runtime sees only a prompt; it does not need to know that the
prompt is a recovery attempt.

## Session Policy

Phase 1 policy on physical-session reset:

* **Only contract failure can force fresh-session retry.**
* `context_pressure` crossing a threshold does **not** force
  restart.
* An observed `runtime_compaction` event does **not** force
  restart.

This keeps the trigger surface small and reactive. Proactive
session policies remain available as later, opt-in layers — not
defaults.

## Codex Telemetry as Observability Layer

Codex-specific telemetry work remains valuable, but scoped strictly
to observability. New modules under `agents/runtimes/`:

* `codex_telemetry.py` — parses Codex rollout JSONL where exposed;
  extracts `event_msg.payload.type == "token_count"`,
  `model_context_window`, `last_token_usage`, `rate_limits`;
  detects sharp token-count drops as inferred compaction evidence.
* `codex_model_windows.py` — model-window constants for
  `config_static` source fallback when rollout is unavailable
  (`--ephemeral`, missing rollout file, private schema change).

`CodexAgent` exposes the same agent attributes as `ClaudeAgent`:
`last_context_window_tokens`, `last_context_used_tokens`,
`last_context_remaining_tokens`, `last_runtime_compaction_event`,
plus Codex-specific extras (`last_codex_last_usage`,
`last_codex_rate_limits`). The existing `context_pressure` and
`runtime_compaction` surfaces consume these via the same attribute
contract.

This is **runtime-extending** the existing telemetry surfaces, not
a parallel system. The surfaces in ADR 0029 stay as the canonical
contract; provider adapters fill them as they can.

A private rollout schema change must degrade observability
cleanly. It must not break pipeline execution.

## Inferred Compaction Signal

A sharp token-count drop in Codex rollout is real observed evidence
that runtime compaction happened, but it remains evidence — not a
trigger:

```python
if previous_used and current_used < previous_used * 0.70:
    last_runtime_compaction_event = {
        "kind": "runtime_auto_compacted",
        "trigger": "rollout_token_count_drop",
        "pre_used_tokens": previous_used,
        "post_used_tokens": current_used,
    }
```

This stamps an event into the existing `runtime_compaction`
surface. It does not restart the session. If the next phase output
violates the contract, contract recovery handles it; if it
satisfies the contract, the inferred compaction was harmless and
nothing further happens.

The 0.70 threshold is a heuristic for detection, not a policy
knob. It can be tuned via config but should never become a restart
trigger.

## No Runtime-Specific Behavior In Correctness Path

No runtime-specific behavior is required for contract recovery
except an optional `reset_session()` capability. Runtimes that
cannot reset their physical session still receive the same
recovery prompt on their next invocation — the runtime simply
continues in the same session and answers a clearer recovery
prompt. The recovery mechanism stays useful even on
session-incapable runtimes.

This removes any notion of "Claude as observability baseline" or
"Codex parity with Claude". Runtime-reported telemetry is
optional and provider-specific; Orcho consumes it through common
agent attributes when present and falls back honestly when not.

## Non-Goals

This ADR explicitly does NOT:

* replace runtime-internal compaction (runtimes own that);
* make context pressure a restart trigger in Phase 1;
* rely on Codex rollout parsing for correctness (only for
  observability);
* implement cache TTL split for Codex (Claude's
  `ephemeral_5m` / `ephemeral_1h` split is Anthropic-specific);
* add unknown profile-JSON keys (recovery defaults stay in
  `core/_config/config.defaults.json` until a profile-schema
  extension is justified);
* create a new telemetry system parallel to `context_pressure` /
  `runtime_compaction` (Codex telemetry extends the existing
  surfaces);
* re-frame the M14.4 protected `coding_agent_compaction` contract
  as a hand-off contract (it becomes part of the **recovery
  bundle** — the preserve-list that the recovery prompt may carry
  forward when relevant; not an autonomous hand-off trigger).

## Consequences

Positive:

* Correctness boundary is provable: Orcho parses the contract; if
  it parses, the run is correct, period.
* Runtime-agnostic by construction: any future runtime joins by
  exposing an `invoke` + `reset_session`; no telemetry contract
  required for correctness.
* Smaller surface area to maintain: one detection path (parser
  failure), one action (fresh-session + recovery bundle), one
  evidence record.
* M14.x surfaces from ADR 0029 retain their value as observability
  and future-policy substrate without being coupled to correctness
  today.
* The Codex blind spot stops blocking decisions: Codex can have
  no live telemetry and the system still recovers correctly when a
  contract breaks; telemetry catches up later as observability.

Negative:

* Longer feedback loop: we wait for the parser to fail rather than
  predicting failure. The cost is one wasted phase invocation per
  contract-failure event. In return we get reliable, universal
  recovery instead of fragile prediction.
* Recovery cannot prevent context exhaustion mid-phase: if the
  runtime exhausts its window inside a single invocation and
  produces a malformed result as a consequence, the retry happens
  after the cost has already been incurred. This is the explicit
  trade — runtimes own that boundary, not Orcho.
* Profile authors lose the (never-shipped) "auto-handoff at
  threshold" intuition. The trade is simpler operator UX:
  reactive, observable, and never silently rerouting requests.

Migration:

* All currently merged M14.x surfaces (M14.1 – M14.4.5) remain in
  place; nothing reverts. Their evidence keeps stamping; the live
  card and debug-block metrics keep rendering. Phase 1 correctness
  no longer reads them.
* The previously discussed `coding_agent_compaction` contract
  registration in M14.4 stays exactly as is — a code-owned block
  in the catalog, currently dormant. It activates the moment a
  recovery bundle wants to embed a preserve-list, or a future
  Phase 2 session-policy layer wants explicit hand-off.
* The proposed M14.7-M14.11 milestones (Codex observability,
  compaction-handoff phase, HumanReview context gate, CI
  auto-handoff, headless halt) collapse: only **Codex
  observability** (rollout parsing + inferred-compaction signal)
  survives, and only as observability work — not as a
  correctness-path slice.

## Plan Reference

The implementing milestone covers, in order:

1. Recovery defaults in `core/_config/config.defaults.json` +
   `core/infra/config.py` loader.
2. Recovery-bundle renderer in `pipeline/prompts/` (reusing the
   typed-part composer + cache-first assembler from ADRs 0026 /
   0028).
3. Recovery trigger + action wiring in
   `pipeline/phases/builtin.py`. Initial scope: `validate_plan`
   (most contract-sensitive); subsequent slices extend to
   `review_changes` and `final_acceptance`.
4. Codex telemetry modules (`agents/runtimes/codex_telemetry.py`,
   `agents/runtimes/codex_model_windows.py`) feeding the existing
   `context_pressure` / `runtime_compaction` surfaces.
5. Unit tests in `tests/unit/pipeline/phases/test_contract_recovery.py`
   and `tests/unit/agents/test_codex_telemetry.py`.
6. Integration test: mock phase where first response is malformed,
   second response is valid → assert phase_log records recovery
   evidence and the retry replaced the failed result.

The plan-doc for this milestone is maintained separately as an internal
planning record.

## Test Suite Authority

Recovery behaviour is authoritatively pinned by:

* `tests/unit/pipeline/phases/test_contract_recovery.py` — the
  trigger / action / evidence contract for every recovery path.
* `tests/unit/agents/test_codex_telemetry.py` — rollout parsing,
  inferred-compaction detection, graceful degradation when rollout
  is missing or schema changes.

Existing observability tests (`test_context_pressure.py`,
`test_runtime_compaction.py`, `test_context_growth.py`,
`test_context_clearing.py`) remain unchanged in their assertions
but their semantic role shifts from "may inform future policy" to
"observability and audit only; Phase 1 correctness does not read
them".
