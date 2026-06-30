# ADR 0054 — Typed cross-plan JSON (the architect speaks JSON)

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** project owner
- **Relates to:** [ADR 0009](0009-composable-prompt-parts.md),
  [ADR 0050](0050-structured-cross-handoff.md),
  [ADR 0052](0052-structured-handoff-plan-slices.md),
  [ADR 0053](0053-contract-check-reads-diffs.md),
  [ADR 0055](0055-cross-planning-session-aware-delta.md),
  [ADR 0057](0057-cross-dispatch-honors-declared-dependencies.md)

## Context

Orcho's posture is "machine-consumed agent output is a typed contract,
not free-form prose." Mono honours this on **every** structured surface:

- Reviewer gates (`validate_plan`, `review_changes`, `final_acceptance`,
  hypothesis QA) emit `review_json` / `release_json`
  (`pipeline/prompts/contracts.py`).
- The **architect** emits a typed plan: `plan_json_contract`
  (contracts.py:195) forces JSON-only output against `PLAN_SCHEMA_DOC`
  and is attached unconditionally by the builder (builders.py:1051) for
  PLAN / REPLAN / DECOMPOSE. Output is validated into `parsed_plan`.

The **cross** pipeline honours it for its reviewer gates
(`cross_validate_plan` / `contract_check` → `review_json`,
`cross_final_acceptance` → `release_json`) but **not** for its
architect. `cross_plan` carries no output contract; instead it emits:

- human-readable markdown with three canonical headings
  (`## Interface Contract`, `## Per-Project Subtasks`,
  `## Implementation Order`), plus
- bespoke `=== SUBTASK [<alias>] === … === END ===` text markers, pinned
  by `cross_subtask_block_contract` (contracts.py:493) and scraped by a
  regex parser (`pipeline/cross_project/plan_parser.py`).

So the cross architect is the **single planning surface in Orcho that
does not emit a typed JSON plan**. This breaks parity with the mono
architect and is the structural root of a recent patch series:

- **ADR 0050** — free-form plan markdown was the *only* handoff data
  channel, which let a stray `project_path` line leak into the runtime
  prompt. Fixed by making the handoff JSON the source of truth, but the
  *upstream* plan is still untyped.
- **ADR 0052** — re-slices the plan by scraping `##` headings into
  `interface_contract` / `implementation_order`. That is heading-scraping
  *because the plan has no structured fields to read*.
- The `=== SUBTASK ===` grammar is a hand-rolled text protocol where a
  schema field (`subtasks[alias]`) belongs.

`ParsedCrossPlan` (plan_parser.py:38) already models the target shape
(`interface_contract`, `implementation_order`, `subtasks`,
`aliases_missing`) — the data model exists; only the *channel* is prose
+ markers instead of a validated JSON contract.

## Decision (proposed)

Give the cross architect a typed **`cross_plan_json_contract`**, exactly
as mono's architect has `plan_json_contract`. The agent emits one JSON
object validated against a `CrossPlanSchema`; the human-readable
`cross_plan.md` becomes a *derived render* of that object (consistent
with the ADR 0050 direction: JSON is the source of truth, markdown is an
audit/preview view).

Proposed schema (mirrors `PLAN_SCHEMA_DOC` + cross-specific fields):

```json
{
  "short_summary": "<= 280 chars, CLI/MCP headline",
  "interface_contract": "<shared producer/consumer contract: field names, types, payloads, persisted shapes, endpoints>",
  "implementation_order": ["<ordered step>", "..."],
  "subtasks": [
    {
      "alias": "<must match a supplied alias exactly>",
      "goal": "<one-sentence outcome for this repo>",
      "spec": "<detailed instructions for the child implementer>",
      "depends_on": ["<sibling alias that must land first>"],
      "files": ["[alias]/relative/path"],
      "produces": "<what this repo gives the others>",
      "consumes": "<what this repo takes from the others>"
    }
  ]
}
```

Validation at the cross level (fail-fast, parity with reviewer JSON
gates and ADR 0050's write-time validation):

- exactly one JSON object; malformed → hard reject (the contract is the
  gate, like reviewer JSON — no prose fallback);
- every supplied alias has exactly one non-empty `subtasks[]` entry;
  missing/extra/duplicate alias → reject (replaces the marker parser's
  soft `aliases_missing`);
- `interface_contract` non-empty when more than one alias is involved
  (a coordinated change must name its shared surface — this is what
  `cross_validate_plan` rejection rule #1 already checks, now enforceable
  structurally before the reviewer runs).

## Knock-on simplifications

- **Subtask routing** reads `plan.subtasks[alias]` directly;
  `extract_subtasks` + the `=== SUBTASK ===` grammar
  (`cross_subtask_block_contract`) are deleted.
- **ADR 0052 is subsumed.** `interface_contract` / `implementation_order`
  become first-class schema fields fed straight into the handoff
  (`Handoff.interface_contract` / `.implementation_order`), not scraped
  from `##` headings. The heading-scrape in `plan_parser.parse_cross_plan`
  is removed.
- **ADR 0050 leak guard becomes near-vacuous.** The handoff is built from
  typed fields end-to-end; there is no free-form blob for a path to ride
  in. Keep the guard as a cheap regression tripwire, but it stops being
  load-bearing.
- `ParsedCrossPlan` stays as the renderer-facing shape; it is now
  *constructed from validated JSON* rather than regex-scraped.

## Scope / non-goals

- Single-developer project, no production install base
  (`orcho-core/CLAUDE.md`): replace the marker grammar **in place**, no
  dual-path / `ORCHO_USE_*` flag. Mocks, scripted providers, and tests
  that emit `=== SUBTASK ===` switch to the JSON contract in the same
  change — the same cutover the reviewer-gate JSON migration already did.
- Does not touch the mono plan schema; it only brings cross to parity.
  A later ADR may unify `PlanSchema` and `CrossPlanSchema` if they
  converge, but this ADR keeps `CrossPlanSchema` separate (cross adds
  `interface_contract` / per-alias `produces`/`consumes`).
- Does not change reviewer-gate contracts.

## Consequences

- The cross architect now emits JSON. Mono already proves a capable
  architect plans well under a JSON contract, so this is a known-good
  pattern, not a gamble. The human-readable plan is preserved as a render
  of the JSON (CLI preview, `cross_plan.md` audit artifact).
- A new schema + parser + prompt contract; deletion of the marker
  grammar and the heading-scrape. Touches the cross plan builder,
  `plan_parser`, `project_dispatch` (routing), the handoff builder
  (ADR 0050/0052), profiles, and the cross plan/replan loop.
- **Wire-adjacent**: cross plan → subtask routing is part of the cross
  contract. Ships with an `orcho-mcp` E2E mock smoke in the same commit
  (`orcho-core/CLAUDE.md` MCP per-phase validation rule).
- `cross_validate_plan` keeps reviewing the rendered plan, but several of
  its rejection rules (missing alias coverage, missing shared surface)
  are now also enforced structurally on write — earlier, cheaper signal.

## Alternatives considered

- **Keep prose + markers, harden the parser.** Rejected: it entrenches
  the one untyped planning surface and leaves 0050/0052 as permanent
  scaffolding around free-form text.
- **JSON for subtask routing only, keep prose for the contract/order.**
  Rejected: half-typed. The interface contract and order are exactly the
  fields ADR 0052 already needs structured; splitting the channel keeps
  the heading-scrape alive.
- **Defer until the mono/cross plan schemas are unified.** Rejected:
  parity is achievable now with a separate `CrossPlanSchema`; unification
  is an independent, later cleanup.

## Migration sketch (for the implementing ADR phase)

1. `core/contracts/cross_plan_schema.py` — `CrossPlanSchema` +
   `CROSS_PLAN_SCHEMA_DOC` + validator (mirror `plan_schema.py`).
2. `pipeline/prompts/contracts.py` — `cross_plan_json_contract`; retire
   `cross_subtask_block_contract`.
3. Cross plan builder attaches the JSON contract (drop the markdown
   three-section task prose's machine-readable role).
4. `plan_parser.py` — parse/validate JSON → `ParsedCrossPlan`; delete
   `extract_subtasks` markers + heading-scrape.
5. `project_dispatch` reads `subtasks[alias]`; handoff builder reads
   typed fields (subsumes ADR 0052 wiring).
6. Render `cross_plan.md` + CLI preview from the typed object.
7. Update mock/scripted providers + tests to emit the JSON contract;
   add the `orcho-mcp` mock smoke.

## Implementation decisions (Accepted)

These refine the proposal above; they are the load-bearing invariants the
implementation settled on.

- **Canonical artifact, persisted latest-valid (not only on approval).**
  `cross_plan.json` = `json.dumps(validated.data)` (the normalized
  post-validation object the runtime consumes). The latest **schema-valid**
  normalized plan is persisted **before** reviewer QA and refreshed each valid
  round (latest-valid-wins), so a QA-rejected-but-schema-valid plan that the
  operator **continues** still has a valid cached plan to dispatch. Raw agent
  text lives only in the round trace. Resume reloads `cross_plan.json`, never
  the `.md`.
- **Continue is gated on the CURRENT round's validity, not on cached JSON
  existing.** `can_continue = (round_n in parsed_by_round)` — the paused round
  itself parsed schema-valid. NOT "does any `cross_plan.json` exist": after a
  round-1-valid-but-rejected → round-2-schema-invalid sequence,
  `cross_plan.json` still holds round 1, but continuing would dispatch an OLDER
  plan than the one just rejected. A schema-invalid pause narrows
  `available_actions` to `[retry_feedback, halt]` via `can_continue=False` in
  `build_cross_plan_handoff_payload`; the resume-continue path re-validates
  `cross_plan.json` before dispatch and refuses if absent/invalid.
- **Parse failure routes through `_validate`, never raises out of the loop.**
  `_produce` parses → stashes `parsed_by_round[n]` (normalized) or
  `parse_error_by_round[n]`, writes raw to the trace, and returns the rendered
  markdown (valid) or raw output (invalid). `_validate` checks the parse error
  first → synthetic `ReviewOutcome(approved=False, critique=<schema error + a
  JSON-schema reminder>)` WITHOUT calling the reviewer; on a clean parse the
  reviewer sees the rendered markdown.
- **Pinned round-trace shape.** Each cross_plan round entry always carries
  `raw_output: str`, `normalized_plan: dict | None` (None on invalid),
  `rendered_markdown: str` (`""` on invalid), `parse_error: str | None` (None on
  valid), `parse_warnings: list[str]` (`[]` when clean). The shared control
  `ReviewedRound.output` is `rendered_markdown` on a valid parse and `raw_output`
  on an invalid parse.
- **Typed `depends_on` edges (data in 0054; ordering in 0057).** Per-subtask
  `depends_on: [alias]` is validated (closed-ref, no self-edge, acyclic via Kahn,
  mirroring mono `validate_dag`) and exposed on `ParsedCrossPlan.dependencies` +
  `cross_plan.json`. A cycle or dangling ref is **invalid plan data** → synthetic
  reject in 0054. `implementation_order` + `produces`/`consumes` stay descriptive
  only; ADR 0057 consumes `dependencies` for the dispatch topo-sort. `depends_on`
  is NOT added to `Handoff` (a child acts on one alias).
- **`interface_contract` key always present; value may be `""` only for a
  single-alias cross.** Non-empty is enforced iff `len(aliases) > 1`.
- **`--plan-file` is JSON-only; `--mode plan` emits both artifacts.** Plan mode
  writes `cross_plan.json` (the editable canonical artifact) and `cross_plan.md`
  (read-only audit render); human edits, `--plan-file`, and resume target the
  JSON. Markdown plan files are rejected.
- **Aliasize the normalized object, then derive every render from it.**
  `aliasize_cross_plan` rewrites absolute project roots to `[alias]/` form
  across **every** string field of the validated object (`interface_contract`,
  `implementation_order`, per-subtask `goal`/`spec`/`produces`/`consumes`/
  `files`) before persisting. So the canonical `cross_plan.json` is itself
  leak-clean — not just the rendered markdown — and `cross_plan.md`, the
  cross-validate reviewer artifact, the dispatch handoff's
  `full_cross_plan_markdown`, and the round-trace `normalized_plan` are all
  derived from (and byte-consistent with) that one aliasized object.
  `write_cross_plan_artifacts` is the single persist site; the rendered
  document (`# Cross-Project Plan` + task + `render_cross_plan_markdown`,
  subtasks ordered by supplied aliases) is what the reviewer sees and what
  lands on disk. (Supersedes the earlier "JSON stored verbatim, aliasize only
  the rendered text" sketch — verbatim left an absolute path in the canonical
  artifact when the architect ignored the alias-form instruction.)
- **Parser warnings non-fatal.** `parse_json_contract_object` recovers one
  schema-valid object from stray prose (recording `parse_warnings`); only
  unrecoverable / schema- / cycle-invalid output → synthetic reject.
- **Mirror/evidence.** `cross_plan.json` is registered alongside `cross_plan.md`
  in the mirror patterns (`core/_config/config.defaults.json` +
  `core/infra/config.py`).

## Knock-on ADR notes

- **ADR 0052 is subsumed:** `interface_contract` / `implementation_order` are
  first-class schema fields, not heading-scrapes.
- **ADR 0050 leak guard is now a near-vacuous tripwire:** the handoff is built
  from typed fields end-to-end; there is no free-form blob for a path to ride in.
- **ADR 0057 consumes the typed `depends_on` edges** this ADR adds; 0054 owns the
  data + acyclicity, 0057 owns the dispatch topo-sort.
