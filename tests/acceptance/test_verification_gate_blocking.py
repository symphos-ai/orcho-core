# SPDX-License-Identifier: Apache-2.0
"""ADR 0090 — a ``require`` verification gate cannot end in a green run.

End-to-end mock proof of the silent-skip incident fix: a project whose
contract schedules ``policy=require`` gates (after_phase implement +
before_delivery) with a broken environment (the gate command cannot
succeed) must NOT complete ``done``/approved — the run pauses at the gate
handoff (``verification_gate_failed``) where the operator can halt, retry,
or ``continue_with_waiver``. The failed receipt is persisted so readiness /
evidence see the same proof routing acted on.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline
from tests.acceptance.test_full_mock_flow import (
    _build_clean_review_provider,
    _init_git_repo,
)

# A command that exists nowhere — models a broken verification env (the
# incident: gates that cannot actually run on this host).
_BROKEN_ARGV = ["orcho-test-definitely-missing-binary"]

GATED_PLUGIN = PluginConfig(
    name="Gated Acceptance Project",
    language="Python",
    work_mode="pro",
    verification={
        "commands": {"gate": {"run": _BROKEN_ARGV}},
        "required": ["gate"],
        "gate_sets": {"required": {"commands": ["gate"]}},
        "selection": [{"always": ["required"]}],
        "schedule": [
            {"after_phase": "implement", "policy": "require",
             "action": "repair_loop", "commands": ["gate"]},
            {"before_delivery": True, "policy": "require",
             "action": "handoff", "commands": ["gate"]},
        ],
    },
)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@pytest.mark.git_worktree
@pytest.mark.filesystem_heavy
class TestRequireGateBlocksGreenRun:
    def _run(self, tmp_path: Path) -> tuple[dict, Path]:
        project = tmp_path / "proj"
        _init_git_repo(project)
        run_dir = tmp_path / "runs" / "20260613_000000"
        run_dir.mkdir(parents=True)
        with patch(
            "pipeline.project.session_run.load_plugin",
            return_value=GATED_PLUGIN,
        ):
            session = run_pipeline(
                task="Add structured logging",
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=1,
                profile_name="feature",
                provider=_build_clean_review_provider(),
            )
        return session, run_dir

    def test_broken_required_gate_pauses_run(self, tmp_path: Path) -> None:
        session, run_dir = self._run(tmp_path)

        # The run must NOT be done — it pauses at the gate handoff where
        # only an explicit operator decision (halt / retry / waiver) can
        # move it forward.
        assert session.get("status") == "awaiting_phase_handoff"

        handoff = session.get("phase_handoff") or {}
        assert handoff.get("trigger") == "verification_gate_failed"
        assert "continue_with_waiver" in (
            handoff.get("available_actions") or ()
        )

    def test_failed_gate_receipt_is_persisted(self, tmp_path: Path) -> None:
        _session, run_dir = self._run(tmp_path)

        receipts_dir = run_dir / "verification_command_receipts"
        files = sorted(p.name for p in receipts_dir.glob("*.json"))
        assert files == ["gate.json"]
        receipt = json.loads((receipts_dir / "gate.json").read_text())
        assert receipt["command"] == "gate"
        assert receipt["exit_code"] != 0  # None (spawn failure) or non-zero

    def test_gate_commands_ran_in_worktree_not_project(
        self, tmp_path: Path,
    ) -> None:
        """The receipt's cwd must be the run worktree checkout — the
        incident ran gates against the pristine original project and
        vacuously passed."""
        _session, run_dir = self._run(tmp_path)

        receipt = json.loads(
            (run_dir / "verification_command_receipts" / "gate.json")
            .read_text(),
        )
        project = str(tmp_path / "proj")
        assert receipt["placeholders"]["checkout"] != project
        assert "checkout" in receipt["placeholders"]["checkout"]
        assert receipt["placeholders"]["project"] == project
