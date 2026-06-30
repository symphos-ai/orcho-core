"""Read-only SDK helper for run-level git diff artifacts.

Capture is the pipeline's job — :func:`pipeline.engine.run_diff.capture_run_diff`
writes ``<run_dir>/diff.patch`` at run lifecycle time. This module is the
read side: it reads that artifact and returns a typed
:class:`RunDiffRecord` for CLI, MCP, and embedder consumption.

The split is intentional. Viewers (``orcho diff``, ``orcho_run_diff``,
``orcho evidence --diff``) must never recompute git state; they only render
what was captured. Missing artifact is a soft "not found" signal
(``found=False``), not an exception — the run may have been clean, or
predate the artifact's introduction.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pipeline.engine.run_diff import (
    FileDiff,
    assemble_patch,
    file_stats,
    filter_diffs_by_path,
    parse_unified_diff,
    render_diff_preview_from_diffs,
    render_diff_stat,
    truncate_bytes,
)
from sdk.runs import _CWD_DEFAULT, find_run, load_meta

_VALID_MODES = ("preview", "stat", "full")

# Read-side advisory prefix for a durable patch-integrity warning. CLI output
# is not wire, so a human-readable note is allowed; the prefix lets the CLI
# route the note to stderr (keeping a piped ``full`` patch byte-clean). The
# triad statuses that warrant a warning — a corrupt or absent captured patch.
_PATCH_INTEGRITY_PREFIX = "patch integrity"
_PATCH_INTEGRITY_STATUSES = frozenset({"patch_invalid", "patch_missing"})


def _patch_integrity_note(run_dir: Path) -> str | None:
    """Advisory note when the durable ``meta.diff_patch`` block flags trouble.

    Reads the durable apply-check block persisted at finalization (the same
    source delivery consults) and returns a human-readable warning — carrying
    the triad status, recorded reason, and patch path — when the captured run
    patch is ``patch_invalid`` or ``patch_missing``. Returns ``None`` for a
    valid patch, an absent block, or any non-actionable status. Never raises;
    this is a read-only advisory and does not add a wire field (it rides on the
    existing ``RunDiffRecord.message``).
    """
    meta = load_meta(run_dir)
    block = meta.get("diff_patch") if isinstance(meta, dict) else None
    if not isinstance(block, dict):
        return None
    status = block.get("status")
    if status not in _PATCH_INTEGRITY_STATUSES:
        return None
    reason = block.get("reason") or "unknown"
    patch_path = block.get("patch_path") or str(run_dir / "diff.patch")
    return (
        f"{_PATCH_INTEGRITY_PREFIX}: {status} — {reason} "
        f"(patch: {patch_path}); recover from worktree or rerun"
    )


@dataclass(frozen=True, slots=True)
class RunDiffFileRecord:
    """Per-file summary in a :class:`RunDiffRecord`.

    ``path`` is the display path — for renames this is the new name, for
    pure deletes it's the old name. Old/new path detail stays in the
    engine ``FileDiff`` and is not yet exposed on this record (deferred
    until rename-aware UX has a real consumer).
    """

    path: str
    added: int
    removed: int


@dataclass(frozen=True, slots=True)
class RunDiffRecord:
    """Result of :func:`get_run_diff`.

    ``found`` distinguishes the three states:

    - ``False`` — no ``diff.patch`` on disk (clean run or pre-artifact).
      ``files`` and ``content`` are empty; ``message`` explains.
    - ``True`` with ``files == ()`` — a ``path`` filter matched nothing.
      ``message`` quotes the path; ``content`` is empty.
    - ``True`` with non-empty ``files`` — normal case; ``content`` is the
      rendered body for ``mode``.

    ``truncated`` is the contract signal for byte-capped output; the SDK
    never appends a user-visible footer. ``max_bytes`` echoes the cap so
    formatters/clients can render ``... truncated at N bytes ...``
    without an out-of-band argument.

    ``scope`` echoes which artifact was read: ``"run"`` for the
    cumulative ``<run_dir>/diff.patch``, ``"phase"`` for a per-phase
    ``<run_dir>/phases/<phase>/diff.patch``. ``phase`` carries the
    normalized phase name on a phase call, ``None`` on a run call —
    clients don't have to remember what they asked for.
    """

    run_id: str
    found: bool
    mode: str
    diff_path: str | None
    files: tuple[RunDiffFileRecord, ...]
    content: str
    truncated: bool
    max_bytes: int | None
    message: str | None
    scope: Literal["run", "phase"] = "run"
    phase: str | None = None


def _normalize_phase(phase: str | None) -> str | None:
    """Validate and trim a phase artifact key.

    ``None`` passes through (caller asked for the cumulative run diff).
    Empty / whitespace-only and traversal-bearing values raise
    :class:`ValueError` — phase is an artifact key, not a filesystem
    path API.
    """
    if phase is None:
        return None
    cleaned = phase.strip()
    if not cleaned:
        raise ValueError("phase must be non-empty")
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError(
            "phase must not contain path separators or parent refs",
        )
    return cleaned


def get_run_diff(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
    mode: Literal["preview", "stat", "full"] = "preview",
    path: str | None = None,
    phase: str | None = None,
    max_bytes: int | None = None,
    color: bool = False,
) -> RunDiffRecord:
    """Read a captured ``diff.patch`` artifact and render it for viewing.

    With ``phase=None`` (default) this reads ``<run_dir>/diff.patch`` —
    the run-level cumulative diff. With ``phase="<name>"`` it reads the
    per-phase artifact ``<run_dir>/phases/<name>/diff.patch`` written
    by the engine during that phase. Backwards-compatible: omitting
    ``phase`` keeps today's behaviour byte-for-byte.

    Parameters
    ----------
    run_id
        Run id; ``None`` means "latest in the resolved runs dir".
    workspace, runs_dir, cwd
        Resolution context, forwarded to :func:`sdk.runs.find_run`. Same
        semantics as the rest of the SDK read surface.
    mode
        ``"preview"`` (Claude-style grouped view), ``"stat"`` (file
        stat table only), or ``"full"`` (raw unified patch).
    path
        Optional filter; matches against the union of ``{display path,
        old path, new path}`` for each file section so renames and
        deletes are findable by either name. Exact match first; falls
        back to prefix match if no exact hit. Stripped before use; empty
        after strip is a :class:`ValueError`.
    phase
        Optional artifact key. ``None`` reads the run-level cumulative
        diff; a non-empty string reads
        ``<run_dir>/phases/<phase>/diff.patch``. Stripped before use;
        empty after strip, or values containing ``/``, ``\\``, or
        ``..``, raise :class:`ValueError`.
    max_bytes
        Cap on ``content`` bytes. ``None`` = unlimited. ``<= 0`` raises
        :class:`ValueError`. Truncation is UTF-8 safe (partial trailing
        multibyte sequence is dropped, never an exception).
    color
        Forwarded to the renderers for ``preview`` and ``stat`` modes;
        ignored for ``full`` (raw patches are byte-faithful artifacts —
        embedding ANSI would corrupt ``git apply`` consumers).

    Raises
    ------
    NoWorkspace
        Workspace/runs dir could not be resolved.
    RunNotFound
        ``run_id`` does not exist on disk.
    ValueError
        Invalid ``mode``, non-positive ``max_bytes``, empty ``path``,
        or empty / traversal-bearing ``phase``.

    Returns
    -------
    :class:`RunDiffRecord`
        Always — missing artifact is reported via ``found=False``, not
        as an exception. ``scope`` and ``phase`` echo the artifact that
        was read (or asked for, when missing).
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"mode must be one of {_VALID_MODES!r}, got {mode!r}",
        )
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be positive or None")

    if path is not None:
        path = path.strip()
        if not path:
            raise ValueError("path must be non-empty")

    normalized_phase = _normalize_phase(phase)
    scope: Literal["run", "phase"] = (
        "phase" if normalized_phase is not None else "run"
    )

    run_ref = find_run(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )
    if normalized_phase is None:
        diff_file = run_ref.run_dir / "diff.patch"
        missing_message = "No diff artifact recorded for this run."
    else:
        diff_file = (
            run_ref.run_dir / "phases" / normalized_phase / "diff.patch"
        )
        missing_message = (
            f"No diff artifact recorded for phase {normalized_phase!r}."
        )

    if not diff_file.is_file():
        # A missing run-level artifact is still found=False, but finalization
        # may have recorded WHY in the durable ``meta.diff_patch`` block
        # (patch_missing/patch_invalid with reason + path). Prefer that
        # actionable advisory over the generic "not recorded" line so the
        # operator sees the recorded reason and path, not just silence. Phase
        # artifacts are not apply-checked, so they keep the generic message.
        missing_note = (
            _patch_integrity_note(run_ref.run_dir)
            if normalized_phase is None
            else None
        )
        return RunDiffRecord(
            run_id=run_ref.run_id,
            found=False,
            mode=mode,
            diff_path=None,
            files=(),
            content="",
            truncated=False,
            max_bytes=max_bytes,
            message=missing_note or missing_message,
            scope=scope,
            phase=normalized_phase,
        )

    raw_text = diff_file.read_text(encoding="utf-8", errors="replace")
    diffs: list[FileDiff] = parse_unified_diff(raw_text)

    if path is not None:
        diffs = filter_diffs_by_path(diffs, path)
        if not diffs:
            return RunDiffRecord(
                run_id=run_ref.run_id,
                found=True,
                mode=mode,
                diff_path=str(diff_file),
                files=(),
                content="",
                truncated=False,
                max_bytes=max_bytes,
                message=f"No diff entries matched path={path!r}.",
                scope=scope,
                phase=normalized_phase,
            )

    files = tuple(
        RunDiffFileRecord(d.path, *file_stats(d)) for d in diffs
    )

    if mode == "full":
        content = assemble_patch(diffs) if path is not None else raw_text
    elif mode == "preview":
        content = render_diff_preview_from_diffs(diffs, color=color)
    else:
        content = render_diff_stat(diffs, color=color)

    content, truncated = truncate_bytes(content, max_bytes)

    # Run-level read carries a durable patch-integrity advisory on ``message``
    # when finalization recorded the captured patch as invalid/missing. Phase
    # artifacts are not apply-checked, so they keep ``message=None``. The patch
    # body (``content``) is never touched — it stays byte-faithful for
    # ``git apply`` consumers.
    integrity_note = (
        _patch_integrity_note(run_ref.run_dir) if normalized_phase is None else None
    )

    return RunDiffRecord(
        run_id=run_ref.run_id,
        found=True,
        mode=mode,
        diff_path=str(diff_file),
        files=files,
        content=content,
        truncated=truncated,
        max_bytes=max_bytes,
        message=integrity_note,
        scope=scope,
        phase=normalized_phase,
    )
