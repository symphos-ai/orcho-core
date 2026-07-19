from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline.evidence.verification_receipt import subject_identity
from pipeline.verification_contract import PlaceholderContext
from pipeline.verification_dependencies import (
    _is_path_prefix,
    capture_dependency_provenance,
    current_dependency_subjects,
)
from pipeline.verification_failure import classify_receipt


def _repo(path: Path) -> None:
    path.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "x@test"), ("config", "user.name", "x")):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "file").write_text("one", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def test_dependency_subjects_are_sorted_and_normalizable(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _repo(a)
    _repo(b)
    ctx = PlaceholderContext(dependencies={"z": str(b), "a": str(a)})
    records = capture_dependency_provenance(ctx, argv=[str(a / "tool")], eff_cwd="", python="", env_overrides={})
    assert [record["name"] for record in records] == ["a", "z"]
    assert records[0]["depends_on"] is True
    assert subject_identity({"status": "available", "identity": {
        "version": records[0]["subject"].identity.version,
        "object_format": records[0]["subject"].identity.object_format,
        "tree_oid": records[0]["subject"].identity.tree_oid,
        "observed_head_oid": records[0]["subject"].identity.observed_head_oid,
        "baseline_oid": None,
    }}) is not None


def test_dependency_content_drift_and_unavailable_effective_dependency_fail_closed(tmp_path: Path) -> None:
    dep = tmp_path / "dep"
    _repo(dep)
    ctx = PlaceholderContext(dependencies={"dep": str(dep)})
    recorded = capture_dependency_provenance(ctx, argv=[str(dep / "tool")], eff_cwd="", python="", env_overrides={})
    receipt = {"exit_code": 0, "assertions": [], "detail": "", "subject": {"status": "available", "identity": {
        "version": 1, "object_format": "sha1", "tree_oid": "a" * 40, "observed_head_oid": "b" * 40, "baseline_oid": None}}, "dependencies": recorded}
    # Establish a usable matching main subject then mutate only dependency content.
    receipt["subject"] = {"status": "available", "identity": {"version": 1, "object_format": "sha1", "tree_oid": "a" * 40, "observed_head_oid": "b" * 40, "baseline_oid": None}}
    from pipeline.verification_subject import VerificationSubjectIdentity
    main = VerificationSubjectIdentity(1, "sha1", "a" * 40, "b" * 40, None)
    (dep / "file").write_text("two", encoding="utf-8")
    assert classify_receipt(receipt, current_subject=main, dependency_subjects=current_dependency_subjects(ctx)).status == "stale"
    assert classify_receipt(receipt, current_subject=main, dependency_subjects={"dep": None}).status == "unverifiable"


def test_non_effective_unavailable_dependency_does_not_influence_receipt() -> None:
    from pipeline.verification_subject import VerificationSubjectIdentity
    main = VerificationSubjectIdentity(1, "sha1", "a" * 40, "b" * 40, None)
    receipt = {"exit_code": 0, "assertions": [], "detail": "", "subject": {"status": "available", "identity": {"version": 1, "object_format": "sha1", "tree_oid": "a" * 40, "observed_head_oid": "b" * 40, "baseline_oid": None}}, "dependencies": [{"name": "optional", "depends_on": False, "subject": {"status": "unavailable", "reason": "no git"}}]}
    assert classify_receipt(receipt, current_subject=main, dependency_subjects={"optional": None}).status == "present"


def test_dirty_submodule_dependency_records_unavailable_subject(tmp_path: Path) -> None:
    child = tmp_path / "child"
    _repo(child)
    superproject = tmp_path / "super"
    _repo(superproject)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(child), "nested"],
        cwd=superproject,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "nested"], cwd=superproject, check=True)
    (superproject / "nested" / "file").write_text("dirty", encoding="utf-8")

    ctx = PlaceholderContext(dependencies={"super": str(superproject)})
    records = capture_dependency_provenance(
        ctx,
        argv=[str(superproject / "tool")],
        eff_cwd="",
        python="",
        env_overrides={},
    )

    assert records[0]["depends_on"] is True
    assert records[0]["subject"].reason == "dirty_submodule_unrepresentable"


class TestDependsOn:
    def test_path_prefix_respects_separator_boundary(self, tmp_path: Path) -> None:
        """``/repo/dep`` is not a dependency of ``/repo/department``."""
        dep = tmp_path / "dep"
        _repo(dep)
        ctx = PlaceholderContext(dependencies={"dep": str(dep)})

        records = capture_dependency_provenance(
            ctx,
            argv=["python", str(tmp_path / "department" / "tool.py")],
            eff_cwd=str(tmp_path / "department"), python="", env_overrides={},
        )

        assert records[0]["depends_on"] is False
        assert _is_path_prefix(str(dep), str(dep / "tool.py")) is True
        assert _is_path_prefix(str(dep), str(tmp_path / "department")) is False


class TestImportDirectionGuard:
    def test_command_uses_dependency_capture_without_engine_imports(self) -> None:
        """Receipt provenance stays in verification modules, never the engine."""
        import pipeline.verification_command as command
        import pipeline.verification_dependencies as dependencies

        assert command.capture_dependency_provenance is dependencies.capture_dependency_provenance
        source = Path(dependencies.__file__).read_text(encoding="utf-8")
        assert "pipeline.engine" not in source
