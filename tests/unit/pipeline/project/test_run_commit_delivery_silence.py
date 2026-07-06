"""Cross-project / sub-pipeline silence at the commit-delivery gate.

``_run_commit_delivery`` short-circuits with an early ``return`` when
the run is a child of another pipeline (``parent_run_id`` set) or
represents a cross-project alias (``project_alias`` set). Neither case
should reach ``resolve_commit_delivery`` / ``apply_commit_delivery`` /
``render_delivery_outcome``, so the operator-visible outcome line must
not be printed at this layer — the parent / cross owner is responsible
for the unified user signal.

The tests below drive the method directly with a minimal
``SimpleNamespace`` stub and assert the captured stdout is empty. No
``monkeypatch`` is used: the early return is unconditional on these
two flags, so nothing downstream of the guard needs to be stubbed.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.project.run import _PipelineRun


def _stub_run(
    *,
    parent_run_id: str | None,
    project_alias: str | None,
    output_dir: Path,
) -> SimpleNamespace:
    """Minimal stub satisfying every attribute ``_run_commit_delivery``
    reads up to (and only up to) the cross/parent early-return."""
    return SimpleNamespace(
        output_dir=output_dir,
        session={"status": "done"},
        parent_run_id=parent_run_id,
        project_alias=project_alias,
    )


def test_parent_run_id_makes_commit_delivery_silent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sub-pipeline path: ``parent_run_id='run_X'`` must skip the
    delivery gate without printing any outcome marker."""
    stub = _stub_run(
        parent_run_id="run_X",
        project_alias=None,
        output_dir=tmp_path / "run",
    )

    _PipelineRun._run_commit_delivery(stub, tmp_path / "wt")

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"sub-pipeline leaked stdout: {captured.out!r}"
    )
    for marker in (
        "📦",
        "DELIVERY —",
        "COMMITTED TO YOUR CHECKOUT",
        "PULL REQUEST OPENED",
    ):
        assert marker not in captured.out
    # No state mutation either — the gate was skipped, not executed.
    assert "commit_delivery" not in stub.session


def test_project_alias_makes_commit_delivery_silent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cross-project alias path: ``project_alias='child'`` must skip
    the delivery gate without printing any outcome marker."""
    stub = _stub_run(
        parent_run_id=None,
        project_alias="child",
        output_dir=tmp_path / "run",
    )

    _PipelineRun._run_commit_delivery(stub, tmp_path / "wt")

    captured = capsys.readouterr()
    assert captured.out == "", (
        f"cross-project alias leaked stdout: {captured.out!r}"
    )
    for marker in (
        "📦",
        "DELIVERY —",
        "COMMITTED TO YOUR CHECKOUT",
        "PULL REQUEST OPENED",
    ):
        assert marker not in captured.out
    assert "commit_delivery" not in stub.session
