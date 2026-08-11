# ADR 0166 — Run-status spend and liveness projection

- **Status:** Accepted
- **Date:** 2026-07-29
- **Relates to:** [ADR 0021](0021-public-sdk-boundary.md) and [ADR 0020](0020-run-evidence-in-core.md)

## Context

Consumers commonly need a run's accounting-aware spend and a lightweight
indication of where its event stream currently ends. Re-reading `metrics.json`
through a second SDK call, or materialising `events.jsonl` through
`read_run_events`, makes a status lookup more expensive than its snapshot role
requires and creates competing client-side projections.

The event stream is append-only JSONL while a run is active. A reader can race
with a writer and therefore can encounter an unfinished or malformed trailing
record. This is normal durable-state behavior, not a reason to fail status.

## Decision

`RunStatus` is the sole public owner of these status projections:

- `total_cost_usd_equivalent: float = 0.0`
- `last_event_seq: int | None = None`
- `last_event_ts: str | None = None`

`load_status` derives the cost only from its already-loaded, normalized
`raw_metrics` mapping. It does not call `sdk.metrics`, open `metrics.json` a
second time, or recompute accounting. Accounting scrubbing remains authoritative:
when accounting is disabled, both the promoted cost and scrubbed raw projection
omit spend information and the typed value is `0.0`.

The tail I/O belongs privately to `sdk.run_control.events`. Its private helper
reads `events.jsonl` backward in bounded blocks and returns only the final
valid `(seq, ts)` pair. It is not exported and does not create a public one-shot
last-event reader. `read_run_events` and `tail_run_events` retain their existing
history and streaming contracts.

The probe observes an append-only snapshot, not a quiescent stream: an event
may be appended after its EOF observation. It skips empty, partial, malformed,
decode-invalid, and shape-invalid trailing lines in favor of the preceding
valid event. Missing, empty, unreadable, or wholly invalid evidence returns
`(None, None)` without raising from `load_status`.

`last_event_ts` is observation data, not a staleness or hung-run policy. The
SDK defines no age threshold, clock comparison, polling cadence, or automatic
intervention; each client chooses those policies explicitly.

The current MCP status adapter does not mirror these promoted scalar fields in
its own wire model. This core SDK slice therefore needs no companion MCP schema
or contract change.

## Consequences

- SDK and CLI consumers can render spend without scraping `raw_metrics`.
- Clients can obtain the final observed event position without materialising
  event history.
- A concurrent writer may make a later status call observe a newer event; this
  is expected append-only race semantics.
- Clients needing full event payloads continue to use `read_run_events` or
  `tail_run_events`; the private probe is intentionally not a replacement API.

## Rejected alternatives

1. **A public `get_last_event` API.** Rejected because `RunStatus` is the
   public snapshot owner and a second reader would duplicate status semantics.
2. **Read all events in `load_status`.** Rejected because liveness needs only
   one position, not an O(file-size) history allocation.
3. **Recalculate cost from phase records.** Rejected because it could diverge
   from normalized accounting and bypass the existing scrub policy.
4. **Embed a stale-run threshold in the SDK.** Rejected because acceptable age
   depends on each client's execution and operator policy.
