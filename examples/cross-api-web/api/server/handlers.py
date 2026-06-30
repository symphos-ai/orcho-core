"""Pure CRUD handlers.

Each handler accepts an open sqlite connection, the resolved
:class:`Section`, plus any URL/body params, and returns
``(status, body_dict)``. They raise :class:`errors.HttpError` for
predictable failures; the HTTP layer maps anything else.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from . import db, errors, wire
from .sections import BY_SLUG, SECTIONS, Section


def meta(conn: sqlite3.Connection) -> tuple[int, dict]:
    return 200, {
        "ok": True,
        "sections": [_section_meta(s) for s in SECTIONS],
        "counts": {s.slug: db.count_rows(conn, s) for s in SECTIONS},
    }


def list_section(
    conn: sqlite3.Connection, section: Section,
) -> tuple[int, dict]:
    return 200, {
        "ok": True,
        "kind": section.slug,
        "items": db.list_rows(conn, section),
    }


def create_section(
    conn: sqlite3.Connection,
    section: Section,
    body: Mapping[str, object],
    api_root: str,
) -> tuple[int, dict]:
    producer_body = dict(body)
    if section.pk_col not in producer_body:
        producer_body[section.pk_col] = db.next_public_id(conn, section)
    payload = wire.call_producer(section.create, producer_body, api_root)
    row = db.insert_row(conn, section, payload)
    return 201, {
        "ok": True,
        "kind": section.slug,
        "row": row,
        "display": render_row(section, row),
    }


def update_section(
    conn: sqlite3.Connection,
    section: Section,
    pk_value: str,
    body: Mapping[str, object],
    api_root: str,
) -> tuple[int, dict]:
    if section.update is None:
        raise errors.method_not_allowed(
            f"{section.slug!r} does not support PUT",
        )
    if section.pk_col in body:
        raise errors.bad_request("identifier must be in URL, not body")
    missing = [c for c in section.update.columns if c not in body]
    if missing:
        raise errors.bad_request(f"missing fields: {', '.join(missing)}")

    wire_input = {**body, section.pk_col: pk_value}
    payload = wire.call_producer(section.update.producer, wire_input, api_root)
    row = db.update_row(
        conn, section, pk_value, payload,
        section.update.columns, section.update.payload_aliases,
    )
    if row is None:
        raise errors.not_found(
            f"unknown {section.pk_col}: {pk_value!r}",
        )
    return 200, {
        "ok": True,
        "kind": section.slug,
        "row": row,
        "display": render_row(section, row),
    }


def parse_json_body(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise errors.bad_request(f"malformed JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise errors.bad_request("JSON body must be an object")
    return body


def resolve_section(slug: str) -> Section:
    section = BY_SLUG.get(slug)
    if section is None:
        raise errors.not_found(f"unknown resource: {slug!r}")
    return section


def render_row(section: Section, row: Mapping[str, object]) -> str:
    if section.slug == "users":
        return f"{row['name']} <{row['email']}>"
    if section.slug == "teams":
        return (
            f"{row['name']}  (id={row['team_id']}, owner={row['owner_id']})"
        )
    if section.slug == "projects":
        return f"[{row['status']}] {row['name']} - team {row['team_id']}"
    return str(row)


def _section_meta(section: Section) -> dict:
    return {
        "slug": section.slug,
        "label": section.label,
        "title": section.title,
        "fields": [f.__dict__ for f in section.fields],
        "display_cols": [list(c) for c in section.display_cols],
    }
