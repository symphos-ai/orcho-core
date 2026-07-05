# Operating Modes (`fast` / `pro` / `governed`)

> Reference — the run's **strictness posture**: how a work mode rewrites
> verification-gate blocking policy and gate-failure routing. Pairs with a
> `SemanticProfile` (work kind) to form the run's `RunShape`.
> Source of truth: `pipeline/verification_selection.py`
> (`derive_effective_policy` / `derive_effective_action`),
> `pipeline/runtime/run_shape.py` (`OperatingMode`), ADR 0064, ADR 0117.

A run carries two orthogonal axes:

- **Work kind** (`SemanticProfile`) — *what kind of work* (`feature`,
  `small_task`, `migration`, …). Selects the pipeline recipe.
- **Operating mode** (`OperatingMode`) — *how strict* the run is about its
  verification gates. Three closed members: `fast`, `pro`, `governed`
  (`""` = unset, treated as "honor declared").

The mode does **not** change which gates exist or which commands run — it only
rewrites (a) each gate's **blocking tier** and (b) the **action taken when a
gate fails**. Everything below is a pure, deterministic projection with no LLM
in the loop and no cost input (see [Cost independence](#cost-independence)).

Set it with `--mode {fast,pro,governed}` on `orcho run` / `orcho cross`. Omit
it to use the work kind's [default](#defaults-per-work-kind).

---

## Qualitative matrix — what each mode does

Two fixed tables drive everything. A gate's **declared tier** is one of
`suggest` (advisory) / `warn` / `require` (blocking); the mode rewrites it.

### Blocking-tier rewrite (`derive_effective_policy`)

| Declared tier | `fast` | `pro` | `governed` |
|---|---|---|---|
| `suggest` | **→ `off`** (dropped for speed) | `suggest` | `suggest` |
| `warn` | `warn` | `warn` | **→ `require`** (escalated to blocking) |
| `require` | `require` | `require` | `require` |

- **`fast`** relaxes *only* the advisory `suggest` tier to `off`; `warn` and
  `require` are honored as declared.
- **`pro`** honors every declared tier exactly — no rewrite.
- **`governed`** escalates `warn` → `require`; `require`/`suggest` unchanged.

### Gate-failure action rewrite (`derive_effective_action`)

| Situation | `fast` (& unset) | `pro` | `governed` |
|---|---|---|---|
| Failed require after `implement` | `continue_warn` | **`repair_loop`** | **`repair_loop`** |
| Failed gate at delivery (`before_delivery`) | `continue_warn` | **`handoff`** | **`handoff`** |
| Everything else | `continue_warn` | `continue_warn` | `continue_warn` |

So `pro` and `governed` share the same *action* routing (auto-repair after
implement; human handoff at delivery); they differ only in the **tier** table
above — `governed` additionally turns every `warn` into a hard `require`.

---

## Quantitative expectation — relative cost

The mode has no direct token knob; cost moves through **how many gates block
and how many repair/handoff loops they trigger**. Monotonic, not absolute:

| | `fast` | `pro` | `governed` |
|---|---|---|---|
| Advisory (`suggest`) gates run | dropped (`off`) | run, non-blocking | run, non-blocking |
| `warn` gates block delivery | no | no | **yes** |
| Auto repair-loop after implement | no | yes | yes |
| Human handoff at delivery on failure | no | yes | yes |
| Relative pauses / rounds / spend | **lowest** | medium | **highest** |

`fast` is the cheapest and least-interrupting; `governed` is the most
blocking (every `warn` becomes a gate that can hold delivery for a human
decision).

---

## When to use each

- **`fast`** — low blast radius, reversible work: prototypes, throwaway
  spikes, docs, a change you'll eyeball yourself. You accept that advisory
  checks are skipped and a failed non-critical gate won't stop the run.
- **`pro`** — production work you intend to ship: every declared gate is
  honored, a failed post-implement `require` auto-routes into repair, and a
  delivery-time failure pauses for you. The default for shipped, non-trivial
  changes.
- **`governed`** — high cost-of-error or irreversible/outward-facing work:
  anything touching money, migrations you can't easily roll back, or where a
  *warning* must be treated as a hard stop rather than a note. Escalating
  `warn → require` means the run cannot quietly deliver over a soft failure.
  **Never a default — always an explicit opt-in.**

Rule of thumb: pick the work kind for *what* you're doing, then raise the mode
above its default only when the cost of a silent miss is high enough to justify
the extra blocking and handoffs.

---

## Defaults per work kind

When `--mode` is omitted, the work kind projects a default posture
(`pipeline/runtime/semantic_mode_defaults.py`, `default_operating_mode`):

| Work kind | Default mode |
|---|---|
| `small_task` | `fast` |
| `feature` | `fast` |
| `research` | `fast` |
| `complex_feature` | `pro` |
| `planning` | `pro` |
| `code_review` | `pro` |
| `delivery_audit` | `pro` |
| `refactor` | `pro` |
| `migration` | `pro` |

`governed` is intentionally absent from this table — it is an explicit opt-in
posture, never selected by a work kind on its own.

> **Provenance note.** The mode vocabulary began in ADR 0064 as
> `fast` / `team` / `governed`; `team` was later renamed `pro`
> (`OperatingMode('team')` now raises `ValueError`). ADR 0064's illustrative
> default posture paired `feature + team` (i.e. `feature + pro`); the shipped
> default table above (the "Stage C product decision" in
> `semantic_mode_defaults.py`) maps `feature → fast`. **The shipped table is
> the source of truth for runtime behavior.** If the intended default is
> `pro`, that is a table change + ADR-0064 reconciliation, not a doc change.

---

## Cost independence

Mode never trades correctness for cost. A `require` gate blocks even when it is
expensive; `fast` drops only the advisory `suggest` tier, never `require` or
`warn`→delivery semantics beyond the tables above (ADR 0117 —
*verification blocking tier is independent of cost*). There is no mode that
turns a required gate off to save money.

---

## See also

- [Profile JSON Schema](profile_schema.md) — work-kind recipes and the
  `default_mode` identity field.
- [Built-in gates](builtin_gates.md) — the gates whose tiers modes rewrite.
- [Resume modes](resume_modes.md) — orthogonal axis: session/resume semantics.
- ADR 0064 (`semantic-profiles-and-operating-modes`) — the two-axis design.
- ADR 0117 (`verification-blocking-tier-independent-of-cost`) — why cost is
  never an input to blocking.
