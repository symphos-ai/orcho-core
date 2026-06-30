"""SQLite storage layer.

One connection per HTTP request, opened by the HTTP layer and passed
into handlers. CRUD helpers receive that connection — they never
reach for a fresh one mid-call.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .sections import Section

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  pk         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    TEXT UNIQUE NOT NULL,
  name       TEXT NOT NULL,
  email      TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
  pk         INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id    TEXT UNIQUE NOT NULL,
  name       TEXT NOT NULL,
  owner_id   TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  pk         INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT UNIQUE NOT NULL,
  name       TEXT NOT NULL,
  team_id    TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

FIXTURES: dict[str, list[tuple[str, ...]]] = {
    "users": [
        ("u-1001", "Maria Garcia", "maria@acme.test"),
        ("u-1002", "Bob Chen",     "bob@acme.test"),
        ("u-1003", "Carla Singh",  "carla@acme.test"),
    ],
    "teams": [
        ("t-1", "Platform", "u-1001"),
        ("t-2", "Growth",   "u-1002"),
    ],
    "projects": [
        ("p-1", "Orcho", "t-1", "active"),
        ("p-2", "Atlas", "t-2", "active"),
    ],
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(db_path: str, *, reset: bool = False) -> None:
    """Create the schema and seed fixtures when the users table is empty."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        if reset:
            for tbl in ("projects", "teams", "users"):
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.executescript(SCHEMA)
        conn.commit()
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed(conn)
    finally:
        conn.close()


def seed(conn: sqlite3.Connection) -> None:
    ts = now()
    conn.executemany(
        "INSERT INTO users (user_id, name, email, created_at)"
        " VALUES (?, ?, ?, ?)",
        [(*row, ts) for row in FIXTURES["users"]],
    )
    conn.executemany(
        "INSERT INTO teams (team_id, name, owner_id, created_at)"
        " VALUES (?, ?, ?, ?)",
        [(*row, ts) for row in FIXTURES["teams"]],
    )
    conn.executemany(
        "INSERT INTO projects (project_id, name, team_id, status, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(*row, ts) for row in FIXTURES["projects"]],
    )
    conn.commit()


def insert_row(
    conn: sqlite3.Connection, section: Section, payload: Mapping[str, str],
) -> dict:
    """Insert a producer-emitted payload using the section's column order.

    Raises ``sqlite3.IntegrityError`` on duplicate pk and ``KeyError``
    if the payload is missing a declared column — both surface to the
    HTTP error mapper.
    """
    cols = [*section.columns, "created_at"]
    values = [payload[c] for c in section.columns] + [now()]
    placeholders = ", ".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO {section.table} ({', '.join(cols)})"
        f" VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return _fetch_pk(conn, section, payload[section.pk_col]) or {}


def update_row(
    conn: sqlite3.Connection,
    section: Section,
    pk_value: str,
    payload: Mapping[str, str],
    columns: Sequence[str],
    aliases: Mapping[str, str],
) -> dict | None:
    """Apply a column-scoped UPDATE with optional payload aliases."""
    assignments = ", ".join(f"{col} = ?" for col in columns)
    values = [payload[aliases.get(col, col)] for col in columns]
    cur = conn.execute(
        f"UPDATE {section.table} SET {assignments}"
        f" WHERE {section.pk_col} = ?",
        [*values, pk_value],
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return _fetch_pk(conn, section, pk_value)


def list_rows(
    conn: sqlite3.Connection, section: Section, *, limit: int = 100,
) -> list[dict]:
    cur = conn.execute(
        f"SELECT * FROM {section.table} ORDER BY pk DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def count_rows(conn: sqlite3.Connection, section: Section) -> int:
    cur = conn.execute(f"SELECT COUNT(*) FROM {section.table}")
    return int(cur.fetchone()[0])


ID_FORMATS: dict[str, tuple[str, int]] = {
    "users": ("u", 1001),
    "teams": ("t", 1),
    "projects": ("p", 1),
}


def next_public_id(conn: sqlite3.Connection, section: Section) -> str:
    """Return the next public id derived from SQLite's sequence."""
    prefix, first_value = ID_FORMATS[section.slug]
    row = conn.execute(
        "SELECT COALESCE(seq, 0) FROM sqlite_sequence"
        " WHERE name = ?",
        (section.table,),
    ).fetchone()
    seq = int(row[0]) if row else 0
    return f"{prefix}-{seq + first_value}"


def _fetch_pk(
    conn: sqlite3.Connection, section: Section, pk_value: str,
) -> dict | None:
    cur = conn.execute(
        f"SELECT * FROM {section.table} WHERE {section.pk_col} = ?",
        (pk_value,),
    )
    row = cur.fetchone()
    return dict(row) if row else None
