# ADR 0069 — Delivery dialog is offered even when acceptance rejects

- Status: Accepted
- Date: 2026-06-03
- Relates to: ADR 0032 (commit-decision gate), ADR 0043 (commit-delivery apply
  action), ADR 0044 (pre-run dirty intake), ADR 0049 (cross-level commit
  delivery), ADR 0025 (release gate / final_acceptance)
- Extends: the commit-delivery gate behavior from ADR 0032. (ADRs are
  append-only; this records the behavior change rather than editing 0032.)

## Context

After a run finishes, `resolve_commit_delivery` decides whether and how to
deliver the run-owned diff from the isolated worktree into the project checkout.
The gate was release-gated: when the active profile runs `final_acceptance` and
its verdict was anything other than `APPROVED`, `resolve_commit_delivery`
short-circuited to `not_applicable` **before** any prompt. So a run that the
gate rejected closed silently — the operator never saw the diff and had no path
to deliver it, even though they may legitimately want to push the change into
their own checkout anyway (it is their repo and their call).

This conflated two different things: "the gate did not approve this change" and
"the operator may not deliver it." The first is Orcho's verdict; the second is
the operator's decision.

## Decision

Scope the hard block to **non-interactive** runs only. At a TTY, still show the
delivery dialog on a non-`APPROVED` verdict, with guard rails.

1. **Non-interactive (CI / piped / no TTY):** unchanged — a non-`APPROVED`
   verdict returns `not_applicable`. Silently delivering a rejected change in an
   unattended run would be unsafe.

2. **Interactive (TTY):** a correction-first dialog is shown even when
   `final_acceptance` rejected. It carries:
   - a clear warning that final acceptance did **not** approve (the verdict is
     surfaced), and
   - a safe default of `fix`, so a bare Enter never delivers or counts the run
     done — the operator must explicitly pick `approve` / `apply` to override.

`apply_commit_delivery` acts purely on the chosen action and never re-checks the
verdict, so an explicit `approve`/`apply` delivers; `fix`/`skip`/`halt` record
the decision without delivering. The run status follows the chosen action:
`fix` → `halted` with `halt_reason="commit_decision_fix"` so a correction
follow-up is the default resume path; `skip` → run stays `done`; `halt` →
`halted` with `halt_reason="commit_decision_halt"`.

Cross children run non-interactive and therefore stay blocked on a rejected
child, exactly as before (ADR 0049's mono gate is unaffected).

## Consequences

- A rejected run no longer closes silently: the operator sees the diff and can
  choose correction (`fix`), consciously override the gate to deliver, retain
  artifacts (`skip`), or halt.
- The safe interactive default (`fix`) plus the explicit rejection warning mean
  the override is deliberate, never accidental.
- CI / unattended behavior is unchanged: a rejected change is never
  auto-delivered.
- No new top-level run status or wire field — this rides the existing
  commit-delivery decision/artifact shape. The `fix` / `skip` / `halt` help text
  surfaces the run-status consequence because all three avoid delivery.

## Out of scope

- Re-gating `apply_commit_delivery` on the verdict (intentionally not done — the
  operator's explicit choice is authoritative once the dialog is shown).
- Any change to how `final_acceptance` computes its verdict.
