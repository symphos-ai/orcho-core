"""Unit tests for pipeline/verification_command.py (Stage 3 executor).

Real-subprocess tests (no mocks): the executor's contract is "we run argv
correctly and attribute git provenance to the run worktree", which only real
``git`` + real subprocesses can validate. Each test pins one guarantee; the
load-bearing one is F1 — git provenance comes from ``ctx.checkout`` even when
the subprocess runs in a different ``eff_cwd``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pipeline.verification_command import run_command
from pipeline.verification_contract import (
    PlaceholderContext,
    VerificationContract,
)


def _init_repo(repo: Path, *, with_commit: bool = True) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    if with_commit:
        (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _head_sha(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _contract(**verification) -> VerificationContract:
    plugin_like = type("P", (), {})()
    plugin_like.dependency_repos = {}
    plugin_like.verification_envs = verification.pop("_envs", {})
    plugin_like.verification = verification
    plugin_like.work_mode = ""
    contract = VerificationContract.from_plugin(plugin_like)
    assert contract is not None
    return contract


class TestRunCommandBasics:
    def test_successful_command_exit0_with_tails_and_log(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        contract = _contract(commands={"echo": {"run": "python -c \"print('hi')\""}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))
        log_dir = tmp_path / "logs"

        receipt = run_command(
            "echo", contract.commands["echo"], contract, ctx, log_dir=log_dir,
        )

        assert receipt["exit_code"] == 0
        assert "hi" in receipt["stdout_tail"]
        assert receipt["log_path"] is not None
        assert Path(receipt["log_path"]).is_file()
        assert receipt["kind"] == "verification_command"
        assert receipt["parity"] == "absolute"

    def test_failing_command_nonzero_exit(self, tmp_path: Path) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        contract = _contract(commands={"boom": {"run": "python -c \"import sys; sys.exit(3)\""}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("boom", contract.commands["boom"], contract, ctx)

        assert receipt["exit_code"] == 3

    def test_missing_binary_does_not_raise(self, tmp_path: Path) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        contract = _contract(commands={"ghost": {"run": "definitely-not-a-real-binary-xyz"}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("ghost", contract.commands["ghost"], contract, ctx)

        assert receipt["exit_code"] is None
        assert receipt["detail"]


class TestPythonTokenAndCwd:
    def test_python_token_resolves_to_declared_interpreter(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        # Declared env pins a python interpreter; the ``python`` token in argv
        # must resolve to it (here, the running interpreter).
        contract = _contract(
            _envs={"ci": {"python": sys.executable}},
            default_env="ci",
            commands={"ver": {"run": "python -c \"import sys; print(sys.executable)\"", "env": "ci"}},
        )
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("ver", contract.commands["ver"], contract, ctx)

        assert receipt["argv"][0] == sys.executable
        assert receipt["exit_code"] == 0

    def test_default_cwd_is_checkout(self, tmp_path: Path) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        contract = _contract(commands={"pwd": {"run": "python -c \"import os; print(os.getcwd())\""}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("pwd", contract.commands["pwd"], contract, ctx)

        assert receipt["cwd"] == str(checkout)


class TestGitProvenance:
    def test_git_fields_filled_in_repo(self, tmp_path: Path) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        (checkout / "dirty.txt").write_text("x\n", encoding="utf-8")
        contract = _contract(commands={"noop": {"run": "python -c \"pass\""}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("noop", contract.commands["noop"], contract, ctx)

        assert receipt["git"]["checkout_head"] == _head_sha(checkout)
        assert receipt["git"]["changed_files_fingerprint"] is not None

    def test_git_fields_none_outside_repo(self, tmp_path: Path) -> None:
        checkout = tmp_path / "not_a_repo"
        checkout.mkdir()
        contract = _contract(commands={"noop": {"run": "python -c \"pass\""}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("noop", contract.commands["noop"], contract, ctx)

        assert receipt["git"]["checkout_head"] is None
        assert receipt["git"]["changed_files_fingerprint"] is None

    def test_required_differential_has_checkout_and_baseline(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        baseline = _head_sha(checkout)
        contract = _contract(
            commands={"diff": {"run": "python -c \"pass\"", "parity": "differential"}},
        )
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command(
            "diff", contract.commands["diff"], contract, ctx,
            required=True, baseline_head=baseline,
        )

        assert receipt["parity"] == "differential"
        assert receipt["git"]["checkout_head"] == _head_sha(checkout)
        assert receipt["git"]["baseline_head"] == baseline

class TestDependencyProvenance:
    """The receipt payload carries a ``dependencies`` block (schema v2). These
    tests also exercise ``run_command`` end-to-end in a tmp repo, pinning the
    verify path's top-level import of ``capture_dependency_provenance``."""

    def test_receipt_records_referenced_declared_dependency(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        dep = tmp_path / "dep"
        _init_repo(dep)
        (dep / "extra.txt").write_text("x\n", encoding="utf-8")  # make it dirty

        # The command argv references the dependency path (via the placeholder),
        # so depends_on must be True.
        contract = _contract(
            commands={"build": {"run": "python {dependency:shared}/m.py"}},
        )
        ctx = PlaceholderContext(
            checkout=str(checkout), project=str(checkout),
            dependencies={"shared": str(dep)},
        )

        receipt = run_command("build", contract.commands["build"], contract, ctx)

        deps = receipt["dependencies"]
        assert len(deps) == 1
        rec = deps[0]
        assert rec["name"] == "shared"
        assert rec["path"] == str(dep)
        assert rec["head"] == _head_sha(dep)
        assert rec["depends_on"] is True
        assert rec["dirty"] is True
        assert rec["changed_files_count"] == 1
        assert rec["changed_files_fingerprint"] is not None

    def test_declared_but_unreferenced_dependency_is_not_depended_on(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        dep = tmp_path / "dep"
        _init_repo(dep)

        contract = _contract(commands={"noop": {"run": "python -c \"pass\""}})
        ctx = PlaceholderContext(
            checkout=str(checkout), project=str(checkout),
            dependencies={"shared": str(dep)},
        )

        receipt = run_command("noop", contract.commands["noop"], contract, ctx)

        deps = receipt["dependencies"]
        assert len(deps) == 1
        assert deps[0]["depends_on"] is False
        assert deps[0]["head"] == _head_sha(dep)

    def test_no_declared_dependencies_gives_empty_list(
        self, tmp_path: Path,
    ) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        contract = _contract(commands={"noop": {"run": "python -c \"pass\""}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("noop", contract.commands["noop"], contract, ctx)

        assert receipt["dependencies"] == []


class TestGitProvenanceCwd:
    def test_f1_cwd_differs_from_git_subject(self, tmp_path: Path) -> None:
        """F1: subprocess runs in eff_cwd (here the project, NOT the run
        worktree) but git provenance is attributed to ctx.checkout."""
        checkout = tmp_path / "worktree"   # the run worktree = git subject
        _init_repo(checkout)
        (checkout / "wt_change.txt").write_text("only in worktree\n", encoding="utf-8")

        project = tmp_path / "canonical"   # a DIFFERENT dir used as eff_cwd
        project.mkdir()

        contract = _contract(
            _envs={"proj": {"cwd": "{project}"}},
            default_env="proj",
            commands={"where": {"run": "python -c \"import os; print(os.getcwd())\"", "env": "proj"}},
        )
        ctx = PlaceholderContext(checkout=str(checkout), project=str(project))

        receipt = run_command("where", contract.commands["where"], contract, ctx)

        # receipt.cwd is the declared env cwd (the project), and the subprocess
        # actually ran there.
        assert receipt["cwd"] == str(project)
        assert str(project) in receipt["stdout_tail"]
        # ...but git provenance is taken from the run worktree (ctx.checkout).
        assert receipt["git"]["checkout_head"] == _head_sha(checkout)
        assert receipt["git"]["changed_files_fingerprint"] is not None
        assert receipt["placeholders"]["checkout"] == str(checkout)
        assert receipt["placeholders"]["project"] == str(project)
