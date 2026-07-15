"""Unit tests for sdk.verify's fail-closed physical-subject resolution.

Pin the load-bearing invariant: the contract is loaded from the *canonical*
project (``{project}``), but declared commands resolve and execute against the
*run worktree* (``{checkout}``) when recorded by the run. The canonical project
is a subject only when metadata explicitly records ``isolation='off'``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sdk.verify import VerifyEnvError, verify_env, verify_list, verify_run

# A plugin declaring commands that echo {checkout} / {project}, a python cwd
# probe, and a required differential command. ``env`` defaults cwd to {checkout}.
_PLUGIN = '''\
PLUGIN = {
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "required": ["req"],
        "commands": {
            "echo_co": {"run": "echo {checkout}"},
            "echo_proj": {"run": "echo {project}"},
            "show_cwd": {"run": "python -c \\"import os;print(os.getcwd())\\""},
            "req": {"run": "python -c \\"pass\\"", "parity": "differential"},
        },
    },
}
'''

_MANUAL_PLUGIN = '''\
PLUGIN = {
    "verification_envs": {"ci": {}},
    "verification": {
        "default_env": "ci",
        "commands": {
            "auto": {"run": "python -c \\"print('auto')\\""},
            "manual": {"run": "python -c \\"print('manual')\\""},
            "operator_e2e": {"run": "python -c \\"print('e2e')\\""},
        },
        "gate_sets": {
            "base": {"commands": ["auto"]},
            "manuals": {"commands": ["manual"]},
            "e2e": {"commands": ["operator_e2e"]},
        },
        "selection": [
            {"always": ["base"]},
            {"operator": ["e2e"]},
        ],
        "schedule": [
            {"after_phase": "implement", "gate_sets": ["base"]},
            {"manual_only": True, "gate_sets": ["manuals"]},
            {"manual_only": True, "gate_sets": ["e2e"]},
        ],
    },
}
'''


def _write_project(root: Path) -> Path:
    project = root / "project"
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_PLUGIN, encoding="utf-8")
    return project


def _write_manual_project(root: Path) -> Path:
    project = root / "manual_project"
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_MANUAL_PLUGIN, encoding="utf-8")
    return project


def _init_repo(repo: Path) -> None:
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


def _head_sha(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _write_meta_run(
    runs_dir: Path, run_id: str, *, project: Path, worktree: dict | None,
) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    meta: dict = {"task": "t", "status": "done", "project": str(project)}
    meta["worktree"] = worktree if worktree is not None else {"isolation": "off"}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch) -> Path:
    rd = tmp_path / "runs"
    rd.mkdir()
    monkeypatch.setenv("ORCHO_RUNSPACE", str(tmp_path))
    return rd


class TestVerifyList:
    def test_lists_commands_with_resolved_text_and_required_flag(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree),
                      "base_ref": "abc123"},
        )

        result = verify_list(project=str(project), run_id="20260101_000000")

        by_name = {c["name"]: c for c in result.commands}
        # {checkout} resolves to the worktree, {project} to the canonical dir.
        assert by_name["echo_co"]["run_resolved"] == f"echo {worktree}"
        assert by_name["echo_proj"]["run_resolved"] == f"echo {project}"
        assert by_name["req"]["required"] is True
        assert by_name["echo_proj"]["required"] is False
        # Nothing executed: no command-receipt dir created.
        from pipeline.evidence.verification_receipt import COMMAND_RECEIPTS_DIRNAME
        assert not (runs_dir / "20260101_000000" / COMMAND_RECEIPTS_DIRNAME).exists()

    def test_fallback_checkout_is_project_without_worktree(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )
        result = verify_list(project=str(project), run_id="20260101_000000")
        by_name = {c["name"]: c for c in result.commands}
        assert by_name["echo_co"]["run_resolved"] == f"echo {project}"

    def test_no_commands_raises(self, tmp_path: Path, runs_dir: Path) -> None:
        project = tmp_path / "empty"
        (project / ".orcho" / "multiagent").mkdir(parents=True)
        (project / ".orcho" / "multiagent" / "plugin.py").write_text(
            "PLUGIN = {}\n", encoding="utf-8",
        )
        _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )
        with pytest.raises(VerifyEnvError):
            verify_list(project=str(project), run_id="20260101_000000")


class TestVerificationSubjectResolution:
    def test_correction_child_inherits_retained_parent_subject(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "retained"
        _init_repo(worktree)
        baseline = _head_sha(worktree)
        parent_dir = _write_meta_run(
            runs_dir, "parent", project=project,
            worktree={
                "isolation": "worktree", "path": str(worktree),
                "base_ref": baseline,
            },
        )
        child_dir = runs_dir / "child"
        child_dir.mkdir()
        (child_dir / "meta.json").write_text(json.dumps({
            "task": "fix", "status": "running", "project": str(project),
            "profile": "correction", "resume_mode": "followup",
            "parent_run_id": "parent", "parent_run_dir": str(parent_dir),
        }), encoding="utf-8")

        env = verify_env(project=str(project), env="ci", run_id="child")
        listed = verify_list(project=str(project), run_id="child")
        ran = verify_run(
            project=str(project), run_id="child", commands=["echo_co"],
        )

        assert env.subject["checkout"] == str(worktree.resolve())
        assert listed.subject_checkout == ran.subject_checkout == str(worktree.resolve())
        assert ran.outcomes[0].stdout_tail.strip() == str(worktree.resolve())
        assert ran.outcomes[0].baseline_head == baseline

    @pytest.mark.parametrize(
        ("child_update", "message"),
        [
            ({"parent_run_id": "child"}, "cycle"),
            ({"parent_run_id": "missing"}, "unavailable"),
        ],
    )
    def test_correction_child_invalid_lineage_fails_closed(
        self, tmp_path: Path, runs_dir: Path, child_update: dict, message: str,
    ) -> None:
        project = _write_project(tmp_path)
        child_dir = runs_dir / "child"
        child_dir.mkdir()
        meta = {
            "task": "fix", "status": "running", "project": str(project),
            "profile": "correction", "resume_mode": "followup",
            **child_update,
        }
        (child_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with pytest.raises(VerifyEnvError, match=message):
            verify_list(project=str(project), run_id="child")

    @pytest.mark.parametrize(
        ("case", "message"),
        [
            ("missing_id", "missing parent_run_id"),
            ("invalid_id", "parent_run_id is invalid"),
            ("conflicting_dir", "parent_run_dir conflicts"),
            ("mismatched_project", "parent project does not match"),
        ],
    )
    def test_correction_child_rejects_untrusted_parent_identity(
        self, tmp_path: Path, runs_dir: Path, case: str, message: str,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "retained"
        worktree.mkdir()
        parent_dir = _write_meta_run(
            runs_dir, "parent", project=project,
            worktree={"isolation": "worktree", "path": str(worktree)},
        )
        child_dir = runs_dir / "child"
        child_dir.mkdir()
        child_meta = {
            "task": "fix", "status": "running", "project": str(project),
            "profile": "correction", "resume_mode": "followup",
            "parent_run_id": "parent", "parent_run_dir": str(parent_dir),
        }
        if case == "missing_id":
            child_meta.pop("parent_run_id")
        elif case == "invalid_id":
            child_meta["parent_run_id"] = "../parent"
        elif case == "conflicting_dir":
            child_meta["parent_run_dir"] = str(runs_dir / "other")
        else:
            parent_meta_path = parent_dir / "meta.json"
            parent_meta = json.loads(parent_meta_path.read_text(encoding="utf-8"))
            parent_meta["project"] = str(tmp_path / "other-project")
            parent_meta_path.write_text(json.dumps(parent_meta), encoding="utf-8")
        (child_dir / "meta.json").write_text(
            json.dumps(child_meta), encoding="utf-8",
        )

        with pytest.raises(VerifyEnvError, match=message):
            verify_list(project=str(project), run_id="child")

    def test_correction_child_normalizes_parent_meta_read_failure(
        self, tmp_path: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = _write_project(tmp_path)
        parent_dir = runs_dir / "parent"
        parent_dir.mkdir()
        (parent_dir / "meta.json").write_text("{}", encoding="utf-8")
        child_dir = runs_dir / "child"
        child_dir.mkdir()
        child_meta = {
            "task": "fix", "status": "running", "project": str(project),
            "profile": "correction", "resume_mode": "followup",
            "parent_run_id": "parent", "parent_run_dir": str(parent_dir),
        }
        (child_dir / "meta.json").write_text(
            json.dumps(child_meta), encoding="utf-8",
        )

        def _load_meta(run_dir: Path) -> dict:
            if run_dir.name == "child":
                return child_meta
            raise OSError("unreadable parent")

        monkeypatch.setattr("sdk.verify.load_meta", _load_meta)

        with pytest.raises(VerifyEnvError, match="parent metadata is unavailable"):
            verify_list(project=str(project), run_id="child")

    def test_all_entry_points_share_recorded_worktree_and_source(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "retained"
        worktree.mkdir()
        _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree)},
        )

        env = verify_env(project=str(project), env="ci", run_id="20260101_000000")
        listed = verify_list(project=str(project), run_id="20260101_000000")
        ran = verify_run(
            project=str(project), run_id="20260101_000000", commands=["echo_co"],
        )

        assert env.subject["checkout"] == str(worktree.resolve())
        assert env.subject["source"] == "run_metadata"
        assert listed.subject_checkout == ran.subject_checkout == str(worktree.resolve())
        assert listed.subject_source == ran.subject_source == "run_metadata"
        assert ran.outcomes[0].stdout_tail.strip() == str(worktree.resolve())

    @pytest.mark.parametrize("worktree", [
        {"isolation": "worktree", "path": "/missing"},
        {"isolation": "worktree"},
    ])
    def test_isolated_missing_subject_fails_before_receipts(
        self, tmp_path: Path, runs_dir: Path, worktree: dict,
    ) -> None:
        from pipeline.evidence.verification_receipt import (
            COMMAND_RECEIPTS_DIRNAME,
            ENV_RECEIPTS_DIRNAME,
        )

        project = _write_project(tmp_path)
        run_dir = _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=worktree,
        )
        for call in (
            lambda: verify_env(project=str(project), env="ci", run_id="20260101_000000"),
            lambda: verify_list(project=str(project), run_id="20260101_000000"),
            lambda: verify_run(
                project=str(project), run_id="20260101_000000", commands=["echo_co"],
            ),
        ):
            with pytest.raises(VerifyEnvError):
                call()
        assert not (run_dir / ENV_RECEIPTS_DIRNAME).exists()
        assert not (run_dir / COMMAND_RECEIPTS_DIRNAME).exists()

    def test_ambiguous_metadata_requires_noncanonical_override(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "controller-subject"
        worktree.mkdir()
        run_dir = _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )
        meta_path = run_dir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("worktree")
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        with pytest.raises(VerifyEnvError, match="does not establish"):
            verify_list(project=str(project), run_id="20260101_000000")
        result = verify_env(
            project=str(project), run_id="20260101_000000",
            env="ci",
            subject_checkout=str(worktree),
        )
        assert result.subject["checkout"] == str(worktree.resolve())
        assert result.subject["source"] == "controller_override"

    def test_conflicting_or_canonical_override_is_rejected(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "retained"
        other = tmp_path / "other"
        worktree.mkdir()
        other.mkdir()
        _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree)},
        )
        with pytest.raises(VerifyEnvError, match="conflicts"):
            verify_run(
                project=str(project), run_id="20260101_000000",
                commands=["echo_co"],
                subject_checkout=str(other),
            )

    def test_unreadable_isolated_subject_is_rejected(
        self, tmp_path: Path, runs_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "retained"
        worktree.mkdir()
        _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree)},
        )
        import sdk.verify as verify_module

        real = verify_module._is_readable_directory
        monkeypatch.setattr(
            verify_module,
            "_is_readable_directory",
            lambda path: False if path == worktree else real(path),
        )
        with pytest.raises(VerifyEnvError, match="recorded isolated checkout"):
            verify_list(project=str(project), run_id="20260101_000000")


class TestVerifyRun:
    def test_no_env_param(self) -> None:
        import inspect

        sig = inspect.signature(verify_run)
        assert "env" not in sig.parameters

    def test_executes_in_worktree_with_git_provenance(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "wt"
        _init_repo(worktree)
        (worktree / "dirty.txt").write_text("x\n", encoding="utf-8")
        run_dir = _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree),
                      "base_ref": "base-sha-xyz"},
        )

        result = verify_run(
            project=str(project), run_id="20260101_000000",
            commands=["show_cwd"],
        )

        outcome = result.outcomes[0]
        # The subprocess ran IN the worktree (default cwd = {checkout}).
        assert str(worktree) in outcome.stdout_tail
        # Git provenance is the worktree HEAD, not the canonical project.
        assert outcome.checkout_head == _head_sha(worktree)
        assert outcome.receipt_path is not None
        assert Path(outcome.receipt_path).is_file()
        assert str(run_dir) in str(outcome.receipt_path)

    def test_subject_checkout_override_pins_execution_when_metadata_is_ambiguous(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        """When ``meta['worktree']`` is absent, an explicit ``subject_checkout``
        pins execution + git provenance to the real worktree, instead of silently
        falling back to the canonical project (which would prove the wrong
        checkout)."""
        project = _write_project(tmp_path)
        worktree = tmp_path / "wt"
        _init_repo(worktree)
        run_dir = _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )
        meta_path = run_dir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("worktree")
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        result = verify_run(
            project=str(project), run_id="20260101_000000",
            commands=["show_cwd"], subject_checkout=str(worktree),
        )

        outcome = result.outcomes[0]
        # Ran IN the worktree, not the canonical project.
        assert str(worktree) in outcome.stdout_tail
        assert str(project) not in outcome.stdout_tail
        assert outcome.checkout_head == _head_sha(worktree)

    def test_subject_checkout_override_rejects_non_directory(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        """An invalid controller override never falls back to another subject."""
        project = _write_project(tmp_path)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )

        with pytest.raises(VerifyEnvError, match="subject_checkout override"):
            verify_run(
                project=str(project), run_id="20260101_000000",
                commands=["show_cwd"], subject_checkout=str(tmp_path / "does-not-exist"),
            )

    def test_required_only_passes_baseline_and_checkout(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        worktree = tmp_path / "wt"
        _init_repo(worktree)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree),
                      "base_ref": "base-sha-xyz"},
        )

        result = verify_run(
            project=str(project), run_id="20260101_000000", required_only=True,
        )

        assert [o.command for o in result.outcomes] == ["req"]
        outcome = result.outcomes[0]
        assert outcome.parity == "differential"
        assert outcome.checkout_head == _head_sha(worktree)
        assert outcome.baseline_head == "base-sha-xyz"
        assert result.all_passed is True

    def test_default_run_excludes_manual_and_operator_only_commands(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_manual_project(tmp_path)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )

        result = verify_run(project=str(project), run_id="20260101_000000")

        assert [o.command for o in result.outcomes] == ["auto"]

    def test_include_manual_runs_full_declared_command_sweep(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_manual_project(tmp_path)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )

        result = verify_run(
            project=str(project),
            run_id="20260101_000000",
            include_manual=True,
        )

        assert [o.command for o in result.outcomes] == [
            "auto",
            "manual",
            "operator_e2e",
        ]

    def test_explicit_manual_command_runs_without_include_manual(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_manual_project(tmp_path)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )

        result = verify_run(
            project=str(project),
            run_id="20260101_000000",
            commands=["operator_e2e"],
        )

        assert [o.command for o in result.outcomes] == ["operator_e2e"]

    def test_fallback_to_project_dir_without_worktree(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        project = _write_project(tmp_path)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )

        result = verify_run(
            project=str(project), run_id="20260101_000000",
            commands=["show_cwd"],
        )

        assert str(project) in result.outcomes[0].stdout_tail

    def test_unknown_command_raises_before_write(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        from pipeline.evidence.verification_receipt import COMMAND_RECEIPTS_DIRNAME

        project = _write_project(tmp_path)
        run_dir = _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )
        with pytest.raises(VerifyEnvError):
            verify_run(
                project=str(project), run_id="20260101_000000",
                commands=["ghost"],
            )
        assert not (run_dir / COMMAND_RECEIPTS_DIRNAME).exists()

    def test_empty_required_raises(self, tmp_path: Path, runs_dir: Path) -> None:
        project = tmp_path / "noreq"
        plugin_dir = project / ".orcho" / "multiagent"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.py").write_text(
            'PLUGIN = {"verification": {"commands": {"a": "python -c \\"pass\\""}}}\n',
            encoding="utf-8",
        )
        _write_meta_run(
            runs_dir, "20260101_000000", project=project, worktree=None,
        )
        with pytest.raises(VerifyEnvError):
            verify_run(
                project=str(project), run_id="20260101_000000",
                required_only=True,
            )


class TestManualOrOperatorOnlyCommands:
    """The raw public helper keeps a required+manual command in the set; the
    private ``verify run`` filter still subtracts ``required``/automatic."""

    @staticmethod
    def _contract():
        from pipeline.plugins import PluginConfig
        from pipeline.verification_contract import VerificationContract

        plugin = PluginConfig(
            verification_envs={"ci": {}},
            verification={
                "default_env": "ci",
                # ``req_manual`` is BOTH required AND parked behind a
                # ``manual_only`` schedule — the case the raw helper exists for.
                "required": ["req_manual"],
                "commands": {
                    "auto": {"run": "python -c \"pass\""},
                    "gated_manual": {"run": "python -c \"pass\""},
                    "req_manual": {"run": "python -c \"pass\""},
                },
                "gate_sets": {
                    "base": {"commands": ["auto"]},
                    "manuals": {"commands": ["gated_manual", "req_manual"]},
                },
                "selection": [{"always": ["base"]}],
                "schedule": [
                    {"after_phase": "implement", "gate_sets": ["base"]},
                    {"manual_only": True, "gate_sets": ["manuals"]},
                ],
            },
        )
        contract = VerificationContract.from_plugin(plugin)
        assert contract is not None
        return contract

    def test_raw_helper_keeps_required_manual_command(self) -> None:
        from sdk.verify import manual_or_operator_only_commands

        raw = manual_or_operator_only_commands(self._contract())

        # Raw set is NOT reduced by required/automatic: a required command that
        # is also manual_only stays in the manual set so auto-run skips it.
        assert raw == {"gated_manual", "req_manual"}

    def test_private_filter_still_subtracts_required(self) -> None:
        from sdk.verify import _manual_or_operator_only_commands

        excluded = _manual_or_operator_only_commands(self._contract())

        # ``verify run`` behavior is byte-identical: the required command is
        # subtracted back out and only the pure-manual command is excluded.
        assert excluded == {"gated_manual"}


def _write_dep_project(root: Path, dep_path: Path) -> Path:
    """Project whose contract declares a ``shared`` dependency repo and two
    commands: ``dep_cmd`` references it (depends_on), ``no_dep`` does not."""
    project = root / "project"
    plugin_dir = project / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    plugin = (
        "PLUGIN = {\n"
        '    "dependency_repos": {"shared": {"path": '
        + repr(str(dep_path)) + "}},\n"
        '    "verification_envs": {"ci": {}},\n'
        '    "verification": {\n'
        '        "default_env": "ci",\n'
        '        "commands": {\n'
        '            "dep_cmd": {"run": "echo {dependency:shared}"},\n'
        '            "no_dep": {"run": "echo {checkout}"},\n'
        "        },\n"
        "    },\n"
        "}\n"
    )
    (plugin_dir / "plugin.py").write_text(plugin, encoding="utf-8")
    return project


class TestVerifyRunDependencyTags:
    def test_depended_command_carries_dependency_tag(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        dep_head = _head_sha(dep)
        project = _write_dep_project(tmp_path, dep)
        worktree = tmp_path / "wt"
        _init_repo(worktree)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree), "base_ref": "b"},
        )

        result = verify_run(
            project=str(project), run_id="20260101_000000",
            commands=["dep_cmd"],
        )

        outcome = result.outcomes[0]
        assert outcome.dependencies == (f"shared@{dep_head[:7]}",)

    def test_unreferenced_command_has_no_dependency_tag(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        project = _write_dep_project(tmp_path, dep)
        worktree = tmp_path / "wt"
        _init_repo(worktree)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree), "base_ref": "b"},
        )

        result = verify_run(
            project=str(project), run_id="20260101_000000",
            commands=["no_dep"],
        )

        assert result.outcomes[0].dependencies == ()

    def test_dirty_dependency_tag_marked(
        self, tmp_path: Path, runs_dir: Path,
    ) -> None:
        dep = tmp_path / "dep"
        _init_repo(dep)
        dep_head = _head_sha(dep)
        (dep / "uncommitted.txt").write_text("x\n", encoding="utf-8")
        project = _write_dep_project(tmp_path, dep)
        worktree = tmp_path / "wt"
        _init_repo(worktree)
        _write_meta_run(
            runs_dir, "20260101_000000", project=project,
            worktree={"isolation": "worktree", "path": str(worktree), "base_ref": "b"},
        )

        result = verify_run(
            project=str(project), run_id="20260101_000000",
            commands=["dep_cmd"],
        )

        assert result.outcomes[0].dependencies == (
            f"shared@{dep_head[:7]}+dirty",
        )
