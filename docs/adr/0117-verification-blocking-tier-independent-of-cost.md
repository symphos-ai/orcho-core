# ADR 0117 — Verification blocking-tier is independent of cost

- **Status:** Accepted
- **Date:** 2026-06-27
- **Deciders:** project owner
- **Relates to:** `pipeline/verification_selection.py` (`derive_effective_policy`),
  `pipeline/verification_contract.py` (the `cheap` / `default_cheap` field),
  `.orcho/multiagent/plugin.py` (gate declarations),
  [project_semantic_profiles_master] (SemanticProfile × OperatingMode → policy),
  [ADR 0114](0114-run-control-state-projection-and-classifier-ownership.md)
- **Supersedes:** nothing (append-only)

## Context

`cheap` currently does **double duty**. It is declared as cost metadata, but the
only code that consumes it — `verification_selection._effective_policy` — uses it
as the lever that **downgrades a gate's effective blocking policy** under work
modes:

```python
# fast: return "warn" if cheap else "suggest"
# pro:  return "require" if (required and cheap) else "warn"
```

So "is this gate cheap?" silently decides "does this gate block to ship?". Two
consequences:

1. An **expensive-but-critical** gate degrades to advisory. `broad-non-e2e` (the
   core correctness suite, ~4 min) is `require` in the contract but, being not
   `cheap`, becomes `warn` in pro — it does not actually block.
2. In practice **no** required gate sets `cheap`, so in pro **all** of them
   degrade to `warn`: the verification schedule has zero hard gates in pro;
   blocking comes only from the release verdict + scope/require-receipt backstops.
   (Observed live: run `20260627_164749`, mode=pro, `kind=unknown` on every
   require gate.)

The conflation is the bug: **cost and must-block are different axes.** A gate can
be expensive AND must-block (`broad-non-e2e`); or cheap and advisory.

## Decision

Separate the two axes:

1. **Blocking tier = the declared `policy`** (`require` / `warn` / `suggest`) —
   the deliberate author intent of how ship-blocking a gate is.
   `derive_effective_policy` keys on **tier × work-mode**, and **never on cost**.
   A `require` gate blocks per its tier regardless of how expensive it is. Its
   signature drops `cheap`: `derive_effective_policy(base_policy, work_mode, *,
   required) -> str`.

2. **Cost is orthogonal metadata.** `cheap` (or a richer cost level) is recorded
   for cost-aware decisions — auto-run/materialize scheduling, cost projection,
   display — and is **removed from the blocking computation entirely**. It is
   still declared on `commands[*].cheap` / `gate_sets[*].default_cheap` and read
   independently by `core/io/verification_header._gate_kind` for the cost column.

3. **Work-mode modulates tiers deliberately, not via cost.** The declared tier
   `T` is the explicit base policy when set, else `require` for a required
   command and `suggest` for an advisory one. The pinned mode × tier table (cost
   is not an input) — identical to the code and to
   `docs/architecture/verification_contract.md`:

   | Mode | `require` tier | `warn` tier | `suggest` tier |
   |---|---|---|---|
   | `fast` | `require` | `warn` | `off` |
   | `pro` | `require` | `warn` | `suggest` |
   | `governed` | `require` | `require` | `suggest` |
   | unset | `require` | `warn` | `suggest` |

   - `require` is honored in every mode (the must-block tier — mode-independent);
   - `governed` escalates `warn → require`; `require` and `suggest` are honored;
   - `fast` relaxes only the advisory `suggest → off` for speed; `warn` and
     `require` are honored;
   - `pro` and unset honor the declared tier as-is;
   - cost never enters this table.

The author's **tier choice** now encodes the cost trade-off explicitly:
`broad-non-e2e = require` (block despite cost) vs `e2e = suggest` (advisory
*because* too expensive to block every run) — instead of an automatic
cost-downgrade deciding it silently.

## Consequence

- `broad-non-e2e = require` (+ expensive metadata) **blocks in pro/fast** —
  correctness is honored. This restores it as the authoritative ship gate the
  verification single-source (`40dc5de`) assumed.
- `e2e = suggest` (+ expensive) stays advisory by **tier**, not by cost.
- The genuinely cheap unit gates stay `require` and block.
- **Tradeoff, stated honestly:** honoring `require` regardless of cost means a
  pro/fast run hard-blocks on the ~4-min broad suite. That is the deliberate cost
  of correctness; the lever to avoid it is the author choosing a **lower tier**
  for a gate, not an automatic cost-based downgrade. (Cost metadata still lets the
  engine be smart about *when/whether to auto-run* a gate vs rely on the
  implement-phase receipt — but that is a scheduling decision, not a
  blocking one.)

## Scope / non-goals

- Refactor `derive_effective_policy` to drop `cheap` from the blocking decision;
  keep cost as metadata consumed only by scheduling/projection/display.
- Update `.orcho/multiagent/plugin.py` gate declarations to set tier (`policy`)
  and cost explicitly per gate.
- Pin the mode × tier table (coordinate with the semantic-profiles projection).
- **Not** a new cost dashboard; cost stays a simple declared flag/level.
- **Not** the finalization reducer / participant set / session disposition.

## Guard tests
- A `require` gate that is not `cheap` still classifies as effective-`require`
  (blocking) in pro and fast — cost no longer downgrades it.
- A `suggest` gate stays advisory regardless of cost.
- Cost metadata is still readable for scheduling/projection but is absent from
  `derive_effective_policy`'s inputs (assert by signature/behavior).

## Related
- [project_semantic_profiles_master] — the mode × tier projection this slots into.
- [ADR 0114](0114-run-control-state-projection-and-classifier-ownership.md) —
  single-source ownership; this keeps "what blocks ship" owned by the declared
  schedule, now read on tier not cost.
