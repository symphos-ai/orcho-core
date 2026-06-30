# ADR 0077 — Verification contract read-only projection (Stage 1)

- Status: Accepted
- Date: 2026-06-09
- Relates to: ADR 0074 (worktree bootstrap), ADR 0076 (durable
  verification-environment receipt), ADR 0026/0028/0060 (typed prompt parts,
  cache-first assembly)

## Context

[`docs/architecture/verification_contract.md`](../architecture/verification_contract.md)
describes a project-level *verification contract* — declared dependency repos,
verification environments, named commands, a phase-aware schedule, and a
`work_mode` strictness control. Before this ADR only `worktree_bootstrap`
(ADR 0074) and the verification-environment receipt (ADR 0076) existed in core;
the rest of the contract was documented as *proposed* and not read by core.

We want the contract's declarative facts to start informing a run **without**
changing any execution, gating, or repair behavior. Operators should be able to
declare a contract and immediately see it surfaced (in the run header and in
phase prompts) so the authoring surface can stabilize before any blocking
semantics are designed. The hard constraint is that turning the contract on must
not change *what runs* — only *what is shown*.

## Decision

Add a read-only **Stage 1 projection** of the verification contract. It is
strictly informational: nothing in this stage executes `verification.commands`,
writes a receipt, blocks a phase transition, or starts a repair loop.

### Protocol extension (`PluginConfig`)

`PluginConfig` gains four optional, raw (normalised-but-not-coerced) fields,
loaded by the same rule as `quality_gates` / `worktree_bootstrap` so the loader
stays non-throwing and forward-compatible:

- `dependency_repos: dict[str, dict]`
- `verification_envs: dict[str, dict]`
- `verification: dict` (keys `default_env`, `required`, `commands`, `schedule`)
- `work_mode: str` (`""` / `fast` / `pro` / `governed`)

`load_plugin` does **not** validate these; an undeclared contract is the empty
default and behaves byte-identically to before.

### Typed model and default prompt-policy

A focused module, `pipeline/verification_contract.py`, owns the protocol:

- `VerificationContract` (frozen) + `VerificationContractError`.
- `VerificationContract.from_plugin(plugin)` returns `None` when no contract
  field is declared, and otherwise normalises and validates structural types,
  `work_mode`, schedule policies (`off`/`suggest`/`warn`/`require`) and hooks
  (`before_phase`/`after_phase`/`before_delivery`/`on_resume`/`manual_only`),
  command `env` references, schedule command references, and `default_env`
  existence — raising `VerificationContractError` on a declared-but-invalid
  contract.
- `PlaceholderContext` + `resolve_placeholders` perform **syntactic**
  substitution of `{checkout}` / `{project}` / `{workspace}` / `{run_dir}` /
  `{dependency:name}`. Unknown or unavailable tokens (including a `None`
  `run_dir`) are left literal; the resolver never raises.
- `render_header_summary` (names-only) and `render_phase_block` (phase-limited).

The **default prompt-policy** is code-owned: `render_phase_block` surfaces only
the schedule entries relevant to a phase (`before_phase`/`after_phase` matching
the phase, `before_delivery` on the final phases) and never dumps the whole
config into a prompt. The documented `prompt_policy` override merge chain
(workspace → project → run/profile) is **deferred to a future stage**; Stage 1
ships only the Orcho default with a `TODO(orcho-verification-stage2)` marker in
the module.

### Integration points

- **Validation — `pipeline/project/session_run.py`.** The contract is validated
  exactly once, unconditionally, between `load_plugin` and
  `print_pipeline_header` (via the `project_verification_contract` seam in
  `run_setup.py`). It is *not* under the presentation gate, so a
  declared-but-invalid contract fails fast with `VerificationContractError` even
  under SILENT, where the header never prints.
- **Header — `core/io/transcript.py` / `run_setup.py`.** `render_run_header`
  gains an optional `verification_line`; `print_pipeline_header` renders
  `render_header_summary(contract)` only when present. No contract → no line →
  byte-identical header.
- **State — `pipeline/project/state_setup.py`.** When a contract is declared,
  the validated object and a resolved `PlaceholderContext` are stored in
  `state.extras` under `verification_contract` / `verification_placeholders`.
  No contract → neither key is added.
- **Prompts — handler→builder + `pipeline/phases/adapters.py`.** Each phase
  handler (plan, implement, review_changes, repair_changes, validate_plan)
  computes its phase-limited block via a `state`-aware helper
  (`_verification_contract_part`) and passes it as a typed dynamic RUN-scoped
  `PromptPart` to its builder **and** to the legacy `adapters.py` mirror, so the
  block reaches the real wire on both dispatch paths. Builders themselves have
  no `state`; delivery flows through the handlers via the existing
  `extra_upper_parts` channel. The part is `stability=RUN` / `cache_scope=
  SESSION` so it rides in the turn payload, never a cross-run cache prefix.

### MCP validation

This stage is read-only and **does not change the runtime/gate wire schema**.
The new fields are project-local plugin configuration consumed inside
`orcho-core`; they are not emitted on the MCP wire, do not alter profile shape,
mode flags, or gate primitives, and `work_mode` is not surfaced as a public MCP
contract field in Stage 1. Therefore **no `orcho-mcp` update is required** for
this change. A future stage that promotes any contract field (e.g. `work_mode`)
onto the MCP wire, or that adds blocking/gate semantics, must ship the matching
`orcho-mcp` update and E2E mock smoke per the MCP Validation rule.

## Consequences

- Operators can declare a verification contract and see it reflected in the run
  header and in bounded per-phase prompt blocks, with placeholders resolved
  syntactically from the available run context.
- The no-contract path is byte-identical to prior behavior (header, prompt
  bytes, wire-cache layout), preserving existing snapshot/boundary tests.
- A declared-but-invalid contract is a hard, early failure — invalid state
  cannot reach the header or a phase prompt.
- No execution, receipt writing, transition blocking, or repair is introduced;
  the override merge chain and any blocking semantics remain future work.

## See also

- [Verification contract](../architecture/verification_contract.md) — the
  authoring reference and the "Implemented today vs proposed" status table
- [ADR 0074](0074-worktree-bootstrap.md) — worktree bootstrap (implemented)
- [ADR 0076](0076-durable-verification-environment-receipt.md) — durable
  verification-environment receipt (implemented)
