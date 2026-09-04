"""The ``orcho update`` command surface (`cli/_update_cli.py`).

The planner decides *whether* an upgrade may run; these tests pin the CLI's
half of the contract: what the operator is shown, and that a subprocess is
spawned only for a plan the planner cleared.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from cli import _update_cli
from cli._update_cli import cmd_update, format_update_plan
from sdk.self_update import KNOWN_MANAGERS, InstallProvenance, UpgradePlan


def _args(*, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(dry_run=dry_run)


def _runnable_plan(prefix: Path) -> UpgradePlan:
    return UpgradePlan(
        provenance=InstallProvenance(manager="pipx", package="orcho", prefix=prefix),
        command=("pipx", "upgrade", "orcho"),
        auto_runnable=True,
    )


def _blocked_plan(prefix: Path) -> UpgradePlan:
    return UpgradePlan(
        provenance=InstallProvenance(
            manager="editable",
            package="orcho-core",
            prefix=prefix,
            editable_source="file:///work/orcho-core",
        ),
        blocked_reason="this is an editable install; the checkout is the upgrade unit",
        hint="Update the checkout backing this install (file:///work/orcho-core).",
    )


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class TestFormatUpdatePlan:
    """What the report tells the operator."""

    def test_report_names_the_manager_location_and_command(self, tmp_path: Path) -> None:
        text = format_update_plan(_runnable_plan(tmp_path))

        assert "pipx-managed venv" in text
        assert str(tmp_path) in text
        assert "pipx upgrade orcho" in text

    def test_blocked_plan_states_the_reason_and_the_hint(self, tmp_path: Path) -> None:
        text = format_update_plan(_blocked_plan(tmp_path))

        assert "editable install" in text
        assert "Not run automatically" in text
        assert "file:///work/orcho-core" in text

    def test_locally_built_source_is_surfaced_before_any_upgrade(
        self, tmp_path: Path,
    ) -> None:
        """The operator must see that an upgrade would discard local code."""
        plan = UpgradePlan(
            provenance=InstallProvenance(
                manager="pipx",
                package="orcho",
                prefix=tmp_path,
                local_source="file:///work/orcho-dist",
            ),
            command=("pipx", "upgrade", "orcho"),
            blocked_reason="this install was built from a local path",
        )

        text = format_update_plan(plan)

        assert "built from file:///work/orcho-dist" in text

    def test_command_is_rendered_as_a_copy_pasteable_shell_line(
        self, tmp_path: Path,
    ) -> None:
        plan = UpgradePlan(
            provenance=InstallProvenance(manager="pip", package="orcho", prefix=tmp_path),
            command=("/opt/my python/bin/python", "-m", "pip", "install", "--upgrade", "orcho"),
            auto_runnable=True,
        )

        assert "'/opt/my python/bin/python' -m pip install --upgrade orcho" in (
            format_update_plan(plan)
        )


class TestManagerLabels:
    """Every manager the planner can report has operator-facing wording."""

    def test_each_known_manager_has_a_label(self) -> None:
        assert set(_update_cli._MANAGER_LABELS) >= KNOWN_MANAGERS

    def test_labels_do_not_outlive_the_managers_they_describe(self) -> None:
        assert set(_update_cli._MANAGER_LABELS) <= KNOWN_MANAGERS


class TestCmdUpdate:
    """When the command spawns an upgrade, and what it exits with."""

    def test_runnable_plan_executes_the_upgrade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(_update_cli, "plan_upgrade", lambda: _runnable_plan(tmp_path))
        monkeypatch.setattr(
            _update_cli.subprocess, "run",
            lambda cmd, **_: (calls.append(tuple(cmd)), _Completed(0))[1],
        )

        assert cmd_update(_args()) == 0
        assert calls == [("pipx", "upgrade", "orcho")]

    def test_dry_run_reports_without_spawning_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(_update_cli, "plan_upgrade", lambda: _runnable_plan(tmp_path))
        monkeypatch.setattr(
            _update_cli.subprocess, "run",
            lambda *_a, **_k: pytest.fail("--dry-run must not spawn an upgrade"),
        )

        assert cmd_update(_args(dry_run=True)) == 0

    def test_blocked_plan_reports_and_exits_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """A print-only plan is a successful report, not a failed upgrade."""
        monkeypatch.setattr(_update_cli, "plan_upgrade", lambda: _blocked_plan(tmp_path))
        monkeypatch.setattr(
            _update_cli.subprocess, "run",
            lambda *_a, **_k: pytest.fail("a blocked plan must not spawn an upgrade"),
        )

        assert cmd_update(_args()) == 0
        assert "Not run automatically" in capsys.readouterr().out

    def test_report_is_flushed_before_the_manager_writes_to_the_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The child inherits stdout; unflushed buffering would reorder the log."""
        events: list[str] = []
        monkeypatch.setattr(_update_cli, "plan_upgrade", lambda: _runnable_plan(tmp_path))
        monkeypatch.setattr(
            _update_cli.sys.stdout, "flush", lambda: events.append("flush"),
        )
        monkeypatch.setattr(
            _update_cli.subprocess, "run",
            lambda *_a, **_k: (events.append("spawn"), _Completed(0))[1],
        )

        cmd_update(_args())

        assert events.index("flush") < events.index("spawn")

    def test_failed_upgrade_propagates_the_manager_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(_update_cli, "plan_upgrade", lambda: _runnable_plan(tmp_path))
        monkeypatch.setattr(
            _update_cli.subprocess, "run", lambda *_a, **_k: _Completed(3),
        )

        assert cmd_update(_args()) == 3

    def test_unspawnable_command_is_reported_with_the_manual_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(_update_cli, "plan_upgrade", lambda: _runnable_plan(tmp_path))

        def _boom(*_a, **_k):
            raise OSError("no such file")

        monkeypatch.setattr(_update_cli.subprocess, "run", _boom)

        assert cmd_update(_args()) == 1
        assert "pipx upgrade orcho" in capsys.readouterr().err


class TestUpdateParser:
    """The subcommand is wired into the real parser and the help catalog."""

    def test_update_parses_with_and_without_dry_run(self) -> None:
        from cli.orcho import build_parser

        parser = build_parser()

        assert parser.parse_args(["update"]).dry_run is False
        assert parser.parse_args(["update", "--dry-run"]).dry_run is True

    def test_update_dispatches_to_its_handler(self) -> None:
        from cli.orcho import build_parser

        assert build_parser().parse_args(["update"]).func is cmd_update
