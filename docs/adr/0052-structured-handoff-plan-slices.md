# ADR 0052 — Structured handoff plan slices

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** project owner
- **Relates to:** [ADR 0050](0050-structured-cross-handoff.md)

## Context

ADR 0050 made the per-alias handoff JSON the source of truth and had the
child render its implement/repair prompt body from the typed `Handoff`
via `render_handoff_markdown`. That render embedded
`full_cross_plan_markdown` — the **entire** approved cross plan — verbatim
under a `## Full cross plan` section, in addition to the alias's own
`project_subtask`.

For a child implementer that is wasteful and noisy. Concretely, the `web`
implementer's prompt carried:

- its own subtask **twice** (once as `## Project subtask`, again inside
  the embedded full plan's `=== SUBTASK [web] ===` block);
- every **sibling's** subtask block (`=== SUBTASK [api] ===`), which the
  web implementer must not act on;
- planner-only sections (`## Sub-agents`, the cross-plan preamble) that
  carry no instruction for a single-project implement turn.

The shared context a child genuinely needs from the plan is narrow: the
`## Interface Contract` (the cross-alias contract it must conform to) and
the `## Implementation Order` (sequencing + dependencies between aliases).
ADR 0050 explicitly left structured slices as an open follow-up
("structured fields like `global_interface_contract`, `execution_order`
… remain deliberately omitted; the cross-plan markdown grammar does not
currently surface them").

That premise no longer holds: `pipeline/cross_project/plan_parser.py`
already parses the canonical three-section grammar
(`parse_cross_plan` → `interface_contract`, `implementation_order`,
per-alias `subtasks`). It was wired only into CLI preview rendering, not
the handoff.

## Decision

Surface the parsed slices on the handoff and render them in place of the
full-plan dump.

- Add two optional fields to `Handoff`: `interface_contract` and
  `implementation_order` (both default `""`).
- `project_dispatch` parses the approved plan **once** with
  `parse_cross_plan` (the two slices are identical for every alias; only
  `project_subtask` differs) and populates them on each child's handoff.
- `render_handoff_markdown` emits `## Interface contract` +
  `## Project subtask` + `## Implementation order` when either slice is
  present, dropping the full-plan dump.
- `full_cross_plan_markdown` stays in the typed object and JSON sidecar
  as a **fallback + audit** field, mirroring how `project_path` is
  retained for audit but kept out of the normal render.

## Fallback

`parse_cross_plan` returns `None` when the plan does not follow the
canonical three-section grammar — review-only projections, `lite`
profiles, mock runs, or planner drift. In that case both slices are empty
and the render falls back to the prior `## Full cross plan` dump, so
non-conformant plans still hand off intact. The fallback is keyed on the
slices being empty, not on a separate flag.

## Scope / non-goals

- Internal cross→child artifact only; not an MCP wire-format change, so
  no `orcho-mcp` schema update is required.
- The slices are best-effort context, **not** runtime-required: they are
  absent from `_REQUIRED_NONEMPTY`. `project_subtask` +
  `full_cross_plan_markdown` remain the required runtime-consumed fields
  from ADR 0050.
- The leak guard in `validate_handoff` is unchanged and still covers the
  new sections — they derive from the same plan markdown, so any source
  `project_path` echo would be caught in the rendered body exactly as
  before.

## Consequences

- The single-project implement/repair prompt drops the duplicated
  subtask, the sibling subtask blocks, and planner-only sections;
  remaining plan context is the interface contract + implementation order
  the child actually conforms to (roughly halves the handoff-contract
  token count on the demo two-alias run).
- `load_handoff` tolerates the two fields being absent from an on-disk
  JSON sidecar (`_OPTIONAL_ON_LOAD`), so a handoff written before this
  change still loads and renders via the fallback path.

## Addendum — subsumed by ADR 0054 (2026-05-29)

[ADR 0054](0054-typed-cross-plan-json.md) makes the cross architect emit a
typed JSON object, so `interface_contract` / `implementation_order` are
first-class schema fields read straight from the validated plan rather than
scraped from `##` headings. The heading-scrape this ADR built on is gone; the
typed-slice handoff wiring it introduced remains, now fed from the schema.
