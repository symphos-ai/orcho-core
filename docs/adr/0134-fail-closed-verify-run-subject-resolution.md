# ADR 0134 — Fail-closed verify-run subject resolution

## Status

Accepted.

## Context

Incident I8-V0 showed that `verify env` could write an environment receipt for
the canonical project while `verify run` wrote command receipts for a retained
worktree of the same run. A permissive missing-worktree fallback made absence
of the recorded subject look like proof of a different checkout.

## Decision

`sdk.verify` owns one typed physical-subject resolver shared by `verify env`,
`verify list`, and `verify run`. The canonical `--project` remains the owner of
the plugin and verification contract. `--run-id` supplies the physical subject:

- recorded isolated metadata requires its exact existing readable checkout;
- explicit `isolation='off'` permits the canonical project;
- incomplete metadata requires a valid non-canonical controller override;
- an override may confirm, but cannot contradict, recorded identity.

Resolution completes before assertions, commands, or receipt writes. Results
and CLI output expose the effective checkout plus provenance, while durable
receipt wire formats remain unchanged.

## Consequences

Operators receive exit code 2 rather than a receipt for a substituted subject.
The decision changes neither scheduled-gate selection, delivery transitions,
nor MCP projection.
