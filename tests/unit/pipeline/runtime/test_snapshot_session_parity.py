"""parity baseline.

Pin the canonical session.json shape produced by today's
``_PipelineRun.run_*_loop`` god-methods so 's collapse can
verify byte-for-byte parity (modulo normalized volatile fields).

Per the side-effect matrix (.orcho/artifacts/side_effect_matrix.md),
every imperative side-effect maps to a new owner; the snapshot test
catches any drift in the resulting session shape regardless of which
owner produced it.

Two scenarios cover the most-trafficked paths:
 * lite-equivalent (FULL mode, skip validate_plan loop, no review rounds).
 * advanced-equivalent (FULL mode, max_rounds=1).

Update workflow: when an intentional shape change ships, regenerate
the golden file by setting ``ORCHO_REGEN_SNAPSHOTS=1`` in the env;
review the diff; commit the regenerated fixture alongside the change
that motivated it.

Exact token counts are intentionally normalized out of these shape
snapshots. ``TestSessionTokenBudgets`` below owns prompt-size regression
thresholds so useful prompt compression does not require fixture churn.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.protocols import SessionMode
from agents.runtimes._strategy import (
    MockAgentProvider,
    make_mock_phase_config,
)
from pipeline.project_orchestrator import run_pipeline
from tests.fixtures._normalize import normalize_events, normalize_session

GOLDEN_DIR = Path(__file__).parents[3] / "fixtures" / "golden"
REGEN = os.environ.get("ORCHO_REGEN_SNAPSHOTS") == "1"

_TOKEN_BUDGET_TOTAL_IN = {
    "task": 4000,
    "feature": 4900,
    "delivery_audit": 2750,
}


@pytest.fixture(autouse=True)
def _adr0119_legacy_bypass_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin delivery to the ADR 0119 ``bypass`` opt-out for the shape snapshots.

    ADR 0119 shipped ``branch_policy=worktree_branch`` as the delivery default,
    which changes the ``commit_delivery`` session block (a published branch +
    ``delivery_branch`` instead of a ``commit_sha`` on the checkout). The golden
    session-shape fixtures pin the prior block, so these runs use ``bypass`` (the
    ADR's explicit legacy opt-out) to keep the goldens valid without regenerating
    high-blast-radius snapshot data. The new branch-policy behavior is covered by
    ``tests/unit/pipeline/engine/test_commit_delivery.py`` and
    ``test_delivery_branch.py``.
    """
    import pipeline.engine.delivery_branch as _db

    monkeypatch.setattr(_db, "normalize_branch_policy", lambda _raw: "bypass")


@pytest.fixture(autouse=True)
def _no_ambient_run_scoped_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the engine's run-scoped env channels for hermetic shape snapshots.

    An orchestrated gate process (or a stale interactive shell — see
    ``pipeline/project/auto_detect.py``) can carry ``ORCHO_AUTODETECT_DECISION``
    / ``ORCHO_WORK_MODE``; a session built under them grows an ``auto_detect``
    block the golden fixtures do not pin, failing these tests for code that is
    green in a clean shell. The engine-side strip lives in
    ``pipeline.verification_env.RUN_SCOPED_ENV_CHANNELS``; this fixture keeps
    the snapshots hermetic regardless of how pytest was launched.
    """
    from pipeline.verification_env import RUN_SCOPED_ENV_CHANNELS

    for key in RUN_SCOPED_ENV_CHANNELS:
        monkeypatch.delenv(key, raising=False)


def test_session_shape_normalizer_masks_prompt_size_estimates() -> None:
    """Shape snapshots pin prompt topology, not exact prompt prose size."""
    normalized = normalize_session({
        "phase": "validate_plan",
        "prompt_render": {
            "part_ids": ["role:plan_reviewer@0", "task:validate_plan@0"],
        },
        "context_clearing": {
            "clearable_part_ids": ["role:plan_reviewer@0"],
            "clearable_tokens": 1300,
            "cleared_tokens": 0,
        },
        "context_growth": {
            "input_tokens_estimate": 490,
            "output_tokens_estimate": 49,
            "summary_tokens": 0,
        },
        "context_pressure": {
            "context_fill_ratio": 0.12,
            "context_remaining_tokens": 1000,
            "context_used_tokens": 490,
            "context_window_tokens": 4096,
        },
    })

    assert normalized["prompt_render"]["part_ids"] == [
        "role:plan_reviewer@0",
        "task:validate_plan@0",
    ]
    assert normalized["context_clearing"]["clearable_part_ids"] == [
        "role:plan_reviewer@0",
    ]
    assert normalized["context_clearing"]["clearable_tokens"] == "<TOKEN_COUNT>"
    assert normalized["context_clearing"]["cleared_tokens"] == "<TOKEN_COUNT>"
    assert normalized["context_growth"]["input_tokens_estimate"] == "<TOKEN_COUNT>"
    assert normalized["context_growth"]["output_tokens_estimate"] == "<TOKEN_COUNT>"
    assert normalized["context_growth"]["summary_tokens"] == "<TOKEN_COUNT>"
    assert normalized["context_pressure"]["context_fill_ratio"] == "<TOKEN_COUNT>"
    assert normalized["context_pressure"]["context_remaining_tokens"] == "<TOKEN_COUNT>"
    assert normalized["context_pressure"]["context_used_tokens"] == "<TOKEN_COUNT>"
    assert normalized["context_pressure"]["context_window_tokens"] == "<TOKEN_COUNT>"


def _setup_project(tmp_path: Path) -> tuple[Path, Path]:
    from tests.conftest import init_git_repo

    project = tmp_path / "proj"
    init_git_repo(project)
    output_dir = tmp_path / "runs" / "snapshot_run"
    output_dir.mkdir(parents=True)
    # Reset core-observability globals so prior tests don't make
    # ``is_sub_pipeline()`` return True (which would skip
    # ``init_event_store`` for our hermetic run).
    from core.observability import logging as _core_logging
    _core_logging._progress_log = None
    return project, output_dir


def _run_hermetic(
    *,
    project: Path,
    output_dir: Path,
    profile_name: str,
    max_rounds: int,
) -> dict:
    """``profile_name`` (str) replaces the legacy
 ``pipeline_mode: PipelineMode`` parameter. ``skip_plan`` is gone too —
 pass ``profile_name="task"`` for the build-only flow.
 """
    # Seed Python's RNG so MockAgent stubs that use random.choice for
    # canned-critique selection produce a deterministic session shape.
    # Without this seed the snapshot fixture drifts between runs (the
    # mocks pick different approved-review summaries).
    import random as _r
    _r.seed(0xDEADBEEF)
    return run_pipeline(
        task="snapshot regression task",
        project_dir=str(project),
        max_rounds=max_rounds,
        output_dir=output_dir,
        dry_run=False,
        provider=MockAgentProvider(),
        phase_config=make_mock_phase_config(),
        session_mode=SessionMode.STATELESS,
        profile_name=profile_name,
    )


def _compare_or_regen(name: str, normalized: dict) -> None:
    """Compare against golden file; regenerate if env var set."""
    fixture = GOLDEN_DIR / f"{name}.json"
    if REGEN or not fixture.exists():
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(
            json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8",
        )
        if not REGEN:
            pytest.skip(
                f"golden fixture {name} created from current run "
                f"(set ORCHO_REGEN_SNAPSHOTS=1 to regenerate intentionally)"
            )
        return

    expected = json.loads(fixture.read_text(encoding="utf-8"))
    assert normalized == expected, (
        f"session shape drifted from {fixture}.\n"
        f"Set ORCHO_REGEN_SNAPSHOTS=1 to update if change is intentional.\n"
    )


# ── Snapshot scenarios ──────────────────────────────────────────────────────

class TestSessionShapeSnapshots:
    def test_task_mode_shape_pinned(self, tmp_path: Path) -> None:
        """TASK mode: skip planning, run BUILD + REVIEW/FIX + FINAL_ACCEPTANCE.
 Closest to the v2 ``task`` profile semantics. Hermetic mock run
 must produce a stable session.json shape across 's
 _PipelineRun collapse."""
        project, output_dir = _setup_project(tmp_path)
        session = _run_hermetic(
            project=project,
            output_dir=output_dir,
            profile_name="task",
            max_rounds=1,
        )
        normalized = normalize_session(session, project_root=str(project))
        _compare_or_regen("task_mode", normalized)

    def test_full_mode_shape_pinned(self, tmp_path: Path) -> None:
        """FULL mode with single rounds. The most-used profile. Captures
 plan / validate_plan / build / rounds / final_acceptance session keys."""
        project, output_dir = _setup_project(tmp_path)
        session = _run_hermetic(
            project=project,
            output_dir=output_dir,
            profile_name="feature",
            max_rounds=1,
        )
        normalized = normalize_session(session, project_root=str(project))
        _compare_or_regen("full_mode_single_round", normalized)

    def test_review_mode_shape_pinned(self, tmp_path: Path) -> None:
        """The ``delivery_audit`` work kind: only review_changes pass +
 final_acceptance, no plan/implement (reuses the former scoped
 ``review`` recipe; phase IDs use ADR 0022 vocabulary)."""
        project, output_dir = _setup_project(tmp_path)
        session = _run_hermetic(
            project=project,
            output_dir=output_dir,
            profile_name="delivery_audit",
            max_rounds=0,
        )
        normalized = normalize_session(session, project_root=str(project))
        _compare_or_regen("review_mode", normalized)


class TestSessionTokenBudgets:
    """Prompt-size regression guard, separate from session-shape snapshots.

    Budgets are deliberately ceilings, not exact counts. Decreases should pass
    without touching golden fixtures; large accidental growth should fail.
    """

    @pytest.mark.parametrize(
        ("profile_name", "max_rounds"),
        [
            ("task", 1),
            ("feature", 1),
            ("delivery_audit", 0),
        ],
    )
    def test_total_input_tokens_stay_under_budget(
        self,
        tmp_path: Path,
        profile_name: str,
        max_rounds: int,
    ) -> None:
        project, output_dir = _setup_project(tmp_path)
        session = _run_hermetic(
            project=project,
            output_dir=output_dir,
            profile_name=profile_name,
            max_rounds=max_rounds,
        )
        actual = session["metrics"]["total_tokens_in"]
        assert actual <= _TOKEN_BUDGET_TOTAL_IN[profile_name], (
            f"{profile_name} prompt input tokens grew to {actual}; "
            f"budget is {_TOKEN_BUDGET_TOTAL_IN[profile_name]}"
        )


# ── Event-stream ordering snapshot ──────────────────────────────────────────

class TestEventStreamSnapshot:
    """``events.jsonl`` ordering must stay identical across.
 Captures the sequence of event names + a reduced payload (no
 timestamps). The full payload diff would be too noisy; ordering +
 event-type-by-phase is the load-bearing contract for orcho-mcp /
 orcho-web dashboard consumers."""

    def test_full_mode_event_order_pinned(self, tmp_path: Path) -> None:
        project, output_dir = _setup_project(tmp_path)
        _run_hermetic(
            project=project,
            output_dir=output_dir,
            profile_name="feature",
            max_rounds=1,
        )
        events_path = output_dir / "events.jsonl"
        if not events_path.exists():
            existing = list(output_dir.rglob("*"))
            pytest.skip(
                f"events.jsonl not produced at {events_path}; "
                f"output_dir contents: {[str(p.relative_to(output_dir)) for p in existing]}"
            )

        lines = events_path.read_text(encoding="utf-8").splitlines()
        normalized = normalize_events(lines, project_root=str(project))

        # Reduce to just (event-type, key fields) to keep the snapshot
        # readable — full event payloads carry per-run paths and IDs.
        reduced = [
            {
                "type":     ev.get("type") or ev.get("event") or "unknown",
                "phase":    ev.get("phase"),
                "approved": ev.get("approved"),
                "attempt":  ev.get("attempt"),
            }
            for ev in normalized
        ]
        _compare_or_regen("full_mode_events", {"events": reduced})
