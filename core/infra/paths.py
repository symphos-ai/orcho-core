"""core.infra.paths — public asset-path helpers.

External packages (``orcho-web``, ``orcho-mcp``, etc.) reference
``orcho-core`` runtime assets (config defaults, prompt templates)
by import, not by ``Path(__file__).parent.parent`` chains — those
break when consumers live in different distributions or when the
package is installed from a wheel.

Resolution uses :func:`importlib.resources.files` so it works for
both editable installs (assets live in the source tree under
``core/_prompts/`` and ``core/_config/``) and built wheels (assets
ship as package data of the ``core`` package). The returned values
are concrete :class:`pathlib.Path` instances — the assets are
always materialised on the filesystem for orcho-core's install
shapes, and downstream code that does ``Path`` operations (rglob,
read_text) keeps working unchanged.
"""
from __future__ import annotations

import os
from importlib.resources import files as _files
from pathlib import Path

from core import PACKAGE_ROOT

# ``files("core")`` returns a ``Traversable`` that, for any
# filesystem-backed install (the only shape orcho-core targets),
# resolves to a real :class:`pathlib.Path`. Wrap in ``Path`` so
# callers can call ``rglob`` / ``read_text`` without conditional
# branches.
_CORE_PKG: Path = Path(str(_files("core")))

CONFIG_DIR: Path = _CORE_PKG / "_config"
PROMPTS_DIR: Path = _CORE_PKG / "_prompts"
USER_CONFIG_DIR: Path = Path.home() / ".orcho"
SOURCE_ROOT: Path = _CORE_PKG.parent
"""Directory containing the installed top-level packages.

Use this as a subprocess ``cwd`` / ``PYTHONPATH`` anchor for tools
that need ``python -m cli.orcho`` to resolve sibling packages. Do not
use it for asset lookup; prefer :data:`CONFIG_DIR` and
:data:`PROMPTS_DIR`.
"""


def user_config_dir() -> Path:
    """Return the per-user Orcho config directory.

    Prefer this function in code that may run after tests or embedders
    monkeypatch ``Path.home()``. ``USER_CONFIG_DIR`` is kept as a
    module-level convenience for static callers.
    """
    return Path.home() / ".orcho"


def workspace_config_dir() -> Path | None:
    """Return ``$ORCHO_WORKSPACE/.orcho`` when ``ORCHO_WORKSPACE`` is set."""
    raw = os.environ.get("ORCHO_WORKSPACE")
    if not raw:
        return None
    return Path(raw).expanduser() / ".orcho"


__all__ = [
    "PACKAGE_ROOT",
    "CONFIG_DIR",
    "PROMPTS_DIR",
    "USER_CONFIG_DIR",
    "SOURCE_ROOT",
    "user_config_dir",
    "workspace_config_dir",
]
