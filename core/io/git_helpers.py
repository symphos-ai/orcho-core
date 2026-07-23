"""
core/git_helpers.py — Tiny wrappers around git CLI used by the pipeline.

Extracted from orchestrator.py to keep the orchestrator focused on phase
sequencing rather than shell plumbing.

Worktree primitives (``create_worktree`` / ``remove_worktree`` /
``worktree_diff_against_base`` / ``apply_patch_to_checkout``) underlie
the GWT-1 isolated-worktree substrate (see ADR 0033). They follow the
"never raise on expected git failure; return a structured result"
discipline so callers in :mod:`pipeline.engine.worktree` can branch on
``ok`` without ``try``/``except``.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


def has_uncommitted(cwd: str) -> bool:
    """True if the working tree at ``cwd`` has uncommitted changes."""
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=cwd, capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def git_diff_stat(cwd: str) -> str:
    """``git diff --stat`` output for ``cwd`` (or '(no diff)' if clean)."""
    r = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=cwd, capture_output=True, text=True,
    )
    return r.stdout.strip() or "(no diff)"


def git_head(cwd: str | Path) -> str | None:
    """Resolved ``HEAD`` sha for ``cwd``, or ``None`` when unavailable.

    Returns the trimmed commit sha when ``git rev-parse HEAD`` succeeds with
    non-empty output. Anything else — ``cwd`` not a git repo, missing git
    binary, detached/unborn HEAD failure, timeout — collapses to ``None``
    without raising, so provenance capture stays best-effort.
    """
    rc, stdout, _stderr = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    if rc != 0:
        return None
    sha = stdout.strip()
    return sha or None


def git_committed_files_since(
    cwd: str | Path, base_ref: str, *, head_ref: str = "HEAD",
) -> list[str]:
    """Files changed by *commits* in ``base_ref..head_ref`` (not the working tree).

    This is the observable, durable companion-``committed`` signal: given a
    base revision recorded when the companion repo was first detected, it
    answers "did HEAD advance past that base, and which tracked paths did the
    new commits touch?". Distinct from :func:`git_changed_files`, which reports
    *uncommitted* working-tree state — here a clean working tree that has moved
    its HEAD forward still yields the committed paths.

    Returns paths relative to ``cwd``. Empty list when ``base_ref`` equals
    ``head_ref`` (no advance), when either ref is unknown, when ``cwd`` is not a
    git repo, or when git is unavailable — never raises, so companion
    classification degrades softly to ``planned_requirement``.
    """
    if not base_ref or not str(base_ref).strip():
        return []
    rc, stdout, _stderr = _run_git(
        ["diff", "--name-only", f"{base_ref}..{head_ref}"], cwd=cwd,
    )
    if rc != 0:
        return []
    return [line for line in stdout.splitlines() if line.strip()]


class GitStatusKind(StrEnum):
    """The working-tree change kinds relevant to write-scope matching."""

    MODIFIED = "modified"
    ADDED = "added"
    UNTRACKED = "untracked"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"


@dataclass(frozen=True, slots=True)
class GitStatusRecord:
    """One exact change from ``git status --porcelain=v1 -z``.

    ``path`` is the current path except for deletions, where it is the removed
    path.  Rename and copy records additionally retain their source in
    ``old_path``.  Git emits destination first in porcelain v1's NUL format.
    """

    kind: GitStatusKind
    path: str
    old_path: str | None = None

    @property
    def scope_identities(self) -> tuple[str, ...]:
        """Exact paths that must each be considered for declared write scope."""
        if self.old_path is not None:
            return (self.old_path, self.path)
        return (self.path,)


class GitStatusParseError(ValueError):
    """A successful git invocation returned invalid porcelain v1 bytes."""


_PORCELAIN_CODES = frozenset({b" ", b"M", b"A", b"D", b"R", b"C", b"T", b"U", b"?"})


def _parse_git_status_porcelain(output: bytes) -> tuple[GitStatusRecord, ...]:
    """Parse exact NUL-delimited porcelain v1 output.

    This deliberately parses bytes rather than Git's human/display format:
    paths are decoded with :func:`os.fsdecode`, without unquoting or splitting
    an arrow-like substring.  A malformed successful response is a contract
    violation, not evidence of a clean working tree.
    """
    if not isinstance(output, bytes):
        raise GitStatusParseError("git status stdout must be bytes")
    if not output:
        return ()

    fields = output.split(b"\0")
    if fields.pop() != b"":
        raise GitStatusParseError("porcelain record is not NUL-terminated")

    records: list[GitStatusRecord] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2:3] != b" ":
            raise GitStatusParseError(f"malformed porcelain record: {field!r}")
        x, y = field[:1], field[1:2]
        if x not in _PORCELAIN_CODES or y not in _PORCELAIN_CODES:
            raise GitStatusParseError(f"unknown porcelain status: {field[:2]!r}")
        if (x == b"?") != (y == b"?"):
            raise GitStatusParseError(f"malformed untracked status: {field[:2]!r}")
        path_bytes = field[3:]
        if not path_bytes:
            raise GitStatusParseError("porcelain record has an empty path")

        if x == y == b"?":
            kind = GitStatusKind.UNTRACKED
        elif b"R" in (x, y):
            kind = GitStatusKind.RENAMED
        elif b"C" in (x, y):
            kind = GitStatusKind.COPIED
        elif b"D" in (x, y):
            kind = GitStatusKind.DELETED
        elif b"A" in (x, y):
            kind = GitStatusKind.ADDED
        else:
            # M, T, and U all represent a path that remains in the worktree.
            kind = GitStatusKind.MODIFIED

        old_path: str | None = None
        if kind in (GitStatusKind.RENAMED, GitStatusKind.COPIED):
            if index == len(fields):
                raise GitStatusParseError("truncated rename/copy porcelain record")
            old_path_bytes = fields[index]
            index += 1
            if not old_path_bytes:
                raise GitStatusParseError("rename/copy porcelain record has an empty source")
            old_path = os.fsdecode(old_path_bytes)
        records.append(
            GitStatusRecord(kind=kind, path=os.fsdecode(path_bytes), old_path=old_path),
        )
    return tuple(records)


def git_changed_file_records(cwd: str | Path) -> tuple[GitStatusRecord, ...]:
    """Collect exact working-tree changes, degrading only on invocation failure."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(cwd), capture_output=True, check=False, timeout=30.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    return _parse_git_status_porcelain(result.stdout)


def git_changed_files(cwd: str | Path) -> list[str]:
    """Stable, de-duplicated scope identities from exact status records.

    Adds, modifications, and untracked files contribute their current path;
    deletions contribute their removed path; renames and copies contribute both
    source and destination.  This is intentionally the sole list projection.
    """
    identities: dict[str, None] = {}
    for record in git_changed_file_records(cwd):
        for path in record.scope_identities:
            identities.setdefault(path, None)
    return list(identities)


# ---------------------------------------------------------------------------
# Worktree primitives (GWT-1 / ADR 0033)
#
# Single, shared ``Result`` shape across the four operations so callers
# don't need four exception types or four ad-hoc tuples. ``ok`` is the
# branch flag, ``error`` carries git's stderr verbatim on failure
# (never invented — keep diagnostic surface authentic), ``path`` /
# ``branch`` carry op-specific data when applicable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitOpResult:
    """Outcome of a worktree-related git operation.

    The pipeline engine prefers this over exceptions: expected failures
    (worktree already exists, base ref unknown, patch conflict on apply)
    are not crashes, they are decisions the engine makes routing logic
    against. Unexpected failures (git binary missing, OSError on path)
    surface as ``ok=False`` with the OS-level error in ``error`` — still
    no raise. Truly fatal cases (caller passed a non-Path object) are
    the only path where the helper raises, signalling a bug in the
    caller, not a git outcome.
    """

    ok: bool
    error: str | None = None
    path: Path | None = None
    branch: str | None = None


def _run_git(
    args: list[str], *, cwd: str | Path | None = None, timeout_s: float = 30.0,
) -> tuple[int, str, str]:
    """Run a git command, never raising on git-side failure.

    Returns ``(returncode, stdout, stderr)``. OS-level failures
    (missing git binary, unreadable cwd) collapse to
    ``(-1, "", str(exc))`` so callers can treat them like any other
    non-zero exit. Timeout collapses to
    ``(-2, "", "git timed out after {N}s")``.
    """
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except FileNotFoundError as e:
        return -1, "", f"git binary not found: {e}"
    except OSError as e:
        return -1, "", f"git invocation failed: {e}"
    except subprocess.TimeoutExpired:
        return -2, "", f"git timed out after {timeout_s}s"
    return r.returncode, r.stdout, r.stderr


def create_worktree(
    *,
    repo: str | Path,
    base_ref: str,
    target_path: str | Path,
    branch_name: str | None = None,
) -> GitOpResult:
    """Create a new git worktree rooted at ``target_path`` from ``base_ref``.

    The worktree shares the source repo's object database (cheap)
    but materialises an independent working tree at ``target_path``.
    Mutations inside the new worktree do NOT affect ``repo``'s
    checkout — that is the isolation guarantee GWT-1 builds on.

    When ``branch_name`` is given, the worktree is created on a new
    branch of that name pointing at ``base_ref``. Without it the
    worktree enters detached-HEAD mode at ``base_ref`` (suitable for
    short-lived inspection worktrees where no commits will land).

    Returns a :class:`GitOpResult`:
      * ``ok=True``, ``path=Path(target_path)``,
        ``branch=branch_name or None``  → success.
      * ``ok=False``, ``error=<git stderr>``                    → failure
        (target already exists, base_ref unknown, branch_name already
        in use, etc.). ``path`` is None.
    """
    target = Path(target_path)
    if target.exists():
        return GitOpResult(
            ok=False,
            error=f"target_path already exists: {target}",
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    args = ["worktree", "add"]
    if branch_name:
        # ``-b <branch>`` creates the branch at <base_ref> as part of
        # the worktree add — single git invocation, atomic on the
        # branch-creation side.
        args += ["-b", branch_name, str(target), base_ref]
    else:
        args += ["--detach", str(target), base_ref]

    rc, _stdout, stderr = _run_git(args, cwd=str(repo))
    if rc != 0:
        return GitOpResult(ok=False, error=stderr.strip() or f"rc={rc}")
    return GitOpResult(ok=True, path=target, branch=branch_name)


def remove_worktree(
    target_path: str | Path,
    *,
    repo: str | Path | None = None,
    force: bool = True,
) -> GitOpResult:
    """Remove a git worktree previously created via :func:`create_worktree`.

    ``force=True`` (default) removes the worktree even if it has
    uncommitted changes — the expected state for orcho-managed
    worktrees during teardown, where the run-owned diff is captured
    elsewhere (evidence bundle, sync-back) before removal. ``force=
    False`` lets the caller demand a clean teardown for audit
    scenarios.

    When ``repo`` is None, runs ``git worktree remove`` from inside
    the target (it works either way because git resolves the
    parent repo from the worktree's ``.git`` file). Pass ``repo``
    explicitly when teardown happens after the target has already
    been ``rmtree``'d from disk and you want git's bookkeeping cleaned
    up via ``git worktree prune`` semantics.
    """
    target = Path(target_path)
    cwd = str(repo) if repo is not None else (str(target) if target.exists() else None)
    if cwd is None:
        # Target gone AND no source repo passed: nothing we can do
        # via git CLI. Best-effort succeed (the bookkeeping will be
        # cleaned up by a later ``git worktree prune`` anywhere in
        # the source repo).
        return GitOpResult(
            ok=True,
            error="target already absent; left git bookkeeping to next prune",
        )

    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    rc, _stdout, stderr = _run_git(args, cwd=cwd)
    if rc != 0:
        return GitOpResult(ok=False, error=stderr.strip() or f"rc={rc}")
    return GitOpResult(ok=True)


def worktree_diff_against_base(
    target_path: str | Path, *, base_ref: str = "HEAD",
) -> str:
    """Unified diff between ``base_ref`` and the worktree's current state.

    Mirrors ``git_diff_stat`` in shape ("no diff" sentinel on clean,
    never raises) but emits the full patch suitable for ``git apply``
    sync-back rather than a stat. The base ref defaults to the
    worktree's HEAD — i.e. "what has the run added on top of where it
    started" — which is the input the sync-back step (see ADR 0032
    commit-decision gate) feeds to ``apply_patch_to_checkout``.

    Untracked files are NOT included by default — ``git diff`` doesn't
    surface them. Callers that need a full snapshot use ``git status
    --porcelain`` in parallel; this helper stays focused on the
    diffable patch.
    """
    rc, stdout, _stderr = _run_git(
        ["diff", base_ref], cwd=str(target_path),
    )
    if rc != 0:
        # On failure, return the marker string so callers that only
        # inspect content stay defensive. Caller that needs to branch
        # on success should use ``create_worktree`` + later helpers
        # that return ``GitOpResult``.
        return "(diff unavailable)"
    # Preserve git's exact bytes — including the trailing newline —
    # so the output round-trips through ``apply_patch_to_checkout``.
    # Stripping would silently corrupt the patch (``git apply`` errors
    # with ``corrupt patch at line N`` when the final newline is gone).
    # Empty-diff detection is done before strip so the marker still
    # surfaces cleanly for human-readable callers.
    if not stdout.strip():
        return "(no diff)"
    return stdout


def apply_patch_to_checkout(
    checkout_path: str | Path, patch_text: str, *, check_only: bool = False,
) -> GitOpResult:
    """Apply a unified diff to ``checkout_path``.

    Used by the commit-decision sync-back step (ADR 0032 PR2) to
    transport a run-owned diff from the orcho-managed worktree to
    the user's source checkout. The orcho worktree is the
    authoritative source of the diff; this helper is the receiving
    side.

    ``check_only=True`` runs ``git apply --check`` — verifies the
    patch would apply cleanly without mutating the checkout. The
    UI uses this to gate the "approve sync-back" button.

    On clean apply (or clean check): ``ok=True``.
    On rejection (merge conflict, unknown path, malformed patch):
    ``ok=False`` with git's stderr verbatim in ``error`` — operator
    surfaces the message to the user, no auto-fixup attempted.

    Empty / whitespace-only patch is a no-op success (mirrors
    ``git apply``'s own behaviour with no hunks).
    """
    if not patch_text or not patch_text.strip():
        return GitOpResult(ok=True)

    args = ["apply"]
    if check_only:
        args.append("--check")
    args.append("-")  # read from stdin

    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(checkout_path),
            input=patch_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
        )
    except FileNotFoundError as e:
        return GitOpResult(ok=False, error=f"git binary not found: {e}")
    except OSError as e:
        return GitOpResult(ok=False, error=f"git invocation failed: {e}")
    except subprocess.TimeoutExpired:
        return GitOpResult(ok=False, error="git apply timed out after 30s")

    if r.returncode != 0:
        return GitOpResult(
            ok=False, error=r.stderr.strip() or f"rc={r.returncode}",
        )
    return GitOpResult(ok=True)


__all__ = [
    "GitOpResult",
    "GitStatusKind",
    "GitStatusParseError",
    "GitStatusRecord",
    "apply_patch_to_checkout",
    "create_worktree",
    "git_changed_files",
    "git_changed_file_records",
    "git_committed_files_since",
    "git_diff_stat",
    "git_head",
    "has_uncommitted",
    "remove_worktree",
    "worktree_diff_against_base",
]
