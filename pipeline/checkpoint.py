"""
pipeline/checkpoint.py — Lightweight checkpoint store for pipeline state.

Uses SQLite for atomic, crash-safe persistence of per-phase snapshots.
The checkpoint DB lives alongside the session artifacts in output_dir.

Design principles:
  * Zero external dependencies (stdlib sqlite3 only).
  * Append-only log of completed phases — no UPDATE, just INSERT.
  * In-memory DB for tests (pass ``:memory:`` as db_path).
  * Thread-safe (sqlite3 with check_same_thread=False + WAL mode).

Usage:

    store = CheckpointStore(output_dir / "checkpoints.db")
    store.save("plan", phase_data)
    store.save("validate_plan", qa_data)

    # After crash / Ctrl-C:
    store = CheckpointStore(output_dir / "checkpoints.db")
    state = store.load()
    # state.completed == ["plan", "validate_plan"]
    # state.phases["plan"] == phase_data
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class PipelineStatus(str, Enum):  # noqa: UP042  # StrEnum changes __str__; persisted in checkpoint JSON, keep value-only repr stable
    """Overall pipeline status for the checkpoint."""

    RUNNING = "running"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    # Generic phase-level human handoff pause. When a phase declares a
    # non-bypass handoff and its trigger condition fires, the
    # orchestrator persists a ``meta.phase_handoff`` payload and the
    # run pauses with this status. Resumable: exit code 4, decision
    # artifact under ``phase_handoff_decisions/``.
    AWAITING_PHASE_HANDOFF = "awaiting_phase_handoff"
    # Cross-runner gate paused for a manual-confirm operator decision
    # (initially contract_check). Same resumable class as phase handoff:
    # exit code 4, resume picks up from ``pending_gate`` in the session.
    AWAITING_GATE_DECISION = "awaiting_gate_decision"
    DONE = "done"
    HALTED = "halted"
    FAILED = "failed"
    # Manual stop — user clicked Cancel at the QA gate (or analogous UI).
    # Distinct from FAILED: the pipeline didn't crash, the user chose to
    # abandon. Resume logic should treat CANCELLED as terminal (do not
    # re-enter the gate); the user must start a new run instead.
    CANCELLED = "cancelled"


@dataclass
class PipelineState:
    """Reconstructed pipeline state from checkpoint DB.

    ``completed`` is an ordered list of phase names that finished successfully.
    ``phases`` maps phase_name → output dict (whatever the phase stored).
    ``status`` is the overall pipeline status.
    ``run_config`` stores the original pipeline config (task, project, models, etc.)
    so ``--resume`` can reconstruct the same setup.
    """

    completed: list[str] = field(default_factory=list)
    phases: dict[str, Any] = field(default_factory=dict)
    status: PipelineStatus = PipelineStatus.RUNNING
    run_config: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""

    @property
    def last_completed_phase(self) -> str | None:
        return self.completed[-1] if self.completed else None

    def has_phase(self, name: str) -> bool:
        return name in self.completed

    def should_skip(self, name: str) -> bool:
        """Return True if this phase was already completed (for --resume)."""
        return name in self.completed


@dataclass(frozen=True, slots=True)
class PhaseCheckpointRecord:
    """One committed phase row in append order."""

    phase: str
    data: dict[str, Any] | list[Any]


@dataclass(frozen=True, slots=True)
class LoopCursorRecord:
    """Durable boundary committed atomically with a loop-inner phase."""

    loop_key: str
    loop_phases: tuple[str, ...]
    round_n: int
    completed_phase: str
    next_phase: str | None


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS checkpoints (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT    NOT NULL,
    phase      TEXT    NOT NULL,
    data_json  TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_meta (
    run_id     TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- E1: per-role agent.session_id persisted across subprocess restarts so
-- ``--resume <sid>`` survives ``orcho_run_resume`` and any other handoff
-- pause. Keyed by ``(run_id, role_attr)``; ``role_attr`` matches the
-- PhaseAgentConfig slot name (``plan_agent``, ``validate_plan_agent``,
-- ...). Last write wins.
CREATE TABLE IF NOT EXISTS agent_sessions (
    run_id     TEXT NOT NULL,
    role_attr  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, role_attr)
);

CREATE TABLE IF NOT EXISTS loop_cursors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT    NOT NULL,
    loop_key         TEXT    NOT NULL,
    loop_phases_json TEXT    NOT NULL,
    round_n          INTEGER NOT NULL,
    completed_phase  TEXT    NOT NULL,
    next_phase       TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


class CheckpointStore:
    """Append-only checkpoint store backed by SQLite.

    Parameters
    ----------
    db_path : str | Path
        Path to the SQLite database file, or ``:memory:`` for in-memory use.
    run_id : str | None
        Identifier for the current pipeline run. When resuming, pass the same
        run_id to load prior state. If None, generates from timestamp.
    """

    def __init__(self, db_path: str | Path = ":memory:", run_id: str | None = None):
        self._db_path = str(db_path)
        self._run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        # Ensure parent directory exists for file-backed DBs
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level="DEFERRED",
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @property
    def run_id(self) -> str:
        return self._run_id

    # ── Write ─────────────────────────────────────────────────────────────────

    def save_config(self, config: dict[str, Any]) -> None:
        """Persist pipeline configuration for resume."""
        self._conn.execute(
            "INSERT OR REPLACE INTO run_meta (run_id, config_json, status, created_at, updated_at) "
            "VALUES (?, ?, 'running', datetime('now'), datetime('now'))",
            (self._run_id, json.dumps(config, ensure_ascii=False)),
        )
        self._conn.commit()

    def save_phase(
        self,
        phase: str,
        data: dict[str, Any] | list[Any],
        *,
        loop_cursor: LoopCursorRecord | None = None,
    ) -> None:
        """Record a completed phase. ``data`` is JSON-serialized as-is; both
        dict (single-shot phases) and list (multi-attempt phases like
        plan/validate_plan with rounds) are accepted. Repeat calls for the same
        phase append rows; ``load()`` returns the last write.

        When ``loop_cursor`` is supplied, its boundary row is committed in the
        same SQLite transaction as the phase row. A crash therefore cannot
        expose "phase completed" without the cursor needed to continue its
        enclosing loop.
        """
        try:
            self._conn.execute(
                "INSERT INTO checkpoints (run_id, phase, data_json) "
                "VALUES (?, ?, ?)",
                (self._run_id, phase, json.dumps(data, ensure_ascii=False)),
            )
            if loop_cursor is not None:
                self._conn.execute(
                    "INSERT INTO loop_cursors "
                    "(run_id, loop_key, loop_phases_json, round_n, "
                    "completed_phase, next_phase) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self._run_id,
                        loop_cursor.loop_key,
                        json.dumps(loop_cursor.loop_phases, ensure_ascii=False),
                        loop_cursor.round_n,
                        loop_cursor.completed_phase,
                        loop_cursor.next_phase,
                    ),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def set_status(self, status: PipelineStatus) -> None:
        """Update overall pipeline status."""
        self._conn.execute(
            "UPDATE run_meta SET status = ?, updated_at = datetime('now') WHERE run_id = ?",
            (status.value, self._run_id),
        )
        self._conn.commit()

    def save_loop_cursor(self, cursor: LoopCursorRecord) -> None:
        """Persist a validated legacy cursor migration before dispatch."""
        self.save_loop_cursors((cursor,))

    def save_loop_cursors(self, cursors: tuple[LoopCursorRecord, ...]) -> None:
        """Atomically persist a validated legacy cursor migration."""
        try:
            self._conn.executemany(
                "INSERT INTO loop_cursors "
                "(run_id, loop_key, loop_phases_json, round_n, completed_phase, "
                "next_phase) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        self._run_id,
                        cursor.loop_key,
                        json.dumps(cursor.loop_phases, ensure_ascii=False),
                        cursor.round_n,
                        cursor.completed_phase,
                        cursor.next_phase,
                    )
                    for cursor in cursors
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def set_agent_session(self, role_attr: str, session_id: str | None) -> None:
        """Persist ``agent.session_id`` for ``role_attr``.

        ``role_attr`` is the ``PhaseAgentConfig`` slot name
        (``plan_agent``, ``validate_plan_agent``, ...). ``session_id``
        is the runtime-side resumable id (Claude CLI / Codex CLI
        session). Passing ``None`` clears any existing record so the
        next subprocess starts the role fresh.

        Idempotent and last-write-wins. Survives subprocess crashes
        because SQLite WAL writes are flushed at commit. Pipeline calls
        this from inside ``_session_aware_invoke`` after every
        successful runtime invocation, so the on-disk view is at most
        one invocation stale.
        """
        if session_id is None:
            self._conn.execute(
                "DELETE FROM agent_sessions WHERE run_id = ? AND role_attr = ?",
                (self._run_id, role_attr),
            )
        else:
            self._conn.execute(
                "INSERT OR REPLACE INTO agent_sessions "
                "(run_id, role_attr, session_id, updated_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (self._run_id, role_attr, session_id),
            )
        self._conn.commit()

    # ── Read ──────────────────────────────────────────────────────────────────

    def load(self, run_id: str | None = None) -> PipelineState:
        """Load the state for a run. Returns empty state if run_id not found."""
        rid = run_id or self._run_id
        state = PipelineState(run_id=rid)

        # Load config + status
        row = self._conn.execute(
            "SELECT config_json, status FROM run_meta WHERE run_id = ?", (rid,)
        ).fetchone()
        if row:
            state.run_config = json.loads(row[0])
            state.status = PipelineStatus(row[1])

        # Load completed phases in order
        rows = self._conn.execute(
            "SELECT phase, data_json FROM checkpoints "
            "WHERE run_id = ? ORDER BY id ASC",
            (rid,),
        ).fetchall()
        for phase_name, data_json in rows:
            state.completed.append(phase_name)
            state.phases[phase_name] = json.loads(data_json)

        return state

    def get_agent_sessions(self, run_id: str | None = None) -> dict[str, str]:
        """Return ``{role_attr → session_id}`` for ``run_id`` (default: own).

        Empty dict for fresh runs, runs that have not yet invoked any
        agent, or runs that pre-date this table. Read on subprocess
        startup; the result feeds ``_apply_followup_session_seeds`` so
        the next invoke goes out with ``--resume <sid>``.
        """
        rid = run_id or self._run_id
        rows = self._conn.execute(
            "SELECT role_attr, session_id FROM agent_sessions WHERE run_id = ?",
            (rid,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_phase_records(
        self, run_id: str | None = None,
    ) -> tuple[PhaseCheckpointRecord, ...]:
        """Return committed phase rows in append order."""
        rid = run_id or self._run_id
        rows = self._conn.execute(
            "SELECT phase, data_json FROM checkpoints "
            "WHERE run_id = ? ORDER BY id ASC",
            (rid,),
        ).fetchall()
        return tuple(
            PhaseCheckpointRecord(phase=phase, data=json.loads(data_json))
            for phase, data_json in rows
        )

    def get_loop_cursors(
        self, run_id: str | None = None,
    ) -> tuple[LoopCursorRecord, ...]:
        """Return durable loop boundaries in commit order."""
        rid = run_id or self._run_id
        rows = self._conn.execute(
            "SELECT loop_key, loop_phases_json, round_n, completed_phase, "
            "next_phase FROM loop_cursors WHERE run_id = ? ORDER BY id ASC",
            (rid,),
        ).fetchall()
        return tuple(
            LoopCursorRecord(
                loop_key=loop_key,
                loop_phases=tuple(json.loads(loop_phases_json)),
                round_n=int(round_n),
                completed_phase=completed_phase,
                next_phase=next_phase,
            )
            for (
                loop_key,
                loop_phases_json,
                round_n,
                completed_phase,
                next_phase,
            ) in rows
        )

    def list_runs(self) -> list[dict[str, Any]]:
        """List all runs in the store, most recent first."""
        rows = self._conn.execute(
            "SELECT run_id, status, config_json, created_at, updated_at "
            "FROM run_meta ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "run_id": r[0],
                "status": r[1],
                "config": json.loads(r[2]),
                "created_at": r[3],
                "updated_at": r[4],
            }
            for r in rows
        ]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CheckpointStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
