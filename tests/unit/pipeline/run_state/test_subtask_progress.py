"""Unit tests for the pure partial-subtask-DAG progress detector."""
from __future__ import annotations

from pipeline.run_state import (
    unfinished_subtask_ids,
    unfinished_subtask_ids_in_run_dir,
)


def _start(sid: str) -> dict:
    return {"kind": "subtask.start", "payload": {"subtask_id": sid}}


def _end(sid: str, *, ok: bool = True) -> dict:
    return {"kind": "subtask.end", "payload": {"subtask_id": sid, "ok": ok}}


def test_fresh_run_with_no_subtask_events_is_inert() -> None:
    assert unfinished_subtask_ids([]) == set()
    assert unfinished_subtask_ids(
        [{"kind": "run.start", "payload": {}},
         {"kind": "phase.start", "payload": {"phase": "implement"}}]
    ) == set()


def test_five_starts_four_ends_flags_the_unfinished_id() -> None:
    events = [
        _start("T1"), _end("T1"),
        _start("T2"), _end("T2"),
        _start("T3"), _end("T3"),
        _start("T4"), _end("T4"),
        _start("T5"),  # started, no end
    ]
    assert unfinished_subtask_ids(events) == {"T5"}


def test_failed_end_ok_false_is_unfinished() -> None:
    events = [_start("T1"), _end("T1", ok=False)]
    assert unfinished_subtask_ids(events) == {"T1"}


def test_end_without_ok_field_is_unfinished() -> None:
    events = [
        _start("T1"),
        {"kind": "subtask.end", "payload": {"subtask_id": "T1"}},
    ]
    assert unfinished_subtask_ids(events) == {"T1"}


def test_retry_success_clears_unfinished_last_write_wins() -> None:
    events = [
        _start("T5"),                 # first attempt: started
        _end("T5", ok=False),         # first attempt: incomplete
        _start("T5"),                 # retry: re-started
        _end("T5", ok=True),          # retry: succeeded -> last write wins
    ]
    assert unfinished_subtask_ids(events) == set()


def test_restart_without_finish_after_success_reflags() -> None:
    events = [
        _start("T5"), _end("T5", ok=True),  # succeeded
        _start("T5"),                       # re-started, not finished
    ]
    assert unfinished_subtask_ids(events) == {"T5"}


def test_events_without_subtask_id_are_ignored() -> None:
    events = [
        {"kind": "subtask.start", "payload": {}},
        {"kind": "subtask.end", "payload": {"subtask_id": ""}},
        {"kind": "subtask.start", "payload": {"subtask_id": 123}},
    ]
    assert unfinished_subtask_ids(events) == set()


def test_run_dir_reader_folds_events_jsonl(tmp_path) -> None:
    from core.observability.events import append_event

    append_event(tmp_path, "subtask.start", {"subtask_id": "T1"})
    append_event(tmp_path, "subtask.end", {"subtask_id": "T1", "ok": True})
    append_event(tmp_path, "subtask.start", {"subtask_id": "T2"})  # no end

    assert unfinished_subtask_ids_in_run_dir(tmp_path) == {"T2"}


def test_run_dir_reader_missing_stream_is_empty(tmp_path) -> None:
    assert unfinished_subtask_ids_in_run_dir(tmp_path) == set()
