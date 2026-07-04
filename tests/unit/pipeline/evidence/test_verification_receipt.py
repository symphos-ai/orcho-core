"""Durable verification-environment receipt (ADR 0076 / T6).

Pins that:

* the writer lands a valid-shape receipt under the run output dir, never
  the source checkout;
* both the ``implement`` and ``repair_changes`` phases write a receipt
  under the run output dir under identical conditions;
* no ``.venv`` / generated environment leaks into the checkout;
* the evidence collector re-exports an additive receipt summary without
  breaking the v1 schema.

Named so the targeted slice ``pytest -q tests/unit/pipeline/evidence -k
"receipt or verification"`` catches every case.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.evidence.verification_receipt import (
    COMMAND_RECEIPT_SCHEMA_VERSION,
    COMMAND_RECEIPTS_DIRNAME,
    ENV_RECEIPTS_DIRNAME,
    RECEIPTS_DIRNAME,
    VERIFICATION_COMMAND_KIND,
    VERIFICATION_ENV_KIND,
    VERIFICATION_RECEIPT_KIND,
    EnvProvenanceFailure,
    collect_environment_checks,
    environment_provenance_failures,
    load_command_receipts,
    load_env_assertion_receipts,
    summarize_command_receipts,
    summarize_verification_receipts,
    write_command_receipt,
    write_env_assertion_receipt,
    write_phase_verification_receipt,
    write_verification_receipt,
)
from pipeline.lifecycle import default_lifecycle_context
from pipeline.plugins import PluginConfig
from pipeline.runtime import PhaseRegistry, PipelineState
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)

_REQUIRED_RECEIPT_KEYS = {
    "phase", "round", "kind", "cwd", "python",
    "checks", "commands", "temp_env_outside_checkout",
}


# ── recording agent (mirrors the phase-handler test surface) ───────────


class _RecordingAgent:
    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        model: str = "claude-opus-4-7",
        session_id: str | None = "sess-1",
    ) -> None:
        self.model = model
        self.session_id = session_id
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or ["phase output"])

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        continue_session: bool = False,
        attachments: tuple = (),
        mutates_artifacts: bool = False,
    ) -> str:
        self.calls.append({"cwd": cwd})
        head, *rest = self._responses
        self._responses = rest or [head]
        return head


def _install_agents(state: PipelineState, agent: _RecordingAgent) -> None:
    from agents.registry import PhaseAgentConfig

    state.phase_config = PhaseAgentConfig(
        plan_agent=agent,
        validate_plan_agent=agent,
        implement_agent=agent,
        review_changes_agent=agent,
        repair_changes_agent=agent,
        repair_escalation_agent=agent,
        final_acceptance_agent=agent,
    )


def _state(*, checkout: Path, run_dir: Path, **extra) -> PipelineState:
    extras: dict[str, Any] = {
        "run_id": "20260101_000000",
        "repair_round": 1,
        "session_mode_initial": "auto",
        "implement_model": "claude-opus-4-7",
        "repair_model": "claude-opus-4-7",
    }
    extras.update(extra)
    st = PipelineState(
        task="do the work",
        project_dir=str(checkout),
        plugin=PluginConfig(),
        extras=extras,
    )
    st.output_dir = run_dir
    # ADR 0113 (declarative continuity): the implement / repair handlers
    # resolve continuity off the active step. These handler-level tests bypass
    # the FSM (which seeds active_step in production), so seed a real lifecycle
    # context with implement/repair's real declared continuity —
    # ``same_zone_continue``. With no prior session / same-write-zone signal it
    # resolves fresh, leaving these receipt-provenance assertions unchanged.
    ctx = default_lifecycle_context(phase_registry=PhaseRegistry())
    ctx.active_step = SimpleNamespace(
        prompt=None,
        execution_policy=SimpleNamespace(
            session_split=None, session_continuity="same_zone_continue",
        ),
    )
    st.lifecycle_ctx = ctx
    return st


@pytest.fixture(autouse=True)
def _drain_render_envelope():
    from core.observability import prompt_trace
    prompt_trace.take_last_upper()
    yield
    prompt_trace.take_last_upper()


def _assert_valid_receipt(path: Path, *, phase: str) -> dict[str, Any]:
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data.keys()) >= _REQUIRED_RECEIPT_KEYS
    assert data["phase"] == phase
    assert data["kind"] == VERIFICATION_RECEIPT_KIND
    assert isinstance(data["checks"], list)
    assert isinstance(data["commands"], list)
    assert isinstance(data["round"], int)
    return data


# ── writer (direct) ────────────────────────────────────────────────────


class TestEnvironmentChecks:
    """The import-invariant must prove the interpreter imports `pipeline`
    from the CHECKOUT (cwd), not from a stale install (F2)."""

    def test_passes_for_checkout_local_pipeline(self, tmp_path) -> None:
        # A checkout that actually contains a `pipeline/` package: the
        # subprocess (cwd on sys.path[0]) must import THAT package.
        checkout = tmp_path / "checkout"
        pkg = checkout / "pipeline"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")

        checks, commands = collect_environment_checks(checkout)
        check = checks[0]
        assert check["name"] == "pipeline_import"
        expected = str((pkg / "__init__.py").resolve())
        assert check["expected"] == expected
        assert check["actual"] == expected
        assert check["passed"] is True
        # actual resolves INSIDE the checkout.
        assert str(checkout.resolve()) in check["actual"]
        assert commands[0]["exit_code"] == 0

    def test_non_core_checkout_without_assertions_is_non_failing(
        self, tmp_path,
    ) -> None:
        # A non-core checkout (no local `pipeline/`) with no declared contract
        # assertions must NOT manufacture a provenance failure (T2 branch c):
        # the import-invariant is core-only. The recorded check is non-failing.
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        checks, commands = collect_environment_checks(checkout)
        assert checks  # never empty
        assert all(c["passed"] for c in checks)
        assert not any(c["name"] == "pipeline_import" for c in checks)
        assert commands  # diagnostic command preserved
        assert "exit_code" in commands[0]

    def test_core_external_import_keeps_provenance_failure(
        self, tmp_path, monkeypatch,
    ) -> None:
        # A core checkout (local `pipeline/__init__.py` present) whose
        # interpreter imports `pipeline` from OUTSIDE the checkout must still
        # fail — the load-bearing ADR 0125 invariant is preserved (branch a).
        import subprocess

        checkout = tmp_path / "checkout"
        pkg = checkout / "pipeline"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")

        external = tmp_path / "install" / "pipeline" / "__init__.py"
        external.parent.mkdir(parents=True)
        external.write_text("", encoding="utf-8")

        class _Proc:
            returncode = 0
            stdout = str(external)
            stderr = ""

        def _fake_run(*_a, **_k):
            return _Proc()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        checks, _commands = collect_environment_checks(checkout)
        check = checks[0]
        assert check["name"] == "pipeline_import"
        assert check["expected"] == str((pkg / "__init__.py").resolve())
        # actual resolved OUTSIDE the checkout -> not green.
        assert check["passed"] is False
        assert str(checkout.resolve()) not in (check["actual"] or "")


class TestProjectAwareProvenance:
    """T2: phase-receipt provenance is contract-aware. A non-core (MCP-shaped)
    checkout proves its own import provenance via declared env assertions; a
    checkout with neither a local pipeline nor assertions never false-fails."""

    def _mcp_contract(
        self, checkout: Path, deproot: Path,
    ) -> VerificationContract:
        pythonpath = "{checkout}/src" + os.pathsep + "{dependency:orcho-core}"
        contract = VerificationContract.from_plugin(PluginConfig(
            dependency_repos={"orcho-core": {"path": str(deproot)}},
            verification_envs={
                "ci": {
                    "cwd": str(checkout),
                    "env": {"PYTHONPATH": pythonpath},
                    "assertions": [
                        {"import": "orcho_mcp", "path_under": "{checkout}/src"},
                        {"import": "pipeline",
                         "path_under": "{dependency:orcho-core}"},
                    ],
                },
            },
            verification={"default_env": "ci"},
        ))
        assert contract is not None
        return contract

    def _ctx_for(self, contract, checkout: Path):
        return placeholder_context_for(
            contract,
            checkout=str(checkout),
            project=str(checkout),
            workspace="",
            run_dir=None,
        )

    def test_mcp_checkout_declared_assertions_pass(self, tmp_path) -> None:
        # An MCP-shaped checkout: orcho_mcp lives under {checkout}/src and the
        # pipeline dependency lives in a separate dependency checkout. No local
        # pipeline/ at the checkout root.
        checkout = tmp_path / "checkout"
        (checkout / "src" / "orcho_mcp").mkdir(parents=True)
        (checkout / "src" / "orcho_mcp" / "__init__.py").write_text(
            "", encoding="utf-8",
        )
        deproot = tmp_path / "dep"
        (deproot / "pipeline").mkdir(parents=True)
        (deproot / "pipeline" / "__init__.py").write_text("", encoding="utf-8")

        contract = self._mcp_contract(checkout, deproot)
        ctx = self._ctx_for(contract, checkout)

        checks, commands = collect_environment_checks(
            checkout, contract=contract, ctx=ctx,
        )

        # Every declared assertion mapped into a check, all passing.
        names = {c["name"] for c in checks}
        assert names == {"orcho_mcp", "pipeline"}
        assert all(c["passed"] for c in checks)
        assert commands  # diagnostic command preserved

        # Written to a phase receipt, it yields NO provenance failure.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_phase_verification_receipt(
            output_dir=run_dir, phase="implement", round=1, cwd=checkout,
            contract=contract, ctx=ctx,
        )
        assert environment_provenance_failures(run_dir) == ()

    def test_checkout_without_pipeline_or_assertions_does_not_fail(
        self, tmp_path,
    ) -> None:
        # A contract declaring an env with NO assertions, and a checkout with no
        # local pipeline: must NOT raise on expected==None and must record no
        # provenance failure (T2 branch c).
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        contract = VerificationContract.from_plugin(PluginConfig(
            verification_envs={"ci": {}},
            verification={"default_env": "ci"},
        ))
        assert contract is not None
        ctx = self._ctx_for(contract, checkout)

        checks, _commands = collect_environment_checks(
            checkout, contract=contract, ctx=ctx,
        )
        assert all(c["passed"] for c in checks)

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_phase_verification_receipt(
            output_dir=run_dir, phase="implement", round=1, cwd=checkout,
            contract=contract, ctx=ctx,
        )
        assert environment_provenance_failures(run_dir) == ()

    def test_core_local_pipeline_ignores_contract(self, tmp_path) -> None:
        # When a local pipeline/ exists, branch (a) wins regardless of contract:
        # the core import-invariant is recorded, never the declared assertions.
        checkout = tmp_path / "checkout"
        pkg = checkout / "pipeline"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        deproot = tmp_path / "dep"
        (deproot / "pipeline").mkdir(parents=True)
        (deproot / "pipeline" / "__init__.py").write_text("", encoding="utf-8")

        contract = self._mcp_contract(checkout, deproot)
        ctx = self._ctx_for(contract, checkout)

        checks, _commands = collect_environment_checks(
            checkout, contract=contract, ctx=ctx,
        )
        assert [c["name"] for c in checks] == ["pipeline_import"]
        assert checks[0]["expected"] == str((pkg / "__init__.py").resolve())


class TestWriter:
    def test_writes_valid_receipt_under_run_dir(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        path = write_verification_receipt(
            output_dir=run_dir,
            phase="implement",
            round=1,
            cwd=checkout,
            checks=[{"name": "ruff", "expected": "0", "actual": "0", "passed": True}],
            commands=[{"argv": ["ruff", "check", "."], "exit_code": 0}],
        )
        assert path is not None
        # Under run dir, never the checkout.
        assert str(path).startswith(str(run_dir))
        assert RECEIPTS_DIRNAME in path.parts
        assert str(checkout) not in str(path)
        data = _assert_valid_receipt(path, phase="implement")
        assert data["checks"][0]["passed"] is True
        assert data["commands"][0]["exit_code"] == 0

    def test_no_output_dir_is_noop(self, tmp_path) -> None:
        assert write_verification_receipt(
            output_dir=None, phase="implement", round=1, cwd=tmp_path,
        ) is None


# ── implement phase writes a receipt ───────────────────────────────────


class TestImplementWritesReceipt:
    def test_receipt_under_run_dir(self, tmp_path) -> None:
        from pipeline.phases.builtin import _phase_implement

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        state = _state(checkout=checkout, run_dir=run_dir)
        _install_agents(state, _RecordingAgent(responses=["implement output"]))

        _phase_implement(state)

        path = run_dir / RECEIPTS_DIRNAME / "implement_round1.json"
        data = _assert_valid_receipt(path, phase="implement")
        assert data["cwd"] == str(checkout)
        # The receipt must record at least one real environment check and
        # one real command run by the interpreter — never empty. This empty
        # non-core checkout with no contract records a non-failing provenance
        # check (T2 branch c), never a false failure.
        assert len(data["checks"]) >= 1
        assert any(
            c.get("name") == "environment_provenance" for c in data["checks"]
        )
        assert not environment_provenance_failures(run_dir)
        assert len(data["commands"]) >= 1
        assert "argv" in data["commands"][0]
        assert "exit_code" in data["commands"][0]
        # No environment leaked into the checkout.
        assert not (checkout / ".venv").exists()
        assert not (checkout / RECEIPTS_DIRNAME).exists()
        assert list(checkout.iterdir()) == []


# ── repair_changes phase writes a receipt ──────────────────────────────


class TestRepairWritesReceipt:
    def test_receipt_under_run_dir(self, tmp_path) -> None:
        from pipeline.phases.builtin import _phase_repair_changes

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        state = _state(
            checkout=checkout, run_dir=run_dir, repair_round=2,
        )
        state.last_critique = "Fix the null deref."
        _install_agents(state, _RecordingAgent(responses=["repair output"]))

        _phase_repair_changes(state)

        path = run_dir / RECEIPTS_DIRNAME / "repair_changes_round2.json"
        data = _assert_valid_receipt(path, phase="repair_changes")
        assert data["round"] == 2
        assert data["cwd"] == str(checkout)
        # The receipt must record at least one real environment check and
        # one real command run by the interpreter — never empty. This empty
        # non-core checkout with no contract records a non-failing provenance
        # check (T2 branch c), never a false failure.
        assert len(data["checks"]) >= 1
        assert any(
            c.get("name") == "environment_provenance" for c in data["checks"]
        )
        assert not environment_provenance_failures(run_dir)
        assert len(data["commands"]) >= 1
        assert "argv" in data["commands"][0]
        assert "exit_code" in data["commands"][0]
        # No environment leaked into the checkout.
        assert not (checkout / ".venv").exists()
        assert list(checkout.iterdir()) == []


# ── collector re-exports the summary additively ────────────────────────


def _engine_result(checkout: Path) -> dict[str, Any]:
    """A minimal T1-engine result shape (pipeline.verification_env)."""
    return {
        "subject": {
            "env": "ci",
            "checkout": str(checkout),
            "project": str(checkout),
        },
        "cwd": str(checkout),
        "interpreter": "3.12.4 (/usr/bin/python3)",
        "env_overrides": {"FOO": "bar"},
        "assertions": [
            {
                "name": "pkg",
                "kind": "import_path_equals",
                "expected": "/abs/pkg/__init__.py",
                "actual": "/abs/pkg/__init__.py",
                "passed": True,
                "detail": "",
            },
        ],
        "all_passed": True,
    }


_REQUIRED_ENV_RECEIPT_KEYS = {
    "kind", "env", "subject", "cwd", "interpreter",
    "env_overrides", "assertions", "all_passed", "temp_env_outside_checkout",
}


class TestEnvAssertionReceiptWriter:
    def test_writes_under_env_receipt_dir_not_checkout(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        checkout = tmp_path / "checkout"
        checkout.mkdir()

        path = write_env_assertion_receipt(
            output_dir=run_dir, result=_engine_result(checkout),
        )

        assert path is not None
        # Under the env-receipt dir, NOT the collector's verification_receipts/.
        assert str(path).startswith(str(run_dir))
        assert ENV_RECEIPTS_DIRNAME in path.parts
        assert RECEIPTS_DIRNAME not in path.parts
        # Never under the checkout.
        assert str(checkout) not in str(path)
        assert not (checkout / ENV_RECEIPTS_DIRNAME).exists()
        assert not (run_dir / RECEIPTS_DIRNAME).exists()

    def test_receipt_has_required_fields(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        checkout = tmp_path / "checkout"
        checkout.mkdir()

        path = write_env_assertion_receipt(
            output_dir=run_dir, result=_engine_result(checkout),
        )
        data = json.loads(path.read_text(encoding="utf-8"))

        assert set(data.keys()) >= _REQUIRED_ENV_RECEIPT_KEYS
        assert data["kind"] == VERIFICATION_ENV_KIND
        assert data["env"] == "ci"
        assert data["subject"] == {
            "checkout": str(checkout), "project": str(checkout),
        }
        assert data["cwd"] == str(checkout)
        assert data["interpreter"].startswith("3.12")
        assert data["env_overrides"] == {"FOO": "bar"}
        assert data["all_passed"] is True
        assert data["temp_env_outside_checkout"] is True
        a = data["assertions"][0]
        assert set(a.keys()) == {
            "name", "kind", "expected", "actual", "passed", "detail",
        }

    def test_no_output_dir_is_noop(self, tmp_path) -> None:
        assert write_env_assertion_receipt(
            output_dir=None, result=_engine_result(tmp_path),
        ) is None

    def test_env_name_sanitized_in_filename(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        result = _engine_result(tmp_path)
        result["subject"]["env"] = "../weird/name"

        path = write_env_assertion_receipt(output_dir=run_dir, result=result)

        # No path traversal — the file stays directly inside the env dir.
        assert path.parent == run_dir / ENV_RECEIPTS_DIRNAME
        assert ".." not in path.name
        assert "/" not in path.name

    def test_loader_reads_only_env_dir(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_env_assertion_receipt(
            output_dir=run_dir, result=_engine_result(tmp_path),
        )
        loaded = load_env_assertion_receipts(run_dir)
        assert len(loaded) == 1
        assert loaded[0]["kind"] == VERIFICATION_ENV_KIND

    def test_env_receipt_isolated_from_evidence_bundle(self, tmp_path) -> None:
        """The new kind must NOT enter the evidence v1 digest, achieved by
        physical directory isolation, not filtering."""
        from pipeline.evidence.collector import _build_verification_receipts

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_env_assertion_receipt(
            output_dir=run_dir, result=_engine_result(tmp_path),
        )

        # The collector summary + the public summary both read only
        # verification_receipts/, so the env kind is absent.
        summary = summarize_verification_receipts(run_dir)
        digest = _build_verification_receipts(run_dir)
        assert summary == []
        assert digest == []
        assert all(s.get("kind") != VERIFICATION_ENV_KIND for s in summary)
        assert all(s.get("kind") != VERIFICATION_ENV_KIND for s in digest)


def _command_result(checkout: Path, **overrides) -> dict[str, Any]:
    """A minimal T3 run_command result shape."""
    result: dict[str, Any] = {
        "kind": "verification_command",
        "command": "lint",
        "env": "ci",
        "cwd": str(checkout),
        "placeholders": {"checkout": str(checkout), "project": str(checkout)},
        "argv": ["ruff", "check", "."],
        "env_overrides": {"FOO": "bar"},
        "assertions": [
            {
                "name": "pkg",
                "kind": "import_path_equals",
                "expected": "/abs/pkg/__init__.py",
                "actual": "/abs/pkg/__init__.py",
                "passed": True,
                "detail": "",
            },
        ],
        "exit_code": 0,
        "duration_s": 0.42,
        "stdout_tail": "ok",
        "stderr_tail": "",
        "log_path": str(checkout / "x.log"),
        "parity": "absolute",
        "detail": "",
        "git": {
            "checkout_head": "abc123",
            "baseline_head": None,
            "changed_files_fingerprint": "deadbeefcafe0000",
        },
    }
    result.update(overrides)
    return result


_REQUIRED_COMMAND_RECEIPT_KEYS = {
    "schema_version", "kind", "command", "env", "cwd", "placeholders",
    "argv", "env_overrides", "assertions", "exit_code", "duration_s",
    "stdout_tail", "stderr_tail", "log_path", "parity", "git", "dependencies",
}


class TestCommandReceiptWriter:
    def test_writes_under_command_dir_with_schema_version(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        checkout = tmp_path / "checkout"
        checkout.mkdir()

        path = write_command_receipt(
            output_dir=run_dir, result=_command_result(checkout),
        )

        assert path is not None
        # Under the command-receipt dir, NOT the collector's dir or checkout.
        assert str(path).startswith(str(run_dir))
        assert COMMAND_RECEIPTS_DIRNAME in path.parts
        assert RECEIPTS_DIRNAME not in path.parts
        assert ENV_RECEIPTS_DIRNAME not in path.parts
        assert str(checkout) not in str(path)
        assert not (checkout / COMMAND_RECEIPTS_DIRNAME).exists()

        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data.keys()) >= _REQUIRED_COMMAND_RECEIPT_KEYS
        assert data["schema_version"] == COMMAND_RECEIPT_SCHEMA_VERSION
        assert data["kind"] == VERIFICATION_COMMAND_KIND
        assert data["placeholders"] == {
            "checkout": str(checkout), "project": str(checkout),
        }
        assert data["git"]["checkout_head"] == "abc123"

    def test_no_output_dir_is_noop(self, tmp_path) -> None:
        assert write_command_receipt(
            output_dir=None, result=_command_result(tmp_path),
        ) is None

    def test_command_name_sanitized_in_filename(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        result = _command_result(tmp_path, command="../weird/cmd")

        path = write_command_receipt(output_dir=run_dir, result=result)

        assert path.parent == run_dir / COMMAND_RECEIPTS_DIRNAME
        assert ".." not in path.name
        assert "/" not in path.name

    def test_required_differential_persists_baseline_and_checkout(
        self, tmp_path,
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        result = _command_result(
            checkout,
            parity="differential",
            git={
                "checkout_head": "headsha111",
                "baseline_head": "basesha222",
                "changed_files_fingerprint": "ffeeddccbbaa9988",
            },
        )

        path = write_command_receipt(output_dir=run_dir, result=result)
        data = json.loads(path.read_text(encoding="utf-8"))

        assert data["parity"] == "differential"
        assert data["git"]["checkout_head"] == "headsha111"
        assert data["git"]["baseline_head"] == "basesha222"
        assert data["placeholders"]["checkout"] == str(checkout)

    def test_dependencies_block_serialized_with_fixed_keys(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        result = _command_result(
            tmp_path,
            dependencies=[
                {
                    "name": "shared",
                    "path": "/abs/shared",
                    "head": "depsha111",
                    "dirty": True,
                    "changed_files_count": 3,
                    "changed_files_fingerprint": "abcd1234abcd1234",
                    "depends_on": True,
                },
            ],
        )

        path = write_command_receipt(output_dir=run_dir, result=result)
        data = json.loads(path.read_text(encoding="utf-8"))

        assert data["schema_version"] == 2
        assert data["dependencies"] == [
            {
                "name": "shared",
                "path": "/abs/shared",
                "head": "depsha111",
                "dirty": True,
                "changed_files_count": 3,
                "changed_files_fingerprint": "abcd1234abcd1234",
                "depends_on": True,
            },
        ]

    def test_dependency_none_fields_preserved(self, tmp_path) -> None:
        # A non-git dependency degrades to head=None with None dirty fields; the
        # writer must keep those as None (not coerce to "" / 0 / False).
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        result = _command_result(
            tmp_path,
            dependencies=[
                {
                    "name": "plain",
                    "path": "/abs/plain",
                    "head": None,
                    "dirty": None,
                    "changed_files_count": None,
                    "changed_files_fingerprint": None,
                    "depends_on": False,
                },
            ],
        )

        path = write_command_receipt(output_dir=run_dir, result=result)
        dep = json.loads(path.read_text(encoding="utf-8"))["dependencies"][0]

        assert dep["head"] is None
        assert dep["dirty"] is None
        assert dep["changed_files_count"] is None
        assert dep["changed_files_fingerprint"] is None
        assert dep["depends_on"] is False

    def test_garbage_dependencies_degrade_to_empty_list(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        for junk in ("not-a-list", 123, {"name": "x"}, None):
            result = _command_result(tmp_path, dependencies=junk)
            path = write_command_receipt(output_dir=run_dir, result=result)
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["dependencies"] == []

    def test_garbage_fields_inside_dependency_entry_degrade(self, tmp_path) -> None:
        # Garbage inside an otherwise list-shaped dependencies block must not
        # raise out of write_command_receipt: non-Mapping entries are dropped
        # and an unparseable changed_files_count degrades to None.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        result = _command_result(
            tmp_path,
            dependencies=[
                "not-a-mapping",
                {
                    "name": "shared",
                    "path": "/abs/shared",
                    "head": "depsha111",
                    "dirty": True,
                    "changed_files_count": "bad",
                    "changed_files_fingerprint": "abcd1234abcd1234",
                    "depends_on": True,
                },
            ],
        )

        path = write_command_receipt(output_dir=run_dir, result=result)
        deps = json.loads(path.read_text(encoding="utf-8"))["dependencies"]

        assert len(deps) == 1
        assert deps[0]["name"] == "shared"
        assert deps[0]["changed_files_count"] is None
        assert deps[0]["depends_on"] is True

    def test_absent_dependencies_key_degrades_to_empty_list(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        result = _command_result(tmp_path)
        result.pop("dependencies", None)

        path = write_command_receipt(output_dir=run_dir, result=result)
        data = json.loads(path.read_text(encoding="utf-8"))

        assert data["dependencies"] == []


class TestCommandReceiptLoaderAndSummary:
    def test_loader_tolerant_of_missing_dir(self, tmp_path) -> None:
        assert load_command_receipts(tmp_path / "nope") == []
        assert summarize_command_receipts(tmp_path / "nope") == []

    def test_loader_reads_only_command_dir_sorted(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_command_receipt(
            output_dir=run_dir, result=_command_result(tmp_path, command="zzz"),
        )
        write_command_receipt(
            output_dir=run_dir, result=_command_result(tmp_path, command="aaa"),
        )
        loaded = load_command_receipts(run_dir)
        assert [r["command"] for r in loaded] == ["aaa", "zzz"]
        assert all(r["kind"] == VERIFICATION_COMMAND_KIND for r in loaded)

    def test_summary_passed_and_has_baseline(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_command_receipt(
            output_dir=run_dir,
            result=_command_result(
                tmp_path, command="green", exit_code=0,
                git={"checkout_head": "h", "baseline_head": "b",
                     "changed_files_fingerprint": "f"},
            ),
        )
        write_command_receipt(
            output_dir=run_dir,
            result=_command_result(tmp_path, command="red", exit_code=1),
        )
        summary = summarize_command_receipts(run_dir)
        green = next(s for s in summary if s["command"] == "green")
        red = next(s for s in summary if s["command"] == "red")
        assert green["passed"] is True
        assert green["has_baseline"] is True
        assert red["passed"] is False
        assert red["has_baseline"] is False

    def test_summary_passed_is_authoritative_over_exit_code(self, tmp_path) -> None:
        """An exit-0 receipt with a failed assertion or a non-empty detail must
        roll up to ``passed`` False — exit code alone is never authoritative."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_command_receipt(
            output_dir=run_dir,
            result=_command_result(
                tmp_path, command="assert_fail", exit_code=0,
                assertions=[{"name": "x", "passed": False, "detail": ""}],
            ),
        )
        write_command_receipt(
            output_dir=run_dir,
            result=_command_result(
                tmp_path, command="detail_fail", exit_code=0, assertions=[],
                detail="differential baseline regression",
            ),
        )
        summary = summarize_command_receipts(run_dir)
        assert next(s for s in summary if s["command"] == "assert_fail")["passed"] is False
        assert next(s for s in summary if s["command"] == "detail_fail")["passed"] is False

    def test_summary_never_carries_dependencies_key(self, tmp_path) -> None:
        """Falsifier: the v2 ``dependencies`` block is a run-local durable
        artifact only. The compact digest (which feeds the evidence v1 bundle /
        MCP wire via the collector) must NOT surface it — the digest key set is
        unchanged from v1. If this regresses, the dependencies block leaks onto
        the wire and an ``orcho-mcp`` alignment becomes mandatory."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_command_receipt(
            output_dir=run_dir,
            result=_command_result(
                tmp_path,
                command="lint",
                dependencies=[
                    {
                        "name": "shared", "path": "/abs/shared",
                        "head": "depsha111", "dirty": True,
                        "changed_files_count": 1,
                        "changed_files_fingerprint": "abcd1234abcd1234",
                        "depends_on": True,
                    },
                ],
            ),
        )

        summary = summarize_command_receipts(run_dir)

        assert len(summary) == 1
        assert "dependencies" not in summary[0]
        assert set(summary[0].keys()) == {
            "command", "env", "exit_code", "parity", "passed", "has_baseline",
        }


class TestCommandReceiptIsolatedFromEvidence:
    def test_command_receipt_absent_from_evidence_bundle(self, tmp_path) -> None:
        """The command kind must NOT enter the evidence v1 digest — achieved by
        physical directory isolation, not filtering."""
        from pipeline.evidence.collector import _build_verification_receipts

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_command_receipt(
            output_dir=run_dir, result=_command_result(tmp_path),
        )

        summary = summarize_verification_receipts(run_dir)
        digest = _build_verification_receipts(run_dir)
        assert summary == []
        assert digest == []
        assert all(s.get("kind") != VERIFICATION_COMMAND_KIND for s in summary)
        assert all(s.get("kind") != VERIFICATION_COMMAND_KIND for s in digest)
        # The command-receipt dir exists but the collector's dir does not.
        assert (run_dir / COMMAND_RECEIPTS_DIRNAME).is_dir()
        assert not (run_dir / RECEIPTS_DIRNAME).exists()


class TestMcpWireFalsifier:
    """ADR 0080 MCP falsifier: prove the command-receipt does NOT enter the
    evidence v1 wire, so no MCP resource (e.g. ``orcho_run_evidence``) reflects
    it and no ``orcho-mcp`` update is required. If this regresses (the kind
    leaks into the v1 schema), the MCP wire IS touched and the falsifier flips:
    an ``orcho-mcp`` update + mock smoke become mandatory."""

    def test_command_kind_absent_from_v1_schema(self) -> None:
        from pipeline.evidence import schema

        # The kind/dirname appear nowhere in the v1 top-level wire contract.
        assert VERIFICATION_COMMAND_KIND not in schema.REQUIRED_TOP_LEVEL_KEYS
        assert "verification_command" not in schema.REQUIRED_TOP_LEVEL_KEYS
        assert "verification_command" not in schema.REQUIRED_COMMAND_KEYS
        # The bundle's ``commands`` rollup is the event-derived command list,
        # NOT the verification command-receipt — disjoint key sets.
        assert frozenset(
            {"argv_summary", "cwd", "exit_code", "duration_s", "outcome"},
        ) == schema.REQUIRED_COMMAND_KEYS

    def test_valid_v1_bundle_never_carries_command_kind(self) -> None:
        from pipeline.evidence import schema

        # A bundle exercising the full v1 contract validates without any
        # verification_command surface — the kind has no slot to occupy.
        bundle = {
            "schema_version": schema.EVIDENCE_SCHEMA_VERSION,
            "run_id": "r", "run_dir": "/d", "status": "done",
            "created_at": "t", "task": "x", "profile": "p",
            "plan": {
                "source": "absent", "short_summary": "", "planning_context": "",
                "subtask_count": 0, "has_contract": False, "goal": None,
                "acceptance_criteria": [], "owned_files": [], "commands_to_run": [],
                "risks": [], "review_focus": [], "mcp_context": {},
            },
            "phases": [], "gates": [], "commands": [], "artifacts": [],
            "metrics": {
                "total_tokens": 0, "total_tokens_in": 0, "total_tokens_out": 0,
                "total_duration_s": 0, "total_rounds": 0,
            },
            "errors": [], "prompt_render": [], "raw_events_path": "events.jsonl",
        }
        schema.validate_bundle(bundle)  # must not raise
        assert "verification_command" not in json.dumps(bundle)


class TestEnvironmentProvenanceFailures:
    """T1-reader: surface failed environment-provenance checks from
    ``verification_environment`` phase-receipts as operator-evidence."""

    def test_failed_pipeline_import_yields_record(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_verification_receipt(
            output_dir=run_dir,
            phase="implement",
            round=1,
            cwd=tmp_path,
            checks=[{
                "name": "pipeline_import",
                "expected": "/abs/checkout/pipeline/__init__.py",
                "actual": "/abs/install/pipeline/__init__.py",
                "passed": False,
            }],
        )

        failures = environment_provenance_failures(run_dir)

        assert len(failures) == 1
        f = failures[0]
        assert isinstance(f, EnvProvenanceFailure)
        assert f.phase == "implement"
        assert f.round == 1
        assert f.check == "pipeline_import"
        assert f.expected == "/abs/checkout/pipeline/__init__.py"
        assert f.actual == "/abs/install/pipeline/__init__.py"
        # receipt_path points at the verification_environment receipt on disk.
        expected_path = run_dir / RECEIPTS_DIRNAME / "implement_round1.json"
        assert f.receipt_path == str(expected_path)
        assert Path(f.receipt_path).is_file()

    def test_all_passed_receipt_yields_nothing(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_verification_receipt(
            output_dir=run_dir,
            phase="implement",
            round=1,
            cwd=tmp_path,
            checks=[{
                "name": "pipeline_import",
                "expected": "/abs/pipeline/__init__.py",
                "actual": "/abs/pipeline/__init__.py",
                "passed": True,
            }],
        )

        assert environment_provenance_failures(run_dir) == ()

    def test_missing_directory_degrades_to_empty(self, tmp_path) -> None:
        assert environment_provenance_failures(tmp_path / "nope") == ()

    def test_broken_receipt_file_degrades_without_raising(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        receipts_dir = run_dir / RECEIPTS_DIRNAME
        receipts_dir.mkdir(parents=True)
        (receipts_dir / "implement_round1.json").write_text(
            "{ not valid json", encoding="utf-8",
        )
        # The malformed file is skipped by the tolerant loader; no exception.
        assert environment_provenance_failures(run_dir) == ()

    def test_one_record_per_failed_check(self, tmp_path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        write_verification_receipt(
            output_dir=run_dir,
            phase="repair_changes",
            round=2,
            cwd=tmp_path,
            checks=[
                {"name": "pipeline_import", "expected": "a", "actual": "b",
                 "passed": False},
                {"name": "ruff", "expected": "0", "actual": "0", "passed": True},
                {"name": "pytest", "expected": "0", "actual": "1",
                 "passed": False},
            ],
        )

        failures = environment_provenance_failures(run_dir)

        assert {f.check for f in failures} == {"pipeline_import", "pytest"}
        assert all(f.phase == "repair_changes" and f.round == 2 for f in failures)


class TestCollectorReexport:
    def test_summary_present_and_additive(self, tmp_path) -> None:
        # Write two receipts, then read them through the public summary.
        write_verification_receipt(
            output_dir=tmp_path, phase="implement", round=1, cwd=tmp_path,
            checks=[{"name": "pytest", "passed": True}],
        )
        write_verification_receipt(
            output_dir=tmp_path, phase="repair_changes", round=1, cwd=tmp_path,
            checks=[{"name": "pytest", "passed": False}],
        )
        summary = summarize_verification_receipts(tmp_path)
        assert len(summary) == 2
        phases = {s["phase"] for s in summary}
        assert phases == {"implement", "repair_changes"}
        impl = next(s for s in summary if s["phase"] == "implement")
        assert impl["all_passed"] is True
        rep = next(s for s in summary if s["phase"] == "repair_changes")
        assert rep["all_passed"] is False

    def test_collector_includes_verification_receipts_key(self, tmp_path) -> None:
        from pipeline.evidence.collector import _build_verification_receipts

        write_verification_receipt(
            output_dir=tmp_path, phase="implement", round=1, cwd=tmp_path,
        )
        receipts = _build_verification_receipts(tmp_path)
        assert isinstance(receipts, list)
        assert receipts and receipts[0]["phase"] == "implement"
