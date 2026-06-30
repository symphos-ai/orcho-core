# ADR 0110 — Scope-expansion notice: classify out-of-plan files as notice / risk / blocker

Status: Accepted

## Overview

`final_acceptance` now classifies every file a run changed **outside its
declared plan scope** into one of three durable statuses — `notice`, `risk`, or
`blocker` — each carrying per-file evidence. The default for a small, verified,
explained companion edit (a regenerated lockfile, a refreshed fixture/snapshot)
is **`notice`**: surfaced, never an auto-reject. Only the `blocker` tier forces
a REJECTED verdict, through the same handler-owned backstop pattern the required
delivery receipts use ([ADR 0090](0090-require-gate-no-silent-green.md)).
`notice` and `risk` change nothing about the verdict.

The classifier is **pure** and deterministic (`pipeline/engine/scope_expansion.py`,
no git / filesystem / model I/O). The `final_acceptance` handler gathers the
signals from durable run artefacts, calls the classifier, renders the assessment
into the readiness prompt, runs the blocker backstop, and writes the assessment
to a single canonical durable path. The DONE/Evidence summary and any future MCP
surface read **only** that durable projection — never terminal-only text.

This is additive and read-only over the existing verification contract: it
introduces **no** new gate primitive, mode flag, or wire schema, and it does
**not** weaken the authority of the verification gates. A genuinely required
verification gate that is missing/failed/stale still blocks via ADR 0090
exactly as before; scope expansion sits beside it, not over it.

## Context

A run is given an explicit plan scope: `ParsedPlan.owned_files` /
`allowed_modifications` (plan- and subtask-level) plus the project-level
`PluginConfig.allowed_modifications`. In practice an honest implementation often
touches a few files *just outside* that declared set — it regenerates a build
lockfile, refreshes a golden snapshot, reconciles an already-exported public
dataclass back to its `frozen=True, slots=True` invariant. Two failure modes
bracket this:

1. **Over-blocking.** Treating every out-of-plan byte as a scope violation turns
   benign, verified companion edits into spurious REJECTED verdicts and erodes
   trust in the closing gate.
2. **Silent approval.** Treating out-of-plan changes as invisible lets a
   genuinely dangerous edit — a public wire/schema change with no paired
   alignment, a persistence/state file, a security/secret file, a destructive
   mass-deletion — ship under a green acceptance.

The resolution is a **graded, evidence-backed** classification with a
conservative floor: a benign change with a green *relevant* gate and an
explanation is a `notice`; anything dangerous or unproven escalates to `risk` or
`blocker`; the absence of a proof never upgrades a file toward `notice`.

## Decision

### Status matrix

`classify_file_signals` maps one file's `FileScopeSignals` to a status,
evaluated in this order (first match wins):

1. **SDK-reconciliation exception.** When the file is a `generated_schema` or
   `public_wire` change that restores an already-public invariant —
   `sdk_already_public ∧ sdk_no_new_exports ∧ sdk_restores_invariant` — and is
   not itself large / destructive / repeated, it is a `notice` (when the
   relevant gate is green) or a `risk` (when it is not). It is **never** a
   blocker and is **never** "deleted blindly". This is the SDK schema/snapshot
   reconciliation case: re-asserting `frozen=True, slots=True` on a dataclass
   that is already exported, under a green snapshot-guard, adds no field and no
   export.
2. **Blocker conditions (hard).** Any of: `is_persistence`, `is_security`,
   `destructive_delete`, `large_diff`, `repeated_across_corrections`, or a
   `public_wire` change **without** `paired_alignment`. These are blockers
   regardless of category or verification.
3. **Notice.** A *benign* category (`build`, `fixture_snapshot`,
   `generated_schema`, `import_wiring`, `project_config`) that is `verified`
   **and** `has_explanation` **and** neither large nor destructive.
4. **Risk (conservative floor).** Everything else — a benign file that is
   unverified or unexplained, a `public_wire` change that is aligned but not a
   reconciliation, an `other`-category out-of-plan file. The floor never
   silently approves: an out-of-plan file the engine cannot clear to `notice`
   stays at least `risk`.

**Notice → risk downgrade.** A file that would otherwise be a `notice` but whose
**relevant gate is not green** (`verified=False`) is downgraded to at least
`risk`. Verification is the discriminator, not the optimistic default.

### Blocker conditions (why each blocks)

| Condition | Category/signal | Rationale |
| --- | --- | --- |
| Unaligned public wire/schema | `public_wire` ∧ ¬`paired_alignment` | A public contract change with no paired test/doc/schema update is an unreviewed wire change. |
| Persistence / state | `persistence` | Storage, `*state*.py`, migrations are durable-state changes that must be in plan. |
| Security / secret | `security` | `*secret*` / `*auth*` / `*credential*` files are never benign companion edits. |
| Destructive delete | `destructive_delete` | A wholesale file delete (or mass removal) out of plan is not a companion edit. |
| Large diff | `large_diff` | A large out-of-plan diff is a scope change, not a satellite touch. |
| Repeated across corrections | `repeated_across_corrections` | A file that keeps reappearing across correction rounds is an unstable, unplanned edit. |

### The invariant: verification gates stay authoritative

Scope expansion is **not** a replacement for the verification contract and does
not soften it. The required-receipt backstop (ADR 0090) still independently
forces REJECTED for any missing/failed/stale **required** delivery gate. The
scope-expansion `blocker` backstop is a *parallel* engine gap source merged into
the same `verification_gaps` list; it adds rejection reasons, it never removes
them. Both backstops are inert under dry-run, without a declared contract, and
when an operator waiver (`continue_with_waiver`) is active — the waiver is the
explicit human decision both backstops respect.

### Per-file `verified` is category → relevant gate

`verified` is **per-file / per-category**, never a single coarse global
`gates_green` flag. Each category is bound to the verification commands that
substantiate it, and a category is marked green only when at least one of its
relevant required receipts is **present** and none of its relevant receipts is
missing/failed/stale:

| Category | Relevant required gate (by command keyword) |
| --- | --- |
| `fixture_snapshot` | test gate (`pytest` / `test`) |
| `generated_schema`, `public_wire` | schema / snapshot-guard |
| `build`, `import_wiring` | lint / build (`ruff` / `lint` / `mypy` / `build` / `compile` / `make`) |
| `persistence`, `security`, `other` | no benign binding — never cleared to notice |

An unknown binding is conservatively **not** green. A `notice` without a green
relevant gate for that file's category is downgraded to at least `risk` (above).

### Signals and their durable sources

The handler facade (`pipeline/phases/builtin/scope_expansion_support.py`)
derives each signal from a **named durable artefact**; every source degrades
softly, and an unobserved signal stays conservatively `False` (never an upgrade
toward `notice`):

| Signal | Durable source |
| --- | --- |
| `changed_files` / `changed_file_set` | `git_changed_files(<agent project dir>)` (working-tree status) |
| `large_diff`, `destructive_delete` | per-file `git diff` numstat, parsed via `pipeline/engine/run_diff.py` (`parse_unified_diff` / `file_stats`); thresholds are module constants in `scope_expansion.py` |
| `in_plan_patterns` | `derive_in_plan_patterns(load_parsed_plan_artifact(output_dir)` ∥ `state.parsed_plan, PluginConfig.allowed_modifications)` |
| `verified` (per-category gate) | `build_final_acceptance_readiness` required-receipt partition (present vs missing/failed/stale), mapped per category |
| `has_explanation` | implement evidence — `implementation_receipts.declared_files` + the implement `output` / `attestation_summary` |
| `repeated_across_corrections` | `session['correction_fixed_point'].repeated` / `phase_log['correction_triage']` (soft) |
| `paired_alignment` | a related test / doc / schema sibling for a `public_wire` file present in `changed_file_set` |
| `sdk_already_public` / `sdk_no_new_exports` / `sdk_restores_invariant` | conservative `git diff` analysis (green snapshot-guard + frozen/slots restoration + no new top-level export/field); unprovable → all `False` |

### Canonical durable evidence shape

There is exactly **one** source of truth and exactly **one** read path. No
diverging write/read keys are permitted.

- **Source of truth (write):** the `final_acceptance` handler writes
  `assessment.to_dict()` to
  `state.phase_log['final_acceptance']['scope_expansion']` — and only when there
  are out-of-plan items, so an ordinary in-scope run keeps a byte-identical
  phase-log entry.
- **Session projection (read):** the `FinalAcceptanceAdapter` projects that key
  verbatim to `session['phases']['final_acceptance']['scope_expansion']`. The
  DONE/Evidence summary (`pipeline/project/finalization.py`) reads **only** this
  session path and renders it through the pure
  `scope_expansion.render_scope_expansion_lines`.

The JSON-safe shape (`ScopeExpansionAssessment.to_dict()`):

```json
{
  "items": [
    {
      "path": "package-lock.json",
      "category": "build",
      "status": "scope_expansion_notice",
      "evidence": ["verified", "explained"]
    }
  ],
  "has_blocker": false,
  "counts": { "notice": 1, "risk": 0, "blocker": 0 }
}
```

- **Per-item fields:** `path`, `category`, `status`, `evidence` (list of
  strings).
- **`status` values:** `scope_expansion_notice` / `scope_expansion_risk` /
  `scope_expansion_blocker`.
- **`category` values:** `build`, `fixture_snapshot`, `generated_schema`,
  `import_wiring`, `project_config`, `public_wire`, `persistence`, `security`,
  `other`.
- **Aggregates:** `has_blocker` (bool) and `counts` (`notice` / `risk` /
  `blocker`), plus the typed `notices` / `risks` / `blockers` projections on the
  in-memory `ScopeExpansionAssessment`.

## MCP follow-up contract

The orcho-core surface is the durable source of truth; aligning the MCP
transport is the **next stage**. This run makes **no** edits to `orcho-mcp`
(separate git history; orcho-core must not depend on orcho-mcp, and cross-repo
commits are forbidden). The companion task file
`orcho-mcp/.orcho/.orcho-tasks/mcp-flagship-stage-correction-evidence-projection.md`
is named here only as an orientation pointer for that follow-up — it is not
edited or executed from this worktree.

The follow-up contract answers four questions, all against the **same** durable
shape this ADR specifies (the shape the handler writes and the DONE summary
reads):

1. **Canonical source of truth.** Durable run metadata at the single path
   `phase_log['final_acceptance']['scope_expansion']`, projected to
   `session['phases']['final_acceptance']['scope_expansion']`. MCP reads the
   durable projection, **not** terminal-only DONE text.
2. **Form.** A typed, JSON-safe evidence field — the exact
   `ScopeExpansionAssessment.to_dict()` shape above (`items[]` of
   `{path, category, status, evidence}` plus `has_blocker` and `counts`). No new
   schema is invented for MCP; it forwards the field verbatim.
3. **Fields MCP exposes to the captain agent.** Per item: `path`, `category`,
   `status`, `evidence`; plus the aggregates `has_blocker` and `counts`
   (`notice` / `risk` / `blocker`). This lets the captain see *why* a file was
   classified, not just that scope expanded.
4. **Which MCP surface carries it first.** `orcho_run_evidence` first (it already
   projects the per-phase `final_acceptance` evidence block, the natural home for
   an additive `scope_expansion` key), then `orcho_run_status` if a compact
   ship-readiness chip is wanted there too. Both are additive pass-throughs of
   the open evidence shape — no field rename, no enum-constrained break — so the
   core change ships correctly *before* the companion lands.

## Consequences

- A small, verified, explained out-of-plan companion edit reads as
  `scope_expansion_notice` and ships; it no longer risks a spurious REJECTED.
- An unaligned public wire/schema, persistence/state, security/secret,
  destructive/mass-delete, large, or repeated out-of-plan change is a
  `scope_expansion_blocker` and forces REJECTED via the ADR 0090-style backstop.
- An SDK schema/snapshot reconciliation of an already-public invariant under a
  green guard reads `notice` (or `risk` when unverified) with explicit evidence —
  never a blocker, never a blind delete.
- The verification gates keep their full authority; scope expansion only adds
  rejection reasons for the blocker tier and surfaces notice/risk.
- An ordinary in-scope diff is byte-identical: no readiness change, no
  `scope_expansion` phase-log key, no DONE line.
- No new command, mode flag, or wire schema is introduced. The MCP alignment is
  a tracked, additive follow-up over the same durable shape.

## Related

- [ADR 0082](0082-verification-contract-final-acceptance-readiness.md) — the
  read-only final-acceptance readiness summary scope expansion renders beside.
- [ADR 0090](0090-require-gate-no-silent-green.md) — the required-receipt
  engine backstop whose handler-owned forced-REJECTED pattern the scope-expansion
  blocker tier reuses.
- [ADR 0105](0105-mcp-followup.md) — precedent for a tracked, additive
  orcho-mcp follow-up that the core change does not depend on.
- `docs/architecture/verification_contract.md` — Stage 5 awareness and the
  scope-expansion read-only evidence projection.
