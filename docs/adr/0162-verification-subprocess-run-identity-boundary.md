# ADR 0162 — Verification subprocess run-identity boundary

Status: Accepted

## Context

`ORCHO_RUN_ID` lets a supervisor assign the identity of an Orcho run before
launch. The project pipeline consumes it while constructing run directories,
worktrees, checkpoints, and delivery artifacts.

Scheduled verification commands are arbitrary project subprocesses. When they
inherited the parent run's `ORCHO_RUN_ID`, tests that launch nested Orcho
sessions silently reused that parent identity. Their worktree branch and
delivery artifacts then differed from the same tests launched in a clean shell.
The official gate consequently failed even though the tested code was
unchanged.

`ORCHO_ISOLATION_ID` has a different contract. It is the documented namespace
for external resources shared by worktree bootstrap, verification, and
teardown. Gate subprocesses must continue to inherit it.

## Decision

Add `ORCHO_RUN_ID` to `RUN_SCOPED_ENV_CHANNELS`, the existing authoritative
sanitizer used by verification environment assertions and scheduled command
execution.

The resulting boundary is:

- the Orcho process consumes `ORCHO_RUN_ID` to construct its own durable run;
- verification subprocesses do not inherit that orchestration identity;
- `ORCHO_ISOLATION_ID` remains available to verification subprocesses;
- a plugin that intentionally tests a specific run identity may declare
  `ORCHO_RUN_ID` in its verification environment's `env` mapping. Declared
  overrides are applied after sanitization.

Official gate receipts remain owned and written by the parent engine. They do
not depend on exposing `ORCHO_RUN_ID` to the command being verified.

## Consequences

- Verification behaves the same when launched from a clean shell, a supervised
  mono run, or a cross-project child.
- Nested Orcho sessions created by project tests mint or receive their own
  identity instead of inheriting the parent gate's identity.
- External-resource isolation keeps its explicit
  `ORCHO_ISOLATION_ID` contract.
- ADR 0131's statement that gate commands inherit `ORCHO_RUN_ID` is superseded;
  its `ORCHO_ISOLATION_ID` lifecycle decision remains unchanged.
