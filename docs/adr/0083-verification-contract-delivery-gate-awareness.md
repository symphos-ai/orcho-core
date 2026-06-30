# ADR 0083 — Verification contract delivery-gate awareness (Stage 6)

- Status: Accepted
- Date: 2026-06-10
- Relates to: ADR 0082 (final-acceptance readiness awareness, Stage 5),
  ADR 0081 (verification contract scheduling and repair routing, Stage 4),
  ADR 0080 (native command-receipts, Stage 3), ADR 0078 (env-assertions,
  Stage 2), ADR 0077 (read-only projection, Stage 1)

## Context

Stages 1–5 made the verification contract declarable (Stage 1), executable on
demand (Stages 2–3), blocking through deterministic gate routing (Stage 4),
and visible to the `final_acceptance` reviewer as read-only readiness
(Stage 5). One boundary still reasoned blind: **commit delivery** — the
post-acceptance step that transports the run-owned diff into the project
checkout and commits it. The delivery executor
(`pipeline/engine/commit_delivery.py`) knew about the release verdict and the
target-checkout dirty state, but it never read the verification contract. So a
run could finish `final_acceptance` with a missing, failed, or stale required
receipt and still deliver — the contract's whole point (declared proof gates
readiness) evaporated at the last step.

A second, quieter problem: a run worktree accumulates **generated runtime
garbage** — `.venv/`, `__pycache__/`, `.pytest_cache/`, `*.egg-info/`, the
verification-receipt directories themselves, etc. These are not product changes
but they sit in the same `git status` the delivery gate surfaces, so an
operator (or an auto-deliver) could fold them into a commit without noticing.

Delivery is the natural place to read the contract because it is the *last*
boundary and the only one that acts on the final tree. But it must do so
without re-litigating staleness differently from Stage 5, without executing
anything, and without changing what content is actually delivered.

## Decision

### 1. A delivery policy with conservative defaults

`verification.delivery_policy` is a new optional contract field validated
against the existing `SCHEDULE_POLICIES` vocabulary (`off | suggest | warn |
require`) — **no new policy constants**. The effective policy
(`resolve_delivery_policy`, `pipeline/verification_delivery.py`):

| Situation                                   | Effective policy |
| ------------------------------------------- | ---------------- |
| No verification contract (`None`)           | `off`            |
| Contract declared, `delivery_policy` unset  | `warn`           |
| Contract declares `delivery_policy`         | that value       |

`require` is reachable **only** by an explicit `delivery_policy: "require"`.
`work_mode` (including `governed`) never escalates the delivery policy — unlike
the Stage 4 gate-action algebra, where `governed` can derive a blocking action.
This keeps the hard delivery block an intentional opt-in.

### 2. A pure assessment module that reads its own subject

`pipeline/verification_delivery.py` (new, focused — like
`verification_readiness.py`, deliberately not folded into the
size-budgeted `verification_contract.py`) owns the Stage 6 verdict as a frozen
`DeliveryVerificationAssessment` (`policy`, `required_missing` /
`required_failed` / `required_stale`, `garbage_paths`, plus `has_blockers` and
`blocking == policy == "require" and has_blockers`).

The single fixed orchestration interface is:

```python
assess_delivery_verification(contract, run_dir, ctx, extras, diff_cwd,
                             baseline_ref="HEAD") -> DeliveryVerificationAssessment | None
```

It returns `None` when the contract is `None` or the policy is `off` (the
no-gate path stays byte-identical). Otherwise **the module itself reads the
subject checkout `diff_cwd`** — untracked paths (`git ls-files --others
--exclude-standard`) and changed paths (`git diff --name-only <baseline_ref>`).
The caller passes only `diff_cwd` plus the baseline; it never reads git or
recomputes path lists. Git/IO failures degrade (paths → `()`, staleness not
asserted); the module never raises.

### 3. Receipt classification reuses Stage 5 with an explicit checkout

Missing/failed/stale classification is **not** re-implemented. Stage 5's
classifier was extracted into the public
`classify_required_receipts(contract, run_dir, ctx, *, checkout, extras=None,
plan=None)` in `pipeline/verification_readiness.py`, which takes the staleness
subject as an **explicit `checkout` argument**. Stage 5 passes `ctx.checkout`;
Stage 6 passes `str(diff_cwd)`. Both recompute the fingerprint/HEAD with the
same `pipeline.verification_command.changed_files_fingerprint` helper the
Stage 3 receipt writer used, against the same subject the receipt recorded its
provenance against — so a receipt written for `diff_cwd` is never falsely
stale, and the two stages can never disagree about staleness from divergent
sources.

### 4. Generated garbage classified separately from the product diff

`classify_generated_paths` is a deterministic, component-based classifier (a
path component in `{venv, .venv, __pycache__, .pytest_cache, .ruff_cache,
.mypy_cache, .tox, node_modules}`, a `*.egg-info` component, a `*.pyc` / `*.pyo`
file, or one of the three receipt directories imported by constant from
`pipeline.evidence.verification_receipt`). It matches whole components, so
`src/venv_utils.py` is product, `.venv/lib/...` is garbage. Garbage is a
*distinct* blocker category and renders in its own prompt section
(`Generated environment garbage (not product diff):`) — the product `M` / `??`
lines are unchanged.

### 5. New decision status and halt reason

The engine consumes the assessment via a new optional
`resolve_commit_delivery(..., verification_gate=None)` parameter — it never
resolves the contract or reads git itself. Behaviour by policy:

- `off` / `None` — nothing (byte-identical);
- `suggest` — one hint line in the interactive prompt;
- `warn` — a warning block interactively, a `core.observability.logging.warn`
  line non-interactively; **delivery proceeds**;
- `require` with blockers — non-interactively, `resolve_commit_delivery`
  returns a decision with the new status `verification_blocked` (`action="none"`,
  an `error` listing the blockers) **before any transport git op**; the
  independent `release_blocked` gate keeps priority. Interactively it mirrors
  the rejected-acceptance correction prompt: warning block + default `fix`, so a
  bare Enter never delivers.

`run.py` maps `verification_blocked` to `mark_run_halted(...,
halt_reason="commit_delivery_verification_blocked")`, and
`finalization._HALT_BANNER_LABELS` renders it amber
(`Run halted — verification receipts incomplete`) — a recoverable halt, never a
green DONE. The decision artifact gains additive keys
(`verification_policy`, `verification_missing` / `_failed` / `_stale`,
`generated_garbage_paths`), surfaced only when non-empty.

## Consequences

- A `require` delivery policy turns a missing/failed/stale required receipt (or
  generated garbage) into a hard, non-interactive stop at the delivery boundary
  — closing the last gap where declared proof could be bypassed.
- `warn` (the default for any declared contract) keeps every existing run
  delivering exactly as before, now with a visible warning when proof is
  incomplete.
- Staleness is single-sourced across Stage 5 and Stage 6: one classifier, one
  fingerprint helper, one explicit checkout argument.

## Boundaries, stated explicitly

- **Transport and delivery content are unchanged.** Stage 6 only detects and
  gates (warn/block); it never silently excludes paths from delivery. The
  `untracked_delivered` / transport composition is identical — garbage is
  surfaced, not auto-stripped.
- **`work_mode` does not escalate.** `require` is an explicit opt-in only.
- **The interactive operator stays in control.** At a TTY the operator may
  still choose to deliver despite blockers; the hard refusal is the
  non-interactive (CI / piped) case, where silent delivery would be unsafe.
- **`release_blocked` is independent and keeps priority** when choosing the
  default action; the two gates do not weaken each other.

## MCP wire falsifier

**Claim: Stage 6 does NOT change the MCP-facing wire shape. No `orcho-mcp`
update is required.**

Evidence:

1. No new mode flag, profile-shape field, runtime schema, or gate primitive is
   added to the wire. `verification.delivery_policy` is a contract-file field
   parsed into the read-only contract projection; it is not surfaced on any MCP
   request/response and reuses the existing `SCHEDULE_POLICIES` vocabulary.
2. The only durable additions are **additive keys on the commit-decision
   artifact** (`verification_policy`, `verification_missing` / `_failed` /
   `_stale`, `generated_garbage_paths`) and the `session['commit_delivery']`
   dict, present only when non-empty. `validate_decision_dict` accepts the new
   keys as *optional* audit fields; its required keys, status enum, and
   cross-field coherence rules are unchanged, and `verification_blocked` itself
   is a resolve-time return value with `action="none"`, never written through
   the schema-validated `_persist` artifact.
3. No evidence v1 schema change; `validate_bundle` and the mock MCP smoke
   (`tests/acceptance/mock_pipeline/test_smoke_matrix.py`, marker
   `mcp_integration`) stay green.

**Stop condition.** If a later requirement puts the delivery verdict or the
`delivery_policy` on an MCP request/response (e.g. exposing `verification_blocked`
as a first-class wire status), that is a separate `orcho-mcp` workstream with
its own contract — not a silent core change.

## References

- `pipeline/verification_contract.py` — `delivery_policy` field + validation
- `pipeline/verification_delivery.py` — `resolve_delivery_policy`,
  `DeliveryVerificationAssessment`, `classify_generated_paths`,
  `assess_delivery_verification`
- `pipeline/verification_readiness.py` — shared `classify_required_receipts`
  (explicit `checkout` argument)
- `pipeline/engine/commit_delivery.py` — `verification_gate` parameter,
  `verification_blocked` status, garbage prompt section, additive decision keys
- `pipeline/project/run.py` — assessment wiring + `verification_blocked` →
  halt mapping
- `pipeline/project/finalization.py` — `commit_delivery_verification_blocked`
  banner label
- `docs/architecture/verification_contract.md` — Stage 6 section
