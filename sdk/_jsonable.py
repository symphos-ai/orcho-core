"""Recursive JSON-friendly projection for SDK return values.

`to_jsonable(value)` walks any value the SDK can hand back — dataclasses,
lists, tuples, dicts, `Path`, `datetime`, `date`, `Enum`, primitives —
and returns a structure that `json.dumps` accepts. The result is the
stable IPC projection: every embedder, in-process or out-of-process,
can serialise SDK output through the same single helper.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Return a JSON-serialisable projection of `value`.

    Supports dataclasses (recursively), lists/tuples/sets, dicts,
    `Path`, `datetime`/`date` (ISO-formatted), `Enum` (its `.value`),
    and primitives (`str`, `int`, `float`, `bool`, `None`). Any other
    type falls back to `str(value)` to keep the contract total — better
    a string than a `TypeError` at the IPC boundary.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    return str(value)
