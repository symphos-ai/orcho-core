# Semantic Profiles — Current-State Alignment

> Alignment note for the semantic work-kind surface. It maps the accepted
> target architecture in
> [ADR 0064](../adr/0064-semantic-profiles-and-operating-modes.md) onto what
> the runtime ships today after the Stage C cutover, so reviewers and authors
> can tell the live semantic surface apart from the still-deferred resolver
> work without re-reading the whole ADR.

## Status

[ADR 0064](../adr/0064-semantic-profiles-and-operating-modes.md) remains the
**accepted target**: two orthogonal axes (`SemanticProfile` × `OperatingMode`)
that a pure resolver folds into an `OperatingModePolicy` + `RunShape`. Nothing
here changes or relaxes that decision.

**Stage C has landed the product surface.** The operator-facing `--profile`
namespace is now the set of nine **semantic work kinds**, and the built-in
profile JSON (`core/_config/pipeline_profiles_v2.json`) is keyed by them:

```text
feature  small_task  complex_feature        (Common)
planning  delivery_audit  code_review  research  refactor  migration  (Focused)
```

plus the two **internal** profiles `task` and `correction` (hidden from the
fresh-run picker, still selectable explicitly and via follow-up).

What is live today:

- **Semantic picker.** The interactive `orcho run` picker presents the work
  kinds grouped Common / Focused, with `feature` first and carrying the
  `[default]` chip. The old flat names (`lite` / `advanced` / `enterprise` /
  `plan` / `review`) are gone from the catalogue.
- **Explicit semantic identity in the schema.** Each built-in profile carries
  `semantic_profile`, `default_mode`, and `recipe_kind` fields. `variant` is no
  longer the source of semantic identity for built-ins — see
  [profile_schema.md](../reference/profile_schema.md).
- **Deterministic default-mode.** Each work kind has a deterministic default
  `OperatingMode` (the projection table below), and a run with no explicit mode
  takes it.
- **Explicit mode override.** `orcho run --mode {fast,pro,governed}` (and an
  explicit project/contract `work_mode`) overrides the projected default.

What is still **deferred** (see [Next](#next)): the full
`resolve_run_shape()` resolver, surfacing `RunShape` on the SDK / MCP wire, and
**auto-selection** of a work kind from the task text or changed paths. Selection
stays explicit and operator-driven.

## North-star model (target; resolver not yet built)

The accepted target resolution flow is:

```text
SemanticProfile  ×  OperatingMode  ->  resolver  ->  OperatingModePolicy + RunShape
   (what work)        (how strict)
```

- `SemanticProfile` — the **kind of work** (`small_task`, `feature`,
  `complex_feature`, `planning`, `code_review`, `delivery_audit`, `research`,
  `refactor`, `migration`). This is the closed Stage C vocabulary in
  `pipeline/runtime/run_shape.py`.
- `OperatingMode` — the **strictness** the run is held to, chosen
  independently of the kind of work (`fast` / `pro` / `governed`).
- the **resolver** — a pure, unit-testable function that would normalise both
  inputs, apply mode defaults and overlays, validate the combination, and emit
  a typed posture. **Not implemented yet** — today a profile is resolved
  directly through the v2 loader, and the default-mode projection is a focused
  pure helper (`pipeline/runtime/semantic_mode_defaults.py`) rather than the
  full resolver.
- `OperatingModePolicy` + `RunShape` — the materialised posture the target
  records. These value objects ship as inert definitions; nothing on the
  runtime path constructs or reads them yet.

## Strictness vocabulary (`work_mode` / `OperatingMode`)

| Mode | Meaning |
|------|---------|
| `fast` | Move quickly; treat gates as hints and cheap feedback. |
| `pro` | Balanced default; run important gates and repair obvious failures. |
| `governed` | Strict delivery discipline; require declared proof before key transitions. |

`governed` is **never a built-in default** — it is an explicit opt-in posture a
run selects via `--mode governed` (or an explicit `work_mode`), never something
a work kind projects on its own.

> **Historical footnote: `team` (now `pro`).** Early ADR 0064 drafts named the
> middle operating mode `team`. That label is **retired**. The runtime
> strictness value is `pro`, and the live `WORK_MODES` tuple is
> `("", "fast", "pro", "governed")` (`""` = unset). New docs and tests must
> **not** introduce `team` as a live runtime value. See
> [verification_contract.md § work_mode](verification_contract.md).

## Default-mode projection table

Each semantic work kind projects a deterministic default `OperatingMode`
(`pipeline/runtime/semantic_mode_defaults.py`). A run uses this default unless
an explicit `--mode` / `work_mode` override is given.

| Work kind | Default mode |
|-----------|--------------|
| `small_task` | `fast` |
| `feature` | `fast` |
| `research` | `fast` |
| `complex_feature` | `pro` |
| `planning` | `pro` |
| `code_review` | `pro` |
| `delivery_audit` | `pro` |
| `refactor` | `pro` |
| `migration` | `pro` |

## Recipe migration (what the cutover reused)

The Stage C work kinds reuse the previously-shipped executable recipes verbatim
(same phase graph, worktree isolation, cross gates, `implementation_execution`,
and handoff types) — only the keys, semantic fields, and default mode are new.

| Work kind | Reused recipe | Default mode | Worktree default |
|-----------|---------------|--------------|------------------|
| `small_task` | former `lite` | `fast` | direct checkout (`worktree_isolation=off`) |
| `feature` | former `advanced` (`implementation_execution=subtask_dag`, both cross gates strict) | `fast` | isolated (per-run default) |
| `complex_feature` | former `enterprise` (+ `compliance_check`, both cross gates strict) | `pro` | isolated (per-run default) |
| `planning` | former `plan` (plan artifact only, `human_feedback_always`) | `pro` | direct checkout |
| `research` | reuses the `plan` recipe (no delivery gates by default) | `fast` | direct checkout |
| `delivery_audit` | former `review` (review_changes → final_acceptance) | `pro` | direct checkout |
| `code_review` | reuses the `review` recipe | `pro` | direct checkout |
| `refactor` | reuses the `advanced` recipe | `pro` | isolated (per-run default) |
| `migration` | reuses the `enterprise` recipe | `pro` | isolated (per-run default) |
| `task` (internal) | execute an existing plan (skips planning) | — | direct checkout |
| `correction` (internal) | system follow-up after a rejected delivery (ADR 0085) | — | inherits retained parent worktree |

`feature` is the fresh-run default (`DEFAULT_PROFILE_NAME` /
`CROSS_DEFAULT_PROFILE`), reusing the former `advanced` recipe so the
fresh-run delivery behaviour — including both terminal cross gates at
`run=always` / `on_skip=block` — is unchanged.

## Today's policy knobs

- **`work_mode`** — the verification contract's strictness projection
  (`fast` | `pro` | `governed`, plus unset `""`). It derives the effective
  per-gate policy and action when a schedule entry leaves them unset. The
  effective `work_mode` for a run is the explicit override (CLI `--mode` or an
  explicit project/contract value) when set, otherwise the profile's projected
  `default_mode`. See
  [verification_contract.md § work_mode](verification_contract.md).
- **`implementation_execution`** — chooses how the implement phase consumes a
  parsed plan: `whole_plan` (one invoke) or `subtask_dag` (execute
  `ParsedPlan.subtasks` as tracked delivery units with receipts). `feature` and
  `refactor` select `subtask_dag`. See
  [profile_schema.md § implementation_execution](../reference/profile_schema.md).
- **`worktree_isolation`** — a profile/config policy (`off` selects
  direct-checkout work; otherwise the per-run isolation default applies). Not
  yet resolved through `RunShape`.

## Next

Everything below is **deferred to later slices and is not implemented today**:

- **Resolver** — `resolve_run_shape()` that turns a
  `SemanticProfile × OperatingMode` pair (plus overrides) into a `RunShape`.
  Today the default-mode projection is a focused pure helper; the full resolver
  that emits `OperatingModePolicy` + `RunShape` is not built.
- **SDK / MCP status fields** — surfacing `RunShape` (requested/resolved
  profile + mode, overrides, resolved policy, reason) on the SDK status and the
  MCP wire. A future wire change with its own `orcho-mcp` schema + mock smoke.
- **Auto-selection** — inferring a `SemanticProfile` (or mode) from the task
  text or changed paths. Not built; selection stays explicit and
  operator-driven.
- **Policy-owned runtime knobs** — routing `work_mode`,
  `implementation_execution`, and `worktree_isolation` through one resolved
  policy layer once the resolver lands.

## See also

- [ADR 0064](../adr/0064-semantic-profiles-and-operating-modes.md) — accepted
  semantic-profile / operating-mode target architecture.
- [profile_schema.md](../reference/profile_schema.md) — the v2 profile schema,
  the semantic identity fields, and the `implementation_execution` knob.
- [profile_authoring.md](../guides/profile_authoring.md) — authoring guide for
  the semantic work kinds.
- [verification_contract.md](verification_contract.md) — where `work_mode`
  lives and how it derives gate policy.
