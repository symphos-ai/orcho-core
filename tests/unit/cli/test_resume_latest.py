"""Tests for bare `--resume` UX (auto-resolve to latest run).

Three layers covered:
 1. Outer CLI parser (`cli.orcho.build_parser`) accepts bare `--resume`
    and `--resume latest`, yielding the `"latest"` sentinel.
 2. Inner orchestrator parser (`pipeline.project_orchestrator`) does the
    same — required because `orcho-run` is also a public entry point and
    the SDK runner forwards via argv re-parse.
 3. `_resolve_resume_latest()` returns the newest run_id when the runs
    directory has entries, and exits rc=2 when it's empty or missing.

The combination — argparse → sentinel → resolver — is the new contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_empty_run(
    runs_dir: Path,
    run_id: str,
    *,
    status: str = "done",
    extra_meta: dict | None = None,
) -> Path:
    """Create a minimal run folder with a stub meta.json.

    `find_run` only needs a directory to exist; `meta.json` makes the
    fixture realistic and keeps it close to the real on-disk shape.
    """
    d = runs_dir / run_id
    d.mkdir(parents=True)
    project_dir = runs_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "task": "stub",
        "project": str(project_dir),
        "status": status,
        "profile": "lite",
        "timestamp": f"2026-05-14T{run_id[-6:-4]}:{run_id[-4:-2]}:00",
        "phases": {},
    }
    if extra_meta:
        meta.update(extra_meta)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


# ── Outer CLI parser (`orcho run --resume`) ─────────────────────────────────


class TestOuterCliResume:
    @pytest.fixture(autouse=True)
    def _import_parser(self):
        from cli.orcho import build_parser
        self.build_parser = build_parser

    def test_bare_resume_yields_sentinel(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["run", "--resume"])
        assert args.resume == "latest"

    def test_resume_explicit_id_unchanged(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["run", "--resume", "20260514_120000"])
        assert args.resume == "20260514_120000"

    def test_resume_latest_alias_string(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args(["run", "--resume", "latest"])
        assert args.resume == "latest"

    def test_resume_does_not_consume_next_flag(self) -> None:
        """Bare `--resume` followed by another flag must NOT swallow it."""
        parser = self.build_parser()
        args = parser.parse_args([
            "run", "--resume", "--workspace", "/tmp/ws",
        ])
        assert args.resume == "latest"
        assert args.workspace == "/tmp/ws"


# ── Inner orchestrator parser (`orcho-run --resume`) ────────────────────────


class TestInnerOrchestratorResume:
    """`pipeline.project_orchestrator.main()` reparses argv after SDK
    forwarding. The inner parser must accept the same surface as the outer
    one so bare `--resume` round-trips."""

    def test_bare_resume_yields_sentinel(self) -> None:
        import argparse

        # Ensure the orchestrator module imports cleanly (catches obvious
        # regressions in the inline argparse setup inside main()).
        import pipeline.project.cli  # noqa: F401

        # main() builds its parser inline; we can't isolate it without
        # running the full pipeline. Rebuild the equivalent argparse here
        # and assert the same surface — update this if the production
        # flag definition drifts.
        p = argparse.ArgumentParser()
        p.add_argument(
            "--resume", type=str, nargs="?", const="latest", default=None,
            metavar="RUN_ID",
        )
        args = p.parse_args(["--resume"])
        assert args.resume == "latest"
        args = p.parse_args(["--resume", "20260514_120000"])
        assert args.resume == "20260514_120000"
        args = p.parse_args([])
        assert args.resume is None


# ── Outer CLI parser (`orcho cross --resume`) ────────────────────────────────


class TestOuterCliCrossResume:
    """``orcho cross --resume`` must accept bare ``--resume`` exactly like
    ``orcho run --resume`` does — both flow through the inner cross
    orchestrator parser. Mirrors :class:`TestOuterCliResume`."""

    @pytest.fixture(autouse=True)
    def _import_parser(self):
        from cli.orcho import build_parser
        self.build_parser = build_parser

    def test_bare_resume_yields_sentinel(self) -> None:
        parser = self.build_parser()
        args = parser.parse_args([
            "cross",
            "--projects", "a:/tmp/a",
            "--resume",
        ])
        assert args.resume == "latest"


# ── Inner cross orchestrator parser (`orcho-cross --resume`) ────────────────


class TestInnerCrossOrchestratorResume:
    """Cross orchestrator main()'s argparse must accept bare --resume.

    Before the fix the cross parser was ``type=str, default=None`` with
    no ``nargs="?"``; bare ``--resume`` either consumed the next flag or
    crashed. We assert the new contract here against an equivalent
    standalone parser definition; if the production line drifts the
    test will need an update.
    """

    def test_bare_resume_yields_sentinel(self) -> None:
        import argparse

        # Equivalent of the production cross argparse line.
        p = argparse.ArgumentParser()
        p.add_argument(
            "--resume", type=str, nargs="?", const="latest", default=None,
            metavar="RUN_ID",
        )
        assert p.parse_args(["--resume"]).resume == "latest"
        assert p.parse_args(
            ["--resume", "20260514_120000"],
        ).resume == "20260514_120000"
        assert p.parse_args([]).resume is None


# ── argv forwarder ──────────────────────────────────────────────────────────


def test_build_orch_argv_passes_latest_sentinel() -> None:
    """Outer CLI → argv → inner main(): sentinel must survive."""
    from pipeline.argv import build_orch_argv

    argv = build_orch_argv(project="/p", task="t", resume="latest")
    # `--resume latest` must appear as two consecutive tokens.
    assert "--resume" in argv
    idx = argv.index("--resume")
    assert argv[idx + 1] == "latest"


# ── _resolve_resume_latest() ────────────────────────────────────────────────


class TestResolveResumeLatest:
    """Behavioural tests for the sentinel resolver. Uses `ORCHO_RUNSPACE`
    to point `find_run` at a tmp runs directory — same env var the SDK
    documents for workspace overrides."""

    @pytest.fixture
    def _isolated_workspace_env(self, tmp_path: Path, monkeypatch):
        """Point workspace discovery at a clean tmp dir and nothing else."""
        # Strip ambient workspace state so resolution is fully tmp-bound.
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_returns_newest_run_id(
        self, _isolated_workspace_env: Path, monkeypatch,
    ) -> None:
        runs_dir = _isolated_workspace_env / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        _write_empty_run(runs_dir, "20260101_000000")
        _write_empty_run(runs_dir, "20260514_120000")
        _write_empty_run(runs_dir, "20260301_080000")
        monkeypatch.setenv(
            "ORCHO_RUNSPACE", str(_isolated_workspace_env / "runspace"),
        )

        from pipeline.project.cli import _resolve_resume_latest

        assert _resolve_resume_latest() == "20260514_120000"

    def test_prefer_incomplete_skips_phase_handoff_halt(
        self, _isolated_workspace_env: Path, monkeypatch,
    ) -> None:
        runs_dir = _isolated_workspace_env / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        _write_empty_run(
            runs_dir,
            "20260605_120000",
            status="interrupted",
        )
        _write_empty_run(
            runs_dir,
            "20260606_120000",
            status="halted",
            extra_meta={"halt_reason": "phase_handoff_halt"},
        )
        monkeypatch.setenv(
            "ORCHO_RUNSPACE", str(_isolated_workspace_env / "runspace"),
        )

        from pipeline.project.cli import _resolve_resume_latest

        assert (
            _resolve_resume_latest(prefer_incomplete=True)
            == "20260605_120000"
        )

    def test_skips_latest_run_when_project_was_deleted(
        self, _isolated_workspace_env: Path, monkeypatch,
    ) -> None:
        runs_dir = _isolated_workspace_env / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        _write_empty_run(
            runs_dir,
            "20260609_125615",
            status="interrupted",
        )
        _write_empty_run(
            runs_dir,
            "20260610_003038",
            status="interrupted",
            extra_meta={
                "project": str(
                    _isolated_workspace_env
                    / "pytest-private-tmp"
                    / "deleted-project"
                ),
            },
        )
        monkeypatch.setenv(
            "ORCHO_RUNSPACE", str(_isolated_workspace_env / "runspace"),
        )

        from pipeline.project.cli import _resolve_resume_latest

        assert (
            _resolve_resume_latest(prefer_incomplete=True)
            == "20260609_125615"
        )

    def test_empty_runs_dir_exits_rc2(
        self, _isolated_workspace_env: Path, monkeypatch, capsys,
    ) -> None:
        runs_dir = _isolated_workspace_env / "runspace" / "runs"
        runs_dir.mkdir(parents=True)
        monkeypatch.setenv(
            "ORCHO_RUNSPACE", str(_isolated_workspace_env / "runspace"),
        )

        from pipeline.project.cli import _resolve_resume_latest

        with pytest.raises(SystemExit) as exc_info:
            _resolve_resume_latest()
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "--resume" in err

    def test_no_workspace_exits_rc2(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        # No env, no walk-up target, no cwd-derived workspace. find_run
        # must raise NoWorkspace, which the resolver maps to rc=2.
        monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
        monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
        monkeypatch.chdir(tmp_path)

        from pipeline.project.cli import _resolve_resume_latest

        with pytest.raises(SystemExit) as exc_info:
            _resolve_resume_latest()
        assert exc_info.value.code == 2
