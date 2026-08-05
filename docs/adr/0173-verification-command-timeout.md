# ADR 0173 — Per-command verification timeout, and a typed error for an invalid contract

- **Status:** Accepted
- **Date:** 2026-08-05
- **Extends:** [ADR 0077](0077-verification-contract-read-only-projection.md) and [ADR 0080](0080-verification-contract-command-receipts.md)

## Context

Two defects surfaced from the same declaration, one run apart.

A project's `vitest` gate hit the executor's hard-coded 600s ceiling: the
receipt recorded `duration_s: 600.018`, `exit_code: null`, and empty
stdout/stderr, which the delivery gate correctly read as a red required gate and
blocked on. The ceiling was not declarable anywhere — `_TIMEOUT_S = 600` in
`pipeline/verification_command.py` was a module constant — while
`worktree_bootstrap` steps, in the same plugin file, do accept a per-step
`timeout`. The asymmetry is not principled: both are engine-run subprocesses
whose honest runtime is a property of the project, not of Orcho.

The plugin author then declared `timeout` on the command by analogy. The
contract rejected the unknown field with a `VerificationContractError` — a plain
`ValueError` that no boundary maps — so the next `orcho run` ended in a Python
traceback out of `cli/orcho.py`, after the run directory, `output.log`, and
`events.jsonl` had already been created. An operator-fixable declaration error
was presented as an engine crash.

## Decision

**1. `timeout` is a declared field of `verification.commands`.**

It is validated at contract load as a positive `int` number of seconds (`bool`
rejected explicitly, since `True` is an `int` and would silently mean one
second). Absent a declaration the executor applies its default backstop, which
stays 600s and is now named `_DEFAULT_TIMEOUT_S`.

The declaration moves the ceiling and nothing else. Exceeding it degrades to the
same failed receipt as before (`exit_code: null` plus a `detail` naming the
effective budget), so a hang stays catchable: an empty `stdout_tail` with
`duration_s` pinned to the ceiling remains the distinguishing signature of a
hung command versus a slow one. There is no unbounded option — core owns the
protocol (a bounded, declarable budget); the project owns the number.

**2. A declared-but-invalid contract raises a typed, operator-facing error.**

`pipeline/project/run_setup.project_verification_contract` — the single point at
which the run path validates the contract — wraps `VerificationContractError` in
`sdk.errors.InvalidVerificationContract` (an `OrchoError`, `exit_code = 2`),
with the original exception kept as `__cause__`. The message names the plugin
file, quotes the structural complaint, and gives the read-only re-check
(`orcho quality-gates --project <dir>`). Every boundary that already maps
`OrchoError` — the CLI adapter in `sdk/runner.py`, MCP, embedders — therefore
reports it as a configuration error instead of a traceback.

The unknown-field complaint itself now lists the legal vocabulary
(`known fields: cost, env, parity, run, timeout`), because the rejection happens
precisely when the author expected a field the contract does not have.

## Consequences

- Projects whose suites legitimately run near the default ceiling declare a
  budget instead of losing a required gate to a truncated receipt.
- The wrapping is at the run path's validation seam only. `orcho quality-gates`
  keeps its own local handling; `sdk/verify.py` and the delivery reader still
  call `VerificationContract.from_plugin` directly and would surface the
  untyped error — acceptable while the crash-shaped path is the run entry, and
  a candidate for the same treatment if either is observed raw.
- The command receipt schema is unchanged: the effective budget appears in the
  timeout `detail` text, not as a new receipt field. Evidence and MCP wire
  formats are untouched (falsifier).
