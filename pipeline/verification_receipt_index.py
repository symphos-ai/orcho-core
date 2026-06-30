# SPDX-License-Identifier: Apache-2.0
"""verification_receipt_index.py — multi-run command-receipt search + provenance.

A follow-up run owns its own run directory, but the work it is delivering may
have been verified in the *parent* run that spawned it (a correction follow-up
re-uses the retained worktree without re-running every command). The Stage 5
readiness layer and the Stage 6 delivery gate must therefore look for a required
command's receipt not only in the current run, but along an ordered search path
of run directories (current run first, then its parents), and record *where* the
accepted receipt came from.

This module owns only the mechanical side of that lookup — the typed search
sources, the per-command candidate loading across several run directories, and
the resolved file path of each candidate receipt. The *selection rule* (current
present wins; a fresh same-diff failure blocks inheritance; an otherwise
absent / stale / foreign-diff current receipt degrades to a valid parent) lives
in :mod:`pipeline.verification_readiness`, next to the single
``_classify_receipt`` mechanism it reuses, so this module never imports the
classification or orchestration layers.

ARCHITECTURAL CONSTRAINT: this module imports only stdlib and the tolerant
receipt loader in :mod:`pipeline.evidence.verification_receipt`. It must NEVER
import :mod:`pipeline.verification_readiness` (which imports *it*) nor any
``pipeline.project.*`` orchestration module. Every IO failure degrades like
:func:`load_command_receipts` — a missing source contributes no candidates and
never raises.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.evidence.verification_receipt import (
    COMMAND_RECEIPTS_DIRNAME,
    _sanitize_filename_stem,
    load_command_receipts,
)

__all__ = [
    "VERIFICATION_PARENT_RUNS_EXTRAS_KEY",
    "ReceiptCandidate",
    "ReceiptSource",
    "coerce_receipt_sources",
    "load_parent_candidates",
    "parent_sources_from_extras",
    "receipt_file_path",
]

# Single documented extras key under which the orchestration layer threads the
# follow-up's parent run sources (current run is NOT included here — it is always
# searched first via its own ``run_dir``). The value is an ordered iterable of
# ``(run_id, run_dir)`` pairs (closest parent first). Both the Stage 5 readiness
# reader and the Stage 6 delivery gate read THIS one key, so the two surfaces can
# never diverge on which parents they consulted.
VERIFICATION_PARENT_RUNS_EXTRAS_KEY = "verification_parent_runs"


@dataclass(frozen=True)
class ReceiptSource:
    """One run directory to search for command-receipts, with its run id.

    ``run_id`` is provenance only (surfaced on the resulting classification);
    ``run_dir`` is the run output directory whose
    ``verification_command_receipts/`` is read.
    """

    run_id: str
    run_dir: str


@dataclass(frozen=True)
class ReceiptCandidate:
    """A command-receipt found in one search source, with its provenance.

    ``source_run_id`` / ``path`` are the provenance the readiness layer copies
    onto the accepted :class:`pipeline.verification_readiness.ReceiptClassification`
    so the reviewer / delivery banner can name the run a receipt was inherited
    from. ``receipt`` is the raw receipt mapping (classified by the readiness
    layer's single ``_classify_receipt`` mechanism).
    """

    command: str
    source_run_id: str
    path: str
    receipt: Mapping[str, Any]


def receipt_file_path(run_dir: Path | str, command: str) -> str:
    """Resolved path of ``command``'s receipt under ``run_dir``.

    Mirrors the writer's filename convention
    (:func:`pipeline.evidence.verification_receipt.write_command_receipt`):
    ``<run_dir>/verification_command_receipts/<sanitized-command>.json``. The
    file need not exist — this is a pure path computation for provenance.
    """
    stem = _sanitize_filename_stem(str(command))
    return str(Path(run_dir) / COMMAND_RECEIPTS_DIRNAME / f"{stem}.json")


def coerce_receipt_sources(value: Any) -> tuple[ReceiptSource, ...]:
    """Normalise a loose sources value into an ordered tuple of sources.

    Tolerant by design (the extras value / test override is never trusted): a
    falsey / non-iterable ``value`` yields ``()``. Each item may be a
    :class:`ReceiptSource`, a ``(run_id, run_dir)`` pair, or a mapping with
    ``run_id`` / ``run_dir`` keys; entries without a usable ``run_dir`` are
    skipped. Order is preserved (caller's search order). Never raises.
    """
    if not value or isinstance(value, (str, bytes)):
        return ()
    if not isinstance(value, Iterable):
        return ()
    out: list[ReceiptSource] = []
    for item in value:
        if isinstance(item, ReceiptSource):
            if item.run_dir:
                out.append(item)
            continue
        if isinstance(item, Mapping):
            run_id = str(item.get("run_id", ""))
            run_dir = str(item.get("run_dir", ""))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            run_id = str(item[0])
            run_dir = str(item[1])
        else:
            continue
        if run_dir:
            out.append(ReceiptSource(run_id=run_id, run_dir=run_dir))
    return tuple(out)


def parent_sources_from_extras(
    extras: Mapping[str, Any] | None,
) -> tuple[ReceiptSource, ...]:
    """Read the ordered parent run sources from ``extras``; degrade to ``()``.

    Reads only :data:`VERIFICATION_PARENT_RUNS_EXTRAS_KEY` and coerces the value
    via :func:`coerce_receipt_sources`. Absent key / wrong shape → ``()``.
    """
    if not isinstance(extras, Mapping):
        return ()
    return coerce_receipt_sources(extras.get(VERIFICATION_PARENT_RUNS_EXTRAS_KEY))


def load_parent_candidates(
    sources: Iterable[ReceiptSource],
    commands: Iterable[str],
) -> dict[str, list[ReceiptCandidate]]:
    """Per-command ordered candidate lists across the parent ``sources``.

    Each source's receipt directory is read once (tolerant, via
    :func:`load_command_receipts`); for every requested command the candidates
    appear in source order (closest parent first). Commands with no receipt in
    any source map to an empty list. Never raises.
    """
    command_list = list(commands)
    by_command: dict[str, list[ReceiptCandidate]] = {c: [] for c in command_list}
    for source in sources:
        receipts = {
            str(r.get("command", "")): r
            for r in load_command_receipts(source.run_dir)
        }
        for command in command_list:
            receipt = receipts.get(command)
            if receipt is None:
                continue
            by_command[command].append(
                ReceiptCandidate(
                    command=command,
                    source_run_id=source.run_id,
                    path=receipt_file_path(source.run_dir, command),
                    receipt=receipt,
                ),
            )
    return by_command
