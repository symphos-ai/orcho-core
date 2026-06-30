"""Handler-level tests exercising the new server layer.

These call the pure handler functions directly, without the HTTP
framing. They use the real producer modules from ``api/`` so the wire
layer's reload path is exercised end-to-end (minus sockets).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from server import db, errors, handlers
from server.sections import BY_SLUG


@pytest.fixture
def fresh_db(tmp_path: Path) -> str:
    path = tmp_path / "demo.db"
    db.init(str(path), reset=True)
    return str(path)


@pytest.fixture
def api_root() -> str:
    # tests/ sits next to server/ and api/ under the project root.
    return str(Path(__file__).resolve().parent.parent)


def test_meta_lists_three_sections(fresh_db: str) -> None:
    conn = db.connect(fresh_db)
    try:
        status, body = handlers.meta(conn)
    finally:
        conn.close()
    assert status == 200
    assert {s["slug"] for s in body["sections"]} == {
        "users", "teams", "projects",
    }
    assert body["counts"] == {"users": 3, "teams": 2, "projects": 2}


def test_list_teams_returns_seeded(fresh_db: str) -> None:
    conn = db.connect(fresh_db)
    try:
        status, body = handlers.list_section(conn, BY_SLUG["teams"])
    finally:
        conn.close()
    assert status == 200
    assert {r["team_id"] for r in body["items"]} == {"t-1", "t-2"}


def test_create_team_via_producer(fresh_db: str, api_root: str) -> None:
    conn = db.connect(fresh_db)
    try:
        status, body = handlers.create_section(
            conn, BY_SLUG["teams"],
            {"name": "Infra", "owner_id": "u-1001"},
            api_root,
        )
    finally:
        conn.close()
    assert status == 201
    assert body["row"]["team_id"] == "t-3"
    assert body["display"].startswith("Infra")


def test_create_project_generates_project_id(
    fresh_db: str, api_root: str,
) -> None:
    conn = db.connect(fresh_db)
    try:
        status, body = handlers.create_section(
            conn, BY_SLUG["projects"],
            {"name": "Compass", "team_id": "t-1", "status": "active"},
            api_root,
        )
    finally:
        conn.close()
    assert status == 201
    assert body["row"]["project_id"] == "p-3"
    assert body["row"]["name"] == "Compass"
    assert body["row"]["team_id"] == "t-1"


def test_create_user_generates_user_id(fresh_db: str, api_root: str) -> None:
    conn = db.connect(fresh_db)
    try:
        status, body = handlers.create_section(
            conn, BY_SLUG["users"],
            {"name": "Ada", "email": "ada@example.com"},
            api_root,
        )
    finally:
        conn.close()
    assert status == 201
    assert body["row"]["user_id"] == "u-1004"
    assert body["row"]["name"] == "Ada"
    assert body["row"]["email"] == "ada@example.com"


def test_create_user_requires_declared_email_field(
    fresh_db: str, api_root: str,
) -> None:
    conn = db.connect(fresh_db)
    try:
        with pytest.raises(KeyError) as ei:
            handlers.create_section(
                conn, BY_SLUG["users"],
                {"name": "Ada"},
                api_root,
            )
    finally:
        conn.close()
    assert ei.value.args == ("email",)


def test_update_on_section_without_update_spec(
    fresh_db: str, api_root: str,
) -> None:
    """No demo section declares an UpdateSpec — PUT must map to 405."""
    conn = db.connect(fresh_db)
    try:
        with pytest.raises(errors.HttpError) as ei:
            handlers.update_section(
                conn, BY_SLUG["users"], "u-1001",
                {"name": "Renamed", "email": "x@x.test"}, api_root,
            )
    finally:
        conn.close()
    assert ei.value.status == 405


def test_resolve_unknown_section_404() -> None:
    with pytest.raises(errors.HttpError) as ei:
        handlers.resolve_section("widgets")
    assert ei.value.status == 404


def test_parse_json_body_rejects_array() -> None:
    with pytest.raises(errors.HttpError) as ei:
        handlers.parse_json_body("[1, 2, 3]")
    assert ei.value.status == 400


def test_parse_json_body_accepts_empty() -> None:
    assert handlers.parse_json_body("") == {}
    assert handlers.parse_json_body("   ") == {}
