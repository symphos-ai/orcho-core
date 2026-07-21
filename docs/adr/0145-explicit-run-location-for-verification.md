# ADR 0145 — Explicit run location for verification

- **Status:** Accepted
- **Date:** 2026-07-21
- **Related:** ADR 0082, ADR 0144

## Context

Stage 9 materializes verification for both top-level project runs and nested
cross-project children.  A child run's logical `project_alias` is not a
filesystem location: different parent runs may contain children with the same
alias.

The previous nested-child failure came from resolving verification by alias as
though every run lived directly below the configured runs directory.  That can
look up `<runs>/<alias>` instead of the child directory
`<parent>/<alias>`, select an ambiguous location, or fail to find the run.

## Decision

Stage 9 passes the physical locator pair `runs_dir=output_dir.parent` and
`run_id=output_dir.name` to the verification SDK.  The SDK resolves that pair
through its existing canonical `find_run` path.

`project_alias` remains a logical project identity.  It is not promoted to a
globally unique identifier or used to infer a run directory.  The explicit
locator applies only when Stage 9 materializes the current run; top-level mono
calls that omit `runs_dir` retain their existing resolution order and CLI
behavior.

## Invariants

- The physical locator is the same for environment and command verification.
- Project-to-run and subject-identity validation remain fail-closed before any
  receipt is written.
- Receipt schemas, receipt locations relative to the resolved run, contract
  loading, command selection, and failure semantics do not change.
- Manual or operator-owned identities remain outside automatic command
  verification.
- `find_run` is the only resolver; this decision adds an explicit input, not a
  second verification path.

## Consequences

- Identical child aliases under separate parent runs resolve independently and
  receipts are written under the resolved child directory.
- Callers that know a run's physical output directory can resolve it without
  filesystem scanning.
- The public verification SDK gains an additive optional location parameter;
  existing mono callers stay compatible.

## Rejected alternatives

### Recursive alias search

Recursively scanning all run directories for an alias is rejected because an
alias is not globally unique, the result can be ambiguous, and search order
would become an undocumented part of verification behavior.

### Path-like or composite run IDs

Encoding a parent path in `run_id` is rejected because it conflates logical run
identity with filesystem addressing, weakens the typed SDK boundary, and would
invite path parsing and normalization semantics into every caller.

### A second executor outside the SDK

Adding a Stage 9-specific executor is rejected because it would duplicate
contract loading, validation, command selection, receipt writing, and failure
semantics.  Stage 9 must use the SDK's canonical verification path.
