"""Replay-first event delivery hub — ADR 0048 Phase B primitive tests.

Five test groups pin the load-bearing ADR 0048 decisions from the
consumer's side. Each group's docstring references the decision it
guards.

  1. **Replay from events.jsonl** (D1, D3 phase 1) — backlog drain
     resolves against the durable file, in seq order, gap-free, for
     any ``since_seq``.

  2. **Subscribe sees appended events** (D2, D3 phase 2) — same-
     process direct fan-out from :func:`emit` delivers live events
     to the subscriber's iterator in seq order.

  3. **Late subscriber replays to completion** (D6) — ``subscribe``
     after ``close()`` still drains the durable log and returns a
     closed iterator. The hub instance terminates with the run; the
     file survives.

  4. **Bounded in-memory window does not affect durable replay**
     (D5) — a subscriber that falls behind loses events from its
     in-memory queue (drop-oldest), but the durable file is
     untouched and a fresh subscribe with ``since_seq=0`` recovers
     the full timeline.

  5. **Wake/subscribe never replaces cursor advancement** (D4 —
     "wake is not delivery") — a subscriber that drops mid-stream
     and reconnects with its persisted ``last_seq`` receives every
     missed event from the durable log, regardless of what happened
     to the in-memory fan-out while it was disconnected.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from core.observability import events as evstore
from core.observability.event_hub import (
    DEFAULT_QUEUE_SIZE,
    RunEventHub,
    close_hub,
    forget_hub,
    get_or_open_hub,
    reset_registry_for_tests,
)

# ── fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Initialized events.jsonl ready for emit()."""
    rd = tmp_path / "run"
    evstore.init_event_store(rd)
    return rd


@pytest.fixture(autouse=True)
def _cleanup_registry():
    """Reset the hub registry + clear the publish hook between tests
    so module-level state never leaks. The hub module wires the hook
    on import; we re-install it after the test so the next one still
    has live fan-out wired."""
    yield
    reset_registry_for_tests()
    # Re-install the hook for the next test (reset_registry doesn't
    # touch it; explicit re-import is enough since the events module
    # holds the reference).
    from core.observability.event_hub import _publish_to_active
    evstore.set_publish_hook(_publish_to_active)


# Tests below use ``asyncio`` extensively; mark the whole module.
pytestmark = pytest.mark.asyncio


# ── 1. Replay from events.jsonl (D1, D3 phase 1) ──────────────────


async def test_backlog_drain_yields_persisted_events_in_seq_order(
    run_dir: Path,
) -> None:
    """``subscribe(since_seq=0)`` on a hub created AFTER the events
    landed still yields every persisted event in seq order — the
    backlog drain reads from the durable file, not from the hub's
    in-memory state. This is the load-bearing D1 invariant: the
    file is the source of truth."""
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    evstore.emit("phase.start", title="implement")
    evstore.emit("phase.end", title="implement", outcome="ok")

    hub = get_or_open_hub(run_dir)
    close_hub(run_dir)  # close so subscribe drains and exits, no live wait

    collected: list[evstore.Event] = []
    async for event in hub.subscribe(since_seq=0):
        collected.append(event)

    assert [e.kind for e in collected] == [
        "run.start", "phase.start", "phase.end",
    ]
    assert [e.seq for e in collected] == [1, 2, 3]


async def test_backlog_drain_respects_since_seq(run_dir: Path) -> None:
    """``since_seq`` filters the backlog. A consumer that already
    persisted ``last_seq=2`` only receives seq 3 onward — the
    contract that makes reconnect cursor-based per D4."""
    for kind in ("run.start", "phase.start", "phase.end", "run.end"):
        evstore.emit(kind, title="implement", outcome="ok")

    hub = get_or_open_hub(run_dir)
    close_hub(run_dir)

    collected = []
    async for event in hub.subscribe(since_seq=2):
        collected.append(event)

    assert [e.seq for e in collected] == [3, 4]


async def test_backlog_drain_empty_when_no_events(run_dir: Path) -> None:
    """Zero events on disk → iterator completes immediately with no
    items. Pins that the drain loop terminates cleanly on an empty
    file (not raising, not hanging)."""
    hub = get_or_open_hub(run_dir)
    close_hub(run_dir)

    collected = [e async for e in hub.subscribe(since_seq=0)]
    assert collected == []


# ── 2. Subscribe sees appended events (D2, D3 phase 2) ────────────


async def test_live_tail_receives_events_emitted_after_subscribe(
    run_dir: Path,
) -> None:
    """A subscriber that started BEFORE any emit() sees every
    subsequent emit() through the live-tail queue. Pins the D2
    fan-out wiring: emit() inside the events module's lock calls
    the hub's publish hook, which puts on each subscriber's queue."""
    hub = get_or_open_hub(run_dir)
    collected: list[evstore.Event] = []
    done = asyncio.Event()

    async def consumer():
        async for event in hub.subscribe(since_seq=0):
            collected.append(event)
            if event.kind == "run.end":
                done.set()
                return

    task = asyncio.create_task(consumer())
    # Let the consumer reach the live-tail wait. One sleep(0) yields
    # control once; the subscribe coroutine drains its (empty)
    # backlog and parks on queue.get().
    await asyncio.sleep(0)

    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    evstore.emit("run.end", status="done")

    await asyncio.wait_for(done.wait(), timeout=2.0)
    await task

    assert [e.kind for e in collected] == ["run.start", "run.end"]
    assert [e.seq for e in collected] == [1, 2]


async def test_live_tail_preserves_seq_order_across_subscribers(
    run_dir: Path,
) -> None:
    """Two subscribers on the same hub both observe the full live
    stream in identical seq order. Fan-out is not destructive."""
    hub = get_or_open_hub(run_dir)
    out_a: list[evstore.Event] = []
    out_b: list[evstore.Event] = []
    done_a = asyncio.Event()
    done_b = asyncio.Event()

    async def consumer(out: list, done: asyncio.Event):
        async for event in hub.subscribe(since_seq=0):
            out.append(event)
            if event.kind == "run.end":
                done.set()
                return

    task_a = asyncio.create_task(consumer(out_a, done_a))
    task_b = asyncio.create_task(consumer(out_b, done_b))
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # both consumers park on their queues

    for kind in (
        "run.start", "phase.start", "phase.end", "run.end",
    ):
        evstore.emit(
            kind, title="implement", outcome="ok", task="t",
            run_kind="single_project", project="/p",
            profile="advanced", status="done",
        )

    await asyncio.wait_for(done_a.wait(), timeout=2.0)
    await asyncio.wait_for(done_b.wait(), timeout=2.0)
    await task_a
    await task_b

    assert [e.kind for e in out_a] == [e.kind for e in out_b]
    assert [e.seq for e in out_a] == [1, 2, 3, 4]


async def test_emit_is_no_op_when_no_hub_registered(
    tmp_path: Path,
) -> None:
    """Pre-flight invariant: ``emit`` writes to disk normally even
    when the hub registry is empty (no consumer ever called
    ``get_or_open_hub``). Zero-overhead path is one dict lookup that
    returns None; the publish branch is skipped."""
    rd = tmp_path / "run_no_hub"
    evstore.init_event_store(rd)
    # Deliberately NOT calling get_or_open_hub. Registry empty.
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    events = evstore.read_all(rd)
    assert len(events) == 1
    assert events[0].kind == "run.start"


# ── 3. Late subscriber replays to completion (D6) ─────────────────


async def test_subscribe_after_close_drains_durable_log_then_exits(
    run_dir: Path,
) -> None:
    """A subscribe call on a closed hub still yields every event
    that landed on disk during the run, then terminates the iterator
    cleanly — no live wait. Pins D6: the durable file outlives the
    hub instance, and post-run subscribers resolve by replay."""
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    evstore.emit("phase.start", title="implement")
    evstore.emit("phase.end", title="implement", outcome="ok")
    evstore.emit("run.end", status="done")

    hub = get_or_open_hub(run_dir)
    close_hub(run_dir)
    assert hub.closed

    collected = [e async for e in hub.subscribe(since_seq=0)]
    assert [e.kind for e in collected] == [
        "run.start", "phase.start", "phase.end", "run.end",
    ]


async def test_close_terminates_active_live_subscribers(
    run_dir: Path,
) -> None:
    """An active subscriber blocked on ``queue.get()`` receives the
    close sentinel and exits its iterator. Pins the lifecycle
    teardown contract: ``close()`` does not leave orphaned
    coroutines."""
    hub = get_or_open_hub(run_dir)
    collected: list[evstore.Event] = []
    finished = asyncio.Event()

    async def consumer():
        async for event in hub.subscribe(since_seq=0):
            collected.append(event)
        finished.set()  # only fires when iterator terminates

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    # Subscriber receives run.start, then we close before run.end.
    close_hub(run_dir)
    await asyncio.wait_for(finished.wait(), timeout=2.0)
    await task

    # Subscriber got the event(s) that landed before close, then
    # exited cleanly on the sentinel.
    assert collected and collected[0].kind == "run.start"


# ── 4. Bounded memory does not affect durable replay (D5) ─────────


async def test_slow_subscriber_drops_in_memory_events_but_file_intact(
    run_dir: Path,
) -> None:
    """A subscriber that never drains its queue fills up; the
    drop-oldest policy discards from the in-memory window only. The
    durable file holds every event; a fresh subscribe with
    ``since_seq=0`` recovers the full timeline. This is the
    load-bearing D5 invariant — the hub is best-effort acceleration,
    the file is the contract."""
    # Tiny queue so we can overflow it deterministically.
    hub = RunEventHub(run_dir, queue_size=2)
    # Register manually so emit() routes to this hub.
    from core.observability import event_hub as _eh
    _eh._active_hubs[run_dir.name] = hub

    # Start a subscriber that drains the backlog (empty) then parks
    # on the live queue. We never read from it after the park, so
    # incoming events accumulate up to queue_size.
    async def stuck_subscriber():
        async for _event in hub.subscribe(since_seq=0):
            # Never consume — let the queue fill, then exit on close.
            await asyncio.sleep(60)
            return

    task = asyncio.create_task(stuck_subscriber())
    await asyncio.sleep(0)  # park on queue.get()

    # Emit 10 events; the subscriber's queue can only hold 2.
    for i in range(10):
        evstore.emit("agent.text", text=f"msg-{i}")

    # ✓ Durable file holds all 10 — drop-oldest does NOT touch it.
    persisted = evstore.read_all(run_dir)
    assert len(persisted) == 10, (
        f"durable file lost events during overflow: {len(persisted)} of 10. "
        f"This breaks D5: durable file is the source of truth."
    )

    # ✓ A fresh subscriber recovers the full timeline from disk.
    # Use a separate variable to keep the stuck subscriber alive
    # without leaking the iterator.
    fresh_hub = hub  # same hub, but fresh subscription
    close_hub(run_dir)  # close to make the fresh subscribe drain-only

    fresh_collected = []
    async for event in fresh_hub.subscribe(since_seq=0):
        fresh_collected.append(event)

    assert len(fresh_collected) == 10
    assert [e.seq for e in fresh_collected] == list(range(1, 11))

    # Cancel the original stuck subscriber so the test exits cleanly.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ── 5. Wake is not delivery (D4) ──────────────────────────────────


async def test_dropped_subscription_recovers_full_state_via_replay(
    run_dir: Path,
) -> None:
    """The "wake is not delivery" rule in executable form.

    Subscriber session A: reads through seq 2 then disconnects.
    Events 3 and 4 happen while A is gone — A's in-memory queue
    isn't even alive to receive them.
    Subscriber session B: reconnects with ``since_seq=2`` (the last
    seq A persisted). Receives 3 and 4 through the backlog drain.

    This is the D4 invariant: any wake mechanism (here, the
    consumer code choosing to reconnect) routes recovery through
    replay against the durable log, NOT through whatever in-memory
    state survived the disconnect. The hub's in-memory fan-out is
    optimization; the file is the contract."""
    hub = get_or_open_hub(run_dir)

    # ── Session A: receive seqs 1 and 2, then disconnect. ──
    session_a: list[evstore.Event] = []
    a_done = asyncio.Event()

    async def session_a_consumer():
        async for event in hub.subscribe(since_seq=0):
            session_a.append(event)
            if event.seq == 2:
                a_done.set()
                return  # explicit disconnect

    task_a = asyncio.create_task(session_a_consumer())
    await asyncio.sleep(0)
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    evstore.emit("phase.start", title="implement")
    await asyncio.wait_for(a_done.wait(), timeout=2.0)
    await task_a

    last_seq_a = session_a[-1].seq
    assert last_seq_a == 2

    # ── Events 3 + 4 happen — A's session no longer cares. ──
    # Session A's async generator may still hold its queue (until
    # GC + finally), but A's coroutine returned, so nobody is
    # draining it. The point of D4 is that B's recovery does NOT
    # depend on what happened to A's in-memory queue while A was
    # disconnected — B replays from the durable log.
    evstore.emit("phase.end", title="implement", outcome="ok")
    evstore.emit("run.end", status="done")

    # Close the hub to simulate run-end. Post-close subscribe is
    # drain-only.
    close_hub(run_dir)

    # ── Session B: reconnect with last_seq from A. ──
    # A wake-style mechanism would NOT have delivered seqs 3 and 4
    # to anyone in memory. The "wake is not delivery" rule means B
    # advances state via replay from the durable log.
    session_b = []
    async for event in hub.subscribe(since_seq=last_seq_a):
        session_b.append(event)

    assert [e.seq for e in session_b] == [3, 4], (
        "wake-is-not-delivery contract broken: reconnect with "
        f"last_seq={last_seq_a} did not recover events 3 and 4 "
        f"from the durable log. Got: {[(e.seq, e.kind) for e in session_b]!r}"
    )
    assert [e.kind for e in session_b] == ["phase.end", "run.end"]


# ── 6. Defensive: hub failure never breaks emit() (D1 corollary) ──


async def test_publish_hook_exception_does_not_break_emit(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the hub's publish raises (programmer error, runtime
    glitch), the durable write must still succeed. The hub is
    best-effort; the file is the contract."""
    hub = get_or_open_hub(run_dir)

    def _broken_publish(_event):
        raise RuntimeError("scripted hub failure")

    monkeypatch.setattr(hub, "publish", _broken_publish)

    # Should NOT raise, should still write to disk.
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    persisted = evstore.read_all(run_dir)
    assert len(persisted) == 1
    assert persisted[0].kind == "run.start"


# ── 7. Default queue size sanity ──────────────────────────────────


async def test_default_queue_size_is_reasonable() -> None:
    """The implementation default closes ADR 0048 open question #1.
    It is allowed to change in a follow-up without breaking the
    subscribe contract. This test pins the current value so changes
    are explicit and traceable in commits. (Async-marked because the
    module-level pytestmark is asyncio — the body is sync.)"""
    assert DEFAULT_QUEUE_SIZE == 1024


# ── 8. Review regressions (Codex P1/P2 findings) ──────────────────


async def test_event_emitted_during_backlog_drain_is_delivered(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review P1 #1 — registration gap.

    An event that lands AFTER ``read_all`` takes its snapshot but
    BEFORE the subscriber would have registered (old order) must
    still reach the subscriber. We reproduce the exact window by
    monkeypatching the hub's ``read_all`` to emit an extra event as
    a side effect, simulating an append during the drain. With the
    fix (register before drain) the during-drain event is buffered
    on the already-registered queue and delivered in phase 2; with
    the old order it hit neither phase and the subscriber timed out.
    """
    hub = get_or_open_hub(run_dir)
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )  # seq 1, on disk before subscribe

    import core.observability.event_hub as _eh
    real_read_all = _eh.read_all

    def read_all_then_emit(rd):
        snapshot = real_read_all(rd)
        # Simulate an append landing in the gap window: after the
        # snapshot is taken, before phase 2. Emitted exactly once.
        if not getattr(read_all_then_emit, "_fired", False):
            read_all_then_emit._fired = True
            evstore.emit("phase.start", title="implement")  # seq 2
        return snapshot

    monkeypatch.setattr(_eh, "read_all", read_all_then_emit)

    collected: list[evstore.Event] = []
    done = asyncio.Event()

    async def consumer():
        async for event in hub.subscribe(since_seq=0):
            collected.append(event)
            if event.seq == 2:
                done.set()
                return

    task = asyncio.create_task(consumer())
    await asyncio.wait_for(done.wait(), timeout=2.0)
    await task

    # Both the pre-existing backlog (seq 1) AND the during-drain
    # event (seq 2) were delivered — no gap.
    assert [e.seq for e in collected] == [1, 2]
    assert [e.kind for e in collected] == ["run.start", "phase.start"]


async def test_emit_from_worker_thread_reaches_subscriber(
    run_dir: Path,
) -> None:
    """Review P1 #2 — thread-safe fan-out.

    ``emit`` is documented as callable from non-loop threads (the
    ``agents.stream`` callback thread; an ``asyncio.to_thread`` worker
    for the in-process async pilot). ``asyncio.Queue`` is not
    thread-safe and ``put_nowait`` from another thread does not
    reliably wake a ``get()`` on the loop. The hub marshals every
    enqueue onto the subscriber's owning loop via
    ``call_soon_threadsafe``; this test pins that an event emitted
    from a worker thread is reliably delivered to a subscriber
    awaiting on the main loop within the timeout."""
    import threading

    hub = get_or_open_hub(run_dir)
    collected: list[evstore.Event] = []
    done = asyncio.Event()

    async def consumer():
        async for event in hub.subscribe(since_seq=0):
            collected.append(event)
            if event.kind == "run.end":
                done.set()
                return

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)  # park on queue.get()

    # Emit from a NON-loop thread — the regression scenario.
    def worker():
        evstore.emit(
            "run.start", task="t", run_kind="single_project",
            project="/p", profile="advanced",
        )
        evstore.emit("run.end", status="done")

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    await asyncio.wait_for(done.wait(), timeout=2.0)
    await task

    assert [e.kind for e in collected] == ["run.start", "run.end"]


async def test_get_or_open_hub_after_close_returns_drain_only_hub(
    run_dir: Path,
) -> None:
    """Review P2 #3 — registry post-close semantics.

    After ``close_hub``, a late consumer using the public registry
    path (``get_or_open_hub``) must get the SAME closed hub so its
    iterator is drain-only (D6) — not a fresh OPEN hub that would
    block forever waiting for live events that will never come."""
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    evstore.emit("run.end", status="done")

    hub = get_or_open_hub(run_dir)
    close_hub(run_dir)

    # Late consumer via the public registry path.
    hub2 = get_or_open_hub(run_dir)
    assert hub2 is hub, (
        "get_or_open_hub after close must return the SAME closed "
        "instance (drain-only), not a fresh open hub"
    )
    assert hub2.closed

    collected = [e async for e in hub2.subscribe(since_seq=0)]
    assert [e.kind for e in collected] == ["run.start", "run.end"]


async def test_forget_hub_evicts_so_next_open_is_fresh(
    run_dir: Path,
) -> None:
    """``forget_hub`` reclaims a closed registry entry; a subsequent
    ``get_or_open_hub`` mints a fresh (open) hub. Memory-reclamation
    companion to the drain-only retention of ``close_hub``."""
    hub = get_or_open_hub(run_dir)
    close_hub(run_dir)
    assert hub.closed

    forget_hub(run_dir)
    hub2 = get_or_open_hub(run_dir)
    assert hub2 is not hub
    assert not hub2.closed


async def test_offer_ingress_buffer_is_bounded_before_loop_drains() -> None:
    """Review follow-up P1 — cross-thread ingress must be bounded
    BEFORE the loop sees it.

    The earlier fix scheduled one ``call_soon_threadsafe(_enqueue,
    item)`` per event, so a producer burst against a loop that is
    blocked / draining / slower than the producer grew the loop's
    callback queue with one retained ``Event`` per call — the
    ``maxsize`` bound only bit later, inside the callback. That
    defeated D5 in exactly the slow-subscriber case.

    The fix bounds at ingress: ``offer`` appends to a
    ``deque(maxlen=queue_size)`` under a lock (drop-oldest at append
    time) and schedules AT MOST ONE wakeup while a drain is pending.
    This test bursts far past ``queue_size`` WITHOUT ever yielding to
    the loop (so no drain runs) and asserts the buffer stays capped
    and only a single wakeup callback was scheduled — i.e. memory is
    bounded at the producer boundary, not on the loop."""
    from core.observability.event_hub import _Subscriber

    loop = asyncio.get_running_loop()
    sub = _Subscriber(loop, queue_size=4, run_id="r")

    # Burst 100 events from this coroutine WITHOUT awaiting — the loop
    # never gets a turn to run the scheduled wakeup, so this models a
    # producer outrunning a blocked consumer.
    for i in range(100):
        evt = evstore.Event(
            seq=i, ts="t", kind="agent.text", phase=None, payload={},
        )
        sub.offer(evt)

    # ✓ Ingress buffer is capped at maxlen — memory does NOT grow with
    # the 100-event burst.
    assert len(sub._buf) == 4, (
        f"ingress buffer must be bounded to queue_size=4; "
        f"got {len(sub._buf)} — the bound leaked onto the loop"
    )
    # ✓ Drop-oldest kept the most recent 4.
    assert [e.seq for e in sub._buf] == [96, 97, 98, 99]
    # ✓ At most one wakeup callback was scheduled for the whole burst
    # (subsequent offers saw the flag set and did not re-schedule), so
    # the loop's callback queue did not grow with the burst either.
    assert sub._wakeup_scheduled is True


async def test_run_end_event_does_not_auto_close_hub(
    run_dir: Path,
) -> None:
    """Review P2 #4 — the hub is event-agnostic (ADR open question
    #5). A terminal ``run.end`` event does NOT auto-close the hub or
    terminate a subscriber that doesn't return on it. Termination is
    via explicit ``close()`` (the run lifecycle's job at finalization,
    not this primitive's). This pins the softened contract so a
    future change that makes the hub sniff ``run.end`` trips here and
    forces an ADR amendment rather than silent scope creep."""
    hub = get_or_open_hub(run_dir)
    collected: list[evstore.Event] = []
    saw_run_end = asyncio.Event()

    async def consumer():
        # Deliberately does NOT return on run.end — proves the hub
        # keeps the subscription live.
        async for event in hub.subscribe(since_seq=0):
            collected.append(event)
            if event.kind == "run.end":
                saw_run_end.set()

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    evstore.emit(
        "run.start", task="t", run_kind="single_project",
        project="/p", profile="advanced",
    )
    evstore.emit("run.end", status="done")
    await asyncio.wait_for(saw_run_end.wait(), timeout=2.0)

    # run.end did NOT auto-close the hub; subscriber is still live.
    assert not hub.closed
    assert hub.subscriber_count == 1

    # Explicit close terminates the subscriber cleanly.
    close_hub(run_dir)
    await asyncio.wait_for(task, timeout=2.0)
    assert hub.subscriber_count == 0
    assert [e.kind for e in collected] == ["run.start", "run.end"]
