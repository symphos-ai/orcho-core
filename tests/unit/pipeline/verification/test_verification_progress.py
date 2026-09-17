"""Unit tests for pipeline/verification_progress.py (ADR 0190).

The aggregator turns raw ``(stream_label, bytes)`` chunks into a coalesced,
self-bounded ``gate.progress`` stream. These pin the decode/bounding/coalescing
contract (acceptance D — interleaved streams, no-newline chunks, split/invalid
bytes) and the best-effort publication guarantee, all without a real subprocess.
"""
from __future__ import annotations

from pipeline import verification_progress as vp
from pipeline.verification_progress import (
    GateProgressAggregator,
    GateProgressContext,
    GateProgressRecord,
    build_gate_progress_context,
    new_invocation_id,
)


class _Clock:
    """Manually advanced monotonic clock for deterministic coalescing."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _capture(monkeypatch) -> list[dict]:
    """Patch the durable emit so tests inspect coalesced payloads directly."""
    emitted: list[dict] = []

    def fake_emit(kind, **payload):
        emitted.append({"kind": str(kind), **payload})

    monkeypatch.setattr(vp._events, "emit", fake_emit)
    return emitted


def _agg(monkeypatch, *, clock=None, presenter=None, **kw) -> GateProgressAggregator:
    ctx = build_gate_progress_context(
        invocation_id="inv-1", name="unit", hook="after_phase", phase="implement",
        presenter=presenter,
    )
    return GateProgressAggregator(ctx, clock=clock or _Clock(), **kw)


# ── identity ──────────────────────────────────────────────────────────────


def test_new_invocation_id_is_unique() -> None:
    assert new_invocation_id() != new_invocation_id()


# ── record bounding + payload projection ──────────────────────────────────


def test_record_bounds_tails_at_construction() -> None:
    record = GateProgressRecord(
        name="unit", invocation_id="i", hook="h", phase="p",
        started_at="t0", elapsed_s=1.0,
        stdout_tail="A" * (vp.TAIL_MAX + 500),
        stderr_tail="B" * (vp.TAIL_MAX + 500),
    )
    assert len(record.stdout_tail) == vp.TAIL_MAX
    assert len(record.stderr_tail) == vp.TAIL_MAX
    # Kept the *trailing* window (rolling tail), not the head.
    assert record.stdout_tail == "A" * vp.TAIL_MAX


def test_event_payload_carries_required_keys_and_omits_phase() -> None:
    record = GateProgressRecord(
        name="unit", invocation_id="i", hook="after_phase", phase="implement",
        started_at="t0", elapsed_s=2.5, has_output=True,
        last_output_at="t1", stdout_tail="out", stderr_tail="err",
        stdout_bytes=3, stderr_bytes=3,
    )
    payload = record.event_payload()
    assert payload["name"] == "unit"
    assert payload["invocation_id"] == "i"
    assert payload["elapsed_s"] == 2.5
    assert payload["hook"] == "after_phase"
    assert payload["stdout_tail"] == "out"
    assert payload["stderr_tail"] == "err"
    # phase rides on the top-level Event.phase, never in the payload.
    assert "phase" not in payload


def test_event_payload_omits_empty_optionals() -> None:
    record = GateProgressRecord(
        name="unit", invocation_id="i", hook="", phase="",
        started_at="t0", elapsed_s=0.0,
    )
    payload = record.event_payload()
    assert "hook" not in payload
    assert "last_output_at" not in payload
    assert "stdout_tail" not in payload
    assert "stderr_tail" not in payload
    assert payload["has_output"] is False


# ── decode: interleaved / no-newline / split / invalid (acceptance D) ──────


def test_interleaved_streams_keep_distinct_tails(monkeypatch) -> None:
    _capture(monkeypatch)
    agg = _agg(monkeypatch)
    agg.feed(vp.STDOUT, b"out-a")
    agg.feed(vp.STDERR, b"err-a")
    agg.feed(vp.STDOUT, b"out-b")
    snap = agg.snapshot()
    assert snap.stdout_tail == "out-aout-b"
    assert snap.stderr_tail == "err-a"
    assert snap.stdout_bytes == 10
    assert snap.stderr_bytes == 5


def test_no_newline_chunks_accumulate(monkeypatch) -> None:
    _capture(monkeypatch)
    agg = _agg(monkeypatch)
    agg.feed(vp.STDOUT, b"no")
    agg.feed(vp.STDOUT, b"newline")
    agg.feed(vp.STDOUT, b"here")
    assert agg.snapshot().stdout_tail == "nonewlinehere"


def test_split_multibyte_sequence_decodes_across_chunks(monkeypatch) -> None:
    _capture(monkeypatch)
    agg = _agg(monkeypatch)
    # "é" is 0xC3 0xA9 in utf-8; splitting it must not raise nor leak b'..'.
    agg.feed(vp.STDOUT, b"caf\xc3")
    partial = agg.snapshot().stdout_tail
    assert "b'" not in partial  # trailing partial byte is buffered, not repr'd.
    agg.feed(vp.STDOUT, b"\xa9 latte")
    full = agg.snapshot().stdout_tail
    assert full == "café latte"


def test_invalid_bytes_decode_lossily_without_repr(monkeypatch) -> None:
    _capture(monkeypatch)
    agg = _agg(monkeypatch)
    agg.feed(vp.STDERR, b"valid\xff\xfe tail")
    tail = agg.snapshot().stderr_tail
    assert "valid" in tail
    assert " tail" in tail
    assert "b'" not in tail


# ── has_output: no-output vs empty completed ──────────────────────────────


def test_has_output_false_until_a_byte_arrives(monkeypatch) -> None:
    _capture(monkeypatch)
    agg = _agg(monkeypatch)
    assert agg.snapshot().has_output is False
    assert agg.snapshot().last_output_at is None
    agg.feed(vp.STDOUT, b"")  # an empty chunk is not output.
    assert agg.snapshot().has_output is False
    agg.feed(vp.STDOUT, b"x")
    snap = agg.snapshot()
    assert snap.has_output is True
    assert snap.last_output_at is not None


# ── coalescing (rate + size) ──────────────────────────────────────────────


def test_first_output_emits_immediately_then_coalesces(monkeypatch) -> None:
    emitted = _capture(monkeypatch)
    clock = _Clock()
    agg = _agg(monkeypatch, clock=clock, min_interval_s=0.5)

    agg.feed(vp.STDOUT, b"first")  # first output always signals liveness.
    assert len(emitted) == 1

    # Within the interval and under the byte threshold: no new emit.
    clock.t += 0.1
    agg.feed(vp.STDOUT, b"more")
    assert len(emitted) == 1

    # Interval elapsed: emit.
    clock.t += 0.5
    agg.feed(vp.STDOUT, b"tick")
    assert len(emitted) == 2

    # Volume cannot bypass the interval.
    agg.feed(vp.STDOUT, b"z" * 600)
    assert len(emitted) == 2
    assert emitted[-1]["kind"] == "gate.progress"
    assert emitted[-1]["invocation_id"] == "inv-1"


def test_emit_initial_publishes_a_no_output_record(monkeypatch) -> None:
    """A silent gate is observable from the start: emit_initial publishes one
    has_output=False record before any bytes arrive (F1)."""
    emitted = _capture(monkeypatch)
    agg = _agg(monkeypatch)
    agg.emit_initial()
    assert len(emitted) == 1
    assert emitted[0]["has_output"] is False
    assert emitted[0]["name"] == "unit"
    assert emitted[0]["invocation_id"] == "inv-1"


def test_first_output_after_initial_forces_a_prompt_emit(monkeypatch) -> None:
    """After the initial record, the no-output → output transition emits at once
    even within the coalescing interval, so has_output flips promptly."""
    emitted = _capture(monkeypatch)
    clock = _Clock()
    agg = _agg(monkeypatch, clock=clock, min_interval_s=0.5)
    agg.emit_initial()
    assert len(emitted) == 1 and emitted[0]["has_output"] is False

    clock.t += 0.01  # well within the coalescing interval, tiny byte count.
    agg.feed(vp.STDOUT, b"x")
    assert len(emitted) == 2
    assert emitted[1]["has_output"] is True


def test_coalesced_events_never_carry_phase_in_payload(monkeypatch) -> None:
    emitted = _capture(monkeypatch)
    agg = _agg(monkeypatch)
    agg.feed(vp.STDOUT, b"hello")
    assert emitted and "phase" not in emitted[0]


def test_emit_passes_gate_phase_as_event_phase(monkeypatch) -> None:
    """The aggregator stamps the gate's phase via emit's event_phase override so
    Event.phase is correct even when the global phase context is cleared (F4)."""
    seen: list[dict] = []

    def fake_emit(kind, *, event_phase=None, **payload):
        seen.append({"event_phase": event_phase, **payload})

    monkeypatch.setattr(vp._events, "emit", fake_emit)
    agg = _agg(monkeypatch)  # context phase="implement"
    agg.emit_initial()
    assert seen and seen[0]["event_phase"] == "implement"


# ── best-effort publication ───────────────────────────────────────────────


def test_emit_failure_never_propagates(monkeypatch) -> None:
    def boom(kind, **payload):
        raise RuntimeError("durable store exploded")

    monkeypatch.setattr(vp._events, "emit", boom)
    agg = _agg(monkeypatch)
    # Must not raise: a storage failure can never break the command it describes.
    agg.feed(vp.STDOUT, b"data")


def test_presenter_failure_never_propagates(monkeypatch) -> None:
    _capture(monkeypatch)

    def bad_presenter(_record) -> None:
        raise RuntimeError("presenter exploded")

    agg = _agg(monkeypatch, presenter=bad_presenter)
    agg.feed(vp.STDOUT, b"data")  # must not raise.


def test_presenter_receives_bounded_record(monkeypatch) -> None:
    _capture(monkeypatch)
    seen: list[GateProgressRecord] = []
    agg = _agg(monkeypatch, presenter=seen.append)
    agg.feed(vp.STDOUT, b"visible")
    assert seen and seen[0].stdout_tail == "visible"
    assert seen[0].name == "unit"


# ── context helper ────────────────────────────────────────────────────────


def test_build_gate_progress_context_shape() -> None:
    ctx = build_gate_progress_context(
        invocation_id="i", name="lint", hook="after_phase", phase="implement",
    )
    assert isinstance(ctx, GateProgressContext)
    assert ctx.invocation_id == "i"
    assert ctx.name == "lint"
    assert ctx.presenter is None


def test_high_volume_cannot_bypass_interval(monkeypatch) -> None:
    emitted = _capture(monkeypatch)
    clock = _Clock()
    agg = _agg(monkeypatch, clock=clock)
    agg.emit_initial()
    for _ in range(1000):
        agg.feed(vp.STDOUT, b"x" * 8192)
    assert len(emitted) == 2
    assert len(agg.snapshot().stdout_tail) == vp.TAIL_MAX
    clock.t += 0.5
    agg.feed(vp.STDERR, b"latest")
    assert len(emitted) == 3
    assert emitted[-1]["stderr_tail"] == "latest"


def test_pending_output_published_without_another_write(monkeypatch) -> None:
    import threading

    seen = threading.Event()
    def present(record):
        if record.stderr_tail == "second":
            seen.set()
    agg = _agg(monkeypatch, clock=__import__("time").monotonic,
               presenter=present, min_interval_s=0.02)
    _capture(monkeypatch)
    agg.start()
    try:
        agg.feed(vp.STDOUT, b"first")
        agg.feed(vp.STDERR, b"second")
        assert seen.wait(2)
    finally:
        agg.close()


def test_close_flushes_partial_decoder_and_ignores_late_output(monkeypatch) -> None:
    emitted = _capture(monkeypatch)
    agg = _agg(monkeypatch)
    agg.feed(vp.STDOUT, b"first")
    agg.feed(vp.STDERR, b"incomplete\xc3")
    agg.close()
    assert emitted[-1]["stderr_tail"] == "incomplete�"
    count = len(emitted)
    agg.feed(vp.STDOUT, b"late")
    agg.close()
    assert len(emitted) == count
