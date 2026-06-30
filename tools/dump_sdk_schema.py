#!/usr/bin/env python3
"""Dump the public SDK surface as a deterministic JSON snapshot.

This is the SDK analogue of an OpenAPI snapshot: not a runtime
contract but a *committed* fingerprint that fails CI on accidental
drift. Embedders (MCP, Web, third-party consumers) read it as the
canonical machine-readable description of what `from sdk import …`
exposes.

Run::

    python tools/dump_sdk_schema.py            # writes docs/sdk_schema.json
    python tools/dump_sdk_schema.py --check    # exit 1 on drift, no write

The drift test in `tests/sdk/test_schema_snapshot.py` invokes
``--check`` mode and tells the developer to re-run without
``--check`` if they meant the change.
"""
from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import os
import sys
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# SDK schema snapshots must not depend on the developer's local
# config.local.json. Tests set the same variable from tests/conftest.py;
# keep the standalone CLI deterministic too.
os.environ.setdefault("ORCHO_DISABLE_LOCAL_CONFIG", "1")

import sdk  # noqa: E402

_SCHEMA_PATH = _REPO_ROOT / "docs" / "sdk_schema.json"


def _annotation_str(ann: Any) -> str:
    """Render a type annotation as a stable string.

    Module qualifiers are stripped where the type is a builtin or a
    well-known typing construct so the snapshot is robust against
    `from __future__ import annotations` differences.
    """
    if ann is inspect.Parameter.empty:
        return "Any"
    if isinstance(ann, str):
        return ann
    if isinstance(ann, type):
        if ann.__module__ in ("builtins", "typing"):
            return ann.__qualname__
        return f"{ann.__module__}.{ann.__qualname__}"
    return str(ann).replace("typing.", "")


def _default_str(default: Any) -> str | None:
    """Render a parameter default in a way that survives JSON serialisation."""
    if default is inspect.Parameter.empty:
        return None
    if default is None:
        return "None"
    if isinstance(default, (str, int, float, bool)):
        return repr(default)
    if isinstance(default, Path):
        return f"Path({str(default)!r})"
    if isinstance(default, (datetime, date)):
        return default.isoformat()
    if isinstance(default, Enum):
        return f"{type(default).__qualname__}.{default.name}"
    # Sentinels (object() instances), classes, etc. — represent stably
    # by their repr; for sentinels in our SDK that's the right hint.
    return repr(default)


def _describe_callable(name: str, fn: Any) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"kind": "callable", "name": name, "signature": "(unintrospectable)"}

    params: list[dict[str, Any]] = []
    for p in sig.parameters.values():
        params.append(
            {
                "name": p.name,
                "kind": p.kind.name,  # POSITIONAL_OR_KEYWORD, KEYWORD_ONLY, …
                "annotation": _annotation_str(p.annotation),
                "default": _default_str(p.default),
            }
        )

    return {
        "kind": "callable",
        "name": name,
        "module": getattr(fn, "__module__", None),
        "params": params,
        "return": _annotation_str(sig.return_annotation),
    }


def _describe_dataclass(name: str, cls: type) -> dict[str, Any]:
    fields = []
    for f in dataclasses.fields(cls):
        fields.append(
            {
                "name": f.name,
                "type": _annotation_str(f.type),
                "default": _default_str(f.default)
                if f.default is not dataclasses.MISSING
                else None,
                "default_factory": (
                    f.default_factory.__qualname__
                    if f.default_factory is not dataclasses.MISSING
                    else None
                ),
            }
        )
    return {
        "kind": "dataclass",
        "name": name,
        "module": cls.__module__,
        "frozen": cls.__dataclass_params__.frozen,
        "slots": "__slots__" in cls.__dict__,
        "fields": fields,
    }


def _describe_exception(name: str, cls: type) -> dict[str, Any]:
    return {
        "kind": "exception",
        "name": name,
        "module": cls.__module__,
        "bases": [b.__qualname__ for b in cls.__bases__ if b is not object],
        "exit_code": getattr(cls, "exit_code", None),
    }


def _describe_export(name: str) -> dict[str, Any]:
    obj = getattr(sdk, name)

    if isinstance(obj, type):
        if dataclasses.is_dataclass(obj):
            return _describe_dataclass(name, obj)
        if issubclass(obj, BaseException):
            return _describe_exception(name, obj)
        return {
            "kind": "class",
            "name": name,
            "module": obj.__module__,
            "qualname": obj.__qualname__,
        }

    if callable(obj):
        return _describe_callable(name, obj)

    return {
        "kind": "value",
        "name": name,
        "type": _annotation_str(type(obj)),
        "repr": repr(obj),
    }


def build_schema() -> dict[str, Any]:
    """Build the deterministic schema document from `sdk.__all__`."""
    exports = sorted(sdk.__all__)
    described = [_describe_export(name) for name in exports]
    return {
        "schema_version": 1,
        "generator": "tools/dump_sdk_schema.py",
        "sdk_module": "sdk",
        "exports": described,
    }


def _serialise(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump SDK schema snapshot.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Diff the computed snapshot against the committed file; exit 1 on drift.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_SCHEMA_PATH,
        help=f"Output path (default: {_SCHEMA_PATH.relative_to(_REPO_ROOT)}).",
    )
    args = parser.parse_args(argv)

    payload = _serialise(build_schema())

    if args.check:
        if not args.out.exists():
            print(
                f"sdk schema snapshot missing: {args.out}\n"
                f"Regenerate with: python tools/dump_sdk_schema.py",
                file=sys.stderr,
            )
            return 1
        committed = args.out.read_text(encoding="utf-8")
        if committed != payload:
            print(
                "sdk schema drift detected:\n"
                f"  expected: {args.out}\n"
                f"  computed: differs from committed snapshot\n"
                "If the change was intentional, regenerate with:\n"
                "  python tools/dump_sdk_schema.py",
                file=sys.stderr,
            )
            return 1
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload, encoding="utf-8")
    print(f"Wrote {args.out.relative_to(_REPO_ROOT)} ({len(payload)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
