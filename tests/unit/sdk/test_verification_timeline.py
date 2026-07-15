"""Unit tests for sdk.verification_timeline — the read-only durable projection.

Pins the load-bearing invariants of :func:`sdk.get_verification_timeline`:

* per-gate ``status`` is EXACTLY one of the six values
  ``{PASS,FAIL,MISSING,STALE,SKIPPED,FRESH}`` — never ``MANUAL``;
* a manual/operator-only gate is ``SKIPPED`` with ``policy='manual_only'``,
  present in the aggregate ``manual_only`` set, and carries NO ``rerun_hint``;
* every missing / stale / failed required gate carries a non-empty per-gate
  ``rerun_hint`` (scoped to its own command) and ``searched_run_dirs``; present /
  manual gates carry neither;
* the per-gate hint is a subset of the aggregate ``suggested_commands``;
* present / missing / stale(+reason) / failed / inherited / fresh / empty cases;
* the result is JSON-able and the projection writes nothing under the run dir.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sdk import to_jsonable
from sdk.verification_timeline import (
    GATE_STATUSES,
    VerificationTimelineProjection,
    get_verification_timeline,
)

# A contract with four required commands; ``manual_gate`` is also closed behind a
# ``manual_only`` schedule so its effective policy is ``manual_only``.
_PLUGIN = '''\
PLUGIN = {
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "required": ["lint", "unit", "smoke", "manual_gate"],
        "commands": {
            "lint": {"run": "echo lint"},
            "unit": {"run": "echo unit"},
            "smoke": {"run": "echo smoke"},
            "manual_gate": {"run": "echo manual"},
        },
        "gate_sets": {"manuals": {"commands": ["manual_gate"]}},
        "schedule": [
            {"manual_only": True, "gate_sets": ["manuals"]},
        ],
    },
}
'''

_EMPTY_PLUGIN = "PLUGIN = {}\n"

# A contract whose required ``env-provenance`` gate is scheduled at
# after_phase(implement), so a failed implement verification_environment receipt
# downgrades it (ADR 0125).
_PROV_PLUGIN = '''\
PLUGIN = {
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "required": ["env-provenance"],
        "commands": {
            "env-provenance": {"run": "echo prov"},
        },
        "schedule": [
            {
                "after_phase": "implement",
                "policy": "require",
                "commands": ["env-provenance"],
            },
        ],
    },
}
'''

# Same phase-scheduled gate, but ALSO marked manual_only: the overlay still
# downgrades it, but a manual gate must stay SKIPPED — never FAIL/blocking.
_PROV_MANUAL_PLUGIN = '''\
PLUGIN = {
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "required": ["env-provenance"],
        "commands": {
            "env-provenance": {"run": "echo prov"},
        },
        "schedule": [
            {
                "after_phase": "implement",
                "policy": "warn",
                "commands": ["env-provenance"],
            },
            {"manual_only": True, "commands": ["env-provenance"]},
        ],
    },
}
'''


def _write_phase_receipt(
    run_dir: Path, phase: str, *, round: int = 1, checks: list[dict],
) -> Path:
    """Write a verification_environment phase receipt via the real writer."""
    from pipeline.evidence.verification_receipt import write_verification_receipt

    path = write_verification_receipt(
        output_dir=run_dir, phase=phase, round=round, cwd=run_dir, checks=checks,
    )
    assert path is not None
    return path


def _write_project(root: Path, plugin: str = _PLUGIN, name: str = "project") -> Path:
    project = root / name
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(plugin, encoding="utf-8")
    return project


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@orcho.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("# x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _write_meta_run(
    runs_dir: Path,
    run_id: str,
    *,
    project: Path,
    extra_meta: dict | None = None,
) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    meta: dict = {
        "task": "t",
        "status": "done",
        "project": str(project),
        "worktree": {"isolation": "off"},
    }
    if extra_meta:
        meta.update(extra_meta)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def _write_receipt(
    run_dir: Path,
    command: str,
    *,
    exit_code: int = 0,
    detail: str = "",
    git: dict | None = None,
) -> Path:
    receipts = run_dir / "verification_command_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 1,
        "kind": "verification_command",
        "command": command,
        "env": "ci",
        "exit_code": exit_code,
        "assertions": [],
        "detail": detail,
        "git": git or {
            "checkout_head": None,
            "baseline_head": None,
            "changed_files_fingerprint": None,
        },
    }
    path = receipts / f"{command}.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch) -> Path:
    rd = tmp_path / "runs"
    rd.mkdir()
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    return rd


def _gates_by_command(projection: VerificationTimelineProjection) -> dict:
    return {g.command: g for g in projection.gates}


class TestStatusEnum:
    def test_present_missing_failed_and_manual(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
        # lint present (exit 0), smoke failed (exit 1), unit missing (no receipt),
        # manual_gate missing but policy manual_only -> SKIPPED.
        _write_receipt(run_dir, "lint", exit_code=0)
        _write_receipt(run_dir, "smoke", exit_code=1)

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gates = _gates_by_command(projection)

        assert gates["lint"].status == "PASS"
        assert gates["smoke"].status == "FAIL"
        assert gates["unit"].status == "MISSING"
        assert gates["manual_gate"].status == "SKIPPED"

        # Every status drawn from exactly the six legal values, no MANUAL.
        assert all(g.status in GATE_STATUSES for g in projection.gates)
        assert "MANUAL" not in {g.status for g in projection.gates}

        # manual_gate: SKIPPED + policy manual_only + in aggregate, no rerun_hint.
        assert gates["manual_gate"].policy == "manual_only"
        assert "manual_gate" in projection.manual_only
        assert gates["manual_gate"].rerun_hint == ()
        assert gates["manual_gate"].searched_run_dirs == ()
        # manual is NOT a missing required gate.
        assert "manual_gate" not in projection.residual_missing


def test_lost_isolated_subject_degrades_to_empty_timeline(
    tmp_path: Path, runs_dir: Path,
) -> None:
    """Read-only timeline never treats canonical checkout as a lost worktree."""
    project = _write_project(tmp_path)
    run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["worktree"] = {"isolation": "worktree", "path": str(tmp_path / "gone")}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    projection = get_verification_timeline(
        project=str(project), run_id="20260101_000000",
    )

    assert projection.has_contract is False
    assert projection.gates == ()

    def test_failed_present_and_missing_in_aggregate_buckets(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
        _write_receipt(run_dir, "lint", exit_code=0)
        _write_receipt(run_dir, "smoke", exit_code=1)

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        assert projection.residual_failed == ("smoke",)
        assert "unit" in projection.residual_missing
        assert projection.has_contract is True


class TestPerGateRemediation:
    def test_missing_required_gate_carries_scoped_hint_and_searched_dirs(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
        # Make lint/smoke present so unit is the only deficit -> per-gate hint
        # equals the aggregate exactly.
        _write_receipt(run_dir, "lint", exit_code=0)
        _write_receipt(run_dir, "smoke", exit_code=0)

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gates = _gates_by_command(projection)
        unit = gates["unit"]

        assert unit.status == "MISSING"
        assert unit.rerun_hint  # non-empty
        assert unit.searched_run_dirs == (str(run_dir),)
        # The per-gate hint is built by the same suggested_verify_commands logic,
        # scoped to this one command; with a single deficit it equals the
        # aggregate exactly. (``unit`` is in ``required`` so the run line takes the
        # compact ``--required`` form rather than a positional name.)
        assert tuple(unit.rerun_hint) == projection.suggested_commands
        assert any(line.startswith("orcho verify") for line in unit.rerun_hint)
        # Present gates carry neither hint nor searched dirs.
        assert gates["lint"].rerun_hint == ()
        assert gates["lint"].searched_run_dirs == ()

    def test_per_gate_hint_is_subset_of_aggregate(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
        # lint missing AND unit failed -> two distinct deficits in the aggregate.
        _write_receipt(run_dir, "smoke", exit_code=0)
        _write_receipt(run_dir, "unit", exit_code=1)

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gates = _gates_by_command(projection)
        aggregate = set(projection.suggested_commands)
        # Each non-present required gate's env-line(s) appear in the aggregate.
        for command in ("lint", "unit"):
            gate = gates[command]
            assert gate.rerun_hint
            env_lines = [ln for ln in gate.rerun_hint if ln.startswith("orcho verify env")]
            assert env_lines
            assert set(env_lines).issubset(aggregate)


class TestStale:
    def test_stale_required_gate_with_reason(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        # A git checkout so the current HEAD is known; a receipt recorded against
        # a different HEAD then classifies stale with a HEAD-move reason.
        project = _write_project(tmp_path)
        _init_repo(project)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
        _write_receipt(run_dir, "lint", exit_code=0)
        _write_receipt(run_dir, "smoke", exit_code=0)
        _write_receipt(
            run_dir, "unit", exit_code=0,
            git={"checkout_head": "deadbeef0000", "baseline_head": None,
                 "changed_files_fingerprint": None},
        )

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gates = _gates_by_command(projection)
        unit = gates["unit"]
        assert unit.status == "STALE"
        assert unit.stale_reason  # populated for stale
        assert "HEAD moved" in unit.stale_reason
        assert "unit" in projection.residual_stale
        assert unit.rerun_hint  # stale required gate is remediable


class TestFresh:
    def test_present_command_in_skipped_fresh_is_fresh(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        autorun = {
            "phase_log": {
                "final_acceptance": {
                    "verification_autorun": {
                        "attempted": True,
                        "reason": "pre-final",
                        "ran_envs": ["ci"],
                        "ran_commands": ["unit"],
                        "skipped_manual": ["manual_gate"],
                        "skipped_fresh": ["lint"],
                        "failed": [],
                        "errors": [],
                        "receipt_paths": [],
                        "phase": "final_acceptance",
                        "source": "stage9_autorun",
                    },
                },
            },
        }
        run_dir = _write_meta_run(
            runs_dir, "20260101_000000", project=project, extra_meta=autorun,
        )
        _write_receipt(run_dir, "lint", exit_code=0)
        _write_receipt(run_dir, "unit", exit_code=0)
        _write_receipt(run_dir, "smoke", exit_code=0)

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gates = _gates_by_command(projection)
        # lint was skipped_fresh -> FRESH; unit ran -> PASS.
        assert gates["lint"].status == "FRESH"
        assert gates["unit"].status == "PASS"
        # The auto-run mirror is reflected on the projection.
        assert projection.autorun_events
        event = projection.autorun_events[0]
        assert event.source == "stage9_autorun"
        assert event.ran_pass == ("unit",)
        assert event.skipped_fresh == ("lint",)


class TestEnvironmentProvenance:
    """ADR 0125: a failed verification_environment phase receipt downgrades the
    gate scheduled at that phase to FAIL with self-sufficient operator-evidence,
    even when the gate's own command receipt is present/fresh."""

    def test_failed_provenance_downgrades_present_gate_to_fail(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path, plugin=_PROV_PLUGIN, name="prov")
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
        # Fresh, passing command receipt for the gate ...
        _write_receipt(run_dir, "env-provenance", exit_code=0)
        # ... but the implement phase's environment provenance broke.
        phase_path = _write_phase_receipt(
            run_dir, "implement",
            checks=[{
                "name": "pipeline_import",
                "expected": "/abs/checkout/pipeline/__init__.py",
                "actual": "/abs/install/pipeline/__init__.py",
                "passed": False,
            }],
        )

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gates = _gates_by_command(projection)
        gate = gates["env-provenance"]

        # Status is FAIL — not FRESH, not PASS.
        assert gate.status == "FAIL"
        assert "env-provenance" in projection.residual_failed
        # Operator-evidence without raw logs: failing check + expected/actual.
        assert gate.detail.startswith("pipeline_import:")
        assert "/abs/checkout/pipeline/__init__.py" in gate.detail
        assert "/abs/install/pipeline/__init__.py" in gate.detail
        # receipt_path points at the verification_environment phase receipt.
        assert gate.receipt_path == str(phase_path)
        # Remediable like any non-present required gate.
        assert gate.rerun_hint
        assert any(line.startswith("orcho verify") for line in gate.rerun_hint)
        # Status stays inside the six legal values — no new enum member.
        assert all(g.status in GATE_STATUSES for g in projection.gates)

    def test_healthy_provenance_leaves_fresh_gate_fresh(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        autorun = {
            "phase_log": {
                "final_acceptance": {
                    "verification_autorun": {
                        "attempted": True,
                        "ran_commands": [],
                        "skipped_fresh": ["env-provenance"],
                        "skipped_manual": [],
                        "failed": [],
                        "receipt_paths": [],
                        "phase": "final_acceptance",
                        "source": "stage9_autorun",
                    },
                },
            },
        }
        project = _write_project(tmp_path, plugin=_PROV_PLUGIN, name="prov_ok")
        run_dir = _write_meta_run(
            runs_dir, "20260101_000000", project=project, extra_meta=autorun,
        )
        _write_receipt(run_dir, "env-provenance", exit_code=0)
        # Healthy implement provenance: every check passed.
        _write_phase_receipt(
            run_dir, "implement",
            checks=[{
                "name": "pipeline_import",
                "expected": "/abs/checkout/pipeline/__init__.py",
                "actual": "/abs/checkout/pipeline/__init__.py",
                "passed": True,
            }],
        )

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gate = _gates_by_command(projection)["env-provenance"]

        # No provenance break -> the prior path stands: skipped_fresh -> FRESH.
        assert gate.status == "FRESH"
        assert gate.detail == ""
        assert "env-provenance" not in projection.residual_failed
        assert gate.rerun_hint == ()

    def test_manual_only_provenance_gate_stays_skipped(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(
            tmp_path, plugin=_PROV_MANUAL_PLUGIN, name="prov_manual",
        )
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
        _write_receipt(run_dir, "env-provenance", exit_code=0)
        _write_phase_receipt(
            run_dir, "implement",
            checks=[{
                "name": "pipeline_import",
                "expected": "/abs/checkout/pipeline/__init__.py",
                "actual": "/abs/install/pipeline/__init__.py",
                "passed": False,
            }],
        )

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gate = _gates_by_command(projection)["env-provenance"]

        # A manual gate is never escalated by the overlay: SKIPPED, not FAIL,
        # and never in the failed residual or carrying a rerun hint.
        assert gate.status == "SKIPPED"
        assert gate.policy == "manual_only"
        assert gate.detail == ""
        assert gate.rerun_hint == ()
        assert "env-provenance" in projection.manual_only
        assert "env-provenance" not in projection.residual_failed


class TestInherited:
    def test_present_receipt_inherited_from_parent_run(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        from core.io.git_helpers import git_head
        from pipeline.verification_dependencies import changed_files_fingerprint

        project = _write_project(tmp_path)
        _init_repo(project)
        fingerprint = changed_files_fingerprint(str(project))
        head = git_head(str(project))

        # Parent run holds a present receipt for ``unit`` proving THIS diff.
        parent_dir = _write_meta_run(runs_dir, "20251231_235959", project=project)
        _write_receipt(
            parent_dir, "unit", exit_code=0,
            git={"checkout_head": head, "baseline_head": None,
                 "changed_files_fingerprint": fingerprint},
        )

        # Follow-up run: present receipts for the others, NO ``unit`` receipt, and
        # durable parent linkage in meta.
        run_dir = _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            extra_meta={
                "parent_run_id": "20251231_235959",
                "parent_run_dir": str(parent_dir),
            },
        )
        _write_receipt(run_dir, "lint", exit_code=0)
        _write_receipt(run_dir, "smoke", exit_code=0)

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        gates = _gates_by_command(projection)
        unit = gates["unit"]
        assert unit.status == "PASS"
        assert unit.inherited is True
        assert unit.source_run_id == "20251231_235959"
        assert any("20251231_235959" in line for line in projection.inherited)
        assert str(parent_dir) in projection.searched_run_dirs


class TestEmptyAndDegrade:
    def test_no_contract_returns_empty_projection(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path, plugin=_EMPTY_PLUGIN, name="empty")
        _write_meta_run(runs_dir, "20260101_000000", project=project)

        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        assert projection.has_contract is False
        assert projection.gates == ()
        assert projection.residual_missing == ()
        assert projection.run_id == "20260101_000000"

    def test_run_not_found_raises(self, tmp_path: Path, runs_dir: Path) -> None:
        from sdk.errors import RunNotFound

        with pytest.raises(RunNotFound):
            get_verification_timeline(run_id="does_not_exist")


class TestContract:
    def test_result_is_jsonable_and_writes_nothing(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        run_dir = _write_meta_run(runs_dir, "20260101_000000", project=project)
        _write_receipt(run_dir, "lint", exit_code=0)

        before = sorted(p.name for p in run_dir.iterdir())
        projection = get_verification_timeline(
            project=str(project), run_id="20260101_000000",
        )
        after = sorted(p.name for p in run_dir.iterdir())
        # Read-only: no new files / dirs under the run dir.
        assert before == after

        payload = to_jsonable(projection)
        # Round-trips through json with no custom encoder.
        text = json.dumps(payload)
        assert json.loads(text)["run_id"] == "20260101_000000"
        # The scheduled-trail gap is reported, never silently dropped.
        assert payload["scheduled_trail_available"] is False
        assert payload["scheduled_trail_gap"]
