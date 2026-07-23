# ADR 0144 — Cross child identity is not checkpoint resume

- **Status:** Accepted
- **Date:** 2026-07-21
- **Related:** ADR 0046, ADR 0132

## Context

Cross dispatch prepares per-alias plan and handoff artifacts in
`<cross-run>/<alias>/` before it starts the project child. The project pipeline
uses that directory as its output directory.

Previously dispatch passed the alias through `resume_from` for every child.
That bypassed the fresh-run collision guard for the prepared artifacts, but it
also made the child a checkpoint resume. A fresh child with a declared
verification contract then correctly failed because no scheduled-gate ledger
existed to resume.

## Decision

`project_alias` remains the child identity for every cross dispatch.
`resume_from` is used only when a cross checkpoint re-dispatches an incomplete
child. A fresh child receives no resume value.

The typed project request gains `preallocated_output_dir`. Cross dispatch sets
it only for a fresh child whose parent has prepared plan or handoff artifacts.
The fresh-run collision guard permits that parent-owned directory while all
fresh top-level runs retain collision protection. The flag does not alter run
identity, checkpoint loading, profile resume behavior, verification-ledger
initialization, or delivery semantics.

## Consequences

- Fresh cross children create their scheduled-gate ledger normally.
- Genuine child checkpoint resumes continue to require and validate the
  existing ledger.
- Parent-created handoff artifacts no longer need a false resume signal.
- A materialized directory from an unrelated fresh run remains rejected.
