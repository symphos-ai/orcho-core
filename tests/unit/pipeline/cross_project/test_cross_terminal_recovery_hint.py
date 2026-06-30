"""Phase 0 recovery hint — terminal wrapper renders retained worktree
+ ``diff.patch`` paths after a cross FAILED banner.

The hint is the cheap standalone closure on the "where did my work go
when the cross gate rejected the bundle?" gap. Before this, the FAILED
banner had no actionable tail; the operator had to spelunk the runspace
by hand. Phase A (CFA pause) + Phase B (cross commit_delivery) build on
top of this surface; until they land, the hint IS the recovery path.

These tests pin:

1. ``_collect_recovery_hint_data`` reads worktree paths from
   ``session["phases"]["projects"][alias]["worktree"]["path"]`` — the
   source of truth child sessions write under
   :func:`pipeline.cross_project.project_dispatch._dispatch_one_alias`.
   It does NOT read top-level ``session["projects"]`` (which is a plain
   ``{alias: project_path_string}`` map and does not carry worktree
   info).

2. When the child session field is absent but the conventional path
   ``<cross_run_dir>/worktrees/wt_<alias>/checkout`` exists on disk,
   the convention path is surfaced as a fallback. If neither source
   yields a path, the alias is omitted from the worktree list
   (operator sees only what is recoverable).

3. Per-alias ``diff.patch`` paths point at
   ``<cross_run_dir>/<alias>/diff.patch`` — only surfaced when the
   file exists on disk.

4. The terminal wrapper renders the Recovery block ONLY on FAILED;
   the DONE banner path is byte-identical to pre-Phase-0 behaviour.

5. ``recovery_hint(..., terminal=False)`` is a pure no-op so SILENT
   callers (Phase E) stay quiet. The structured data is still
   available to programmatic callers via ``_collect_recovery_hint_data``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pipeline.cross_project.finalization import (
    CrossFinalizationContext,
    _collect_recovery_hint_data,
    finalize_cross_with_terminal_output,
)
from pipeline.cross_project.rendering import recovery_hint

# ── helpers ───────────────────────────────────────────────────────────


def _make_cfa(*, approved: bool, source: str = "agent") -> SimpleNamespace:
    return SimpleNamespace(
        parsed=SimpleNamespace(approved=approved),
        source=source,
    )


def _make_context(
    *,
    run_dir: Path,
    session: dict,
    projects: dict[str, Path] | None = None,
    cfa_result=None,
    contract_results: dict | None = None,
    contract_check_failed: bool = False,
    contract_check_failure_reason: str | None = None,
) -> CrossFinalizationContext:
    return CrossFinalizationContext(
        run_dir=run_dir,
        output_dir=False,
        session=session,
        projects=projects if projects is not None else {
            "api": Path("/tmp/api"), "web": Path("/tmp/web"),
        },
        max_rounds=2,
        cfa_result=cfa_result,
        contract_results=contract_results or {},
        contract_check_failed=contract_check_failed,
        contract_check_failure_reason=contract_check_failure_reason,
        cross_phase_usage={},
    )


def _session_with_children(
    *,
    api_worktree: str | None = None,
    web_worktree: str | None = None,
) -> dict:
    """Cross session shape with two child entries under
    ``phases.projects``. Matches what
    ``project_dispatch._dispatch_one_alias`` writes."""
    children: dict = {}
    if api_worktree is not None:
        children["api"] = {"worktree": {"path": api_worktree}}
    if web_worktree is not None:
        children["web"] = {"worktree": {"path": web_worktree}}
    return {
        "phases": {
            "projects": children,
            "cross_final_acceptance": {"verdict": "REJECTED"},
        },
        "run_id": "TEST_RUN",
    }


# ── _collect_recovery_hint_data ───────────────────────────────────────


def test_collects_worktree_paths_from_child_session(tmp_path: Path) -> None:
    """The source of truth for per-alias worktree path is the child
    session field ``session["phases"]["projects"][alias]["worktree"]
    ["path"]`` — pinned against any future drift toward reading from
    the top-level ``session["projects"]`` map (which is a plain
    ``{alias: project_path_string}`` and would be the wrong source)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    api_wt = str(tmp_path / "wt_api_checkout")
    web_wt = str(tmp_path / "wt_web_checkout")

    ctx = _make_context(
        run_dir=run_dir,
        session=_session_with_children(
            api_worktree=api_wt,
            web_worktree=web_wt,
        ),
    )

    data = _collect_recovery_hint_data(ctx)

    assert data["run_dir"] == str(run_dir)
    assert ("api", api_wt) in data["worktrees"]
    assert ("web", web_wt) in data["worktrees"]
    assert data["diffs"] == []


def test_collects_diff_paths_when_files_exist(tmp_path: Path) -> None:
    """Per-alias ``diff.patch`` is surfaced only when the file exists
    on disk — ``<cross_run_dir>/<alias>/diff.patch``. A missing file
    drops the alias from the diffs list (no false recovery affordance)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Create the alias artifact dir + diff.patch for api only.
    (run_dir / "api").mkdir()
    (run_dir / "api" / "diff.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    # web has no diff.patch yet.
    (run_dir / "web").mkdir()

    ctx = _make_context(
        run_dir=run_dir,
        session=_session_with_children(),
    )

    data = _collect_recovery_hint_data(ctx)

    diffs = dict(data["diffs"])
    assert "api" in diffs
    assert diffs["api"] == str(run_dir / "api" / "diff.patch")
    assert "web" not in diffs


def test_convention_fallback_when_child_session_field_absent(
    tmp_path: Path,
) -> None:
    """When the child session does not carry ``worktree.path`` (child
    crashed before persisting its session) AND the conventional
    on-disk path exists, surface the convention path. This is the
    last-resort fallback; it never invents a path that doesn't exist."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Create the conventional layout for api only.
    (run_dir / "worktrees" / "wt_api" / "checkout").mkdir(parents=True)

    ctx = _make_context(
        run_dir=run_dir,
        # No child session entries — simulating a pre-dispatch crash.
        session={"phases": {"projects": {}}, "run_id": "TEST_RUN"},
    )

    data = _collect_recovery_hint_data(ctx)

    worktrees = dict(data["worktrees"])
    assert worktrees == {
        "api": str(run_dir / "worktrees" / "wt_api" / "checkout"),
    }, "convention fallback should surface api but NOT web (no on-disk path)"


def test_empty_when_nothing_dispatched(tmp_path: Path) -> None:
    """Cross failed before any alias dispatched (e.g. cross_plan
    rejected with no rounds left). Recovery data still carries
    run_dir; worktree + diff lists are empty. The renderer omits
    empty sections so the block degrades gracefully."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    ctx = _make_context(
        run_dir=run_dir,
        session={"phases": {"projects": {}}, "run_id": "TEST_RUN"},
    )

    data = _collect_recovery_hint_data(ctx)

    assert data["run_dir"] == str(run_dir)
    assert data["worktrees"] == []
    assert data["diffs"] == []


# ── recovery_hint renderer ────────────────────────────────────────────


def test_recovery_hint_silent_callers_get_noop(
    capsys: pytest.CaptureFixture,
) -> None:
    """SILENT presentation must produce ZERO stdout (ADR 0046 stop #9
    parity). The structured data is still available via
    ``_collect_recovery_hint_data`` for headless consumers — only the
    presentation layer goes quiet."""
    recovery_hint(
        {
            "run_dir": "/tmp/run",
            "worktrees": [("api", "/tmp/wt")],
            "diffs": [],
        },
        terminal=False,
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_recovery_hint_renders_run_dir_and_paths(
    capsys: pytest.CaptureFixture,
) -> None:
    recovery_hint({
        "run_dir": "/tmp/cross_run",
        "worktrees": [
            ("api", "/tmp/cross_run/worktrees/wt_api/checkout"),
            ("web", "/tmp/cross_run/worktrees/wt_web/checkout"),
        ],
        "diffs": [
            ("api", "/tmp/cross_run/api/diff.patch"),
        ],
    })
    captured = capsys.readouterr()
    assert "Recovery" in captured.out
    assert "/tmp/cross_run" in captured.out
    assert "/tmp/cross_run/worktrees/wt_api/checkout" in captured.out
    assert "/tmp/cross_run/worktrees/wt_web/checkout" in captured.out
    assert "/tmp/cross_run/api/diff.patch" in captured.out
    assert "phase_handoff_decide" in captured.out


def test_recovery_hint_omits_empty_sections(
    capsys: pytest.CaptureFixture,
) -> None:
    """Operator-facing degradation: when no worktrees / diffs are
    surfaced, the renderer drops those headings entirely rather than
    showing empty ``worktrees`` / ``diffs`` labels with no content.

    Asserts the absence of per-alias detail lines (``      [alias]  …``)
    rather than the bare word ``worktrees`` / ``diffs``, because the
    next-step text intentionally mentions both in prose.
    """
    recovery_hint({
        "run_dir": "/tmp/cross_run",
        "worktrees": [],
        "diffs": [],
    })
    captured = capsys.readouterr()
    assert "Recovery" in captured.out
    assert "/tmp/cross_run" in captured.out
    # Per-alias detail lines use the ``      [`` indent prefix; their
    # absence means both section bodies were skipped.
    assert "      [" not in captured.out


# ── terminal wrapper integration ──────────────────────────────────────


def test_failed_terminal_includes_recovery_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end through the terminal wrapper: a FAILED cross run
    renders the FAILED banner AND the Recovery block. Pinned against
    a regression to the bare-banner state where the operator had no
    actionable tail."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "api").mkdir()
    (run_dir / "api" / "diff.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    api_wt = str(tmp_path / "wt_api_checkout")

    ctx = _make_context(
        run_dir=run_dir,
        # CFA rejected → status=failed flows through the FAILED branch.
        cfa_result=_make_cfa(approved=False),
        session=_session_with_children(api_worktree=api_wt),
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        result = finalize_cross_with_terminal_output(ctx)

    assert result.status == "failed"
    captured = capsys.readouterr()
    assert "[FAILED]" in captured.out
    assert "Recovery" in captured.out
    assert str(run_dir) in captured.out
    assert api_wt in captured.out
    assert str(run_dir / "api" / "diff.patch") in captured.out


def test_failed_terminal_with_no_worktrees_still_shows_run_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Early-fail before dispatch: no child sessions, no diff.patch
    files. Recovery block still surfaces ``run_dir`` so the operator
    knows where to look. The worktrees / diffs sections are omitted
    by the renderer when their lists are empty."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    ctx = _make_context(
        run_dir=run_dir,
        cfa_result=_make_cfa(approved=False),
        session={"phases": {"projects": {}}, "run_id": "TEST_RUN"},
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        finalize_cross_with_terminal_output(ctx)

    captured = capsys.readouterr()
    assert "[FAILED]" in captured.out
    assert "Recovery" in captured.out
    assert str(run_dir) in captured.out
    # No per-alias detail lines surface — both worktrees + diffs were
    # empty so the renderer skipped their section bodies. The bare
    # words appear in the next-step prose; their indent-prefixed
    # detail lines (``      […``) do not.
    assert "      [" not in captured.out


def test_done_terminal_does_not_render_recovery_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The Recovery block is FAILED-only. A DONE cross run must NOT
    render it — the operator does not need recovery affordance on a
    successful bundle, and showing the hint would imply otherwise."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    ctx = _make_context(
        run_dir=run_dir,
        cfa_result=_make_cfa(approved=True),
        session=_session_with_children(api_worktree="/tmp/wt"),
    )

    with (
        patch("core.observability.events.emit"),
        patch("pipeline.engine.artifact_mirror.mirror_to_projects",
              return_value=[]),
    ):
        finalize_cross_with_terminal_output(ctx)

    captured = capsys.readouterr()
    assert "[DONE]" in captured.out
    assert "Recovery" not in captured.out
