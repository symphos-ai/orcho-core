# ADR 0164: Open operator-pause request timestamp

## Status

Accepted

## Context

An active phase handoff records why a run is waiting, but previously did not
record when that open pause began. Reopening the same handoff can occur during
resume and recovery, so a timestamp must not become a retry clock that is reset
by control-plane work.

Delivery and correction gates already persist their operator-decision context
under `meta.commit_delivery`; their time source is distinct from a phase-handoff
open transition.

## Decision

`meta.phase_handoff.requested_at` is an additive, offset-aware UTC ISO-8601
string assigned by `pipeline.run_state.handoff.request_active_handoff`, the
single owner of an active-pause open transition.

The transition applies these temporal and compatibility rules:

1. reopening an active payload with the same `handoff_id` preserves its
   existing non-empty `requested_at` string exactly;
2. opening a different `handoff_id` assigns a new UTC timestamp;
3. legacy payloads without the field remain readable, and readers expose
   `payload.get("requested_at") is None` rather than manufacturing a time;
4. bootstrap, verification recovery, and cross-project persistence copy or
   delegate the payload and never assign their own timestamp.

`DeliveryDecisionState.requested_at` is a separate additive read projection.
For a decidable delivery or correction gate it returns the valid durable
`meta.commit_delivery.decided_at` string verbatim; non-decidable, absent, or
malformed legacy contexts return `None`. It never reads filesystem mtimes or
the current clock.

This ADR creates no elapsed-time calculation, SLA, UI formatting, new artifact,
action value, or automatic decision policy. Clients may render elapsed time
from the durable timestamp according to their own presentation policy.

## Consequences

- Mono and cross active handoffs have one durable open-pause timestamp owner.
- Existing phase-handoff and delivery data remain backward-readable.
- SDK consumers receive typed additive fields without changing runtime signals
  or existing action vocabulary.
- `orcho-mcp` adapter/schema/registration and mock E2E validation are deferred
  to the companion repository. Before promotion, a transport that enumerates
  fields manually is a stop condition until it projects `requested_at`.

## References

- [Phase lifecycle](../architecture/phase_lifecycle.md)
- [SDK API reference](../reference/sdk_api.md)
- [ADR 0021](0021-public-sdk-boundary.md)
