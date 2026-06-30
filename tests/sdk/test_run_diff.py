"""Tests for ``sdk.get_run_diff``.

Synthesizes ``diff.patch`` artifacts under the shared ``runs_root`` fixture
so the SDK call exercises the read-side path (parse, filter, render,
truncate) end-to-end without involving git.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from pipeline.engine.diff_apply_check import DiffApplyCheckResult
from sdk import RunDiffFileRecord, RunDiffRecord, get_run_diff
from sdk.errors import RunNotFound

_MODIFY = (
    "diff --git a/api/payload.py b/api/payload.py\n"
    "index abc1234..def5678 100644\n"
    "--- a/api/payload.py\n"
    "+++ b/api/payload.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-old_a\n"
    "-old_b\n"
    "+new_a\n"
    "+new_b\n"
)

_ADD = (
    "diff --git a/api/util.py b/api/util.py\n"
    "new file mode 100644\n"
    "index 0000000..abc1234\n"
    "--- /dev/null\n"
    "+++ b/api/util.py\n"
    "@@ -0,0 +1 @@\n"
    "+helper = 1\n"
)

_RENAME = (
    "diff --git a/old_name.py b/new_name.py\n"
    "similarity index 80%\n"
    "rename from old_name.py\n"
    "rename to new_name.py\n"
    "index abc1234..def5678 100644\n"
    "--- a/old_name.py\n"
    "+++ b/new_name.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+renamed\n"
)

_PATCH = _MODIFY + _ADD + _RENAME


def _apply_check_metadata(
    status: Literal["fail", "degraded"] = "fail",
) -> dict[str, object]:
    reason = (
        "patch_does_not_apply"
        if status == "fail" else "baseline_unavailable"
    )
    return DiffApplyCheckResult(
        status=status,
        reason=reason,
        cwd="/repo",
        patch_path="/runs/20260519_000022/diff.patch",
        baseline_ref="abc123",
        command=("git", "apply", "--check", "--cached", "/runs/diff.patch"),
        stderr="error: No valid patches in input",
        detail="git apply --check exited with 128",
    ).to_metadata()


def _write_run_with_diff(
    runs_dir: Path, run_id: str, diff_text: str | None = _PATCH,
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    if diff_text is not None:
        (run_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
    return run_dir


def test_missing_diff_returns_not_found_record_not_exception(
    runs_root: Path,
) -> None:
    _write_run_with_diff(runs_root, "20260519_000000", diff_text=None)
    rec = get_run_diff("20260519_000000", runs_dir=runs_root, cwd=None)

    assert isinstance(rec, RunDiffRecord)
    assert rec.run_id == "20260519_000000"
    assert rec.found is False
    assert rec.diff_path is None
    assert rec.files == ()
    assert rec.content == ""
    assert rec.truncated is False
    assert rec.message and "No diff artifact" in rec.message


def test_full_mode_no_filter_returns_raw_artifact_byte_for_byte(
    runs_root: Path,
) -> None:
    _write_run_with_diff(runs_root, "20260519_000001")
    rec = get_run_diff(
        "20260519_000001", runs_dir=runs_root, cwd=None, mode="full",
    )
    assert rec.found is True
    assert rec.content == _PATCH
    assert rec.diff_path and rec.diff_path.endswith("/diff.patch")


def test_full_mode_ignores_apply_check_metadata_sidecars(
    runs_root: Path,
) -> None:
    patch_text = _PATCH
    run_dir = _write_run_with_diff(
        runs_root,
        "20260519_000022",
        diff_text=patch_text,
    )
    apply_check = _apply_check_metadata("fail")
    event = {
        "seq": 1,
        "ts": "2026-05-08T10:00:01.000",
        "kind": "artifact.created",
        "phase": None,
        "payload": {
            "path": str(run_dir / "diff.patch"),
            "artifact_kind": "diff",
            "size_bytes": len(patch_text.encode("utf-8")),
            "apply_check": apply_check,
        },
    }
    (run_dir / "events.jsonl").write_text(
        json.dumps(event) + "\n",
        encoding="utf-8",
    )
    (run_dir / "evidence.json").write_text(
        json.dumps({"artifacts": [{"apply_check": apply_check}]}),
        encoding="utf-8",
    )

    rec = get_run_diff(
        "20260519_000022", runs_dir=runs_root, cwd=None, mode="full",
    )

    assert rec.found is True
    assert rec.content == patch_text
    assert rec.diff_path == str(run_dir / "diff.patch")
    assert "patch_does_not_apply" not in rec.content


def test_full_mode_with_path_filter_emits_valid_patch(
    runs_root: Path,
) -> None:
    _write_run_with_diff(runs_root, "20260519_000002")
    rec = get_run_diff(
        "20260519_000002",
        runs_dir=runs_root,
        cwd=None,
        mode="full",
        path="api/util.py",
    )
    assert rec.found is True
    assert rec.content == _ADD
    assert "diff --git" in rec.content
    assert "--- /dev/null" in rec.content
    assert "+++ b/api/util.py" in rec.content
    assert rec.content.endswith("\n")


def test_full_mode_with_path_filter_matches_rename_by_old_name(
    runs_root: Path,
) -> None:
    _write_run_with_diff(runs_root, "20260519_000003")
    rec = get_run_diff(
        "20260519_000003",
        runs_dir=runs_root,
        cwd=None,
        mode="full",
        path="old_name.py",
    )
    assert rec.found is True
    assert rec.content == _RENAME
    assert len(rec.files) == 1
    assert rec.files[0].path == "new_name.py"


def test_files_reflects_filtered_slice_not_unfiltered_diff(
    runs_root: Path,
) -> None:
    _write_run_with_diff(runs_root, "20260519_000004")
    rec = get_run_diff(
        "20260519_000004",
        runs_dir=runs_root,
        cwd=None,
        mode="stat",
        path="api/payload.py",
    )
    assert rec.files == (RunDiffFileRecord("api/payload.py", 2, 2),)


def test_preview_mode_groups_files(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000005")
    rec = get_run_diff(
        "20260519_000005", runs_dir=runs_root, cwd=None, mode="preview",
    )
    assert rec.found is True
    assert "Update(api/payload.py)" in rec.content
    assert "Update(api/util.py)" in rec.content


def test_stat_mode_renders_table(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000006")
    rec = get_run_diff(
        "20260519_000006", runs_dir=runs_root, cwd=None, mode="stat",
    )
    assert rec.found is True
    assert "api/payload.py" in rec.content
    assert "+2 -2" in rec.content
    assert "api/util.py" in rec.content
    assert "+1 -0" in rec.content


def test_path_filter_exact_match(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000007")
    rec = get_run_diff(
        "20260519_000007",
        runs_dir=runs_root,
        cwd=None,
        mode="stat",
        path="api/payload.py",
    )
    assert rec.found is True
    assert len(rec.files) == 1
    assert rec.files[0].path == "api/payload.py"


def test_path_filter_prefix_fallback(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000008")
    rec = get_run_diff(
        "20260519_000008",
        runs_dir=runs_root,
        cwd=None,
        mode="stat",
        path="api/",
    )
    assert rec.found is True
    assert {f.path for f in rec.files} == {"api/payload.py", "api/util.py"}


def test_path_filter_no_match_returns_message(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000009")
    rec = get_run_diff(
        "20260519_000009",
        runs_dir=runs_root,
        cwd=None,
        mode="stat",
        path="nothing/here.py",
    )
    assert rec.found is True
    assert rec.files == ()
    assert rec.content == ""
    assert rec.message and "nothing/here.py" in rec.message


def test_max_bytes_truncates_and_sets_flag(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000010")
    rec = get_run_diff(
        "20260519_000010",
        runs_dir=runs_root,
        cwd=None,
        mode="full",
        max_bytes=50,
    )
    assert rec.found is True
    assert rec.truncated is True
    assert len(rec.content.encode("utf-8")) <= 50
    assert rec.max_bytes == 50


def test_max_bytes_truncates_safely_mid_codepoint(runs_root: Path) -> None:
    body = "Ж" * 100
    text = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        f"@@ -1 +1 @@\n+{body}\n"
    )
    _write_run_with_diff(runs_root, "20260519_000011", diff_text=text)
    rec = get_run_diff(
        "20260519_000011",
        runs_dir=runs_root,
        cwd=None,
        mode="full",
        max_bytes=15,
    )
    assert rec.truncated is True
    assert isinstance(rec.content, str)
    assert "�" not in rec.content


def test_max_bytes_none_is_unlimited(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000012")
    rec = get_run_diff(
        "20260519_000012", runs_dir=runs_root, cwd=None, mode="full",
    )
    assert rec.truncated is False
    assert rec.max_bytes is None


def test_max_bytes_zero_raises(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000013")
    with pytest.raises(ValueError, match="max_bytes"):
        get_run_diff(
            "20260519_000013",
            runs_dir=runs_root,
            cwd=None,
            mode="full",
            max_bytes=0,
        )


def test_max_bytes_negative_raises(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000014")
    with pytest.raises(ValueError, match="max_bytes"):
        get_run_diff(
            "20260519_000014",
            runs_dir=runs_root,
            cwd=None,
            mode="full",
            max_bytes=-1,
        )


def test_invalid_mode_raises(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000015")
    with pytest.raises(ValueError, match="mode"):
        get_run_diff(
            "20260519_000015",
            runs_dir=runs_root,
            cwd=None,
            mode="garbage",  # type: ignore[arg-type]
        )


def test_empty_path_raises(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000016")
    with pytest.raises(ValueError, match="path"):
        get_run_diff(
            "20260519_000016",
            runs_dir=runs_root,
            cwd=None,
            mode="full",
            path="",
        )


def test_whitespace_only_path_raises(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000017")
    with pytest.raises(ValueError, match="path"):
        get_run_diff(
            "20260519_000017",
            runs_dir=runs_root,
            cwd=None,
            mode="full",
            path="   ",
        )


def test_unknown_run_id_raises_runnotfound(runs_root: Path) -> None:
    with pytest.raises(RunNotFound):
        get_run_diff("does_not_exist", runs_dir=runs_root, cwd=None)


def test_record_max_bytes_echoes_cap(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000018")
    rec = get_run_diff(
        "20260519_000018",
        runs_dir=runs_root,
        cwd=None,
        mode="full",
        max_bytes=10_000,
    )
    assert rec.max_bytes == 10_000


def test_color_false_produces_no_ansi(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000019")
    rec = get_run_diff(
        "20260519_000019",
        runs_dir=runs_root,
        cwd=None,
        mode="preview",
        color=False,
    )
    assert "\033[" not in rec.content


def test_runref_resolves_latest_when_run_id_none(runs_root: Path) -> None:
    _write_run_with_diff(runs_root, "20260519_000020")
    _write_run_with_diff(runs_root, "20260519_000021")
    rec = get_run_diff(None, runs_dir=runs_root, cwd=None, mode="stat")
    assert rec.run_id == "20260519_000021"


# ── Per-phase diff reads ───────────────────────────────────────────────


def _write_run_with_phase_diff(
    runs_dir: Path,
    run_id: str,
    phase: str,
    diff_text: str | None = _PATCH,
    *,
    also_write_root_diff: str | None = None,
) -> Path:
    """Synthesize a run with a per-phase diff at phases/<phase>/diff.patch.

    ``also_write_root_diff`` lets a test prove the SDK reads from the
    phase path and not the run-level fallback when ``phase`` is set.
    """
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if diff_text is not None:
        phase_dir = run_dir / "phases" / phase
        phase_dir.mkdir(parents=True)
        (phase_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
    if also_write_root_diff is not None:
        (run_dir / "diff.patch").write_text(
            also_write_root_diff, encoding="utf-8",
        )
    return run_dir


def test_phase_none_still_reads_root_diff(runs_root: Path) -> None:
    """Regression guard: default behaviour unchanged."""
    _write_run_with_phase_diff(
        runs_root, "20260601_000001", "implement",
        diff_text=_ADD,
        also_write_root_diff=_PATCH,
    )
    rec = get_run_diff(
        "20260601_000001", runs_dir=runs_root, cwd=None, mode="full",
    )
    assert rec.found is True
    assert rec.scope == "run"
    assert rec.phase is None
    assert rec.content == _PATCH  # root patch, not the phase one


def test_phase_reads_phase_diff(runs_root: Path) -> None:
    _write_run_with_phase_diff(
        runs_root, "20260601_000002", "implement",
        diff_text=_MODIFY,
        also_write_root_diff=_PATCH,
    )
    rec = get_run_diff(
        "20260601_000002",
        runs_dir=runs_root, cwd=None,
        mode="full", phase="implement",
    )
    assert rec.found is True
    assert rec.scope == "phase"
    assert rec.phase == "implement"
    # Must read the phase artifact, not the root cumulative one.
    assert rec.content == _MODIFY
    assert rec.diff_path is not None
    assert rec.diff_path.endswith("phases/implement/diff.patch")


def test_phase_missing_returns_found_false_with_phase_message(
    runs_root: Path,
) -> None:
    """Quiet phase: no per-phase patch on disk. Must NOT silently fall
    back to the root cumulative diff under a phase scope.
    """
    _write_run_with_phase_diff(
        runs_root, "20260601_000003", "implement",
        diff_text=None,
        also_write_root_diff=_PATCH,
    )
    rec = get_run_diff(
        "20260601_000003",
        runs_dir=runs_root, cwd=None,
        phase="repair_changes",
    )
    assert rec.found is False
    assert rec.scope == "phase"
    assert rec.phase == "repair_changes"
    assert rec.diff_path is None
    assert rec.content == ""
    assert rec.message is not None
    assert "phase" in rec.message and "repair_changes" in rec.message


def test_phase_supports_all_modes(runs_root: Path) -> None:
    _write_run_with_phase_diff(
        runs_root, "20260601_000004", "implement", diff_text=_PATCH,
    )
    for mode in ("preview", "stat", "full"):
        rec = get_run_diff(
            "20260601_000004",
            runs_dir=runs_root, cwd=None,
            mode=mode, phase="implement",
        )
        assert rec.found is True
        assert rec.mode == mode
        assert rec.scope == "phase"
        assert rec.phase == "implement"
        assert rec.content  # non-empty for each mode


def test_phase_supports_path_filter(runs_root: Path) -> None:
    _write_run_with_phase_diff(
        runs_root, "20260601_000005", "implement", diff_text=_PATCH,
    )
    rec = get_run_diff(
        "20260601_000005",
        runs_dir=runs_root, cwd=None,
        mode="full", phase="implement", path="api/util.py",
    )
    assert rec.found is True
    assert rec.scope == "phase"
    assert tuple(f.path for f in rec.files) == ("api/util.py",)
    assert rec.content == _ADD


def test_phase_path_filter_no_match_keeps_scope(runs_root: Path) -> None:
    _write_run_with_phase_diff(
        runs_root, "20260601_000006", "implement", diff_text=_PATCH,
    )
    rec = get_run_diff(
        "20260601_000006",
        runs_dir=runs_root, cwd=None,
        mode="full", phase="implement", path="does/not/exist.py",
    )
    assert rec.found is True
    assert rec.files == ()
    assert rec.scope == "phase"
    assert rec.phase == "implement"


def test_phase_strips_whitespace(runs_root: Path) -> None:
    _write_run_with_phase_diff(
        runs_root, "20260601_000007", "implement", diff_text=_MODIFY,
    )
    rec = get_run_diff(
        "20260601_000007",
        runs_dir=runs_root, cwd=None,
        mode="full", phase="  implement  ",
    )
    assert rec.found is True
    assert rec.phase == "implement"
    assert rec.content == _MODIFY


@pytest.mark.parametrize("bad_phase", ["", "   ", "\t"])
def test_phase_empty_after_strip_raises(
    runs_root: Path, bad_phase: str,
) -> None:
    _write_run_with_phase_diff(
        runs_root, "20260601_000008", "implement",
    )
    with pytest.raises(ValueError, match="phase must be non-empty"):
        get_run_diff(
            "20260601_000008",
            runs_dir=runs_root, cwd=None, phase=bad_phase,
        )


@pytest.mark.parametrize(
    "bad_phase",
    ["..", "../etc", "phases/implement", "a/b", "a\\b", "phase..name"],
)
def test_phase_traversal_or_separator_raises(
    runs_root: Path, bad_phase: str,
) -> None:
    _write_run_with_phase_diff(
        runs_root, "20260601_000009", "implement",
    )
    with pytest.raises(
        ValueError,
        match="path separators or parent refs",
    ):
        get_run_diff(
            "20260601_000009",
            runs_dir=runs_root, cwd=None, phase=bad_phase,
        )


def test_phase_validation_runs_before_run_resolution(
    runs_root: Path,
) -> None:
    """ValueError on phase must precede RunNotFound on the run id —
    matches existing param-validation ordering for ``mode`` / ``path``.
    """
    with pytest.raises(ValueError):
        get_run_diff(
            "does-not-exist",
            runs_dir=runs_root, cwd=None, phase="../escape",
        )


def test_phase_unknown_run_still_raises_runnotfound(
    runs_root: Path,
) -> None:
    with pytest.raises(RunNotFound):
        get_run_diff(
            "does-not-exist",
            runs_dir=runs_root, cwd=None, phase="implement",
        )
