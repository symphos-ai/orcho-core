"""Storage-layer tests against a temp SQLite file."""
from __future__ import annotations

from pathlib import Path

import pytest
from server import db
from server.sections import BY_SLUG


@pytest.fixture
def fresh_db(tmp_path: Path) -> str:
    path = tmp_path / "demo.db"
    db.init(str(path), reset=True)
    return str(path)


def test_init_seeds_fixtures(fresh_db: str) -> None:
    conn = db.connect(fresh_db)
    try:
        users = db.list_rows(conn, BY_SLUG["users"])
        teams = db.list_rows(conn, BY_SLUG["teams"])
        projects = db.list_rows(conn, BY_SLUG["projects"])
    finally:
        conn.close()
    assert {u["user_id"] for u in users} == {"u-1001", "u-1002", "u-1003"}
    assert {t["team_id"] for t in teams} == {"t-1", "t-2"}
    assert {p["project_id"] for p in projects} == {"p-1", "p-2"}


def test_insert_and_list_teams(fresh_db: str) -> None:
    conn = db.connect(fresh_db)
    try:
        section = BY_SLUG["teams"]
        row = db.insert_row(conn, section, {
            "team_id": "t-9", "name": "Infra", "owner_id": "u-1001",
        })
        assert row["team_id"] == "t-9"
        assert any(
            r["team_id"] == "t-9"
            for r in db.list_rows(conn, section)
        )
    finally:
        conn.close()


def test_update_row_applies_aliases(fresh_db: str) -> None:
    """UPDATE bridges differing payload keys via the alias mapping."""
    conn = db.connect(fresh_db)
    try:
        row = db.update_row(
            conn, BY_SLUG["users"], "u-1001",
            payload={"name": "Maria G.", "contact": "mg@acme.test"},
            columns=("name", "email"),
            aliases={"email": "contact"},
        )
        assert row is not None
        assert row["name"] == "Maria G."
        assert row["email"] == "mg@acme.test"
    finally:
        conn.close()


def test_update_row_returns_none_for_unknown_pk(fresh_db: str) -> None:
    conn = db.connect(fresh_db)
    try:
        row = db.update_row(
            conn, BY_SLUG["users"], "u-does-not-exist",
            payload={"name": "X", "email": "x@x.test"},
            columns=("name", "email"),
            aliases={},
        )
        assert row is None
    finally:
        conn.close()


def test_count_rows_returns_seed_count(fresh_db: str) -> None:
    conn = db.connect(fresh_db)
    try:
        assert db.count_rows(conn, BY_SLUG["users"]) == 3
        assert db.count_rows(conn, BY_SLUG["teams"]) == 2
        assert db.count_rows(conn, BY_SLUG["projects"]) == 2
    finally:
        conn.close()
