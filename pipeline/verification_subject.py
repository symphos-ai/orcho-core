"""Git-backed, fail-closed verification subject identities.

This module deliberately has no dependency on the pipeline engine or receipt
layers.  It is the one owner of the temporary-index snapshot operation used by
verification and by run-diff capture.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VerificationSubjectIdentity:
    """An immutable description of the exact checkout content observed."""

    version: int
    object_format: str
    tree_oid: str
    observed_head_oid: str
    baseline_oid: str | None


@dataclass(frozen=True, slots=True)
class VerificationSubjectAvailable:
    identity: VerificationSubjectIdentity


@dataclass(frozen=True, slots=True)
class VerificationSubjectUnavailable:
    """A typed, non-throwing failure to observe a verification subject."""

    reason: str


VerificationSubjectCapture = VerificationSubjectAvailable | VerificationSubjectUnavailable


class VerificationSubjectComparisonVerdict(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class VerificationSubjectComparison:
    verdict: VerificationSubjectComparisonVerdict
    reason: str

    @property
    def is_fresh(self) -> bool:
        return self.verdict is VerificationSubjectComparisonVerdict.FRESH


def capture_verification_subject(
    checkout: Path,
    *,
    baseline_ref: str | None = None,
) -> VerificationSubjectCapture:
    """Capture a complete, directly-comparable identity for ``checkout``.

    Git failures, missing repositories, invalid baselines, and dirty nested
    submodules are observations that cannot safely prove freshness, not
    exceptions.  All such cases therefore return ``VerificationSubjectUnavailable``.
    """
    root = _git_root(checkout)
    if root is None:
        return VerificationSubjectUnavailable("git_repository_unavailable")
    object_format = _git_text(root, "rev-parse", "--show-object-format")
    observed_head_oid = _git_text(root, "rev-parse", "--verify", "HEAD")
    if not object_format or not observed_head_oid:
        return VerificationSubjectUnavailable("head_or_object_format_unavailable")
    baseline_oid = None
    if baseline_ref is not None:
        baseline_oid = _git_text(root, "rev-parse", "--verify", baseline_ref)
        if not baseline_oid:
            return VerificationSubjectUnavailable("baseline_unavailable")
    if _has_dirty_submodule(root):
        return VerificationSubjectUnavailable("dirty_submodule_unrepresentable")
    tree_oid = snapshot_worktree_tree(root)
    if tree_oid is None:
        return VerificationSubjectUnavailable("worktree_snapshot_unavailable")
    identity = VerificationSubjectIdentity(
        version=1,
        object_format=object_format,
        tree_oid=tree_oid,
        observed_head_oid=observed_head_oid,
        baseline_oid=baseline_oid,
    )
    if not is_usable_verification_subject(identity):
        return VerificationSubjectUnavailable("malformed_git_identity")
    return VerificationSubjectAvailable(identity)


def compare_verification_subjects(
    recorded: VerificationSubjectIdentity | None,
    current: VerificationSubjectIdentity | None,
) -> VerificationSubjectComparison:
    """Directly compare identities; baseline is provenance, never content."""
    if not is_usable_verification_subject(recorded) or not is_usable_verification_subject(current):
        return VerificationSubjectComparison(
            VerificationSubjectComparisonVerdict.UNVERIFIABLE,
            "usable_subject_identity_unavailable",
        )
    assert recorded is not None and current is not None
    if recorded.object_format != current.object_format:
        return VerificationSubjectComparison(VerificationSubjectComparisonVerdict.STALE, "object_format_changed")
    if recorded.observed_head_oid != current.observed_head_oid:
        return VerificationSubjectComparison(VerificationSubjectComparisonVerdict.STALE, "observed_head_changed")
    if recorded.tree_oid != current.tree_oid:
        return VerificationSubjectComparison(VerificationSubjectComparisonVerdict.STALE, "worktree_tree_changed")
    return VerificationSubjectComparison(VerificationSubjectComparisonVerdict.FRESH, "subject_identity_matches")


def is_usable_verification_subject(subject: VerificationSubjectIdentity | None) -> bool:
    """Return whether a value can be used as a direct freshness proof."""
    if (
        not isinstance(subject, VerificationSubjectIdentity)
        or subject.version != 1
        or not isinstance(subject.object_format, str)
        or not isinstance(subject.tree_oid, str)
        or not isinstance(subject.observed_head_oid, str)
        or subject.baseline_oid is not None and not isinstance(subject.baseline_oid, str)
    ):
        return False
    oid_length = 64 if subject.object_format == "sha256" else 40 if subject.object_format == "sha1" else 0
    if not oid_length:
        return False
    required = (subject.tree_oid, subject.observed_head_oid)
    return all(len(oid) == oid_length and all(c in "0123456789abcdef" for c in oid) for oid in required) and (
        subject.baseline_oid is None
        or len(subject.baseline_oid) == oid_length
        and all(c in "0123456789abcdef" for c in subject.baseline_oid)
    )


def snapshot_worktree_tree(git_root: Path) -> str | None:
    """Write a tree for the full worktree using an isolated temporary index.

    The index begins from ``HEAD`` so ignored paths which are nevertheless
    tracked remain represented, then ``git add -A`` overlays every working-tree
    edit and non-ignored untracked file.  This can create unreachable Git
    objects but never changes refs, HEAD, the real index, or the worktree.
    """
    if git_root is None:
        return None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="orcho-idx-")
    except OSError:
        return None
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_INDEX_FILE": str(Path(tmp_dir) / "index")}
    try:
        # A repository without commits cannot seed from HEAD; an empty index is
        # correct in that special case.
        head = _run_git(git_root, ("rev-parse", "--verify", "HEAD"), env=env)
        if head is None:
            return None
        if head.returncode == 0:
            seeded = _run_git(git_root, ("read-tree", "HEAD"), env=env)
            if seeded is None or seeded.returncode != 0:
                return None
        added = _run_git(git_root, ("add", "-A"), env=env)
        if added is None or added.returncode != 0:
            return None
        written = _run_git(git_root, ("write-tree",), env=env)
        if written is None or written.returncode != 0:
            return None
        return written.stdout.strip() or None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _git_root(checkout: Path) -> Path | None:
    if not checkout or not checkout.exists():
        return None
    root = _git_text(checkout, "rev-parse", "--show-toplevel")
    return Path(root) if root else None


def _has_dirty_submodule(git_root: Path) -> bool:
    """Return whether a tracked gitlink has a dirty status entry.

    Both commands deliberately use NUL-delimited output.  Porcelain's default
    C-style quoting would otherwise turn a non-ASCII submodule pathname into a
    string that cannot be compared to the index pathname, allowing its dirty
    nested worktree to be represented by an unchanged gitlink.
    """
    status = _run_git(
        git_root,
        ("status", "--porcelain=v1", "-z", "--ignore-submodules=none"),
    )
    entries = _run_git(git_root, ("ls-files", "-s", "-z"))
    if (
        status is None
        or status.returncode != 0
        or entries is None
        or entries.returncode != 0
    ):
        return True

    dirty_paths = {
        entry[3:]
        for entry in status.stdout.split("\0")
        if len(entry) >= 4 and entry[2] == " "
    }
    submodule_paths = {
        entry.partition("\t")[2]
        for entry in entries.stdout.split("\0")
        if entry.startswith("160000 ") and "\t" in entry
    }
    return bool(dirty_paths & submodule_paths)


def _git_text(cwd: Path, *args: str) -> str | None:
    result = _run_git(cwd, args)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _run_git(
    cwd: Path,
    args: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=30, env=env or {**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
