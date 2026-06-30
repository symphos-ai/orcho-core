# ADR 0006: SessionAdapter is not an External Runtime Integration Bus

- **Status:** Accepted
- **Date:** 2026-05-06
- **Phase:** 3
- **Deciders:** project owner

## Context

Phase 3 introduces `SessionAdapter` — a Strategy + Registry surface
where phase-specific writers translate `state.phase_log[name]` into
`session["phases"][name]`. Six built-in adapters ship: PlanAdapter,
PlanQAAdapter, BuildAdapter, RoundAdapter, FinalQAAdapter,
HypothesisAdapter.

A natural next question: could `SessionAdapter` also serve as the
mechanism for *importing* foreign transcript shapes (forge-session,
codex-session, claude-session) into the orcho session schema for
`--resume` continuity or post-mortem audit?

## Decision

**No. SessionAdapter scope is `phase_log → session` translation only.**

If foreign-runtime transcript import becomes a real need (e.g. a user
runs orcho once, then continues the same task in `claude` CLI
interactively, then wants to resume in orcho), that capability gets a
separate concept:

- **Likely name:** `ExternalRuntimeAdapter` or `TranscriptImporter`.
- **Likely module:** `pipeline/transcripts/` or
  `pipeline/runtime_adapters/`.
- **Likely registry:** `orcho.transcript_importers` entry_points.

This ADR commits to keeping the two concepts separate even though both
have "adapter" in their plausible names.

## Drivers

- **Single Responsibility.** `SessionAdapter` does one thing: write
  session shape from in-memory state. Mixing transcript import would
  bloat the contract (input is no longer a `PipelineState`; it's a
  foreign JSON / streaming format).

- **Concept clarity for plugin authors.** A plugin author writing a
  `MyCompanyPlanAdapter` should think about session shape, not about
  parsing foreign tool transcripts. Keeping the concepts separate
  preserves that clarity.

- **Different lifecycle.** Session adapters fire after every phase
  invocation. A transcript importer fires once at run start (or
  `--resume`) — a fundamentally different cadence.

- **Different security posture.** Importing foreign transcripts means
  parsing untrusted JSON from another tool's run directory; that
  belongs in a sandboxed parser surface, not on a registry that
  plugin authors freely override.

## Consequences

### Positive

- `SessionAdapter` Protocol stays minimal:
  `write(name, state, session, *, round_n=None) -> None`.
- Phase 3 implementation is bounded — six adapters, all reading from
  `state.phase_log[name]`, no parser code needed.
- Phase 7 plugin extension surface is easy to document: "implement
  `SessionAdapter`, register through `orcho.session_adapters`
  entry_points, override the built-in for your phase".

### Negative / Costs

- A future transcript-import feature has to live in a separate module
  and entry_points group, not piggyback on the existing surface.
  Acceptable: the use case is hypothetical; YAGNI applies until a real
  user need emerges.

### Neutral

- The naming ambiguity (`SessionAdapter` could be misread as "session
  format adapter") is mitigated by the docstring's explicit scope
  declaration and this ADR.

## Validation

`pipeline/session_adapters.py:SessionAdapter` docstring carries the
scope invariant verbatim. `tests/unit/pipeline/runtime/test_session_adapters.py`
covers the contract (write to session dict given state input) without
ever consuming external transcript formats.

## Alternatives Considered

### A. Make `SessionAdapter` the universal "anything that touches session shape" Strategy

Rejected — leads to a bloated Protocol with input shapes the registry
can't predict. Forge transcripts and orcho-internal phase_log are too
different.

### B. Defer the decision

Rejected — without the ADR, future maintainers
would naturally extend `SessionAdapter` for transcript import when the
need first arose, by which point overlap and confusion would be
locked in. Best to draw the line now.

## References

- ADR 0001: pipeline architecture redesign
- `docs/architecture/session_shape.md`
- `pipeline/session_adapters.py`
