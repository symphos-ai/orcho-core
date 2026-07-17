# ADR 0138 — Preserve declared write scope and exact working-tree change identity

- **Status:** Accepted for implementation
- **Date:** 2026-07-17
- **Supersedes:** the change-observation and plan-scope source descriptions in
  [ADR 0110](0110-scope-expansion-notice.md); its severity matrix remains in
  force
- **Builds on:** [ADR 0038](0038-cross-plan-phase-handoff-parity.md),
  [ADR 0050](0050-structured-cross-handoff.md),
  [ADR 0112](0112-multi-project-participant-set-and-scope-expansion-resetup.md)

## Context

Scope-expansion control is correct only when both sides of the comparison carry
the same path identity:

1. the paths the approved plan declares writable; and
2. the paths the working tree actually changed.

Two production run shapes violated that premise before the pure classifier ran.

In a project run, Git's default porcelain status may collapse nested untracked
files into one parent-directory entry. An exact declared file then cannot match
the observed directory. The existing line parser also treats a rename as one
display string and depends on Git's quoting rules.

In a cross-project run, the approved `CrossTaskUnit.files` collection is typed
and durable at the parent level, but the child handoff carries only the subtask
prose. A child without its own parsed plan therefore resolves no declared files
and treats its entire diff as scope expansion.

These are input-identity failures, not classification-policy failures. Changing
severity or hiding directory findings would suppress symptoms while allowing
real unplanned files to disappear.

## Decision

### 1. One resolved declared-write scope per project run

Project control stages consume a typed `DeclaredWriteScope`, resolved from all
authoritative declarations that apply to that project:

- plan-level and subtask-level `owned_files`;
- plan-level and subtask-level `allowed_modifications`;
- the current cross unit's `files`, when the project run is a cross child; and
- project plugin `allowed_modifications`.

Each rule retains its origin so diagnostics can distinguish plan ownership,
cross ownership, and plugin allowance. Resolution removes duplicates but does
not infer ownership from prompts, transcripts, markdown, or changed files.

Existing matching behavior remains: declarations may be exact paths, wildcard
patterns, or directory-prefix patterns. This decision does not introduce a new
task-authoring grammar. It makes the existing declarations reach every control
consumer without being flattened into prose.

### 2. Cross handoff carries the alias-local declared files

The canonical cross handoff gains a typed tuple containing the current
`CrossTaskUnit.files`, normalized from `[alias]/path` to child-relative `path`.
Dispatch validates that entries belong to the child alias; a sibling alias is
not silently accepted.

The child run hydrates `DeclaredWriteScope` from this field during initial setup
and cold resume. The field is control data and is not rendered as an instruction
to the implementation agent. The human-readable handoff may summarize it for
audit, but the runtime never parses that rendering.

Cross dispatch must not synthesize a fake `ParsedPlan`. A cross plan and a
project plan are different artifacts; both project into the same
`DeclaredWriteScope` value object.

### 3. Git observation returns typed exact changes

Working-tree observation uses a NUL-delimited porcelain format with explicit
`--untracked-files=all`. Parsing produces typed change records instead of a
list derived from display lines.

The record preserves:

- status kind;
- current path for additions, untracked files, and modifications;
- removed path for deletions; and
- source and destination paths for renames or copies.

Paths containing whitespace, quoting-sensitive characters, or non-ASCII text
remain exact. Expected Git failures continue to degrade to an empty observation
at the existing helper boundary; malformed successful porcelain output is a
programming error covered by focused parser tests.

For scope comparison, a rename checks both identities: moving a declared file
to an undeclared destination is scope expansion even when the source was owned.
A deletion checks the removed path. Other changes check their current path.

### 4. Classification receives only unmatched file identities

The scope-expansion classifier remains pure and retains the ADR 0110 severity
matrix. A control-layer matcher compares each observed identity with the
resolved declared scope and sends only unmatched paths to signal building and
classification.

An exact in-plan change produces no assessment item, no scope-expansion
handoff, and no terminal warning. A neighboring undeclared file remains fully
classified. Empty declared scope remains fail-closed: every observed change is
out of plan.

### 5. Durable evidence remains owned by core

The existing final-acceptance scope assessment remains the durable source of
truth. Clients forward that assessment; they do not derive scope from Git or
reclassify files.

This correction does not require a new client wire field. If a later change
exposes rule provenance or typed working-tree changes publicly, its schema and
client projection must ship together. That is outside this decision.

## Required invariants

1. A nested untracked file exactly named by `owned_files` is in plan.
2. A second untracked file in the same directory but absent from the declared
   scope is out of plan.
3. A cross child receives exactly its alias-local `CrossTaskUnit.files` on first
   execution and cold resume.
4. Cross ownership is never reconstructed from subtask prose or markdown.
5. Delete and rename classification uses the correct old/new path identities.
6. A path containing whitespace or quoting-sensitive characters is not changed
   by observation.
7. In-plan-only diffs create neither scope findings nor a scope handoff.

## Implementation sequence

1. **I5-S1 — declared scope continuity.** Add the typed resolved scope,
   alias-local handoff field, child hydration, and first-run/resume tests.
2. **I5-S2 — exact working-tree changes.** Add NUL porcelain collection and
   typed add/modify/delete/rename parsing with focused Git-backed tests.
3. **I5-S3 — assessment projection.** Connect both primitives to final
   acceptance and pin mono/cross regressions for empty in-plan assessments and
   genuine neighboring expansion.

The sequence is intentionally narrow. Shared scope fixtures make parallel
implementation unsafe, while each step has a focused contract that can be
verified before the next starts.

## Consequences

- Exact planned files remain exact from plan approval through final acceptance.
- Cross children no longer lose approved file ownership at their handoff
  boundary.
- Untracked files and special-character paths are observed without directory
  aggregation or display parsing.
- Rename-to-new-scope remains detectable rather than being hidden by the owned
  source path.
- The existing risk and blocker policy remains unchanged and receives more
  trustworthy inputs.
- Core gains two small typed primitives instead of adding cross-specific reads
  to the classifier.

## Non-goals

- Redesigning the ADR 0110 severity matrix.
- Changing operating-mode sanction routing from ADR 0112.
- Adding directory-specific suppression for test trees.
- Parsing ownership from agent output.
- Adding a second client-side scope classifier.
