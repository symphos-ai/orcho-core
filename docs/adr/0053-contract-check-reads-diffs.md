# ADR 0053 — contract_check reviews the actual per-alias diffs

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** project owner
- **Relates to:** [ADR 0047](0047-cross-project-application-boundary.md),
  [ADR 0049](0049-cross-level-commit-delivery.md)

## Context

The cross run has two read-only gates after the per-project pipelines:

1. **`contract_check`** (`cross_contract_bundle`, artifact-bundle mode)
   — a single Codex call over a *text bundle*: the task, the cross
   plan, and per-alias `final_acceptance` summaries
   (`pipeline/cross_project/artifact_bundle.py`). It is explicitly told
   **not** to inspect the working tree.
2. **`cross_final_acceptance`** (CFA) — the system release gate. It is
   pointed at the per-alias worktrees and runs `git diff` itself, and
   its prompt **explicitly defers** pairwise contract checking to
   `contract_check` ("You are not re-checking pairwise contracts").

The problem: `contract_check` only ever saw the bundle's *textual
claims* (the plan + the child verdicts, which already gated each repo).
Its docstring claimed it received "recent diffs", but
`_per_alias_summary` never added them. So it re-attested claims it
could not verify — in a clean run its `checks` literally read "Verified
the bundle **claims** …". Meanwhile CFA, which *does* read the diffs, is
told not to own contract checking. The actual cross-contract
verification on code therefore fell between the two gates; it only
happened in practice because CFA incidentally diffs the worktrees.

## Decision

Make `contract_check` earn its keep: splice each alias's **actual
unified diff** into the bundle so the reviewer verifies producer/
consumer contract consistency against changed code, not claims.

- The caller (`pipeline/cross_project/app.py`) captures each alias's
  diff from its worktree checkout via
  `core.io.git_helpers.worktree_diff_against_base` (never raises;
  returns a sentinel on clean/unavailable) and passes them to
  `build_bundle_markdown(alias_diffs=...)`.
- `build_bundle_markdown` stays pure (no git I/O). It renders a
  `## Per-alias diffs` section, each diff capped
  (`_ALIAS_DIFF_CHAR_CAP = 6000`) with a truncation marker; the full
  diff stays in run artifacts. Sentinel/empty diffs are skipped, so a
  verification-only alias adds no noise and the section is omitted
  entirely when nothing changed.
- The checklist gains a bullet grounding every finding in those diffs.
- CFA's deferral to `contract_check` is now honest: the gate it defers
  to actually inspects the code.

## Scope / non-goals

- The `contract_check` verdict shape is unchanged — still one
  `review_json` object mirrored per alias (`source="artifact_bundle"`).
  No wire-type or evidence-shape change; MCP/web consumers are
  unaffected.
- Does **not** remove `contract_check` or fold it into CFA (the
  "fifth wheel" option). That remains a possible future ADR; this
  change makes the gate non-redundant instead, which is the cheaper
  and lower-blast-radius fix.
- No source-path leak (cf. [ADR 0050](0050-structured-cross-handoff.md)):
  `git diff` bodies are repo-relative (`a/… b/…`), and the worktree
  paths already shown in the bundle are the run checkouts, never the
  user's source.

## Consequences

- `contract_check` costs more tokens (bounded by the per-alias cap) but
  produces an independent, code-grounded cross-contract signal instead
  of restating child verdicts.
- Untracked (brand-new) files are not surfaced — `git diff` excludes
  them, matching how the cross gates already operate in
  `review_uncommitted` mode. If a future task needs new-file coverage,
  add a `git status --porcelain` companion (same gap CFA has today).
