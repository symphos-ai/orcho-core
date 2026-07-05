"""Unit tests for :mod:`cli._cross_launch`.

Covers the launch path taken when ``orcho run`` auto-detect recommends a cross
topology and the operator picks 'Start cross run':

* alias → path resolution (current alias = current path; siblings resolve to
  ``<current>/../<alias>``; a missing sibling is prompted for once; an
  unresolved alias yields ``None``);
* ``build_cross_argv`` shape (projects pairs + task, plus forwarded
  profile/model/mock);
* ``launch_cross_from_directive`` end-to-end with an injected launcher — a
  resolvable layout launches and returns the child code; an unresolved layout
  returns ``2`` and never launches.

No real cross process is spawned — ``launch_fn`` is injected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli._cross_launch import (
    build_cross_argv,
    launch_cross_from_directive,
    resolve_project_paths,
)


def _layout(tmp_path: Path, *aliases: str) -> Path:
    """Create sibling repo dirs under a shared base; return the base."""
    base = tmp_path / "ws"
    for alias in aliases:
        (base / alias).mkdir(parents=True)
    return base


def _never_prompt(alias: str) -> str:
    raise AssertionError(f"prompt_fn must not run for {alias!r}")


# ── resolve_project_paths ────────────────────────────────────────────────────


def test_current_alias_maps_to_current_path_siblings_resolve(
    tmp_path: Path,
) -> None:
    base = _layout(tmp_path, "orcho-core", "orcho-mcp")
    pairs = resolve_project_paths(
        ("orcho-core", "orcho-mcp"),
        str(base / "orcho-core"),
        interactive=True,
        prompt_fn=_never_prompt,
    )
    assert pairs == {
        "orcho-core": (base / "orcho-core").resolve(),
        "orcho-mcp": (base / "orcho-mcp").resolve(),
    }


def test_missing_sibling_is_prompted_once(tmp_path: Path) -> None:
    base = _layout(tmp_path, "orcho-core")
    elsewhere = tmp_path / "elsewhere" / "orcho-mcp"
    elsewhere.mkdir(parents=True)

    asked: list[str] = []

    def _prompt(alias: str) -> str:
        asked.append(alias)
        return str(elsewhere)

    pairs = resolve_project_paths(
        ("orcho-core", "orcho-mcp"),
        str(base / "orcho-core"),
        interactive=True,
        prompt_fn=_prompt,
    )
    assert asked == ["orcho-mcp"]
    assert pairs == {
        "orcho-core": (base / "orcho-core").resolve(),
        "orcho-mcp": elsewhere.resolve(),
    }


def test_unresolved_sibling_non_interactive_returns_none(
    tmp_path: Path,
) -> None:
    base = _layout(tmp_path, "orcho-core")
    pairs = resolve_project_paths(
        ("orcho-core", "orcho-mcp"),
        str(base / "orcho-core"),
        interactive=False,
        prompt_fn=_never_prompt,
    )
    assert pairs is None


def test_declined_prompt_returns_none(tmp_path: Path) -> None:
    base = _layout(tmp_path, "orcho-core")
    pairs = resolve_project_paths(
        ("orcho-core", "orcho-mcp"),
        str(base / "orcho-core"),
        interactive=True,
        prompt_fn=lambda _alias: "",  # operator hits Enter / Ctrl-D
    )
    assert pairs is None


# ── build_cross_argv ─────────────────────────────────────────────────────────


def test_build_cross_argv_carries_projects_and_task() -> None:
    pairs = {"orcho-core": Path("/a/core"), "orcho-mcp": Path("/a/mcp")}
    argv = build_cross_argv(pairs, "do the thing")
    assert argv == [
        "--projects",
        "orcho-core:/a/core",
        "orcho-mcp:/a/mcp",
        "--task",
        "do the thing",
    ]


def test_build_cross_argv_forwards_model_and_mock() -> None:
    pairs = {"orcho-core": Path("/a/core"), "orcho-mcp": Path("/a/mcp")}
    argv = build_cross_argv(
        pairs, "t", profile="small_task", model="opus", mock=True,
    )
    assert argv[-5:] == [
        "--profile", "small_task", "--model", "opus", "--mock",
    ]
    assert "--task" in argv and "t" in argv


# ── launch_cross_from_directive ──────────────────────────────────────────────


def test_launch_resolves_and_dispatches_fresh_process(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _layout(tmp_path, "orcho-core", "orcho-mcp")
    monkeypatch.setenv("ORCHO_WORK_MODE", "previous")
    captured: dict[str, list[str] | str | None] = {}

    def _launch(argv: list[str]) -> int:
        captured["argv"] = argv
        captured["work_mode"] = os.environ.get("ORCHO_WORK_MODE")
        return 7

    code = launch_cross_from_directive(
        projects=("orcho-core", "orcho-mcp"),
        task="wire change end to end",
        current_project=str(base / "orcho-core"),
        profile="small_task",
        work_mode="governed",
        model="opus",
        mock=True,
        interactive=True,
        color=False,
        prompt_fn=_never_prompt,
        launch_fn=_launch,
    )
    assert code == 7  # child's exit code is propagated
    argv = captured["argv"]
    assert argv[0] == "--projects"
    assert f"orcho-core:{(base / 'orcho-core').resolve()}" in argv
    assert f"orcho-mcp:{(base / 'orcho-mcp').resolve()}" in argv
    assert argv[argv.index("--task") + 1] == "wire change end to end"
    assert argv[-5:] == [
        "--profile", "small_task", "--model", "opus", "--mock",
    ]
    assert captured["work_mode"] == "governed"
    assert os.environ["ORCHO_WORK_MODE"] == "previous"
    # The banner echoes the resolved command, not a <path> template.
    out = capsys.readouterr().out
    assert "Starting cross run" in out
    assert "ORCHO_WORK_MODE=governed" in out
    assert "--profile small_task" in out
    assert "--model opus" in out
    assert "--mock" in out
    assert "<path>" not in out


def test_launch_unresolved_returns_2_and_never_dispatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    base = _layout(tmp_path, "orcho-core")  # orcho-mcp missing

    def _launch(_argv: list[str]) -> int:
        raise AssertionError("must not launch when a path is unresolved")

    code = launch_cross_from_directive(
        projects=("orcho-core", "orcho-mcp"),
        task="t",
        current_project=str(base / "orcho-core"),
        interactive=False,
        color=False,
        prompt_fn=_never_prompt,
        launch_fn=_launch,
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "Could not resolve repo paths" in err
    assert "orcho cross --projects" in err  # manual-launch hint
