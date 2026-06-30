# ADR 0096 — Operator-self-sufficient verification-gate timeline (run-level projection)

- Status: Accepted
- Date: 2026-06-16
- Relates to: ADR 0082 (final-acceptance readiness; the
  `classify_required_receipts` verdict home), ADR 0089 (delivery-receipt
  continuity; parent-run inheritance, `searched_run_dirs` / `suggested_commands`
  / `receipt_provenance` diagnostics), ADR 0090 (require-gates cannot end in a
  silent green run), ADR 0094 (auto-run missing/stale required receipts; the
  frozen `ReceiptAutoRunResult.to_evidence()` key set), ADR 0095 (auditable
  per-event verification-gate timeline over additive durable trails; the DONE
  block in its §4)
- Supersedes: nothing. This ADR is purely **additive** — it adds a read-only
  run-level projection and the operator lines that render it, and changes no
  gate-selection, execution, receipt schema, `to_evidence` key set, or readiness
  verdict.

## Context — the DONE timeline was auditable but not yet operator-self-sufficient

ADR 0095 made the official gate signal **auditable per event**: the DONE/HALTED
`Verification gates` block (ADR 0095 §4) shows one count-first line per hook
firing, a `receipts:` line, and a `residual: missing=… stale=…` line. That
answers *"what did each hook do?"*, but an operator reading a HALTED run still
could not act from the block alone:

- **`failed` residual was invisible on the run-level line.** The residual line
  named `missing` / `stale` required commands but not `failed` ones, so a run
  blocked purely by a failed required gate showed no run-level residual at all.
- **No "what do I run next?"** The block named no searched run dir and no
  `orcho verify` command, even though the Stage 5 readiness block already
  computes both (ADR 0089). An operator had to cross-reference two surfaces.
- **Manual-only required gates read as missing auto-work.** A `required` +
  `manual_only` command (intentionally never auto-run, ADR 0094) simply appeared
  absent, indistinguishable from a gate that *should* have produced a receipt.
- **Inherited (parent-run) proof was indistinguishable from current-run proof.**
  A present receipt inherited from a follow-up's parent (ADR 0089) looked
  identical to one produced by this run.

The fix has the same hard constraint as ADR 0095: it must be **presentation
only**. Gate policy, the set of executed commands, `GateRepairOutcome`,
`sdk.verify`, the receipt schema, the ADR 0094 `to_evidence()` keys, and the
readiness verdict must all stay byte-identical — this is operator UX over
existing evidence, not new policy.

## Decision

Add one read-only **run-level projection** over the trails ADR 0095 already
records, and render its non-empty fields as additional DONE lines. Reuse the
Stage 5 readiness helpers verbatim so DONE and readiness share a single source
of phrasing and can never diverge.

### 1. F1 — shared readiness diagnostics extended to `failed`

`build_final_acceptance_readiness` (`pipeline/verification_readiness.py`)
previously formed its `searched_run_dirs` / `suggested_commands` advisory only
when a required receipt was `missing` **or** `stale`. It now forms them when a
required receipt is `missing` **or** `stale` **or** `failed`, and passes the
union `(*missing, *stale, *failed)` to `suggested_verify_commands`. This is the
**only** change to readiness and it is advisory-surface only:

- `required_present` / `required_missing` / `required_failed` / `required_stale`
  partitioning is **unchanged**.
- the `Remaining before ready` verdict and `READINESS_POLICY_LINE` are
  **unchanged**.
- the `transcript_not_proof_note` trigger (missing/stale + observed exploratory
  commands) is **unchanged**.
- `render_readiness_block` already gates its `Searched run dirs:` /
  `Suggested verification:` sections on `summary.suggested_commands`, so it now
  fires for a failed-only run automatically — no render change.

Because both surfaces now feed `suggested_verify_commands` the **same**
`(missing + stale + failed)` set with the same `run_id` / `project`, the DONE
hint and the readiness hint are **identical by construction**, not by parallel
maintenance.

### 2. Run-level projection from one read-only classify pass

`verification_timeline.py` replaces the old `_reclassify_residual` with
`_run_level_projection`, which makes **exactly one**
`classify_required_receipts` call (the same read-only verdict home as readiness
and delivery, ADR 0082/0089) and derives from that single pass:

- `residual_missing` / `residual_stale` / `residual_failed` — required commands
  still open in each non-present status. `failed` is the materializer's
  authoritative on-disk rollup (exit code, assertions, `detail`), never inferred
  from a receipt's mere presence.
- `manual_only` — required commands the contract marks `manual_only` /
  operator-only (`sdk.verify.manual_or_operator_only_commands`, the **raw** set
  taken *before* the `verify run` subtraction of `required`, so a
  `required` + `manual_only` command stays manual) that are **not present**.
  These are intentional non-auto work, not missing auto-work.
- `inherited` — present receipts whose `ReceiptClassification.source_run_id`
  differs from the current run id, formatted `<command> from run <id> (<path>)`,
  the same provenance shape as `readiness.receipt_provenance` (ADR 0089).
- `searched_run_dirs` — current run dir then any follow-up parent dirs
  (`parent_sources_from_extras`, the ADR 0089 helper).
- `suggested_commands` — `suggested_verify_commands(contract,
  (*missing, *stale, *failed), …)`, the **same** helper and argument set as F1.

The projection uses lazy imports and is `suppress`-wrapped: any git/IO/import
failure degrades the whole projection to empty rather than raising, matching the
read-only never-raise contract of `build_verification_timeline`.

### 3. Additive DONE lines, gated on non-empty fields

`VerificationTimeline` gains additive fields defaulting to `()`
(`residual_failed`, `manual_only`, `inherited`, `searched_run_dirs`,
`suggested_commands`). `render_verification_gate_done_block` **augments** the
ADR 0095 §4 block — it does not replace it:

- the existing `residual:` line gains a `failed=…` segment (only when
  `residual_failed` is non-empty), printed alongside `missing=…` / `stale=…`;
- a `manual-only: …` line, only when `manual_only` is non-empty;
- an `inherited: …` line, only when `inherited` is non-empty;
- and, only when a required deficit is open (`suggested_commands` non-empty),
  compact `searched run dirs: …` and `fix: …` lines carrying the shared hints.

Every new line is gated on its own non-empty field, so an already-green or
no-parent run renders **byte-identically** to ADR 0095: the new lines appear
only when there is new operator-relevant content. `searched_run_dirs` /
`suggested_commands` are deliberately excluded from `is_empty`, so the early
`return None` omission path is preserved. Colour is overlaid only by the caller
(`finalize_with_terminal_output`); the returned strings carry none.

## Consequences

- A HALTED run's `Verification gates` block is **operator-self-sufficient**: it
  shows what ran, what is `missing`/`stale`/`failed`, which dirs were searched,
  the exact `orcho verify` command to run next, what is intentionally
  manual-only (not missing auto-work), and which present proof was inherited
  from a parent run rather than produced now.
- **DONE and readiness cannot contradict each other.** Both compute the searched
  dirs and the `orcho verify` hint from the same `suggested_verify_commands`
  helper over the same `(missing + stale + failed)` set; a focused regression
  pins their byte-equality for a failed-only run.
- **No policy/execution change.** Gate selection, `sdk.verify`, the executed
  command set, `GateRepairOutcome`, and the auto-run target set are untouched;
  run-level residual/manual/inherited never override a per-hook decision.
- **No schema/verdict change.** The receipt schema, the ADR 0094 autorun
  `to_evidence()` keys, and the readiness verdict (`required_*` partitioning and
  `Remaining before ready`) are all unchanged; the new fields are in-process
  render-only projections, never persisted.
- The block stays omitted (`None`) when there is no contract and no
  receipts/trail/gate-events, and a green/no-parent run is byte-identical to the
  ADR 0095 output — the additive lines are strictly opt-in on content.
