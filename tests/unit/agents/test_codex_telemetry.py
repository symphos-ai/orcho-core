"""Tests for the Codex rollout telemetry parser.

Covers the layered rollout lookup, JSONL parsing, graceful degradation
on partial/malformed data, the today/yesterday hot-path window, and the
captured-id contract that lets ``find_codex_rollout`` resolve real Codex
session UUIDs regardless of which key-flavor the CLI emitted them under.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from agents.runtimes import codex_telemetry
from agents.runtimes.codex import _extract_codex_session_id
from agents.runtimes.codex_telemetry import (
    CodexTelemetrySnapshot,
    _candidate_ids,
    _date_dir,
    find_codex_rollout,
    load_codex_telemetry,
    parse_codex_rollout,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    codex_telemetry._ROLLOUT_PATH_CACHE.clear()
    yield
    codex_telemetry._ROLLOUT_PATH_CACHE.clear()


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set ``CODEX_HOME`` to a hermetic tmp dir; return its sessions root."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions


def _write_rollout(
    sessions_dir: Path,
    sid: str,
    events: list[dict],
    *,
    when: date | None = None,
    filename_id: str | None = None,
    header_id: str | None = None,
) -> Path:
    """Write a rollout-*.jsonl under ``sessions_dir`` for date ``when``.

    Defaults: ``when=date.today()``, ``filename_id=sid``, ``header_id=sid``.
    Prepends a ``session_meta`` line with ``payload.id == header_id``.
    """
    when = when or date.today()
    filename_id = filename_id if filename_id is not None else sid
    header_id = header_id if header_id is not None else sid
    day_dir = _date_dir(sessions_dir, when)
    day_dir.mkdir(parents=True, exist_ok=True)
    ts = "2026-05-19T10-30-43"
    path = day_dir / f"rollout-{ts}-{filename_id}.jsonl"
    lines: list[str] = [
        json.dumps({
            "timestamp": "2026-05-19T10:30:43.000Z",
            "type": "session_meta",
            "payload": {"id": header_id, "cwd": "/tmp"},
        }),
    ]
    for ev in events:
        lines.append(json.dumps(ev))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _token_count(
    *,
    window: int | None = None,
    input_tokens: int | None = None,
    rate_limits: dict | None = None,
) -> dict:
    info: dict = {}
    if window is not None:
        info["model_context_window"] = window
    if input_tokens is not None:
        info["last_token_usage"] = {"input_tokens": input_tokens}
    payload: dict = {"type": "token_count", "info": info}
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return {
        "timestamp": "2026-05-19T10:30:46.623Z",
        "type": "event_msg",
        "payload": payload,
    }


def _task_started(window: int | None = None) -> dict:
    payload: dict = {"type": "task_started"}
    if window is not None:
        payload["model_context_window"] = window
    return {
        "timestamp": "2026-05-19T10:30:46.470Z",
        "type": "event_msg",
        "payload": payload,
    }


# ── 1. Parses token_count with used + window ────────────────────────────────


def test_parses_token_count_with_used_and_window(codex_home: Path) -> None:
    _write_rollout(
        codex_home, "sid-1",
        [_token_count(window=258400, input_tokens=42000)],
    )
    snap = load_codex_telemetry("sid-1")
    assert snap is not None
    assert snap.context_window_tokens == 258400
    assert snap.context_used_tokens == 42000
    assert snap.context_remaining_tokens == 216400


# ── 2. Last event wins ──────────────────────────────────────────────────────


def test_last_event_wins(codex_home: Path) -> None:
    _write_rollout(
        codex_home, "sid-2",
        [
            _token_count(window=258400, input_tokens=10000),
            _token_count(window=258400, input_tokens=42000),
        ],
    )
    snap = load_codex_telemetry("sid-2")
    assert snap is not None
    assert snap.context_used_tokens == 42000


# ── 3. task_started window alone yields no used ─────────────────────────────


def test_task_started_window_alone_yields_no_used(codex_home: Path) -> None:
    _write_rollout(codex_home, "sid-3", [_task_started(window=258400)])
    snap = load_codex_telemetry("sid-3")
    assert snap is not None
    assert snap.context_window_tokens == 258400
    assert snap.context_used_tokens is None
    assert snap.context_remaining_tokens is None


# ── 4. Missing rollout returns None ─────────────────────────────────────────


def test_missing_rollout_returns_none(codex_home: Path) -> None:
    assert load_codex_telemetry("nope-uuid") is None


# ── 5. Malformed JSON lines are skipped ─────────────────────────────────────


def test_malformed_json_line_skipped(codex_home: Path) -> None:
    path = _write_rollout(
        codex_home, "sid-5",
        [_token_count(window=258400, input_tokens=4242)],
    )
    text = path.read_text(encoding="utf-8")
    # Inject garbage + a truncated JSON line between valid events.
    text += "not-json-at-all\n"
    text += '{"type":"event_msg","payload":{"type":"token_count"\n'  # truncated
    path.write_text(text, encoding="utf-8")
    snap = parse_codex_rollout(path)
    assert snap.context_window_tokens == 258400
    assert snap.context_used_tokens == 4242


# ── 6. Unknown schema returns snapshot with Nones ───────────────────────────


def test_unknown_schema_returns_snapshot_with_nones(codex_home: Path) -> None:
    path = _write_rollout(codex_home, "sid-6", [
        {
            "timestamp": "2026-05-19T10:30:46Z",
            "type": "event_msg",
            "payload": {"type": "token_count"},  # no info, no rate_limits
        },
    ])
    snap = parse_codex_rollout(path)
    assert snap.context_window_tokens is None
    assert snap.context_used_tokens is None
    assert snap.context_remaining_tokens is None
    assert snap.rate_limits is None


# ── 7. Partial used without window: snapshot reports used but no window ─────


def test_partial_used_without_window_yields_no_remaining(codex_home: Path) -> None:
    """The snapshot reports the partial used value but the both-or-neither
    rule applied by ``CodexAgent._capture_brain_telemetry`` will refuse to
    populate the runtime context attrs without a window (covered in the
    CodexAgent tests). Here we verify the snapshot side: window stays None,
    remaining stays None, used is honest about what was observed.
    """
    path = _write_rollout(codex_home, "sid-7", [
        _token_count(input_tokens=100),  # no window
    ])
    snap = parse_codex_rollout(path)
    assert snap.context_window_tokens is None
    assert snap.context_used_tokens == 100
    assert snap.context_remaining_tokens is None


# ── 8. thread_id aliases session_id ─────────────────────────────────────────


def test_thread_id_aliases_session_id(codex_home: Path) -> None:
    _write_rollout(
        codex_home, "thread-abc",
        [_token_count(window=258400, input_tokens=7000)],
    )
    snap = load_codex_telemetry(session_id=None, thread_id="thread-abc")
    assert snap is not None
    assert snap.context_used_tokens == 7000


# ── 9. CODEX_HOME env overrides default ─────────────────────────────────────


def test_codex_home_env_overrides_default(
    codex_home: Path, tmp_path: Path,
) -> None:
    path = _write_rollout(
        codex_home, "sid-9",
        [_token_count(window=258400, input_tokens=1)],
    )
    resolved = find_codex_rollout("sid-9")
    assert resolved is not None
    assert resolved == path
    # The resolved path must be under the tmp CODEX_HOME, never under ~/.codex.
    assert str(resolved).startswith(str(tmp_path))


# ── 10. rate_limits captured ────────────────────────────────────────────────


def test_rate_limits_captured(codex_home: Path) -> None:
    rl = {
        "limit_id": "codex",
        "primary": {"used_percent": 2.0},
        "plan_type": "prolite",
    }
    _write_rollout(
        codex_home, "sid-10",
        [_token_count(window=258400, input_tokens=1, rate_limits=rl)],
    )
    snap = load_codex_telemetry("sid-10")
    assert snap is not None
    assert snap.rate_limits is not None
    assert snap.rate_limits["plan_type"] == "prolite"


# ── 11. Cache hit skips filesystem walk ─────────────────────────────────────


def test_cache_hit_skips_filesystem_walk(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_rollout(
        codex_home, "sid-11",
        [_token_count(window=258400, input_tokens=1)],
    )
    first = find_codex_rollout("sid-11")
    assert first is not None
    # Block any further filesystem walk; only cache may answer.
    def _boom(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("walked")
    monkeypatch.setattr(Path, "glob", _boom)
    monkeypatch.setattr(Path, "rglob", _boom)
    second = find_codex_rollout("sid-11")
    assert second == first


# ── 12. Stale cache entry is purged ─────────────────────────────────────────


def test_stale_cache_entry_is_purged(codex_home: Path) -> None:
    real_path = _write_rollout(
        codex_home, "sid-12",
        [_token_count(window=258400, input_tokens=1)],
    )
    # Pre-seed cache with a non-existent path.
    bogus = codex_home / "does-not-exist" / "rollout-x-sid-12.jsonl"
    codex_telemetry._ROLLOUT_PATH_CACHE["sid-12"] = bogus
    resolved = find_codex_rollout("sid-12")
    assert resolved == real_path
    assert codex_telemetry._ROLLOUT_PATH_CACHE["sid-12"] == real_path


# ── 13. Recent date dirs resolved before full rglob ─────────────────────────


def test_recent_date_dirs_resolved_before_full_rglob(codex_home: Path) -> None:
    today_path = _write_rollout(
        codex_home, "sid-13",
        [_token_count(window=258400, input_tokens=99)],
        when=date.today(),
    )
    _write_rollout(
        codex_home, "sid-13",
        [_token_count(window=258400, input_tokens=1)],
        when=date.today() - timedelta(days=7),
    )
    resolved = find_codex_rollout("sid-13")
    assert resolved == today_path


# ── 14. Yesterday dir resolved at local day boundary ────────────────────────


def test_yesterday_dir_resolved_at_local_day_boundary(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    yesterday_path = _write_rollout(
        codex_home, "sid-14",
        [_token_count(window=258400, input_tokens=14)],
        when=date.today() - timedelta(days=1),
    )
    # If the cold path were taken, this would raise.
    def _boom(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("cold path taken")
    monkeypatch.setattr(Path, "rglob", _boom)
    resolved = find_codex_rollout("sid-14")
    assert resolved == yesterday_path


# ── 15. Header fallback when filename suffix differs ────────────────────────


def test_header_id_fallback_when_filename_suffix_differs(
    codex_home: Path,
) -> None:
    path = _write_rollout(
        codex_home, "sid-15",
        [_token_count(window=258400, input_tokens=15)],
        filename_id="aaa",
        header_id="bbb",
    )
    resolved = find_codex_rollout("bbb")
    assert resolved == path


# ── 16. _extract_codex_session_id contract pins rollout lookup ──────────────


@pytest.mark.parametrize(
    ("stdout", "expected_id"),
    [
        # Top-level session_id (mirrors test_codex_agent.py:144).
        ('{"type":"thread.started","session_id":"sess-123"}\n', "sess-123"),
        # Top-level thread_id (extra flavor not covered upstream).
        ('{"type":"thread.started","thread_id":"thread-xyz"}\n', "thread-xyz"),
        # Nested thread.thread_id (mirrors line 148).
        ('{"type":"event","thread":{"thread_id":"thread-abc"}}\n', "thread-abc"),
        # Regex-fallback conversation_id in colorized line (mirrors line 153).
        ('\x1b[36m{"conversation_id": "conv-xyz"}\x1b[0m', "conv-xyz"),
    ],
)
def test_extract_codex_session_id_contract_pins_rollout_lookup(
    codex_home: Path, stdout: str, expected_id: str,
) -> None:
    captured = _extract_codex_session_id(stdout)
    assert captured == expected_id
    path = _write_rollout(
        codex_home, captured,
        [_token_count(window=258400, input_tokens=1)],
    )
    assert find_codex_rollout(captured) == path


# ── Bonus: helper coverage ──────────────────────────────────────────────────


# ── 17. Resume duplicates: freshest rollout wins ────────────────────────────


def test_resume_duplicate_in_recent_dir_picks_newest(codex_home: Path) -> None:
    """If two rollouts share the same session-id suffix in the same recent
    date dir (e.g. ``codex exec resume`` produced a second file), the
    resolver must pick the newest by mtime so Live context tracks the
    active session rather than a stale duplicate.
    """
    import os
    import time

    today = date.today()
    day_dir = _date_dir(codex_home, today)
    day_dir.mkdir(parents=True, exist_ok=True)
    older = day_dir / "rollout-2026-05-19T08-00-00-sid-17.jsonl"
    newer = day_dir / "rollout-2026-05-19T20-00-00-sid-17.jsonl"
    older.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "sid-17"},
        }) + "\n",
        encoding="utf-8",
    )
    newer.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "sid-17"},
        }) + "\n",
        encoding="utf-8",
    )
    # Force a deterministic mtime gap (filesystem granularity can be 1s).
    past = time.time() - 60
    os.utime(older, (past, past))
    os.utime(newer, (time.time(), time.time()))
    assert find_codex_rollout("sid-17") == newer


def test_resume_duplicate_in_cold_rglob_picks_newest(codex_home: Path) -> None:
    """Same contract for the Layer 4 cold path: pick the newest match."""
    import os
    import time

    older_day = date.today() - timedelta(days=30)
    newer_day = date.today() - timedelta(days=15)
    older_dir = _date_dir(codex_home, older_day)
    newer_dir = _date_dir(codex_home, newer_day)
    older_dir.mkdir(parents=True, exist_ok=True)
    newer_dir.mkdir(parents=True, exist_ok=True)
    older = older_dir / "rollout-2026-04-19T08-00-00-sid-18.jsonl"
    newer = newer_dir / "rollout-2026-05-04T20-00-00-sid-18.jsonl"
    older.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "sid-18"},
        }) + "\n",
        encoding="utf-8",
    )
    newer.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "sid-18"},
        }) + "\n",
        encoding="utf-8",
    )
    past = time.time() - 60
    os.utime(older, (past, past))
    os.utime(newer, (time.time(), time.time()))
    assert find_codex_rollout("sid-18") == newer


def test_candidate_ids_dedupes_and_skips_empty() -> None:
    assert _candidate_ids("a", "a", None, "", "b") == ["a", "b"]
    assert _candidate_ids(None, None) == []


def test_snapshot_is_frozen_dataclass() -> None:
    import dataclasses

    snap = CodexTelemetrySnapshot(None, None, None, None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.context_used_tokens = 1  # type: ignore[misc]
