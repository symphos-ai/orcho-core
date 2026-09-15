"""``orcho status --json`` is the same answer, addressed to a machine.

The flag must not fork the verdict: the JSON object carries the very
``RunDiagnosis`` the text report renders — one ``run_diagnosis`` call, one
``Next:`` projection — so a caller parsing stdout and an operator reading
the terminal can never be told different things. These tests pin the four
properties a consumer depends on: stdout is exactly one JSON object, the
key set is fixed (``null`` for absence, never a missing key), the degraded
diagnosis path still exits 0, and nothing paints into a parsed stream.

Everything here runs off ``tmp_path`` + ``monkeypatch``: no git, no
worktree, no process spawning, safe under xdist.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import orcho
from core.io.ansi import get_color_enabled, set_color_enabled, strip_ansi

_DEAD_PID = 2_147_480_000  # far above any live pid; the probe answers "dead"

RUN = "20260908_131908"

_TOP_LEVEL_KEYS = {
    "run_id",
    "run_dir",
    "status",
    "stalled_reason",
    "diagnosis",
    "diagnosis_error",
    "next_step",
}


def _args(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=kwargs.pop("run_id", None),
        workspace=kwargs.pop("workspace", None),
        verbose=kwargs.pop("verbose", False),
        json=kwargs.pop("json", True),
        **kwargs,
    )


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    rd = tmp_path / "runs"
    rd.mkdir()
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    return rd


def _write_meta(runs_dir: Path, run_id: str, meta: dict) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    return run_dir


def _write_done_run(runs_dir: Path, run_id: str = RUN) -> Path:
    return _write_meta(runs_dir, run_id, {
        "task": "t", "project": "/p", "profile": "small_task",
        "timestamp": "2026-09-08T13:19:08", "status": "done",
        "phases": {"implement": [{}]},
    })


def _write_stalled_run(runs_dir: Path, run_id: str, *, age_seconds: float) -> Path:
    """A ``running`` record whose recorded process is long gone."""
    run_dir = _write_meta(runs_dir, run_id, {
        "task": "hedge", "project": "/some/proj", "profile": "small_task",
        "timestamp": "2026-08-28T09:21:52", "status": "running",
        "phases": {"implement": [{}]},
    })
    (run_dir / "run_supervisor.json").write_text(json.dumps({
        "pid": _DEAD_PID,
        "started_at": (
            datetime.now(UTC) - timedelta(seconds=age_seconds)
        ).isoformat(),
    }), encoding="utf-8")
    # The event writer's real format: naive local wall-clock.
    stamped = (datetime.now() - timedelta(seconds=age_seconds)).isoformat()
    (run_dir / "events.jsonl").write_text(
        json.dumps({
            "seq": 1, "ts": stamped, "kind": "agent.tool_use", "payload": {},
        }) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _payload(capsys) -> dict:
    """The single JSON object on stdout, asserted to be exactly that."""
    out = capsys.readouterr().out.strip()
    assert out.startswith("{")
    assert out.endswith("}")
    payload = json.loads(out)
    assert isinstance(payload, dict)
    return payload


# ── the object a consumer parses ─────────────────────────────────────────────


def test_done_run_emits_one_object_with_the_full_key_set(
    runs_dir: Path, capsys,
) -> None:
    run_dir = _write_done_run(runs_dir)

    assert orcho.cmd_status(_args(run_id=RUN)) == 0

    payload = _payload(capsys)
    assert set(payload) == _TOP_LEVEL_KEYS
    assert payload["run_id"] == RUN
    assert payload["run_dir"] == str(run_dir)
    assert payload["status"]["meta"]["status"] == "done"
    assert payload["status"]["run_ref"]["run_id"] == RUN
    assert payload["stalled_reason"] is None
    assert payload["diagnosis_error"] is None
    # The structured next step reads the same diagnosis the text block does.
    assert payload["diagnosis"]["condition"] == payload["next_step"]["condition"]
    assert payload["next_step"]["lines"][0].startswith("inspect only")


def test_stalled_run_reports_the_stall_and_the_repair_command(
    runs_dir: Path, capsys,
) -> None:
    """A vanished process must read as stalled in JSON too, not as work."""
    _write_stalled_run(runs_dir, RUN, age_seconds=8 * 3600)

    assert orcho.cmd_status(_args(run_id=RUN)) == 0

    payload = _payload(capsys)
    assert isinstance(payload["stalled_reason"], str)
    assert payload["stalled_reason"]
    assert payload["diagnosis"]["condition"] == "stalled"
    assert payload["next_step"]["lines"] == [f"orcho repair-state {RUN}"]


def test_a_failed_diagnosis_degrades_inside_the_object(
    runs_dir: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrichment failure is data, not a traceback and not a non-zero exit."""
    _write_done_run(runs_dir)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("sdk.run_control.run_diagnosis", _boom)

    assert orcho.cmd_status(_args(run_id=RUN)) == 0

    payload = _payload(capsys)
    assert set(payload) == _TOP_LEVEL_KEYS
    assert payload["diagnosis"] is None
    assert payload["diagnosis_error"] == "RuntimeError: boom"
    assert payload["next_step"]["condition"] is None
    assert payload["next_step"]["lines"] == [
        "(diagnosis unavailable: RuntimeError: boom)",
    ]


# ── the error path keeps stdout parseable by keeping it empty ────────────────


def test_empty_workspace_says_nothing_on_stdout(runs_dir: Path, capsys) -> None:
    assert orcho.cmd_status(_args()) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No run found" in captured.err
    assert "Runs dir:" in captured.err


def test_unknown_run_id_says_nothing_on_stdout(runs_dir: Path, capsys) -> None:
    _write_done_run(runs_dir)

    assert orcho.cmd_status(_args(run_id="20260101_nosuch")) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No run found for id=20260101_nosuch." in captured.err
    assert "Runs dir:" in captured.err


# ── a parsed stream is never painted ─────────────────────────────────────────


def test_forced_color_never_reaches_the_json(runs_dir: Path, capsys) -> None:
    _write_done_run(runs_dir)
    previous = get_color_enabled()

    try:
        set_color_enabled(True)
        assert orcho.cmd_status(_args(run_id=RUN)) == 0
        out = capsys.readouterr().out
    finally:
        set_color_enabled(previous)

    assert "\x1b" not in out
    assert json.loads(out)["run_id"] == RUN


# ── --verbose is a text-report knob; JSON already carries everything ─────────


def test_verbose_does_not_change_the_json_shape(runs_dir: Path, capsys) -> None:
    _write_done_run(runs_dir)

    assert orcho.cmd_status(_args(run_id=RUN)) == 0
    plain = _payload(capsys)

    assert orcho.cmd_status(_args(run_id=RUN, verbose=True)) == 0
    verbose = _payload(capsys)

    assert set(verbose) == set(plain) == _TOP_LEVEL_KEYS
    assert verbose == plain


# ── the flag itself ──────────────────────────────────────────────────────────


def test_parser_defaults_the_flag_off(runs_dir: Path, capsys) -> None:
    parser = orcho.build_parser()

    assert parser.parse_args(["status", "--json"]).json is True
    assert parser.parse_args(["status"]).json is False

    # Without the flag the operator still gets the text report.
    _write_done_run(runs_dir)
    assert orcho.cmd_status(_args(run_id=RUN, json=False)) == 0
    out = strip_ansi(capsys.readouterr().out).strip()
    assert not out.startswith("{")
    assert f"Run:     {RUN}" in out
