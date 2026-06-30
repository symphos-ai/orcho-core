# ADR 0050 — Structured cross handoff (JSON source of truth)

- **Status:** Accepted
- **Date:** 2026-05-28
- **Deciders:** project owner
- **Relates to:** [ADR 0047](0047-cross-project-application-boundary.md),
  [ADR 0049](0049-cross-level-commit-delivery.md)

## Context

The cross orchestrator writes a per-alias handoff as **two** artifacts
(`pipeline/cross_project/handoff.py`): `implementation_handoff.md` and
`implementation_handoff.json`. The child sub-pipeline
(`pipeline/project/app._resolve_cross_handoff`) reads the **markdown**
and injects its raw text verbatim into the implement/repair prompt — no
parsing. The JSON sidecar is written for audit but **never read back**.

This surfaced a real bug (fixed in `7300768`): the markdown echoed
`- project_path: <SOURCE>` into the runtime prompt, contradicting the
child's cwd (the isolated worktree) and its project-context block
("make task changes here, not in the source checkout"). Because the
runtime depends on free-form markdown as its *only* data channel, a
stray path line silently became a misleading instruction. There is no
schema, no validation, and no separation between "data the runtime
needs" and "prose for a human reader".

## Decision (proposed)

Make the **JSON the source of truth** for handoff data; render the
markdown *from* it as a human-readable / prompt artifact.

- `Handoff` (typed) → serialize to JSON (canonical) → render markdown
  from the typed object for the prompt + audit. The runtime consumes
  structured fields (or a render derived from them), not hand-authored
  markdown.
- Keep the markdown: it is useful for audit and as the rendered prompt
  body. It just stops being the *only* / authoritative channel.
- Path fields the runtime must not be misled by (e.g. source
  `project_path`) are either omitted from the prompt render or rendered
  as the worktree (consistent with the child cwd + context block).
- Validate the structured handoff on write so a missing/contradictory
  field fails at the cross level, not silently inside a prompt.

## Scope / non-goals

- Does NOT tackle the broader cross↔mono duplication — see
  [ADR 0051](0051-shared-runtime-path-context-layer.md).
- Keep the change additive to the wire format; the JSON shape is the
  contract, the markdown is a derived view.

## Open questions

- Which handoff fields are genuinely runtime-consumed vs prose-only?
- Should the child render the prompt body itself (from JSON) so the
  worktree path is always the child's real cwd, removing any predicted
  path from the cross writer?

## Resolution

Implemented in `pipeline/cross_project/handoff.py` +
`pipeline/project/app.py` + `pipeline/cross_project/project_dispatch.py`:

- The JSON is canonical. `write_handoff` now validates the typed
  `Handoff` (`validate_handoff`) and returns the **JSON** path; the
  `.md` is rendered from the same object (`render_handoff_markdown`) as
  an audit/human view only.
- The child resolves the handoff by loading + re-validating the JSON
  (`load_handoff`) and rendering the prompt body from the typed object —
  it no longer reads a free-form markdown blob verbatim.
- Open question #1: the runtime-consumed (required, non-empty) fields are
  `parent_run_id`, `profile`, `alias`, `project_subtask`,
  `full_cross_plan_markdown`. `project_path` is audit-only and never
  rendered.
- Open question #2: yes — the child renders the body itself, so no
  predicted worktree path crosses the writer/child boundary. The only
  path the runtime sees is the child worktree, from the child's own
  project-context block. A leak guard in `validate_handoff` fails the
  write if the source `project_path` ever appears in the rendered body
  (direct regression guard for the `7300768` path-leak bug).

## Addendum — leak guard is now a near-vacuous tripwire (ADR 0054, 2026-05-29)

[ADR 0054](0054-typed-cross-plan-json.md) makes the entire cross plan a typed
JSON object rendered into the handoff from typed fields end-to-end. There is no
free-form prose blob left for a source path to ride in, so the
`validate_handoff` leak guard stops being load-bearing. It is kept as a cheap
regression tripwire for the `7300768` path-leak bug.
