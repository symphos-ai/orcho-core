# ADR 0087 — `allowed_modifications`: project- and task-scoped companion changes for review gates

- Status: Accepted
- Date: 2026-06-12
- Relates to: ADR 0009 (composable prompt parts / prompt-boundary
  discipline), ADR 0025 (review / release JSON gate contract), ADR 0028
  (cache-first prompt assembly), ADR 0083/0084 (verification-contract
  delivery-gate awareness)
- Sibling but independent: ADR 0085 (`allowed_scope` in
  `correction_triage`). See "Relationship to ADR 0085" below — the two
  mechanisms are deliberately **not** merged.

## Context

The plan contract already carries `owned_files` — the write scope a task
is allowed to touch. The review gates (`review_changes`,
`final_acceptance`) and `validate_plan` treat that scope as binary: a diff
that touches a path outside `owned_files` reads as a scope violation and
is flagged as a finding (or, at `final_acceptance`, blocks release).

That binary rule produces measured false rejections for a recurring class
of **derived, non-authored** files that change as a mechanical consequence
of an in-scope edit, not as independent work:

- **Lockfiles** — editing `package.json` regenerates `package-lock.json` /
  `yarn.lock`; the lockfile churn is derived, not a scope expansion.
- **Golden snapshots / hashes** — a deliberate behavior change regenerates
  a golden-hash or snapshot fixture; the new bytes are an *expected*
  consequence the reviewer should read, not reject.
- **Verify-report artifacts** — a verification run writes a
  `verify-report.json` (or similar) into the tree; the report is output,
  not authored scope.

A second, sharper symptom: the `review_changes` gate and
`final_acceptance` **disagreed** whenever an operator widened scope to let
such a companion change through. One gate would accept the diff (operator
context in the loop) while the closing gate re-flagged it from a clean
read of `owned_files`, because there was no single durable source that both
gates read for "these companion changes are allowed". The allowance lived
in operator memory, not in the contract.

What was missing is a declared, durable list of companion files whose
modification is **not** a scope violation in any task — read identically by
every review surface, including the typed `validate_plan` path.

## Decision

Introduce `allowed_modifications` as an additive, informational allowance
read by all review gates from one source.

1. **Project-level list (`PluginConfig.allowed_modifications`).** A flat
   `list[str]` of `"glob — reason"` entries declared in the project's
   `plugin.py`. `load_plugin` normalises it like the neighbouring fields:
   a non-list value is dropped, non-string entries are filtered, both with
   a yellow warning; `load_plugin` never raises. It renders through a
   code-owned module, `pipeline.allowed_modifications`, into a
   `## Allowed Companion Modifications` block injected as a typed
   `PromptPart` (`kind="allowed_modifications"`, `source="code-owned"`,
   `stability=STATIC`, `cache_scope=PROJECT`) into **every** review
   surface: `review_focus` (which backs both `review_changes` and
   `final_acceptance`), the diff-only `plan_review_focus` fallback, the
   uncommitted `review_changes` wire, **and the typed `validate_plan` path
   `plan_file_review_prompt`** — the path that was twice missed in review
   and is explicitly covered here.

2. **Per-task and plan-level contract field.** The plan contract gains an
   optional `allowed_modifications` at both the plan level and per
   subtask, mirroring `owned_files` mechanics end to end: schema
   validation as `list[str]` (bare strings and mixed types rejected, never
   coerced), round-trip through `pipeline.plan_artifacts`, and the
   canonical renderer `pipeline.plan_contract.render_plan_contract`
   aggregates both into one `**Allowed companion modifications:**` section
   inside `## Plan Contract`. Per-task entries are tagged `[<task-id>]` so
   a reviewer reading a single Plan Contract block can attribute each
   companion change to its task. `ParsedPlan.has_contract` accounts for
   the field at both levels, so a plan that declares only companion
   modifications still renders the contract block. Because the gates
   already read `render_plan_contract` output, **all gates read one
   source**.

3. **Semantics: scope objection only.** The allowance removes *only* the
   scope objection. The **content** of a companion change is still
   reviewed by the usual quality criteria, and any diff outside
   `owned_files` **plus** this list remains a scope violation and is
   rejected as before. The review verdict contract (ADR 0025,
   `review_json_contract`, APPROVED/REJECTED) is unchanged; scope
   violations stay findings. This semantics text is owned by the code
   renderer (`pipeline.allowed_modifications`), not by any
   `core/_prompts` part (ADR 0009 prompt-boundary discipline).

## Deliberate non-goals

Held back until a real abuse case appears, confirmed with the operator:

- **No trigger conditions.** The list is unconditional context; there is
  no "applies when …" predicate.
- **No expected-shape description.** Entries are free-text `"glob —
  reason"`; the contract does not parse or constrain the change's shape.
- **No auto-classification.** Core does not infer which files are
  lockfiles / snapshots / reports; the human declares them.
- **No code-side glob matching or diff enforcement.** There is **no**
  mechanical scope gate in core. The feature is purely informational for
  the review agents — they reason about the list; core never matches a
  diff path against it.

## Wire-format boundary and MCP alignment

The AGENTS.md MCP-validation rule fires for **wire-format** changes (runtime
schemas, profile shape, mode flags, gate primitives). This change splits
cleanly across that boundary:

- **`PluginConfig.allowed_modifications` is not wire.** It is project
  config consumed locally to render a prompt block; no MCP surface carries
  it.
- **The plan-schema extension is wire**, but additive and optional. The
  `orcho_plan_validate` MCP tool owns **no** copy of the plan schema:
  `validate_plan_document` (its sync backing) delegates to core
  `pipeline.plan_parser.parse_plan` and lazily imports
  `core.contracts.plan_schema` at
  `orcho-mcp/src/orcho_mcp/authoring/plan_validation.py:53`. Its
  `SubTaskRecord` projection
  (`orcho-mcp/src/orcho_mcp/schemas/authoring.py`) lists only
  `id / goal / spec / files / skill / model / depends_on / done_criteria`
  — it does **not** surface per-task `owned_files` today, and therefore
  does not surface per-task `allowed_modifications` either. Because the new
  field mirrors `owned_files` exactly (schema-validated, dropped from the
  fresh-parse per-task projection), parity with `owned_files` means **zero
  edits to orcho-mcp**. No MCP surface re-declares or rejects the new key,
  so the stop condition for an independent orcho-mcp update did not fire.

The smoke `tests/unit/pipeline/test_plan_schema_mcp_surface.py` pins this
binding: it runs plan markdown carrying `allowed_modifications` at both
levels through exactly `parse_plan` + `core.contracts.plan_schema` — the
two calls `validate_plan_document` makes — asserting acceptance, no-field
parity, and `PlanSchemaError` on a malformed type.

### Deferred-evidence run result (T4a handoff)

- Read-only, as-is: `cd /path/to/orcho/orcho-mcp &&
  python -m pytest tests -q -k plan_valid` → **9 passed, 778 deselected**
  (EXIT 0). Caveat: orcho-mcp resolved `core.contracts.plan_schema` against
  the stable install (`~/.local/share/orcho-core/...`), which did not
  yet carry the new field — so this run proves orcho-mcp plan validation is
  healthy against the published core.
- Airtight cross-check against this worktree's core: forcing the worktree
  onto `PYTHONPATH`, `core.contracts.plan_schema.__file__` resolved to the
  worktree copy (new field present) and
  `validate_plan_document(markdown=…both-level allowed_modifications…)`
  returned **`ok=True`, error=None**, subtask `t1` projected — proving the
  new key flows through `orcho_plan_validate` without rejection.
- Re-running the full `-k plan_valid` suite with that forced `PYTHONPATH`
  failed at conftest import (`ModuleNotFoundError:
  tests.fixtures.mcp_workspace`) because the worktree's top-level `tests/`
  package shadows orcho-mcp's `tests/` package during collection — a
  test-discovery artifact, not a validation failure. The cross-check above
  already establishes the behavioural result.

## Relationship to ADR 0085

ADR 0085 introduced `allowed_scope` in the `correction_triage` phase — the
narrow set of files/areas a *correction follow-up* may touch when closing a
listed rejection blocker. That is a **per-correction, run-scoped routing
constraint** consumed by the triage/route machinery. `allowed_modifications`
is a **standing, review-informational allowance** for derived companion
files across every task of a project. They share a surface vocabulary
("what may change beyond the obvious scope") but differ in lifetime,
consumer, and effect. They are kept as separate mechanisms; merging them
would overload one field with two unrelated lifecycles.

## Consequences

- Lockfile / golden-hash / verify-report churn declared once no longer
  reads as a scope violation at any gate, while its content is still
  reviewed — the measured false-rejection class is closed without weakening
  the review contract.
- `review_changes` and `final_acceptance` read the same declared allowance,
  so an operator-accepted companion change is no longer re-flagged by the
  closing gate.
- Plans can scope an allowance to a single task (`[<task-id>]` in the Plan
  Contract) when the companion change belongs to one slice, not the whole
  project.
- When neither level is set, every prompt is byte-identical to before:
  the project block is suppressed (empty list → no `PromptPart`), the
  contract section is omitted, and cache invariants
  (`test_wire_cache_layout.py`, `test_prefix_leak_guard.py`) hold without
  weakened assertions.
- There is no mechanical enforcement: a malicious or careless diff is not
  blocked by code on the strength of this list. That is intentional for
  now; a code-side glob gate is deferred until a real abuse case justifies
  the complexity.
