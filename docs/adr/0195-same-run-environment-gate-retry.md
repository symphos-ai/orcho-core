# ADR 0195 — Same-run environment-gate retry

- **Status:** Accepted
- **Date:** 2026-09-17
- **Related:** [ADR 0031](0031-generic-phase-handoff-contract.md),
  [ADR 0072](0072-continue-with-waiver-handoff-action.md),
  [ADR 0088](0088-review-retry-worktree-subject-continuity.md),
  [ADR 0130](0130-typed-verification-failure-and-hygiene-delivery-policy.md),
  [ADR 0153](0153-gate-handoff-retry-executability.md),
  [ADR 0186](0186-gate-hooks-route-the-whole-failure-set.md)

## Context

An `env_failure` (ADR 0130) is the verification outcome where the command
never produced a verdict at all: the *environment* it was declared to run in
was wrong. ADR 0130 routes it like hygiene — no repair rounds, because source
edits cannot fix a missing interpreter — and ADR 0153 pins the resulting menu
to exactly `continue_with_waiver` / `halt`.

Dogfood run `20260917_172755_061517` shows what that costs when the
environment is repairable. Its `after_phase(implement)` hook resolved six
declared rows, selected four, and every one of the four failed `env_failure`
on the same assertion — a promotion checkout's `.venv/bin/python` did not
exist:

```
selection after_phase implement  (6 rows: 4 selected, 2 not_selected)
execution after_phase implement env-provenance  fail  …/env-provenance--after_phase--implement--0001.json
execution after_phase implement lint            fail  …/lint--after_phase--implement--0001.json
execution after_phase implement broad-non-e2e   fail  …/broad-non-e2e--after_phase--implement--0001.json
execution after_phase implement cli-sdk-unit    fail  …/cli-sdk-unit--after_phase--implement--0001.json
```

The run parked on `gate:env-provenance:1` with
`available_actions: ["continue_with_waiver", "halt"]`. The operator can create
the missing venv in under a minute — but the menu offers no way to say so. The
two published actions are:

- **`continue_with_waiver`** — accept the change on a durable waiver, i.e.
  ship an implement phase whose lint, broad test suite, and CLI/SDK units were
  never actually measured. The waiver is honest about *what it waives*, but it
  is not proof, and ADR 0192 already establishes that a general waiver does not
  excuse required verification proof.
- **`halt`** — throw away a completed implement phase and its retained
  worktree because a path outside the checkout was missing.

Neither is the truth of the situation, which is: *nothing is wrong with the
change; re-measure it*.

**Why `retry_feedback` is the wrong transition.** It is tempting to reuse the
existing retry, and the existing retry is wrong on every axis:

- it dispatches an agent round (`repair_changes`) against a change no agent
  needs to touch, spending tokens and a repair budget on work nobody performed;
- it requires operator feedback (ADR 0176 delivers that text to the agent) —
  but there is no critique to give, since the failure was never about the code;
- it mutates the subject, so the tree the gates re-measure is no longer the
  tree the failing receipts observed;
- it is not even offered here: ADR 0153 correctly withholds it from a hygiene
  menu, precisely because a repair round cannot fix an environment.

The missing action is not a variant of the repair retry. It is a different
transition: *re-execute the measurement, change nothing*.

## Decision

Add a fifth operator action to the phase-handoff vocabulary:
**`retry_verification`** — the operator repaired the external preconditions a
blocking gate set tripped on, and the engine re-executes exactly that
persisted set on the retained subject, with no agent involved.

### Admission

The action is published in `available_actions` only when *both* hold, checked
by `pipeline/project/gate_handoff_actions.py`:

1. **Every** blocking finding is explicitly `failure_kind: "env_failure"`.
   Read from the typed kind, never proxied through severity, and never
   defaulted: a finding written without an explicit kind is not evidence of an
   environment failure, and a set mixing `env_failure` with a timeout or a real
   test failure is not one a rerun can close.
2. The persisted record proves *which* gates to re-execute: a valid,
   duplicate-free `gate_identities` set whose primary (`gate_identity`) is a
   member, a `receipt_evidence` pointer on **every** element, and agreement
   between the findings' commands and the identities' commands.

Clause 2 is the reason the action is honest rather than optimistic. Offering a
re-execution the engine cannot address — because it cannot say which receipts
the operator decided on — would be a promise made on a guess. Where the record
falls short, the menu is simply the ADR 0153 one: `continue_with_waiver` /
`halt`.

Admission is recomputed from the persisted record every time a menu is
published, including the re-park path, so a record that has become defective
never re-offers the action.

### Semantics

- **No feedback.** `retry_verification` takes no operator text, in the SDK, in
  the interactive prompt, or in the advice contract. The operator already acted
  — outside the run — by repairing the environment.
- **Immutable decision, fresh id on re-park.** The decision artifact keeps the
  ADR 0031 / ADR 0153 properties: exact-payload idempotent, append-only audit
  evidence. A blocked retry never reuses its id; it re-parks with a fresh
  `:retry_blocked` id whose menu is recomputed from what the record can still
  prove.
- **Same run, same subject.** No new run id, no new run dir, no new checkout.
  The gates re-measure the retained verification subject the failing receipts
  observed.
- **Exactly the persisted set.** The re-execution runs the identities the
  handoff recorded — not a re-derived selection — through the existing
  executor (`gate_repair.rerun_verification_handoff_gate`, `rerun=true` in the
  ledger trail), the same owner the `retry_feedback` path uses after its repair
  round. Nothing about execution, receipt writing, or ledger accounting is
  re-implemented for this action.
- **No phases.** No `plan`, no `implement`, no `review_changes`, no
  `repair_changes`. The env-retry owner deliberately never reaches the
  repair/FSM dispatch seams: borrowing one would spend a repair budget on work
  no agent performed.

### Routing

`retry_verification` gets its own resume owner,
`pipeline/project/verification_env_retry.py`, and the whole pre-router resume
setup is folded into its boundary:

- **Early branch.** `apply_phase_handoff_resume` routes on the action *before*
  route classification and before the scheduled-gate ledger is read. For this
  action the ledger is the retry's *evidence*; letting a generic route blocker
  raise on it first would convert a recoverable, re-parkable operator state
  into a hard resume failure.
- **One detection.** Whether a resume is an env retry is decided once, in
  `session_run` before the header prints, from the prior `meta.json` and the
  tolerantly-read decision artifacts. Two independent derivations could
  disagree about which resume they are setting up — one treating a corrupt
  ledger as fatal while the other made it survivable.
- **Tolerant header read.** The run header reads the ledger only to colour the
  gate matrix with what already ran. Under an env retry that decorative read is
  downgraded (`ledger_read_tolerant`): a failed read renders exactly as an
  unwritten ledger does, so a corrupt artifact cannot kill the run from inside
  a courtesy banner before its owner has judged it. The flag carries no
  authoritative meaning, and every other resume keeps the strict read.
- **Setup wrapper → marker → re-park.** Authoritative ledger initialization
  makes the *same* `initialize(state, resume=True)` call; under an env retry a
  `LedgerStoreError` / `ResumeVerificationLedgerError` / `OSError` is recorded
  as a typed marker in `state.extras` instead of propagating out of setup. The
  owner reads the marker, executes nothing, and re-parks the pause with that
  reason through the normal pause tail. Fail-closed is preserved — the marker
  is what blocks re-execution — but the operator is left with a decidable pause
  instead of a refused run. The owner deliberately does not re-read the ledger
  the setup step already refused: two readers disagreeing about one file, with
  the laxer one winning, is the failure mode itself.
- **The claim, not the record, opens the boundary.** Every pre-router guard
  above keys off a *tolerant* reading of the decision artifacts: any file
  addressed to the active pause that records `retry_verification` puts the
  resume inside this boundary, even when its own persisted ids are corrupted
  and the strict reader will refuse it. The strict refusal happens inside the
  resume router, after the point where an ordinary resume may legitimately mint
  a fresh checkout — so a guard that demanded a valid record first would
  destroy the retained subject on the way to discovering the record was
  unusable. Treating the claim as sufficient costs an ordinary resume nothing:
  the strict reader still refuses the decision, and the refusal lands as a
  `:retry_blocked` re-park naming the corrupted audit record instead of a hard
  resume failure on a run that is otherwise intact and decidable.
- **Retained worktree is mandatory.** An env retry joins the review retry as a
  resume whose retained tree the resolver may not re-derive (ADR 0088, class
  (c)): a missing, unregistered, or reclaimed worktree blocks *before* any
  checkout is materialised, with an operator message naming the path to
  restore, and leaves `meta.phase_handoff` and `meta.worktree` intact. A fresh
  checkout would lose the rejected diff in the review case and re-measure a
  *different* tree in this one — a rerun against content the operator never
  decided on.

### Evidence chain

Because nothing is repaired, the only thing between the decision and a
re-execution is evidence. Four independent facts must name the same thing, all
checked before any mutation:

| Link | Proof |
|---|---|
| decision | the decision artifact passes strict validation, the active payload still matches the decided id, the menu really offered the action, and the record is admissible |
| scope | every identity names the same hook and phase, the pause's own phase is the one routing derives from that hook (`_handoff_phase`, one rule on both sides), and a recorded loop position names that same hook |
| ledger | every identity is a durably `selected` row whose **latest** `execution` event both `fail`ed and points at the same `receipt_evidence` the handoff recorded |
| receipt | the pointer resolves *inside this run's* execution-evidence directory, and the file it names parses in **strict form before classification** and classifies `failed` / `env_failure` |
| subject | the retained worktree exists, is not reclaimed, is the gate cwd, and sits at the `observed_head_oid` the failing receipts agree on |

Details that are decisions, not incidentals:

- **Latest execution, exact triple.** A `pass` recorded after the decided
  failure means the evidence is stale; a missing execution means the record was
  altered. Either way the operator decided on something that is no longer true.
- **The pause's phase is bound to its gates.** Every check after admission
  reads the *identities*; the continuation reads the pause's *phase*, which is
  what decides how much of the pipeline is behind the resume point. Left
  unbound the two halves can disagree: a record whose gates still prove
  `after_phase(implement)` but whose phase was moved to `final_acceptance`
  re-runs the right commands and then reports the review loop and the terminal
  gate complete, ending a run that never verified them. So the scope must be
  one scope, and it is checked before any gate runs.
- **The evidence pointer is resolved, not joined.** `receipt_evidence` is a
  run-dir-relative pointer, but it is a string in a record a crash or a
  hand-edit can rewrite. An absolute path, a `..` segment, or a symlink leading
  out of `verification_command_receipts/executions/` is refused before the file
  is read: a handoff and a ledger that were damaged *consistently* would
  otherwise name a perfectly valid receipt belonging to another run, and the
  retry would prove its gate set against evidence this run never produced. A
  pointer the filesystem cannot resolve at all (a symlink loop, a broken
  traversal) is the same block, not an exception out of resume — unresolvable
  and misdirected are equally unproven, and both must leave a decidable pause.
- **Strict receipt form first.** `classify_receipt` is tolerant by design (it
  must read historical and hand-built receipts), so it would happily read `{}`
  as a missing-exit-code `env_failure` — exactly the shape this retry looks
  for. Only a receipt whose fields are all present and typed is evidence.
- **Explicit unavailable subject is admissible.** A receipt that honestly
  recorded `subject: {"status": "unavailable", "reason": …}` weakens the HEAD
  comparison to nothing, but the retained-worktree checks stay mandatory. A
  set whose available HEADs disagree is a block: it was measured across two
  trees.
- **`tree_oid` is deliberately not compared.** A gate that writes inside its
  own checkout — a cache dir, a build artifact — moves the tree oid without the
  run having been resumed anywhere else. `observed_head_oid` plus the retained
  path is the honest identity here. Note this proves the *opposite* property to
  the ADR 0088 repair guard, which requires a dirty tree: there the subject is
  a diff to fix, here it is a measurement to repeat.

### Outcomes

- **Every gate passes** → the decision is consumed and the run continues from
  the point the gates were scheduled at, in the same run, with real receipts.
  Where that point is depends on the hook: an `after_phase` gate reported on a
  phase that has run, so the run continues *after* it; a `before_phase` /
  `before_delivery` gate *guards* a phase that has not run, so that phase is
  the first thing the continuation executes. Reporting a guarded phase complete
  would end the run without the very phase the gate was protecting — a green
  `before_delivery` rerun that skipped `final_acceptance` is the shape of that
  mistake.
  Everything up to and including that phase is reported completed, and reported
  **silently**: an ordinary resume-skip fires the runner's trace callbacks so an
  operator can see why a phase did not re-run, but here a `phase.start` for
  `implement` would announce to every `events.jsonl` consumer an agent round
  this action is forbidden to take. The phase log still records the skip, so the
  run's own account of itself stays complete. The plan loop is dropped from the
  walked profile (the same treatment the gate `continue` arms give it) because a
  loop still present can be re-entered by a persisted round cursor; its parsed
  plan is rehydrated for the phases ahead. A raising phase *inside* a loop is
  positioned by a validated `LoopResumeCursor` built from the loop position
  routing recorded on the pause (`gate_loop_position`: loop key, declared member
  order, round, phase, the gate's hook, the effective round budget, whether that
  round satisfied the loop's `until` clause, the members the round actually ran
  in the order it ran them, and the *dispatch mode* that produced that order) and re-checked against the live profile — the round picks up at the
  first member it still owes, which the runner reaches with everything already
  finished skipped callback-free. The executed list is what makes that
  trustworthy: a `retry_feedback` round repairs *before* it reviews, so a gate
  pausing on its repair leaves the review owed even though the repair is the
  later member in the declaration. Members finished out of declared order are
  carried as the cursor's `done_phases`, because an ordered prefix cannot say
  "the second member ran and the first has not". An order alone is only a
  claim — "review then repair" is what the runner leaves *and* what a record
  would say to make a half-finished human-directed round look complete — so the
  record names its dispatcher and each dispatcher admits exactly one family of
  orders: the runner and the plan retry walk the declared order, the
  human-directed review retry repairs then reviews. An order outside its own
  mode's family, an unknown mode, or a raising phase that is not where that mode
  would stand, is refused before a gate runs. A pause after the round ran
  *every* member lands on the loop's own boundary question, and the recorded
  answer decides it: a satisfied `until` or an exhausted budget closes the loop,
  otherwise the continuation opens the next round without replaying the finished
  one. The restored extension is read from the recorded budget, never from the
  round that paused — an operator who granted two extra rounds and paused in the
  first still has the second coming. The verdict is recorded at pause
  time on purpose — a rebuilt state has no round verdict in its phase log, so a
  resume asking the predicate afterwards would read "not satisfied" and re-open
  a loop the run had already closed. The budget is the *effective* one: a
  `retry_feedback` round runs past the loop's declared `max_rounds`, and both
  direct retry seams stamp the full position so a gate failing inside such a
  round stays resumable. A loop-internal pause whose position is missing or
  disagrees with the profile is blocked *before* the transition, with no gate
  executed: the engine has no round-level resume it may invent, and continuing
  from a guessed member is the re-execution this action exists to prevent. One more member can be reached and
  have nothing to do — the repair member of a round whose review came back
  clean. It still dispatches (handler, adapters, checkpoint and metrics
  unchanged), but with its start/end callbacks withheld, because a `phase.start`
  for a write phase is what an agent round looks like from outside the run. The
  test is the loop's own `until` clause, and a member that turns out to have
  worked anyway still announces itself. Everything else *after* the resume point
  — the review round, the terminal gate — runs exactly as it would have if the
  gates had passed the first time: this action re-measures, it never shortens
  the pipeline. The interactive prompt path re-dispatches in-process and carries
  the identical continuation rules, so an operator deciding at the TTY and one
  deciding through the SDK leave the same trace.
- **Some gate still fails** → the rerun publishes a fresh handoff naming what
  is still red; the run re-pauses normally. That pause is published from
  *outside* the loop that owned the round, where no live position exists, so
  the boundary the previous pause proved is carried into it verbatim — nothing
  executed in between, so it is still the run's real position. Without the
  carry the fresh menu would keep offering a retry that the next resume could
  only refuse as unlocatable.
- **Any evidence defect** → a `:retry_blocked` re-park carrying the specific
  reason, with **zero gate commands executed** and the original subject left
  intact. A provider/process exception (`AgentCallError`) is deliberately
  re-raised rather than re-parked: an env retry dispatches no agent, so one
  surfacing from the shared executor keeps its established
  interrupted/failed lifecycle.

### What does not change

- The hygiene routing of ADR 0130 and the menu policy of ADR 0153 for every
  set this action does not admit. `timeout`, `provenance_failure`, and
  `unverifiable` keep `continue_with_waiver` / `halt`.
- `continue_with_waiver` (ADR 0072) and `retry_feedback` (ADR 0176) semantics,
  including their mandatory operator verdict.
- The delivery guard. It remains the last fail-closed line; a red required
  receipt still blocks delivery.
- Header rendering, ledger initialization, and worktree resolution for **all
  other** resumes: the tolerant read, the marker-recording wrapper, and the
  retained-worktree block are each gated on the env-retry detection, and the
  default path is the original code.

## Consequences

- **`receipt_evidence` is the only artifact-shape addition.** Each
  `gate_identities` element (ADR 0186) may carry a run-dir-relative
  `receipt_evidence` pointer to the immutable evidence file of the execution
  that produced that failure. The key is *omitted*, never blanked, when the
  evidence write did not land, so a consumer reads its absence as "unproven"
  rather than as an empty pointer. `gate_identity` stays a bare triple: it is
  the waiver identity and the handoff-route key, both single-identity contracts
  that compare the whole mapping.
- **The action vocabulary is wider.** `retry_verification` is added to
  `PhaseHandoffAction`, `HandoffAction`, the SDK's `PhaseHandoffActionValue`,
  the decision-action literal, the advice contract, and the interactive prompt
  (key `7` / `v`, deliberately skipping the numbers the advisory pseudo-actions
  own). It is a protocol change: clients that enumerate actions exhaustively
  see a new value. SDK membership validation is unchanged — a client still
  cannot submit an action the published menu did not offer.
- **An env failure can now end in proof instead of a waiver.** The hygiene
  advisor recommends `retry_verification` over `continue_with_waiver` whenever
  the menu offers it, because the change keeps real verification rather than
  being accepted on a waived one.
- **A blocked retry costs a pause, not a run.** Every evidence defect and
  every setup refusal lands as a decidable pause with a named reason, so the
  operator can restore the missing artifact and decide again.

## Non-goals

- **Not a recovery framework.** This is one action for one typed failure kind.
  No generalized action-policy matrix, no retry for `timeout` /
  `provenance_failure` / `unverifiable`, no cross-project handoff change.
- **MCP rendering is a separate plugin task.** The core wire carries the new
  value through existing fields (`available_actions`, the decision artifact,
  `artifacts.gate_identities`); how a client surfaces and labels it belongs to
  that client.
- **Extension lexicon stays neutral.** The action is part of the engine's
  handoff protocol, not a provider- or profile-specific behaviour; registered
  runtimes, phases, and skills own no part of it.
