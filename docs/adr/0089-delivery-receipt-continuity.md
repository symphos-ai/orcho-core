# ADR 0089 — Delivery receipt continuity (parent-run receipt inheritance)

- Status: Accepted
- Date: 2026-06-13
- Relates to: ADR 0088 (review-retry worktree subject continuity), ADR 0084
  (cross-repo receipt graph, Stage 7), ADR 0083 (delivery-gate awareness,
  Stage 6), ADR 0082 (final-acceptance readiness awareness, Stage 5), ADR 0080
  (native command-receipts, Stage 3), ADR 0086 (correction route, Stage 1)

## Context

A **correction follow-up** run reuses the parent run's retained worktree (ADR
0088): the child run is launched to fix a narrow defect on top of the parent,
against the *same* subject diff. The motivating incident is two real runs: the
follow-up `20260612_225347` launched on top of parent `20260612_213530`,
inheriting the parent's retained worktree and therefore the parent's exact
changed-files set.

The parent run had already executed the required verification commands and
written their durable command-receipts under
`20260612_213530/verification_command_receipts/` (e.g. `env-provenance.json`,
`lint.json`), each carrying the subject checkout's
`git.changed_files_fingerprint`. Those receipts are valid proof for precisely
the diff the child is delivering.

But Stage 5 readiness (`build_final_acceptance_readiness`) and the Stage 6
delivery gate (`assess_delivery_verification`) both read command-receipts from a
**single** run directory — the *current* run's `run_dir`. The child run's own
`verification_command_receipts/` was empty (the child re-ran nothing, because the
proof already existed). So:

- the `final_acceptance` readiness block reported the required receipts as
  **missing**, and
- the delivery gate printed a `missing required receipts` banner with **no next
  step** — for a diff that was, in fact, fully proven one run earlier.

Two failures compound here. First, **lost continuity**: valid proof for the same
diff, written by the parent, was invisible to the child. Second, a **latent
contradiction risk**: `final_acceptance` and the delivery gate each classify
receipts; if they ever read different sources they could disagree (one green, one
red) on the same run — a confusing, locally-correct-but-globally-wrong state.

## Decision

A single **receipt index** with a search path spanning the current run and its
parent run(s), a candidate-priority rule that inherits valid parent proof for the
same diff while *never* masking a fresh failure, provenance on every
classification, and actionable diagnostics — read from **one** `state.extras` key
shared by both consuming surfaces so they cannot diverge.

### 1. A focused receipt-index module + a multi-run search path

A new low-level module `pipeline/verification_receipt_index.py` owns the
mechanical side of multi-run lookup. It imports only stdlib and the tolerant
receipt loader (`pipeline.evidence.verification_receipt.load_command_receipts`);
it **never** imports `verification_readiness` (which imports *it*) nor any
`pipeline.project.*` orchestration module, so there is no import cycle and the
verdict layer stays its single home.

It exposes the typed search source `ReceiptSource(run_id, run_dir)`, the
candidate `ReceiptCandidate(command, source_run_id, path, receipt)`, tolerant
coercion (`coerce_receipt_sources`, `parent_sources_from_extras`), the
writer-aligned `receipt_file_path` (`<run_dir>/verification_command_receipts/<sanitized-command>.json`),
and `load_parent_candidates` (reads each source's receipt dir once, ordered
candidates per command). Every IO failure degrades exactly like
`load_command_receipts` — a missing source contributes no candidates and never
raises.

The **search path** is ordered: the **current run first**, then the follow-up's
parent run(s) (closest parent first). The current run's own receipts always win
where present (see the priority rule); parents are consulted only to *fill a gap*
the current run left.

### 2. The candidate-priority rule (review F1)

For each required delivery command, `classify_required_receipts` routes the
current-run receipt and the ordered parent candidates through one selection rule,
all classified against the **current** subject identity (fingerprint + HEAD) with
the same `_classify_receipt` mechanism Stage 5/7 already use:

1. **Current present wins.** If the current run's receipt classifies `present`,
   it is chosen (provenance = current run).
2. **A fresh same-diff failure blocks inheritance.** If the current run's receipt
   classifies `failed` **and** its recorded `changed_files_fingerprint` equals
   the current subject fingerprint *or* either side's fingerprint is unrecorded,
   that `failed` is reported (current-run provenance) and **no parent is
   consulted**.
3. **Otherwise, degrade to a valid parent.** When the current receipt is absent,
   stale, or `failed` against a *different* fingerprint, the env-eligible parent
   candidates are scanned in search order and the **first** that classifies
   `present` against the current subject is **inherited**, carrying that parent's
   provenance.
4. **Most informative fallback.** If no parent qualified, report the current
   receipt's classification when one exists, else the first env-eligible parent
   candidate's classification (e.g. `stale` naming the fingerprint move), each
   with its own provenance.
5. **Missing.** No usable receipt anywhere → `missing`.

**Why rule (2) — "never falsely green."** A correction follow-up exists *because
the parent's work needed fixing*. If the child re-runs a required command and it
**fails on the same diff**, an older parent `pass` for that same diff must never
silently override the fresh failure — that would deliver a change the current
state does not actually prove. So a same-diff (or undated) current failure is
authoritative and short-circuits inheritance. Inheritance is only ever allowed to
*fill an absence* or replace proof the current run could not produce against this
diff — never to overwrite a current verdict that contradicts it. This is the
load-bearing invariant of the whole change.

### 3. The five conditions to accept a parent receipt

A parent candidate is inherited as `present` only when **all five** hold (the
first is structural; the rest reuse the existing classifier, so no new staleness
logic is introduced):

1. **Command id matches** — the candidate is looked up by command, so this is
   guaranteed by construction.
2. **Env matches** — when the command declares a non-empty `env`
   (`contract.commands[command].env`), the parent receipt's `env` must equal it;
   an env mismatch disqualifies the candidate entirely (it is not even the
   "most informative" fallback). A command with no declared env accepts any env.
3. **Exit / assertions clean** — `exit_code == 0`, no failed declared assertion,
   no execution `detail` (i.e. the receipt does not classify `failed`).
4. **Fingerprint matches the current subject** — the parent receipt's
   `changed_files_fingerprint` and the current subject-checkout fingerprint must
   both be **known and equal** (a stricter rule than the current-run classifier,
   applied by `_classify_parent_candidate`): a parent that proved a *different*
   diff, that recorded **no** fingerprint, or that is evaluated when the current
   fingerprint is unavailable is rejected as `stale` — never inherited. The
   current-run classifier's "unrecorded fingerprint → staleness not asserted"
   leniency is deliberately **not** extended to inheritance, so an unverifiable
   parent pass can never go falsely green; the `checkout_head` drift check is the
   same `_classify_receipt` mechanism.
5. **No dependency staleness** — `dependency_stale_reason` is empty: no
   depended-on dependency's HEAD moved since the parent receipt was written
   (ADR 0084 semantics, unchanged).

### 4. Provenance on every classification

`ReceiptClassification` gains two **additive, default-empty** fields:
`source_run_id` and `path`. Together with the existing `status` (and `reason`)
they form the provenance tuple **`{command, source_run_id, path, status}`**: the
run a classification was decided from and the receipt file it points at. For a
current-run receipt this is the current run; for an inherited one it is the parent
run. Stage 5 renders an *Inherited receipt provenance* line
(`<command>: <status> from run <source_run_id> (<path>)`) **only** for receipts
inherited from a parent (source run ≠ current run), so a no-parent run's block is
byte-identical to before.

### 5. One `state.extras` key — the constructive no-contradiction guarantee

The parent search sources travel under a single documented key,
`VERIFICATION_PARENT_RUNS_EXTRAS_KEY = "verification_parent_runs"`, whose value is
an ordered tuple of `(run_id, run_dir)` pairs (closest parent first; the current
run is **not** listed — it is always searched first via its own `run_dir`).
`pipeline/project/state_setup.py:build_pipeline_state` stamps this key from the
correction follow-up's `followup_parent_run_id` + `followup_parent_run_dir` (a
fresh run leaves it absent → byte-identical behaviour).

Both surfaces read the **same** key: `build_final_acceptance_readiness` (via
`review_support`, passing `state.extras`) and `assess_delivery_verification` (via
`run.py`, passing `extras`) call `classify_required_receipts`, which reads parent
sources from this one key. Because the verdict is computed **once**, by **one**
function, from **one** source of parents, `final_acceptance` and the delivery
gate can **never** disagree on the missing/failed/stale partition for the same
run. This is closed by an agreement test asserting the three verdict sets of
`ReadinessSummary` and `DeliveryVerificationAssessment` are equal for identical
inputs (a divergence fails the build).

### 6. Diagnostics — no contradictory banner

When a required receipt is genuinely missing/stale, both surfaces now print the
**run directories searched** (current + parents) and the exact, copy-paste
`orcho verify` commands carrying the current run id and project:

```
orcho verify env --env <env> --run-id <run_id> --project <project>
orcho verify run --required --run-id <run_id> --project <project>
```

Built from one shared helper (`suggested_verify_commands`) so the readiness block
and the delivery banner print identical guidance. A bare `missing required
receipts` banner with no next step — the original incident's worst symptom — is
now structurally impossible.

## Status-surface inventory (review F2)

To prove the verdict is single-sourced, every surface that touches receipt state
was re-surveyed:

- **Evidence v1 bundle** — `pipeline/evidence/collector.py:_build_verification_readiness`
  emits **observed facts only** (`summarize_command_receipts`: command / env /
  exit_code / parity / `passed` / has_baseline, plus per-env `all_passed`); it
  reads only the current run dir and carries **no** `required` / `missing` /
  `failed` / `stale` key. `passed` is a single receipt's own rollup, not a
  verdict over the required set. This is locked by
  `tests/unit/pipeline/evidence/test_verification_readiness_digest.py` (digest
  shape + no-verdict invariant).
- **CLI** — `cli/_formatters.py` `format_verify_env` / `format_verify_run` /
  `format_verify_list` render the **execution** result of `orcho verify`
  (PASS/FAIL from exit codes / assertions), not a readiness verdict.
- **Grep sweep** — `cli/`, `sdk/`, `pipeline/control/`, and
  `pipeline/observability/` were searched for any required-missing/failed/stale
  computation over command-receipts: **no hits**.

**Conclusion:** the required-missing/failed/stale **verdict exists only in
`classify_required_receipts`**. The receipt-index module loads candidates but
renders no verdict of its own. No second verdict-surface existed, so none needed
to be migrated onto the shared helper.

## MCP-wire falsifier

**Claim: parent-receipt continuity does NOT change any MCP-facing wire shape. No
`orcho-mcp` update or mock smoke is required.** (Same discipline as the ADR 0080
/ ADR 0084 falsifiers.)

Evidence:

1. **Receipts are not rewritten.** `COMMAND_RECEIPT_SCHEMA_VERSION` stays **2**.
   Provenance (`source_run_id`, `path`) and inheritance are computed **at read
   time** from the search path; nothing is written back into receipt files, and
   no field is added to the persisted receipt.
2. **Evidence v1 is unchanged.** `_build_verification_readiness` and
   `summarize_command_receipts` keep their prior shape (observed facts only); the
   bundle gains no key. The lock test above fails if that shape drifts.
3. **No new mode flag, profile-shape field, runtime schema, or gate primitive.**
   `VERIFICATION_PARENT_RUNS_EXTRAS_KEY` is an in-process `state.extras` key, not
   a serialized contract field. The `commit_decision` artifact keeps its existing
   additive audit keys (`verification_missing` / `_failed` / `_stale` use command
   names only); the new `searched_run_dirs` / `suggested_commands` /
   `receipt_provenance` are render-only and never persisted to the
   schema-validated artifact.

**Stop condition.** If a later requirement puts parent-run inheritance or the
receipt search path onto an MCP request/response, that is a separate `orcho-mcp`
workstream with its own contract — not a silent core change.

## Consequences

- A correction follow-up inherits the parent run's valid proof for the same diff:
  the `20260612_213530 → 20260612_225347` incident no longer reports false
  `missing` receipts, and the delivery banner is no longer self-contradictory.
- A fresh same-diff failure in the child is **never** masked by an older parent
  pass — the "never falsely green" invariant is enforced by rule (2) and a
  current-failed-blocks-parent test.
- `final_acceptance` and the delivery gate are constructively guaranteed to agree
  (one verdict function, one extras key), pinned by an agreement test.
- When proof is genuinely absent, both surfaces name where they looked and the
  exact `orcho verify` commands to produce it.

## Boundaries, stated explicitly

- **No writes into retained worktrees or incident run dirs.** Parent runs are
  read-only sources; receipts are never copied, rewritten, or moved, and no
  verify command is auto-run.
- **Never-raise discipline.** Every git/IO failure during candidate loading or
  classification degrades (a source contributes nothing, staleness is not
  asserted) — exactly as in the Stage 2–7 modules. No new raise path.
- **Additive, default-empty dataclass fields only.** `ReceiptClassification`
  (`source_run_id`, `path`), `ReadinessSummary` (`searched_run_dirs`,
  `receipt_provenance`, `suggested_commands`), and
  `DeliveryVerificationAssessment` (`searched_run_dirs`, `suggested_commands`)
  extend additively with defaults; a no-parent / no-blocker surface is
  byte-identical to before.
- **Single-direction import graph.** `verification_receipt_index` imports only
  stdlib + the receipt loader; `verification_readiness` imports *from* it; neither
  the index nor readiness/delivery imports `pipeline.project.*`.
- **Status vocabulary unchanged.** `present / missing / failed / stale` is intact;
  provenance, searched dirs, and hints are additive context, not new statuses.

## References

- `pipeline/verification_receipt_index.py` — `ReceiptSource`, `ReceiptCandidate`,
  `VERIFICATION_PARENT_RUNS_EXTRAS_KEY`, `coerce_receipt_sources`,
  `parent_sources_from_extras`, `load_parent_candidates`, `receipt_file_path`
- `pipeline/verification_readiness.py` — `ReceiptClassification.source_run_id` /
  `.path`, `_select_classification` (priority rule), `_classify_parent_candidate`
  (strict parent-fingerprint inheritance rule), `suggested_verify_commands`,
  `ReadinessSummary.searched_run_dirs` / `.receipt_provenance` /
  `.suggested_commands`, render sections
- `pipeline/verification_delivery.py` —
  `DeliveryVerificationAssessment.searched_run_dirs` / `.suggested_commands`,
  `receipt_blocker_lines` / `diagnostic_lines` (shared by `lines` and the
  interactive delivery prompt)
- `pipeline/engine/commit_delivery.py` — `_verification_prompt_warning_lines`
  reuses the shared diagnostic lines so the interactive prompt carries the same
  searched dirs + verify hints as the banner
- `pipeline/project/state_setup.py` / `session_run.py` — stamping
  `VERIFICATION_PARENT_RUNS_EXTRAS_KEY` from the follow-up's parent run
- `pipeline/evidence/collector.py` — `_build_verification_readiness` observed-facts
  docstring (review F2 inventory)
- `tests/unit/pipeline/verification/test_receipt_index.py`,
  `tests/unit/pipeline/test_verification_delivery.py`
  (`TestIncidentParentContinuity`),
  `tests/unit/pipeline/evidence/test_verification_readiness_digest.py`
- `docs/architecture/verification_contract.md` — Stage 8 section
