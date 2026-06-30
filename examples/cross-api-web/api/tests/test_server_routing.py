"""Routing-table tests — pure, no HTTP setup."""
from __future__ import annotations

from pathlib import Path

import pytest
from server import http, routing, static


def test_match_list_route() -> None:
    r = routing.match("GET", "/api/users")
    assert r is not None
    assert r.name == "api.list"
    assert r.params == {"slug": "users"}


def test_match_create_route() -> None:
    r = routing.match("POST", "/api/teams")
    assert r is not None
    assert r.name == "api.create"
    assert r.params == {"slug": "teams"}


def test_match_update_route_captures_id() -> None:
    r = routing.match("PUT", "/api/users/u-42")
    assert r is not None
    assert r.name == "api.update"
    assert r.params == {"slug": "users", "id": "u-42"}


def test_match_meta_route() -> None:
    r = routing.match("GET", "/api/meta")
    assert r is not None and r.name == "api.meta"
    assert r.params == {}


def test_match_returns_none_for_unknown_path() -> None:
    assert routing.match("GET", "/random") is None
    assert routing.match("DELETE", "/api/users/u-1") is None


def test_match_returns_none_for_method_mismatch() -> None:
    assert routing.match("POST", "/api/users/u-1") is None
    assert routing.match("PUT", "/api/users") is None


def test_safe_header_value_rejects_response_splitting() -> None:
    assert http._safe_header_value("text/html; charset=utf-8") == "text/html; charset=utf-8"
    assert http._safe_header_value("text/anything") == "application/octet-stream"
    with pytest.raises(ValueError, match="CR or LF"):
        http._safe_header_value("text/html\r\nSet-Cookie: session=evil")


def test_load_asset_serves_file_inside_allowed_root(tmp_path: Path) -> None:
    asset = tmp_path / "src" / "main.ts"
    asset.parent.mkdir()
    asset.write_text("console.log('ok')", encoding="utf-8")

    loaded = static.load_asset(str(tmp_path), "/src/main.ts")

    assert loaded == (b"console.log('ok')", "text/javascript")


def test_load_asset_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    assert static.load_asset(str(tmp_path), "/src/../secret.txt") is None
    assert static.load_asset(str(tmp_path), "/src//main.ts") is None
