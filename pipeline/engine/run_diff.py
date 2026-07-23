"""Run-level git diff capture and parsed-diff helpers.

The engine owns the canonical run directory, so the run-local ``diff.patch``
artifact is captured here rather than by a UI layer. UI clients may still call
the same function lazily for older runs.

This module also exposes a fidelity-preserving unified-diff parser
(:class:`FileDiff` + :func:`parse_unified_diff`) and a small set of helpers
used by ``sdk.get_run_diff`` to render previews, stat tables, filter by path,
reassemble subsets into a valid patch, and bound output by bytes. These
helpers are engine-internal-public — they live here for reuse by the SDK but
are *not* part of the SDK/API surface. CLI and MCP must go through
``sdk.get_run_diff``; do not import these helpers directly from those layers.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.io.ansi import C, paint
from pipeline.verification_subject import snapshot_worktree_tree


@dataclass(frozen=True, slots=True)
class FileDiff:
    """A single file section of a unified diff, parsed losslessly.

    ``raw_lines`` preserves the entire section byte-for-byte (newlines kept
    via ``splitlines(keepends=True)``), so ``"".join(d.raw_lines for d in
    diffs)`` round-trips the original patch text exactly — including
    ``diff --git`` / ``index`` / mode / rename markers, ``---``/``+++``
    headers, hunks, ``Binary files ... differ``, and
    ``\\ No newline at end of file`` trailers. This is what makes
    ``mode="full"`` with a ``path=`` filter emit a patch that ``git apply``
    accepts.

    ``body_lines`` is the subset suitable for preview/stat rendering:
    ``@@`` hunk headers and ``+``/``-`` lines (excluding ``+++``/``---``
    file-header markers). Empty for binary, mode-only, and pure-rename
    sections — those still appear in :attr:`files` with ``+0 -0``.
    """

    path: str
    old_path: str | None
    new_path: str | None
    raw_lines: tuple[str, ...]
    body_lines: tuple[str, ...]


def resolve_git_root(project_path: Path) -> Path | None:
    """Return the git root for ``project_path``, honoring workspace-config ``git_dir``."""
    if not project_path or not project_path.exists():
        return None

    try:
        from pipeline.project.project_aliases import load_workspace_project_git_dir

        git_dir_rel = load_workspace_project_git_dir(project_path).strip()
    except Exception:
        git_dir_rel = ""

    if git_dir_rel:
        candidate = project_path / git_dir_rel
        if (candidate / ".git").exists():
            return candidate

    if (project_path / ".git").exists():
        return project_path

    return None


def render_diff_preview(
    diff_text: str,
    *,
    max_files: int | None = None,
    max_lines_per_file: int | None = None,
) -> str:
    """Render a Claude-style preview from a unified diff.

    The default is intentionally unbounded: the run artifact is the source of
    truth, and the engine transcript should not hide changed files in larger
    projects. Callers that need a compact widget can pass explicit limits.

    Thin wrapper around :func:`parse_unified_diff` +
    :func:`render_diff_preview_from_diffs` — kept for backwards compatibility
    with existing engine callers that pass raw patch text.
    """
    return render_diff_preview_from_diffs(
        parse_unified_diff(diff_text),
        max_files=max_files,
        max_lines_per_file=max_lines_per_file,
    )


def render_diff_preview_from_diffs(
    diffs: list[FileDiff],
    *,
    max_files: int | None = None,
    max_lines_per_file: int | None = None,
    color: bool = True,
) -> str:
    """Render a Claude-style preview from already-parsed file sections.

    Identical output shape to :func:`render_diff_preview` for the same input;
    use this variant when you already have parsed sections to avoid a
    re-stringify-then-reparse round-trip (which would lose ``raw_lines``
    fidelity).
    """
    if not diffs:
        return ""

    out: list[str] = []
    shown_files = diffs if max_files is None else diffs[:max_files]
    for file_diff in shown_files:
        added, removed = file_stats(file_diff)
        shown = (
            file_diff.body_lines
            if max_lines_per_file is None
            else file_diff.body_lines[:max_lines_per_file]
        )
        omitted = len(file_diff.body_lines) - len(shown)

        path_display = paint(file_diff.path, C.BOLD, color=color)
        out.append("\n")
        out.append(f"  📝 Update({path_display})\n")
        out.append(
            f"     Added {added} {_plural(added, 'line')}, "
            f"removed {removed} {_plural(removed, 'line')}\n",
        )
        for raw in shown:
            line = raw.rstrip("\n")
            out.append(f"     {_color_diff_line(line, color=color)}\n")
        if omitted > 0:
            out.append(f"     ... {omitted} more diff lines omitted\n")
    if len(diffs) > len(shown_files):
        out.append(f"  ... {len(diffs) - len(shown_files)} more files omitted\n")
    return "".join(out)


def _snapshot_tree(git_root: Path) -> str | None:
    """Write an immutable tree object for the full current worktree state.

    Builds the tree in a **temporary index** (``GIT_INDEX_FILE`` pointing
    at a throwaway path) so the real index and working tree are never
    touched — no ``git add`` against the user's index, no staged-state
    side effects. The temporary index is seeded from ``HEAD`` before
    ``git add -A`` overlays every worktree path (tracked edits *and* new
    untracked files), honoring ``.gitignore`` the same way ``--exclude-standard`` would, then
    ``git write-tree`` persists a tree object in the object DB.

    Why a tree (not ``git stash create``): ``git stash create`` silently
    omits untracked files, so a phase whose only output is new files
    produced an empty diff. A worktree-snapshot tree captures them, and
    tree-vs-tree diffing (see :func:`capture_run_diff`) surfaces new
    files as ``new file`` sections.

    Returns the tree SHA, or ``None`` when git is unavailable or the
    snapshot fails. Never raises. The blobs/tree it writes are dangling
    objects, reclaimed by routine ``git gc``.
    """
    return snapshot_worktree_tree(git_root)


def snapshot_worktree(git_root: Path) -> str | None:
    """Capture an immutable tree-ish SHA for the current worktree state.

    Returns a tree object (via :func:`_snapshot_tree`) representing the
    full worktree — tracked edits **and** new untracked files — without
    touching any ref, branch, index, or working-tree state. The SHA is
    immutable: if a phase's runtime commits during the phase,
    ``git diff <returned_sha>`` still compares against the pre-phase
    state. Never returns the literal string ``"HEAD"``.

    Falls back to ``git rev-parse --verify HEAD`` only if the tree
    snapshot itself fails; a commit-ish is still a valid left-hand side
    for ``git diff``. Returns ``None`` when git is unavailable, the path
    is not a git root, or the repo has no commits yet. Caller resolves
    ``git_root`` via :func:`resolve_git_root` first; this helper does
    not re-resolve and does not accept a project cwd.

    Never raises.
    """
    if git_root is None:
        return None

    tree = _snapshot_tree(git_root)
    if tree:
        return tree

    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            timeout=30,
            env=git_env,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def capture_run_diff(
    project_path: Path,
    run_dir: Path,
    *,
    baseline_ref: str | None = None,
    patch_subpath: str = "diff.patch",
    emit_event: bool = True,
) -> Path | None:
    """Capture a project unified diff into ``<run_dir>/<patch_subpath>``.

    Both modes diff against a fresh worktree-snapshot tree (see
    :func:`_snapshot_tree`) so **new untracked files are included** —
    a plain ``git diff`` / ``git diff <ref>`` against the working tree
    only sees tracked-file changes, which silently dropped any file the
    phase created.

    Two modes:

    * **Cumulative** (``baseline_ref is None``) — strategy, first
      non-empty result wins:

      1. ``git diff --no-color HEAD <worktree-tree>`` for uncommitted
         changes, including new untracked files.
      2. ``git diff HEAD~1..HEAD --no-color`` when the run committed.
      3. ``git show --no-color HEAD`` for single-commit repositories.

    * **Baseline** (``baseline_ref`` set) — runs **only**
      ``git diff --no-color <baseline_ref> <worktree-tree>``. Empty
      output returns ``None``: a phase that didn't change files must
      produce no per-phase patch, not silently fall back to the
      cumulative diff under a phase-name path.

    ``patch_subpath`` is resolved against ``run_dir``; parent
    directories are auto-created. ``emit_event=False`` suppresses the
    ``artifact.created`` event for cases where the patch is internal
    transcript material (per-phase) rather than evidence-bundle
    content.

    Returns the artifact path, or ``None`` when git is unavailable or
    no diff was produced. Never raises.
    """
    git_root = resolve_git_root(project_path)
    if git_root is None:
        return None

    diff_path = run_dir / patch_subpath
    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    # Snapshot the current worktree (tracked + untracked) into a tree so
    # the diff endpoints are tree-vs-tree and new files are visible. When
    # the snapshot fails, degrade to a working-tree diff (tracked-only)
    # rather than producing nothing.
    current_tree = _snapshot_tree(git_root)

    # ``--binary`` makes binary changes emit applicable literal/delta hunks
    # ("GIT binary patch") instead of the inapplicable "Binary files ...
    # differ" marker, so the captured ``diff.patch`` round-trips through
    # ``git apply``.
    if baseline_ref is not None:
        if current_tree is not None:
            attempts = [["git", "diff", "--no-color", "--binary", baseline_ref, current_tree]]
        else:
            attempts = [["git", "diff", "--binary", baseline_ref, "--no-color"]]
    elif current_tree is not None:
        attempts = [
            ["git", "diff", "--no-color", "--binary", "HEAD", current_tree],
            ["git", "diff", "HEAD~1..HEAD", "--no-color", "--binary"],
            ["git", "show", "--no-color", "--binary", "HEAD"],
        ]
    else:
        attempts = [
            ["git", "diff", "--no-color", "--binary"],
            ["git", "diff", "HEAD~1..HEAD", "--no-color", "--binary"],
            ["git", "show", "--no-color", "--binary", "HEAD"],
        ]

    for cmd in attempts:
        try:
            result = subprocess.run(
                cmd,
                cwd=str(git_root),
                capture_output=True,
                # Capture raw bytes (no ``text=True``): git diff of Unity/game
                # repos has non-UTF8 bytes (binary .asset/.meta/textures,
                # latin1 configs). Decoding with errors="replace" rewrote those
                # bytes to U+FFFD and re-encoded to UTF-8, corrupting the patch
                # so ``git apply`` failed with "corrupt patch at line N". The
                # byte path is non-UTF8-safe by construction — it never decodes,
                # so it cannot raise UnicodeDecodeError — and preserves git's
                # exact bytes, including the trailing newline.
                timeout=30,
                env=git_env,
                check=False,
            )
            diff_bytes = result.stdout
            if diff_bytes:
                try:
                    diff_path.parent.mkdir(parents=True, exist_ok=True)
                    diff_path.write_bytes(diff_bytes)
                except OSError:
                    return None
                if emit_event:
                    _emit_diff_artifact(diff_path)
                return diff_path
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

    return None


def capture_and_render_run_diff(project_path: Path, run_dir: Path) -> str:
    """Capture ``diff.patch`` and return a compact preview for stdout."""
    path = capture_run_diff(project_path, run_dir)
    if path is None:
        return ""
    try:
        return render_diff_preview(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def capture_phase_diff(
    project_path: Path,
    run_dir: Path,
    *,
    baseline_ref: str,
    phase_name: str,
) -> tuple[str, tuple[str, ...]] | None:
    """Capture a per-phase diff against ``baseline_ref`` and parse it.

    Writes ``<run_dir>/phases/<phase_name>/diff.patch`` (no
    ``artifact.created`` event) and returns ``(preview, files)`` where
    ``files`` is the tuple of touched paths derived from
    :func:`parse_unified_diff` so renames/deletes use the parser's
    normalized ``FileDiff.path``.

    Returns ``None`` when git is unavailable or when the phase produced
    no diff against the baseline — never falls back to the cumulative
    strategies, so a quiet phase produces no per-phase artifact.
    """
    path = capture_run_diff(
        project_path,
        run_dir,
        baseline_ref=baseline_ref,
        patch_subpath=f"phases/{phase_name}/diff.patch",
        emit_event=False,
    )
    if path is None:
        return None
    try:
        diff_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    diffs = parse_unified_diff(diff_text)
    preview = render_diff_preview_from_diffs(diffs)
    files = tuple(d.path for d in diffs)
    return preview, files


def _emit_diff_artifact(path: Path) -> None:
    try:
        from core.observability import events as _events

        _events.emit(
            "artifact.created",
            path=str(path),
            artifact_kind="diff",
            size_bytes=path.stat().st_size,
        )
    except Exception:
        pass


def parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified-diff text into a list of :class:`FileDiff` sections.

    Section boundary: each ``diff --git `` line. As a fallback for diffs
    produced without ``diff --git`` headers (e.g. ``diff -u`` output), the
    first bare ``--- `` line opens a single section spanning to EOF.

    Each section's full text — including ``diff --git`` / ``index`` / mode
    markers, rename markers, ``---``/``+++`` headers, hunks, binary
    ``Binary files ... differ`` lines, and ``\\ No newline at end of file``
    trailers — is preserved in ``raw_lines`` with line endings intact.

    Parsing is **hunk-state-aware**: ``---``/``+++`` lines are treated as
    file-path headers only before the first ``@@`` hunk in a section.
    Once inside hunks, lines starting with ``---`` / ``+++`` are body
    content (e.g. a markdown change from ``-- old`` to ``++ new``
    appears in unified diff as ``--- old`` / ``+++ new``). Treating
    them as headers in the hunk zone would drop real content lines and
    can leak content text into the parsed paths.

    ``body_lines`` collects post-hunk lines (``@@`` headers, ``+`` and
    ``-`` lines including literal ``+++``/``---`` content, space-context,
    and ``\\ No newline`` trailers). Binary, mode-only, and pure-rename
    sections have an empty ``body_lines`` tuple but still appear in the
    result with their paths populated.
    """
    if not diff_text:
        return []

    raw_lines = diff_text.splitlines(keepends=True)

    section_starts: list[int] = [
        i for i, line in enumerate(raw_lines)
        if line.startswith("diff --git ")
    ]

    if not section_starts:
        for i, line in enumerate(raw_lines):
            if line.startswith("--- "):
                section_starts.append(i)
                break
        if not section_starts:
            return []

    section_starts.append(len(raw_lines))

    results: list[FileDiff] = []
    for start, end in zip(section_starts[:-1], section_starts[1:], strict=False):
        section = raw_lines[start:end]
        old_path, new_path = _extract_paths(section)
        path = new_path or old_path or "(unknown)"

        body: list[str] = []
        in_hunk = False
        for line in section:
            if line.startswith("@@"):
                in_hunk = True
                body.append(line)
                continue
            if not in_hunk:
                continue
            if line.startswith(("+", "-", " ", "\\")):
                body.append(line)

        results.append(
            FileDiff(
                path=path,
                old_path=old_path,
                new_path=new_path,
                raw_lines=tuple(section),
                body_lines=tuple(body),
            ),
        )
    return results


def file_stats(diff: FileDiff) -> tuple[int, int]:
    """Return ``(added, removed)`` line counts for a parsed section.

    Counts hunk body lines only (``body_lines`` already excludes
    ``+++``/``---`` headers). Binary and mode-only diffs return ``(0, 0)``.
    """
    added = sum(1 for line in diff.body_lines if line.startswith("+"))
    removed = sum(1 for line in diff.body_lines if line.startswith("-"))
    return added, removed


def render_diff_stat(diffs: list[FileDiff], *, color: bool = True) -> str:
    """Render a one-line-per-file stat table.

    ``api/payload.py        | +12 -3`` style. Path column is left-padded to
    the widest path so the ``+A -R`` columns align. Binary, mode-only, and
    pure-rename sections render with ``+0 -0`` — single contract, no
    separate ``binary`` rendering path.
    """
    if not diffs:
        return ""

    stats = [(d.path, *file_stats(d)) for d in diffs]
    width = max(len(path) for path, _, _ in stats)

    out: list[str] = []
    for path, added, removed in stats:
        added_s = paint(f"+{added}", C.GREEN, color=color)
        removed_s = paint(f"-{removed}", C.RED, color=color)
        out.append(f"{path.ljust(width)} | {added_s} {removed_s}\n")
    return "".join(out)


def filter_diffs_by_path(diffs: list[FileDiff], path: str) -> list[FileDiff]:
    """Filter parsed sections by a path string.

    Matches against the union of ``{d.path, d.old_path, d.new_path}`` so
    renames and deletes are findable by either name. Exact match first
    across the union; if zero results, falls back to prefix match
    (``candidate == path`` or ``candidate.startswith(path + "/")``).
    Trailing slash in ``path`` is normalized.

    Empty input list returns ``[]``; non-empty ``path`` that matches
    nothing also returns ``[]`` — the caller decides what to do with the
    empty result.
    """
    needle = path.rstrip("/")

    def candidates(d: FileDiff) -> tuple[str, ...]:
        return tuple(p for p in (d.path, d.old_path, d.new_path) if p)

    exact = [d for d in diffs if needle in candidates(d)]
    if exact:
        return exact

    prefix = needle + "/"
    return [
        d for d in diffs
        if any(c == needle or c.startswith(prefix) for c in candidates(d))
    ]


def assemble_patch(diffs: list[FileDiff]) -> str:
    """Reassemble a list of :class:`FileDiff` sections into raw patch text.

    Because :func:`parse_unified_diff` keeps line endings on every
    ``raw_lines`` entry, this is byte-identical to the original slice — a
    filtered subset is a *valid* unified patch (``diff --git`` / ``index``
    / mode / rename / ``---``/``+++`` / hunks / binary markers all
    present), so consumers can pipe it to ``git apply``.
    """
    return "".join(line for d in diffs for line in d.raw_lines)


def truncate_bytes(text: str, max_bytes: int | None) -> tuple[str, bool]:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes.

    Returns ``(possibly_truncated_text, was_truncated)``.

    - ``max_bytes is None`` → ``(text, False)`` (unlimited; no allocation).
    - ``max_bytes <= 0`` → raises ``ValueError``.
    - Otherwise encodes UTF-8, slices at the byte boundary, and decodes
      with ``errors="ignore"`` so any partial trailing multibyte sequence
      is dropped cleanly (no ``UnicodeDecodeError`` for callers).

    This helper does not append a user-visible footer; presentation
    layers do that using the ``truncated`` flag.
    """
    if max_bytes is None:
        return text, False
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive or None")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _extract_paths(section: list[str]) -> tuple[str | None, str | None]:
    """Return ``(old_path, new_path)`` for one ``FileDiff`` section.

    Hunk-state-aware: ``---``/``+++`` lines are interpreted as file-path
    headers only before the first ``@@`` hunk. Inside hunks they are
    body content (a markdown ``--- old heading`` removal would otherwise
    silently retarget the parsed path to ``old heading``).

    Authoritative source order:
      1. ``--- a/<path>`` / ``+++ b/<path>`` in the header zone (with
         ``/dev/null`` → ``None``).
      2. ``rename from`` / ``rename to`` for pure renames.
      3. Header-derived (``diff --git a/X b/Y``) when neither ``---`` nor
         ``+++`` is present — covers binary, mode-only, and pure-rename
         sections.
    """
    header_old: str | None = None
    header_new: str | None = None
    minus_seen = False
    plus_seen = False
    in_hunk = False
    old_path: str | None = None
    new_path: str | None = None

    for raw in section:
        line = raw.rstrip("\n").rstrip("\r")
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            continue
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                header_old = _strip_ab_prefix(parts[2])
                header_new = _strip_ab_prefix(parts[3])
        elif line.startswith("rename from "):
            if not minus_seen:
                old_path = line[len("rename from "):].strip() or None
        elif line.startswith("rename to "):
            if not plus_seen:
                new_path = line[len("rename to "):].strip() or None
        elif line.startswith("--- "):
            minus_seen = True
            p = line[4:].strip()
            old_path = None if p == "/dev/null" else _strip_ab_prefix(p)
        elif line.startswith("+++ "):
            plus_seen = True
            p = line[4:].strip()
            new_path = None if p == "/dev/null" else _strip_ab_prefix(p)

    if not minus_seen and old_path is None:
        old_path = header_old
    if not plus_seen and new_path is None:
        new_path = header_new

    return old_path, new_path


def _strip_ab_prefix(path: str) -> str | None:
    """Strip a leading ``a/`` or ``b/`` from a git diff path.

    ``/dev/null`` collapses to ``None`` so callers can use truthiness to
    detect pure adds/deletes.
    """
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _plural(n: int, word: str) -> str:
    return word if n == 1 else f"{word}s"


def _color_diff_line(line: str, *, color: bool = True) -> str:
    if line.startswith("+"):
        return paint(line, C.GREEN, color=color)
    if line.startswith("-"):
        return paint(line, C.RED, color=color)
    if line.startswith("@@"):
        return paint(line, C.CYAN, color=color)
    return line
