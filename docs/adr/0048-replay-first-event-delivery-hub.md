# ADR 0048: Replay-First Event Delivery Hub

Status: Proposed

## Context

Orcho already has a durable run timeline: `events.jsonl` plus the
persisted artifacts catalogued in
[run_artifacts.md](../reference/run_artifacts.md). ADR 0046 and
ADR 0047 made that timeline the formal contract — every event sink
write fires regardless of presentation policy, and stdout is no
longer a machine surface.

The event taxonomy itself is locked by
[event_registry.md](../reference/event_registry.md). What is
unspecified today is the **delivery layer** that gets those events
from the disk sink to a live consumer (orcho-web's reactive UI,
orcho-mcp's `orcho_run_watch` long-poll, a future local client,
third-party embedders). The current state:

* Snapshot reads go through `sdk.list_events(run_id, since_seq)`,
  which calls `core.observability.events.read_all(run_dir)` and
  filters by seq. Solid; works under SILENT; survives reconnects.
* Live progress is approximated by **polling**: orcho-mcp's
  `orcho_run_watch` re-reads the file at a 250ms interval until a
  trigger condition fires or the timeout expires. Works but burns a
  read per interval per run.
* No in-process notifier surface. Web cannot yet subscribe; MCP
  cannot yet skip the polling tick when nothing has changed.

The drift risk if this stays unspecified: each downstream invents
its own delivery shape (web reaches for WebSockets, MCP reaches for
file watchers, a third-party reaches for an in-memory pub/sub) and
each picks a slightly different recovery contract. The 250ms polling
ceiling becomes load-bearing for one consumer and a foot-gun for
another. The next person to add a consumer rediscovers the
"replay-first or push-first?" debate from scratch.

This ADR pins the answer once. **Events are delivered through the
durable log first; any push/notification mechanism is an
optimisation layered on top of the same `since_seq` cursor.** The
hub is an in-process fan-out + tail primitive over `events.jsonl`,
not a new durable store.

## Decisions

### D1. Durable source of truth is unchanged
`events.jsonl` remains the only durable run timeline. The hub does
not introduce a second store. Every consumer's recovery path —
restart, reconnect, replay from `since_seq` — resolves against the
file, not against in-memory state. The append-only file is the
contract; the hub is a window onto it.

This anchors on the existing primitives:

* `core.observability.events.emit(...)` writes the append.
* `core.observability.events.read_all(run_dir)` is the bulk read.
* `sdk.list_events(run_id, since_seq=..., limit=...)` is the public
  SDK read surface (used by MCP today).

The hub composes with these, never replaces them.

### D2. Hub shape: in-process fan-out + tail, scoped to active runs
The replay-first event hub lives inside the same process that
emits the events. Concretely: the orcho process (CLI, mcp server
embedding the runtime, or a future direct-library host) that calls
`emit(...)` is also the process that fans the same event out to
in-process subscribers.

The hub does NOT:

* serve subscribers across processes;
* multiplex over an external bus (Redis, Kafka, asyncio queues
  bound to a socket);
* buffer past events in memory beyond what a slow subscriber's
  cursor needs.

Subscribers receive events in the same seq order as the file. Slow
subscribers are reconciled by **catching up through replay**, not
by an in-memory backlog the hub maintains forever.

### D3. Subscribe API surface (design intent, not signature lock)
The hub exposes one async iterator entry point. Naming and exact
signature shape are intentionally not pinned by this ADR; the
implementation slice decides those. The semantic contract is:

```text
subscribe(run_id, since_seq) -> AsyncIterator[Event]
  1. Drain backlog: every event with seq > since_seq currently on
     disk, in seq order.
  2. Switch to live: every newly-appended event, in seq order,
     for as long as the iterator stays alive.
  3. Termination: caller breaks the loop OR the run ends OR the
     hub is told to drop the subscription.
```

The iterator never emits gaps. If the caller falls behind, events
queue (bounded; backpressure resolution per D5). If the caller
reconnects with a new `since_seq`, the backlog drain re-runs and
no event is lost.

`subscribe` is additive — `list_events` and the existing read
surface stay exactly as they are. A consumer that does not want
push semantics never imports the hub.

### D4. "Wake is not delivery" — the load-bearing rule
Push, notify, and subscribe primitives may **wake** a consumer.
They never **deliver** state.

When a consumer receives any wake signal — an event from the
subscriber iterator, a `notifications/progress` over MCP, a
`resources/updated` notification, a file-watcher fire, a process-
level signal — it MUST advance its state through the same replay
path it would use after a cold reconnect:

```text
wake received
  → advance cursor against events.jsonl (via list_events or
    equivalent)
  → optionally refresh artifacts (meta.json, metrics.json,
    evidence.json) for the bodies events reference
  → update its own state with the highest seen seq
```

This rule exists because every consumer (web, MCP, future clients)
will at some point lose a notification — a dropped websocket, a
restart between MCP polls, a file watcher kernel event coalesced
under load. The replay path is the only one that survives those
losses. If a wake signal IS treated as delivery, the consumer ends
up in a half-state that needs a full reload to recover; the rule
makes that scenario structurally unreachable.

This is not new — orcho-mcp's
`docs/architecture/observation_delivery.md`
already documents it on the MCP side. This ADR promotes it to a
core-level invariant that every consumer obeys.

### D5. Bounded memory, no in-hub durability
The hub is allowed to keep an in-memory buffer per active
subscription — large enough that a momentarily slow subscriber
does not lose its position, small enough that an indefinitely slow
subscriber does not OOM the process.

The exact bound, backpressure policy (drop oldest? close
subscription? force replay?), and the relationship to the
subscribe iterator's queueing are **open question** for the
implementation slice (see § Open questions). The invariant pinned
here is: **no event is durable in memory.** A consumer that drops
its subscription and reconnects later replays from `events.jsonl`,
period. The hub does not become a second source of truth.

### D6. Hub lifetime is bound to run lifetime
The hub instance for a given `run_id` is alive while the run is
active. Run start opens it; `run.end` (the terminal event)
authorises its tear-down. After tear-down, `subscribe(run_id, ...)`
for that run resolves by replaying the durable log to completion
and returning a closed iterator immediately — the contract still
works post-run, it just doesn't wait for live events that will
never come.

This avoids a permanent in-process pub/sub fixture and matches the
run-scoped artifact lifecycle already documented in run_artifacts.

### D7. Presentation policy stays out of the hub
The hub does not consult `PresentationPolicy.{TERMINAL, SILENT}`.
Event-store writes are unconditional per ADR 0046 stop #9, so the
hub fans them out unconditionally too. SILENT runs are first-class
hub publishers; the only difference between TERMINAL and SILENT
from the hub's perspective is nothing.

### D8. Consumer differentiation lives in transport, not core
The hub's API is single-shape (one subscribe + the existing
`list_events`). Each downstream consumer chooses how to use it
based on its own transport's strengths:

| Consumer | How it uses the hub | Why |
|---|---|---|
| **orcho-web** | Subscribe-driven reactive projection. Web view opens with `run_id` + `last_seq`, replays via `list_events`, then subscribes to live tail and reduces events into view-model state incrementally. Recovery on browser refresh: same replay path. | Browser UI has a long-lived process that can hold an asyncio subscription; reactive shape minimises full-page polls. |
| **orcho-mcp** | Polling-first reliable path (per `observation_delivery.md`) is the wire contract; the hub is an **accelerator** that wakes `orcho_run_watch`'s loop when the run's event file actually advances, replacing fixed-interval re-reads with on-event wakes. The MCP wire shape — `since_seq` cursor on `events_tail` / `events_summary` / `watch` — does NOT change. | MCP clients may reconnect; the polling cursor is the only thing guaranteed to survive. The hub is an internal speedup, not a wire change. |
| **CLI** | Does not consume the hub. Terminal transcript stays a presentation layer on the producer side; CLI users read the file directly if they want machine state. | Terminal output is human, not machine. |
| **SDK / offline tools** | Continue to use `sdk.list_events` for full replay; no live tail needed. | Offline consumers do batch reads. |
| **Future third-party embedder** | Either subscribe (long-lived process) or replay-poll (short-lived), at the embedder's discretion. The hub does not require subscription. | Open extension point with no transport assumptions. |

Cross-process clients (a future browser frontend talking to a
remote orcho host, an out-of-process worker pool) need a
transport-specific bridge layered on top of the in-process hub.
That bridge is NOT in scope for this ADR; see § Non-goals.

## Architecture target

```
┌──────────────────────────────────────────────────────────────────┐
│  core/observability/events.py — emit() / read_all()              │
│  events.jsonl                                                    │
│  ↑ source of truth; append-only; survives every restart          │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         │ append → fan-out
                         ▼
┌──────────────────────────────────────────────────────────────────┐
│  Replay-first event hub (in-process; per-run; bounded memory)    │
│    subscribe(run_id, since_seq) → AsyncIterator[Event]           │
│      1. drain backlog from events.jsonl                          │
│      2. switch to live tail                                      │
│      3. emit in seq order; no gaps                               │
│    "wake is not delivery"                                        │
└────────────┬──────────────┬────────────────┬────────────────────┘
             │              │                │
             ▼              ▼                ▼
       ┌──────────┐  ┌──────────────┐  ┌────────────────┐
       │ orcho-   │  │ orcho-mcp    │  │ third-party    │
       │ web      │  │ watch loop   │  │ embedder       │
       │ (sub-    │  │ (wake +      │  │ (sub OR poll;  │
       │  scribe) │  │  cursor      │  │  free choice)  │
       │          │  │  replay)     │  │                │
       └────┬─────┘  └──────┬───────┘  └──────┬─────────┘
            │               │                 │
            │ + lazy artifact reads on event references
            ▼               ▼                 ▼
         meta.json / metrics.json / evidence.json / phase artifacts
```

Invariants this diagram pins:

1. Every consumer arrow ultimately resolves through
   `events.jsonl`. Wake signals optimise, never replace.
2. The hub is leaf — it does not import transport-specific
   primitives (no FastMCP, no Streamlit, no socket).
3. Artifact reads stay lazy and stay on the consumer side; the
   event stream carries references, not bodies.

## Wake-is-not-delivery, concretely

A consumer that respects this rule looks like:

```text
last_seq = <persisted or 0>

async for event in hub.subscribe(run_id, last_seq):
    # 'event' is delivery in the subscribe path — fine.
    state.apply(event)
    last_seq = event.seq

# But ANY wake from outside subscribe (notifications/progress,
# resources/updated, file-watcher fire, OS signal):
on_wake_from_anything():
    new_events = sdk.list_events(run_id, since_seq=last_seq)
    for event in new_events:
        state.apply(event)
        last_seq = event.seq
```

The subscribe iterator IS delivery within its own contract — it
guarantees seq ordering and no gaps. The rule applies to
**out-of-band wakes** that the subscribe iterator isn't responsible
for (notifications, OS-level signals, file-watcher fires). Those
must always loop through `list_events` (or equivalent replay) with
the persisted `last_seq` cursor.

A consumer that violates the rule — e.g. updates UI state directly
from an MCP `notifications/progress` payload without re-reading
events — will drift into a half-state whenever a notification is
lost. The rule makes that drift structurally unreachable.

## Non-goals

* **Not a persistent event store outside `events.jsonl`.** The hub
  is in-memory fan-out only.
* **Not cross-process pub/sub.** A future orcho-web served by a
  remote orcho host needs a transport bridge (HTTP SSE, WebSocket,
  gRPC stream — pick one in a separate ADR) layered on top of the
  in-process hub. That bridge is out of scope here.
* **Not a queue with durability guarantees.** Backpressure is local
  to one process; the durability guarantee belongs to
  `events.jsonl` only.
* **Not an event-vocabulary expansion.** Event kinds + payloads are
  governed by `event_registry.md` and the gap register. This ADR
  delivers existing events better; it does not add new ones.
* **Not a presentation-policy seam.** The hub is policy-agnostic;
  see D7.
* **Not a SDK contract break.** `sdk.list_events` keeps its current
  signature. The hub adds a new subscribe surface alongside it,
  does not replace.
* **Not browser-side state.** A web client may subscribe to the
  hub via a transport bridge, but the browser tab is not the
  source of truth; the file is.
* **Not an MCP wire change.** `orcho_run_watch` /
  `orcho_run_events_tail` / `orcho_run_events_summary` keep their
  `since_seq` cursor contract. The hub accelerates them
  internally; consumers see no schema drift.

## Open questions

Each is a candidate for a follow-up ADR or a decision in the
implementation slice. None of them block this ADR being Accepted —
they are explicitly deferred.

| # | Question | Why deferred |
|---|---|---|
| 1 | **Backpressure policy.** When a subscriber falls behind, does the hub drop the slow consumer, queue with a bound, or force the consumer back to replay? | Depends on real workload measurement; cheap to flip in a single module once one consumer hits the limit. |
| 2 | **Subscription handle shape.** Is `subscribe(...)` a context manager, a returnable handle, or an iterator with explicit cancel? Async generator semantics decide some of this. | Implementation detail; orthogonal to the contract. |
| 3 | **Multi-run multiplexing.** Does a single subscriber observe N runs through one call, or one call per run? | Premature optimisation until at least one consumer needs it. |
| 4 | **Tail mechanism.** File-watcher (inotify / FSEvents), tail-poll, or in-process direct fan-out from `emit(...)`? Same-process direct fan-out is cheapest but only works while the run is in the same process; external tail (e.g. another orcho process appending to the file) needs file-watcher. | Slice can ship same-process direct fan-out first; file-watcher is a strict addition. |
| 5 | **`agent.notice` / `run.notice` / `phase.notice` etc.** Some gap-register candidate events would naturally flow through the hub. Should the hub require them, or stay event-agnostic? | Hub stays event-agnostic per D2; the gap register decides those independently. |
| 6 | **Cross-run lifetime.** Cross-project runs spawn child project runs that emit their own events under their own run dir. Does the cross consumer subscribe to the cross hub, the per-alias child hubs, or both? | Depends on the consumer; the cross event taxonomy already carries `project_alias` tags so the multiplex is data-driven, but the exact subscribe shape is a slice decision. |
| 7 | **Subscription auth.** If a future remote bridge fronts the hub, who decides which `run_id` a caller may subscribe to? | Out of scope for the in-process primitive; belongs to the bridge ADR. |

## Stop conditions

Promote-to-Accepted requires:

1. No consumer's design depends on the hub becoming a durable
   store.
2. The wake-is-not-delivery rule appears verbatim in every
   downstream consumer doc (orcho-mcp `observation_delivery.md` —
   present; orcho-web `reactive_run_ui.md` — needs a pointer in the
   implementation phase).
3. The SDK `list_events` signature stays unchanged through the
   implementation slice.
4. No new event kind is added in the implementation slice — the
   hub delivers existing events. New events flow through the gap
   register, not through this ADR.
5. No cross-process bridge code lands in the implementation slice;
   that's an explicit follow-up ADR.

If any of those breaks during slice work, stop and surface — do
not silently widen the ADR scope.

## Phase plan (deferred — implementation slice decides)

This ADR is **design-only**. The phase plan below is a sketch for
the slice that will implement it; the slice gets its own ADR phase
table.

1. **Hub primitive.** `core/observability/event_hub.py` (name
   tentative). One module, one class, ~150 LoC. Backed by a small
   per-run queue + a tail mechanism (open question #4). Unit
   tests pin: backlog drain in seq order, live tail in seq order,
   reconnect from arbitrary `since_seq` is gap-free, run-end
   closes the iterator.

2. **MCP wake accelerator.** orcho-mcp's `observe/watch.py`
   replaces the 250ms re-read loop with a hub subscription that
   wakes the watch on event append. The `since_seq` wire shape and
   the `until` triggers stay unchanged; the `notifications/progress`
   contract stays unchanged. Parity test against the current
   polling loop.

3. **Web reactive projection.** orcho-web's run view subscribes
   to the hub via a transport bridge (Streamlit's reactive
   primitive or an SSE/WS shim — slice decides). Replay-first
   recovery on tab refresh, lazy artifact reads on event
   references.

4. **Cross hub.** If single-run hub design holds, cross-run
   consumers subscribe to the cross run's hub directly; per-alias
   child hubs are surfaced as multiplex when a consumer asks for
   them. Decision point: open question #6.

The implementation ADR opens with a Phase A pre-flight that
locks the hub module name + class shape against this ADR's
decisions.

## Reused existing code

This ADR composes against, does not duplicate:

* `core/observability/events.py::emit` / `read_all` — the durable
  layer the hub fans out from.
* `sdk/events.py::list_events` — the existing replay surface; hub
  consumers use this verbatim for out-of-band wake refresh.
* `docs/reference/event_registry.md` — the event vocabulary the
  hub delivers.
* `docs/reference/run_artifacts.md` — the persisted-body surface
  consumers read lazily on event references.
* ADR 0046 + 0047 stop #9 — event-sink writes never gated by
  presentation policy; the hub inherits the same invariant.

## Cross-links

* [event_registry.md](../reference/event_registry.md) — the event
  vocabulary the hub carries.
* [run_artifacts.md](../reference/run_artifacts.md) — the
  persisted-body surface event references point at.
* The reactive event delivery plan (internal planning record) — the
  implementation roadmap this ADR's design feeds.
* The stdout-to-event gap register (internal planning record) —
  candidate events that may eventually flow through the hub;
  governed independently.
* [ADR 0046](0046-silent-app-level-boundary.md) — silent app
  boundary; the file-sink-unconditional invariant the hub builds
  on.
* [ADR 0047](0047-cross-project-application-boundary.md) — cross
  boundary; same invariant inherited.
* orcho-mcp `docs/architecture/observation_delivery.md` —
  polling-first reliable path; the hub is its accelerator only.
* orcho-web `docs/architecture/reactive_run_ui.md` — reactive UI
  intent; the hub is the upstream contract this UI needs.

## Status table

| Phase | Status   | Commit    | Notes |
|-------|----------|-----------|-------|
| A     | Proposed | `PENDING` | ADR doc (this file). Design only. No code changes. Implementation phases B+ live in a follow-up ADR opened by the slice that lands the hub primitive. |
