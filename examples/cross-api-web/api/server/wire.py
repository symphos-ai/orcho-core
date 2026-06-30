"""Producer-module dispatcher.

Re-imports the producer module on every call so edits to ``api/*.py``
take effect without restarting the server. This module owns the only
``importlib.reload`` in the demo.
"""
from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping

from .sections import ProducerRef


def ensure_on_path(api_root: str) -> None:
    if api_root not in sys.path:
        sys.path.insert(0, api_root)


def call_producer(
    ref: ProducerRef, body: Mapping[str, object], api_root: str,
) -> dict:
    ensure_on_path(api_root)
    module = importlib.import_module(ref.module)
    importlib.reload(module)
    func = getattr(module, ref.func)
    args = [str(body[arg]).strip() for arg in ref.arg_names]
    return dict(func(*args))
