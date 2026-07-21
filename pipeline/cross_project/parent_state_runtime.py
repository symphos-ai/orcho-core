"""Live-session adapter for the canonical cross-parent state reducer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.run_state.cross_parent import (
    CrossParentFacts,
    CrossParentState,
    reduce_cross_parent_state,
)
from pipeline.run_state.cross_parent_disk import facts_from_session


def build_cross_parent_facts(
    session: dict[str, Any], checkpoint: dict[str, Any], run_dir: Path | str
) -> CrossParentFacts:
    """Adapt explicit live state while retaining exact physical child facts."""
    return facts_from_session(session, checkpoint, run_dir)


def reduce_runtime_cross_parent_state(
    session: dict[str, Any], checkpoint: dict[str, Any], run_dir: Path | str
) -> CrossParentState:
    """Return the same reduction a durable reader obtains for equivalent facts."""
    return reduce_cross_parent_state(build_cross_parent_facts(session, checkpoint, run_dir))


__all__ = ["build_cross_parent_facts", "reduce_runtime_cross_parent_state"]
