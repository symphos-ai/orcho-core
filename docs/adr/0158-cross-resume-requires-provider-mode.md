# ADR 0158 — Cross resume requires durable provider mode

- **Status:** Accepted
- **Date:** 2026-07-25
- **Supersedes:** the missing-`meta.mock` fallback in
  [ADR 0157](0157-cross-cli-intent-fidelity.md)

## Context

ADR 0157 made provider mode durable for new cross runs, but retained an
args-driven fallback for earlier run metadata without `mock`. That fallback
selected real providers when `--mock` was absent. A warning did not prevent the
provider construction or make the implicit selection safe.

Provider mode is operator intent and a run-level safety boundary. Resume cannot
infer it from an absent field.

## Decision

Every resumed cross run requires a top-level boolean `meta.mock`.

- A valid persisted value is inherited unless the supported explicit `--mock`
  override selects mock mode.
- A missing value fails closed before provider or phase-agent construction.
- A present non-boolean value also fails closed.
- There is no warning fallback, inferred default, compatibility branch, or
  migration path for run metadata without the required field.

Fresh runs continue to persist the resolved boolean before they can be resumed.

## Consequences

Run metadata created without `meta.mock` cannot be resumed. The operator must
start a new run instead of allowing Orcho to guess whether external providers
may be invoked.

The provider-mode resolver has only three sources: `fresh`, `explicit`, and
`inherited`. CLI output no longer describes a legacy fallback.

## Rejected alternatives

1. **Default missing metadata to real and print a warning.** Rejected because
   provider invocation has already been authorized implicitly by the time the
   warning is visible.
2. **Allow `--mock` to repair incomplete resume metadata.** Rejected because it
   preserves a compatibility path for artifacts that do not satisfy the
   durable run contract.
3. **Infer mode from model names, events, or usage.** Rejected because those
   are observations, not the authoritative operator-intent field.
