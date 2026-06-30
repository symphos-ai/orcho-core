# ADR 0085 — Correction profile and `correction_triage` entry phase

- Status: Accepted
- Date: 2026-06-11
- Relates to: ADR 0070 (auto-correction follow-up loop), ADR 0069 (delivery
  dialog on rejected acceptance), ADR 0025 (release gate / `final_acceptance`),
  ADR 0022 (workflow-semantic phase taxonomy), ADR 0009 (composable prompt
  parts / prompt-boundary discipline)
- Extends: ADR 0070. (ADRs are append-only; this records the follow-on
  behavior rather than editing 0070.)

## Context

ADR 0070 made the operator's `fix` choice at the correction gate
auto-launch a follow-up run that carries the rejection's remediation,
reuses the parent's retained worktree, and re-enters `run_pipeline`. But
that follow-up re-entered under **the parent's profile** — typically
`advanced`. So closing a narrow release blocker (one missing test, a stale
gate, a contract acknowledgement) ran a full `advanced` cycle end to end:
`plan ↔ validate_plan` re-planning of the *original* task, then
`implement`, the `review_changes ↔ repair_changes` loop, and
`final_acceptance`.

That is the wrong ROI. The change already has a plan and a diff in the
retained worktree; re-planning the original task wastes architect/reviewer
rounds and invites scope drift — the agent re-derives the whole task
instead of closing the listed blockers. What the follow-up actually needs
is a cheap, scoped entry that reads the recorded rejection and decides the
smallest honest way to clear the blockers, then continues straight into
`implement`.

A second, smaller problem: a correction follow-up is a *system* path. It
must never be something an operator picks for a first run — without
correction context it has nothing to triage. So it needs to exist as a
real, dispatchable profile (for `--profile`, resume inheritance, and the
catalog) while staying out of the interactive fresh-run picker.

## Decision

Introduce a first-class internal `correction` profile that opens on a new
read-only `correction_triage` phase.

1. **`correction_triage` built-in phase.** A read-only phase (reuses the
   `review_changes` agent slot, the `compliance_check` precedent) that
   reads `<output_dir>/correction_context.md` — the rejection artifact the
   ADR 0070 driver writes — and classifies how to close the recorded
   release blockers. It does **not** re-plan the original task and does
   **not** widen scope beyond the recorded blockers; the retained worktree
   is the subject of the correction. The machine output shape is
   code-owned in the handler (not in the user-editable `tasks/*.md` part),
   so the ADR 0009 prompt-boundary contract stays intact.

   **Triage contract.** The handler persists a structured record into
   `state.phase_log["correction_triage"]` (and, via
   `CorrectionTriageAdapter`, into
   `session["phases"]["correction_triage"]`):

   - `kind` — one of `code_fix` | `contract_ack` | `gate_rerun` |
     `blocked`.
   - `summary` — one/two sentences on how to close the blockers.
   - `allowed_scope` — the narrow set of files/areas that may be touched.
   - `required_checks` — verification each remediation must pass.
   - `blockers` — populated when `kind=blocked` (what prevents
     remediation).

   Parsing is lenient: an unparseable response or a `kind` outside the
   set normalizes to `blocked` with an explanatory blocker, so evidence
   never sees a bare or invalid verdict.

   **Fail-fast guard.** When started with no correction context (no
   non-empty `correction_context.md` in the run output dir), the phase
   halts the run with `halt_reason="correction_triage_missing_context"`
   before invoking any agent. Run lineage alone — e.g.
   `plan_source_run_id` set by `--from-run-plan` — does not count as
   correction context, because it carries no rejection blockers. This is
   what makes a direct fresh run of the internal profile safe: it stops
   loudly instead of triaging nothing.

   **Stage 0 — no routing FSM.** Every supported `kind` continues the
   pipeline into `implement`; triage records a verdict but does not branch
   the route. No finite-state machine lives in this phase yet.

2. **Internal `correction` profile.** A `kind=custom`, `variant=correction`
   profile in `core/_config/pipeline_profiles_v2.json` with
   `internal=true`. Steps: `correction_triage` → `implement` →
   `review_changes ↔ repair_changes` loop (`until review_changes.clean`,
   `max_rounds 1`) → `final_acceptance`. There is **no** `plan` or
   `validate_plan` — the plan already exists from the parent run. It
   declares no hypothesis block, no `cross` annotations, and no
   `cross_gates`: the correction profile is never projected to cross mode.

3. **First-class `internal` profile field.** `Profile` gains
   `internal: bool = False`, parsed by the JSON loader (default `False`, so
   existing profiles parse unchanged). It is an additive shape extension,
   not a hidden control-flow flag — the profile is a real registry entry.

4. **Visibility policy.** The interactive fresh-run picker
   (`prompt_for_profile_if_needed`) drops `internal=true` profiles from the
   menu **before** applying any caller-supplied filter, so they never reach
   the menu, sub-headers, number slots, or the `?N` detail view. The
   `orcho profiles` catalog keeps internal profiles **visible** (the
   registry must show them) but tags them with an `[internal]` chip.
   Explicit `--profile correction`, resume / `--from-run-plan` inheritance,
   and the programmatic SDK path still reach the profile — the fail-fast in
   `correction_triage` is the safety net for a context-less direct run.

5. **Routing the gate → fix.** The ADR 0070 driver
   (`drive_correction_followups`) pins `profile_name="correction"` on every
   child dispatch (overriding the parent's profile, which never leaks) and
   names the profile in its announce line. The driver is invoked
   exclusively for correction follow-ups, so the override is scoped there;
   the ordinary follow-up / resume / non-interactive paths are untouched.
   Worktree continuity is unchanged — it rides the existing
   `followup_worktree` machinery (a dirty retained parent worktree is
   reused).

## Consequences

- A `fix` follow-up now costs a scoped triage + implement + one review
  round + release gate instead of a full `advanced` re-plan; the agent is
  told to close the listed blockers, not to re-solve the task.
- The triage verdict is durable and readable for evidence / logs, giving a
  structured record of *why* and *how* a correction round proceeded.
- The `correction` profile is dispatchable and catalog-visible yet can
  never be chosen by accident in the interactive picker; a context-less
  direct run fails fast instead of triaging nothing.
- `internal` is a reusable profile property: any future system/internal
  profile inherits the same picker-hiding + catalog-chip behavior.

## Schema / snapshot impact

The new `internal` field and the seventh shipped profile are additive.
Profiles without `internal` parse as `internal=False`; no existing profile
changed. No SDK/MCP wire-format snapshot required updating for Stage 0
(the picker filter and catalog chip are CLI-presentation only). The
built-in phase set and shipped prompt-catalog baseline tests were extended
to include `correction_triage`, and the profile-loader / interactive-picker
tests gained coverage for the new profile and the `internal` filter.

## Out of scope (next stages)

- **Triage-driven routing FSM.** Stage 0 always continues into `implement`.
  A later stage can branch on `kind` — e.g. `gate_rerun` short-circuiting
  straight to the gate without an `implement` pass, or `contract_ack`
  skipping code edits.
- **`gate_rerun` shortcuts.** No phase currently consumes `gate_rerun`
  specially; it is recorded but treated like the other continuing kinds.
- **MCP / UI parity for `internal`.** Exposing the `internal` flag (and
  hiding internal profiles) on the MCP `profiles_list` surface and any UI
  picker is deferred — this is an operator-CLI stage. The wire contract is
  unchanged for Stage 0.
- **Non-interactive correction.** Unchanged from ADR 0070: CI / MCP / piped
  runs still leave a `commit_decision_fix` run `halted` for an external
  controller.
