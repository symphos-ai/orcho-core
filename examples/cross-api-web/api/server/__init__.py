"""HTTP demo server internals.

The split mirrors a small production layout — sections describe the
domain, db owns storage, wire owns hot-swappable producers, handlers
own CRUD logic, routing/http own framing. ``demo_server.py`` is the
entry-point wrapper.
"""
from __future__ import annotations

from .http import build_handler

__all__ = ["build_handler"]
