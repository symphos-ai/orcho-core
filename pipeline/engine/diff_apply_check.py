"""Apply-check helpers for persisted run diff artifacts.

The run-level ``diff.patch`` is an artifact on disk, so validation must
check that exact file against the baseline tree used to produce it. This
module keeps that check isolated from finalization and from the diff
capture/parsing helpers.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pipeline.engine.run_diff import capture_run_diff, resolve_git_root

DiffApplyCheckStatus = Literal["pass", "fail", "degraded"]

#: Durable triad projected from a :class:`DiffApplyCheckResult`. This is the
#: single source of truth for the run-level ``diff_patch.status`` that both
#: delivery and the read surfaces (CLI / SDK) consume — keep the mapping in
#: :func:`diff_patch_triad` so no surface re-derives the strings inline.
DiffPatchTriad = Literal[
    "patch_valid", "patch_invalid", "patch_missing", "patch_unknown"
]

#: ``degraded`` reasons that mean the artifact itself is absent/unreadable —
#: these project to ``patch_missing`` rather than ``patch_unknown``.
_PATCH_MISSING_REASONS = frozenset({"patch_unavailable", "patch_unreadable"})

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_OUTPUT_BYTES = 4096
_RUN_LEVEL_DIFF_PATCH = "diff.patch"


@dataclass(frozen=True, slots=True)
class DiffApplyCheckResult:
    """Result of checking whether a saved patch applies to a baseline tree."""

    status: DiffApplyCheckStatus
    reason: str
    cwd: str | None
    patch_path: str
    baseline_ref: str | None
    command: tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""
    detail: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def to_metadata(self) -> dict[str, object]:
        """Return JSON-friendly artifact metadata for evidence events."""
        return {
            "status": self.status,
            "reason": self.reason,
            "cwd": self.cwd,
            "patch_path": self.patch_path,
            "baseline_ref": self.baseline_ref,
            "command": list(self.command),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "detail": self.detail,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
        }


@dataclass(frozen=True, slots=True)
class CapturedRunDiff:
    """A captured run-level ``diff.patch`` plus its apply-check result.

    ``capture_run_diff_with_apply_check`` returns this so the caller keeps the
    computed :class:`DiffApplyCheckResult` instead of discarding it — the
    evidence event already carries it, but finalization needs it to persist
    the durable ``diff_patch`` block in ``meta.json``. ``apply_check`` is
    ``None`` only for per-phase (non run-level) captures, which are not
    apply-checked.
    """

    path: Path
    apply_check: DiffApplyCheckResult | None


def diff_patch_triad(apply_check: DiffApplyCheckResult | None) -> DiffPatchTriad:
    """Project a :class:`DiffApplyCheckResult` onto the durable triad string.

    Single source of truth for the run-level ``diff_patch.status`` consumed by
    finalization (durable meta), delivery decisions, and read surfaces:

    * ``pass`` → ``patch_valid``
    * ``fail`` → ``patch_invalid``
    * ``degraded`` whose reason is ``patch_unavailable`` / ``patch_unreadable``
      → ``patch_missing`` (the artifact is absent or unreadable)
    * any other ``degraded`` (or a missing result) → ``patch_unknown``
    """
    if apply_check is None:
        return "patch_missing"
    if apply_check.status == "pass":
        return "patch_valid"
    if apply_check.status == "fail":
        return "patch_invalid"
    if apply_check.reason in _PATCH_MISSING_REASONS:
        return "patch_missing"
    return "patch_unknown"


def diff_patch_durable_block(apply_check: DiffApplyCheckResult) -> dict[str, object]:
    """Build the compact, durable ``diff_patch`` block for ``meta.json``.

    Carries only the operator-actionable fields — the triad status plus the
    raw ``reason`` / ``patch_path`` / ``baseline_ref`` / ``detail`` — and not
    the verbose stdout/stderr, which stay in the evidence event.
    """
    return {
        "status": diff_patch_triad(apply_check),
        "reason": apply_check.reason,
        "patch_path": apply_check.patch_path,
        "baseline_ref": apply_check.baseline_ref,
        "detail": apply_check.detail,
    }


@dataclass(frozen=True, slots=True)
class _GitCommandResult:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    detail: str = ""
    unavailable_reason: str | None = None


def capture_run_diff_with_apply_check(
    project_path: str | Path,
    run_dir: str | Path,
    *,
    baseline_ref: str | None = None,
    patch_subpath: str = _RUN_LEVEL_DIFF_PATCH,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> CapturedRunDiff | None:
    """Capture a run diff and emit an artifact event with apply-check metadata.

    Returns a :class:`CapturedRunDiff` carrying both the artifact path and the
    computed :class:`DiffApplyCheckResult` (``None`` for non run-level
    captures), so the caller can persist the durable ``diff_patch`` block
    rather than re-reading it from the evidence event. Returns ``None`` when no
    diff was produced.
    """
    diff_path = capture_run_diff(
        Path(project_path),
        Path(run_dir),
        baseline_ref=baseline_ref,
        patch_subpath=patch_subpath,
        emit_event=False,
    )
    if diff_path is None:
        return None

    apply_check: DiffApplyCheckResult | None = None
    if patch_subpath == _RUN_LEVEL_DIFF_PATCH:
        apply_check = check_diff_patch_apply(
            project_path,
            patch_path=diff_path,
            baseline_ref=baseline_ref,
            timeout=timeout,
        )
    _emit_diff_artifact(diff_path, apply_check=apply_check)
    return CapturedRunDiff(path=diff_path, apply_check=apply_check)


def check_diff_patch_apply(
    project_path: str | Path | None = None,
    *,
    git_root: str | Path | None = None,
    patch_path: str | Path,
    baseline_ref: str | None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = _DEFAULT_OUTPUT_BYTES,
) -> DiffApplyCheckResult:
    """Check a saved ``diff.patch`` against ``baseline_ref`` without mutation.

    The check uses a temporary ``GIT_INDEX_FILE`` and runs:

    1. ``git read-tree <baseline_ref>`` to load the baseline tree into the
       temporary index.
    2. ``git apply --check --cached <patch_path>`` against that temporary
       index.

    Missing inputs or unavailable baseline context return ``degraded``.
    A patch that Git can evaluate but cannot apply returns ``fail``.
    """

    patch = Path(patch_path)
    patch_display = _display_path(patch)

    if not patch.is_file():
        return _make_result(
            "degraded",
            "patch_unavailable",
            cwd=None,
            patch_path=patch_display,
            baseline_ref=baseline_ref,
            detail="patch file is missing or is not a regular file",
            max_output_bytes=max_output_bytes,
        )

    try:
        with patch.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return _make_result(
            "degraded",
            "patch_unreadable",
            cwd=None,
            patch_path=patch_display,
            baseline_ref=baseline_ref,
            detail=str(exc),
            max_output_bytes=max_output_bytes,
        )

    baseline = baseline_ref.strip() if baseline_ref else ""
    if not baseline:
        return _make_result(
            "degraded",
            "baseline_unavailable",
            cwd=None,
            patch_path=patch_display,
            baseline_ref=baseline_ref,
            detail="baseline ref is empty",
            max_output_bytes=max_output_bytes,
        )

    resolved_git_root = _resolve_root(project_path=project_path, git_root=git_root)
    if resolved_git_root is None:
        return _make_result(
            "degraded",
            "git_root_unavailable",
            cwd=None,
            patch_path=patch_display,
            baseline_ref=baseline,
            detail="git root could not be resolved",
            max_output_bytes=max_output_bytes,
        )

    cwd = str(resolved_git_root)
    read_tree_cmd = ("git", "read-tree", baseline)
    apply_cmd = ("git", "apply", "--check", "--cached", patch_display)

    with tempfile.TemporaryDirectory(prefix="orcho-apply-check-") as tmp_dir:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_INDEX_FILE": os.path.join(tmp_dir, "index"),
        }

        read_tree = _run_git_command(read_tree_cmd, cwd=cwd, env=env, timeout=timeout)
        if read_tree.returncode is None:
            reason = (
                "git_unavailable"
                if read_tree.unavailable_reason == "git_unavailable"
                else "baseline_unavailable"
            )
            return _make_result(
                "degraded",
                reason,
                cwd=cwd,
                patch_path=patch_display,
                baseline_ref=baseline,
                command=read_tree_cmd,
                stdout=read_tree.stdout,
                stderr=read_tree.stderr,
                detail=read_tree.detail,
                max_output_bytes=max_output_bytes,
            )

        if read_tree.returncode != 0:
            return _make_result(
                "degraded",
                "baseline_unavailable",
                cwd=cwd,
                patch_path=patch_display,
                baseline_ref=baseline,
                command=read_tree_cmd,
                stdout=read_tree.stdout,
                stderr=read_tree.stderr,
                detail=f"git read-tree exited with {read_tree.returncode}",
                max_output_bytes=max_output_bytes,
            )

        applied = _run_git_command(apply_cmd, cwd=cwd, env=env, timeout=timeout)
        if applied.returncode is None:
            reason = (
                "git_unavailable"
                if applied.unavailable_reason == "git_unavailable"
                else "apply_check_unavailable"
            )
            return _make_result(
                "degraded",
                reason,
                cwd=cwd,
                patch_path=patch_display,
                baseline_ref=baseline,
                command=apply_cmd,
                stdout=applied.stdout,
                stderr=applied.stderr,
                detail=applied.detail,
                max_output_bytes=max_output_bytes,
            )

        if applied.returncode != 0:
            return _make_result(
                "fail",
                "patch_does_not_apply",
                cwd=cwd,
                patch_path=patch_display,
                baseline_ref=baseline,
                command=apply_cmd,
                stdout=applied.stdout,
                stderr=applied.stderr,
                detail=f"git apply --check exited with {applied.returncode}",
                max_output_bytes=max_output_bytes,
            )

        return _make_result(
            "pass",
            "patch_applies",
            cwd=cwd,
            patch_path=patch_display,
            baseline_ref=baseline,
            command=apply_cmd,
            stdout=applied.stdout,
            stderr=applied.stderr,
            max_output_bytes=max_output_bytes,
        )


def _resolve_root(
    *,
    project_path: str | Path | None,
    git_root: str | Path | None,
) -> Path | None:
    if git_root is not None:
        root = Path(git_root)
        return root if root.exists() else None
    if project_path is None:
        return None
    return resolve_git_root(Path(project_path))


def _emit_diff_artifact(
    path: Path,
    *,
    apply_check: DiffApplyCheckResult | None = None,
) -> None:
    try:
        payload: dict[str, object] = {
            "path": str(path),
            "artifact_kind": "diff",
            "size_bytes": path.stat().st_size,
        }
        if apply_check is not None:
            payload["apply_check"] = apply_check.to_metadata()
        from core.observability import events as _events

        _events.emit("artifact.created", **payload)
    except Exception:
        pass


def _run_git_command(
    command: tuple[str, ...],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
) -> _GitCommandResult:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _GitCommandResult(
            returncode=None,
            stdout=_coerce_output(exc.stdout),
            stderr=_coerce_output(exc.stderr),
            detail=f"{_command_label(command)} timed out after {timeout:g}s",
            unavailable_reason="timeout",
        )
    except FileNotFoundError as exc:
        return _GitCommandResult(
            returncode=None,
            detail=str(exc),
            unavailable_reason="git_unavailable",
        )
    except OSError as exc:
        return _GitCommandResult(
            returncode=None,
            detail=str(exc),
            unavailable_reason="os_error",
        )

    return _GitCommandResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _make_result(
    status: DiffApplyCheckStatus,
    reason: str,
    *,
    cwd: str | None,
    patch_path: str,
    baseline_ref: str | None,
    command: tuple[str, ...] = (),
    stdout: str = "",
    stderr: str = "",
    detail: str = "",
    max_output_bytes: int,
) -> DiffApplyCheckResult:
    bounded_stdout, stdout_truncated = _bound_text(stdout, max_output_bytes)
    bounded_stderr, stderr_truncated = _bound_text(stderr, max_output_bytes)
    return DiffApplyCheckResult(
        status=status,
        reason=reason,
        cwd=cwd,
        patch_path=patch_path,
        baseline_ref=baseline_ref,
        command=command,
        stdout=bounded_stdout,
        stderr=bounded_stderr,
        detail=detail,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _bound_text(text: str, max_bytes: int) -> tuple[str, bool]:
    if max_bytes <= 0:
        return "", bool(text)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _command_label(command: tuple[str, ...]) -> str:
    if len(command) >= 2:
        return " ".join(command[:2])
    return " ".join(command) or "command"


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path)
