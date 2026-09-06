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

    def test_empty_command_receipt_shape(self, tmp_path: Path) -> None:
        # C5: an empty ``run`` declaration produces no argv, so _execute short
        # -circuits to the ``empty`` outcome with no exit code and empty tails.
        checkout = tmp_path / "co"
        _init_repo(checkout)
        contract = _contract(commands={"nothing": {"run": ""}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("nothing", contract.commands["nothing"], contract, ctx)

        assert receipt["outcome"] == "empty"
        assert receipt["exit_code"] is None
        assert receipt["stdout_tail"] == ""
        assert receipt["stderr_tail"] == ""
        assert receipt["detail"] == "empty command (nothing to run)"

    def test_missing_binary_does_not_raise(self, tmp_path: Path) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        contract = _contract(commands={"ghost": {"run": "definitely-not-a-real-binary-xyz"}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("ghost", contract.commands["ghost"], contract, ctx)

        assert receipt["exit_code"] is None
        assert receipt["detail"]


class TestCommandTimeout:
    """The declared per-command budget is what the subprocess actually gets.

    Real sleep, real ceiling: the hang path is exactly the one that produced a
    ``duration_s`` pinned to the ceiling with an empty stdout, so the test
    asserts the receipt shape an operator reads (``exit_code=None`` + a detail
    naming the effective budget), not just the call argument.
    """

    def test_declared_timeout_bounds_a_hanging_command(self, tmp_path: Path) -> None:
        checkout = tmp_path / "co"
        _init_repo(checkout)
        contract = _contract(commands={
            "hang": {"run": "python -c \"import time; time.sleep(30)\"", "timeout": 1},
        })
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("hang", contract.commands["hang"], contract, ctx)

        assert receipt["exit_code"] is None
        assert "timed out after 1s" in receipt["detail"]
        assert receipt["duration_s"] < 30

    def test_engine_default_applies_without_a_declaration(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from pipeline import verification_command

        checkout = tmp_path / "co"
        _init_repo(checkout)
        monkeypatch.setattr(verification_command, "_DEFAULT_TIMEOUT_S", 1)
        contract = _contract(commands={
            "hang": {"run": "python -c \"import time; time.sleep(30)\""},
        })
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("hang", contract.commands["hang"], contract, ctx)

        assert receipt["exit_code"] is None
        assert "timed out after 1s" in receipt["detail"]

    def test_timeout_preserves_flushed_stdout_and_stderr(
        self, tmp_path: Path,
    ) -> None:
        """C1/C2/C4: a child that flushes distinct stdout/stderr markers before
        exceeding a bounded timeout yields both markers in the respective
        receipt tails and in the on-disk log, while outcome stays ``timeout``
        and exit_code stays ``None`` (even when the output contains 'passed')."""
        checkout = tmp_path / "co"
        _init_repo(checkout)
        # The child writes a distinct marker to each stream, flushes both, then
        # sleeps well past the declared timeout so the kill happens after the
        # bytes are captured. 'passed' is embedded to prove it never flips the
        # execution outcome.
        child = (
            "import sys, time; "
            "sys.stdout.write('OUTMARKER passed\\n'); sys.stdout.flush(); "
            "sys.stderr.write('ERRMARKER\\n'); sys.stderr.flush(); "
            "time.sleep(30)"
        )
        contract = _contract(commands={
            "hang": {"run": ["python", "-c", child], "timeout": 1},
        })
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))
        log_dir = tmp_path / "logs"

        receipt = run_command(
            "hang", contract.commands["hang"], contract, ctx, log_dir=log_dir,
        )

        assert receipt["outcome"] == "timeout"
        assert receipt["exit_code"] is None
        assert receipt["duration_s"] < 30
        assert "OUTMARKER passed" in receipt["stdout_tail"]
        assert "ERRMARKER" in receipt["stderr_tail"]

        # Read the saved log back from disk: both markers present and stdout vs
        # stderr remain distinguishable via the stderr divider.
        log_text = Path(receipt["log_path"]).read_text(encoding="utf-8")
        assert "OUTMARKER passed" in log_text
        assert "ERRMARKER" in log_text
        assert "--- stderr ---" in log_text
        assert log_text.index("OUTMARKER") < log_text.index("--- stderr ---")
        assert log_text.index("--- stderr ---") < log_text.index("ERRMARKER")

    def test_timeout_tail_is_bounded_but_log_retains_full_output(
        self, tmp_path: Path,
    ) -> None:
        """C2: a short ``tail_chars`` bounds the receipt tail while the on-disk
        log keeps the full captured output."""
        checkout = tmp_path / "co"
        _init_repo(checkout)
        child = (
            "import sys, time; "
            "sys.stdout.write('A' * 500 + 'ZEND\\n'); sys.stdout.flush(); "
            "time.sleep(30)"
        )
        contract = _contract(commands={
            "hang": {"run": ["python", "-c", child], "timeout": 1},
        })
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))
        log_dir = tmp_path / "logs"

        receipt = run_command(
            "hang", contract.commands["hang"], contract, ctx,
            log_dir=log_dir, tail_chars=10,
        )

        assert receipt["outcome"] == "timeout"
        assert receipt["exit_code"] is None
        # The tail is bounded to the trailing 10 chars (the end of the marker).
        assert len(receipt["stdout_tail"]) == 10
        assert "ZEND" in receipt["stdout_tail"]
        # The full output survives on disk, tail bounding notwithstanding.
        log_text = Path(receipt["log_path"]).read_text(encoding="utf-8")
        assert "A" * 500 + "ZEND" in log_text


class TestStreamCoercion:
    """C3: normalisation of TimeoutExpired-carried stdout/stderr shapes must
    never crash receipt/log construction and must not repr-wrap bytes.

    These use a fake subprocess boundary (monkeypatched ``subprocess.run``) so
    the exact captured-stream shape is controlled; only the normalisation is
    under test here, not the real kill path."""

    def _run_with_timeout_streams(
        self, tmp_path, monkeypatch, *, stdout, stderr,
    ):
        from pipeline import verification_command

        checkout = tmp_path / "co"
        _init_repo(checkout)

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd=args[0] if args else kwargs.get("args"),
                timeout=1,
                output=stdout,
                stderr=stderr,
            )

        monkeypatch.setattr(verification_command.subprocess, "run", fake_run)
        contract = _contract(commands={"c": {"run": "python -c \"pass\"", "timeout": 1}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))
        log_dir = tmp_path / "logs"
        return run_command(
            "c", contract.commands["c"], contract, ctx, log_dir=log_dir,
        )

    def test_bytes_streams_are_decoded(self, tmp_path, monkeypatch) -> None:
        receipt = self._run_with_timeout_streams(
            tmp_path, monkeypatch,
            stdout=b"hello out", stderr=b"hello err",
        )
        assert receipt["outcome"] == "timeout"
        assert receipt["exit_code"] is None
        assert "hello out" in receipt["stdout_tail"]
        assert "hello err" in receipt["stderr_tail"]
        log_text = Path(receipt["log_path"]).read_text(encoding="utf-8")
        assert "b'" not in log_text

    def test_str_streams_pass_through(self, tmp_path, monkeypatch) -> None:
        receipt = self._run_with_timeout_streams(
            tmp_path, monkeypatch,
            stdout="str out", stderr="str err",
        )
        assert "str out" in receipt["stdout_tail"]
        assert "str err" in receipt["stderr_tail"]

    def test_none_streams_become_empty(self, tmp_path, monkeypatch) -> None:
        receipt = self._run_with_timeout_streams(
            tmp_path, monkeypatch, stdout=None, stderr=None,
        )
        assert receipt["outcome"] == "timeout"
        assert receipt["exit_code"] is None
        assert receipt["stdout_tail"] == ""
        assert receipt["stderr_tail"] == ""

    def test_invalid_encoded_bytes_do_not_crash_or_repr_wrap(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Incomplete/invalid utf-8 sequences must decode lossily, not raise.
        receipt = self._run_with_timeout_streams(
            tmp_path, monkeypatch,
            stdout=b"valid\xff\xfe tail", stderr=b"\x80\x81",
        )
        assert receipt["outcome"] == "timeout"
        assert receipt["exit_code"] is None
        assert "valid" in receipt["stdout_tail"]
        # No repr-style wrapping leaked into text.
        assert "b'" not in receipt["stdout_tail"]
        assert "b'" not in receipt["stderr_tail"]
        log_text = Path(receipt["log_path"]).read_text(encoding="utf-8")
        assert "b'" not in log_text


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
        assert receipt["subject"].identity.tree_oid is not None

    def test_git_fields_none_outside_repo(self, tmp_path: Path) -> None:
        checkout = tmp_path / "not_a_repo"
        checkout.mkdir()
        contract = _contract(commands={"noop": {"run": "python -c \"pass\""}})
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))

        receipt = run_command("noop", contract.commands["noop"], contract, ctx)

        assert receipt["git"]["checkout_head"] is None
        assert receipt["subject"].reason == "git_repository_unavailable"

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
        assert rec["subject"].identity.tree_oid is not None
        assert rec["subject"].identity.observed_head_oid == _head_sha(dep)

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
        assert receipt["subject"].identity.tree_oid is not None
        assert receipt["placeholders"]["checkout"] == str(checkout)
        assert receipt["placeholders"]["project"] == str(project)
