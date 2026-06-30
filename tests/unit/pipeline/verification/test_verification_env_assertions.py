"""T1 — generic env-assertion engine (:mod:`pipeline.verification_env`).

Covers the five assertion kinds, the unknown-key / no-raise degradation, that
placeholders are applied, and the load-bearing default cwd: with no declared
``cwd``, import/version run from ``ctx.checkout`` (fallback ``ctx.project``),
independent of the process cwd.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pipeline.verification_contract import PlaceholderContext
from pipeline.verification_env import run_env_assertions


def _ctx(checkout: str = "", project: str = "") -> PlaceholderContext:
    return PlaceholderContext(checkout=checkout, project=project)


def _make_package(root: Path, name: str = "pkg_under_test") -> Path:
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VALUE = 1\n")
    return pkg


def _only(result: dict) -> dict:
    assert len(result["assertions"]) == 1
    return result["assertions"][0]


class TestImportAssertions:
    def test_import_path_equals_pass(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        spec = {
            "cwd": str(tmp_path),
            "assertions": [
                {"import": "pkg_under_test", "path_equals": str(pkg / "__init__.py")},
            ],
        }
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["kind"] == "import_path_equals"
        assert a["passed"] is True

    def test_import_path_equals_fail(self, tmp_path: Path) -> None:
        _make_package(tmp_path)
        spec = {
            "cwd": str(tmp_path),
            "assertions": [
                {"import": "pkg_under_test", "path_equals": str(tmp_path / "wrong.py")},
            ],
        }
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["passed"] is False

    def test_import_path_under_pass(self, tmp_path: Path) -> None:
        pkg = _make_package(tmp_path)
        spec = {
            "cwd": str(tmp_path),
            "assertions": [
                {"import": "pkg_under_test", "path_under": str(pkg)},
            ],
        }
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["kind"] == "import_path_under"
        assert a["passed"] is True

    def test_import_without_path_clause_is_failed_not_crash(
        self, tmp_path: Path,
    ) -> None:
        spec = {"cwd": str(tmp_path), "assertions": [{"import": "os"}]}
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["kind"] == "unknown"
        assert a["passed"] is False


class TestPathAssertions:
    def test_path_exists_relative_to_eff_cwd(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        spec = {"cwd": str(tmp_path), "assertions": [{"path_exists": "data"}]}
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["passed"] is True

    def test_file_exists_dir_is_not_file(self, tmp_path: Path) -> None:
        (tmp_path / "data").mkdir()
        spec = {"cwd": str(tmp_path), "assertions": [{"file_exists": "data"}]}
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["kind"] == "file_exists"
        assert a["passed"] is False

    def test_file_exists_pass(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("x")
        spec = {"cwd": str(tmp_path), "assertions": [{"file_exists": "f.txt"}]}
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["passed"] is True


class TestCommandExists:
    def test_known_interpreter_command(self, tmp_path: Path) -> None:
        spec = {"cwd": str(tmp_path), "assertions": [{"command_exists": "python3"}]}
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["kind"] == "command_exists"
        # python3 is on PATH in the test environment.
        assert a["passed"] is True

    def test_missing_command_is_failed(self, tmp_path: Path) -> None:
        spec = {
            "cwd": str(tmp_path),
            "assertions": [{"command_exists": "definitely-not-a-real-binary-xyz"}],
        }
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["passed"] is False


class TestVersionAssertion:
    def test_version_contains_pass(self, tmp_path: Path) -> None:
        major = str(sys.version_info[0])
        spec = {
            "cwd": str(tmp_path),
            "assertions": [
                {"version": [sys.executable, "--version"], "contains": major},
            ],
        }
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["kind"] == "version_contains"
        assert a["passed"] is True

    def test_version_contains_fail(self, tmp_path: Path) -> None:
        spec = {
            "cwd": str(tmp_path),
            "assertions": [
                {"version": [sys.executable, "--version"], "contains": "ZZ-nope"},
            ],
        }
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["passed"] is False


class TestDegradation:
    def test_unknown_key_is_failed_not_crash(self, tmp_path: Path) -> None:
        spec = {"cwd": str(tmp_path), "assertions": [{"wat": "huh"}]}
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["kind"] == "unknown"
        assert a["passed"] is False

    def test_broken_interpreter_does_not_raise(self, tmp_path: Path) -> None:
        spec = {
            "python": str(tmp_path / "no-such-python"),
            "cwd": str(tmp_path),
            "assertions": [
                {"import": "os", "path_under": str(tmp_path)},
            ],
        }
        # Must not raise even though the interpreter does not exist.
        a = _only(run_env_assertions("ci", spec, _ctx()))
        assert a["passed"] is False
        assert a["detail"]


class TestPlaceholders:
    def test_placeholders_applied_to_cwd_and_paths(self, tmp_path: Path) -> None:
        (tmp_path / "marker").write_text("x")
        ctx = PlaceholderContext(checkout=str(tmp_path), project=str(tmp_path))
        spec = {
            "cwd": "{checkout}",
            "assertions": [{"file_exists": "marker"}],
        }
        result = run_env_assertions("ci", spec, ctx)
        assert result["cwd"] == str(tmp_path)
        assert _only(result)["passed"] is True


class TestResultShape:
    def test_subject_and_overrides(self, tmp_path: Path) -> None:
        ctx = PlaceholderContext(checkout=str(tmp_path), project=str(tmp_path))
        spec = {
            "cwd": str(tmp_path),
            "env": {"FOO": "{project}"},
            "assertions": [{"path_exists": "."}],
        }
        result = run_env_assertions("ci", spec, ctx)
        assert result["subject"] == {
            "env": "ci",
            "checkout": str(tmp_path),
            "project": str(tmp_path),
        }
        assert result["env_overrides"] == {"FOO": str(tmp_path)}
        assert result["all_passed"] is True
        assert result["interpreter"]


class TestDefaultCwdIsDeclaredCheckout:
    """The load-bearing rule: no declared cwd ⇒ run from ctx.checkout, never the
    process cwd. Proven by changing the process cwd to an unrelated dir and
    importing a package that only exists under ctx.checkout."""

    def test_default_cwd_uses_checkout_not_process_cwd(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        pkg = _make_package(checkout, "proj_pkg")

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))
        spec = {  # NOTE: no "cwd" key — effective cwd must default to checkout.
            "assertions": [
                {"import": "proj_pkg", "path_equals": str(pkg / "__init__.py")},
            ],
        }
        result = run_env_assertions("ci", spec, ctx)
        assert result["cwd"] == str(checkout)
        assert _only(result)["passed"] is True

    def test_default_cwd_falls_back_to_project(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        pkg = _make_package(project, "proj_pkg")
        monkeypatch.chdir(tmp_path)

        # checkout empty ⇒ fall back to project.
        ctx = PlaceholderContext(checkout="", project=str(project))
        spec = {
            "assertions": [
                {"import": "proj_pkg", "path_under": str(pkg)},
            ],
        }
        result = run_env_assertions("ci", spec, ctx)
        assert result["cwd"] == str(project)
        assert _only(result)["passed"] is True

    def test_import_fails_when_only_process_cwd_has_package(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Package lives only under the process cwd, NOT under checkout. If the
        # engine wrongly used the process cwd the import would pass; it must fail.
        proc_cwd = tmp_path / "proc"
        proc_cwd.mkdir()
        _make_package(proc_cwd, "shell_only_pkg")
        monkeypatch.chdir(proc_cwd)

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        ctx = PlaceholderContext(checkout=str(checkout), project=str(checkout))
        spec = {
            "assertions": [
                {"import": "shell_only_pkg", "path_under": str(proc_cwd)},
            ],
        }
        result = run_env_assertions("ci", spec, ctx)
        assert _only(result)["passed"] is False


def test_no_assertions_yields_all_passed_true(tmp_path: Path) -> None:
    spec = {"cwd": str(tmp_path), "assertions": []}
    result = run_env_assertions("ci", spec, _ctx())
    assert result["assertions"] == []
    assert result["all_passed"] is True


def test_absolute_path_exists_ignores_cwd(tmp_path: Path) -> None:
    target = tmp_path / "abs.txt"
    target.write_text("x")
    spec = {
        "cwd": str(tmp_path / "unrelated"),
        "assertions": [{"path_exists": str(target)}],
    }
    a = _only(run_env_assertions("ci", spec, _ctx()))
    assert a["passed"] is True
    assert a["actual"] == os.path.join(str(target))
