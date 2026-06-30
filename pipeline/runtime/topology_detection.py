"""
pipeline/runtime/topology_detection.py — deterministic topology heuristic.

This focused module owns the *topology* recommendation layer: the
``TopologyRecommendation`` value object and the pure ``recommend_topology``
function. Topology is a distinct axis from the semantic profile and the
operating mode (see ``run_shape.RunTopology``): it records whether a run is
best executed as a single-repo ``mono`` run or whether a multi-repo
``cross_recommended`` run is advisable.

Home rationale:

- ``run_shape.py`` owns the closed ``RunTopology`` / ``DeliveryScope`` enums
  and the inert run-shape value objects; the Architecture Fitness Gate forbids
  piling the heuristic onto that body.
- ``work_kind_detection.py`` owns the auto-detect *selector* (profile + mode);
  the topology heuristic is a distinct cohesive responsibility and lives here.

**Deterministic, provider-neutral, no LLM.** ``recommend_topology`` is a pure
function: it lower-cases the task text and does substring matching against a
data-driven table of signals (phrase → list of project aliases). It never
calls a model.

**Side-effect-free import.** Importing this module performs no I/O and reads no
configuration. The default signal table is loaded lazily — only when
``recommend_topology`` is called without an explicit ``signals`` mapping — via
``AutoDetectConfig``'s config module, mirroring ``work_kind_detection``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pipeline.runtime.run_shape import RunTopology

# The primary project alias placed first in any cross recommendation.
_PRIMARY_PROJECT = "orcho-core"

# Confidence emitted for a deterministic keyword match. High by construction:
# a substring match against the curated signal table is a strong signal.
_MATCH_CONFIDENCE = 0.8

# Short, provider-neutral rationale surfaced on a cross recommendation.
_CROSS_REASON = "core SDK wire change likely requires MCP schema/tool update"


@dataclass(frozen=True)
class TopologyRecommendation:
    """Deterministic topology recommendation for a run.

    Fields
    ------
    topology:
        ``RunTopology.MONO`` when no signal matched, or
        ``RunTopology.CROSS_RECOMMENDED`` when at least one signal matched.
    projects:
        Ordered, de-duplicated tuple of project aliases implicated by the
        matched signals. ``orcho-core`` is placed first as the primary when
        present. Empty for a mono recommendation.
    confidence:
        Float in the closed range 0.0..1.0. High (>=0.7) for a match, low for
        a mono recommendation.
    reason:
        Short, provider-neutral rationale. Empty for a mono recommendation.

    ``__post_init__`` validates types / coerces the enum and normalises
    ``projects`` to a tuple of strings. It performs no I/O.
    """

    topology: RunTopology
    projects: tuple[str, ...] = ()
    confidence: float = 0.0
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.topology, RunTopology):
            object.__setattr__(self, "topology", RunTopology(self.topology))
        object.__setattr__(
            self, "projects", _coerce_projects(self.projects)
        )
        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ):
            raise TypeError(
                "TopologyRecommendation.confidence must be a real number in "
                f"[0.0, 1.0], got {type(self.confidence).__name__}"
            )
        confidence = float(self.confidence)
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(
                "TopologyRecommendation.confidence must be within [0.0, 1.0], "
                f"got {confidence}"
            )
        object.__setattr__(self, "confidence", confidence)
        if not isinstance(self.reason, str):
            raise TypeError(
                "TopologyRecommendation.reason must be str, got "
                f"{type(self.reason).__name__}"
            )


def _coerce_projects(value: object) -> tuple[str, ...]:
    """Coerce a sequence of project aliases into a ``tuple[str, ...]``.

    A bare ``str`` is rejected to avoid iterating it into characters; a
    non-string element raises ``TypeError``.
    """

    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(
            "TopologyRecommendation.projects must be a sequence of str, got "
            f"{type(value).__name__}"
        )
    projects: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(
                "TopologyRecommendation.projects entries must be str, got "
                f"{type(item).__name__}"
            )
        projects.append(item)
    return tuple(projects)


def _order_projects(projects: Sequence[str]) -> tuple[str, ...]:
    """De-duplicate while preserving order; place the primary alias first."""

    ordered: list[str] = []
    for alias in projects:
        if alias not in ordered:
            ordered.append(alias)
    if _PRIMARY_PROJECT in ordered:
        ordered.remove(_PRIMARY_PROJECT)
        ordered.insert(0, _PRIMARY_PROJECT)
    return tuple(ordered)


def _default_signals() -> Mapping[str, Sequence[str]]:
    """Lazily load the default topology-signal table from app config.

    Reads ``AppConfig.pipeline['auto_detect']['topology_signals']`` — the
    shipped ``config.defaults.json`` table plus any local overlay. Imports the
    config module lazily so importing this module stays side-effect free with
    respect to config I/O.
    """

    from core.infra import config as _config

    auto_detect = _config.AppConfig.load().pipeline.get("auto_detect") or {}
    signals = auto_detect.get("topology_signals") or {}
    if not isinstance(signals, Mapping):
        return {}
    return signals


def recommend_topology(
    task: str,
    *,
    signals: Mapping[str, Sequence[str]] | None = None,
) -> TopologyRecommendation:
    """Deterministically recommend a run topology from the task text.

    Lower-cases ``task`` and substring-matches it against ``signals`` (phrase →
    list of project aliases). When ``signals`` is ``None`` the default table is
    loaded lazily from config. ``_comment`` keys (and any non-string phrase)
    are ignored.

    On at least one match: ``topology=CROSS_RECOMMENDED``, ``projects`` is the
    ordered union of matched aliases (``orcho-core`` first), confidence is high
    (>=0.7), and ``reason`` is a short provider-neutral phrase. With no match:
    ``topology=MONO``, empty ``projects``, low confidence, empty ``reason``.
    """

    if not isinstance(task, str):
        raise TypeError(
            f"recommend_topology task must be str, got {type(task).__name__}"
        )
    table = _default_signals() if signals is None else signals

    haystack = task.lower()
    matched: list[str] = []
    for phrase, aliases in table.items():
        if not isinstance(phrase, str) or phrase.startswith("_"):
            continue
        if phrase.lower() in haystack:
            if isinstance(aliases, str) or not isinstance(aliases, Sequence):
                continue
            for alias in aliases:
                if isinstance(alias, str):
                    matched.append(alias)

    if not matched:
        return TopologyRecommendation(
            topology=RunTopology.MONO,
            projects=(),
            confidence=0.0,
            reason="",
        )

    return TopologyRecommendation(
        topology=RunTopology.CROSS_RECOMMENDED,
        projects=_order_projects(matched),
        confidence=_MATCH_CONFIDENCE,
        reason=_CROSS_REASON,
    )


__all__ = [
    "TopologyRecommendation",
    "recommend_topology",
]
