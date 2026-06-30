from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.observability import events as evstore
from pipeline.engine.diff_apply_check import (
    DiffApplyCheckResult,
    capture_run_diff_with_apply_check,
    check_diff_patch_apply,
    diff_patch_durable_block,
    diff_patch_triad,
)
from pipeline.engine.run_diff import (
    assemble_patch,
    capture_phase_diff,
    capture_run_diff,
    file_stats,
    filter_diffs_by_path,
    parse_unified_diff,
    render_diff_preview,
    render_diff_preview_from_diffs,
    render_diff_stat,
    resolve_git_root,
    snapshot_worktree,
    truncate_bytes,
)


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _git_output(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "orcho@example.test")
    _git(path, "config", "user.name", "Orcho Test")
    (path / "payload.py").write_text("value = 1\n", encoding="utf-8")
    _git(path, "add", "payload.py")
    _git(path, "commit", "-qm", "initial")


def _artifact_events(run_dir: Path):
    return [
        event for event in evstore.read_all(run_dir)
        if event.kind == "artifact.created"
    ]


def test_capture_run_diff_writes_core_artifact_and_event(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _init_repo(project)
    evstore.init_event_store(run_dir)
    try:
        (project / "payload.py").write_text("value = 2\n", encoding="utf-8")

        diff_path = capture_run_diff(project, run_dir)

        assert diff_path == run_dir / "diff.patch"
        diff_text = diff_path.read_text(encoding="utf-8")
        assert "-value = 1" in diff_text
        assert "+value = 2" in diff_text
        artifact_events = _artifact_events(run_dir)
        assert artifact_events[-1].payload["artifact_kind"] == "diff"
        assert artifact_events[-1].payload["path"] == str(diff_path)
    finally:
        evstore.init_event_store(None)


def _strip_ansi(text: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def test_render_diff_preview_groups_by_file() -> None:
    preview = render_diff_preview(
        "diff --git a/payload.py b/payload.py\n"
        "--- a/payload.py\n"
        "+++ b/payload.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n",
    )
    plain = _strip_ansi(preview)
    assert "Update(payload.py)" in plain
    assert "Added 1 line, removed 1 line" in plain
    assert "@@ -1 +1 @@" in plain
    assert "-value = 1" in plain
    assert "+value = 2" in plain


def test_render_diff_preview_inserts_spacing_before_file_headers() -> None:
    preview = _strip_ansi(render_diff_preview(_MODIFY_PATCH + _ADD_PATCH))
    assert preview.startswith("\n  📝 Update(payload.py)\n")
    assert "\n\n  📝 Update(new.py)\n" in preview


def test_render_diff_preview_is_unbounded_by_default() -> None:
    diff_text = "".join(
        f"diff --git a/file{i}.py b/file{i}.py\n"
        f"--- a/file{i}.py\n"
        f"+++ b/file{i}.py\n"
        "@@ -1 +1 @@\n"
        f"-old_{i}\n"
        f"+new_{i}\n"
        for i in range(6)
    )

    preview = _strip_ansi(render_diff_preview(diff_text))

    assert "omitted" not in preview
    for i in range(6):
        assert f"Update(file{i}.py)" in preview
        assert f"+new_{i}" in preview


def test_render_diff_preview_bolds_file_path_when_color_enabled() -> None:
    preview = render_diff_preview(
        "diff --git a/payload.py b/payload.py\n"
        "--- a/payload.py\n"
        "+++ b/payload.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n",
    )
    assert "\033[1mpayload.py\033[0m" in preview


def test_render_diff_preview_from_diffs_no_color_omits_bold() -> None:
    diffs = parse_unified_diff(_MODIFY_PATCH)
    plain = render_diff_preview_from_diffs(diffs, color=False)
    assert "\033[1m" not in plain
    assert "Update(payload.py)" in plain


_MODIFY_PATCH = (
    "diff --git a/payload.py b/payload.py\n"
    "index abc1234..def5678 100644\n"
    "--- a/payload.py\n"
    "+++ b/payload.py\n"
    "@@ -1 +1 @@\n"
    "-value = 1\n"
    "+value = 2\n"
)

_ADD_PATCH = (
    "diff --git a/new.py b/new.py\n"
    "new file mode 100644\n"
    "index 0000000..abc1234\n"
    "--- /dev/null\n"
    "+++ b/new.py\n"
    "@@ -0,0 +1 @@\n"
    "+hello\n"
)

_DELETE_PATCH = (
    "diff --git a/gone.py b/gone.py\n"
    "deleted file mode 100644\n"
    "index abc1234..0000000\n"
    "--- a/gone.py\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-bye\n"
)

_RENAME_PURE_PATCH = (
    "diff --git a/old_name.py b/new_name.py\n"
    "similarity index 100%\n"
    "rename from old_name.py\n"
    "rename to new_name.py\n"
)

_RENAME_WITH_EDITS_PATCH = (
    "diff --git a/old_mod.py b/new_mod.py\n"
    "similarity index 80%\n"
    "rename from old_mod.py\n"
    "rename to new_mod.py\n"
    "index abc1234..def5678 100644\n"
    "--- a/old_mod.py\n"
    "+++ b/new_mod.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
)

_MODE_ONLY_PATCH = (
    "diff --git a/script.sh b/script.sh\n"
    "old mode 100644\n"
    "new mode 100755\n"
)

_BINARY_PATCH = (
    "diff --git a/img.png b/img.png\n"
    "index abc1234..def5678 100644\n"
    "Binary files a/img.png and b/img.png differ\n"
)


def test_parse_unified_diff_roundtrips_byte_for_byte() -> None:
    text = (
        _MODIFY_PATCH
        + _ADD_PATCH
        + _DELETE_PATCH
        + _RENAME_PURE_PATCH
        + _RENAME_WITH_EDITS_PATCH
        + _MODE_ONLY_PATCH
        + _BINARY_PATCH
    )
    diffs = parse_unified_diff(text)
    assembled = "".join(line for d in diffs for line in d.raw_lines)
    assert assembled == text


def test_parse_unified_diff_preserves_newlines_via_keepends() -> None:
    diffs = parse_unified_diff(_MODIFY_PATCH)
    assert len(diffs) == 1
    assert all(line.endswith("\n") for line in diffs[0].raw_lines)
    assert all(line.endswith("\n") for line in diffs[0].body_lines)


def test_parse_unified_diff_modify_paths_and_body() -> None:
    [diff] = parse_unified_diff(_MODIFY_PATCH)
    assert diff.path == "payload.py"
    assert diff.old_path == "payload.py"
    assert diff.new_path == "payload.py"
    body = [line.rstrip("\n") for line in diff.body_lines]
    assert body == ["@@ -1 +1 @@", "-value = 1", "+value = 2"]


def test_parse_unified_diff_pure_add() -> None:
    [diff] = parse_unified_diff(_ADD_PATCH)
    assert diff.path == "new.py"
    assert diff.old_path is None
    assert diff.new_path == "new.py"


def test_parse_unified_diff_pure_delete() -> None:
    [diff] = parse_unified_diff(_DELETE_PATCH)
    assert diff.path == "gone.py"
    assert diff.old_path == "gone.py"
    assert diff.new_path is None


def test_parse_unified_diff_rename_pure() -> None:
    [diff] = parse_unified_diff(_RENAME_PURE_PATCH)
    assert diff.old_path == "old_name.py"
    assert diff.new_path == "new_name.py"
    assert diff.path == "new_name.py"
    assert diff.body_lines == ()


def test_parse_unified_diff_rename_with_edits() -> None:
    [diff] = parse_unified_diff(_RENAME_WITH_EDITS_PATCH)
    assert diff.old_path == "old_mod.py"
    assert diff.new_path == "new_mod.py"
    assert diff.path == "new_mod.py"
    assert len(diff.body_lines) == 3


def test_parse_unified_diff_mode_only() -> None:
    [diff] = parse_unified_diff(_MODE_ONLY_PATCH)
    assert diff.path == "script.sh"
    assert diff.old_path == "script.sh"
    assert diff.new_path == "script.sh"
    assert diff.body_lines == ()
    assert file_stats(diff) == (0, 0)


def test_parse_unified_diff_binary() -> None:
    [diff] = parse_unified_diff(_BINARY_PATCH)
    assert diff.path == "img.png"
    assert diff.body_lines == ()
    assert file_stats(diff) == (0, 0)


def test_parse_unified_diff_empty_text() -> None:
    assert parse_unified_diff("") == []


_MARKDOWN_HEADING_PATCH = (
    "diff --git a/doc.md b/doc.md\n"
    "index abc1234..def5678 100644\n"
    "--- a/doc.md\n"
    "+++ b/doc.md\n"
    "@@ -1 +1 @@\n"
    "--- old heading\n"
    "+++ new heading\n"
)


def test_parse_unified_diff_hunk_content_starting_with_triple_signs() -> None:
    """Regression: content lines starting with ``---`` / ``+++`` (e.g. a
    markdown ``-- old`` → ``++ new`` change) must be parsed as hunk body,
    not silently dropped or mistaken for file headers.

    Previous parser filtered any line starting with ``---``/``+++`` out
    of body_lines (substring match, no hunk awareness), so this case
    reported ``(0, 0)`` and could leak content text into the parsed
    path (``_extract_paths`` would treat the in-hunk ``--- old heading``
    as a ``--- a/<path>`` re-declaration).
    """
    [diff] = parse_unified_diff(_MARKDOWN_HEADING_PATCH)
    assert diff.path == "doc.md"
    assert diff.old_path == "doc.md"
    assert diff.new_path == "doc.md"
    assert file_stats(diff) == (1, 1)
    body = [line.rstrip("\n") for line in diff.body_lines]
    assert "--- old heading" in body
    assert "+++ new heading" in body


def test_render_diff_preview_shows_content_starting_with_triple_signs() -> None:
    preview = _strip_ansi(render_diff_preview(_MARKDOWN_HEADING_PATCH))
    assert "Update(doc.md)" in preview
    assert "Added 1 line, removed 1 line" in preview
    assert "--- old heading" in preview
    assert "+++ new heading" in preview


def test_parse_unified_diff_no_diff_git_header_fallback() -> None:
    text = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    [diff] = parse_unified_diff(text)
    assert diff.path == "foo.py"
    assert file_stats(diff) == (1, 1)


def test_file_stats_counts_added_removed() -> None:
    [diff] = parse_unified_diff(_MODIFY_PATCH)
    assert file_stats(diff) == (1, 1)


def test_render_diff_preview_from_diffs_matches_text_wrapper() -> None:
    text = _MODIFY_PATCH + _ADD_PATCH
    via_text = render_diff_preview(text)
    via_diffs = render_diff_preview_from_diffs(parse_unified_diff(text))
    assert via_text == via_diffs


def test_render_diff_preview_color_can_be_disabled() -> None:
    diffs = parse_unified_diff(_MODIFY_PATCH)
    rendered = render_diff_preview_from_diffs(diffs, color=False)
    assert "\033[" not in rendered
    assert "@@ -1 +1 @@" in rendered
    assert "-value = 1" in rendered
    assert "+value = 2" in rendered


def test_filediff_is_frozen() -> None:
    [diff] = parse_unified_diff(_MODIFY_PATCH)
    with pytest.raises((AttributeError, TypeError)):
        diff.path = "other"  # type: ignore[misc]


def test_render_diff_stat_one_line_per_file() -> None:
    diffs = parse_unified_diff(_MODIFY_PATCH + _ADD_PATCH)
    out = render_diff_stat(diffs, color=False)
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("payload.py")
    assert lines[0].endswith("| +1 -1")
    assert lines[1].startswith("new.py")
    assert lines[1].endswith("| +1 -0")


def test_render_diff_stat_aligns_path_column() -> None:
    diffs = parse_unified_diff(_MODIFY_PATCH + _ADD_PATCH)
    out = render_diff_stat(diffs, color=False)
    bars = [line.index("|") for line in out.rstrip("\n").split("\n")]
    assert len(set(bars)) == 1


def test_render_diff_stat_binary_and_mode_render_as_zeros() -> None:
    diffs = parse_unified_diff(_BINARY_PATCH + _MODE_ONLY_PATCH)
    out = render_diff_stat(diffs, color=False)
    assert "img.png" in out
    assert "+0 -0" in out
    assert "script.sh" in out


def test_render_diff_stat_empty_input() -> None:
    assert render_diff_stat([], color=False) == ""


def test_filter_diffs_by_path_exact_match() -> None:
    diffs = parse_unified_diff(_MODIFY_PATCH + _ADD_PATCH)
    [match] = filter_diffs_by_path(diffs, "payload.py")
    assert match.path == "payload.py"


def test_filter_diffs_by_path_prefix_fallback_when_no_exact() -> None:
    text = (
        "diff --git a/api/payload.py b/api/payload.py\n"
        "--- a/api/payload.py\n"
        "+++ b/api/payload.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git a/api/util.py b/api/util.py\n"
        "--- a/api/util.py\n"
        "+++ b/api/util.py\n"
        "@@ -1 +1 @@\n"
        "-c\n"
        "+d\n"
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -1 +1 @@\n"
        "-e\n"
        "+f\n"
    )
    diffs = parse_unified_diff(text)
    out = filter_diffs_by_path(diffs, "api")
    assert {d.path for d in out} == {"api/payload.py", "api/util.py"}


def test_filter_diffs_by_path_trailing_slash_normalized() -> None:
    text = (
        "diff --git a/api/payload.py b/api/payload.py\n"
        "--- a/api/payload.py\n"
        "+++ b/api/payload.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    diffs = parse_unified_diff(text)
    [match] = filter_diffs_by_path(diffs, "api/")
    assert match.path == "api/payload.py"


def test_filter_diffs_by_path_matches_rename_by_old_name() -> None:
    diffs = parse_unified_diff(_RENAME_PURE_PATCH)
    [match] = filter_diffs_by_path(diffs, "old_name.py")
    assert match.new_path == "new_name.py"


def test_filter_diffs_by_path_matches_delete_by_old_name() -> None:
    diffs = parse_unified_diff(_DELETE_PATCH)
    [match] = filter_diffs_by_path(diffs, "gone.py")
    assert match.old_path == "gone.py"
    assert match.new_path is None


def test_filter_diffs_by_path_no_match_returns_empty() -> None:
    diffs = parse_unified_diff(_MODIFY_PATCH)
    assert filter_diffs_by_path(diffs, "nothing_like_this.py") == []


def test_assemble_patch_round_trips_filtered_subset() -> None:
    text = _MODIFY_PATCH + _DELETE_PATCH + _ADD_PATCH
    diffs = parse_unified_diff(text)
    keep = filter_diffs_by_path(diffs, "new.py")
    assert assemble_patch(keep) == _ADD_PATCH


def test_truncate_bytes_none_is_noop() -> None:
    text = "abcdef"
    out, truncated = truncate_bytes(text, None)
    assert out == text
    assert truncated is False


def test_truncate_bytes_within_budget_is_noop() -> None:
    text = "abcdef"
    out, truncated = truncate_bytes(text, 100)
    assert out == text
    assert truncated is False


def test_truncate_bytes_cuts_and_sets_flag() -> None:
    text = "abcdefghij"
    out, truncated = truncate_bytes(text, 5)
    assert out == "abcde"
    assert truncated is True


def test_truncate_bytes_handles_mid_codepoint() -> None:
    text = "a" + "Ж" * 5
    out, truncated = truncate_bytes(text, 4)
    assert truncated is True
    assert isinstance(out, str)
    assert out.startswith("a")
    assert "�" not in out
    assert len(out.encode("utf-8")) <= 4


def test_truncate_bytes_rejects_zero_or_negative() -> None:
    with pytest.raises(ValueError):
        truncate_bytes("x", 0)
    with pytest.raises(ValueError):
        truncate_bytes("x", -1)


def test_resolve_git_root_honors_workspace_git_dir(tmp_path: Path) -> None:
    """resolve_git_root reads git_dir from workspace config, not plugin.py."""
    import json
    from unittest.mock import patch

    project = tmp_path / "project"
    git_root = project / "src"
    project.mkdir()
    _init_repo(git_root)

    # Write workspace config with object-form project entry.
    workspace_dir = tmp_path / "workspace-orchestrator"
    config_dir = workspace_dir / ".orcho"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.local.json"
    config_path.write_text(
        json.dumps({"projects": {"project": {"path": str(project), "git_dir": "src"}}}),
        encoding="utf-8",
    )

    with patch(
        "pipeline.project.project_aliases._resolve_workspace_dir",
        return_value=workspace_dir,
    ):
        assert resolve_git_root(project) == git_root


# ── snapshot_worktree + per-phase capture ────────────────────────────────


def test_snapshot_worktree_returns_none_outside_git(tmp_path: Path) -> None:
    assert snapshot_worktree(tmp_path) is None


def test_snapshot_worktree_clean_repo_returns_head_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    head_tree = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD^{tree}"],
        cwd=str(tmp_path), text=True,
    ).strip()
    # A clean worktree snapshots to the same tree as HEAD's commit.
    assert snapshot_worktree(tmp_path) == head_tree


def test_snapshot_worktree_includes_untracked(tmp_path: Path) -> None:
    """The snapshot tree must contain new untracked files — the bug was
    ``git stash create`` silently omitting them.
    """
    _init_repo(tmp_path)
    (tmp_path / "brand_new.py").write_text("created = True\n", encoding="utf-8")
    snap = snapshot_worktree(tmp_path)
    assert snap is not None
    listing = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", snap],
        cwd=str(tmp_path), text=True,
    )
    assert "brand_new.py" in listing


def test_snapshot_worktree_dirty_returns_distinct_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "payload.py").write_text("value = 99\n", encoding="utf-8")
    head = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=str(tmp_path), text=True,
    ).strip()
    snap = snapshot_worktree(tmp_path)
    assert snap is not None
    assert snap != head


def test_snapshot_worktree_immutable_across_subsequent_commit(tmp_path: Path) -> None:
    """A snapshot SHA must keep pointing at pre-snapshot state even after
    the runtime commits during the phase — load-bearing guarantee that
    per-phase diff is computed against the right baseline.
    """
    _init_repo(tmp_path)
    (tmp_path / "payload.py").write_text("value = 2\n", encoding="utf-8")
    snap = snapshot_worktree(tmp_path)
    assert snap is not None
    _git(tmp_path, "add", "payload.py")
    _git(tmp_path, "commit", "-qm", "phase-internal commit")

    diff_path = capture_run_diff(tmp_path, tmp_path / "_run", baseline_ref=snap)
    # No further worktree changes since the snapshot → no per-phase diff.
    assert diff_path is None


def test_capture_run_diff_baseline_mode_no_fallback(tmp_path: Path) -> None:
    """Baseline mode with a SHA that matches the current worktree must
    return None and NEVER fall back to the cumulative 3-strategy
    capture. Otherwise a quiet phase would print the run's cumulative
    diff under a per-phase header.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    # Make a pre-existing uncommitted change *before* taking the snapshot
    # so the cumulative ``git diff`` would be non-empty — proves the
    # baseline-mode call ignores it.
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    snap = snapshot_worktree(project)
    assert snap is not None

    diff_path = capture_run_diff(project, run_dir, baseline_ref=snap)

    assert diff_path is None
    assert not (run_dir / "diff.patch").exists()


def test_capture_run_diff_baseline_mode_diffs_post_snapshot_only(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    snap = snapshot_worktree(project)
    assert snap is not None
    (project / "payload.py").write_text("value = 3\n", encoding="utf-8")

    diff_path = capture_run_diff(project, run_dir, baseline_ref=snap)

    assert diff_path == run_dir / "diff.patch"
    diff_text = diff_path.read_text(encoding="utf-8")
    assert "-value = 2" in diff_text
    assert "+value = 3" in diff_text
    # Pre-snapshot transition 1→2 must NOT appear.
    assert "-value = 1" not in diff_text


def test_capture_run_diff_baseline_mode_emit_event_default_false_path(
    tmp_path: Path,
) -> None:
    """``emit_event=False`` must suppress ``artifact.created``."""
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _init_repo(project)
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    snap = snapshot_worktree(project)
    (project / "payload.py").write_text("value = 3\n", encoding="utf-8")

    evstore.init_event_store(run_dir)
    try:
        diff_path = capture_run_diff(
            project, run_dir, baseline_ref=snap, emit_event=False,
        )
        assert diff_path is not None
        artifact_events = [
            e for e in evstore.read_all(run_dir)
            if e.kind == "artifact.created"
        ]
        assert artifact_events == []
    finally:
        evstore.init_event_store(None)


def test_capture_run_diff_nested_patch_subpath_auto_mkdir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")

    diff_path = capture_run_diff(
        project, run_dir, patch_subpath="phases/implement/diff.patch",
    )

    assert diff_path == run_dir / "phases" / "implement" / "diff.patch"
    assert diff_path.exists()


def test_capture_phase_diff_returns_preview_and_normalized_paths(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    (project / "extra.py").write_text("x = 1\n", encoding="utf-8")
    _git(project, "add", "extra.py")
    _git(project, "commit", "-qm", "extra")
    snap = snapshot_worktree(project)
    assert snap is not None
    (project / "payload.py").write_text("value = 9\n", encoding="utf-8")
    (project / "extra.py").write_text("x = 2\n", encoding="utf-8")

    captured = capture_phase_diff(
        project, run_dir, baseline_ref=snap, phase_name="implement",
    )

    assert captured is not None
    preview, files = captured
    assert preview  # non-empty
    assert set(files) == {"payload.py", "extra.py"}
    # And matches the per-phase patch path the helper wrote.
    assert (run_dir / "phases" / "implement" / "diff.patch").exists()


def test_capture_phase_diff_quiet_phase_returns_none(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    snap = snapshot_worktree(project)
    assert snap is not None

    captured = capture_phase_diff(
        project, run_dir, baseline_ref=snap, phase_name="repair_changes",
    )

    assert captured is None
    assert not (run_dir / "phases" / "repair_changes" / "diff.patch").exists()


# ── untracked-file capture (regression: phase diff was tracked-only) ──────


def test_capture_run_diff_baseline_mode_captures_new_untracked_file(
    tmp_path: Path,
) -> None:
    """Reproduces the reported bug: a phase whose only output is a NEW
    file (left untracked, as ``change_handoff`` requires) must still show
    up in the per-phase baseline diff.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    snap = snapshot_worktree(project)
    assert snap is not None
    # New file created during the phase — never ``git add``-ed.
    (project / "report.md").write_text("# verification\nok\n", encoding="utf-8")

    diff_path = capture_run_diff(project, run_dir, baseline_ref=snap)

    assert diff_path is not None
    diff_text = diff_path.read_text(encoding="utf-8")
    assert "report.md" in diff_text
    assert "new file" in diff_text
    assert "+# verification" in diff_text


def test_capture_phase_diff_captures_new_untracked_file(
    tmp_path: Path,
) -> None:
    """End-to-end of the reported bug through the per-phase renderer path:
    new untracked files land in both ``files`` and the preview.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    snap = snapshot_worktree(project)
    assert snap is not None
    (project / "_docs" / "plans").mkdir(parents=True)
    (project / "_docs" / "plans" / "inventory.md").write_text(
        "item\n", encoding="utf-8",
    )

    captured = capture_phase_diff(
        project, run_dir, baseline_ref=snap, phase_name="implement",
    )

    assert captured is not None
    preview, files = captured
    assert preview  # non-empty — no longer "(no changes)"
    assert "_docs/plans/inventory.md" in files


def test_capture_run_diff_cumulative_captures_new_untracked_file(
    tmp_path: Path,
) -> None:
    """The cumulative ``diff.patch`` (delivery/evidence) must also include
    new untracked files — same root cause, broader blast radius.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    (project / "fresh.txt").write_text("hello\n", encoding="utf-8")

    diff_path = capture_run_diff(project, run_dir)

    assert diff_path is not None
    diff_text = diff_path.read_text(encoding="utf-8")
    assert "fresh.txt" in diff_text
    assert "+hello" in diff_text


def test_capture_run_diff_preserves_applyable_patch_terminator(
    tmp_path: Path,
) -> None:
    """``diff.patch`` must stay byte-shape compatible with ``git apply``.

    Regression: trimming ``git diff`` stdout removed the final LF when the last
    hunk belonged to a new untracked file, making the artifact fail with
    ``corrupt patch at line ...``.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    (project / "fresh.txt").write_text("hello\n", encoding="utf-8")

    diff_path = capture_run_diff(project, run_dir)

    assert diff_path is not None
    assert diff_path.read_bytes().endswith(b"\n")
    _git(project, "apply", "--check", "--cached", str(diff_path))


def test_capture_run_diff_with_apply_check_emits_pass_metadata(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")

    evstore.init_event_store(run_dir)
    try:
        captured = capture_run_diff_with_apply_check(
            project,
            run_dir,
            baseline_ref=baseline,
        )

        diff_path = captured.path
        assert diff_path == run_dir / "diff.patch"
        assert diff_path.read_bytes().endswith(b"\n")
        assert captured.apply_check is not None
        assert captured.apply_check.status == "pass"
        _git(project, "apply", "--check", "--cached", str(diff_path))
        [artifact_event] = _artifact_events(run_dir)
        apply_check = artifact_event.payload["apply_check"]
        assert apply_check["status"] == "pass"
        assert apply_check["reason"] == "patch_applies"
        assert apply_check["command"][:4] == [
            "git", "apply", "--check", "--cached",
        ]
    finally:
        evstore.init_event_store(None)


def test_capture_run_diff_with_apply_check_emits_fail_for_corrupt_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None
    diff_path = run_dir / "diff.patch"
    diff_path.write_text("not a unified patch\n", encoding="utf-8")

    monkeypatch.setattr(
        "pipeline.engine.diff_apply_check.capture_run_diff",
        lambda *_args, **_kwargs: diff_path,
    )
    evstore.init_event_store(run_dir)
    try:
        captured = capture_run_diff_with_apply_check(
            project,
            run_dir,
            baseline_ref=baseline,
        )

        assert captured.path == diff_path
        assert captured.apply_check is not None
        assert captured.apply_check.status == "fail"
        [artifact_event] = _artifact_events(run_dir)
        apply_check = artifact_event.payload["apply_check"]
        assert apply_check["status"] == "fail"
        assert apply_check["reason"] == "patch_does_not_apply"
        assert apply_check["stderr"] or apply_check["detail"]
    finally:
        evstore.init_event_store(None)


def test_capture_run_diff_with_apply_check_emits_degraded_without_baseline(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _init_repo(project)
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")

    evstore.init_event_store(run_dir)
    try:
        captured = capture_run_diff_with_apply_check(
            project,
            run_dir,
            baseline_ref=None,
        )

        assert captured.path == run_dir / "diff.patch"
        assert captured.apply_check is not None
        assert captured.apply_check.status == "degraded"
        [artifact_event] = _artifact_events(run_dir)
        apply_check = artifact_event.payload["apply_check"]
        assert apply_check["status"] == "degraded"
        assert apply_check["reason"] == "baseline_unavailable"
    finally:
        evstore.init_event_store(None)


# ── triad projection: single source of truth ─────────────────────────────


def _apply_check(status: str, reason: str) -> DiffApplyCheckResult:
    return DiffApplyCheckResult(
        status=status,  # type: ignore[arg-type]
        reason=reason,
        cwd="/repo",
        patch_path="/run/diff.patch",
        baseline_ref="base-tree",
        detail="why",
    )


def test_diff_patch_triad_pass_is_valid() -> None:
    assert diff_patch_triad(_apply_check("pass", "patch_applies")) == "patch_valid"


def test_diff_patch_triad_fail_is_invalid() -> None:
    assert (
        diff_patch_triad(_apply_check("fail", "patch_does_not_apply"))
        == "patch_invalid"
    )


def test_diff_patch_triad_degraded_missing_artifact_is_missing() -> None:
    assert (
        diff_patch_triad(_apply_check("degraded", "patch_unavailable"))
        == "patch_missing"
    )
    assert (
        diff_patch_triad(_apply_check("degraded", "patch_unreadable"))
        == "patch_missing"
    )


def test_diff_patch_triad_other_degraded_is_unknown() -> None:
    # A degraded result whose cause is not an absent/unreadable artifact —
    # e.g. baseline_unavailable — is unknown, NOT invalid (do not conflate
    # degraded with fail).
    assert (
        diff_patch_triad(_apply_check("degraded", "baseline_unavailable"))
        == "patch_unknown"
    )
    assert diff_patch_triad(None) == "patch_missing"


def test_diff_patch_durable_block_is_compact() -> None:
    block = diff_patch_durable_block(_apply_check("fail", "patch_does_not_apply"))
    assert block == {
        "status": "patch_invalid",
        "reason": "patch_does_not_apply",
        "patch_path": "/run/diff.patch",
        "baseline_ref": "base-tree",
        "detail": "why",
    }
    # Verbose stdout/stderr stay in the evidence event, not the durable block.
    assert "stdout" not in block
    assert "stderr" not in block


def test_check_diff_patch_apply_passes_valid_artifact_and_preserves_checkout(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    diff_path = capture_run_diff(project, run_dir, baseline_ref=baseline)
    assert diff_path is not None

    # A staged sentinel makes accidental real-index use visible.
    (project / "sentinel.txt").write_text("keep staged\n", encoding="utf-8")
    _git(project, "add", "sentinel.txt")
    status_before = _git_output(project, "status", "--short")
    index_before = _git_output(project, "ls-files", "--stage")

    result = check_diff_patch_apply(
        project,
        patch_path=diff_path,
        baseline_ref=baseline,
    )

    assert result.status == "pass"
    assert result.reason == "patch_applies"
    assert result.command[:4] == ("git", "apply", "--check", "--cached")
    assert result.cwd == str(project)
    assert result.patch_path == str(diff_path.resolve())
    assert _git_output(project, "status", "--short") == status_before
    assert _git_output(project, "ls-files", "--stage") == index_before
    assert (project / "payload.py").read_text(encoding="utf-8") == "value = 2\n"


def test_check_diff_patch_apply_fails_corrupt_artifact(tmp_path: Path) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    diff_path = capture_run_diff(project, run_dir, baseline_ref=baseline)
    assert diff_path is not None
    diff_path.write_text("not a unified patch\n", encoding="utf-8")

    result = check_diff_patch_apply(
        project,
        patch_path=diff_path,
        baseline_ref=baseline,
    )

    assert result.status == "fail"
    assert result.reason
    assert result.stderr or result.detail


@pytest.mark.parametrize("baseline_ref", [None, "", "missing-baseline-ref"])
def test_check_diff_patch_apply_degrades_without_usable_baseline(
    tmp_path: Path,
    baseline_ref: str | None,
) -> None:
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "payload.py").write_text("value = 2\n", encoding="utf-8")
    diff_path = capture_run_diff(project, run_dir, baseline_ref=baseline)
    assert diff_path is not None

    result = check_diff_patch_apply(
        project,
        patch_path=diff_path,
        baseline_ref=baseline_ref,
    )

    assert result.status == "degraded"
    assert result.reason == "baseline_unavailable"


def test_check_diff_patch_apply_degrades_when_patch_missing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _init_repo(project)
    baseline = snapshot_worktree(project)
    assert baseline is not None

    result = check_diff_patch_apply(
        project,
        patch_path=tmp_path / "missing.patch",
        baseline_ref=baseline,
    )

    assert result.status == "degraded"
    assert result.reason == "patch_unavailable"


# ── byte-accurate capture: binary / non-UTF8 repos (regression) ──────────


def _init_bare_repo(path: Path) -> None:
    """Init a git repo with identity but no seeded text file."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "orcho@example.test")
    _git(path, "config", "user.name", "Orcho Test")


def test_capture_run_diff_non_utf8_text_patch_applies(tmp_path: Path) -> None:
    """A repo with non-UTF8 (latin-1) text bytes must produce a ``diff.patch``
    that ``git apply --check`` accepts.

    Git treats a file without NUL bytes as text, so ``\\xe9`` (``é`` in
    latin-1) appears verbatim in the diff context. The pre-fix path captured
    git output with ``text=True, errors="replace"`` and re-encoded as UTF-8,
    rewriting ``\\xe9`` to U+FFFD and corrupting the patch. The byte-accurate
    path preserves the exact bytes, so the apply-check passes.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_bare_repo(project)
    (project / "data.txt").write_bytes(b"caf\xe9\nline2\n")
    _git(project, "add", "data.txt")
    _git(project, "commit", "-qm", "initial")
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "data.txt").write_bytes(b"caf\xe9\nline2 changed\n")

    diff_path = capture_run_diff(project, run_dir, baseline_ref=baseline)

    assert diff_path is not None
    # Exact non-UTF8 byte survived capture (no U+FFFD re-encode).
    assert b"\xe9" in diff_path.read_bytes()

    result = check_diff_patch_apply(
        project,
        patch_path=diff_path,
        baseline_ref=baseline,
    )

    assert result.status == "pass"
    assert result.reason == "patch_applies"


def test_capture_run_diff_binary_patch_applies(tmp_path: Path) -> None:
    """A binary-file change must capture as an applicable literal/delta patch.

    Without ``--binary`` git emits "Binary files a/x and b/x differ", which
    ``git apply --check`` rejects. With ``--binary`` the patch carries a
    "GIT binary patch" block and applies cleanly.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_bare_repo(project)
    (project / "blob.bin").write_bytes(bytes(range(256)))
    _git(project, "add", "blob.bin")
    _git(project, "commit", "-qm", "initial binary")
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "blob.bin").write_bytes(bytes(range(255, -1, -1)))

    diff_path = capture_run_diff(project, run_dir, baseline_ref=baseline)

    assert diff_path is not None
    captured = diff_path.read_bytes()
    assert b"GIT binary patch" in captured
    assert b"Binary files" not in captured

    result = check_diff_patch_apply(
        project,
        patch_path=diff_path,
        baseline_ref=baseline,
    )

    assert result.status == "pass"
    assert result.reason == "patch_applies"


def test_non_utf8_diff_corrupted_by_text_reencode_would_fail(
    tmp_path: Path,
) -> None:
    """Negative control: the pre-fix ``text=True, errors="replace"`` capture
    re-encoded non-UTF8 bytes to U+FFFD, producing a patch ``git apply
    --check`` rejects.

    Reproduces the old write path by decoding the byte-accurate patch with
    ``errors="replace"`` and re-encoding to UTF-8, then asserts the apply
    check fails — proving the fix (write_bytes) is load-bearing.
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_bare_repo(project)
    (project / "data.txt").write_bytes(b"caf\xe9\nline2\n")
    _git(project, "add", "data.txt")
    _git(project, "commit", "-qm", "initial")
    baseline = snapshot_worktree(project)
    assert baseline is not None
    (project / "data.txt").write_bytes(b"caf\xe9\nline2 changed\n")

    diff_path = capture_run_diff(project, run_dir, baseline_ref=baseline)
    assert diff_path is not None
    raw = diff_path.read_bytes()

    # Simulate the pre-fix path: decode with errors="replace", re-encode UTF-8.
    corrupted = raw.decode("utf-8", errors="replace").encode("utf-8")
    assert corrupted != raw  # re-encode actually mutated the bytes
    corrupt_path = run_dir / "corrupt.patch"
    corrupt_path.write_bytes(corrupted)

    result = check_diff_patch_apply(
        project,
        patch_path=corrupt_path,
        baseline_ref=baseline,
    )

    assert result.status == "fail"
    assert result.reason == "patch_does_not_apply"


def test_capture_run_diff_baseline_mode_ignores_preexisting_untracked(
    tmp_path: Path,
) -> None:
    """An untracked file that existed *before* the snapshot is user-owned
    and must NOT be attributed to the phase (tree-vs-tree: it is in both
    endpoints, so it cancels out).
    """
    project = tmp_path / "project"
    run_dir = tmp_path / "run"
    _init_repo(project)
    (project / "preexisting.txt").write_text("user data\n", encoding="utf-8")
    snap = snapshot_worktree(project)
    assert snap is not None
    # Phase makes no changes.

    diff_path = capture_run_diff(project, run_dir, baseline_ref=snap)

    assert diff_path is None
