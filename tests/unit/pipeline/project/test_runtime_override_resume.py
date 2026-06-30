# SPDX-License-Identifier: Apache-2.0
"""ADR 0101 / T2 — operator runtime/model override: persist + resume apply.

Two halves:

* :class:`TestPersistRuntimeOverride` pins the durable write in
  ``sdk/run_control/runtime_override.py`` — candidate validation, exact-payload
  idempotency, and conflict-on-divergence.
* :class:`TestApplyOnResume` pins the read + per-phase application in
  ``pipeline/project/session_run.py`` + ``pipeline/project/runtime_setup.py`` —
  the override survives the persist→resume boundary, isolates to its phase, and
  never activates without a persisted record.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sdk.run_control.runtime_override as ro
from pipeline.project.runtime_setup import _synthesize_phase_config
from pipeline.project.session_run import _read_persisted_runtime_override
from sdk.run_control.runtime_override import (
    RuntimeOverrideConflict,
    RuntimeOverrideError,
    persist_runtime_override,
    read_runtime_override,
)

_SLOTS = (
    "plan_agent",
    "validate_plan_agent",
    "implement_agent",
    "review_changes_agent",
    "repair_changes_agent",
    "repair_escalation_agent",
    "final_acceptance_agent",
)


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeAgent:
    def __init__(self, runtime: str, model: str, effort: object = None) -> None:
        self.runtime = runtime
        self.model = model
        self.effort = effort


class _FakeProvider:
    """Records nothing; just echoes the (runtime, model) it was asked for so the
    synthesized slot's runtime/model are inspectable."""

    def resolve(self, runtime: str, model: str, effort: object = None) -> _FakeAgent:
        return _FakeAgent(runtime, model, effort)


def _synth(provider: _FakeProvider, runtime_override: dict | None):
    return _synthesize_phase_config(
        None,
        _provider=provider,
        plan_model="pm",
        implement_model="im",
        repair_model="rm",
        repair_escalation_model="rem",
        review_model="rvm",
        runtime_override=runtime_override,
    )


def _patch_candidates(monkeypatch: pytest.MonkeyPatch, candidates: list[dict]) -> None:
    monkeypatch.setattr(
        ro, "configured_replacement_candidates", lambda phase: candidates,
    )


# ── persist ──────────────────────────────────────────────────────────────────


class TestPersistRuntimeOverride:
    def test_persist_writes_validated_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_candidates(monkeypatch, [{"runtime": "codex", "model": "gpt5"}])
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "meta.json").write_text(json.dumps({"status": "failed"}))

        record = persist_runtime_override(
            run_dir, phase="plan", runtime="codex", model="gpt5", note="switch",
        )
        assert record["phase"] == "plan"
        assert record["runtime"] == "codex"
        assert record["model"] == "gpt5"
        assert record["note"] == "switch"
        assert record["decided_at"]

        on_disk = json.loads((run_dir / "meta.json").read_text())
        assert on_disk["runtime_override"] == record
        # Pre-existing meta keys are preserved.
        assert on_disk["status"] == "failed"

    def test_persist_rejects_non_candidate_pair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_candidates(monkeypatch, [{"runtime": "codex", "model": "gpt5"}])
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        with pytest.raises(RuntimeOverrideError):
            persist_runtime_override(
                run_dir, phase="plan", runtime="codex", model="wrong-model",
            )
        # Nothing written on rejection.
        assert not (run_dir / "meta.json").is_file()

    def test_persist_is_idempotent_on_exact_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_candidates(monkeypatch, [{"runtime": "codex", "model": "gpt5"}])
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        first = persist_runtime_override(
            run_dir, phase="plan", runtime="codex", model="gpt5",
            decided_at="2026-06-24T00:00:00",
        )
        second = persist_runtime_override(
            run_dir, phase="plan", runtime="codex", model="gpt5",
            decided_at="2026-06-24T11:11:11",  # ignored — record unchanged
        )
        assert second == first
        assert second["decided_at"] == "2026-06-24T00:00:00"

    def test_persist_conflict_on_divergent_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_candidates(
            monkeypatch,
            [{"runtime": "codex", "model": "gpt5"},
             {"runtime": "gemini", "model": "g2"}],
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        persist_runtime_override(run_dir, phase="plan", runtime="codex", model="gpt5")
        with pytest.raises(RuntimeOverrideConflict):
            persist_runtime_override(
                run_dir, phase="plan", runtime="gemini", model="g2",
            )

    def test_read_runtime_override_tolerant(self) -> None:
        assert read_runtime_override({}) is None
        assert read_runtime_override({"runtime_override": {"phase": "plan"}}) is None
        ok = read_runtime_override({
            "runtime_override": {
                "phase": "plan", "runtime": "codex", "model": "gpt5",
                "decided_at": "t", "note": None,
            },
        })
        assert ok == {"phase": "plan", "runtime": "codex", "model": "gpt5"}


# ── read + apply on resume ───────────────────────────────────────────────────


class TestApplyOnResume:
    def test_read_persisted_override_survives_to_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Persist exactly as RunService.resume does, then read back the way
        # _resolve_profile_runtime does on the resumed run.
        _patch_candidates(monkeypatch, [{"runtime": "codex", "model": "gpt5"}])
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "meta.json").write_text(json.dumps({"status": "failed"}))
        persist_runtime_override(run_dir, phase="plan", runtime="codex", model="gpt5")

        triple = _read_persisted_runtime_override(run_dir)
        assert triple == {"phase": "plan", "runtime": "codex", "model": "gpt5"}

    def test_override_survives_resume_session_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Fix F1: ``RunService.resume`` persists ``runtime_override`` into the
        # run dir's ``meta.json`` before resume; the resume then re-enters and
        # ``init_session_with_atexit`` rewrites ``meta.json`` from a fresh
        # session dict. The durable record must be carried forward so it
        # survives that rewrite — otherwise a later resume / SDK / evidence read
        # would lose the operator's decision.
        from types import SimpleNamespace

        from pipeline.project.bootstrap import init_session_with_atexit

        # Don't leave a real atexit hook registered by the unit test.
        monkeypatch.setattr(
            "pipeline.project.bootstrap.atexit.register", lambda fn: None,
        )
        _patch_candidates(monkeypatch, [{"runtime": "codex", "model": "gpt5"}])
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "meta.json").write_text(json.dumps({"status": "failed"}))
        persist_runtime_override(
            run_dir, phase="plan", runtime="codex", model="gpt5",
        )

        # Re-enter on resume: init reads prior meta and rewrites meta.json.
        init_session_with_atexit(
            task="t",
            project_path=tmp_path / "proj",
            plugin=SimpleNamespace(name="p"),
            model="m",
            profile_name="default",
            session_mode=SimpleNamespace(value="auto"),
            change_handoff="uncommitted",
            output_dir=run_dir,
        )

        on_disk = json.loads((run_dir / "meta.json").read_text())
        assert on_disk["runtime_override"] == {
            "phase": "plan", "runtime": "codex", "model": "gpt5",
            "decided_at": on_disk["runtime_override"]["decided_at"], "note": None,
        }
        # And the resumed run can still read its triple back for application.
        assert _read_persisted_runtime_override(run_dir) == {
            "phase": "plan", "runtime": "codex", "model": "gpt5",
        }

    def test_plain_resume_session_init_writes_no_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A plain resume (no persisted record) must leave ``runtime_override``
        # absent after session init — the carry-forward is opt-in.
        from types import SimpleNamespace

        from pipeline.project.bootstrap import init_session_with_atexit

        monkeypatch.setattr(
            "pipeline.project.bootstrap.atexit.register", lambda fn: None,
        )
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "meta.json").write_text(json.dumps({"status": "failed"}))

        init_session_with_atexit(
            task="t",
            project_path=tmp_path / "proj",
            plugin=SimpleNamespace(name="p"),
            model="m",
            profile_name="default",
            session_mode=SimpleNamespace(value="auto"),
            change_handoff="uncommitted",
            output_dir=run_dir,
        )

        on_disk = json.loads((run_dir / "meta.json").read_text())
        assert "runtime_override" not in on_disk

    def test_read_persisted_override_absent_is_none(self, tmp_path: Path) -> None:
        assert _read_persisted_runtime_override(None) is None
        empty = tmp_path / "empty"
        empty.mkdir()
        assert _read_persisted_runtime_override(empty) is None  # no meta.json
        (empty / "meta.json").write_text(json.dumps({"status": "failed"}))
        assert _read_persisted_runtime_override(empty) is None  # no record

    def test_override_applies_to_named_phase_only(self) -> None:
        base = _synth(_FakeProvider(), None)
        overridden = _synth(
            _FakeProvider(),
            {"phase": "plan", "runtime": "codex", "model": "gpt5"},
        )
        # Named phase swapped to the override pair.
        assert overridden.plan_agent.runtime == "codex"
        assert overridden.plan_agent.model == "gpt5"
        # Every other slot is byte-identical to the no-override baseline —
        # the override does not leak across phases.
        for slot in _SLOTS:
            if slot == "plan_agent":
                continue
            base_agent = getattr(base, slot)
            ovr_agent = getattr(overridden, slot)
            assert (ovr_agent.runtime, ovr_agent.model) == (
                base_agent.runtime, base_agent.model
            ), slot

    def test_no_override_is_unchanged(self) -> None:
        base = _synth(_FakeProvider(), None)
        again = _synth(_FakeProvider(), None)
        for slot in _SLOTS:
            a, b = getattr(base, slot), getattr(again, slot)
            assert (a.runtime, a.model) == (b.runtime, b.model), slot

    def test_override_for_review_phase_isolated(self) -> None:
        # review_changes shares its model with validate_plan / final_acceptance
        # in the fallback map; an override for review_changes must NOT touch the
        # other two reviewer slots.
        base = _synth(_FakeProvider(), None)
        overridden = _synth(
            _FakeProvider(),
            {"phase": "review_changes", "runtime": "claude", "model": "swapped"},
        )
        assert overridden.review_changes_agent.model == "swapped"
        assert overridden.review_changes_agent.runtime == "claude"
        for slot in ("validate_plan_agent", "final_acceptance_agent"):
            base_agent = getattr(base, slot)
            ovr_agent = getattr(overridden, slot)
            assert (ovr_agent.runtime, ovr_agent.model) == (
                base_agent.runtime, base_agent.model
            ), slot
