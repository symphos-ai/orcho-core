# ADR 0066 — Repair receipts for re-review freshness

- Status: Accepted
- Date: 2026-06-01
- Relates to: ADR 0026 (session-aware prompt parts), ADR 0039
  (post-repair re-review), ADR 0060 (PromptTurn canonical render surface),
  ADR 0063 (resume-delta task drop)

## Context

Orcho has several loops with the same semantic shape:

```text
produce subject -> review subject -> repair subject -> re-review subject
```

The plan loop uses `plan -> validate_plan -> replan -> validate_plan`.
The implementation loop uses
`implement -> review_changes -> repair_changes -> review_changes`.

Before this ADR, the loop carried reviewer feedback forward, but it did not
carry a protocol-level message that answered two simple questions for the next
review pass:

```text
1. What does the repair phase claim it fixed?
2. What is the current subject to check now?
```

This gap lets a resumed reviewer session repeat stale findings from memory
instead of checking the repaired subject. The issue is not a PromptTurn render
bug: the prompt engine can honestly render full/delta/effective prompts. The
missing piece is the review/repair communication contract above the prompt
transport.

`git status` / `git diff` are not a general solution. Planning may keep the
review subject in pipeline state as typed plan data, not as a file change. Code
review may target a main checkout, an isolated checkout, or generated files.
The protocol must therefore stay subject-neutral.

## Decision

Add a minimal repair receipt protocol:

```text
RepairReceipt:
  fixed / partially_fixed / waived / still_open items
  source phase + round
  repair phase + round
  notes

Re-review packet:
  repair receipt
  current review subject
```

The current review subject is a text projection built by the owning phase, not
a universal object model:

- `validate_plan` after replan renders the subject from `state.parsed_plan`
  (typed contract + task decomposition). It does not depend on git.
- `review_changes` after `repair_changes` renders a small current change
  subject from the active project directory. Git data is only a backend detail
  for this code/file-oriented phase.

The next review pass receives the packet as two PromptTurn parts:

```text
repair_receipt:latest
current_review_subject:latest
```

Both are `TURN` / `NONE` parts. They are always selected on the first
post-repair re-review, even when the runtime session is resumed and delta
rendering is active. Delta may omit old stable method parts, but it must not
hide the fresh receipt or fresh subject.

## Reviewer contract

Re-review should treat the receipt as a claim, not proof:

```text
First verify the repair receipt against the current review subject.
Do not repeat a previous finding unless the current subject still proves it.
Carry operator-waived findings separately from blocking findings.
Report new findings separately from unresolved prior findings.
```

The first implementation enforces the transport invariant: the packet reaches
the reviewer. Richer reviewer output buckets can evolve later.

## Consequences

- Reviewer session continuity is preserved. Orcho does not need to start a
  fresh provider session by default after every repair.
- The stale-memory failure mode is reduced: the reviewer gets fresh evidence
  and a concrete repair claim inside the effective prompt.
- Plan validation remains independent of git/worktree state.
- Code review stays flexible: the phase chooses how to render the current
  subject for its active change source.
- Round session records persist `repair_receipt`, so later evidence/resume
  surfaces can inspect the repair claim without parsing free-form transcript.

## Non-goals

- No large `ReviewSubjectSnapshot` hierarchy.
- No universal diff engine.
- No requirement that every subject be backed by git.
- No immediate rewrite of reviewer JSON schemas. The receipt protocol is the
  missing handoff message; schema refinements can follow when needed.
