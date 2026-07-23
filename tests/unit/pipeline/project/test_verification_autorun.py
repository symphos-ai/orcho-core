"""Unit coverage for Stage 9 required-receipt auto-run (ADR 0094).

Every test mocks ``sdk.verify.verify_env`` / ``sdk.verify.verify_run`` — no real
subprocess or model runs. The mocks are deterministic: ``verify_run`` writes
fresh command-receipts to the run dir (simulating materialization) and returns
typed outcomes, so the post-run classification reads real on-disk evidence.

Cases mirror the acceptance criteria:

1. missing required env+command -> auto-run generates them; classify -> present
2. fresh passing required -> skipped_fresh, verify_run NOT called
3. stale required -> rerun exactly once, fresh result
4. failed auto-run -> failed receipt saved, in result.failed, classifies failed
5. manual/operator-only required -> skipped_manual, not run, delivery blocked
6. delivery assessment sees the auto-generated receipts (missing empty)
7. dry-run -> attempted=False, sdk.verify untouched
8. no-contract / empty required -> no-op
9. persisted evidence (F3): state.extras list + session phase_log mirror
10. required+manual in gate_rerun (F2): required_passed not True, skipped_manual

Plus the T3 regression for ADR 0094's full-extras targeting: a path-selected
delivery gate (``cli-sdk-unit``, selected only for ``sdk/**`` touches) is
materialized when the cached ``before_delivery`` routing plan is threaded through
``extras`` even on a clean live checkout, and is NOT targeted from a stripped
``extras`` (the old fresh-plan-from-worktree behaviour) — proving readiness and
delivery stay consistent after the auto-run.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.evidence.verification_receipt import (
    COMMAND_RECEIPTS_DIRNAME,
)
from pipeline.plugins import PluginConfig
from pipeline.project.verification_autorun import (
    ReceiptAutoRunResult,
    auto_run_required_receipts,
    materialize_required_receipts,
    select_before_delivery_epoch,
)
from pipeline.project.verification_timeline import (
    VerificationGateEvent,
    VerificationTimeline,
    build_verification_timeline,
    render_gate_live_block,
    render_verification_gate_done_block,
)
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)
from pipeline.verification_ledger_store import load_ledger
from pipeline.verification_readiness import (
    ROUTING_PLANS_EXTRAS_KEY,
    classify_required_receipts,
    suggested_verify_commands,
)
from pipeline.verification_receipt_index import (
    VERIFICATION_PARENT_RUNS_EXTRAS_KEY,
    receipt_file_path,
)
from pipeline.verification_subject import VerificationSubjectAvailable
from tests.fixtures.verification_subject import (
    DEFAULT_VERIFICATION_SUBJECT,
    FakeVerificationSubjectCapture,
    fake_verification_subject_capture as fake_verification_subject_capture,
)

pytestmark = pytest.mark.usefixtures("fake_verification_subject_capture")

# ── contract + receipt fixtures ───────────────────────────────────────────


def _contract(
    required: list[str],
    *,
    manual: tuple[str, ...] = (),
    default_env: str = "ci",
    per_cmd_env: dict[str, str] | None = None,
) -> VerificationContract:
    """A minimal projected contract: every ``required`` name is a declared
    command. ``manual`` names are also parked behind a ``manual_only`` schedule
    so the materializer must withhold them."""
    per_cmd_env = per_cmd_env or {}
    commands: dict[str, Any] = {}
    for name in required:
        spec: dict[str, Any] = {"run": "true"}
        if name in per_cmd_env:
            spec["env"] = per_cmd_env[name]
        commands[name] = spec
    automatic = [name for name in required if name not in manual]
    verification: dict[str, Any] = {
        "default_env": default_env,
        "required": list(required),
        "commands": commands,
        "schedule": [{
            "before_delivery": True,
            "policy": "require",
            "commands": automatic,
        }],
    }
    envs = {default_env: {}}
    for env_name in per_cmd_env.values():
        envs[env_name] = {}
    if manual:
        verification["gate_sets"] = {"manuals": {"commands": list(manual)}}
        verification["schedule"].append(
            {"manual_only": True, "gate_sets": ["manuals"]},
        )
    plugin = PluginConfig(verification_envs=envs, verification=verification)
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _write_receipt(
    run_dir: Path,
    command: str,
    *,
    exit_code: int = 0,
    env: str = "ci",
    checkout_head: str | None = None,
    fingerprint: str | None = None,
    detail: str = "",
    assertions: list[dict[str, Any]] | None = None,
    checkout: Path | None = None,
) -> Path:
    """Write one on-disk command-receipt with exactly the fields classification
    reads (``exit_code`` / ``assertions`` / ``detail`` / ``git`` provenance)."""
    from pipeline.verification_subject import (
        VerificationSubjectAvailable,
        VerificationSubjectIdentity,
        capture_verification_subject,
    )

    candidates = [checkout] if checkout is not None else []
    candidates.append(run_dir.parent / "checkout")
    if len(run_dir.parents) > 3:
        candidates.append(run_dir.parents[3] / "proj")
    candidate = next((path for path in candidates if path.is_dir()), None)
    captured = capture_verification_subject(candidate) if candidate else None
    subject = captured.identity if isinstance(captured, VerificationSubjectAvailable) else None
    if subject is not None and checkout_head is not None and checkout_head != subject.observed_head_oid:
        identity = subject
        subject = VerificationSubjectIdentity(
            identity.version, identity.object_format, identity.tree_oid,
            checkout_head, identity.baseline_oid,
        )

    serialized_subject: dict[str, Any]
    if subject is None:
        serialized_subject = {"status": "unavailable", "reason": "identity_unavailable"}
    else:
        serialized_subject = {
            "status": "available",
            "identity": {
                "version": subject.version,
                "object_format": subject.object_format,
                "tree_oid": subject.tree_oid,
                "observed_head_oid": subject.observed_head_oid,
                "baseline_oid": subject.baseline_oid,
            },
        }

    rdir = run_dir / COMMAND_RECEIPTS_DIRNAME
    rdir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 3,
        "kind": "verification_command",
        "command": command,
        "env": env,
        "exit_code": exit_code,
        "assertions": assertions or [],
        "detail": detail,
        "git": {
            "checkout_head": checkout_head,
            "baseline_head": None,
            "changed_files_fingerprint": fingerprint,
        },
        "subject": serialized_subject,
        "dependencies": [],
    }
    path = rdir / f"{command}.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "README.md").write_text("# x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return head.stdout.strip()


class _Recorder:
    """Mock ``sdk.verify`` pair. ``verify_run`` writes fresh present-or-failed
    receipts so post-run classification reflects the materialization."""

    def __init__(
        self,
        run_dir: Path,
        *,
        exit_codes: dict[str, int] | None = None,
        write: bool = True,
        env_all_passed: bool = True,
        assertions: dict[str, list[dict[str, Any]]] | None = None,
        details: dict[str, str] | None = None,
        production_writer: bool = False,
    ) -> None:
        self.run_dir = run_dir
        self.exit_codes = exit_codes or {}
        self.write = write
        self.env_all_passed = env_all_passed
        # Per-command receipt content written at exit 0 to exercise the
        # authoritative rollup: a failed assertion or a non-empty detail must
        # surface as failed even though ``verify_run`` reports exit 0.
        self.assertions = assertions or {}
        self.details = details or {}
        self.production_writer = production_writer
        self.env_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []

    def verify_env(self, **kwargs: Any) -> Any:
        self.env_calls.append(kwargs)
        return SimpleNamespace(
            all_passed=self.env_all_passed, receipt_path=self.run_dir / "env.json",
        )

    def verify_run(self, **kwargs: Any) -> Any:
        self.run_calls.append(kwargs)
        outcomes = []
        for name in kwargs["commands"]:
            code = self.exit_codes.get(name, 0)
            receipt_path = None
            if self.write:
                if self.production_writer:
                    from pipeline.evidence.verification_receipt import write_command_receipt
                    from pipeline.verification_subject import capture_verification_subject

                    checkout = Path(kwargs["subject_checkout"])
                    receipt_path = write_command_receipt(
                        output_dir=self.run_dir,
                        result={
                            "command": name, "env": "ci", "cwd": str(checkout),
                            "placeholders": {"checkout": str(checkout), "project": kwargs["project"]},
                            "argv": ["true"], "env_overrides": {},
                            "assertions": self.assertions.get(name, []), "exit_code": code,
                            "duration_s": 0.0, "stdout_tail": "", "stderr_tail": "",
                            "log_path": None, "parity": "absolute",
                            "detail": self.details.get(name, ""), "git": {},
                            "subject": capture_verification_subject(checkout), "dependencies": [],
                        },
                    )
                else:
                    receipt_path = _write_receipt(
                        self.run_dir, name, exit_code=code,
                        env=kwargs.get("env", "ci"),
                        assertions=self.assertions.get(name),
                        detail=self.details.get(name, ""),
                        checkout=Path(kwargs["subject_checkout"]),
                    )
            outcomes.append(SimpleNamespace(
                command=name, exit_code=code, receipt_path=receipt_path,
            ))
        return SimpleNamespace(
            outcomes=outcomes,
            all_passed=all(o.exit_code == 0 for o in outcomes),
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
        monkeypatch.setattr("sdk.verify.verify_env", self.verify_env)
        monkeypatch.setattr("sdk.verify.verify_run", self.verify_run)
        return self


def _ctx(contract: VerificationContract, *, checkout: Path, project: Path,
         workspace: Path, run_dir: Path) -> Any:
    return placeholder_context_for(
        contract, checkout=str(checkout), project=str(project),
        workspace=str(workspace), run_dir=str(run_dir),
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return ``(project, workspace, run_dir)`` with the standard run layout."""
    project = tmp_path / "proj"
    project.mkdir()
    workspace = tmp_path / "ws"
    run_dir = workspace / "runspace" / "runs" / "rid"
    run_dir.mkdir(parents=True)
    return project, workspace, run_dir


# ── case 1: missing -> materialized present ───────────────────────────────


def test_missing_required_is_materialized_then_classifies_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint", "unit"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.attempted is True
    assert result.ran_commands == ("lint", "unit")
    assert result.ran_envs == ("ci",)
    assert result.receipt_paths  # command receipts recorded
    # One env pass, one command pass — no retry loop.
    assert len(rec.env_calls) == 1
    assert len(rec.run_calls) == 1
    # Post-materialization, both required receipts now classify present.
    after = classify_required_receipts(
        contract, run_dir, ctx, checkout=str(project),
    )
    assert {c: cl.status for c, cl in after.items()} == {
        "lint": "present", "unit": "present",
    }


def test_materializer_threads_subject_checkout_into_both_verify_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved subject checkout is pinned through to verify_run, so gates
    execute against the run's real worktree even when meta lacks one."""
    project, workspace, run_dir = _layout(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=worktree, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(worktree), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert rec.env_calls[0]["subject_checkout"] == str(worktree)
    assert rec.run_calls[0]["subject_checkout"] == str(worktree)
    for call in (*rec.env_calls, *rec.run_calls):
        assert call["run_id"] == run_dir.name
        assert call["runs_dir"] == run_dir.parent


def test_nested_stage9_uses_child_locator_and_real_sdk_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child alias is resolved only below its physical parent run directory."""
    workspace = tmp_path / "workspace"
    runs_dir = workspace / "runspace" / "runs"
    child_dir = runs_dir / "parent-a" / "alias"
    child_dir.mkdir(parents=True)
    project = tmp_path / "project"
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    plugin = {
        "verification_envs": {"ci": {}},
        "verification": {
            "default_env": "ci",
            "required": ["engine", "manual"],
            "commands": {
                "engine": {
                    "run": (
                        "python -c \"from pathlib import Path; "
                        "Path('engine-count.txt').open('a').write('x')\""
                    ),
                },
                "manual": {
                    "run": (
                        "python -c \"from pathlib import Path; "
                        "Path('manual-count.txt').open('a').write('x')\""
                    ),
                },
            },
            "gate_sets": {"manuals": {"commands": ["manual"]}},
            "schedule": [
                {"before_delivery": True, "policy": "require", "commands": ["engine"]},
                {"manual_only": True, "gate_sets": ["manuals"]},
            ],
        },
    }
    (plugin_dir / "plugin.py").write_text(f"PLUGIN = {plugin!r}\n", encoding="utf-8")
    (child_dir / "meta.json").write_text(json.dumps({
        "task": "nested", "status": "running", "project": str(project),
        "worktree": {"isolation": "off"},
    }), encoding="utf-8")

    # Same alias elsewhere must not influence resolution or receive receipts.
    root_alias = runs_dir / "alias"
    other_alias = runs_dir / "parent-b" / "alias"
    root_alias.mkdir()
    other_alias.mkdir(parents=True)

    from pipeline.plugins import load_plugin

    contract = VerificationContract.from_plugin(load_plugin(project))
    assert contract is not None
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=child_dir)
    _write_receipt(child_dir, "engine", checkout_head="0" * 40, checkout=project)
    assert classify_required_receipts(
        contract, child_dir, ctx, checkout=str(project),
    )["engine"].status == "stale"

    import sdk.verify as verify_sdk

    real_find_run = verify_sdk.find_run
    find_calls: list[tuple[str | None, Path | str | None]] = []

    def find_run_spy(run_id: str | None = None, **kwargs: Any) -> Any:
        find_calls.append((run_id, kwargs.get("runs_dir")))
        return real_find_run(run_id, **kwargs)

    monkeypatch.setattr(verify_sdk, "find_run", find_run_spy)
    result = materialize_required_receipts(
        run_id=child_dir.name, run_dir=child_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    from pipeline.evidence.verification_receipt import ENV_RECEIPTS_DIRNAME

    assert result.ran_commands == ("engine",)
    assert result.skipped_manual == ("manual",)
    assert (project / "engine-count.txt").read_text(encoding="utf-8") == "x"
    assert not (project / "manual-count.txt").exists()
    assert (child_dir / ENV_RECEIPTS_DIRNAME / "verify_env_ci.json").is_file()
    assert (child_dir / COMMAND_RECEIPTS_DIRNAME / "engine.json").is_file()
    assert not (root_alias / ENV_RECEIPTS_DIRNAME).exists()
    assert not (root_alias / COMMAND_RECEIPTS_DIRNAME).exists()
    assert not (other_alias / ENV_RECEIPTS_DIRNAME).exists()
    assert not (other_alias / COMMAND_RECEIPTS_DIRNAME).exists()
    assert find_calls == [
        (child_dir.name, child_dir.parent),
        (child_dir.name, child_dir.parent),
    ]
    assert classify_required_receipts(
        contract, child_dir, ctx, checkout=str(project),
    )["engine"].status == "present"


# ── case 2: fresh -> skipped, verify_run not called ───────────────────────


def test_fresh_passing_required_skipped_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint", "unit"])
    # Pre-existing passing receipts; non-git checkout => no staleness asserted.
    _write_receipt(run_dir, "lint", exit_code=0)
    _write_receipt(run_dir, "unit", exit_code=0)
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.attempted is True
    assert set(result.skipped_fresh) == {"lint", "unit"}
    assert result.ran_commands == ()
    assert rec.run_calls == []  # nothing executed
    assert rec.env_calls == []


# ── case 3: stale -> rerun once ───────────────────────────────────────────


def test_stale_required_is_rerun_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    # A real git checkout gives a concrete HEAD; a receipt recorded against a
    # different head is stale.
    checkout = tmp_path / "checkout"
    head = _init_repo(checkout)
    assert head  # sanity
    contract = _contract(["lint"])
    _write_receipt(
        run_dir, "lint", exit_code=0, checkout_head="0" * 40,
        checkout=checkout,
    )
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    # Pre-condition: classifies stale before the run.
    before = classify_required_receipts(
        contract, run_dir, ctx, checkout=str(checkout),
    )
    assert before["lint"].status == "stale"

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(checkout), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.ran_commands == ("lint",)
    assert len(rec.run_calls) == 1  # exactly one rerun, no loop
    # Fresh receipt (no recorded head) is no longer stale.
    after = classify_required_receipts(
        contract, run_dir, ctx, checkout=str(checkout),
    )
    assert after["lint"].status == "present"


def test_phase_only_required_gate_is_refreshed_before_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale phase-only require gate gets the implicit delivery refresh."""
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    _init_repo(checkout)
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "delivery_policy": "require",
            "commands": {"lint": {"run": "true"}},
            "required": ["lint"],
            "schedule": [{
                "before_phase": "implement",
                "policy": "require",
                "commands": ["lint"],
            }],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    _write_receipt(run_dir, "lint", checkout_head="0" * 40)
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(checkout), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.ran_commands == ("lint",)
    assert result.residual_required == ()
    assert len(rec.run_calls) == 1


def test_before_delivery_operator_commands_are_not_required_residuals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual and suggested delivery gates stay operator-owned end to end."""
    project, workspace, run_dir = _layout(tmp_path)
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci",
            "commands": {
                "manual": {"run": "true"},
                "suggest": {"run": "true"},
            },
            "required": ["manual", "suggest"],
            "schedule": [
                {"before_delivery": True, "policy": "manual", "commands": ["manual"]},
                {"before_delivery": True, "policy": "suggest", "commands": ["suggest"]},
            ],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.skipped_manual == ("manual", "suggest")
    assert result.residual_required == ()
    assert rec.env_calls == [] and rec.run_calls == []
    live = "\n".join(render_gate_live_block(
        result, hook_label="pre-final auto-run",
    ))
    assert "manual SKIPPED MANUAL" in live
    assert "suggest SKIPPED MANUAL" in live
    assert "MISSING/STALE" not in live

    timeline = build_verification_timeline(
        run_dir=run_dir,
        extras={
            "verification_contract": contract,
            "verification_placeholders": ctx,
        },
    )
    assert timeline is not None
    assert timeline.manual_only == ("manual", "suggest")
    assert timeline.residual_missing == ()
    assert timeline.residual_stale == ()
    assert timeline.residual_failed == ()
    assert "operator available" in "\n".join(
        render_verification_gate_done_block(timeline),
    )


# ── case 4: failed auto-run stays failed ──────────────────────────────────


def test_failed_autorun_is_recorded_failed_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir, exit_codes={"lint": 1}).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.ran_commands == ("lint",)
    assert result.failed == ("lint",)
    # The failed receipt is on disk and classifies failed (never missing/green).
    after = classify_required_receipts(
        contract, run_dir, ctx, checkout=str(project),
    )
    assert after["lint"].status == "failed"


def test_failed_current_receipt_refreshes_once_after_typed_subject_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_verification_subject_capture: FakeVerificationSubjectCapture,
) -> None:
    """A tree change with a stable HEAD refreshes one failed receipt."""
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    _write_receipt(run_dir, "lint", exit_code=1, checkout=checkout)

    subject_a = fake_verification_subject_capture(checkout)
    assert isinstance(subject_a, VerificationSubjectAvailable)
    subject_b = fake_verification_subject_capture.set_identity(
        checkout,
        tree_oid="3" * 40,
        observed_head_oid=subject_a.identity.observed_head_oid,
    )
    assert subject_a.identity.tree_oid != subject_b.tree_oid
    assert subject_a.identity.observed_head_oid == subject_b.observed_head_oid

    rec = _Recorder(run_dir, production_writer=True).install(monkeypatch)
    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(checkout), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.ran_commands == ("lint",)
    assert len(rec.run_calls) == 1
    written = json.loads(Path(receipt_file_path(run_dir, "lint")).read_text())
    assert written["schema_version"] == 3
    assert written["subject"]["identity"]["tree_oid"] == subject_b.tree_oid
    assert classify_required_receipts(
        contract, run_dir, ctx, checkout=str(checkout),
    )["lint"].status == "present"


def test_failed_current_receipt_with_same_subject_is_not_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    _init_repo(checkout)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    _write_receipt(run_dir, "lint", exit_code=1, checkout=checkout)
    rec = _Recorder(run_dir).install(monkeypatch)

    materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(checkout), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert rec.run_calls == []
    assert classify_required_receipts(
        contract, run_dir, ctx, checkout=str(checkout),
    )["lint"].status == "failed"


def test_inherited_parent_pass_does_not_hide_stale_failed_current_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_verification_subject_capture: FakeVerificationSubjectCapture,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    _write_receipt(run_dir, "lint", exit_code=1, checkout=checkout)
    fake_verification_subject_capture.set_identity(checkout, tree_oid="3" * 40)
    parent_dir = tmp_path / "parent"
    _write_receipt(parent_dir, "lint", exit_code=0, checkout=checkout)
    extras = {VERIFICATION_PARENT_RUNS_EXTRAS_KEY: (("parent", str(parent_dir)),)}
    assert classify_required_receipts(
        contract, run_dir, ctx, checkout=str(checkout), extras=extras,
    )["lint"].status == "present"
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(checkout), contract=contract, ctx=ctx,
        workspace=str(workspace), extras=extras, reason="pre-final",
    )

    assert result.ran_commands == ("lint",)
    assert len(rec.run_calls) == 1


@pytest.mark.parametrize("subject", [
    {"status": "unavailable", "reason": "nope"},
    {"status": "available", "identity": {}},
    None,
])
def test_failed_receipt_without_usable_typed_subject_is_not_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subject: dict[str, Any] | None,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    _init_repo(checkout)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    path = _write_receipt(run_dir, "lint", exit_code=1, checkout=checkout)
    raw = json.loads(path.read_text())
    if subject is None:
        raw.pop("subject")
        raw["schema_version"] = 2  # historical receipt with no typed subject
    else:
        raw["subject"] = subject
    path.write_text(json.dumps(raw), encoding="utf-8")
    rec = _Recorder(run_dir).install(monkeypatch)

    materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(checkout), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert rec.run_calls == []


@pytest.mark.parametrize("policy", ["manual", "suggest"])
def test_failed_stale_operator_receipt_is_not_auto_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    _init_repo(checkout)
    (checkout / "README.md").write_text("A\n", encoding="utf-8")
    plugin = PluginConfig(
        verification_envs={"ci": {}},
        verification={
            "default_env": "ci", "commands": {"manual": {"run": "true"}},
            "required": ["manual"],
            "schedule": [{"before_delivery": True, "policy": policy, "commands": ["manual"]}],
        },
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    _write_receipt(run_dir, "manual", exit_code=1, checkout=checkout)
    (checkout / "README.md").write_text("B\n", encoding="utf-8")
    rec = _Recorder(run_dir).install(monkeypatch)

    materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(checkout), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert rec.run_calls == []


# ── case 4b: exit-0 but failed assertion/detail is authoritatively failed ──


def test_exit0_failed_assertion_is_failed_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command that exits 0 but whose receipt has a failed assertion must land
    in ``result.failed`` (authoritative), never read as green."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(
        run_dir,
        assertions={"lint": [{"name": "no-warnings", "passed": False}]},
    ).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.ran_commands == ("lint",)
    # Exit code was 0, yet the authoritative on-disk classification is failed.
    assert result.failed == ("lint",)
    assert "lint" not in result.residual_required
    after = classify_required_receipts(
        contract, run_dir, ctx, checkout=str(project),
    )
    assert after["lint"].status == "failed"


def test_exit0_nonempty_detail_is_failed_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command that exits 0 but whose receipt carries a non-empty execution
    ``detail`` is authoritatively failed, never green."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(
        run_dir, details={"lint": "differential baseline regression"},
    ).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.failed == ("lint",)
    after = classify_required_receipts(
        contract, run_dir, ctx, checkout=str(project),
    )
    assert after["lint"].status == "failed"


# ── case 5: manual/operator-only required is withheld ─────────────────────


def test_manual_required_is_skipped_and_blocks_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint", "manual_cmd"], manual=("manual_cmd",))
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.skipped_manual == ("manual_cmd",)
    assert result.ran_commands == ("lint",)
    # The manual command was never handed to verify_run.
    assert rec.run_calls[0]["commands"] == ["lint"]
    # The withheld manual/operator-only command is surfaced as a visible
    # manual-only gap, but per ADR 0097 it is NOT counted as a missing-required
    # receipt and is never blocking (manual_only never blocks delivery).
    from pipeline.verification_delivery import assess_delivery_verification
    assessment = assess_delivery_verification(
        contract, run_dir, ctx, extras=None, diff_cwd=project,
    )
    assert assessment is not None
    assert "manual_cmd" not in assessment.required_missing
    assert "manual_cmd" in assessment.operator_commands
    assert dict(assessment.policy_by_command)["manual_cmd"] == "manual"
    assert "lint" not in assessment.required_missing


# ── case 6: delivery assessment sees materialized receipts ────────────────


def test_delivery_assessment_sees_autorun_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.verification_delivery import assess_delivery_verification

    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint", "unit"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir).install(monkeypatch)

    # Before: both required receipts missing.
    before = assess_delivery_verification(
        contract, run_dir, ctx, extras=None, diff_cwd=project,
    )
    assert before is not None
    assert set(before.required_missing) == {"lint", "unit"}

    materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    after = assess_delivery_verification(
        contract, run_dir, ctx, extras=None, diff_cwd=project,
    )
    assert after is not None
    assert after.required_missing == ()


# ── case 7: dry-run is a strict no-op ─────────────────────────────────────


def test_dry_run_is_noop_without_calling_sdk_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), dry_run=True, reason="pre-final",
    )

    assert result.attempted is False
    assert rec.env_calls == []
    assert rec.run_calls == []


# ── case 8: no-contract / empty required ──────────────────────────────────


def test_no_contract_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=None, ctx=None,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.attempted is False
    assert rec.env_calls == [] and rec.run_calls == []


def test_empty_required_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract([])  # declared contract, but no required commands
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.attempted is False
    assert rec.env_calls == [] and rec.run_calls == []


# ── case 9: persisted evidence (F3) ───────────────────────────────────────


def _stub_run(project: Path, run_dir: Path, contract: Any, ctx: Any) -> Any:
    state = SimpleNamespace(
        dry_run=False,
        extras={
            "verification_contract": contract,
            "verification_placeholders": ctx,
        },
    )
    return SimpleNamespace(
        state=state, output_dir=run_dir, project_path=project,
        session={}, _effective_diff_cwd=lambda: project,
    )


@pytest.mark.parametrize(
    ("prior_exit", "stage9_exit", "disposition"),
    [(0, 1, "executed_fail"), (1, 0, "executed_pass")],
    ids=["pass-to-stage9-fail", "fail-to-stage9-pass"],
)
def test_stage9_execution_receipt_is_immutable_and_identity_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_exit: int,
    stage9_exit: int,
    disposition: str,
) -> None:
    """Stage 9 snapshots its own attempt before appending an execution event."""
    from pipeline.evidence.verification_receipt import (
        load_command_receipts,
        write_scheduled_command_receipt,
    )
    from pipeline.project.verification_ledger_runtime import finalize, record_execution

    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    run = _stub_run(project, run_dir, contract, ctx)
    run.state.output_dir = run_dir
    plan = select_before_delivery_epoch(run)
    entry = plan.entries[0]

    initial_flat = _write_receipt(
        run_dir, "lint", exit_code=prior_exit, checkout=project,
    )
    initial = json.loads(initial_flat.read_text(encoding="utf-8"))
    prior_evidence = write_scheduled_command_receipt(
        output_dir=run_dir, result=initial, hook=entry.hook, phase=entry.phase,
    )
    assert prior_evidence is not None
    record_execution(
        run, entry, passed=prior_exit == 0,
        receipt_evidence=prior_evidence.relative_to(run_dir).as_posix(),
    )
    # The Stage 9 materializer sees a genuinely absent current receipt; the
    # immutable prior attempt remains solely in the execution-evidence store.
    initial_flat.unlink()
    _Recorder(run_dir, exit_codes={"lint": stage9_exit}).install(monkeypatch)

    result = auto_run_required_receipts(
        run, "final_acceptance", reason="pre-final", delivery_plan=plan,
    )

    assert result.ran_commands == ("lint",)
    executions = [
        event for event in load_ledger(run_dir).trail
        if event.kind == "execution" and event.identity == (entry.command, entry.hook, entry.phase)
    ]
    assert [event.outcome for event in executions] == [
        "pass" if prior_exit == 0 else "fail",
        "pass" if stage9_exit == 0 else "fail",
    ]
    assert [event.rerun for event in executions] == [False, True]
    assert executions[0].receipt_evidence != executions[1].receipt_evidence
    assert executions[1].receipt_evidence is not None
    latest_evidence = run_dir / executions[1].receipt_evidence
    assert json.loads(latest_evidence.read_text(encoding="utf-8"))["exit_code"] == stage9_exit
    flat = load_command_receipts(run_dir)
    assert flat == [json.loads((run_dir / COMMAND_RECEIPTS_DIRNAME / "lint.json").read_text())]
    assert flat[0]["exit_code"] == stage9_exit

    closed = finalize(run)
    assert closed is not None
    row = next(row for row in closed.rows if row.identity == executions[-1].identity)
    assert row.disposition == disposition
    assert row.receipt_evidence == executions[-1].receipt_evidence
    readiness = classify_required_receipts(contract, run_dir, ctx, checkout=str(project))
    assert readiness["lint"].status == ("present" if stage9_exit == 0 else "failed")

    (run_dir / "meta.json").write_text(
        json.dumps({"task": "t", "status": "done", "project": str(project)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHO_RUNSPACE", str(workspace))
    from sdk.verification_timeline import ReceiptEvidence, get_verification_timeline

    projection = get_verification_timeline(
        run_id=run_dir.name, workspace=workspace,
    )
    projected_row = next(
        row for row in projection.rows if row.command == entry.command
        and row.hook == entry.hook and row.phase == entry.phase
    )
    assert projected_row.disposition == disposition
    assert projected_row.receipt_evidence == ReceiptEvidence(
        path=executions[-1].receipt_evidence, rerun=True,
    )
    assert projection.events[-1].receipt_evidence == ReceiptEvidence(
        path=executions[-1].receipt_evidence, rerun=True,
    )


def test_persisted_evidence_append_only_and_session_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir).install(monkeypatch)
    run = _stub_run(project, run_dir, contract, ctx)

    result = auto_run_required_receipts(
        run, "final_acceptance", reason="pre-final_acceptance",
    )

    assert result.attempted is True
    trail = run.state.extras["verification_autorun"]
    assert isinstance(trail, list) and len(trail) == 1
    entry = trail[0]
    # The durable trail entry is the fixed to_evidence() shape enriched
    # additively with phase/source (T1).
    assert set(entry) == {
        "attempted", "reason", "ran_envs", "ran_commands", "skipped_manual",
        "skipped_fresh", "failed", "errors", "receipt_paths", "phase", "source",
    }
    assert entry["attempted"] is True
    assert entry["phase"] == "final_acceptance"
    assert entry["source"] == "stage9_autorun"
    # session mirror under phase_log[phase] is the SAME entry.
    mirror = run.session["phase_log"]["final_acceptance"]["verification_autorun"]
    assert mirror == entry

    # A second final phase appends (append-only) and mirrors under its own key.
    auto_run_required_receipts(run, "compliance_check", reason="pre-compliance")
    assert len(run.state.extras["verification_autorun"]) == 2
    assert (
        "verification_autorun"
        in run.session["phase_log"]["compliance_check"]
    )


def test_dry_run_adapter_records_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)
    run = _stub_run(project, run_dir, contract, ctx)
    run.state.dry_run = True

    result = auto_run_required_receipts(
        run, "final_acceptance", reason="pre-final_acceptance",
    )

    assert result.attempted is False
    assert "verification_autorun" not in run.state.extras
    assert "phase_log" not in run.session
    assert rec.env_calls == [] and rec.run_calls == []


# ── case 10: required+manual in gate_rerun (F2) ───────────────────────────


def test_gate_rerun_manual_required_blocks_required_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.project.correction_gate_rerun import execute_gate_rerun_receipts

    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint", "manual_cmd"], manual=("manual_cmd",))
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)
    run = _stub_run(project, run_dir, contract, ctx)

    execution, _result = execute_gate_rerun_receipts(run)

    # F2: a withheld manual required command must NOT yield required_passed.
    assert execution.attempted is True
    assert execution.required_passed is not True
    # The manual command was withheld (skipped_manual), only lint ran.
    assert rec.run_calls[0]["commands"] == ["lint"]


# ── F1: authoritative on-disk state, not optimistic exit codes ────────────


def test_failed_env_receipt_blocks_env_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verify_env that returns ``all_passed=False`` (writes a failed env
    receipt) must surface as ``failed_envs`` and drive ``env_passed`` false —
    even though nothing raised. Regression for the F1 false-green env defect."""
    from pipeline.project.correction_gate_rerun import execute_gate_rerun_receipts

    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir, env_all_passed=False).install(monkeypatch)
    run = _stub_run(project, run_dir, contract, ctx)

    execution, _result = execute_gate_rerun_receipts(run)

    assert execution.attempted is True
    # verify_env returned without raising but did not pass -> not green.
    assert execution.env_passed is False


def test_unwritten_command_receipt_blocks_required_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command that exits 0 but whose receipt never lands on disk stays
    ``missing`` under re-classification, so ``required_passed`` must NOT go green
    despite the optimistic exit code. Regression for the F1 false-green defect."""
    from pipeline.project.correction_gate_rerun import execute_gate_rerun_receipts

    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    # write=False: verify_run reports exit 0 but persists no receipt.
    _Recorder(run_dir, write=False).install(monkeypatch)
    run = _stub_run(project, run_dir, contract, ctx)

    execution, _result = execute_gate_rerun_receipts(run)

    assert execution.attempted is True
    # No failed command and no error, but the receipt is missing on disk.
    assert execution.required_passed is not True


def test_materialize_reports_residual_when_receipt_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared materializer carries the authoritative residual set: an exit-0
    command with no persisted receipt is reported in ``residual_required`` (and
    not in ``failed``), so the gate adapter can refuse a false green."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir, write=False).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), reason="pre-final",
    )

    assert result.ran_commands == ("lint",)
    assert result.failed == ()  # exit 0 -> not a failed receipt
    assert result.residual_required == ("lint",)  # but still missing on disk


# ── T3: path-selected delivery gate via cached before_delivery plan ────────


def _path_selected_contract(
    *, manual_e2e: bool = False, with_lint: bool = True, default_env: str = "ci",
) -> VerificationContract:
    """Contract whose ``cli-sdk-unit`` gate is **path-selected** (``sdk/**``) via a
    ``cli-sdk`` gate set — it is NOT in static ``verification.required``. Blanket
    ``before_delivery`` + ``after_phase(implement)`` schedule entries place every
    selected command at the delivery positions, so when the gate set is selected
    ``cli-sdk-unit`` becomes a required *delivery* command. With ``manual_e2e`` an
    extra required ``e2e`` command is parked behind a ``manual_only`` gate set so
    the materializer must withhold it even though it is required. ``with_lint``
    drops the static required ``lint`` so the resolved delivery-required set is
    empty when the path gate is not selected."""
    commands: dict[str, Any] = {
        "lint": {"run": "true"},
        "cli-sdk-unit": {"run": "true", "env": default_env},
    }
    gate_sets: dict[str, Any] = {"cli-sdk": {"commands": ["cli-sdk-unit"]}}
    required = ["lint"] if with_lint else []
    schedule: list[dict[str, Any]] = [
        {"before_delivery": True, "policy": "warn", "commands": ["lint"] if with_lint else []},
        {"before_delivery": True, "policy": "warn", "gate_sets": ["cli-sdk"]},
        {"after_phase": "implement"},
    ]
    if manual_e2e:
        commands["e2e"] = {"run": "true"}
        gate_sets["manuals"] = {"commands": ["e2e"]}
        required.append("e2e")
        schedule.append({"manual_only": True, "gate_sets": ["manuals"]})
    verification: dict[str, Any] = {
        "default_env": default_env,
        "required": required,
        "commands": commands,
        "gate_sets": gate_sets,
        "selection": [{"paths": ["sdk/**"], "include": ["cli-sdk"]}],
        "schedule": schedule,
    }
    plugin = PluginConfig(
        verification_envs={default_env: {}}, verification=verification,
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


def _prefinal_path_contract() -> VerificationContract:
    """CLI path gate plus operator-owned delivery identities."""
    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {
            "cli-sdk-unit": {"run": "true"},
            "manual": {"run": "true"},
            "suggest": {"run": "true"},
        },
        "required": ["manual", "suggest"],
        "gate_sets": {"cli": {"commands": ["cli-sdk-unit"]}},
        "selection": [{"paths": ["tests/unit/cli/**"], "include": ["cli"]}],
        "schedule": [
            {"after_phase": "implement", "gate_sets": ["cli"], "policy": "require"},
            {"before_delivery": True, "gate_sets": ["cli"], "policy": "require"},
            {"before_delivery": True, "commands": ["manual"], "policy": "manual"},
            {"before_delivery": True, "commands": ["suggest"], "policy": "suggest"},
        ],
    }))
    assert contract is not None
    return contract


def _cached_before_delivery_extras(
    contract: VerificationContract, touched: list[str],
) -> dict[str, Any]:
    """Build the executable ``before_delivery`` routing plan from ``touched`` paths
    and cache it under :data:`ROUTING_PLANS_EXTRAS_KEY` keyed by the
    ``"before_delivery:"`` epoch — exactly the cache ``gate_repair`` writes and
    readiness/delivery read."""
    from pipeline.verification_selection import (
        build_scheduled_gate_plan,
        selection_context_from_extras,
    )

    plan = build_scheduled_gate_plan(
        contract,
        selection_context_from_extras(
            {}, contract, touched_paths=tuple(touched),
        ),
    )
    return {ROUTING_PLANS_EXTRAS_KEY: {"before_delivery:": plan}}


def test_prefinal_epoch_precedes_verify_and_reuses_exact_path_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair-time CLI paths are frozen before materialization, not afterwards."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _prefinal_path_contract()
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    run = _stub_run(project, run_dir, contract, ctx)
    run.state.output_dir = run_dir
    # The implement epoch sees no CLI change; repair makes it visible only at
    # the final boundary. An advisory preview must not influence that boundary.
    paths = {"value": ()}
    monkeypatch.setattr("core.io.git_helpers.git_changed_files", lambda _cwd: paths["value"])
    from pipeline.project.verification_ledger_runtime import select_epoch
    from pipeline.verification_selection import SelectionContext

    implement_plan = select_epoch(
        run, contract, epoch="after_phase:implement", context=SelectionContext(),
    )
    assert "cli-sdk-unit" not in [entry.command for entry in implement_plan.entries]
    run.state.extras["verification_gate_prompt_preview"] = object()
    paths["value"] = ("tests/unit/cli/test_gate.py",)
    plan = select_before_delivery_epoch(run)
    assert [entry.command for entry in plan.entries if entry.hook == "before_delivery"] == [
        "cli-sdk-unit", "manual", "suggest",
    ]

    recorder = _Recorder(run_dir).install(monkeypatch)
    original_verify_run = recorder.verify_run

    def verify_after_selection(**kwargs: Any) -> Any:
        ledger = load_ledger(run_dir)
        assert any(
            event.kind == "selection" and event.reason == "before_delivery:"
            for event in ledger.trail
        )
        return original_verify_run(**kwargs)

    monkeypatch.setattr("sdk.verify.verify_run", verify_after_selection)
    monkeypatch.setattr(
        "pipeline.verification_selection.build_scheduled_gate_plan",
        lambda *_: pytest.fail("materializer rebuilt the selected delivery plan"),
    )
    result = auto_run_required_receipts(
        run, "final_acceptance", reason="pre-final", delivery_plan=plan,
    )

    assert result.ran_commands == ("cli-sdk-unit",)
    assert recorder.run_calls[0]["commands"] == ["cli-sdk-unit"]
    assert result.skipped_manual == ("manual", "suggest")
    from pipeline.verification_readiness import build_final_acceptance_readiness

    readiness = build_final_acceptance_readiness(
        contract, run_dir, ctx, extras=run.state.extras,
    )
    assert "cli-sdk-unit" not in readiness.required_missing
    assert "cli-sdk-unit" not in readiness.required_failed

    from pipeline.project import gate_repair

    monkeypatch.setattr(
        gate_repair, "_run_gate_command",
        lambda *_args, **_kwargs: pytest.fail("before_delivery reran materialized command"),
    )
    outcome = gate_repair.run_gate_hook(run, object(), object(), hook="before_delivery")
    assert outcome == gate_repair.GateRepairOutcome(active=True, passed=True)
    assert len(recorder.run_calls) == 1
    assert len([
        event for event in load_ledger(run_dir).trail
        if event.kind == "selection" and event.reason == "before_delivery:"
    ]) == 3


def test_resume_materializes_recorded_phase_delivery_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed pre-final pass retains phase-only durable delivery scope."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _prefinal_path_contract()
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    first = _stub_run(project, run_dir, contract, ctx)
    first.state.output_dir = run_dir
    from pipeline.project.verification_ledger_runtime import select_epoch
    from pipeline.verification_selection import SelectionContext

    select_epoch(
        first, contract, epoch="after_phase:implement",
        context=SelectionContext(touched_paths=("tests/unit/cli/test_orcho.py",)),
    )
    select_epoch(first, contract, epoch="before_delivery:", context=SelectionContext())

    resumed = _stub_run(project, run_dir, contract, ctx)
    resumed.state.output_dir = run_dir
    resumed.checkpoint_resume = True
    monkeypatch.setattr(
        "pipeline.project.verification_ledger_runtime.build_scheduled_gate_plan",
        lambda *_: pytest.fail("resume rebuilt a recorded delivery plan"),
    )
    plan = select_before_delivery_epoch(resumed)
    assert [(entry.command, entry.hook, entry.phase) for entry in plan.entries] == [
        ("cli-sdk-unit", "after_phase", "implement"),
        ("manual", "before_delivery", ""),
        ("suggest", "before_delivery", ""),
    ]

    recorder = _Recorder(run_dir).install(monkeypatch)
    result = auto_run_required_receipts(
        resumed, "final_acceptance", reason="pre-final", delivery_plan=plan,
    )

    assert result.ran_commands == ("cli-sdk-unit",)
    assert recorder.run_calls[0]["commands"] == ["cli-sdk-unit"]
    assert any(
        event.identity == ("cli-sdk-unit", "after_phase", "implement")
        and event.kind == "execution"
        for event in load_ledger(run_dir).trail
    )


def test_correction_autorun_does_not_create_delivery_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _prefinal_path_contract()
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    run = _stub_run(project, run_dir, contract, ctx)
    run.state.output_dir = run_dir
    _Recorder(run_dir).install(monkeypatch)

    auto_run_required_receipts(
        run, "review_changes", reason="pre-review correction required-receipt materialization",
    )

    assert not run_dir.joinpath("scheduled_gate_ledger.json").exists()


def test_cached_delivery_plan_targets_path_selected_gate_clean_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KEY regression: with the cached ``before_delivery`` plan threaded through
    ``extras`` and a CLEAN live checkout, the materializer targets the
    path-selected ``cli-sdk-unit``; with a stripped ``extras`` (the old behaviour,
    a fresh plan rebuilt from the clean worktree) it does not."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _path_selected_contract()
    # Live checkout is clean — a FRESH plan would select no path-gated set.
    monkeypatch.setattr("core.io.git_helpers.git_changed_files", lambda cwd: [])
    extras = _cached_before_delivery_extras(contract, ["sdk/foo.py"])

    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)
    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), extras=extras, reason="pre-final",
    )
    # The cached before_delivery plan pulls the path-selected gate into targets.
    assert "cli-sdk-unit" in result.ran_commands
    assert "cli-sdk-unit" in rec.run_calls[0]["commands"]

    # Stripped extras (old behaviour): the fresh plan over the CLEAN checkout sees
    # no path gate, so cli-sdk-unit is NOT targeted — only static required lint.
    run_dir2 = workspace / "runspace" / "runs" / "rid2"
    run_dir2.mkdir(parents=True)
    ctx2 = _ctx(contract, checkout=project, project=project,
                workspace=workspace, run_dir=run_dir2)
    rec2 = _Recorder(run_dir2).install(monkeypatch)
    stripped = materialize_required_receipts(
        run_id="rid2", run_dir=run_dir2, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx2,
        workspace=str(workspace), extras=None, reason="pre-final",
    )
    assert stripped.ran_commands == ("lint",)
    assert "cli-sdk-unit" not in stripped.ran_commands
    assert rec2.run_calls[0]["commands"] == ["lint"]


def test_materialized_path_selected_gate_consistent_readiness_and_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the auto-run, classification (readiness) and
    ``assess_delivery_verification`` (delivery) agree on the path-selected gate:
    both see it ``present`` — never one ``missing`` while the other clears."""
    from pipeline.verification_delivery import assess_delivery_verification

    project, workspace, run_dir = _layout(tmp_path)
    contract = _path_selected_contract()
    monkeypatch.setattr("core.io.git_helpers.git_changed_files", lambda cwd: [])
    extras = _cached_before_delivery_extras(contract, ["sdk/foo.py"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir).install(monkeypatch)

    # Before: delivery (same extras) sees the path-selected gate missing.
    before = assess_delivery_verification(
        contract, run_dir, ctx, extras=extras, diff_cwd=project,
    )
    assert before is not None
    assert "cli-sdk-unit" in before.required_missing

    materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), extras=extras, reason="pre-final",
    )

    after_cls = classify_required_receipts(
        contract, run_dir, ctx, checkout=str(project), extras=extras,
    )
    assert after_cls["cli-sdk-unit"].status == "present"
    after_del = assess_delivery_verification(
        contract, run_dir, ctx, extras=extras, diff_cwd=project,
    )
    assert after_del is not None
    assert "cli-sdk-unit" not in after_del.required_missing
    assert after_del.required_missing == ()


def test_failed_path_selected_gate_is_failed_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path-selected gate the auto-run executes with exit!=0 materializes a
    FAILED receipt: delivery reports it ``failed`` (red), never ``missing`` — the
    failure is not masked into a gap."""
    from pipeline.verification_delivery import assess_delivery_verification

    project, workspace, run_dir = _layout(tmp_path)
    contract = _path_selected_contract()
    monkeypatch.setattr("core.io.git_helpers.git_changed_files", lambda cwd: [])
    extras = _cached_before_delivery_extras(contract, ["sdk/foo.py"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir, exit_codes={"cli-sdk-unit": 1}).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), extras=extras, reason="pre-final",
    )
    assert "cli-sdk-unit" in result.failed
    after_del = assess_delivery_verification(
        contract, run_dir, ctx, extras=extras, diff_cwd=project,
    )
    assert after_del is not None
    assert "cli-sdk-unit" not in after_del.required_missing
    assert "cli-sdk-unit" in after_del.required_failed


def test_manual_e2e_withheld_even_with_path_selected_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required ``manual_only`` ``e2e`` command stays ``skipped_manual`` (never
    handed to verify_run) even when the delivery-selected set also pulls in the
    path-selected ``cli-sdk-unit`` — manual exclusion survives full-extras
    targeting (acceptance #5)."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _path_selected_contract(manual_e2e=True)
    monkeypatch.setattr("core.io.git_helpers.git_changed_files", lambda cwd: [])
    extras = _cached_before_delivery_extras(contract, ["sdk/foo.py"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), extras=extras, reason="pre-final",
    )
    assert result.skipped_manual == ("e2e",)
    assert "cli-sdk-unit" in result.ran_commands
    assert "e2e" not in rec.run_calls[0]["commands"]


def test_empty_resolved_delivery_set_is_noop_despite_path_gate_unselected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the contract has NO static required and the path gate is not selected
    (clean checkout, no cached plan), the resolved delivery-required set is empty
    -> strict no-op (``attempted=False``), sdk.verify untouched."""
    project, workspace, run_dir = _layout(tmp_path)
    # No static required; cli-sdk-unit only reachable via path selection.
    contract = _path_selected_contract(with_lint=False)
    monkeypatch.setattr("core.io.git_helpers.git_changed_files", lambda cwd: [])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    rec = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project),
        checkout=str(project), contract=contract, ctx=ctx,
        workspace=str(workspace), extras=None, reason="pre-final",
    )
    assert result.attempted is False
    assert rec.env_calls == [] and rec.run_calls == []


# ── verification_timeline: live blocks ────────────────────────────────────


def test_live_block_happy_path_envs_and_commands_pass() -> None:
    result = ReceiptAutoRunResult(
        attempted=True,
        reason="pre-final",
        ran_envs=("core-local",),
        ran_commands=("env-provenance", "lint", "cli-sdk-unit"),
    )
    lines = render_gate_live_block(result, hook_label="pre-final auto-run")
    assert lines[0] == "Verification gates — pre-final auto-run"
    assert "core-local PASS" in lines[1]
    body = "\n".join(lines)
    assert "env-provenance PASS" in body
    assert "lint PASS" in body
    assert "cli-sdk-unit PASS" in body
    assert "FAIL" not in body


def test_print_gate_live_block_mirrors_plain_output_log(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from agents.stream import set_agent_log
    from core.observability import logging as _logging
    from pipeline.project.run import _print_gate_live_block

    # This test pins the full multi-line live block on stdout; ``summary``
    # (the default run-output mode) collapses it to a one-line presenter
    # card. Force ``live`` so the framed block renders; the output.log
    # mirror asserted below is mode-independent either way.
    _mode_before = _logging.get_output_mode()
    _logging._output_mode = "live"
    log_path = tmp_path / "output.log"
    set_agent_log(log_path)
    try:
        _print_gate_live_block((
            "Verification gates -- pre-final auto-run",
            "commands: env-provenance FRESH · lint FRESH",
        ))
    finally:
        set_agent_log(None)
        _logging._output_mode = _mode_before

    terminal = capsys.readouterr().out
    assert "Official verification gates" in terminal
    assert "Verification gates -- pre-final auto-run" in terminal
    assert "env-provenance" in terminal

    log_text = log_path.read_text(encoding="utf-8")
    assert "Verification gates -- pre-final auto-run" in log_text
    assert "commands: env-provenance FRESH · lint FRESH" in log_text
    assert "Official verification gates" not in log_text
    assert "\x1b[" not in log_text


def test_live_block_skipped_manual_is_not_pass() -> None:
    result = ReceiptAutoRunResult(
        attempted=True, reason="pre-final", skipped_manual=("e2e",),
    )
    lines = render_gate_live_block(result, hook_label="pre-final auto-run")
    body = "\n".join(lines)
    assert "e2e SKIPPED MANUAL" in body
    assert "e2e PASS" not in body


def test_live_block_failed_and_residual_priority() -> None:
    result = ReceiptAutoRunResult(
        attempted=True,
        reason="pre-final",
        ran_commands=("lint", "cli-sdk-unit"),
        failed=("lint",),
        residual_required=("cli-sdk-unit",),
    )
    body = "\n".join(render_gate_live_block(result, hook_label="correction gate rerun"))
    assert "lint FAIL" in body
    assert "cli-sdk-unit MISSING/STALE" in body
    # Priority kept fail/residual out of the PASS bucket.
    assert "lint PASS" not in body
    assert "cli-sdk-unit PASS" not in body


def test_live_block_errors_line_is_compact() -> None:
    result = ReceiptAutoRunResult(
        attempted=True, reason="x", ran_commands=("lint",),
        errors=("verify_run RuntimeError: boom", "verify_env OSError: nope"),
    )
    body = "\n".join(render_gate_live_block(result, hook_label="pre-delivery"))
    assert "errors: 2" in body
    assert "boom" not in body  # no dump


def test_live_block_not_attempted_is_empty() -> None:
    result = ReceiptAutoRunResult(attempted=False, reason="dry_run")
    assert render_gate_live_block(result, hook_label="pre-final auto-run") == ()


# ── summary gate line: glyph reflects all unsuccessful states ─────────────


def test_gate_summary_line_all_pass_is_ok() -> None:
    from core.io.ansi import strip_ansi
    from pipeline.project.run import _gate_summary_line

    line = strip_ansi(_gate_summary_line((
        "Verification gates -- after_phase(implement)",
        "commands: lint PASS · unit PASS",
    )))
    assert line.startswith("✓ gates")
    assert "lint PASS" in line


def test_gate_summary_line_missing_stale_is_failure() -> None:
    from core.io.ansi import strip_ansi
    from pipeline.project.run import _gate_summary_line

    # A residual required receipt (MISSING/STALE) is blocking: it must not
    # render as a success ``✓``. Regression guard for F1.
    line = strip_ansi(_gate_summary_line((
        "Verification gates -- after_phase(implement)",
        "commands: lint MISSING/STALE · unit PASS",
    )))
    assert line.startswith("✗ gates")
    assert "lint MISSING/STALE" in line


def test_gate_summary_line_executor_errors_is_failure() -> None:
    from core.io.ansi import strip_ansi
    from pipeline.project.run import _gate_summary_line

    # An ``errors: N`` body line means the pass captured executor errors; the
    # summary glyph must be ``✗`` even when every gate token reads PASS.
    line = strip_ansi(_gate_summary_line((
        "Verification gates -- after_phase(implement)",
        "commands: lint PASS",
        "errors: 2",
    )))
    assert line.startswith("✗ gates")


def test_gate_summary_line_skipped_manual_and_fresh_stay_ok() -> None:
    from core.io.ansi import strip_ansi
    from pipeline.project.run import _gate_summary_line

    # SKIPPED MANUAL and FRESH are not failures; a block with only those and
    # PASS tokens keeps the success glyph.
    line = strip_ansi(_gate_summary_line((
        "Verification gates -- before_delivery",
        "commands: lint PASS · unit FRESH · e2e SKIPPED MANUAL",
    )))
    assert line.startswith("✓ gates")


# ── verification_timeline: DONE render (T2) ───────────────────────────────


def test_done_render_per_event_lines_receipts_and_residual() -> None:
    timeline = VerificationTimeline(
        events=(
            VerificationGateEvent(
                hook_label="after_phase(implement)", source="scheduled",
                ran_pass=("lint", "unit"), ran_fail=("smoke",),
            ),
            VerificationGateEvent(
                hook_label="before_delivery", source="scheduled",
                ran_pass=("lint",), skipped_fresh=("unit", "smoke"),
            ),
            VerificationGateEvent(
                hook_label="pre-final auto-run", source="stage9_autorun",
                skipped_fresh=("lint",),
            ),
        ),
        residual_missing=("e2e",),
        residual_stale=("smoke",),
        receipts=("lint", "smoke", "unit"),
    )
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "Verification gates:" in block
    assert "events: 3 official gate events" in block
    assert "after_phase(implement): 2 ran/pass, 1 ran/fail" in block
    # Fresh-without-run renders as 'skipped fresh', never inferred ran/pass.
    assert "before_delivery: 1 ran/pass, 2 skipped fresh" in block
    assert "pre-final auto-run: 1 skipped fresh" in block
    assert "receipts: lint, smoke, unit" in block
    assert "residual: missing=e2e stale=smoke" in block


def test_done_render_skipped_manual_bucket() -> None:
    timeline = VerificationTimeline(
        events=(
            VerificationGateEvent(
                hook_label="pre-final auto-run", source="stage9_autorun",
                skipped_manual=("e2e",),
            ),
        ),
    )
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "pre-final auto-run: 1 skipped manual" in block


def test_done_render_omitted_for_empty_or_none() -> None:
    assert render_verification_gate_done_block(None) == ()
    assert render_verification_gate_done_block(VerificationTimeline()) == ()


def test_done_render_from_built_timeline_end_to_end(tmp_path: Path) -> None:
    """The render consumes exactly what ``build_verification_timeline`` produces
    from durable evidence (the scheduled gate-event trail here)."""
    from pipeline.project.gate_repair import VERIFICATION_GATE_EVENTS_KEY

    _, _, run_dir = _layout(tmp_path)
    extras = {
        VERIFICATION_GATE_EVENTS_KEY: [
            {"hook": "after_phase", "phase": "implement", "command": "lint",
             "gate_set": "core", "decision": "executed_pass"},
            {"hook": "after_phase", "phase": "implement", "command": "unit",
             "gate_set": "core", "decision": "executed_pass"},
        ],
    }
    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "after_phase(implement): 2 ran/pass" in block


def test_done_render_omitted_without_contract_or_evidence(tmp_path: Path) -> None:
    _, _, run_dir = _layout(tmp_path)
    assert build_verification_timeline(
        run_dir=run_dir, extras=None, session=None,
    ) is None
    assert build_verification_timeline(
        run_dir=run_dir, extras={}, session={},
    ) is None


# ── F1: Stage 9 auto-run live wiring in _on_phase_start ────────────────────


def _phase_start_run(presentation: Any, *, profile_name: str = "advanced") -> Any:
    """Minimal ``_PipelineRun`` stand-in for driving ``_on_phase_start``.

    Only the attributes the FINAL_PHASES branch touches are present; the
    correction-route lookup sees no triage record (strict no-op) and the
    pre-phase gate is monkeypatched to a no-op by the caller."""
    state = SimpleNamespace(phase_log={}, extras={})
    return (
        SimpleNamespace(
            _presentation=presentation,
            profile_name=profile_name,
            state=state,
        ),
        state,
    )


def test_stage9_live_block_printed_in_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from core.io.ansi import strip_ansi
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    monkeypatch.setattr(
        "pipeline.project.gate_repair.evaluate_pre_phase_gates",
        lambda *a, **k: None,
    )
    # Pin the full live block: ``summary`` (the default) collapses it to a
    # one-line presenter card. monkeypatch auto-restores the mode.
    monkeypatch.setattr("core.observability.logging._output_mode", "live")
    result = ReceiptAutoRunResult(
        attempted=True,
        reason="pre-final",
        ran_envs=("core-local",),
        ran_commands=("env-provenance", "lint", "cli-sdk-unit"),
    )
    monkeypatch.setattr(
        "pipeline.project.verification_autorun.auto_run_required_receipts",
        lambda *a, **k: result,
    )
    run, state = _phase_start_run(PresentationPolicy.TERMINAL)

    _PipelineRun._on_phase_pre(run, "final_acceptance", state)

    out = strip_ansi(capsys.readouterr().out)
    assert "Verification gates — pre-final auto-run" in out
    assert "core-local PASS" in out
    assert "env-provenance" in out and "lint" in out and "cli-sdk-unit" in out


def test_stage9_live_block_omitted_in_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    monkeypatch.setattr(
        "pipeline.project.gate_repair.evaluate_pre_phase_gates",
        lambda *a, **k: None,
    )
    result = ReceiptAutoRunResult(
        attempted=True, reason="pre-final", ran_commands=("lint",),
    )
    monkeypatch.setattr(
        "pipeline.project.verification_autorun.auto_run_required_receipts",
        lambda *a, **k: result,
    )
    run, state = _phase_start_run(PresentationPolicy.SILENT)

    _PipelineRun._on_phase_pre(run, "final_acceptance", state)

    assert capsys.readouterr().out == ""


def test_stage9_live_block_omitted_when_not_attempted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    monkeypatch.setattr(
        "pipeline.project.gate_repair.evaluate_pre_phase_gates",
        lambda *a, **k: None,
    )
    result = ReceiptAutoRunResult(attempted=False, reason="no contract")
    monkeypatch.setattr(
        "pipeline.project.verification_autorun.auto_run_required_receipts",
        lambda *a, **k: result,
    )
    run, state = _phase_start_run(PresentationPolicy.TERMINAL)

    _PipelineRun._on_phase_pre(run, "final_acceptance", state)

    assert "Verification gates" not in capsys.readouterr().out


def test_correction_review_materializes_receipts_before_review(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from core.io.ansi import strip_ansi
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    monkeypatch.setattr(
        "pipeline.project.gate_repair.evaluate_pre_phase_gates",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "pipeline.project.verification_autorun.select_before_delivery_epoch",
        lambda *_args: pytest.fail("correction pre-review froze delivery epoch"),
    )
    # Pin the full live block: ``summary`` (the default) collapses it to a
    # one-line presenter card. monkeypatch auto-restores the mode.
    monkeypatch.setattr("core.observability.logging._output_mode", "live")
    calls: list[dict[str, Any]] = []
    result = ReceiptAutoRunResult(
        attempted=True,
        reason="pre-review",
        ran_envs=("core-local",),
        ran_commands=("pytest-all",),
    )

    def fake_autorun(run: Any, phase: str, *, reason: str) -> ReceiptAutoRunResult:
        calls.append({"phase": phase, "reason": reason})
        return result

    monkeypatch.setattr(
        "pipeline.project.verification_autorun.auto_run_required_receipts",
        fake_autorun,
    )
    run, state = _phase_start_run(
        PresentationPolicy.TERMINAL, profile_name="correction",
    )

    _PipelineRun._on_phase_pre(run, "review_changes", state)

    assert calls == [
        {
            "phase": "review_changes",
            "reason": "pre-review correction required-receipt materialization",
        },
    ]
    out = strip_ansi(capsys.readouterr().out)
    assert "Verification gates — correction pre-review auto-run" in out
    assert "core-local PASS" in out
    assert "pytest-all" in out


def test_non_correction_review_does_not_materialize_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    monkeypatch.setattr(
        "pipeline.project.gate_repair.evaluate_pre_phase_gates",
        lambda *a, **k: None,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "pipeline.project.verification_autorun.auto_run_required_receipts",
        lambda *a, **k: calls.append("called"),
    )
    run, state = _phase_start_run(
        PresentationPolicy.TERMINAL, profile_name="advanced",
    )

    _PipelineRun._on_phase_pre(run, "review_changes", state)

    assert calls == []


# ── T1: durable autorun identity (phase/source) + to_evidence compat ───────


def test_to_evidence_keys_are_fixed_compat() -> None:
    """ADR 0094: ``ReceiptAutoRunResult.to_evidence`` keys are frozen — the T1
    phase/source enrichment lives only on the recorded trail entry, never here."""
    result = ReceiptAutoRunResult(attempted=True, reason="x")
    assert set(result.to_evidence()) == {
        "attempted", "reason", "ran_envs", "ran_commands", "skipped_manual",
        "skipped_fresh", "failed", "errors", "receipt_paths",
    }


def test_autorun_source_correction_pre_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir).install(monkeypatch)
    run = _stub_run(project, run_dir, contract, ctx)

    auto_run_required_receipts(
        run, "review_changes",
        reason="pre-review correction required-receipt materialization",
    )
    entry = run.state.extras["verification_autorun"][-1]
    assert entry["phase"] == "review_changes"
    assert entry["source"] == "correction_pre_review"


def test_autorun_source_gate_rerun_same_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.project.correction_gate_rerun import execute_gate_rerun_receipts

    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    _Recorder(run_dir).install(monkeypatch)
    run = _stub_run(project, run_dir, contract, ctx)

    execute_gate_rerun_receipts(run)
    # gate_rerun writes through the SAME autorun sink, tagged source=gate_rerun.
    entry = run.state.extras["verification_autorun"][-1]
    assert entry["phase"] == "correction_triage"
    assert entry["source"] == "gate_rerun"


# ── T1: gate_repair scheduled-gate recorder ────────────────────────────────


def _routing_contract(
    schedule: list[dict[str, Any]],
    *,
    required: tuple[str, ...] = ("lint",),
    manual: tuple[str, ...] = (),
    work_mode: str = "governed",
) -> VerificationContract:
    """A contract wired for executable routing: an always-selected ``core`` gate
    set over ``required``, plus the given ``schedule``. ``manual`` names are also
    parked behind a ``manual_only`` entry."""
    commands: dict[str, Any] = {name: {"run": "true"} for name in required}
    gate_sets: dict[str, Any] = {"core": {"commands": list(required)}}
    full_schedule = list(schedule)
    if manual:
        gate_sets["manuals"] = {"commands": list(manual)}
        full_schedule.append({"manual_only": True, "gate_sets": ["manuals"]})
    verification: dict[str, Any] = {
        "default_env": "ci",
        "commands": commands,
        "required": list(required),
        "gate_sets": gate_sets,
        "selection": [{"always": ["core"]}],
        "schedule": full_schedule,
    }
    plugin = PluginConfig(
        work_mode=work_mode, verification_envs={"ci": {}}, verification=verification,
    )
    contract = VerificationContract.from_plugin(plugin)
    assert contract is not None
    return contract


class _GateState:
    def __init__(self, contract: Any, ctx: Any, output_dir: Path) -> None:
        self.extras: dict[str, Any] = {
            "verification_contract": contract,
            "verification_placeholders": ctx,
        }
        self.output_dir = output_dir
        self.last_critique = ""
        self.last_test_output = ""
        self.halt = False
        self.halt_reason = ""
        self.phase_handoff_request = None

    def stop(self, reason: str) -> None:
        self.halt = True
        self.halt_reason = reason


def _gate_run(contract: Any, ctx: Any, output_dir: Path) -> Any:
    return SimpleNamespace(
        state=_GateState(contract, ctx, output_dir), session={}, max_rounds=1,
        _on_phase_start=None, _on_phase_end=None,
    )


def _ledger_events(run: Any) -> tuple[Any, ...]:
    """The scheduled-gate trail is durable; state.extras is not its cache."""
    return load_ledger(run.state.output_dir).trail


def _execution_events(run: Any) -> tuple[Any, ...]:
    return tuple(event for event in _ledger_events(run) if event.kind != "selection")


def _gate_receipt(run: Any, exit_code: int) -> dict[str, Any]:
    """Minimal fresh schema-v3 receipt returned by mocked gate execution."""
    from pipeline.verification_subject import (
        VerificationSubjectAvailable,
        capture_verification_subject,
    )

    ctx = run.state.extras["verification_placeholders"]
    captured = capture_verification_subject(Path(ctx.checkout))
    assert isinstance(captured, VerificationSubjectAvailable)
    identity = captured.identity
    return {
        "exit_code": exit_code,
        "subject": {"status": "available", "identity": {
            "version": identity.version,
            "object_format": identity.object_format,
            "tree_oid": identity.tree_oid,
            "observed_head_oid": identity.observed_head_oid,
            "baseline_oid": identity.baseline_oid,
        }},
    }


def test_recorder_executed_pass_without_changing_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.project import gate_repair

    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract([{"after_phase": "implement", "commands": ["lint"]}])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)
    monkeypatch.setattr(gate_repair, "_run_gate_command", lambda run, *a, **k: _gate_receipt(run, 0))

    outcome = gate_repair.run_gate_hook(
        run, object(), object(), hook="after_phase", phase="implement",
    )

    # GateRepairOutcome is byte-identical to the no-recorder routing result.
    assert outcome == gate_repair.GateRepairOutcome(active=True, passed=True)
    events = _execution_events(run)
    assert [(e.kind, e.outcome) for e in events] == [("execution", "pass")]
    assert events[0].identity == ("lint", "after_phase", "implement")
    assert gate_repair.VERIFICATION_GATE_EVENTS_KEY not in run.state.extras


def test_recorder_executed_fail_non_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline.project import gate_repair

    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract([
        {"after_phase": "implement", "policy": "require", "commands": ["lint"], "action": "continue_warn"},
    ])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)
    monkeypatch.setattr(gate_repair, "_run_gate_command", lambda run, *a, **k: _gate_receipt(run, 1))

    outcome = gate_repair.run_gate_hook(
        run, object(), object(), hook="after_phase", phase="implement",
    )

    # continue_warn keeps the run going; the recorder still proves the execution.
    assert outcome == gate_repair.GateRepairOutcome(active=True, passed=True)
    events = _execution_events(run)
    assert [(e.kind, e.outcome) for e in events] == [("execution", "fail")]


def test_phase_warn_executes_once_and_continues_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warn is engine-owned: one failed execution is a warning, never a halt."""
    from pipeline.project import gate_repair

    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract(
        [{"after_phase": "implement", "policy": "warn", "commands": ["lint"]}],
        work_mode="pro",
    )
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)
    calls = 0

    def _failed(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return _gate_receipt(_args[0], 1)

    monkeypatch.setattr(gate_repair, "_run_gate_command", _failed)
    outcome = gate_repair.run_gate_hook(
        run, object(), object(), hook="after_phase", phase="implement",
    )

    assert outcome == gate_repair.GateRepairOutcome(active=True, passed=True)
    assert calls == 1
    assert run.state.phase_handoff_request is None and run.state.halt is False
    assert _execution_events(run)[0].outcome == "fail"


def test_before_delivery_reuses_prefinal_receipt_without_late_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh pre-final receipt is reconciled, not claimed as hook execution."""
    from pipeline.project import gate_repair

    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract([
        {"before_delivery": True, "policy": "require", "commands": ["lint"]},
    ])
    _write_receipt(run_dir, "lint", exit_code=0)
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)
    monkeypatch.setattr(
        gate_repair, "_run_gate_command",
        lambda *_args, **_kwargs: pytest.fail("fresh receipt must not rerun the command"),
    )

    outcome = gate_repair.run_gate_hook(run, object(), object(), hook="before_delivery")

    assert outcome == gate_repair.GateRepairOutcome(active=True, passed=True)
    events = _execution_events(run)
    assert [(event.identity, event.kind, event.outcome) for event in events] == [
        (("lint", "before_delivery", ""), "reuse", "fresh"),
    ]


def test_before_delivery_routes_failed_prefinal_warn_without_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pre-final warn is reused for its warning consequence once."""
    from pipeline.project import gate_repair

    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract([
        {"before_delivery": True, "policy": "warn", "commands": ["lint"]},
    ], work_mode="pro")
    _write_receipt(run_dir, "lint", exit_code=1)
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)
    monkeypatch.setattr(
        gate_repair,
        "_run_gate_command",
        lambda *_args, **_kwargs: pytest.fail("failed pre-final receipt must not rerun"),
    )

    outcome = gate_repair.run_gate_hook(run, object(), object(), hook="before_delivery")

    assert outcome == gate_repair.GateRepairOutcome(active=True, passed=True)
    assert run.state.extras.get(gate_repair.VERIFICATION_GATE_EVENTS_KEY, []) == []


def test_materializer_targets_only_engine_owned_before_delivery_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual/suggest do not reach verify_env or verify_run; warn/require do."""
    from pipeline.verification_selection import SelectionContext, build_scheduled_gate_plan

    project, workspace, run_dir = _layout(tmp_path)
    names = ("manual", "suggest", "warn", "require")
    contract = _routing_contract(
        [
            {"before_delivery": True, "policy": policy, "commands": [policy]}
            for policy in names
        ],
        required=names,
        work_mode="pro",
    )
    plan = build_scheduled_gate_plan(contract, SelectionContext(work_mode="pro"))
    extras = {ROUTING_PLANS_EXTRAS_KEY: {"before_delivery:": plan}}
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    recorder = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project), checkout=str(project),
        contract=contract, ctx=ctx, workspace=str(workspace), extras=extras, reason="pre-final",
    )

    assert result.skipped_manual == ("manual", "suggest")
    assert result.ran_commands == ("warn", "require")
    assert recorder.run_calls[0]["commands"] == ["warn", "require"]
    assert recorder.env_calls and all(
        set(call) >= {"project", "env", "run_id", "workspace"} for call in recorder.env_calls
    )


def test_materializer_refreshes_after_phase_delivery_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale after_phase(implement) require remains materializable pre-final."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract([
        {
            "after_phase": "implement",
            "policy": "require",
            "commands": ["lint"],
        },
    ])
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    recorder = _Recorder(run_dir).install(monkeypatch)

    result = materialize_required_receipts(
        run_id="rid", run_dir=run_dir, project_dir=str(project), checkout=str(project),
        contract=contract, ctx=ctx, workspace=str(workspace), reason="pre-final",
    )

    assert result.ran_commands == ("lint",)
    assert recorder.run_calls[0]["commands"] == ["lint"]


@pytest.mark.parametrize(
    ("policy", "executions", "paused"),
    (("manual", 0, False), ("suggest", 0, False), ("warn", 1, False), ("require", 1, True)),
)
def test_on_resume_uses_typed_execution_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    policy: str, executions: int, paused: bool,
) -> None:
    from pipeline.project import gate_repair

    project, workspace, run_dir = _layout(tmp_path)
    schedule = {"on_resume": True, "policy": policy, "commands": ["lint"]}
    if policy == "require":
        schedule["action"] = "handoff"
    contract = _routing_contract(
        [schedule],
        work_mode="pro",
    )
    ctx = _ctx(contract, checkout=project, project=project, workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)
    calls = 0

    def _failed(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return _gate_receipt(_args[0], 1)

    monkeypatch.setattr(gate_repair, "_run_gate_command", _failed)
    outcome = gate_repair.run_gate_hook(run, object(), object(), hook="on_resume")

    assert calls == executions
    assert outcome.paused is paused
    assert outcome.passed is (executions == 1 and not paused)


def test_recorder_repair_loop_records_fail_then_pass_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repair_loop recheck is a real execution of the official gate command on
    the hook: a fail -> repair -> pass round must append BOTH executed_fail (the
    initial run) and executed_pass (the recheck that flipped the outcome), and
    the timeline / live block must source both from those decisions — never lose
    the recheck that produced GateRepairOutcome(passed=True)."""
    from pipeline.project import gate_repair

    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract([
        {"after_phase": "implement", "policy": "require", "commands": ["lint"], "action": "repair_loop"},
    ])
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)
    run.max_rounds = 2

    # First execution fails, the post-repair recheck passes.
    codes = iter([1, 0])
    monkeypatch.setattr(
        gate_repair, "_run_gate_command",
        lambda run, *a, **k: _gate_receipt(run, next(codes)),
    )
    # A real repair flow: a repair_changes step exists and the dispatch is a
    # no-op (it only needs to "happen" so the recheck runs).
    monkeypatch.setattr(gate_repair, "_repair_step", lambda profile: object())
    monkeypatch.setattr(
        gate_repair, "_dispatch_repair", lambda *a, **k: None,
    )

    outcome = gate_repair.run_gate_hook(
        run, object(), object(), hook="after_phase", phase="implement",
    )

    # Routing/outcome unchanged: the recheck pass flips it to passed in round 1.
    assert outcome == gate_repair.GateRepairOutcome(
        active=True, passed=True, rounds=1,
    )
    # Both the initial fail AND the recheck pass are in the durable trail.
    events = _execution_events(run)
    assert [(e.kind, e.outcome) for e in events] == [
        ("execution", "fail"), ("execution", "pass"),
    ]
    assert all(e.identity == ("lint", "after_phase", "implement") for e in events)


def test_reconcile_records_skipped_manual_and_fresh(
    tmp_path: Path,
) -> None:
    from pipeline.project import gate_repair
    from pipeline.verification_selection import (
        ScheduledGateEntry,
        ScheduledGatePlan,
    )

    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint", "e2e"], manual=("e2e",))
    _write_receipt(run_dir, "lint", exit_code=0)  # fresh present receipt
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)

    plan = ScheduledGatePlan(entries=(
        ScheduledGateEntry("lint", "before_delivery", "", "suggest",
                           "continue_warn", ("core",), "core"),
        ScheduledGateEntry("e2e", "before_delivery", "", "suggest",
                           "continue_warn", ("manuals",), "manuals"),
    ))

    gate_repair._reconcile_skipped_gate_events(
        run, contract, plan, hook="before_delivery", phase="", executed=set(),
    )

    events = _ledger_events(run)
    # Operator-owned rows close as manual_available at finalization; no synthetic
    # execution/skip event is written for them. The fresh reuse is explicit.
    assert [(event.command, event.kind, event.outcome) for event in events] == [
        ("lint", "reuse", "fresh"),
    ]


def test_reconcile_missing_required_writes_no_hook_skip(
    tmp_path: Path,
) -> None:
    from pipeline.project import gate_repair
    from pipeline.verification_selection import (
        ScheduledGateEntry,
        ScheduledGatePlan,
    )

    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint"])  # no receipt on disk -> missing
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)
    plan = ScheduledGatePlan(entries=(
        ScheduledGateEntry("lint", "before_delivery", "", "suggest",
                           "continue_warn", ("core",), "core"),
    ))

    gate_repair._reconcile_skipped_gate_events(
        run, contract, plan, hook="before_delivery", phase="", executed=set(),
    )

    # An operator-owned suggest identity has no execution/reuse event; terminal
    # closure derives its intentional suggested disposition from the row.
    assert not (run_dir / "scheduled_gate_ledger.json").exists()


def test_reconcile_tolerates_stub_run_without_output_dir() -> None:
    from pipeline.project import gate_repair
    from pipeline.verification_selection import (
        ScheduledGateEntry,
        ScheduledGatePlan,
    )

    run = SimpleNamespace(state=SimpleNamespace(extras={}))
    plan = ScheduledGatePlan(entries=(
        ScheduledGateEntry("lint", "before_delivery", "", "require",
                           "continue_warn", ("core",), "core"),
    ))
    # No contract facts available -> no-op, never raises.
    gate_repair._reconcile_skipped_gate_events(
        run, None, plan, hook="before_delivery", phase="", executed=set(),
    )
    assert run.state.extras == {}


# ── T1 KEY: fresh receipt, scheduled gate not executed -> skipped_fresh ─────


def test_fresh_receipt_unexecuted_gate_is_skipped_fresh_end_to_end(
    tmp_path: Path,
) -> None:
    """KEY regression. A required command scheduled ``suggest`` at
    ``before_delivery`` is NOT executed there (routing runs only ``require``
    gates). With a fresh receipt on disk, the recorder writes a durable
    ``skipped_fresh`` decision and the timeline shows it as skipped_fresh — never
    ran/pass (which would falsely claim the hook executed it) and never an absent
    event."""
    from pipeline.project import gate_repair

    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract([
        {"before_delivery": True, "policy": "suggest", "commands": ["lint"]},
    ])
    _write_receipt(run_dir, "lint", exit_code=0)  # fresh, exit 0, present
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    run = _gate_run(contract, ctx, run_dir)

    outcome = gate_repair.run_gate_hook(
        run, object(), object(), hook="before_delivery",
    )

    # suggest gate is not a require gate -> routing executed nothing here.
    assert outcome == gate_repair.GateRepairOutcome(active=False)
    events = _execution_events(run)
    assert [(e.command, e.kind, e.outcome) for e in events] == [
        ("lint", "reuse", "fresh"),
    ]


# ── T1: per-event timeline aggregation ─────────────────────────────────────


def test_timeline_autorun_event_splits_pass_and_fail(tmp_path: Path) -> None:
    _, _, run_dir = _layout(tmp_path)
    extras = {
        "verification_autorun": [
            {
                "attempted": True, "reason": "pre-final",
                "phase": "final_acceptance", "source": "stage9_autorun",
                "ran_commands": ["lint", "unit"], "failed": ["unit"],
                "skipped_fresh": ["fmt"], "skipped_manual": [], "receipt_paths": [],
            },
        ],
    }
    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert len(timeline.events) == 1
    event = timeline.events[0]
    assert event.hook_label == "pre-final auto-run"
    assert event.source == "stage9_autorun"
    assert event.ran_pass == ("lint",)
    assert event.ran_fail == ("unit",)
    assert event.skipped_fresh == ("fmt",)


def test_timeline_scheduled_events_only_from_decisions(tmp_path: Path) -> None:
    from pipeline.project.gate_repair import VERIFICATION_GATE_EVENTS_KEY

    _, _, run_dir = _layout(tmp_path)
    extras = {
        VERIFICATION_GATE_EVENTS_KEY: [
            {"hook": "after_phase", "phase": "implement", "command": "lint",
             "gate_set": "core", "decision": "executed_pass"},
            {"hook": "after_phase", "phase": "implement", "command": "unit",
             "gate_set": "core", "decision": "executed_fail"},
            {"hook": "before_delivery", "phase": "", "command": "lint",
             "gate_set": "core", "decision": "skipped_fresh"},
        ],
    }
    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    by_label = {e.hook_label: e for e in timeline.events}
    assert by_label["after_phase(implement)"].ran_pass == ("lint",)
    assert by_label["after_phase(implement)"].ran_fail == ("unit",)
    assert by_label["before_delivery"].skipped_fresh == ("lint",)
    assert by_label["before_delivery"].ran_pass == ()


def test_timeline_merges_scheduled_then_autorun(tmp_path: Path) -> None:
    from pipeline.project.gate_repair import VERIFICATION_GATE_EVENTS_KEY

    _, _, run_dir = _layout(tmp_path)
    extras = {
        VERIFICATION_GATE_EVENTS_KEY: [
            {"hook": "after_phase", "phase": "implement", "command": "lint",
             "gate_set": "core", "decision": "executed_pass"},
        ],
        "verification_autorun": [
            {"attempted": True, "reason": "pre-final", "phase": "final_acceptance",
             "source": "stage9_autorun", "ran_commands": ["unit"], "failed": []},
        ],
    }
    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert [e.hook_label for e in timeline.events] == [
        "after_phase(implement)", "pre-final auto-run",
    ]


def test_timeline_omitted_without_contract_or_evidence(tmp_path: Path) -> None:
    _, _, run_dir = _layout(tmp_path)
    assert build_verification_timeline(run_dir=run_dir, extras=None) is None
    assert build_verification_timeline(
        run_dir=run_dir, extras={}, session={},
    ) is None


# ── T2: run-level operator projection (residual failed / manual / inherited) ─
#
# These cover the additive run-level DONE fields T1 added: the per-event line
# still carries hook/phase + receipt path; ``manual-only`` surfaces intentional
# non-auto required commands; ``residual`` now also lists ``failed`` (distinct
# from ``stale``); the actionable ``searched run dirs`` / ``fix`` lines share
# their source with readiness; and parent-inherited receipts are distinguishable
# from current-run proof. Build through ``build_verification_timeline`` so the
# single classify pass and the render are exercised end-to-end.


def _projection_extras(contract: Any, ctx: Any, **more: Any) -> dict[str, Any]:
    """Extras wiring the run-level projection reads (contract + placeholders)."""
    return {
        "verification_contract": contract,
        "verification_placeholders": ctx,
        **more,
    }


def test_timeline_event_carries_hook_phase_and_receipt_path(
    tmp_path: Path,
) -> None:
    """A scheduled event keeps its hook(phase) label and the durable receipt
    path recorded by the executed decision."""
    from pipeline.project.gate_repair import VERIFICATION_GATE_EVENTS_KEY

    _, _, run_dir = _layout(tmp_path)
    receipt_path = str(run_dir / "verification_command_receipts" / "lint.json")
    extras = {
        VERIFICATION_GATE_EVENTS_KEY: [
            {"hook": "after_phase", "phase": "implement", "command": "lint",
             "gate_set": "core", "decision": "executed_pass",
             "receipt_path": receipt_path},
        ],
    }
    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    event = timeline.events[0]
    assert event.hook_label == "after_phase(implement)"
    assert receipt_path in event.receipt_paths


def test_timeline_manual_only_required_shown_as_intentional(
    tmp_path: Path,
) -> None:
    """A required command parked behind ``manual_only`` is surfaced on the
    dedicated ``manual-only`` line — the operator signal that its absence is
    intentional, not failed auto-work — even with no skipped_manual hook
    record. A plain required command with no receipt stays a genuine residual."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["lint", "manual-gate"], manual=("manual-gate",))
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    # Only the plain required command was auto-run (passed); manual-gate withheld.
    extras = _projection_extras(contract, ctx, **{
        "verification_autorun": [
            {"attempted": True, "reason": "pre-final", "phase": "final_acceptance",
             "source": "stage9_autorun", "ran_commands": ["lint"], "failed": []},
        ],
    })

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    # manual-gate is flagged manual (intentional), distinct from the residual.
    assert timeline.manual_only == ("manual-gate",)
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "manual-only: manual-gate" in block


def test_timeline_manual_only_excluded_from_residual_and_blocking(
    tmp_path: Path,
) -> None:
    """A required manual/operator-only command with no receipt is surfaced ONLY
    on the manual-only line — never in the required residual and never in the
    blocking set (ADR 0097 / criterion: manual_only ∉ blocking/residual)."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _contract(["req", "mgate"], manual=("mgate",))
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    extras = _projection_extras(contract, ctx)

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert timeline.manual_only == ("mgate",)
    assert "mgate" not in timeline.residual_missing
    assert "mgate" not in timeline.blocking_residual
    assert "mgate" not in timeline.warning_residual
    # The plain required command is a real (warn-policy) residual.
    assert "req" in timeline.residual_missing


def test_timeline_require_residual_is_blocking_in_done_block(
    tmp_path: Path,
) -> None:
    """A require-policy gate (scheduled require at before_delivery) that is
    missing surfaces as a blocking residual in the DONE block, with no
    'shipping allowed by policy' note."""
    project, workspace, run_dir = _layout(tmp_path)
    contract = _routing_contract(
        [{"before_delivery": True, "policy": "require", "commands": ["lint"]}],
        required=("lint",),
    )
    ctx = _ctx(contract, checkout=project, project=project,
               workspace=workspace, run_dir=run_dir)
    extras = _projection_extras(contract, ctx)

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert timeline.blocking_residual == ("lint",)
    assert timeline.warning_residual == ()
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "residual: missing=lint" in block
    assert "blocking (require): lint" in block
    assert "shipping allowed by policy" not in block


def test_done_render_splits_blocking_and_warning_by_policy() -> None:
    """The DONE render distinguishes require (blocking) from warn/suggest
    (warning, shipping allowed) residual commands and shows each gate's policy."""
    timeline = VerificationTimeline(
        residual_missing=("req", "warned"),
        policy_by_command=(("req", "require"), ("warned", "warn")),
    )
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "residual: missing=req, warned" in block
    assert "blocking (require): req" in block
    assert (
        "warning (warn/suggest): warned (warn) — shipping allowed by policy"
        in block
    )


def test_done_render_without_policy_map_omits_classification_lines() -> None:
    """A directly-constructed timeline (no policy map) renders the residual block
    byte-identically — no blocking/warning classification lines added."""
    timeline = VerificationTimeline(residual_missing=("unit",))
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "residual: missing=unit" in block
    assert "blocking (require):" not in block
    assert "shipping allowed by policy" not in block


def _failing_residual_run(
    tmp_path: Path,
) -> tuple[Any, Any, Path, Path]:
    """A typed-subject run where lint is failed, unit missing, smoke stale."""
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    head = DEFAULT_VERIFICATION_SUBJECT.observed_head_oid
    fingerprint = "f" * 64
    contract = _contract(["lint", "unit", "smoke"])
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    # lint failed (exit 1, same diff); smoke stale (head mismatch); unit absent.
    _write_receipt(run_dir, "lint", exit_code=1,
                   checkout_head=head, fingerprint=fingerprint)
    _write_receipt(run_dir, "smoke", exit_code=0,
                   checkout_head="0" * 40, fingerprint=fingerprint)
    return contract, ctx, project, run_dir


@pytest.mark.git_worktree
def test_timeline_residual_failed_with_searched_and_fix(tmp_path: Path) -> None:
    """missing/stale/failed required all surface: the residual line carries a
    ``failed=`` segment alongside ``missing=`` / ``stale=``, and the actionable
    ``searched run dirs`` + ``fix`` lines name the run dir and the exact verify
    command (identical to readiness's ``suggested_verify_commands``)."""
    contract, ctx, project, run_dir = _failing_residual_run(tmp_path)
    extras = _projection_extras(contract, ctx)

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    assert timeline.residual_missing == ("unit",)
    assert timeline.residual_stale == ("smoke",)
    assert timeline.residual_failed == ("lint",)

    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "residual: missing=unit stale=smoke failed=lint" in block
    assert f"searched run dirs: {run_dir}" in block
    # The fix hint is exactly what readiness would print for the same input set
    # (missing + stale + failed), so the two surfaces can never contradict.
    expected = suggested_verify_commands(
        contract, ("unit", "smoke", "lint"),
        run_id=run_dir.name, project=str(project),
    )
    assert timeline.suggested_commands == expected
    assert f"fix: {'; '.join(expected)}" in block


@pytest.mark.git_worktree
def test_timeline_stale_and_failed_are_distinguishable(tmp_path: Path) -> None:
    """A failed receipt and a stale receipt land in distinct residual buckets —
    smoke is stale (never reported failed), lint is failed (never reported
    stale)."""
    contract, ctx, _project, run_dir = _failing_residual_run(tmp_path)
    timeline = build_verification_timeline(
        run_dir=run_dir, extras=_projection_extras(contract, ctx),
    )
    assert timeline is not None
    assert "smoke" in timeline.residual_stale
    assert "smoke" not in timeline.residual_failed
    assert "lint" in timeline.residual_failed
    assert "lint" not in timeline.residual_stale


@pytest.mark.git_worktree
def test_timeline_inherited_parent_receipt_distinct_from_current(
    tmp_path: Path,
) -> None:
    """A present receipt inherited from a parent run is shown on the ``inherited``
    line with its parent run id + path, distinct from current-run proof."""
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    head = DEFAULT_VERIFICATION_SUBJECT.observed_head_oid
    fingerprint = "f" * 64
    parent_run = tmp_path / "parent_run_20260612"
    parent_run.mkdir()
    # Child has NO receipt; the parent carries a valid same-diff present one.
    _write_receipt(parent_run, "test", exit_code=0,
                   checkout_head=head, fingerprint=fingerprint)
    contract = _contract(["test"])
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)
    extras = _projection_extras(contract, ctx, **{
        VERIFICATION_PARENT_RUNS_EXTRAS_KEY: ((parent_run.name, str(parent_run)),),
    })

    timeline = build_verification_timeline(run_dir=run_dir, extras=extras)
    assert timeline is not None
    expected = (
        f"test from run {parent_run.name} "
        f"({receipt_file_path(parent_run, 'test')})"
    )
    assert timeline.inherited == (expected,)
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert f"inherited: {expected}" in block


@pytest.mark.git_worktree
def test_timeline_no_parent_present_receipt_has_no_inherited_line(
    tmp_path: Path,
) -> None:
    """A current-run present receipt adds no ``inherited`` line — a no-parent run
    stays byte-identical (the line is absent, not empty)."""
    project, workspace, run_dir = _layout(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    head = DEFAULT_VERIFICATION_SUBJECT.observed_head_oid
    fingerprint = "f" * 64
    _write_receipt(run_dir, "test", exit_code=0,
                   checkout_head=head, fingerprint=fingerprint)
    contract = _contract(["test"])
    ctx = _ctx(contract, checkout=checkout, project=project,
               workspace=workspace, run_dir=run_dir)

    timeline = build_verification_timeline(
        run_dir=run_dir, extras=_projection_extras(contract, ctx),
    )
    assert timeline is not None
    assert timeline.inherited == ()
    block = "\n".join(render_verification_gate_done_block(timeline))
    assert "inherited:" not in block
