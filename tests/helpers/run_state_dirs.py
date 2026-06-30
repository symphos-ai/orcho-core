"""Synthetic run-directory builders for run-state tests.

Additive consolidation of the inline ``_write_*`` builders already duplicated
across ``tests/unit/pipeline/run_state/*`` (consistency, cross, repair,
cross_repair). Every builder writes tiny JSON artifacts into a caller-provided
``run_dir`` (always a ``tmp_path`` subdir in practice) and does nothing else:

- no subprocess, no git, no worktree, no provider, no real pipeline;
- no fixed/global run ids or shared paths, so callers stay xdist-safe — the
  only state touched is the directory the caller already owns.

The JSON shapes match the readers under :mod:`pipeline.run_state`
(``consistency``/``projector`` read ``events.jsonl``, ``meta.json`` and
``phase_handoff_decisions/``; ``cross`` reads ``cross_checkpoint.json`` and the
child ``meta.json`` rows). Serialization is compact and tolerant — the readers
parse either compact or indented JSON.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _dump(path: Path, payload: Any) -> None:
    """Write ``payload`` as compact JSON, creating the parent dir if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def run_event(
    kind: str,
    *,
    seq: int = 1,
    phase: str | None = None,
    ts: str = "t",
    **payload: Any,
) -> dict[str, Any]:
    """Build one ``events.jsonl`` line dict for ``kind``.

    Extra keyword args become the event ``payload`` — e.g.
    ``run_event("phase.end", seq=3, phase="PLAN", title="PLAN", outcome="ok")``.
    """
    return {
        "seq": seq,
        "ts": ts,
        "kind": kind,
        "phase": phase,
        "payload": dict(payload),
    }


def handoff_event(
    handoff_id: str, phase: str = "validate_plan", seq: int = 1,
) -> dict[str, Any]:
    """Build a ``phase.handoff_requested`` event line (id + phase payload)."""
    return {
        "seq": seq,
        "ts": "t",
        "kind": "phase.handoff_requested",
        "phase": phase,
        "payload": {"handoff_id": handoff_id, "phase": phase},
    }


def write_events(run_dir: Path, lines: list[dict[str, Any]]) -> None:
    """Write ``lines`` to ``run_dir/events.jsonl`` (one JSON object per line)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    """Write ``meta`` to ``run_dir/meta.json`` (single-project or cross)."""
    _dump(run_dir / "meta.json", meta)


# ``cross_checkpoint.json`` + the cross ``meta.json`` are the two durable cross
# surfaces. ``write_cross_meta`` is an intent-revealing alias of ``write_meta``
# (the on-disk shape is identical); ``write_checkpoint`` writes the cross
# checkpoint; ``write_child_meta`` writes a per-alias child ``meta.json`` for
# the ``run_dir/<alias>/meta.json`` rows the cross classifier reads.
write_cross_meta = write_meta


def write_checkpoint(run_dir: Path, checkpoint: dict[str, Any]) -> None:
    """Write ``checkpoint`` to ``run_dir/cross_checkpoint.json``."""
    _dump(run_dir / "cross_checkpoint.json", checkpoint)


def write_child_meta(run_dir: Path, alias: str, meta: dict[str, Any]) -> None:
    """Write a child run's ``meta.json`` under ``run_dir/<alias>/meta.json``."""
    _dump(run_dir / alias / "meta.json", meta)


def write_decision(run_dir: Path, name: str, decision: dict[str, Any]) -> None:
    """Write a decision artifact to ``phase_handoff_decisions/<name>.json``."""
    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    decisions_dir.joinpath(f"{name}.json").write_text(
        json.dumps(decision), encoding="utf-8"
    )


__all__ = [
    "handoff_event",
    "run_event",
    "write_checkpoint",
    "write_child_meta",
    "write_cross_meta",
    "write_decision",
    "write_events",
    "write_meta",
]
