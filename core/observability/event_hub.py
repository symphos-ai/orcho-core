"""Replay-first event delivery hub — ADR 0048 Phase B (primitive).

In-process fan-out + tail over ``core.observability.events`` for live
consumers (orcho-web reactive UI, orcho-mcp watch accelerator, future
embedders). Backlog drains from ``events.jsonl`` first; live tail
follows in seq order; recovery on subscription drop is always replay-
based — the in-memory buffer is best-effort, the durable file is the
source of truth.

This module owns one decision unique to the implementation slice:
**bounded per-subscriber queue with drop-oldest policy**. ADR 0048
open question #1 (backpressure policy) is closed here as the initial
default; the policy is allowed to flip in a follow-up without
breaking the subscribe contract because recovery is replay-based per
D4 (wake is not delivery). The other open question this slice
closes is #4 (tail mechanism): **same-process direct fan-out** from
:func:`core.observability.events.emit`. External appenders that go
through :func:`core.observability.events.append_event` (orcho-mcp
synthetic events) land on disk but do not fan out live — subscribers
see them on the next backlog drain. A file-watcher fallback is
explicit follow-up work, not part of this slice.

## ADR 0048 decisions, anchored verbatim

D1. **Durable source of truth is unchanged.** ``events.jsonl``
    remains the only durable run timeline. The hub does not introduce
    a second store. Every consumer's recovery path — restart,
    reconnect, replay from ``since_seq`` — resolves against the file,
    not against in-memory state. The append-only file is the
    contract; the hub is a window onto it.

D2. **Hub shape: in-process fan-out + tail, scoped to active runs.**
    Same process that calls ``emit(...)`` is the process that fans
    the event out to in-process subscribers. No cross-process bus,
    no external multiplex, no permanent in-memory backlog.

D3. **Subscribe API surface (design intent, not signature lock).**
    ``subscribe(since_seq) -> AsyncIterator[Event]``: backlog drain
    from disk in seq order, then live tail in seq order, gap-free.
    Termination: caller breaks the loop OR the run ends OR the hub
    is closed.

D4. **"Wake is not delivery" — load-bearing rule.** Any push /
    notify / subscribe / file-watcher / OS-signal mechanism MAY wake
    a consumer. It NEVER delivers state. After any wake, the
    consumer advances its state through replay (list_events / hub
    backlog drain) with its persisted ``last_seq``. This rule makes
    half-state structurally unreachable when a notification is lost.

D5. **Bounded memory, no in-hub durability.** Per-subscriber queue
    is bounded; overflow drops the oldest in-memory event for that
    subscriber only. The durable file is untouched. A consumer
    detecting an out-of-order seq jump must resync via list_events.

D6. **Hub lifetime is bound to run lifetime.** The hub instance for
    a given run is alive while the run is active. ``close()``
    authorises tear-down. Post-close ``subscribe(...)`` still works
    — it resolves by draining the durable log to completion and
    returning a closed iterator. Termination is **explicit**: the
    hub is event-agnostic (see D2 / ADR open question #5), so a
    terminal ``run.end`` event does NOT auto-close the hub. The run
    lifecycle calls ``close_hub`` at finalization; that wiring is the
    downstream consumer's job, not this primitive's. A subscriber may
    return from its own loop on observing ``run.end`` if it wants to
    stop there.

D7. **Presentation policy stays out of the hub.** Event-store writes
    are unconditional per the silent-app-boundary invariant; the hub
    inherits the same unconditional fan-out. SILENT runs are
    first-class publishers.

D8. **Consumer differentiation lives in transport, not core.** Hub
    API is single-shape. Each downstream consumer chooses how to
    use it.

The full ADR lives at
``docs/adr/0048-replay-first-event-delivery-hub.md``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from core.observability.events import (
    Event,
    read_all,
    set_publish_hook,
)

logger = logging.getLogger(__name__)

# Per-subscriber queue capacity. Implementation default per ADR 0048
# open question #1 (closed as drop-oldest at this size). The policy
# is allowed to change without breaking the subscribe contract
# because recovery is replay-based per D4.
DEFAULT_QUEUE_SIZE = 1024

# Sentinel placed on a subscriber's queue by ``close()`` to break the
# live-tail loop without leaking past run-end. Identity-compared, never
# yielded as an event.
_CLOSE = object()


# Active hubs by run_id (== run_dir.name). At most one process-active
# hub exists at a time today (the events module is single-store), but
# the registry is keyed by run_id so a future multi-run host can drop
# this restriction without changing the public API.
_active_hubs: dict[str, RunEventHub] = {}


class _Subscriber:
    """One live-tail subscriber: a thread-bounded ingress buffer plus
    an asyncio wakeup, bound to its owning loop.

    Thread-safety (ADR 0048 review P1 #2). ``publish`` runs on
    whatever thread called :func:`core.observability.events.emit` —
    documented as possibly the ``agents.stream`` callback thread, and
    an ``asyncio.to_thread`` worker for the in-process async pilot.

    Bounded ingress (ADR 0048 review follow-up P1). The bound is
    applied at cross-thread INGRESS, in :meth:`offer`, under a
    ``threading.Lock``, via a ``collections.deque(maxlen=...)`` whose
    ``maxlen`` enforces drop-oldest the moment a producer appends —
    **before** the asyncio loop ever sees the item. ``offer`` then
    schedules **at most one** wakeup callback on the owning loop
    while a drain is pending. So a producer burst against a blocked
    or slow loop grows neither the buffer (capped at ``maxlen``) nor
    the loop's callback queue (capped at one pending wakeup). The
    earlier design scheduled one ``call_soon_threadsafe`` per event
    and only bounded later inside the loop callback — that retained
    every burst event in the loop's ready queue and defeated D5; this
    is the fix.

    The asyncio ``Event`` wakeup and the buffer drain
    (:meth:`drain`) both run on the owning loop thread; the producer
    thread only touches the deque + the schedule flag, both under the
    lock. ``Event.set`` / ``.clear`` are touched only on the loop
    thread (in :meth:`_signal` and :meth:`drain`).
    """

    __slots__ = (
        "loop", "_buf", "_lock", "_wakeup", "_wakeup_scheduled", "_run_id",
    )

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue_size: int,
        run_id: str,
    ) -> None:
        self.loop = loop
        # Bounded ingress buffer. ``maxlen`` drops the oldest item on
        # overflow (drop-oldest, D5) at append time on the producer
        # thread — the bound is in place before the loop is involved.
        self._buf: collections.deque = collections.deque(maxlen=queue_size)
        self._lock = threading.Lock()
        self._wakeup = asyncio.Event()
        self._wakeup_scheduled = False
        self._run_id = run_id

    def offer(self, item: object) -> None:
        """Thread-safe bounded ingress. May be called from any
        producer thread.

        Appends to the bounded deque under the lock (``maxlen``
        enforces drop-oldest immediately) and schedules AT MOST ONE
        wakeup callback on the owning loop until the consumer drains.
        Both the buffer and the loop callback queue stay bounded under
        a producer burst.
        """
        dropped: object = None
        with self._lock:
            will_drop = (
                self._buf.maxlen is not None
                and len(self._buf) == self._buf.maxlen
            )
            if will_drop:
                dropped = self._buf[0]  # the item maxlen is about to evict
            self._buf.append(item)  # drop-oldest via maxlen
            schedule = not self._wakeup_scheduled
            self._wakeup_scheduled = True
        if isinstance(dropped, Event):
            logger.warning(
                "RunEventHub(%s): subscriber ingress buffer full; "
                "dropped oldest event seq=%d (drop-oldest). Subscriber "
                "must resync via list_events / read_all after detecting "
                "the seq jump.",
                self._run_id, dropped.seq,
            )
        if schedule:
            # Loop closed/stopped — subscriber is gone. Durable file is
            # unaffected; a reconnect recovers via replay (D4).
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._signal)

    def _signal(self) -> None:
        """Runs ON the owning loop thread. Clears the schedule flag so
        the next burst schedules a fresh wakeup, then wakes the
        consumer."""
        with self._lock:
            self._wakeup_scheduled = False
        self._wakeup.set()

    async def drain(self) -> list:
        """Await items, then return the whole buffered batch in seq
        order. Runs on the owning loop.

        Returns a list (possibly containing the ``_CLOSE`` sentinel as
        its last element). A spurious wakeup with an empty buffer
        returns ``[]`` — the caller simply re-awaits.
        """
        await self._wakeup.wait()
        with self._lock:
            batch = list(self._buf)
            self._buf.clear()
            self._wakeup.clear()
        return batch


class RunEventHub:
    """In-process fan-out + tail for one active run (ADR 0048 D2).

    Subscribers drain ``events.jsonl`` backlog first (D3 phase 1)
    then receive live appended events in seq order (D3 phase 2).
    The hub is created via :func:`get_or_open_hub` so the registry
    knows to fan emit() events here; constructing directly is
    allowed but bypasses live fan-out.

    Lifecycle (D6). The hub is event-agnostic — it does NOT sniff
    event kinds (ADR open question #5), so it does **not** auto-close
    on a terminal ``run.end`` event. Termination is via an explicit
    :meth:`close` call, which the run lifecycle is expected to make at
    run-end finalization (downstream wiring, not this primitive).
    A subscriber that wants to stop on ``run.end`` returns from its
    own loop on seeing it; a subscriber that does not return stays
    live until :meth:`close`. ``close()`` is idempotent.

    Concurrency. ``publish`` may be called from any producer thread
    (the events module is multi-threaded); ``subscribe`` runs on an
    asyncio loop. A ``threading.Lock`` guards the subscriber set + the
    closed flag so registration is atomic against ``close`` /
    ``publish``. Per-subscriber delivery is marshalled onto each
    subscriber's owning loop (see :class:`_Subscriber`).
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._run_id = self._run_dir.name
        self._subscribers: set[_Subscriber] = set()
        self._closed = False
        self._queue_size = queue_size
        # Guards ``_subscribers`` + ``_closed``. Held only for the
        # set snapshot / membership mutation — never across the
        # thread-safe ``offer`` (which schedules onto a loop), so no
        # lock-ordering risk against the events module lock.
        self._lock = threading.Lock()

    # ── properties (read-only views) ──────────────────────────────

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscriber_count(self) -> int:
        """Number of active live-tail subscribers. Backlog-only
        iterators (created after close, or that have not yet
        reached the live phase) do not count."""
        with self._lock:
            return len(self._subscribers)

    # ── public surface (ADR 0048 D3) ──────────────────────────────

    async def subscribe(
        self,
        since_seq: int = 0,
    ) -> AsyncIterator[Event]:
        """Yield every event with ``seq > since_seq``, in seq order.

        Two phases per D3:
          1. Backlog drain — read every persisted event past
             ``since_seq`` from ``events.jsonl`` in seq order.
          2. Live tail — receive newly-published events through the
             per-subscriber queue; yield in seq order. Stops when
             :meth:`close` is called or the caller breaks the loop.

        If the hub is already closed when ``subscribe`` is called,
        only phase 1 runs (D6 — post-run subscribe resolves by
        replay to completion).

        **Gap-free (ADR 0048 review P1 #1).** The subscriber queue is
        registered *before* the backlog drain, under the lock, atomic
        against ``close`` / ``publish``. So an event emitted while the
        drain is in flight is buffered on the queue (phase 2 sees it)
        even though it landed after ``read_all`` took its snapshot.
        Overlap between the disk snapshot and the queue is removed by
        the ``seq > last_yielded`` filter. The previous order
        (register after drain) had a window where such an event hit
        neither phase — fixed here.
        """
        last_yielded = since_seq
        loop = asyncio.get_running_loop()
        sub = _Subscriber(loop, self._queue_size, self._run_id)

        # Register BEFORE the drain so during-drain events are
        # buffered. The lock makes the closed-check + registration
        # atomic against close()/publish() running on another thread.
        with self._lock:
            if self._closed:
                drain_only = True
            else:
                self._subscribers.add(sub)
                drain_only = False

        if drain_only:
            # Hub already torn down — no live events will ever come.
            # Replay disk to completion (D6).
            for event in read_all(self._run_dir):
                if event.seq > since_seq:
                    yield event
            return

        try:
            # Phase 1: backlog drain.
            for event in read_all(self._run_dir):
                if event.seq > since_seq:
                    yield event
                    last_yielded = event.seq

            # Phase 2: live tail. Each ``drain`` returns the buffered
            # batch in seq order. If close() raced the drain, the
            # batch holds any during-drain events followed by the
            # _CLOSE sentinel, so we yield to completion then
            # terminate — never block past run-end.
            while True:
                for item in await sub.drain():
                    if item is _CLOSE:
                        return
                    if item.seq > last_yielded:
                        yield item
                        last_yielded = item.seq
        finally:
            with self._lock:
                self._subscribers.discard(sub)

    def publish(self, event: Event) -> None:
        """Fan out one event to all active live-tail subscribers.

        Called from the events module's :func:`emit` (inside its
        lock) after the durable write succeeds. May run on any
        producer thread; delivery to each subscriber is marshalled
        onto that subscriber's owning loop (thread-safe — review
        P1 #2). Closed hubs are no-ops.

        Per-subscriber bounded queue with **drop-oldest** policy
        (D5): if a subscriber's queue is full, the oldest event in
        their window is discarded to make room for the new one.
        The durable file is untouched. The slow subscriber MUST
        detect the resulting seq jump and resync via
        :func:`core.observability.events.read_all` (or
        ``sdk.list_events``) with its persisted ``last_seq``.
        """
        with self._lock:
            if self._closed:
                return
            subscribers = list(self._subscribers)
        # Offer OUTSIDE the lock — offer() only schedules onto a loop
        # (call_soon_threadsafe), so there is no lock-ordering risk
        # against the events module lock held by the emit() caller.
        for sub in subscribers:
            sub.offer(event)

    def close(self) -> None:
        """Authorize tear-down (D6).

        Future ``publish()`` calls become no-ops. Active live-tail
        subscribers receive a close sentinel; their iterators
        terminate cleanly. New ``subscribe()`` calls after close
        still get the full backlog drain from disk — the durable
        file is the only thing that matters after the run ends.

        Idempotent: calling close twice is a no-op.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub.offer(_CLOSE)


# ── registry (module-level) ───────────────────────────────────────


def get_or_open_hub(
    run_dir: Path,
    *,
    queue_size: int = DEFAULT_QUEUE_SIZE,
) -> RunEventHub:
    """Return the registered hub for ``run_dir``, opening one if
    needed.

    Returns the existing registry entry whether it is open OR closed
    (ADR 0048 review P2 #3). A *closed* hub gives a late consumer a
    drain-only iterator per D6 — it replays the durable log to
    completion and terminates. Run ids are unique timestamps, so a
    given run is opened once and never re-opened; returning a fresh
    *open* hub for a run that already ended would leave that late
    consumer blocked forever waiting for live events that will never
    come. The closed hub is the correct answer.

    Hubs created via this factory receive live fan-out from
    :func:`core.observability.events.emit`. Direct construction
    bypasses fan-out and is useful only for unit tests of the
    backlog-drain path.

    Memory: closed hubs linger in the registry so the drain-only
    contract holds. A long-lived host that wants to reclaim them
    after all consumers are done calls :func:`forget_hub`.
    """
    run_id = Path(run_dir).name
    existing = _active_hubs.get(run_id)
    if existing is not None:
        return existing
    hub = RunEventHub(run_dir, queue_size=queue_size)
    _active_hubs[run_id] = hub
    return hub


def close_hub(run_dir: Path) -> None:
    """Close the hub for ``run_dir`` (D6 tear-down) but KEEP it in
    the registry.

    Retention is deliberate (review P2 #3): a late
    :func:`get_or_open_hub` for the same run must return this *same
    closed* instance so the consumer gets a drain-only iterator, not
    a fresh open hub that would block forever. Use :func:`forget_hub`
    to actually evict a closed hub once no more consumers will arrive.

    No-op when no hub is registered. Idempotent (``close`` is too).
    """
    run_id = Path(run_dir).name
    hub = _active_hubs.get(run_id)
    if hub is not None:
        hub.close()


def forget_hub(run_dir: Path) -> None:
    """Evict the hub for ``run_dir`` from the registry, closing it
    first if still open.

    Memory-reclamation companion to :func:`close_hub`. A long-lived
    host (e.g. an embedding server that runs many pipelines in one
    process) calls this once a run's consumers are all done, so the
    closed-hub registry entry does not accumulate. After
    ``forget_hub``, a subsequent :func:`get_or_open_hub` for the same
    run id would mint a fresh hub — fine, because run ids are unique
    timestamps and a forgotten run is never re-driven.

    No-op when no hub is registered.
    """
    run_id = Path(run_dir).name
    hub = _active_hubs.pop(run_id, None)
    if hub is not None:
        hub.close()


def reset_registry_for_tests() -> None:
    """Clear every registered hub. Test-only helper — production
    code uses :func:`close_hub` (D6 tear-down, keeps drain-only) and
    :func:`forget_hub` (memory reclamation) keyed by ``run_dir``.

    Provided because the registry is module-level (D2: per-run, but
    process-wide for routing). Pytest fixtures call this in teardown
    so a hub from one test does not leak into the next.
    """
    while _active_hubs:
        _, hub = _active_hubs.popitem()
        hub.close()


# ── emit() integration ────────────────────────────────────────────


def _publish_to_active(run_dir: Path, event: Event) -> None:
    """Hook called from :func:`core.observability.events.emit` for
    every persisted event. Routes to the hub registered for
    ``run_dir.name`` if any.

    Zero-cost when no hub exists for the run: one dict lookup, None
    return. Guarded so a hub bug never breaks the emit path —
    :func:`emit` catches any exception we raise here and swallows
    it.
    """
    if run_dir is None:
        return
    hub = _active_hubs.get(run_dir.name)
    if hub is None:
        return
    hub.publish(event)


# Wire the hook on first import. Symmetric ``set_publish_hook(None)``
# is available via the events module for test isolation.
set_publish_hook(_publish_to_active)


__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "RunEventHub",
    "close_hub",
    "forget_hub",
    "get_or_open_hub",
    "reset_registry_for_tests",
]
