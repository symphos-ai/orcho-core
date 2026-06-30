# ADR 0097 — Policy-aware UX for the delivery/readiness verification gate

- Status: Accepted
- Date: 2026-06-19
- Relates to: ADR 0082 (final-acceptance readiness; the
  `classify_required_receipts` verdict home and the readiness block), ADR 0083
  (delivery-gate awareness; `resolve_delivery_policy`, the
  `DeliveryVerificationAssessment`, and the `commit_decision` audit fields),
  ADR 0090 (require-gates cannot end in a silent green run; the "never falsely
  green" invariant), ADR 0094 (auto-run / manual-or-operator-only command set),
  ADR 0095 / ADR 0096 (the DONE/HALTED `Verification gates` timeline and its
  run-level projection)
- Supersedes: nothing. This ADR is purely **additive UX/classification** — it
  changes no policy *rule*, no executed command set, no receipt schema, and no
  persisted `commit_decision` field name. It records the per-gate policy a gap
  already had and re-words the surfaces that render it.

## Context — one boundary policy, but the surfaces spoke as if every gap blocked

ADR 0083 gave the delivery boundary a single effective policy
(`off | suggest | warn | require`) via `resolve_delivery_policy`, and ADR 0090
made a `require` gap a hard, never-falsely-green blocker. But the *surfaces* that
render an unproven required command — the Stage 5 readiness block, the Stage 6
delivery banner/prompt, and the DONE `Verification gates` summary — all phrased
**every** open required command as `missing required receipts`, defaulted the
delivery prompt toward `fix`, and listed it under "Remaining before ready",
regardless of whether the gate was actually `require`, `warn`, `suggest`, or an
intentionally-withheld `manual_only` command.

That over-stated the contract in three ways:

- A `warn` / `suggest` gate (delivery *allowed* by policy) read as a hard
  `missing required` blocker, so an operator could not tell an advisory gap from
  a real one.
- A `manual_only` / operator-only required command (intentionally never
  auto-run, ADR 0094) read as missing auto-work — indistinguishable from a gate
  that should have produced a receipt.
- An APPROVED final acceptance carrying only `warn`/`suggest` gaps rendered as a
  contradiction: a green release whose verification block looked like a blocker.

The fix must keep ADR 0090 intact: a `require` gap stays blocking, and reclassifying
anything as `manual_only` must never *hide* a genuine auto-required gap.

## Decision

### 1. One pure source of per-gate effective policy — `pipeline/verification_policy.py`

A new pure module is the single place that answers *"what is the effective
delivery policy of this required command, and which gaps block?"*. It imports
only the pure `pipeline.verification_*` layer (no `pipeline.project.*`; a test
locks this), and exposes:

- `effective_delivery_policy_by_command(contract, plan, manual_set, boundary_policy)`
  → `dict[command, policy]`. For each command in
  `required_delivery_commands`: `manual_only` when the command is in the
  caller-supplied manual/operator set
  (`sdk.verify.manual_or_operator_only_commands`); else the policy of the
  matching delivery-hook gate in the resolved `ScheduledGatePlan`
  (`before_delivery` / `after_phase(implement)` — the **strictest** wins when a
  command is scheduled at more than one delivery position); else the
  `boundary_policy` (the value `resolve_delivery_policy` returned). A
  `contract.required` command with no delivery-hook gate therefore inherits the
  boundary policy.
- `partition_gaps(status_by_command, policy_by_command)` → `GapPartition` with
  `blocking` (effective `require`), `warning` (`warn` / `suggest`), and
  `manual_only` buckets, input order preserved. `present` and `off` contribute
  nothing.

Stage 5/6/DONE all consume this one module, so the three surfaces classify a gap
identically by construction.

### 2. The policy matrix

For a required delivery command whose receipt is **missing / failed / stale**,
the effective per-gate policy decides both how the gap is *named* and the
delivery gate's *default action*:

| Effective policy | Gap meaning | Counts as missing-required? | Blocks delivery? | Delivery default action |
| ---------------- | ----------- | --------------------------- | ---------------- | ----------------------- |
| `require`        | blocking — must be proven before delivery | yes | **yes** | `fix` (gate rerun / correction) |
| `warn`           | surfaced, **shipping allowed by policy**  | no (advisory) | no | `apply` / `approve` |
| `suggest`        | hint, **shipping allowed by policy**      | no (advisory) | no | `apply` / `approve` |
| `manual_only` / operator-only | visible, **not auto-run** | **no** | no | `apply` / `approve` |

- The phrase **`missing required`** is used only for an effective `require` gap.
- `warn` / `suggest` gaps are rendered with their policy and the explicit note
  **`shipping allowed by policy`**, and never appear under "Remaining before
  ready" / as a release blocker.
- `manual_only` / operator-only commands are surfaced on their own line
  (`not auto-run`) and are **excluded** from `required_missing` / the blocking
  residual on every surface.
- An operator may still choose `approve` / `apply` at a `require` block; the
  delivery prompt marks that choice as an explicit **override / waiver** so the
  confirming output never reads as a clean, fully-proven delivery.

### 3. `resolve_delivery_policy` is unchanged

The boundary-policy rule from ADR 0083/0090 is **not touched**: a `None` contract
→ `off`; a declared contract with neither an explicit `delivery_policy` nor a
`before_delivery` `policy: require` → `warn`; an explicit `before_delivery`
require gate or an explicit `delivery_policy` → `require`; `work_mode` never
escalates it. This ADR only adds the *per-gate* refinement on top of that
boundary value — it never makes `warn` / `suggest` start blocking, and it never
weakens a `require` boundary.

### 4. Surfaces that consume the policy (no rule change)

- **Stage 6 delivery (`DeliveryVerificationAssessment`)** — partitions gaps into
  `blocking` / `warning` / `manual_only`; `required_*` exclude `manual_only`;
  `blocking` is true iff a `require` gap exists; `receipt_blocker_lines` are
  policy-aware (require → `missing required receipts`, warn/suggest →
  `shipping allowed by policy`). The persisted `commit_decision` field names
  (`verification_policy`, `verification_missing` / `_failed` / `_stale`,
  `generated_garbage_paths`) are unchanged — the new data is render-only.
- **Stage 6 prompt/banner** — `require` reads as `blocked by required
  verification` / `delivery blocked until receipt or waiver` (default `fix`);
  `warn`/`suggest` read as shipping-allowed (default `apply`/`approve`);
  `approve`/`apply` at a require block is marked an operator waiver/override.
- **Stage 5 readiness block** — each gap is annotated with its effective policy;
  `missing required` is used only for `require`; warn/suggest carry
  `shipping allowed by policy`; `manual_only` shows `not auto-run` and is out of
  "Remaining before ready". The no-blocker block stays byte-identical.
- **DONE `Verification gates` summary** — splits the residual into `blocking
  (require)` vs `warning (warn/suggest)` with per-gate policy, excludes
  `manual_only` from the blocking residual, and frames an APPROVED release whose
  only open gaps are warn/suggest as **`approved + verification warning`** rather
  than a contradiction.

## Consequences

- **The contract is no longer over-stated.** An operator can tell a hard
  `require` blocker from an advisory `warn`/`suggest` gap from an intentional
  `manual_only` withholding — on the readiness block, the delivery prompt, and
  the DONE summary, worded identically because all three read one policy module.
- **"Never falsely green" (ADR 0090) holds.** A `require` gap still blocks, still
  forces `approved=False / ship_ready=False` via the readiness backstop
  (`required_receipt_gaps`, now emitting gaps only for `require`-policy gaps),
  and still defaults the delivery prompt to `fix`. Reclassifying a command as
  `manual_only` cannot hide an auto-required gap — manual membership is
  authoritative only for commands the contract actually marks manual/operator-only.
- **No rule, schema, or wire change.** `resolve_delivery_policy`, the executed
  command set, the receipt schema, and the `commit_decision` / autorun
  `to_evidence` field names are all unchanged; every policy-aware string is a
  render-derived projection. The correction `gate_rerun` route and the
  release-blocked banner priority are unaffected.
