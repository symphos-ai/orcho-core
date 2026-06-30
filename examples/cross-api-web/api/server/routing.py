"""Tiny URL routing table.

Patterns are lists of segments where any segment starting with ``:``
is a placeholder. :func:`match` returns the resolved route name and
captured params or ``None`` when nothing fits.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    name: str
    params: dict[str, str]


_ROUTES: list[tuple[str, list[str], str]] = [
    ("GET",  ["api", "meta"],         "api.meta"),
    ("GET",  ["api", ":slug"],        "api.list"),
    ("POST", ["api", ":slug"],        "api.create"),
    ("PUT",  ["api", ":slug", ":id"], "api.update"),
]


def match(method: str, path: str) -> Route | None:
    parts = [p for p in path.strip("/").split("/") if p]
    for m, pattern, name in _ROUTES:
        if m != method or len(pattern) != len(parts):
            continue
        params: dict[str, str] = {}
        ok = True
        for got, want in zip(parts, pattern, strict=True):
            if want.startswith(":"):
                params[want[1:]] = got
            elif got != want:
                ok = False
                break
        if ok:
            return Route(name=name, params=params)
    return None
