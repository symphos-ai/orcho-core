# ADR 0174 — `timeout` is a typed command outcome and failure kind

- **Status:** Accepted
- **Date:** 2026-08-05
- **Extends:** [ADR 0080](0080-verification-contract-command-receipts.md), [ADR 0117](0117-verification-blocking-tier-independent-of-cost.md), and [ADR 0173](0173-verification-command-timeout.md)

## Context

ADR 0173 made the per-command wall-clock budget declarable, but a gate that ran
out of budget was still indistinguishable — everywhere above the raw receipt —
from a gate whose execution environment broke:

- `classify_receipt` mapped every missing exit code to `env_failure` with the
  boilerplate reason "command execution did not report an exit code",
  discarding the executor's `command timed out after Ns` detail. The handoff
  evidence line (`class=env_failure; exit_code=None; assertions=0/0 passed`)
  carried nothing an operator could act on.
- `consequence_by_command` downgrades hygiene failures (`provenance_failure`,
  `env_failure`) from `required_action` to `warning`. A timed-out required gate
  inherited that downgrade — so the readiness projection called it a warning
  while the delivery gate blocked on the very same receipt. One receipt, two
  opposite meanings.
- Gate routing labelled it hygiene: severity P3, and a `required_fix` that said
  "fix the verification environment", which is not where the fix lives.

The real incident: a vitest gate hung in a run worktree (600.018s, empty
output), was reported as an environment problem, and cost the operator a manual
diff through the receipt JSON to learn what actually happened.

## Decision

**1. The executor records how the execution ended.** `_execute` returns a typed
`outcome` — one of `completed | timeout | error | empty` — and the command
receipt persists it (schema v4). `completed` means the process reached its own
exit code; the other three explain an absent exit code without parsing prose.
The evidence v1 bundle and MCP wire are unchanged: the receipt is a run-local
durable artifact, and `summarize_command_receipts` deliberately omits the new
field (falsifier).

**2. `timeout` is a `FailureKind`.** `classify_receipt` maps
`outcome == "timeout"` to `("failed", "timeout")` and carries the executor's
detail as the reason. A pre-v4 receipt (no `outcome`) keeps the untyped
`env_failure` path, but now also carries its `detail` as the reason instead of
boilerplate. `format_receipt_failure` appends the reason for timeouts — a
timed-out receipt has empty output tails by construction, so without it the
evidence line is empty of facts.

**3. Routing treats timeout as agent-unfixable but not hygiene.**

- *Consequence:* `timeout` is **not** added to the hygiene downgrade in
  `consequence_by_command`. A command that never finished proved nothing about
  the change; a required gate stays `required_action`. This aligns the
  readiness projection with the delivery gate's existing blocking behaviour —
  it does not newly block anything that previously passed.
- *Repair:* `timeout` joins the agent-unfixable set (`_AGENT_UNFIXABLE_KINDS`)
  in gate repair — no repair rounds are burned, the run pauses for the
  operator with `continue_with_waiver` / `halt`. An agent cannot raise the
  declared budget, and a genuine hang is a diagnosis, not a code edit.
- *Severity and fix:* the finding is **P1** (a required gate with no verdict is
  a blocker, not a hygiene note), and its `required_fix` names the actual
  levers: raise `verification.commands[<name>].timeout` if the command is
  honestly that long, or diagnose the hang — empty output with duration pinned
  to the budget means it never started producing work.
- The re-park path (`repark_verification_handoff_retry_blocked`) reads the
  persisted `failure_kind` to decide the offered actions, falling back to the
  old P3-severity proxy only for findings persisted before this ADR. The proxy
  alone would have offered a timeout a repair retry it cannot use.

## Consequences

- Operators see `class=timeout … command timed out after Ns` at the pause, in
  gap dicts, and in handoff findings — not "environment problem".
- Command receipt schema is v4. Readers are tolerant (unknown fields ignored;
  absent `outcome` classifies as before), so old receipts need no migration.
- The hygiene set and the severity set are now distinct on purpose:
  `_AGENT_UNFIXABLE_KINDS` (routing) ⊋ `_HYGIENE_SEVERITY_KINDS` (severity).
  A future kind must choose its membership in each explicitly.
