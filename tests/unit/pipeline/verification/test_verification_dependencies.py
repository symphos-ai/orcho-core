"""Unit tests for pipeline/verification_dependencies.py (cross-repo provenance).

Real-git fixtures (no mocks): the module's contract is "read-only git provenance
per declared dependency, never raising". Each test pins one guarantee — the
load-bearing ones are (a) ``depends_on`` is a boundary-aware path prefix (not a
bare substring), (b) a non-git dependency degrades to ``head=None`` with all
dirty fields ``None``, and (c) the import direction is fixed: the fingerprint
helper lives here and ``verification_command`` imports *this* object.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.verification_contract import PlaceholderContext
from pipeline.verification_dependencies import (
    capture_dependency_provenance,
    changed_files_fingerprint,
    current_dependency_heads,
    dependency_stale_reason,
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


def _ctx(deps: dict[str, str]) -> PlaceholderContext:
    return PlaceholderContext(dependencies=deps)


class TestDependsOn:
    def test_depends_on_via_argv_token(self, tmp_path: Path) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx,
            argv=["python", str(dep / "tool.py")],
            eff_cwd="",
            python="",
            env_overrides={},
        )

        assert records[0]["depends_on"] is True

    def test_depends_on_via_eff_cwd(self, tmp_path: Path) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx, argv=["echo"], eff_cwd=str(dep), python="", env_overrides={},
        )

        assert records[0]["depends_on"] is True

    def test_depends_on_via_python_interpreter(self, tmp_path: Path) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx,
            argv=["echo"],
            eff_cwd="",
            python=str(dep / ".venv" / "bin" / "python"),
            env_overrides={},
        )

        assert records[0]["depends_on"] is True

    def test_depends_on_via_env_override_value(self, tmp_path: Path) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx,
            argv=["echo"],
            eff_cwd="",
            python="",
            env_overrides={"PYTHONPATH": str(dep / "src")},
        )

        assert records[0]["depends_on"] is True

    def test_substring_without_separator_boundary_is_not_dependency(
        self, tmp_path: Path,
    ) -> None:
        # ``/.../dep`` must NOT count as a prefix of ``/.../department``.
        dep = tmp_path / "dep"
        _init_repo(dep)
        sibling = tmp_path / "department"
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx,
            argv=["python", str(sibling / "tool.py")],
            eff_cwd=str(sibling),
            python="",
            env_overrides={},
        )

        assert records[0]["depends_on"] is False

    def test_unrelated_command_is_not_dependency(self, tmp_path: Path) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx, argv=["ruff", "check", "."], eff_cwd="/elsewhere",
            python="/usr/bin/python3", env_overrides={"FOO": "bar"},
        )

        assert records[0]["depends_on"] is False


class TestCaptureProvenance:
    def test_non_git_path_degrades_to_none_without_raising(
        self, tmp_path: Path,
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        ctx = _ctx({"d": str(plain)})

        records = capture_dependency_provenance(
            ctx, argv=["echo"], eff_cwd="", python="", env_overrides={},
        )

        rec = records[0]
        assert rec["head"] is None
        assert rec["dirty"] is None
        assert rec["changed_files_count"] is None
        assert rec["changed_files_fingerprint"] is None

    def test_dirty_summary_has_no_file_names(self, tmp_path: Path) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        (dep / "secret_dependency_file.txt").write_text("x\n", encoding="utf-8")
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx, argv=["echo"], eff_cwd="", python="", env_overrides={},
        )

        rec = records[0]
        assert rec["head"] == _head_sha(dep)
        assert rec["dirty"] is True
        assert rec["changed_files_count"] == 1
        assert rec["changed_files_fingerprint"] is not None
        # The dependency's file names must never leak into the record.
        assert "secret_dependency_file.txt" not in repr(rec)

    def test_clean_dependency_is_not_dirty(self, tmp_path: Path) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx, argv=["echo"], eff_cwd="", python="", env_overrides={},
        )

        rec = records[0]
        assert rec["dirty"] is False
        assert rec["changed_files_count"] == 0

    def test_rename_provenance_counts_both_identities_and_matches_reader(
        self, tmp_path: Path,
    ) -> None:
        """A renamed dependency is two scope identities, not one display path."""
        from pipeline.verification_readiness import _current_checkout_identity

        dep = tmp_path / "dep"
        _init_repo(dep)
        (dep / "README.md").rename(dep / "renamed.md")
        subprocess.run(["git", "add", "-A"], cwd=dep, check=True)
        ctx = _ctx({"d": str(dep)})

        records = capture_dependency_provenance(
            ctx, argv=["echo"], eff_cwd="", python="", env_overrides={},
        )
        rec = records[0]
        expected_fingerprint = "0d9610d6771424f4"

        assert rec["dirty"] is True
        assert rec["changed_files_count"] == 2
        assert rec["changed_files_fingerprint"] == expected_fingerprint
        assert changed_files_fingerprint(str(dep)) == expected_fingerprint
        assert _current_checkout_identity(str(dep)) == (
            expected_fingerprint, _head_sha(dep),
        )

    def test_records_sorted_by_name(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        _init_repo(a)
        _init_repo(b)
        ctx = _ctx({"zeta": str(b), "alpha": str(a)})

        records = capture_dependency_provenance(
            ctx, argv=["echo"], eff_cwd="", python="", env_overrides={},
        )

        assert [r["name"] for r in records] == ["alpha", "zeta"]


class TestCurrentDependencyHeads:
    def test_none_ctx_returns_empty(self) -> None:
        assert current_dependency_heads(None) == {}

    def test_no_dependencies_returns_empty(self) -> None:
        assert current_dependency_heads(_ctx({})) == {}

    def test_heads_per_dependency(self, tmp_path: Path) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        plain = tmp_path / "plain"
        plain.mkdir()

        heads = current_dependency_heads(
            _ctx({"d": str(dep), "p": str(plain)}),
        )

        assert heads["d"] == _head_sha(dep)
        assert heads["p"] is None


class TestDependencyStaleReason:
    def test_moved_head_on_depended_dependency_is_stale(self) -> None:
        receipt = {
            "dependencies": [
                {"name": "d", "head": "aaaa", "depends_on": True},
            ],
        }
        reason = dependency_stale_reason(receipt, {"d": "bbbb"})
        assert reason == "dependency d HEAD moved aaaa -> bbbb"

    def test_depends_on_false_is_not_stale(self) -> None:
        receipt = {
            "dependencies": [
                {"name": "d", "head": "aaaa", "depends_on": False},
            ],
        }
        assert dependency_stale_reason(receipt, {"d": "bbbb"}) is None

    def test_missing_dependencies_block_is_not_stale(self) -> None:
        assert dependency_stale_reason({}, {"d": "bbbb"}) is None
        assert dependency_stale_reason({"dependencies": "junk"}, {}) is None

    def test_current_head_none_is_not_stale(self) -> None:
        receipt = {
            "dependencies": [
                {"name": "d", "head": "aaaa", "depends_on": True},
            ],
        }
        assert dependency_stale_reason(receipt, {"d": None}) is None
        assert dependency_stale_reason(receipt, {}) is None

    def test_recorded_head_none_is_not_stale(self) -> None:
        receipt = {
            "dependencies": [
                {"name": "d", "head": None, "depends_on": True},
            ],
        }
        assert dependency_stale_reason(receipt, {"d": "bbbb"}) is None

    def test_matching_head_is_not_stale(self) -> None:
        receipt = {
            "dependencies": [
                {"name": "d", "head": "aaaa", "depends_on": True},
            ],
        }
        assert dependency_stale_reason(receipt, {"d": "aaaa"}) is None

    def test_first_moved_depended_dependency_wins(self) -> None:
        receipt = {
            "dependencies": [
                {"name": "skip", "head": "x", "depends_on": False},
                {"name": "d", "head": "aaaa", "depends_on": True},
                {"name": "e", "head": "cccc", "depends_on": True},
            ],
        }
        reason = dependency_stale_reason(
            receipt, {"d": "bbbb", "e": "dddd"},
        )
        assert reason == "dependency d HEAD moved aaaa -> bbbb"


class TestImportDirectionGuard:
    def test_verification_command_imports_fingerprint_from_here(self) -> None:
        """The fingerprint helper has exactly one home (verification_dependencies);
        verification_command re-exposes that same object, never its own copy."""
        import pipeline.verification_command as vc
        import pipeline.verification_dependencies as vd

        assert vc.changed_files_fingerprint is vd.changed_files_fingerprint
        assert vc.changed_files_fingerprint is changed_files_fingerprint
