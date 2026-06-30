"""Exception → HTTP error mapping.

Handlers raise ``HttpError`` for predictable failures; the HTTP layer
translates anything unexpected through :func:`from_exception`. We
never echo traceback bodies to clients — the original demo did and
that hid a real responsibility split.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class HttpError(Exception):
    status: int
    type: str
    message: str

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_body(self) -> dict:
        return {
            "ok": False,
            "error": {"type": self.type, "message": self.message},
        }


def bad_request(msg: str) -> HttpError:
    return HttpError(400, "BadRequest", msg)


def not_found(msg: str) -> HttpError:
    return HttpError(404, "NotFound", msg)


def method_not_allowed(msg: str) -> HttpError:
    return HttpError(405, "MethodNotAllowed", msg)


def conflict(msg: str) -> HttpError:
    return HttpError(409, "Conflict", msg)


def from_exception(exc: BaseException) -> HttpError:
    """Map any unhandled exception to an HttpError."""
    if isinstance(exc, HttpError):
        return exc
    if isinstance(exc, sqlite3.IntegrityError):
        return conflict(f"resource already exists: {exc}")
    return HttpError(500, type(exc).__name__, str(exc))
