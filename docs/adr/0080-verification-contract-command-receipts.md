# ADR 0080 — Verification contract native command-receipts (Stage 3)

- Status: Accepted
- Date: 2026-06-10
- Relates to: ADR 0078 (verification contract env-assertions and CLI, Stage 2),
  ADR 0077 (verification contract read-only projection, Stage 1),
  ADR 0076 (durable verification-environment receipt),
  ADR 0074 (worktree bootstrap)

## Context

Stage 1 ([ADR 0077](0077-verification-contract-read-only-projection.md)) loads,
validates, and projects the verification contract read-only. Stage 2
([ADR 0078](0078-verification-contract-env-assertions.md)) executes one
`verification_env`'s declared *assertions* on demand via `orcho verify env` and
persists an env-assertion receipt. What was still missing is the other half of
the contract's promise: executing the declared `verification.commands`
themselves — the native invocations a project names as readiness proof — and
recording a durable, reviewable receipt of each run.

The original incident ([ADR 0077](0077-verification-contract-read-only-projection.md)
Context) is the constraint that shapes this stage. A command must run *somewhere*
(its working directory) but its result is only meaningful *against a named
subject* (the run worktree under test). Those two are not the same path: a
cross-repo command may legitimately run from `{project}` or a dependency dir
while still proving a change made in the run worktree. If the receipt's git
provenance were taken from the command's working directory, a differential gate
would silently compare the wrong tree. Stage 3 must therefore keep "where the
command ran" and "which checkout the proof is about" as distinct fields.

## Decision

Add native execution of declared `verification.commands` via a new CLI surface
and a durable command-receipt, with a strict subject separation.

1. **Native execution, no wrapper-bin.** `orcho verify run [names] [--required]`
   executes a command's argv directly through `subprocess` (no shell, no
   generated wrapper binary). A string `run` is `shlex.split`; a list `run` is
   used verbatim; every token is placeholder-resolved and a bare `python` token
   maps to the declared env interpreter. `orcho verify list` is a pure
   projection: it prints each declared command's name, env, required marker, and
   placeholder-resolved run text, executing nothing.

2. **No env-override on `verify run`.** A command's env is its declared `env`
   (else the contract's `default_env`). There is deliberately no `--env` flag:
   the authoritative env is a property of the declared command, not an operator
   choice at run time.

3. **Run-scoped execution, canonical contract.** The contract is loaded from the
   canonical project (`{project}`), but commands execute in the run worktree:
   `{checkout}` resolves to `meta['worktree']['path']` when that is a real
   directory, falling back to `{project}` otherwise. This is the subject
   separation `{checkout}` = run worktree vs `{project}` = canonical repo.

4. **Command cwd vs git subject checkout — the load-bearing distinction.**
   - `eff_cwd` is the command's working directory: the declared env `cwd`
     (which may be `{project}`, a `{dependency:name}` dir, or a subdirectory),
     defaulting to `{checkout}`. It is the subprocess cwd and the value of
     `receipt.cwd` — nothing more.
   - The **git subject checkout** is always `ctx.checkout` (the run worktree).
     `git.checkout_head` and `git.changed_files_fingerprint` are computed from
     that subject, *independent of `eff_cwd`*. `git.baseline_head` comes from
     `meta['worktree']['base_ref']` — the same subject as `checkout_head`.

5. **Receipt stored outside evidence v1.** Each receipt is written to
   `<run_dir>/verification_command_receipts/<command>.json` with a
   `schema_version` and `kind: "verification_command"`. The directory is
   distinct from `verification_receipts/` (which the evidence collector reads),
   so the new kind never enters the evidence v1 bundle — same physical-isolation
   principle as the Stage 2 env-assertion receipt. The receipt is never written
   into the checkout.

6. **`required` is a list of command names.** `verification.required` is a tuple
   of declared command names (validated against `commands`), not a boolean.
   `orcho verify run --required` executes exactly that set; an empty/missing
   required set is a resolution error (exit 2).

7. **`parity` is a validated enum.** Each command may declare
   `parity: absolute | differential` (default `absolute`), validated in the
   contract *before* execution. `absolute` means the command's assertions stand
   on their own; `differential` means the receipt is read against a baseline
   (the run worktree's `base_ref`), and the receipt carries both
   `checkout_head` and `baseline_head` (the differential lens).

### Command-receipt schema (v1)

`write_command_receipt` persists the flat result of `run_command` as:

```json
{
  "schema_version": 1,
  "kind": "verification_command",
  "command": "lint",
  "env": "canonical-core",
  "cwd": "/abs/eff-cwd",
  "placeholders": {"checkout": "/abs/run-worktree", "project": "/abs/canonical"},
  "argv": ["/abs/.../python", "-m", "ruff", "check", "."],
  "env_overrides": {"PYTHONPATH": "src:."},
  "assertions": [
    {"name": "pipeline", "kind": "import_path_equals",
     "expected": "/abs/.../pipeline/__init__.py",
     "actual": "/abs/.../pipeline/__init__.py", "passed": true, "detail": ""}
  ],
  "exit_code": 0,
  "duration_s": 0.42,
  "stdout_tail": "…last N chars…",
  "stderr_tail": "",
  "log_path": "/abs/run_dir/verification_command_receipts/lint.log",
  "parity": "absolute",
  "detail": "",
  "git": {
    "checkout_head": "<HEAD of ctx.checkout = run worktree>",
    "baseline_head": "<meta.worktree.base_ref, differential only>",
    "changed_files_fingerprint": "<sha256[:16] of sorted changed files in ctx.checkout>"
  }
}
```

The differential lens — `parity`, `git.checkout_head`, `git.baseline_head`,
`git.changed_files_fingerprint` — is the vocabulary a later gating stage reads
to decide whether a `differential` command's proof is current. Crucially every
git field is about the run-worktree subject, while `cwd` is about where the
command happened to run. The executor never raises: an `OSError` /
`SubprocessError` / timeout degrades to `exit_code: null` with a `detail`.

## Consequences

- A project can now get real pass/fail for its declared commands, with a durable
  receipt naming the exact subject, argv, env, and (for differential commands)
  the two compared heads. Reviewers read receipts, not re-runs.
- Stage 3 remains **non-blocking**: `verify run` is operator-invoked. No
  transition is gated and `require` still gates nothing — that is a later stage.
- The subject-separation invariant (git provenance from `ctx.checkout`, not
  `eff_cwd`) is the property most likely to regress; it is pinned by a test with
  an env `cwd` deliberately different from `{checkout}`.
- Core writes only the `parity` / `baseline_head` / `fingerprint` fields; it
  does **not** embed a domain-specific diff runner. Interpreting the
  differential lens is left to the (future) gate consumer.

## MCP wire falsifier (T9)

Stage 3 adds native execution of declared verification commands
(`orcho verify list` / `orcho verify run`) and a durable **command-receipt**.
Before the final gate we explicitly tested whether this touches any MCP-facing
wire, rather than deferring the question.

**Conclusion: the MCP wire is NOT touched. No `orcho-mcp` update or mock smoke
is required for this change.**

Evidence gathered:

1. **Command-receipt is out-of-wire by physical location.** The command-receipt
   is written under `<run_dir>/verification_command_receipts/`, a directory
   distinct from `verification_receipts/`. The evidence collector reads only
   `verification_receipts/`, so the `verification_command` kind never enters the
   evidence v1 bundle. `pipeline/evidence/schema.py` (`REQUIRED_TOP_LEVEL_KEYS`,
   `REQUIRED_COMMAND_KEYS`, `validate_bundle`) has no slot for it — the bundle's
   `commands` rollup is the event-derived command list, a disjoint concept. This
   mirrors the Stage 2 env-assertion receipt isolation (ADR 0078).

2. **No MCP resource projects the changed surface.** A grep across `pipeline/`
   and `sdk/` for the new symbols (`verification_command`,
   `VERIFICATION_COMMAND_KIND`, `write_command_receipt`, `verify_run`,
   `verify_list`) and for `VerificationContract` / `contract.required` found no
   reference inside any MCP resource/projection module. There is no MCP module
   in `orcho-core`; `orcho-mcp` is a separate package outside the checkout. The
   existing MCP read tools (`orcho_run_evidence`, `orcho_run_status`, …) project
   the evidence v1 bundle / run state, which are unchanged.

3. **The `required: bool → tuple[str, ...]` change is in-process only.** The
   field is consumed solely in `sdk/verify.py` and validated in
   `pipeline/verification_contract.py`. It is not serialized to `meta.json`, the
   run header, or any wire. `render_header_summary` (the one contract→text
   projection feeding the printed run header) surfaces env/command/schedule
   *names* only — never `required` and never receipts.

The conclusion is pinned by `TestMcpWireFalsifier` in
`tests/unit/pipeline/evidence/test_verification_receipt.py`: if a future change
leaks the `verification_command` kind into the v1 schema, that test fails and
the falsifier flips — an `orcho-mcp` update plus an E2E mock smoke
(`tests/acceptance/test_full_mock_flow.py` or the mcp-smoke) then become a
mandatory dependency of the final gate.
