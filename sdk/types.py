"""Public dataclass shapes returned across multiple SDK modules.

Discipline: only shapes that two or more `sdk/*.py` modules genuinely
reference live here. Local-to-one-module dataclasses stay next to their
function and graduate here only on the second consumer.

All public dataclasses default to `frozen=True, slots=True` — embedders
get hashable, immutable values that round-trip cleanly through
`to_jsonable`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Run identity / metadata
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunRef:
    """Resolved pointer to a single run on disk."""

    run_id: str
    run_dir: Path


@dataclass(frozen=True, slots=True)
class RunMeta:
    """Parsed `meta.json` projection.

    Carries only fields the SDK promises to surface; the raw JSON is
    available under `extra` for embedders that need fields the SDK
    hasn't promoted yet.
    """

    project: str | None
    task: str
    status: str | None
    profile: str | None
    timestamp: str | None
    phases: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()  # cross-run aliases
    extra: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Status surface (consumed by status + cost + history rendering)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PhaseStatus:
    """One sub-project status row inside a cross-run."""

    name: str
    status: str | None


@dataclass(frozen=True, slots=True)
class RunStatus:
    """Full status snapshot of a single run.

    The CLI's `cmd_status` printer reads only these fields; embedders
    get the same projection without scraping `meta.json`/`metrics.json`
    on their side.

    ``next_actions`` (MCP UX A1, Principle 1): suggested follow-up
    tool calls the caller could invoke next, computed from the run's
    state. State-derived, recomputed on every load — no persistence,
    no drift risk. Empty tuple when the run is running, terminal-
    success, or in an unrecognised state. See :mod:`sdk.actions` for
    the rule set.
    """

    run_ref: RunRef
    meta: RunMeta | None
    total_tokens: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_duration_s: float = 0.0
    total_rounds: int = 0
    total_retries: int = 0
    sub_projects: tuple[PhaseStatus, ...] = ()
    worktree: dict[str, Any] | None = None
    raw_meta: dict[str, Any] = field(default_factory=dict)
    raw_metrics: dict[str, Any] = field(default_factory=dict)
    next_actions: tuple = ()  # tuple[Action, ...] — avoids forward-ref cycle
    artefacts: tuple = ()  # tuple[ArtefactRef, ...] — avoids forward-ref order


# ─────────────────────────────────────────────────────────────────────────────
# History / metrics
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One row in the history listing.

    Mirrors the fields the CLI's `cmd_history` table prints; nothing
    more, so embedders can render a similar table without re-loading
    `meta.json`. `project` is the raw `meta["project"]` string (or
    `None`); cross-run aliases live in `cross_aliases` so formatters
    can render either form without re-parsing the meta dict.
    """

    run_id: str
    run_dir: Path
    task: str
    status: str | None
    project: str | None
    cross_aliases: tuple[str, ...]
    timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Per-run metrics summary surfaced by `sdk.metrics`."""

    run_id: str
    run_dir: Path
    total_tokens: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_duration_s: float = 0.0
    total_rounds: int = 0
    total_retries: int = 0
    total_cost_usd_equivalent: float = 0.0
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One event from a run's ``events.jsonl`` stream.

    Mirrors the durable event-store shape while keeping embedders away
    from ``core.observability.events`` internals.
    """

    seq: int
    ts: str
    kind: str
    phase: str | None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtefactRef:
    """Agent-facing pointer to a readable artefact of a run.

    Carried on ``RunStatus.artefacts`` so embedders / agents discover
    what they can read for a run without scanning ``run_dir`` themselves
    or remembering filename conventions.

    ``uri`` uses the MCP resource scheme (``orcho://``) — same scheme
    ``sdk.actions`` already uses for the MCP tool names it returns.
    The SDK contains the agent-facing hint; the concrete resource
    implementation lives in ``orcho-mcp``. See ADR 0045 for the
    scheme-separation rationale.

    ``size_bytes`` is ``None`` for composable resources (e.g. evidence,
    assembled from multiple files at read time by
    ``sdk.evidence.collect_evidence``). Physical artefacts
    (``parsed_plan.json``, ``diff.patch``) carry the file size from
    ``os.stat``.

    ``kind`` is a plain ``str`` (not Literal) so the SDK stays
    forward-compatible if a new artefact kind is added in a later
    release. Embedders narrow to a closed set at their wire layer —
    e.g. orcho-mcp's wire model tightens ``kind`` to
    ``Literal["parsed_plan", "evidence", "diff"]``.
    """

    kind: str
    uri: str
    mime: str
    size_bytes: int | None


# ─────────────────────────────────────────────────────────────────────────────
# Cost
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PhaseBreakdown:
    """One row in the by-phase aggregation of a `CostReport`."""

    name: str
    cost: float
    tokens: int
    runs: int
    tokens_exact: bool
    cost_estimated: bool = False


@dataclass(frozen=True, slots=True)
class AgentBreakdown:
    """One row in the by-runtime aggregation of a `CostReport`.

    The ``provider`` field is the resolved agent-runtime id when the phase
    metrics carry one (e.g. ``claude``, ``claude-glm``); otherwise it falls
    back to a model→provider bucket (``claude`` / ``codex`` / ``gemini`` /
    ``other``) for legacy runs written without a runtime id. The field name
    stays ``provider`` for wire compatibility.
    """

    # Resolved runtime id (e.g. ``claude`` / ``claude-glm``) when the phase
    # carries one, else a model→provider fallback bucket
    # (``claude`` / ``codex`` / ``gemini`` / ``other``).
    provider: str
    cost: float
    tokens: int
    runs: int
    tokens_exact: bool
    cost_estimated: bool = False


@dataclass(frozen=True, slots=True)
class CostRunRow:
    """One per-run line inside a `CostReport`."""

    run_id: str
    task: str
    cost: float
    tokens: int
    tokens_in: int
    tokens_out: int
    duration_s: float
    rounds: int
    retries: int
    cost_estimated: bool = False


@dataclass(frozen=True, slots=True)
class CostReport:
    """Aggregated cost report across runs in a window.

    Pure data — no formatting, no print. The CLI's `format_cost_report`
    consumes this; embedders consume the same shape.
    """

    runs_dir: Path
    window: str  # "7d" / "30d" / "all"
    cutoff: datetime | None
    total_runs: int
    total_cost: float
    total_tokens: int
    total_tokens_in: int
    total_tokens_out: int
    total_duration_s: float
    rows: tuple[CostRunRow, ...]
    top_runs: tuple[CostRunRow, ...]
    phase_breakdown: tuple[PhaseBreakdown, ...]
    agent_breakdown: tuple[AgentBreakdown, ...]
    priced_entries_count: int
    pricing_source: str | None  # "user" / "bundled" / None
    pricing_snapshot_date: str | None  # ISO date of source snapshot
    pricing_snapshot_age_days: int | None
    any_estimated: bool
    accounting_enabled: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Evidence
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Composed evidence bundle for a run.

    `body` is the structured evidence dict (pipeline.evidence schema);
    `markdown` is the rendered presentation. Embedders pick the form
    they need; CLI prints `markdown`, MCP returns `body`.
    """

    run_ref: RunRef
    body: dict[str, Any]
    markdown: str
    valid: bool
    validation_errors: tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PromptResolutionStep:
    """One link in the prompt-resolution chain."""

    location: str  # human-readable origin (project / workspace / engine / built-in)
    path: Path | None
    exists: bool
    is_winner: bool


@dataclass(frozen=True, slots=True)
class PromptResolution:
    """Result of resolving a prompt by name.

    `chain` is ordered from highest-priority candidate to lowest.
    `winner` is the path that actually loaded (or `None` when none did).
    """

    name: str
    chain: tuple[PromptResolutionStep, ...]
    winner: Path | None
    body: str | None  # final rendered prompt body, when resolvable


# ─────────────────────────────────────────────────────────────────────────────
# Pricing
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PricingEntry:
    """One row in the merged pricing table."""

    model: str
    input_per_million: float | None
    output_per_million: float | None
    source: str  # "user" / "bundled"


@dataclass(frozen=True, slots=True)
class PricingTable:
    """Snapshot of the active pricing table."""

    entries: tuple[PricingEntry, ...]
    user_snapshot_date: str | None  # ISO date string
    bundled_snapshot_date: str | None
    snapshot_age_days: int | None
    user_path: Path | None
    bundled_path: Path | None


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of a successful pricing refresh."""

    written_path: Path
    snapshot_date: str  # ISO date string
    models_count: int
    source_label: str  # human-readable "scraped from …" tag
