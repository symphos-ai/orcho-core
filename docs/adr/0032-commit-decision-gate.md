# ADR 0032 — Commit Decision Gate

- **Status:** Accepted
- **Date:** 2026-05-21
- **Deciders:** project owner
- **Companion to:** [ADR 0025](0025-release-gate-and-cross-final-acceptance.md)
  (release gate verdict surface), [ADR 0031](0031-generic-phase-handoff-contract.md)
  (operator-decision artifact pattern this gate reuses)
- **Extended by:** [ADR 0035](0035-terminal-status-and-resume-observability.md)
  (2026-05-24) — the `commit_decision_halt` reason this ADR introduces
  now reaches downstream consumers consistently: top-level
  `meta.halt_reason` is stamped on every halt path (was: only the SDK
  phase-handoff halt wrote it; `state.halt`-driven halts including
  this gate's path hid the reason under nested `meta.halt.reason`).

## Context

The pipeline ends after the release gate (project `final_acceptance`,
cross `cross_final_acceptance`) emits a verdict. When that verdict is
`APPROVED` the change has shipped from the orchestrator's perspective —
but the working tree still carries the diff the run produced. Today the
operator has to:

1. Discover the run finished.
2. Open a terminal in the project directory.
3. Read `git status`, decide what to stage, write a commit message by
   hand, run `git commit`.

That is a manual, undifferentiated chore at the end of every successful
run. The release verdict already carries the structured rationale needed
to summarise the change; the runtime already has a session-scoped agent
that could draft a Conventional Commits message; the engine already has
an audit-grade operator-decision artifact pattern from the phase-handoff
gate.

The post-release commit is the natural place to close the loop.

## Decision

Introduce a generic post-release **commit-decision gate** that fires
whenever a finished run leaves a non-empty diff in the project's git
working tree. The gate is **not** a new first-class phase: it is a
post-finalize lifecycle pause that reuses the operator-decision
machinery of [ADR 0031](0031-generic-phase-handoff-contract.md).

### Lifecycle

After the run reaches a terminal status (`done`, or a halt that still
left files changed), the orchestrator runs:

1. **Snapshot detection.** Compare the current git worktree state to
   the `pre_run_dirty_set` snapshot the orchestrator captured at run
   start. Partition the diff into `changed_by_run` (the delta the run
   produced) and `pre_existing_dirty` (carried forward from before the
   run). Untracked files are listed separately.
2. **Gate decision.** If `changed_by_run` is empty AND no untracked
   files are present, skip the gate — the run produced no commitable
   change. Otherwise build a pending payload and persist it into
   `meta.commit_decision`, flip `meta.status` to
   `awaiting_commit_decision`, and exit.
3. **Operator decision** via `sdk.commit_decision.commit_decide(...)`
   from CLI, MCP, or web UI. Three actions:
   - `approve` — execute `git add` + `git commit` using the chosen
     `CommitMessageStrategy`. The executor returns a sha (on success)
     or an error (hook rejected, signing failure). Either way an
     audit artifact lands under
     `<run_dir>/commit_decisions/<safe_id>.json`.
   - `skip` — finalise the run without committing. The diff stays in
     the working tree as a user-owned change.
   - `halt` — terminate the run as halted with
     `halt_reason = 'commit_decision_halt'`. No commit. Suitable for
     "discard this run, I'll handle the diff manually".

### Commit-message strategies

The pending payload lists three available strategies; the operator
picks one per decision (default seeded from config):

- `release_summary` — reuse the release-gate `short_summary` as the
  subject. No extra LLM call.
- `llm_generate` — synchronously invoke the runtime with the new
  `commit_message_json_contract` (code-owned system-tail block) and
  parse a Conventional Commits JSON object via
  `pipeline.commit_message_parser.parse_commit_message`.
- `operator_typed` — no suggested text; the operator supplies the
  full message at decision time.

`message_override` on the decision payload lets the operator edit any
strategy's output before commit. Strategy and overrides are persisted
into the decision artifact so the audit record is unambiguous about
the exact message used.

### Pre-existing dirty state

Pre-run dirty files are surfaced to the operator but **never auto-
staged**. The default decision payload sets
`include_pre_existing_dirty=false`; an explicit opt-in toggle widens
the staging set. This honours the same "never hide pre-existing dirty
state" rule the workspace development pipeline enforces for manual
work.

### Untracked files

Untracked files are added via `git add -A` when
`include_untracked=true` (config default). The gate relies on the
project's `.gitignore` and surfaces the untracked list to the
operator so a noisy `.gitignore` is visible before approval.

### Cross-project

In cross-project runs the gate fires **per alias** after each child
sub-pipeline's release. The cross-orchestrator pauses on
`awaiting_commit_decision` (kind=`cross_per_alias`) listing every
pending alias. Operator decisions land per-alias; once all are
recorded the cross run resumes into `cross_final_acceptance` as
normal. A `cross_commit_summary` aggregator row table is written
into `session["phases"]` with one row per alias: `{alias, action,
strategy, commit_sha, commit_status, commit_error}`. Cross-project
**fail-forward semantics**: a commit failure on one alias does not
re-pause the cross run; the failure is recorded in the summary and
the cross run continues to `cross_final_acceptance`, which treats
the failure as a blocker by precondition.

### Config and CI defaults

A new `commit` section in `config.defaults.json` controls behaviour:

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | Gate fires post-release. Set to `false` to disable wholesale. |
| `default_strategy` | `"release_summary"` | Strategy preselected in interactive UI. |
| `auto_in_ci` | `"approve"` | Non-interactive default: `"approve"` auto-commits with the default strategy; `"skip"` never commits. |
| `add_untracked` | `true` | Whether `git add -A` semantics apply on approve. |
| `include_pre_existing_dirty` | `false` | Whether pre-run dirty files are auto-staged. |
| `git_user_identity` | `null` | Optional `{name, email}` fallback for projects without `user.email`. |

CI behaviour: with `auto_in_ci="approve"` and no operator decision,
the executor commits using the chosen `default_strategy`. The
`commit_decisions/<safe_id>.json` artifact records `operator: null`
so the audit trail makes the CI origin obvious.

### Wire surface

`meta.commit_decision` is the active pending payload; the persisted
decision artifact is the audit record. `meta.status` gains the
`awaiting_commit_decision` value. The SDK exposes
`commit_decide`, `load_active_commit_decision`,
`load_commit_decisions`. The MCP server adds an
`orcho_commit_decide` tool mirroring `orcho_phase_handoff_decide`.

The schema bodies live in
`core.contracts.commit_decision_schema`:

- `validate_commit_message_dict` — LLM strategy output;
- `validate_pending_dict` — active gate payload;
- `validate_decision_dict` — persisted operator artifact.

The prompt contract lives in `pipeline.prompts.contracts.commit_message_json_contract` —
code-owned per the prompt boundary rule. A user-editable
`_prompts/tasks/commit_message.md` exists for prose framing only and
carries no parser contract, no schema body, no language directive,
no protocol enum.

## Consequences

**Adds** an audit-grade, structured close to every pipeline run. Cuts
manual `git commit` ceremony. Makes the run's intent visible in
`git log` automatically.

**Couples** the runtime to a third gate verdict surface (post the
review and release gates); the operator-decision artifact directory
becomes the single audit trail for both kinds of pause.

**Expects** projects to set `commit.enabled=false` in
`config.local.json` when they own commits via an outer wrapper (e.g.
a project automation that bundles many runs into a single commit).
The gate is enabled by default because the common case is one-run-
one-commit.

## Out of scope

- Pushing, tagging, branching, PR creation, signing setup. The gate
  only invokes `git add` + `git commit`. Anything else is a follow-up.
- Squash / amend / interactive rebase modes. The gate creates new
  commits only; amending a published commit is hostile to audit.
- Partial-commit recovery when a hook rejects a cross-project alias
  commit (see fail-forward above). A re-pause-on-failure follow-up
  may revisit this when there is operator demand.
