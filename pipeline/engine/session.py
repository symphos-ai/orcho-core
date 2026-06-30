"""
pipeline/engine/session.py — Shared session dict management.

Provides init_session() and save_session() used by both orchestrator.py and
cross_orchestrator.py — previously duplicated as `save_session` /
`save_cross_session` (same logic, two definitions).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def init_session(**fields: Any) -> dict:
    """Return a fresh session dict with required top-level keys.

    Standard keys (always present):
      timestamp, status, phases

    Caller passes project-specific keys via **fields:
      project, projects, model, profile, etc.

    Example:
        session = init_session(
            task="Add logging",
            project=str(project_path),
            plugin=plugin.name,
            model="claude-sonnet",
            profile="feature",
        )
    """
    base: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "status": "running",
        "phases": {},
    }
    base.update(fields)
    return base


def save_session(output_dir: Path, session: dict) -> Path:
    """Write session dict as meta.json inside output_dir.

    The timestamp is encoded in the parent runs/{ts}/ directory name, so the
    filename is always fixed as ``meta.json``.

    Replaces the duplicate pair:
      orchestrator.save_session()       — identical logic
      cross_orchestrator.save_cross_session()
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    f = output_dir / "meta.json"
    f.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
    return f
