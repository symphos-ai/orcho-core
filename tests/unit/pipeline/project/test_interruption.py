from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core.observability import events
from pipeline.project import interruption
from pipeline.project.interruption import persist_interruption


def test_persist_interruption_preserves_handoff_and_emits_once(tmp_path: Path) -> None:
    events.init_event_store(tmp_path)
    session = {
        "status": "running",
        "phase_handoff": {"id": "handoff-1", "phase": "review_changes"},
    }

    assert persist_interruption(tmp_path, session) is True
    assert persist_interruption(tmp_path, session) is False

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["status"] == "interrupted"
    assert meta["phase_handoff"]["id"] == "handoff-1"
    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["run.interrupted"]


def test_persist_interruption_keeps_terminal_session_unchanged(tmp_path: Path) -> None:
    events.init_event_store(tmp_path)
    session = {"status": "done", "phase_handoff": {"id": "h"}}

    assert persist_interruption(tmp_path, session) is False
    assert session == {"status": "done", "phase_handoff": {"id": "h"}}
    assert not (tmp_path / "meta.json").exists()


def test_sigterm_persists_artifacts_before_child_exits(tmp_path: Path) -> None:
    code = """
import sys, time
from pathlib import Path
from core.observability import events
from pipeline.project.interruption import install_interrupt_handlers
run_dir = Path(sys.argv[1])
events.init_event_store(run_dir)
session = {\"status\": \"running\", \"phase_handoff\": {\"id\": \"h1\"}}
install_interrupt_handlers(run_dir, session)
print(\"ready\", flush=True)
time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ready"
    os.kill(child.pid, 15)
    assert child.wait(timeout=3) != 0

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["status"] == "interrupted"
    assert meta["phase_handoff"]["id"] == "h1"
    emitted = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in emitted] == ["run.interrupted"]


class TestSignalHandlerConstraints:
    """A handler runs inside the frame it interrupted, and the pipeline is not
    always the main thread. Both constraints are load-bearing."""

    def test_handler_defers_the_event_off_the_locked_append_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Appending an event takes an exclusive lock on ``events.jsonl``. If the
        interrupted frame already holds it, emitting from the handler would wait
        for a release that can only happen after the handler returns."""
        emitted: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            interruption.events, "emit",
            lambda kind, **payload: emitted.append((kind, payload)),
        )
        session = {"status": "running"}

        assert interruption.persist_interruption(
            tmp_path, session, reason="signal:15", defer_event=True,
        ) is True
        assert emitted == [], "the signal path must not touch the events lock"
        assert session["status"] == "interrupted"
        assert session["halt_reason"] == "signal:15"

        interruption._flush_deferred_event()

        assert [kind for kind, _payload in emitted] == ["run.interrupted"]
        assert emitted[0][1]["reason"] == "signal:15"

    def test_deferred_event_is_emitted_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        emitted: list[str] = []
        monkeypatch.setattr(
            interruption.events, "emit", lambda kind, **_payload: emitted.append(kind),
        )
        interruption.persist_interruption(
            tmp_path, {"status": "running"}, reason="signal:15", defer_event=True,
        )

        interruption._flush_deferred_event()
        interruption._flush_deferred_event()

        assert emitted == ["run.interrupted"]

    def test_install_reports_failure_off_the_main_thread(self, tmp_path: Path) -> None:
        """Embedders run the pipeline inside a worker thread (in-process typed
        runs use ``asyncio.to_thread``). ``signal.signal`` is main-thread only,
        so installation must degrade to the atexit fallback, not raise."""
        outcome: list[bool] = []

        def _install() -> None:
            outcome.append(
                interruption.install_interrupt_handlers(tmp_path, {"status": "running"}),
            )

        worker = threading.Thread(target=_install)
        worker.start()
        worker.join()

        assert outcome == [False]

    def test_install_succeeds_on_the_main_thread(self, tmp_path: Path) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        try:
            assert interruption.install_interrupt_handlers(
                tmp_path, {"status": "running"},
            ) is True
            assert signal.getsignal(signal.SIGTERM) is not previous
        finally:
            signal.signal(signal.SIGTERM, previous)


class TestExitPaths:
    """The two registered callables are the product surface: what the signal
    handler does when it fires, and what atexit does when it does not."""

    def test_installed_handler_persists_then_exits_with_the_signal_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        emitted: list[str] = []
        monkeypatch.setattr(
            interruption.events, "emit", lambda kind, **_payload: emitted.append(kind),
        )
        session = {"status": "running", "phase_handoff": {"id": "h1"}}
        previous = signal.getsignal(signal.SIGTERM)
        try:
            assert interruption.install_interrupt_handlers(tmp_path, session) is True
            handler = signal.getsignal(signal.SIGTERM)

            with pytest.raises(SystemExit) as exit_info:
                handler(signal.SIGTERM, None)
        finally:
            signal.signal(signal.SIGTERM, previous)

        assert exit_info.value.code == 128 + signal.SIGTERM
        assert session["status"] == "interrupted"
        assert session["halt_reason"] == f"signal:{signal.SIGTERM}"
        assert session["phase_handoff"] == {"id": "h1"}
        assert emitted == [], "the handler must leave the event to the exit path"

        interruption._flush_deferred_event()
        assert emitted == ["run.interrupted"]

    def test_atexit_fallback_persists_and_flushes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        registered: list = []
        monkeypatch.setattr(interruption.atexit, "register", registered.append)
        emitted: list[str] = []
        monkeypatch.setattr(
            interruption.events, "emit", lambda kind, **_payload: emitted.append(kind),
        )
        session = {"status": "running"}
        interruption.register_interruption_fallback(tmp_path, session)

        assert registered, "atexit fallback was not registered"
        registered[0]()

        assert session["status"] == "interrupted"
        assert session["halt_reason"] == "interrupted"
        assert emitted == ["run.interrupted"]
        assert json.loads((tmp_path / "meta.json").read_text())["status"] == "interrupted"

    def test_a_settled_run_is_never_re_marked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A run that finished normally must not be rewritten as interrupted by
        a late exit hook."""
        emitted: list[str] = []
        monkeypatch.setattr(
            interruption.events, "emit", lambda kind, **_payload: emitted.append(kind),
        )
        session = {"status": "done"}

        assert interruption.persist_interruption(tmp_path, session) is False
        assert session["status"] == "done"
        assert emitted == []
