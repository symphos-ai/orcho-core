# ADR 0082 — Verification contract final-acceptance readiness awareness (Stage 5)

- Status: Accepted
- Date: 2026-06-10
- Relates to: ADR 0081 (verification contract scheduling and repair routing,
  Stage 4), ADR 0080 (native command-receipts, Stage 3), ADR 0078
  (env-assertions, Stage 2), ADR 0077 (read-only projection, Stage 1),
  ADR 0076 (durable verification-environment receipt)

## Context

Stages 1–4 made the verification contract declarable, executable on demand,
and (Stage 4) blocking through deterministic gate routing. The **final
acceptance gate** still reasoned blind: the `final_acceptance` reviewer saw
the contract projection ("these gates exist") but not the *state of proof* —
which required receipts exist, which failed, which no longer match the tree
under review, and which are simply absent. In practice the reviewer either
re-ran ad-hoc host commands (re-opening the original
implementer-vs-reviewer dispute the contract exists to close) or approved
without checking the declared proof at all.

Two concrete failure modes shaped this stage:

- **F1 — ad-hoc mismatch treated as a blocker.** A reviewer host command
  "sees a different world" than the declared subject (the stable-install
  import incident) and readiness is rejected even though every declared
  receipt is valid.
- **F2 — stale selection input hides a delivery gate.** A gate plan memoized
  early in the run (the advisory prompt preview, possibly built before
  `implement` mutates the tree) misses a path-selected gate that became
  delivery-relevant afterwards; readiness built on that plan would not report
  the gate's receipt as missing.

## Decision

### 1. A pure readiness module

`pipeline/verification_readiness.py` (new, focused — deliberately **not**
added to `pipeline/verification_contract.py`, which is already at its size
budget) computes a typed, frozen `ReadinessSummary`:

- env statuses from the Stage 2 env-assertion receipts
  (`load_env_assertion_receipts`);
- the required delivery command set — `verification.required` plus commands
  scheduled at the delivery positions `after_phase(implement)` and
  `before_delivery` (`required_delivery_commands`, a separately tested pure
  function);
- per-command classification **present / missing / failed / stale** over the
  Stage 3 command-receipts (`load_command_receipts`). *Failed* = non-zero
  exit, a failed declared assertion, or an execution `detail`. *Stale* = the
  receipt's `git.changed_files_fingerprint` / `git.checkout_head` no longer
  match the current checkout; the fingerprint is recomputed with the **same
  public helper the receipt writer used**
  (`pipeline.verification_command.changed_files_fingerprint`, extracted from
  the previously private `_changed_files_fingerprint` without behavior
  change), so valid receipts can never be falsely stale via hash drift. With
  no usable checkout, staleness is not asserted (degrades to present);
- a count of observed ad-hoc `command.end` events, labelled exploratory and
  non-authoritative.

`render_readiness_block` renders the block with the sections Environments /
Scheduled gates / Required receipts (present) / Missing / Failed / Stale /
Exploratory commands, a **Remaining before ready** section (the union of
missing+failed+stale, or `(none — declared proof complete)`), and the
verbatim reviewer policy:

```text
Readiness blockers should be based on missing/failed/stale/invalid declared
receipts, not only an ad-hoc host command mismatch.
```

All reads go through the tolerant loaders in
`pipeline.evidence.verification_receipt`; git/IO failures degrade and never
raise. The module imports no `pipeline.phases.*` and runs no subprocess.

### 2. Authoritative selection input (resolves F2)

The gate plan feeding readiness is the **delivery selection plan**
(`delivery_gate_plan`): the executable routing plan cached by Stage 4 for the
`before_delivery` epoch (`verification_gate_routing_plans`) when the hook
already built one, otherwise a fresh `build_scheduled_gate_plan` over the
*current* checkout's changed files. The advisory prompt-preview plan
(`verification_gate_prompt_preview`) is **never** consulted — it is a
prompt-timing artifact and may predate `implement`. A regression test pins
the exact F2 scenario: a preview cached before `implement` omits a
path-selected gate, the checkout later matches that path rule, and readiness
must report the gate's receipt as missing.

### 3. Prompt wiring (resolves F1)

`_verification_readiness_text` (`pipeline/phases/builtin/review_support.py`)
short-circuits `state.dry_run` **before any receipt loader runs**, returns
`""` without a run dir or a declared contract (the no-contract wire prompt
stays byte-identical), and otherwise renders the block. The
`final_acceptance` handler passes it through
`adapters.run_review(readiness_summary=...)` into a dedicated builder slot
(`runtime_review_uncommitted_prompt(verification_readiness=...)`) that wraps
it as a TURN-scoped `verification_readiness` part
(`id="verification_readiness:final_acceptance"`) — distinct from the
`verification_receipt` part (the ADR 0076 developer-side env probe digest
used by `review_changes`).

### 4. Additive evidence digest — observed facts only

The evidence v1 bundle gains one additive top-level key,
`verification_readiness`: the Stage 3 per-command summary
(`summarize_command_receipts` — passed/failed, parity, baseline presence) and
per-env `all_passed`. It deliberately carries **no stale/missing verdict**:
`collect_evidence(run_dir)` has neither the declared contract (no required
set) nor the live checkout/HEAD (the worktree may be cleaned after the run),
so a deterministic stale/missing call is impossible there — that
classification is owned by the prompt layer above. Existing keys are
untouched; `validate_bundle` continues to pass (v1 permits added keys).

## Consequences

- `final_acceptance` can answer "what remains before ready?" from declared
  proof: missing/failed/stale official receipts are visible in the prompt,
  and an exploratory command mismatch is explicitly non-blocking while the
  declared receipts are valid.
- Stage 5 is read-only awareness: it executes nothing, writes nothing,
  blocks nothing, and changes no schema, mode flag, or gate primitive.
- The prompt-preview cache is now documented as advisory-only; any consumer
  needing authoritative selection must use the executable routing epochs or
  build fresh from the current checkout.

## MCP wire falsifier

**Claim: Stage 5 does NOT change the MCP-facing wire shape beyond one
additive evidence key. No `orcho-mcp` update is required.**

Evidence:

1. The readiness summary is prompt text plus an in-memory dataclass — never
   written to `meta.json`, a receipt, or the run header.
2. The only durable surface is the additive `verification_readiness` evidence
   key; evidence v1 explicitly permits added top-level keys, and
   `validate_bundle` plus the mock MCP smoke
   (`tests/acceptance/mock_pipeline/test_smoke_matrix.py`, marker
   `mcp_integration`) stay green, proving `orcho_run_evidence` is unbroken.
3. No runtime schema, profile shape, mode flag, or gate primitive moves.

**Stop condition.** If a later requirement puts the readiness verdict itself
(present/missing/failed/stale) on the wire, that is a separate `orcho-mcp`
workstream with its own contract — not a silent core change; the collector
cannot produce that verdict deterministically (see §4).

## References

- `pipeline/verification_readiness.py` — summary computation + rendering
- `pipeline/verification_command.py` — public `changed_files_fingerprint`
- `pipeline/phases/builtin/review_support.py` — `_verification_readiness_text`
- `pipeline/prompts/builders.py` — `_verification_readiness_part`
- `pipeline/evidence/collector.py` — additive `verification_readiness` digest
- `docs/architecture/verification_contract.md` — Stage 5 section
