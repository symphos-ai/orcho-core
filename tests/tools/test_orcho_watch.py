# SPDX-License-Identifier: Apache-2.0
"""Unit coverage for ``tools/orcho-watch.py`` — the read-only live monitor.

The module name contains a hyphen, so it is loaded via ``importlib`` rather
than a normal import. The live tail loop is exercised by injecting a *bounded*
fake event stream (monkeypatching ``evstore.tail``); the real poll loop lives
in ``core.observability.events`` and is covered by
``tests/unit/core/test_event_store.py`` — this file does not re-test it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WATCH_PATH = _ROOT / "tools" / "orcho-watch.py"


def _load_watch():
    spec = importlib.util.spec_from_file_location("orcho_watch_mod", _WATCH_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def watch():
    return _load_watch()


def _event(watch, *, seq=1, ts="2026-07-01T13:10:01.123", kind="", phase=None, **payload):
    return watch.evstore.Event(seq=seq, ts=ts, kind=kind, phase=phase, payload=dict(payload))


# ── Pure formatters ───────────────────────────────────────────────────────────

def test_fmt_time_slices_iso(watch):
    assert watch.fmt_time("2026-07-01T13:10:01.123") == "13:10:01"
    assert watch.fmt_time("short") == "short"  # too short → passthrough


def test_phase_icon_known_prefix_unknown_and_empty(watch):
    assert watch.phase_icon("PLAN") == "📐"
    assert watch.phase_icon("REVIEW_CHANGES") == "🕵️"
    assert watch.phase_icon("CROSS_PLAN") == "📐"  # startswith match
    assert watch.phase_icon("") == "▶️"
    assert watch.phase_icon("SOMETHING_ELSE") == "▶️"


def test_shorten_all_branches(watch, monkeypatch):
    assert watch.shorten("") == ""
    assert watch.shorten(watch._ENGINE_PREFIX + "pipeline/x.py") == "./pipeline/x.py"
    assert watch.shorten(watch._HOME + "/foo").startswith("~/")
    assert watch.shorten("/totally/other/abs") == "/totally/other/abs"
    # workspace prefix — inject the import-frozen global to exercise the branch
    monkeypatch.setattr(watch, "_WS_PREFIX", "/ws/")
    assert watch.shorten("/ws/runs/1") == "ws/runs/1"


# ── render_event dispatcher ───────────────────────────────────────────────────

def test_render_event_each_kind(watch):
    def R(**kw):
        return watch.render_event(_event(watch, **kw))

    assert "ORCHO WATCH" in R(kind="run.start", task="do the thing\nsecond line")
    assert "RUN END" in R(kind="run.end", status="done")
    assert "PLAN" in R(kind="phase.start", phase="PLAN", title="scoping")
    assert "done" in R(kind="phase.end", phase="PLAN", outcome="ok")
    assert "claude" in R(kind="agent.start", agent="claude", model="opus", label="T1")
    assert "ok" in R(kind="agent.end", agent="claude", return_code=0, duration=1.5)
    assert "FAIL" in R(kind="agent.end", agent="claude", return_code=1)
    # tool_use with a Read path (shortened) and a Grep " in " split path
    assert "Read" in R(kind="agent.tool_use", tool_name="Read",
                       summary=watch._ENGINE_PREFIX + "a.py")
    assert "Grep" in R(kind="agent.tool_use", tool_name="Grep",
                       summary="pat in " + watch._ENGINE_PREFIX + "b.py")
    assert "🔧" in R(kind="agent.tool_use", tool_name="MysteryTool", summary="x")
    assert "💬" in R(kind="agent.text", text="\n   \n  first real line\nsecond")
    assert "❌" in R(kind="agent.error", error_class="ValueError", message="boom")
    assert "retry" in R(kind="agent.retry", attempt=2, reason="rate_limit")
    assert "$" in R(kind="agent.summary", cost_usd=1.5, input_tokens=10, output_tokens=5)


def test_render_event_skippable_kinds_return_none(watch):
    # agent.summary with no cost/token bits → nothing to show
    assert watch.render_event(_event(watch, kind="agent.summary")) is None
    # unknown kind → skip
    assert watch.render_event(_event(watch, kind="totally.unknown")) is None


# ── Runspace resolution ───────────────────────────────────────────────────────

def test_resolve_runspace_env_override_wins(watch, tmp_path, monkeypatch):
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    assert watch._resolve_runspace() == tmp_path


def test_resolve_runspace_walkup_from_cwd(watch, tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    ws = tmp_path / "workspace-orchestrator"
    (ws / "runspace" / "runs").mkdir(parents=True)
    deep = ws / "orcho-core" / "sub"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert watch._resolve_runspace() == ws / "runspace"


def test_resolve_runspace_falls_back_to_engine_resolver(watch, tmp_path, monkeypatch):
    import core.infra.platform as plat
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    monkeypatch.chdir(tmp_path)  # isolated temp; no runspace/runs up the tree
    sentinel = tmp_path / "engine_runspace"
    monkeypatch.setattr(plat, "runspace_dir", lambda: sentinel)
    assert watch._resolve_runspace() == sentinel


def test_resolve_runspace_returns_none_when_resolver_raises(watch, tmp_path, monkeypatch):
    import core.infra.platform as plat
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    monkeypatch.chdir(tmp_path)

    def _boom():
        raise plat.WorkspaceNotResolvedError("nope")

    monkeypatch.setattr(plat, "runspace_dir", _boom)
    assert watch._resolve_runspace() is None


# ── find_latest_run ───────────────────────────────────────────────────────────

def test_find_latest_run_prefers_run_with_events(watch, tmp_path, monkeypatch):
    rs = tmp_path / "runspace"
    (rs / "runs" / "older").mkdir(parents=True)
    newer = rs / "runs" / "newer"
    newer.mkdir(parents=True)
    (newer / "events.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watch, "_resolve_runspace", lambda: rs)
    assert watch.find_latest_run() == newer


def test_find_latest_run_fallback_to_newest_dir(watch, tmp_path, monkeypatch):
    rs = tmp_path / "runspace"
    (rs / "runs" / "only").mkdir(parents=True)
    monkeypatch.setattr(watch, "_resolve_runspace", lambda: rs)
    assert watch.find_latest_run() == rs / "runs" / "only"


def test_find_latest_run_exits_without_runspace(watch, monkeypatch):
    monkeypatch.setattr(watch, "_resolve_runspace", lambda: None)
    with pytest.raises(SystemExit):
        watch.find_latest_run()


def test_find_latest_run_exits_without_runs_dir(watch, tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "_resolve_runspace", lambda: tmp_path)  # no runs/ child
    with pytest.raises(SystemExit):
        watch.find_latest_run()


def test_find_latest_run_exits_on_empty_runs(watch, tmp_path, monkeypatch):
    rs = tmp_path / "runspace"
    (rs / "runs").mkdir(parents=True)
    monkeypatch.setattr(watch, "_resolve_runspace", lambda: rs)
    with pytest.raises(SystemExit):
        watch.find_latest_run()


# ── resolve_run_dir ───────────────────────────────────────────────────────────

def test_resolve_run_dir_arg_is_dir(watch, tmp_path, monkeypatch):
    monkeypatch.setattr(watch.sys, "argv", ["orcho-watch", str(tmp_path)])
    assert watch.resolve_run_dir() == tmp_path


def test_resolve_run_dir_arg_is_file_returns_parent(watch, tmp_path, monkeypatch):
    f = tmp_path / "events.jsonl"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watch.sys, "argv", ["orcho-watch", str(f)])
    assert watch.resolve_run_dir() == tmp_path


def test_resolve_run_dir_no_arg_delegates_to_latest(watch, tmp_path, monkeypatch):
    monkeypatch.setattr(watch.sys, "argv", ["orcho-watch"])
    monkeypatch.setattr(watch, "find_latest_run", lambda: tmp_path)
    assert watch.resolve_run_dir() == tmp_path


def test_resolve_run_dir_exits_on_missing_path(watch, tmp_path, monkeypatch):
    monkeypatch.setattr(watch.sys, "argv", ["orcho-watch", str(tmp_path / "nope")])
    with pytest.raises(SystemExit):
        watch.resolve_run_dir()


# ── main (bounded fake stream, no real polling) ───────────────────────────────

def test_main_reports_missing_event_store(watch, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(watch, "resolve_run_dir", lambda: tmp_path)  # no events.jsonl
    assert watch.main() == 1
    assert "event-store" in capsys.readouterr().out


def test_main_renders_stream_and_completes(watch, tmp_path, monkeypatch, capsys):
    (tmp_path / "events.jsonl").write_text("{}", encoding="utf-8")  # pass exists() gate
    monkeypatch.setattr(watch, "resolve_run_dir", lambda: tmp_path)
    stream = [
        _event(watch, seq=1, kind="run.start", task="do the thing"),
        _event(watch, seq=2, kind="phase.start", phase="PLAN", title="scoping"),
        _event(watch, seq=3, kind="run.end", status="done"),
    ]
    monkeypatch.setattr(watch.evstore, "tail", lambda *a, **k: iter(stream))
    assert watch.main() == 0
    out = capsys.readouterr().out
    assert "ORCHO WATCH" in out
    assert "PIPELINE COMPLETE" in out


def test_main_handles_keyboard_interrupt(watch, tmp_path, monkeypatch):
    (tmp_path / "events.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watch, "resolve_run_dir", lambda: tmp_path)

    def _interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(watch.evstore, "tail", _interrupt)
    assert watch.main() == 130
