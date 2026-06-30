# ADR 0078 — Verification contract env-assertions and CLI (Stage 2)

- Status: Accepted
- Date: 2026-06-09
- Relates to: ADR 0077 (verification contract read-only projection),
  ADR 0076 (durable verification-environment receipt), ADR 0074 (worktree
  bootstrap)

## Context

[ADR 0077](0077-verification-contract-read-only-projection.md) added a
read-only Stage 1 projection of the verification contract: core loads,
validates, and surfaces the contract (run header + per-phase prompt blocks) but
executes nothing. The durable verification-environment receipt
([ADR 0076](0076-durable-verification-environment-receipt.md)) is real but
fixed: it runs exactly one hard-coded import-invariant (`pipeline` imports from
the checkout, not a separately installed copy) during `implement` /
`repair_changes`.

That bare-host invariant is not enough. The contract's whole point is that a
project declares *which* env-assertions make its proof valid — import paths,
binaries on `PATH`, tool versions, required files — and those vary per stack
(Python import paths, PHP `vendor/bin/phpunit`, Go/Rust toolchains). Until core
can *execute* the declared env-assertions of a chosen `verification_env`, the
operator still cannot get a real pass/fail for "does this checkout resolve the
subject under test the contract claims?".

We want an operator-invokable execution of one env's declared assertions that
proves facts about the **declared checkout/project**, not the bare host running
the CLI — without yet introducing any blocking, gating, or repair semantics.

## Decision

Add a **Stage 2** that *executes* the declared env-assertions of a single
`verification_env`, persists an env-assertion receipt, and exposes two operator
CLI commands. Stage 2 is still non-blocking: nothing here gates a transition or
starts repair.

### Generic assertion engine (`pipeline/verification_env.py`)

A focused module owns execution: `run_env_assertions(env_name, env_spec, ctx)`.
The assertion vocabulary is **generic and dispatched by key** — Python is one
interpreter, not a hard-coded path:

- `{"import": M, "path_equals": p}` / `{"import": M, "path_under": d}` — run the
  declared interpreter (placeholder-resolved `python`, else the current
  `sys.executable`) as a subprocess and compare the resolved `M.__file__`.
- `{"path_exists": p}` — path exists.
- `{"file_exists": p}` — path exists and is a regular file.
- `{"command_exists": name}` — `shutil.which(name)` against the (overridden)
  `PATH`.
- `{"version": [argv...], "contains": substr}` — the **single** version form
  (no `version_command` alias): run `argv`, assert `substr` is in
  stdout+stderr.

An unknown assertion key is a **failed check** (`passed=false` with a detail),
never a crash. Subprocesses run with a ~60s timeout and never raise outward: an
`OSError` / `SubprocessError` (including timeout) degrades to `passed=false` with
a detail. Every string (interpreter, cwd, env values, paths, argv) is run
through the Stage 1 `resolve_placeholders` with the supplied context, so the
syntactic placeholder rules from ADR 0077 are reused, not reinvented.

#### Default cwd = declared checkout (load-bearing)

The effective working directory is resolved as: if the env declares `cwd`, use
its placeholder-resolved value; otherwise use `ctx.checkout`; if that is empty,
fall back to `ctx.project`. This effective cwd is the working directory of the
import/version subprocesses (so `sys.path[0]` is the declared checkout) **and**
the base for relative `path_exists` / `file_exists`. Crucially, import/version
execution never depends on the CLI/test process cwd — that is what makes an
import assertion prove the *declared* checkout instead of a bare-host invariant.

The DRY seam for building the resolved context is
`pipeline/verification_contract.placeholder_context_for(contract, *, checkout,
project, workspace, run_dir)`, shared by the Stage 1 state projection and the
Stage 2 engine. Stage 1's `state_setup` projection is byte-identical after the
refactor.

### Env-assertion receipt and its isolation from evidence v1

`pipeline/evidence/verification_receipt.py` gains
`write_env_assertion_receipt(*, output_dir, result)` plus two constants:

- `ENV_RECEIPTS_DIRNAME = "verification_env_receipts"` — **distinct** from the
  Stage 1/ADR 0076 `RECEIPTS_DIRNAME = "verification_receipts"`.
- `VERIFICATION_ENV_KIND = "verification_env_assertions"`.

The receipt is a flat JSON object under
`<run_dir>/verification_env_receipts/verify_env_<env>.json` (env name
sanitised — no path traversal). Its shape:

```json
{
  "kind": "verification_env_assertions",
  "env": "ci",
  "subject": {"checkout": "/abs/checkout", "project": "/abs/project"},
  "cwd": "/abs/effective-cwd",
  "interpreter": "3.12.4 (/abs/.../python)",
  "env_overrides": {"PYTHONPATH": "src:."},
  "assertions": [
    {"name": "pipeline", "kind": "import_path_equals",
     "expected": "/abs/.../pipeline/__init__.py",
     "actual": "/abs/.../pipeline/__init__.py", "passed": true, "detail": ""}
  ],
  "all_passed": true,
  "temp_env_outside_checkout": true
}
```

The receipt is written **only** under `run_dir`, never under the checkout
(ADR 0076 invariant). It is physically isolated from the schema-validated
evidence v1 bundle: the evidence collector reads only
`verification_receipts/`, so placing env-assertion receipts in their **own**
directory keeps `VERIFICATION_ENV_KIND` out of the bundle by *location*, not by
filtering. `summarize_verification_receipts` / `_build_verification_receipts`
are unchanged and never see the new kind.

### CLI surface

- `orcho verify env` (SDK `sdk.verify.verify_env`). Flags: `--project/-p`,
  `--env`, `--run-id`, `--workspace/-w`. It resolves the run, **proves the run
  belongs to the project** (explicit `--project` is `Path.resolve()`-normalised
  and compared to the run's `meta['project']`; a missing/mismatched value raises
  with no write), loads the contract, selects the env (`--env` else
  `default_env`), runs the assertions from the declared checkout cwd, and writes
  the env-receipt. A missing contract / unknown env raises before any write.
  Exit codes: 0 all-passed, 1 an assertion failed, 2 a resolution error.
- `orcho workspace fine-tune --dry-run` (SDK `sdk.fine_tune.fine_tune_project`).
  Inspects a project by its repo markers (`pyproject.toml` / `package.json` /
  `composer.json` / `go.mod` / `Cargo.toml`) and prints a **candidate**
  contract (`verification_envs` + `verification.commands` + `default_env` +
  `work_mode="pro"`) in the generic vocabulary above. It is **pure-read**:
  proven by a content fingerprint (path + size + sha256) of the whole tree
  being identical before and after, with or without `--dry-run`.

### Boundary — what Stage 2 does NOT do

- It does **not** block a phase transition, fire a `require` schedule, or start
  a repair loop. Execution produces a receipt; it does not gate anything.
- It does **not** write per-command receipts (the ADR 0076 phase receipt and
  this env-assertion receipt are the only two receipt kinds); the richer
  named-command receipt remains a future goal.
- It does **not** materialise a `plugin.py` from `fine-tune`; the candidate is
  printed for the operator to review and apply by hand.
- It does **not** implement the `prompt_policy` override merge chain (workspace
  → project → run/profile); that remains deferred from ADR 0077.

### MCP validation

Stage 2 changes **no** runtime/gate wire schema, profile shape, mode flags, or
gate primitives. `orcho verify env` and `orcho workspace fine-tune` are local
operator CLI commands over the public SDK; the env-assertion receipt is a
run-local artifact outside the evidence v1 bundle. Nothing is emitted on the
MCP wire. Therefore **no `orcho-mcp` update is required** for this change. A
future stage that promotes env-assertion results onto the MCP wire, or that adds
blocking/gate semantics, must ship the matching `orcho-mcp` update and E2E mock
smoke per the MCP Validation rule.

## Consequences

- Operators can execute one env's declared assertions and get a real pass/fail
  proving the declared checkout/project, replacing the single bare-host
  import-invariant as the only executable check.
- The generic vocabulary lets non-Python stacks (path / command / version /
  file assertions) be proven without a Python-only code path.
- The env-assertion receipt is durable and reviewable yet cannot pollute the
  schema-validated evidence v1 bundle, because it lives in a separate directory
  the collector never reads.
- `fine-tune --dry-run` gives a zero-risk authoring on-ramp: a project sees a
  candidate contract without a single file being written.
- No blocking, gating, repair, per-command receipts, fine-tune materialisation,
  or override-chain prompt-policy is introduced; those remain future work.

## See also

- [Verification contract](../architecture/verification_contract.md) — the
  authoring reference and the "Implemented today vs proposed" status table
- [ADR 0077](0077-verification-contract-read-only-projection.md) — read-only
  Stage 1 projection (implemented)
- [ADR 0076](0076-durable-verification-environment-receipt.md) — durable
  verification-environment receipt (implemented)
- [ADR 0074](0074-worktree-bootstrap.md) — worktree bootstrap (implemented)
