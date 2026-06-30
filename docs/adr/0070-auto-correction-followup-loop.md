# ADR 0070 — Auto-correction follow-up loop on the operator's `fix` choice

- Status: Accepted
- Date: 2026-06-03
- Relates to: ADR 0069 (delivery dialog on rejected acceptance), ADR 0032
  (commit-decision gate), ADR 0025 (release gate / final_acceptance), the
  follow-up resume machinery (`pipeline.control.resume_context`,
  `is_terminal_commit_decision_fix`, `extract_followup_session_seeds`)
- Extends: ADR 0069. (ADRs are append-only; this records the follow-on
  behavior rather than editing 0069.)

## Context

ADR 0069 made the correction gate appear at a TTY even when `final_acceptance`
rejects, with `fix` as the safe default. Choosing `fix` set the run to
`halted` with `halt_reason="commit_decision_fix"` and stopped there. The
intent — stated in 0069 — was that "a correction follow-up is the default
resume path."

In practice that left two gaps the operator actually hit:

1. **The follow-up never launched.** Nothing consumed `commit_decision_fix`
   except `get_resume_intent_options`, which only makes `FOLLOWUP` the default
   *if the operator later runs `--resume` by hand*. No code, and no
   workspace controller, auto-resumed. So `fix` marked the run for correction
   and exited.

2. **The terminal lied.** The halt happens inside `_run_commit_delivery`,
   which runs mid-`finalize()` after the run already reached its `done` tail.
   `finalize_with_terminal_output` printed the green `[DONE] Pipeline complete`
   header unconditionally, so a run whose real status was `halted` looked like
   a clean success. (Phase-handoff pauses avoid this because they return before
   `finalize()` in `profile_dispatch`; delivery-driven and quality-gate halts
   are the ones that reach the wrapper.)

The menu text already promised "Continue with a correction follow-up in the
same retained worktree" — a promise the code did not keep.

## Decision

Honor the promise at a TTY by **auto-launching** the correction follow-up, and
render an honest terminal banner.

1. **Honest halt banner.** `finalize_with_terminal_output` branches on
   `result.status`. A `halted` run renders a `HALTED` header keyed off
   `halt_reason` (amber for recoverable halts — `commit_decision_fix`,
   `commit_decision_halt`, `commit_delivery_target_dirty`; red for
   `commit_delivery_failed`) instead of the green `Pipeline complete`. Unknown
   reasons fall back to the raw reason so a new halt path is never silently
   mislabelled as success. `done` and sub-pipeline paths are unchanged.

2. **Auto-correction loop (interactive only).** When the run returns `halted`
   with `halt_reason="commit_decision_fix"` and the invocation is interactive,
   the CLI re-enters the pipeline as a follow-up run. The interactivity guard
   is the **same** `not no_interactive and stdin+stdout-are-TTYs` test the
   commit-delivery gate uses (`_stdio_interactive`), so the loop fires under
   exactly the conditions that produced the interactive `fix` halt — a piped
   run is never auto-resumed even when `--no-interactive` is absent. Each
   follow-up run:
   - carries the rejection's remediation as its task (synthesized from
     `final_acceptance`'s `short_summary` + structured `verification_gaps`,
     falling back to the rendered `critique`),
   - reuses the parent run as follow-up context, so the retained worktree and
     provider-session seeds carry forward via the existing follow-up
     machinery,
   - mints a fresh run id as a sibling of the parent run dir (the parent stays
     untouched as history).

3. **Operator-gated, no artificial ceiling.** Each correction round again ends
   at the correction gate when acceptance still rejects. The loop therefore
   continues only while the operator keeps choosing `fix`, and stops the moment
   they pick `approve` / `apply` / `skip` / `halt`, or acceptance approves.
   Because a human decision sits between every round, there is no infinite-loop
   risk and no fixed round limit.

4. **Non-interactive transports unchanged.** CI, MCP, and piped runs never
   enter the loop. A `commit_decision_fix` run stays `halted` for an external
   controller (or the operator via `--resume`) to drive. No new run status and
   no wire-format change — this rides the existing follow-up resume contract,
   so `orcho-mcp` needs no matching update.

The loop driver lives in `pipeline/project/correction_followup.py` (sequencing
+ correction-task synthesis only); it injects `run_pipeline` so the CLI and
tests share one path. The CLI owns the interactive guard and run-id minting.

## Consequences

- A TTY operator who picks `fix` sees the correction round start immediately,
  in the same session, against the same worktree — no manual `--resume`.
- The terminal no longer reports `Pipeline complete` for a halted run; the same
  fix corrects the long-standing mislabelling of other delivery/quality-gate
  halts that reach the wrapper.
- Follow-up rounds are real, independent runs (own run dir / meta / metrics),
  so history and retrospective analytics stay per-run and auditable.
- The behavior is purely additive on top of the ADR 0069 dialog; the gate
  semantics, `apply_commit_delivery`, and the verdict computation are
  untouched.

## Out of scope

- A non-interactive auto-correction loop (CI would need a bounded policy and an
  unattended-safety story — deliberately deferred).
- Changing how `final_acceptance` computes its verdict or how the gate renders.
- A hard round ceiling or a no-progress detector — the operator-gated design
  makes both unnecessary for the interactive path.
