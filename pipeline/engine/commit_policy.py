"""pipeline/engine/commit_policy.py — the delivery policy a parked gate pins.

A deferred delivery gate is decided out of band (SDK ``decide_delivery``,
``orcho delivery decide``, ``orcho_delivery_decide``) by a process whose
``AppConfig`` resolves from *its own* environment and cwd, not from the run's
workspace. The replay used to take ``branch_policy`` / ``publish`` /
``default_strategy`` from that process, so the same parked gate could commit
into the checkout from one shell and onto a published branch from another.

This module owns the snapshot the producer stamps on the parked decision at
park time and the overlay the replay applies: one owner for "which delivery
policy this gate was parked under". Values are normalised through the same
helpers the engine reads at delivery, never through a second table.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pipeline.engine.delivery_branch import normalize_branch_policy
from pipeline.engine.delivery_publish import normalize_publish_gate

__all__ = [
    "COMMIT_POLICY_KEYS",
    "normalize_commit_message_strategy",
    "overlay_commit_policy",
    "snapshot_commit_policy",
]

# Exactly the keys ``apply_commit_delivery`` / ``resolve_delivery_branch`` /
# ``publish_delivery`` read to decide WHERE and HOW a delivery lands.
# ``add_untracked`` stays pinned through ``include_untracked`` on the decision
# itself (ADR 0100); ``decision_mode`` is forced by the replay.
COMMIT_POLICY_KEYS: tuple[str, ...] = (
    "branch_policy",
    "branch_name",
    "publish",
    "publish_provider",
    "default_strategy",
)

_STRATEGIES = frozenset({"release_summary", "llm_generate", "operator_typed"})


def normalize_commit_message_strategy(raw: object) -> str:
    """Coerce ``commit.default_strategy`` to a known value (default fallback)."""
    value = str(raw or "release_summary").strip()
    return value if value in _STRATEGIES else "release_summary"


def snapshot_commit_policy(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalised delivery policy of ``cfg``, in the shape the replay overlays.

    Optional string keys (``branch_name``, ``publish_provider``) are kept only
    when set, so a default configuration snapshots to the three policy axes.
    """
    cfg = dict(cfg or {})
    out: dict[str, Any] = {
        "branch_policy": normalize_branch_policy(cfg.get("branch_policy")),
        "publish": normalize_publish_gate(cfg.get("publish")),
        "default_strategy": normalize_commit_message_strategy(cfg.get("default_strategy")),
    }
    for key in ("branch_name", "publish_provider"):
        value = str(cfg.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def overlay_commit_policy(
    base: Mapping[str, Any], snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """``base`` with the snapshot's policy keys pinned on top.

    Only :data:`COMMIT_POLICY_KEYS` are taken from the snapshot; every other
    key keeps the caller's value. A missing / non-mapping snapshot returns a
    copy of ``base`` unchanged (legacy gate parked before snapshots existed).
    """
    out = dict(base)
    if not isinstance(snapshot, Mapping):
        return out
    for key in COMMIT_POLICY_KEYS:
        if key in snapshot:
            out[key] = snapshot[key]
    return out
