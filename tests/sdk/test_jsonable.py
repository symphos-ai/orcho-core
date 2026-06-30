"""`to_jsonable` recursive projection contract."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from sdk import to_jsonable


class _Color(Enum):
    RED = "red"
    BLUE = 42


@dataclass(frozen=True, slots=True)
class _Inner:
    value: int


@dataclass(frozen=True, slots=True)
class _Outer:
    name: str
    when: datetime
    where: Path
    inner: _Inner
    siblings: tuple[_Inner, ...]
    extras: dict[str, _Color]


def test_primitives_pass_through():
    assert to_jsonable(None) is None
    assert to_jsonable(True) is True
    assert to_jsonable("hi") == "hi"
    assert to_jsonable(3.14) == 3.14


def test_path_to_string():
    assert to_jsonable(Path("/tmp/x")) == "/tmp/x"


def test_datetime_iso():
    dt = datetime(2026, 5, 9, 14, 30, 0)
    assert to_jsonable(dt) == "2026-05-09T14:30:00"
    assert to_jsonable(date(2026, 5, 9)) == "2026-05-09"


def test_enum_value():
    assert to_jsonable(_Color.RED) == "red"
    assert to_jsonable(_Color.BLUE) == 42


def test_nested_dataclass_roundtrip():
    val = _Outer(
        name="run-01",
        when=datetime(2026, 5, 9),
        where=Path("/tmp/run-01"),
        inner=_Inner(value=10),
        siblings=(_Inner(20), _Inner(30)),
        extras={"a": _Color.RED, "b": _Color.BLUE},
    )
    projection = to_jsonable(val)
    encoded = json.dumps(projection)
    decoded = json.loads(encoded)
    assert decoded["name"] == "run-01"
    assert decoded["where"] == "/tmp/run-01"
    assert decoded["inner"]["value"] == 10
    assert decoded["siblings"] == [{"value": 20}, {"value": 30}]
    assert decoded["extras"] == {"a": "red", "b": 42}


def test_collections():
    # Sets and frozensets project to lists (order undefined but JSON-safe).
    out = to_jsonable({1, 2, 3})
    assert sorted(out) == [1, 2, 3]
    assert to_jsonable((1, "x", Path("/a"))) == [1, "x", "/a"]


def test_unknown_type_falls_back_to_str():
    class _Foo:
        def __str__(self) -> str:
            return "<foo>"

    assert to_jsonable(_Foo()) == "<foo>"
