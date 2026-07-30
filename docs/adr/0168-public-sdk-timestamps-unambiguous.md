# ADR 0168 — Public SDK timestamps are unambiguous

- **Status:** Accepted
- **Date:** 2026-07-30
- **Relates to:** [ADR 0164](0164-open-operator-pause-requested-at.md), [ADR 0166](0166-run-status-spend-and-liveness.md), and [ADR 0021](0021-public-sdk-boundary.md)

## Context

The public SDK exposes timestamps from distinct durable sources.  Active
phase-handoff pauses already use an offset-aware UTC `requested_at`, but
historical event writers emitted naive local timestamps.  A public timestamp
without an offset cannot be compared safely to UTC or another machine's clock.

Delivery decision state is a projection of an existing durable value and must
not invent a time when its source is absent, malformed, or naive.

## Decision

Every non-`None` public `RunStatus.last_event_ts` is an offset-aware ISO-8601
timestamp.  The private SDK tail probe applies the sole normalization rule:

1. an aware event timestamp is returned byte-for-byte, including `Z`, offset
   spelling, and fractional precision;
2. a naive legacy event timestamp is interpreted as a wall clock in the local
   timezone of the machine reading it, then serialized with that numeric UTC
   offset; and
3. an unparseable event timestamp becomes `None` while the valid event `seq`
   remains the last observed position.

`meta.phase_handoff.requested_at` retains ADR 0164's offset-aware UTC open-pause
contract.  `DeliveryDecisionState.requested_at` publishes the durable
`meta.commit_delivery.decided_at` string only when it is valid and
offset-aware; it otherwise returns `None`.  Neither projection rewrites a
valid aware durable value.

The legacy event rule cannot recover the original writer's timezone after an
artifact moves between machines.  It intentionally preserves wall-clock
components under the reader's local offset rather than using mtime or guessing
another zone.

## Consequences

- Clients may parse each non-`None` promoted timestamp as an aware datetime.
- The public fields remain additive and unchanged in name, type, and default.
- `RunEvent.ts`, existing `events.jsonl` data, and the event writer are not
  migrated or rewritten.
- This decision adds no timestamp fields, clock policy, elapsed-time policy,
  historical artifact rewrite, or writer migration.

## References

- [SDK API reference](../reference/sdk_api.md)
- [ADR 0164](0164-open-operator-pause-requested-at.md)
- [ADR 0166](0166-run-status-spend-and-liveness.md)
