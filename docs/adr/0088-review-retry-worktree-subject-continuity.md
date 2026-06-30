# ADR 0088 — Review-retry worktree-subject continuity on checkpoint-resume

- Status: Accepted
- Date: 2026-06-12
- Relates to: ADR 0033 (orcho-managed per-run worktree isolation), ADR 0075
  (event-sourced run state and terminal writes), ADR 0079 (resume-artifact
  bootstrap)

## Context

A run paused after `review_changes` rejected its change, the operator decided
`retry_feedback`, and the resume was expected to re-run `repair_changes`
against **the rejected diff the reviewer just looked at**. Instead it repaired
nothing: incident `20260612_213530`.

Two facts combined into the bug:

1. **run-dir / `session_ts` drift.** The resumed run directory name was
   `20260612_213530`, but the original run's per-run worktree (ADR 0033) was
   `wt_20260612_213531` — the directory name and the worktree id no longer
   agreed (a one-second drift between the run id used for the run dir and the
   id stamped into the worktree path on the original launch).

2. **`wt_<run_id>` re-minting on resume.** The worktree resolver
   (`resolve_worktree_for_run`) derived the checkout path from
   `wt_<run_id>` and only reused an existing worktree when *that* path already
   existed. Because the resumed run id pointed at `wt_20260612_213530`, which
   was never created, the resolver minted a **fresh, clean** checkout at HEAD.

The repair phase therefore ran in a clean tree with no rejected diff. The
review gate had nothing to repair, and the run silently "succeeded" against an
empty change — the rejected work was abandoned.

The persistent `meta.worktree` block written by the original run already
recorded the real retained worktree (`isolation`, `path`, `base_ref`,
`source_start_head`, …). Nothing consumed it on a checkpoint-resume; the
resolver re-derived the path from the run id instead of rehydrating it.

### Non-Goals (carried from the plan contract)

- No change to provider-session resume/fresh-fallback **semantics** — only how
  they are *surfaced*.
- No global redesign of worktree resolution for follow-up or cross-children
  paths.
- No weakening of decision exact-payload idempotency or halt terminal
  semantics.

## Decision

Make the retained worktree subject a first-class, rehydrated input to
checkpoint-resume, prove the repair subject before any write phase, and make
the operator banner distinguish *provider-session* freshness from
*worktree-subject* continuity.

### 1. Rehydrate the retained worktree subject for all checkpoint-resumes

A focused classifier, `pipeline/project/resume_worktree.py`, reads the prior
persistent `meta.worktree` block (and the `phase_handoff_decisions/` artifacts)
**before** `init_run_session` overwrites `meta.json`, and returns one of three
decision classes:

- **(a) passthrough** — no prior block, or `isolation=off` → returns `None`;
  the resolver behaves exactly as before.
- **(b) reuse retained subject** — the recorded isolated worktree exists and is
  registered in the source repo's `git worktree list` → reuse **that exact
  path** for *any* checkpoint-resume, regardless of the resumed run-dir name.
  The path is handed to `resolve_worktree_for_run` via a new
  `resume_prior_worktree` parameter, attached by recorded path (the same
  attach mechanism as a follow-up parent), **never** re-derived as
  `wt_<run_id>`.
- **(c) retained subject unavailable** — the block records an isolated worktree
  whose path is missing or unregistered:
  - **only when an active review-retry depends on it** (an active
    `review_changes` handoff, or a recorded `retry_feedback` decision for one)
    → a **recoverable** operator error naming the missing path, raised **before
    any clean checkout is materialised**;
  - **otherwise (generic resume)** → passthrough; the resolver keeps its
    current behaviour (reuse `wt_<run_id>` if present, else a fresh checkout).

The abort branch is deliberately **narrow**: it fires only on the active
review-retry path, so generic checkpoint-resume, follow-up continuity, and
cross-children paths are unchanged.

### 2. Prove the repair subject before the write phase (clean-HEAD guard)

`pipeline/project/retry_subject.py` proves the repair subject is present
immediately before `repair_changes` dispatches on the review-retry path
(`apply_review_repair_handoff_retry` in `pipeline/project/handoff.py`). The
subject is proven when:

- **isolated run**: the repair cwd matches the recorded retained path **and**
  the tree carries the diff — `git status --porcelain` non-empty **or** `HEAD`
  has moved off the recorded base (`source_start_head` / `base_ref`); a
  committed diff is a valid subject too;
- **isolation off**: only the dirty / HEAD-shift check on the cwd applies.

An unproven subject raises `RepairSubjectUnproven` (a narrow `RuntimeError`
subclass). The clean-HEAD case uses the exact operator text:

> Cannot run repair_changes against clean HEAD: review retry requires the
> retained rejected diff subject. Resume/apply the retained worktree diff or
> halt this run.

The guard is **read-only** and runs **before** `retry_feedback_handoff` clears
the active payload, so an abort leaves `meta.phase_handoff` and the recorded
decision intact — the run stays decidable and can be resumed again once the
retained worktree diff is restored. It is **not** a torn write.

### 3. Distinguish provider-session freshness from worktree-subject continuity

The pre-retry operator banner (`pipeline/control/handoff_banners.py`) now
carries two independent lines:

- `provider session: …` — resume vs fresh-session fallback (unchanged
  semantics);
- `worktree: retained retry subject <path>` for an isolated run, or
  `worktree: in-place checkout <path>` when isolation was off.

A fresh-session fallback never moves the worktree, so the worktree line is
computed from `meta.worktree` and does **not** change with the provider-session
line. This makes the *change subject* legible separately from the *session*.

## Consequences

- **`meta.worktree.resume_continuity` is additive.** When a checkpoint-resume
  reuses the retained subject, an additive `resume_continuity` sub-block
  (`mode_label` / `path` / `source`) is recorded on the session worktree block
  for inspectability. It is purely additive: existing `meta.worktree` consumers
  are unaffected, and no MCP surface re-declares or rejects it, so no
  `orcho-mcp` change is required.
- **SDK status / snapshot is the MCP-facing handoff-id contract.** The current
  pending handoff id is surfaced deterministically from `meta.phase_handoff`
  through `sdk.status.load_status` (`raw_meta['phase_handoff']['id']`) and the
  `run_control` snapshot's `pending_action.handoff_id`. MCP tools relay those
  SDK surfaces rather than reading `meta.json` themselves, so id-progression
  visibility is fixed once at the SDK boundary with no `orcho-mcp` edit.
- **The incident shape is closed.** A resume whose run-dir name differs from
  the original `wt_<id>` now reuses the retained worktree instead of minting a
  clean one; a genuinely missing retained subject fails loudly and recoverably
  instead of silently repairing an empty tree.
- **No torn state from the guard.** Because the guard is read-only and precedes
  the payload-clearing transition, a clean-HEAD / wrong-cwd abort never marks
  the run terminal; the active handoff + decision remain valid for a later
  resume.

## Alternatives considered

- **Fix only the run-id minting (stabilise `wt_<run_id>` so it matches the
  run dir).** Rejected: it patches one symptom of the drift but does not make
  the retained subject an explicit resume input, so any future divergence
  between the run dir and the recorded worktree (a restored run, a renamed
  directory, a cross alias) would re-open the same silent clean-checkout
  failure. The durable `meta.worktree` block is the authoritative subject;
  resume must read it.
- **Globally abort on any missing retained worktree.** Rejected: it is a
  redesign beyond the stated Non-Goals. A generic checkpoint-resume with no
  active review-retry has legitimate reasons to fall back to the existing
  resolver behaviour (reuse `wt_<run_id>` or a fresh checkout); turning every
  missing retained path into a hard error would break follow-up and
  cross-children paths and change unrelated resume semantics. The hard
  recoverable error is therefore scoped to exactly the branch where losing the
  subject is incorrect — an active review-retry.
