"""``_PipelineRun`` — the single-project execution-state object.

Moved out of ``pipeline.project_orchestrator`` per ADR 0042 Phase F.
Captures everything the imperative ``run_pipeline`` body used to thread
through closures: inputs, resolved models, mode gates, agent provider,
phase_config + state + registry + session, metrics, checkpoint store,
session-mode flags, follow-up linkage, worktree context, and the
``_dispatch_active`` guard.

The class is a ``@dataclass``; field shape is part of the orchestrator's
private contract (tests in ``tests/unit/pipeline/orchestrator/`` and
``tests/unit/pipeline/quality_gates/`` build it directly). Methods stay
as-is from the orchestrator with one exception: ``finalize()`` is now a
thin delegator that builds a :class:`pipeline.project.finalization.FinalizationContext`
and calls :func:`pipeline.project.finalization.finalize_with_terminal_output`
(ADR 0042 Phase G — silent service + terminal wrapper split).

Two small helpers travel with the class because they are called
exclusively from inside it and have no other consumers:

* ``_run_one_phase`` — inline-profile dispatch wrapper called by
  ``_PipelineRun._safe_phase``.
* ``_is_mock_provider`` — provider-shape check used by
  ``_PipelineRun._on_phase_end`` for the mock-plan-write fallback.

The DONE-summary trio (``_render_done_summary`` /
``_done_phase_outcome`` / ``_profile_phase_names_in_order``) moved on
to :mod:`pipeline.project.finalization` in Phase G — that module is
now their only consumer.

The orchestrator re-imports ``_PipelineRun`` under the legacy alias
so ``run_pipeline``'s instantiation site stays byte-identical. Tests
that build ``_PipelineRun`` directly were repointed at
``pipeline.project.run`` in the same commit.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from agents.stall_protocol import AgentCommandStalledError
from core.infra import config
from core.io.ansi import C, paint
from core.io.retry import (
    AgentAccessError,
    AgentCallError,
    provider_access_detail,
    sanitized_failure_excerpt,
)
from core.observability.logging import warn
from core.observability.metrics import MetricsCollector
from pipeline.checkpoint import CheckpointStore, PipelineStatus
from pipeline.engine import save_session
from pipeline.plugins import PluginConfig
from pipeline.project import provider_recovery
from pipeline.project.correction_route import derive_correction_route
from pipeline.project.profile_dispatch import (
    emit_phase_banner,
    emit_phase_log_end,
)
from pipeline.project.types import PresentationPolicy
from pipeline.run_state.provider_runtime import (
    build_provider_runtime_failure,
    is_provider_runtime_failure,
)
from pipeline.run_state.release_verdict import is_release_blocked
from pipeline.run_state.stalled_command import build_stalled_command_failure
from pipeline.run_state.terminal import (
    mark_run_failed,
    mark_run_halted,
    mark_run_stalled,
)
from pipeline.runtime import PipelineState
from pipeline.runtime.runner import PHASE_END_SKIPPED_KEY, PHASE_PRE_SKIP_KEY
from pipeline.verification_contract import FINAL_PHASES
from pipeline.verification_delivery import assess_delivery_verification

# ── helpers (private; Phase G lifts the DONE-summary trio to
# pipeline.project.finalization) ─────────────────────────────────────────


# Inline status tints for a verification-gate live block. Multi-word tokens
# come first so they tint before their single-word substrings. Pure
# presentation: the line *content* is built by verification_timeline; this only
# overlays the house color style (neutral header, never success-green).
_GATE_STATUS_COLORS: tuple[tuple[str, str], ...] = (
    ("SKIPPED MANUAL", C.YELLOW),
    ("MISSING/STALE", C.RED),
    ("FRESH", C.CYAN),
    ("FAIL", C.RED),
    ("PASS", C.GREEN),
)


def _print_gate_live_block(lines: tuple[str, ...]) -> None:
    """Print a verification-gate live block in the house color style.

    No-op for an empty block (the renderer returns ``()`` for the
    not-attempted no-op). The header is neutral cyan; status tokens on the body
    lines are tinted inline. The plain block is also mirrored to ``output.log``
    so final acceptance and follow-up reviewers can audit the same official
    gate signal the operator saw live. All line content is built by
    :func:`pipeline.project.verification_timeline.render_gate_live_block`.
    """
    if not lines:
        return
    from agents.stream_log import append_agent_log_section

    append_agent_log_section(lines[0], "\n".join(lines[1:]))
    # Summary mode: collapse the framed block to a single presenter line.
    # The durable ``output.log`` mirror above runs in every mode, so this
    # is purely a stdout substitution; live/debug keep the multi-line block
    # below byte-identical. Branch strictly on ``get_output_mode``.
    from core.observability.logging import get_output_mode
    if get_output_mode() == "summary":
        print(_gate_summary_line(lines))
        return
    print()
    print(paint("+-- Official verification gates", C.CYAN, C.BOLD))
    print(paint(f"| {lines[0]}", C.CYAN, C.BOLD))
    for line in lines[1:]:
        for token, color in _GATE_STATUS_COLORS:
            if token in line:
                line = line.replace(token, paint(token, color))
        print(f"| {line}")
    print(paint("+-- durable receipts for this run", C.GREY))
    print()


def _gate_summary_line(lines: tuple[str, ...]) -> str:
    """Collapse one gate live block into a single presenter line.

    Parses the pre-rendered block: the header carries the timing/hook
    label, each ``envs:`` / ``commands:`` body line carries the
    ``name PASS|FAIL`` gate tokens, and an optional ``receipts:`` line
    carries the durable receipt paths (whose parent is the run's receipts
    directory). Gate names, verdicts, and the receipts path are structured
    tokens — the presenter never truncates them. ``ok`` drives the
    ``✓``/``✗`` glyph and is false when any gate token is unsuccessful
    (``FAIL`` or a residual ``MISSING/STALE``) or when the pass captured
    executor errors (an ``errors: N`` body line). On success the presenter
    shows the
    receipts *directory*; on failure this collapses to the *specific*
    failing receipt file when it can be matched by gate name, so a
    ``✗ gates … · receipt → <path>`` line points at the real failing proof
    rather than only its parent directory.
    """
    from core.io import summary_lines

    header = lines[0]
    timing = header.removeprefix("Verification gates").lstrip(" —-") or header
    result_parts: list[str] = []
    receipt_paths: list[str] = []
    failing_names: list[str] = []
    has_errors = False
    for line in lines[1:]:
        if line.startswith("receipts: "):
            receipt_paths.extend(
                p for p in line[len("receipts: "):].split(" · ") if p
            )
            continue
        if line.startswith("errors: "):
            has_errors = True
            result_parts.append(line)
            continue
        body = line
        for prefix in ("envs: ", "commands: "):
            if line.startswith(prefix):
                body = line[len(prefix):]
                break
        result_parts.append(body)
        failing_names.extend(_failing_gate_names(body))
    results = " · ".join(result_parts)
    ok = not failing_names and not has_errors
    receipts_path = _gate_receipts_reference(receipt_paths, failing_names, ok=ok)
    return summary_lines.gates_line(timing, results, receipts_path, ok)


# Gate-token statuses that mean the gate did not succeed. ``FAIL`` is an
# executed failure; ``MISSING/STALE`` is a residual required receipt that was
# never satisfied — both must break the summary ``✓`` glyph. ``FRESH``,
# ``PASS`` and ``SKIPPED MANUAL`` are not failures.
_FAILING_GATE_STATUSES: frozenset[str] = frozenset({"FAIL", "MISSING/STALE"})


def _failing_gate_names(body: str) -> list[str]:
    """Extract the ``name`` of each unsuccessful ``name STATUS`` gate token.

    Gate tokens are ``·``-joined ``name STATUS`` pairs; ``STATUS`` is the
    final whitespace-delimited word (``PASS``/``FAIL``/``MISSING/STALE``/…)
    and everything before it is the gate name (which itself may contain
    spaces). A token counts as failing when its status is in
    :data:`_FAILING_GATE_STATUSES` (``FAIL`` or a residual ``MISSING/STALE``).
    """
    names: list[str] = []
    for token in body.split(" · "):
        parts = token.rsplit(" ", 1)
        if len(parts) == 2 and parts[1] in _FAILING_GATE_STATUSES:
            names.append(parts[0].strip())
    return names


def _gate_receipts_reference(
    receipt_paths: list[str], failing_names: list[str], *, ok: bool,
) -> str:
    """Pick the receipts reference for the summary gate line.

    On success (or when no receipt paths are known) return the receipts
    *directory* — the parent of the first receipt path. On failure, try to
    match a failing gate name to its specific receipt file (by sanitized
    filename stem, covering both command receipts ``<stem>.json`` and env
    receipts ``verify_env_<stem>.json``) and return that concrete file so
    the operator lands on the real failing proof; fall back to the
    directory when no path matches.
    """
    if not receipt_paths:
        return ""
    receipts_dir = str(Path(receipt_paths[0]).parent)
    if ok:
        return receipts_dir
    from pipeline.evidence.verification_receipt import _sanitize_filename_stem

    for name in failing_names:
        stem = _sanitize_filename_stem(name)
        candidates = {stem, f"verify_env_{stem}"}
        for path in receipt_paths:
            if Path(path).stem in candidates:
                return path
    return receipts_dir


def _auto_run_required_receipts_live(
    run: Any,
    phase: str,
    *,
    reason: str,
    hook_label: str,
) -> None:
    """Materialize required receipts and render the official live block.

    Kept as the single pre-phase presentation seam for Stage 9 so lifecycle
    callers do not fork the evidence path. The underlying autorun module owns
    all guards (dry-run, no contract, empty required set, manual-only commands)
    and returns a no-op result when there is nothing official to run.
    """
    from pipeline.project.verification_autorun import auto_run_required_receipts

    autorun_result = auto_run_required_receipts(run, phase, reason=reason)
    if getattr(run, "_presentation", None) is PresentationPolicy.TERMINAL:
        from pipeline.project.verification_timeline import render_gate_live_block

        _print_gate_live_block(
            render_gate_live_block(autorun_result, hook_label=hook_label),
        )


def _gate_events_list(run: Any) -> list:
    """Read-only view of the durable scheduled-gate decision trail (or [])."""
    from pipeline.project.gate_repair import VERIFICATION_GATE_EVENTS_KEY

    extras = getattr(getattr(run, "state", None), "extras", None)
    events = extras.get(VERIFICATION_GATE_EVENTS_KEY) if isinstance(extras, dict) else None
    return events if isinstance(events, list) else []


def _print_scheduled_gate_live_blocks(
    run: Any, new_records: list[Mapping[str, Any]],
) -> None:
    """Print one framed live block per hook for scheduled gate decisions (F2).

    ``new_records`` is the *delta* of ``extras['verification_gate_events']``
    recorded across a single gate seam (the gate_repair recorder appends its
    routing decisions there). This is purely READ-ONLY over that recorded
    evidence — it never re-runs a gate or re-reads the receipt directory; a
    fresh-but-unexecuted gate surfaces from its ``skipped_fresh`` decision, not
    from the on-disk receipt. No-op outside TERMINAL or with no decisions. The
    delta can span two hooks (before_phase + before_delivery on a FINAL phase),
    so it is grouped by hook label and printed as one block each.
    """
    if getattr(run, "_presentation", None) is not PresentationPolicy.TERMINAL:
        return
    if not new_records:
        return
    from pipeline.project.verification_timeline import (
        render_scheduled_gate_live_block,
        scheduled_hook_label,
    )

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in new_records:
        if not isinstance(record, Mapping):
            continue
        label = scheduled_hook_label(
            str(record.get("hook", "")), str(record.get("phase", "")),
        )
        groups.setdefault(label, []).append(record)
    for label, records in groups.items():
        _print_gate_live_block(
            render_scheduled_gate_live_block(records, hook_label=label),
        )


def _run_one_phase(
    phase_names, state, *, registry, on_phase_start=None, on_phase_end=None,
):
    """Build an inline PipelineProfile and dispatch through ``run_profile``.

    Tags any exception with the failing phase name (``_orcho_failed_phase``)
    so the orchestrator's outer try/except can record a structured failure
    in meta.json instead of leaking up to atexit's generic ``interrupted``.
    """
    from pipeline.runtime import PipelineProfile, run_profile
    profile = PipelineProfile(name="<inline>", phases=tuple(phase_names))
    try:
        return run_profile(
            profile,
            state,
            registry,
            on_phase_start=on_phase_start,
            on_phase_end=on_phase_end,
        )
    except Exception as exc:
        if not getattr(exc, "_orcho_failed_phase", None):
            # frozen / immutable exception class — best-effort tag
            with contextlib.suppress(Exception):
                exc._orcho_failed_phase = phase_names[0] if phase_names else "?"
        raise


def _is_mock_provider(provider) -> bool:
    """Detect MockAgentProvider so plan_*.md mock-write parity is preserved."""
    try:
        from agents.runtimes import MockAgentProvider
    except Exception:
        return False
    return isinstance(provider, MockAgentProvider)


def _session_allows_commit_delivery(session: Mapping[str, object]) -> bool:
    if session.get("status") == "done":
        return True
    phases = session.get("phases")
    if not isinstance(phases, Mapping):
        return False
    final_acceptance = phases.get("final_acceptance")
    if not isinstance(final_acceptance, Mapping):
        return False
    verdict = final_acceptance.get("verdict")
    return isinstance(verdict, str) and verdict.upper() == "REJECTED"


def _failure_metadata_for_exception(
    exc: Exception,
    *,
    failed_phase: str,
    model_resolver: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """Provider-neutral durable failure fields for terminal failures.

    Delegates the full provider-access recovery projection — failed
    phase/runtime/model plus the durable ``recovery_actions`` list — to
    :mod:`pipeline.project.provider_recovery`. The configured phase maps and
    the failed phase's runtime/model are resolved here; resolution is
    defensive because this runs inside the failure handler and must never mask
    the original failure with a new exception. ``model_resolver`` is the run's
    ``_model_for_phase`` (passed in so this stays a free function); it falls
    back to the configured model map when unavailable. Non-provider-access
    exceptions keep the historical empty-meta behaviour, leaving their
    ``error`` fields and downstream consumers untouched.
    """
    if isinstance(exc, AgentCommandStalledError):
        # Terminal stalled command (idle-timeout escalation). The bounded
        # carrier already holds the diagnostic; the provider-neutral recovery
        # record (failed phase, reason, recovery_actions) is built from it.
        return build_stalled_command_failure(exc.stalled)
    # Precedence is load-bearing: ``AgentAccessError`` is a subclass of
    # ``AgentCallError`` and must classify to ``provider_access`` BEFORE the
    # ``provider_runtime`` branch, otherwise a provider-access failure would be
    # mis-tagged. The ``provider_runtime`` set
    # ({RateLimit, ApiConnection, ApiTimeout, SystemResource}) is disjoint from
    # access, so the ordering is also a guard, not only a fast path.
    if isinstance(exc, AgentAccessError):
        runtime, model, runtime_map, model_map = _resolve_failed_phase_runtime_model(
            failed_phase, model_resolver,
        )
        # Validation-only registry set (fix F3): the configured phase maps are
        # the sole *source* of replacement candidate pairs, but a configured
        # pair whose runtime is not actually registered in the
        # ``AgentRegistry`` is not executable — surfacing it as a ``replace``
        # option would later be rejected by ``persist_runtime_override`` and
        # confuse the operator. Resolve the registered runtime names here and
        # pass them as a validation filter so the durable ``recovery_actions``
        # only carry buildable runtimes. Defensive: this runs inside the
        # failure handler, so a registry-discovery error must never mask the
        # original failure — fall back to ``None`` (no extra filter).
        known_runtimes: list[str] | None
        try:
            from agents.registry import AgentRegistry
            known_runtimes = AgentRegistry.default().names()
        except Exception:
            known_runtimes = None
        return provider_recovery.build_provider_access_recovery(
            failed_phase=failed_phase,
            runtime=runtime,
            model=model,
            runtime_map=runtime_map,
            model_map=model_map,
            known_runtimes=known_runtimes,
        )
    if is_provider_runtime_failure(exc):
        # Recoverable provider/runtime condition (rate-limit / connection /
        # timeout / local resource) escalated past the retry budget. Stable
        # ``provider_runtime`` classification with the sanitized provider
        # message; resume/retry-phase is the safe next action (ADR 0118). The
        # runtime/model are resolved the same way as the access path so the
        # durable record names which phase config to resume.
        runtime, model, _, _ = _resolve_failed_phase_runtime_model(
            failed_phase, model_resolver,
        )
        return build_provider_runtime_failure(
            exc, failed_phase=failed_phase, runtime=runtime, model=model,
        )
    # Persist a sanitized excerpt of the raw failure output for any other
    # agent-call failure (generic ``AgentCallError`` / auth / context-overflow)
    # so the captured provider signature survives the process exit and a later
    # resume, which overwrites the raw run logs. Without it a generic
    # ``AgentCallError`` lands as a bare ``exit=N`` message and the real
    # stderr — the only evidence that could later classify the failure — is
    # lost. Routed through the same JSONL-stripping channel as the access path
    # (ADR 0101 sanitary boundary); empty excerpt stores nothing.
    if isinstance(exc, AgentCallError):
        excerpt = sanitized_failure_excerpt(exc)
        return {"stderr_excerpt": excerpt} if excerpt else {}
    return {}


def _resolve_failed_phase_runtime_model(
    failed_phase: str,
    model_resolver: Callable[[str], str] | None,
) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """Resolve the failed phase's configured runtime/model and the phase maps.

    Shared by the ``provider_access`` and ``provider_runtime`` branches of
    :func:`_failure_metadata_for_exception`. Defensive throughout — this runs
    inside the failure handler and must never mask the original failure with a
    new exception, so a config-load error degrades to empty maps and the
    ``model_resolver`` call is suppressed. ``model_resolver`` is the run's
    ``_model_for_phase`` (passed in to keep this a free function); it falls
    back to the configured model map when unavailable.
    """
    try:
        app_cfg = config.AppConfig.load()
        runtime_map = app_cfg.phase_runtime_map
        model_map = app_cfg.phase_model_map
    except Exception:
        runtime_map, model_map = {}, {}
    runtime = runtime_map.get(failed_phase, "claude")
    model = ""
    if model_resolver is not None:
        with contextlib.suppress(Exception):
            model = model_resolver(failed_phase)
    if not model:
        model = model_map.get(failed_phase, "")
    return runtime, model, runtime_map, model_map


def _record_commit_delivery_waived(
    session: MutableMapping[str, Any], assessment: object,
) -> None:
    """Persist waived required gates onto the session (non-wire evidence).

    When the Stage 6 assessment carries ``waived_gates`` (a required receipt
    excused by an exact durable ``phase_handoff_waiver``), record one entry per
    gate under ``session['commit_delivery_verification_waived']`` so it lands in
    ``meta.json`` via ``save_session``. Each entry carries the ``command`` /
    ``gate_name`` (the gate command), the durable ``handoff_id``, a bounded
    ``waiver_preview`` (the waiver rationale), and the ``status`` (``failed`` /
    ``missing``). Best-effort: any error degrades silently and never breaks
    delivery, and the key is omitted entirely when nothing was waived (additive,
    byte-identical otherwise). Kept off the SDK/MCP wire — it never travels
    through ``CommitDeliveryDecision.to_dict()``.

    Module-level (not a method) so the focused stub-based unit tests can invoke
    the delivery path against a lightweight ``SimpleNamespace`` ``self``.
    """
    try:
        waived = getattr(assessment, "waived_gates", None) or ()
        records = [
            {
                "command":        w.gate_command,
                "gate_name":      w.gate_command,
                "handoff_id":     w.handoff_id,
                "waiver_preview": w.waiver_text or w.note or "",
                "status":         w.status,
            }
            for w in waived
        ]
        if records:
            session["commit_delivery_verification_waived"] = records
        else:
            # Current delivery went through no waiver: clear any stale entry from
            # an earlier waived attempt in the same session so persisted evidence
            # (meta.json / evidence.json / DONE) reflects THIS delivery, and the
            # no-waiver path stays byte-identical (key absent).
            session.pop("commit_delivery_verification_waived", None)
    except Exception:  # noqa: BLE001 — evidence must never break delivery
        return


def _record_multi_project_delivery(
    session: MutableMapping[str, Any], decision: Any,
) -> None:
    """Propagate the T1 companion disclosure into a durable session block.

    Mirrors the companion repos carried on the resolved/applied delivery
    decision (``decision.scope_companions`` — derived in T1 from the durable plan
    scope, NOT re-scanned here) into ``session['multi_project_delivery']``: the
    primary delivery status plus per-repo ``{alias, path, state, changed_paths}``
    disclosure. This is the single durable surface finalization reads to raise a
    companion caveat
    (:func:`pipeline.project.finalization.build_companion_delivery_caveat`) and
    the evidence collector reads to emit the ``multi_project_delivery`` bundle
    block. No-op (block omitted, byte-identical) when the run touched no companion
    repo, so a clean single-repo mono run is unaffected.

    Module-level (not a method) so the focused stub-based delivery unit tests can
    invoke the delivery path against a lightweight ``SimpleNamespace`` ``self``.
    """
    companions = getattr(decision, "scope_companions", ()) or ()
    if not companions:
        return
    session["multi_project_delivery"] = {
        "primary_status": decision.status,
        "companions": [companion.to_dict() for companion in companions],
    }


def _capture_companion_bases_early(run: Any) -> None:
    """Record companion base revisions at plan-end, before implementation (F2).

    Fired from ``_on_phase_end`` the first time the durable plan scope exists (the
    plan / decompose phase). That moment is *before* the implementation phase can
    advance any companion HEAD, so the captured base is the companion's pre-work
    revision — the observable signal a later delivery scan measures a committed
    advance against. Capturing here (not at delivery time) is what lets a
    companion that is cleanly committed *during* the run read as ``committed``
    instead of mis-classifying as ``planned_requirement``.

    Delegates derivation + capture to
    :func:`pipeline.engine.delivery_scope.record_companion_bases_at_detection`
    (idempotent — never moves an already-recorded base) and persists ``meta.json``
    only when a new base was actually recorded. Module-level so the focused
    finalization / delivery unit tests can exercise it against a lightweight
    ``SimpleNamespace`` ``self``. Strict no-op (and never raises) for a run with no
    ``delivery_scope`` or no derivable companion — the hot path of every run.
    """
    output_dir = getattr(run, "output_dir", None)
    session = getattr(run, "session", None)
    project_path = getattr(run, "project_path", None)
    if (
        output_dir is None
        or not isinstance(session, MutableMapping)
        or project_path is None
    ):
        return
    auto = session.get("auto_detect")
    if not isinstance(auto, Mapping) or not auto.get("delivery_scope"):
        return
    from pipeline.engine.delivery_scope import record_companion_bases_at_detection

    before = dict(auto.get("companion_base_revisions") or {})
    record_companion_bases_at_detection(
        session=session,
        primary_project_dir=Path(project_path),
        run_dir=Path(output_dir),
    )
    auto_after = session.get("auto_detect")
    after = (
        dict(auto_after.get("companion_base_revisions") or {})
        if isinstance(auto_after, Mapping) else {}
    )
    if after != before:
        with contextlib.suppress(Exception):
            save_session(output_dir, session)


def _push_handoff_advice_usage(run: Any) -> None:
    """Push observe-only handoff-advice usage into metrics from durable artifacts.

    Module-level (not a method) so a focused ``_fsm_metrics`` unit test can
    invoke the phase-end path against a lightweight stand-in ``self`` without
    having to mimic this capability; every attribute is read via ``getattr``, so
    an incomplete stand-in (no ``output_dir`` / ``session`` / record API) is a
    clean no-op.

    Source of truth is the durable advice surface (``phase_handoff_advice/`` +
    ``phase_handoff_decisions/`` under ``output_dir``), normalized by the T1 leaf
    ``collect_handoff_advice`` — the SAME digest the evidence bundle carries, so
    the metrics slot and the evidence section agree. Only the aggregated
    ``summary.usage`` (tokens, plus cost when accounting was available) is
    forwarded to the collector's observe-only slot; it is never folded into
    ``total_*``. REPLACE semantics — the collector re-derives the full aggregate
    each call, so this is idempotent with the finalize-time backstop.

    No ``output_dir`` (or no advice surface / no usage) → nothing recorded, so a
    run without advice keeps the historical metrics shape. Best-effort: a
    usage-attribution failure must never break the phase-end path. This is the
    upper-layer push the spec mandates — ``core.observability.metrics`` stays
    unaware of ``pipeline.project``.
    """
    output_dir = getattr(run, "output_dir", None)
    if output_dir is None:
        return
    record = getattr(getattr(run, "_metrics", None), "record_advice_usage", None)
    if not callable(record):
        return
    try:
        from pipeline.project.handoff_advice_evidence import (
            collect_handoff_advice,
        )

        advice = collect_handoff_advice(output_dir, getattr(run, "session", {}) or {})
        if not isinstance(advice, Mapping):
            return
        summary = advice.get("summary")
        usage = summary.get("usage") if isinstance(summary, Mapping) else None
        if isinstance(usage, Mapping) and usage:
            record(usage)
    except Exception:
        return


# ============================================================================
#  _PipelineRun — encapsulated execution
# ============================================================================
@dataclass
class _PipelineRun:
    """One pipeline execution. Captures everything the imperative ``run_pipeline``
    body used to thread through closures.

    Each phase block is a method that mutates ``self.state`` and ``self.session``
    in place. The top-level ``run_pipeline`` becomes thin orchestration: build
    this object, call the phase methods, return ``finalize()``.
    """
    # ── Inputs ──────────────────────────────────────────────────────────────
    task: str
    project_path: Path
    git_cwd: str
    plugin: PluginConfig
    output_dir: Path | None
    dry_run: bool
    profile_name: str               # Phase 6: was ``pipeline_mode: PipelineMode``
    session_mode: SessionMode
    max_rounds: int
    # Models
    plan_model: str
    implement_model: str
    repair_model: str
    repair_escalation_model: str
    review_model: str

    # Mode gates
    do_plan: bool
    do_build: bool
    do_review: bool

    # Resources
    _provider: object  # AgentProvider (typed loosely to avoid forward-import gymnastics)
    phase_config: PhaseAgentConfig
    state: PipelineState
    registry: object  # PhaseRegistry
    session: dict
    session_ts: str
    codemap: str
    _metrics: MetricsCollector
    _ckpt: CheckpointStore | None
    _chain_same_model_only: bool

    # When ``--no-interactive`` is set (CI, MCP, piped invocations), a
    # fired phase handoff persists + exits rc=4 instead of prompting
    # the operator at the keyboard. See
    # ``pipeline.control.handoff_prompt.should_prompt_for_phase_handoff``
    # for the TTY-attachment side of the same gate. Defaults to
    # ``False`` so test fixtures that construct ``_PipelineRun``
    # directly don't have to track yet another field.
    no_interactive: bool = False
    unattended: bool = False

    # Populated during the run
    research_hypothesis: str | None = None

    # Per-run override for the pre-PLAN hypothesis gut-check (None = use
    # config). The CLI sets this from ``--hypothesis`` / ``--no-hypothesis``
    # so an operator can skip the gut-check on a feature task where it
    # adds no value (e.g. a straightforward "add a field" change).
    hypothesis_enabled: bool | None = None
    # True when this invocation continues an existing run directory from
    # checkpoints.db. Hypothesis is fresh-run pre-work only; checkpoint
    # resumes replay persisted context and must not start a new gut-check.
    checkpoint_resume: bool = False

    # Cross-orchestrator linkage. When this run is a sub-pipeline spawned
    # by ``orcho cross``, the parent run id and per-project alias travel
    # along so the DONE banner / status line can distinguish "one slice
    # of a cross-project run" from "the full pipeline finished".
    parent_run_id: str | None = None
    project_alias: str | None = None

    # Session-shape adapter registry. Adapters live in
    # ``pipeline.session_adapters`` (PlanAdapter / ValidatePlanAdapter /
    # RoundAdapter) behind a Strategy + Registry surface so customer
    # plugins can override session shape per phase. Lazy-initialised in
    # ``__post_init__`` so existing test fixtures that build ``_PipelineRun``
    # piecemeal don't have to thread the registry through every constructor.
    _session_adapters: object | None = None  # SessionAdapterRegistry

    # Phase 5e-5 substep 6c: orchestrator-private dispatch-active guard.
    # Replaces the substep-6b ``state.extras["_v2_dispatch_active"]``
    # channel — flag now lives on the run object, where it belongs (it
    # only ever guarded orchestrator-internal banner / mock-plan
    # side-effects, never a cross-module concern). ``dispatch_via_v2_profile``
    # toggles it for the duration of the run; ``_on_phase_*`` callbacks
    # read it via ``self`` instead of state.extras.
    _dispatch_active: bool = False
    _done_summary_profile: object | None = None

    # ADR 0081 Stage 4: the resolved profile + lifecycle ctx stashed for the
    # duration of dispatch so the per-phase gate hooks (wired through
    # ``_on_phase_pre`` / ``_on_phase_end``) can build the executable gate plan
    # and dispatch repair_changes. ``None`` outside dispatch keeps the gate
    # callbacks inert (direct-instantiation tests, no-contract runs).
    # ``_in_gate_hook`` guards against re-entrant gate evaluation when the
    # critical-flow repair dispatch re-fires the phase callbacks.
    _gate_profile: object | None = None
    _gate_ctx: object | None = None
    _in_gate_hook: bool = False

    # ADR 0046 Phase C: per-run presentation policy. Kept distinct from
    # ``_dispatch_active`` (different concern: "are we inside dispatch
    # right now" vs "is this run silent"). Read by ``_on_phase_start`` /
    # ``_on_phase_end`` / ``_record_phase_failure`` / ``_fsm_checkpoint``
    # / ``finalize()`` to gate every reachable stdout/stderr site.
    # Threaded in from ``request.presentation`` at the ``_PipelineRun``
    # constructor call in ``pipeline.project.app``. Default
    # ``TERMINAL`` preserves direct-instantiation test fixtures.
    _presentation: PresentationPolicy = PresentationPolicy.TERMINAL

    # GWT-1 (ADR 0033): run-owned worktree context. Set during run_pipeline
    # setup; ``None`` only for test fixtures that construct _PipelineRun
    # directly without the worktree wiring. ``finalize()`` calls
    # ``teardown_worktree`` when this is set.
    worktree_context: object | None = None  # WorktreeContext
    # ContextVar token for the active-checkout registration (F2 fix). Reset
    # in ``finalize()`` so the ContextVar doesn't leak across runs in the
    # same thread (important for test suites).
    _worktree_cvar_token: object | None = None  # Token[str | None]
    # ContextVar token for the active sandbox-policy registration (ADR
    # 0034). Same lifecycle as the worktree token — set after the
    # sandbox resolver returns at run init, reset in ``finalize()``.
    _sandbox_cvar_token: object | None = None  # Token[SandboxPolicy | None]

    def __post_init__(self) -> None:
        if self._session_adapters is None:
            from pipeline.session_adapters import default_session_adapter_registry
            object.__setattr__(
                self, "_session_adapters", default_session_adapter_registry()
            )

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _model_for_phase(self, name: str) -> str:
        # validate_plan / review_changes / final_acceptance / validate_hypothesis run via codex.
        if name in {"plan", "hypothesis"}:
            return self.plan_model
        if name in {"implement", "repair_changes"}:
            return self.implement_model
        return self.review_model

    def _runtime_for_phase(self, name: str) -> str:
        """Resolve the registered agent-runtime id a phase executed under.

        The authoritative source is the *actual* phase agent from
        ``state.phase_config`` — its ``runtime`` attribute reflects the runtime
        that really executed, including a resume ``runtime_override`` or an
        explicitly supplied ``phase_config`` that diverges from the global
        config. The global ``AppConfig.phase_runtime_map`` is only a per-phase
        default and can be stale relative to the resolved slot, so it is used
        strictly as a fallback when no agent slot is available. Returns ``""``
        when neither yields a known id, so old/unknown runtimes keep bucketing
        by model downstream and the legacy ``metrics.json`` shape is preserved.
        """
        agent = self._agent_for_phase(name)
        rt = str(getattr(agent, "runtime", "") or "").strip()
        if rt and rt != "unknown":
            return rt
        try:
            rt_map = config.AppConfig.load().phase_runtime_map
        except Exception:  # noqa: BLE001 — a config read must never break metrics
            rt_map = {}
        rt = str(rt_map.get(name, "") or "").strip()
        return "" if rt in {"", "unknown"} else rt

    def _agent_for_phase(self, name: str):
        """Resolve which phase_config slot drove the just-finished phase.
        Used by metrics to pull ``last_cost_usd`` / ``last_tokens_in/out``.
        """
        pc = self.state.phase_config
        if pc is None:
            return None
        slot = {
            "plan":             "plan_agent",
            "validate_plan":    "validate_plan_agent",
            "implement":        "implement_agent",
            "review_changes":   "review_changes_agent",
            "repair_changes":   "repair_changes_agent",
            "final_acceptance": "final_acceptance_agent",
            "decompose":        "plan_agent",
            "decompose_qa":     "validate_plan_agent",
            "integrate_qa":     "review_changes_agent",
        }.get(name)
        return getattr(pc, slot, None) if slot else None

    def _on_phase_start(self, name: str, st: PipelineState) -> None:
        st.extras[f"_phase_t0_{name}"] = time.monotonic()
        # Phase 5d-fixup: track current phase so v2 dispatch's
        # try/except can give ``_record_phase_failure`` a meaningful
        # phase name when a handler raises (legacy ``_safe_phase`` set
        # ``exc._orcho_failed_phase`` before re-raising; v2 ``run_profile``
        # path doesn't go through ``_safe_phase``, so we track via
        # state.extras instead).
        st.extras["_current_phase"] = name

        # Phase 5d-fixup: emit progress-log banners that legacy
        # ``run_*_loop`` methods used to print. ``progress.log``
        # consumers (CLI watchers, dashboard tail-followers, acceptance
        # tests) expect the canonical PHASE-N banners.
        # ADR 0046 Phase C (site 7) — split render from log: pass
        # ``terminal=False`` under SILENT so the helper skips its
        # ``print(render_phase_header(...))`` half but still fires
        # ``log_phase(...)`` (which writes progress.log + emits
        # ``phase.start`` to events.jsonl). ADR 0046 stop #9 forbids
        # gating ``log_phase`` itself — the silent boundary keeps
        # observability parity with TERMINAL.
        if self._dispatch_active:
            emit_phase_banner(
                name, st,
                terminal=self._presentation is PresentationPolicy.TERMINAL,
            )

    def _on_phase_pre(self, name: str, st: PipelineState) -> None:
        """Pre-phase gate seam (ADR 0081): evaluate ``before_phase`` (and
        ``before_delivery`` on delivery boundaries) before the handler runs, so a
        required gate can abort/handoff before entering the phase. No-op without a
        declared verification contract.

        Correction routing (ADR 0086) runs first: a persisted ``correction_triage``
        verdict can classify the follow-up as a shortcut route (``gate_rerun`` /
        ``contract_ack``) whose code-change phases are not applicable. When the
        derived route names this phase, mark it for the runner's pre-phase skip
        channel so the handler / FSM / gates never run for it. Strict no-op for
        any profile without a triage record — the hot path of every other run.
        """
        route = derive_correction_route(st.phase_log.get("correction_triage"))
        if route is not None and name in route.skip_phases:
            st.extras[PHASE_PRE_SKIP_KEY] = route.reason
            # The phase will not execute, so its ``before_phase`` /
            # ``before_delivery`` gates must not run for it either — a
            # skipped phase performs no work, gate commands included.
            return

        # ADR 0112 §4 (increment C): discovery-time scope-expansion promotion.
        # MUST run BEFORE the pre-phase required-receipt auto-runs below: those
        # auto-runs materialize required receipts against the current
        # ``verification_placeholders`` snapshot / participant set, so if this
        # phase already dirtied an out-of-set sibling repo, promotion has to add
        # that repo (non-colliding worktree, live resolver snapshot refresh,
        # delivery coverage) FIRST — otherwise the correction pre-review and
        # pre-final auto-runs would materialize receipts over the stale set and
        # the new participant would escape this phase's verification. Phase-
        # agnostic; strict no-op for single-participant / dry-run / no-contract /
        # waiver / re-entrant gate hook. All logic lives in participant_promotion;
        # run.py only routes the call.
        from pipeline.participant_promotion import evaluate_scope_expansion_promotion
        evaluate_scope_expansion_promotion(self, name)

        # Correction follow-ups can be created specifically to close a missing
        # receipt blocker. If the code-fix route reaches review before official
        # receipts are materialized, the reviewer sees only transcript/ad-hoc
        # commands and rejects the same blocker again. Run the same Stage 9
        # materializer before correction review so review audits durable
        # command receipts, not an LLM's informal test log.
        if name == "review_changes" and self.profile_name == "correction":
            _auto_run_required_receipts_live(
                self,
                name,
                reason="pre-review correction required-receipt materialization",
                hook_label="correction pre-review auto-run",
            )

        # Stage 9 (ADR 0094): before a final phase runs, materialise the run's
        # missing/stale required receipts so the ``before_delivery`` gate below,
        # the readiness render in the handler, and the delivery gate all read
        # fresh evidence from disk. Side-effect-free under dry-run / no-contract;
        # all logic (guards, context, evidence) lives in the autorun module.
        if name in FINAL_PHASES:
            _auto_run_required_receipts_live(
                self,
                name,
                reason="pre-final_acceptance required-receipt materialization",
                hook_label="pre-final auto-run",
            )

        # ADR 0112 §3: isolated-source provenance preflight. Before ``implement``
        # runs, fail-fast if the run owns an isolated per-run worktree whose verify
        # source is not bound to that worktree (sibling/unresolved) or whose
        # env-provenance assertion fails — so a wrong-tree run aborts before the
        # implement/review cycle is spent. Strict no-op for single-checkout runs
        # and every phase but ``implement``; decision logic lives in gate_repair.
        from pipeline.project.gate_repair import (
            evaluate_isolated_source_preflight,
            evaluate_pre_phase_gates,
        )
        evaluate_isolated_source_preflight(self, name)
        if getattr(st, "halt", False):
            return

        before = len(_gate_events_list(self))
        evaluate_pre_phase_gates(self, name)
        # F2: print the scheduled-gate decisions this seam recorded (before_phase,
        # plus before_delivery on a FINAL phase) as their own framed live blocks.
        _print_scheduled_gate_live_blocks(self, _gate_events_list(self)[before:])

    def _emit_phase_log_end(self, name: str, st: PipelineState) -> None:
        if self._dispatch_active:
            emit_phase_log_end(
                name, st,
                terminal=self._presentation is PresentationPolicy.TERMINAL,
                phases=self.session.get("phases"),
            )

    def _on_phase_end(self, name: str, st: PipelineState) -> None:
        """Phase 5e-5 substep 4 + 6b + 6c: slimmed to orchestrator-internal
        concerns that don't fit in the FSM stages. FSM stages 8-10
        own adapter / checkpoint / metrics via ctx callbacks
        (``_fsm_metrics`` / ``_fsm_checkpoint`` /
        ``ctx.session_adapter_registry``).

        Remaining concerns:
          * Pop the ``_phase_t0_<name>`` timer (was used for metrics
            duration; ``_fsm_metrics`` reads it before this fires)
          * Emit progress-log END banner (acceptance tests read this)
          * Legacy mock plan_*.md write fallback when older handlers have not
            already rendered a deterministic plan artifact.

        ``self._dispatch_active`` is the orchestrator-private guard
        keeping banner + mock-plan side-effects out of direct
        ``_PipelineRun`` test instantiations — substep 6c moved this
        flag from ``state.extras`` to a typed attribute on the run
        object, where it belongs. Cross-module channel surface is now
        empty.
        """
        # Pop timer — _fsm_metrics already consumed the start time;
        # leftover key is no longer needed.
        st.extras.pop(f"_phase_t0_{name}", None)

        # F2: capture companion base revisions the first time the durable plan
        # scope exists (plan / decompose phase end), before the implementation
        # phase advances any companion HEAD. Idempotent + no-op without a recorded
        # delivery scope; see ``_capture_companion_bases_early``.
        if name in {"plan", "decompose"}:
            _capture_companion_bases_early(self)

        # Correction routing evidence (ADR 0086): when triage completes on
        # the non-halted path, stamp the derived route as a flat dict onto
        # the phase_log record and mirror it into the session so operators
        # see in session.json *why* the downstream phases were skipped. The
        # session adapter already promoted the triage record inside the FSM,
        # so the session mirror is stamped here by hand. A missing or halted
        # record is a strict no-op (the halt path owns its own evidence).
        if name == "correction_triage":
            record = st.phase_log.get("correction_triage")
            if isinstance(record, dict) and not record.get("halted"):
                route = derive_correction_route(record)
                if route is not None:
                    route_dict = route.to_evidence()
                    record["route"] = route_dict
                    session_phase = (
                        self.session.get("phases", {}).get("correction_triage")
                    )
                    if isinstance(session_phase, dict):
                        session_phase["route"] = route_dict
                    if route.kind == "gate_rerun":
                        from pipeline.project.correction_gate_rerun import (
                            execute_gate_rerun_receipts,
                        )
                        gate_execution, gate_result = execute_gate_rerun_receipts(
                            self,
                        )
                        execution = gate_execution.to_evidence()
                        record["gate_rerun_execution"] = execution
                        if isinstance(session_phase, dict):
                            session_phase["gate_rerun_execution"] = execution
                        # Live block (TERMINAL only): the rerun command *names* and
                        # their statuses, which the fixed evidence shape omits.
                        if self._presentation is PresentationPolicy.TERMINAL:
                            from pipeline.project.verification_timeline import (
                                render_gate_live_block,
                            )
                            _print_gate_live_block(
                                render_gate_live_block(
                                    gate_result,
                                    hook_label="correction gate rerun",
                                ),
                            )

        # ADR 0073: an in-process implement auto-waiver is recorded onto
        # ``state.extras`` by the subtask_dag handler, which has only ``state``
        # (no run handle). Mirror it into the session at the implement phase-end
        # so it persists to ``meta.phase_handoff_waiver`` + evidence (the
        # operator-resume accept path syncs directly instead, and skips implement
        # so this never double-fires there). Conflict-aware + idempotent.
        if name == "implement" and "phase_handoff_waiver" in st.extras:
            from pipeline.project.handoff_waiver import sync_waiver_to_session
            sync_waiver_to_session(self)

        if not self._dispatch_active:
            return

        # Banner END (paired with _on_phase_start banner — acceptance
        # tests assert START / END pairs in progress.log).
        # ADR 0046 Phase C (site 8) — split render from log: pass
        # ``terminal=False`` under SILENT so the helper skips its
        # ``print("↳ skipped: …")`` chip but still fires
        # ``log_phase("END", ...)``. The structural ``phase.end`` event
        # + progress.log line are preserved under both presentations
        # (ADR 0046 stop #9 — ``log_phase`` is never gated).
        emit_phase_log_end(
            name, st,
            terminal=self._presentation is PresentationPolicy.TERMINAL,
            phases=self.session.get("phases"),
        )

        # Legacy mock plan_*.md fallback. The PLAN handler now renders the
        # artifact for all providers; keep this for direct/older paths that
        # might still reach the callback without ``plan_artifact_path`` set.
        if (name == "plan"
                and not st.dry_run
                and _is_mock_provider(self._provider)
                and not st.extras.get("plan_artifact_path")):
            _plan_round = (
                st.extras.get("plan_round")
                or st.extras.get("loop_round")
                or 1
            )
            _plan_dir = self.project_path / self.plugin.ma_artifacts_dir
            _plan_dir.mkdir(parents=True, exist_ok=True)
            _plan_file = _plan_dir / (
                f"plan_{self.session_ts}_r{_plan_round}.md"
            )
            _plan_file.write_text(
                st.plan_markdown or "", encoding="utf-8",
            )
            st.extras["plan_artifact_path"] = str(_plan_file)
            # REA-2: announce the plan artifact so the evidence bundle
            # can enumerate it via the event spine without filesystem scans.
            from core.observability import events as _events
            _events.emit(
                "artifact.created",
                path=str(_plan_file),
                artifact_kind="plan",
                size_bytes=_plan_file.stat().st_size,
                attempt=int(_plan_round),
            )

        # ADR 0081 Stage 4: after a phase finishes (and after its adapter /
        # checkpoint / metrics / banner settled), evaluate the ``after_phase``
        # gate for it. ``implement`` is the critical path: a failed required
        # gate routes into ``repair_changes`` here, BEFORE run_profile advances
        # to the review loop, so a reviewer turn is never spent on a known-bad
        # state. No-op without a declared contract.
        #
        # ADR 0086: a runner-skipped phase (resume-skip or correction-route
        # skip) performed no work, so ``after_phase`` must not fire for it —
        # running e.g. after_phase(implement) gate commands against a phase
        # that never executed would repair/halt/handoff on a state implement
        # never touched. The runner marks the skip-end via a consume-once
        # extras key scoped to this callback; checking the phase_log
        # ``skipped`` marker instead would mis-skip loop phases that wrote a
        # handler-side skip record in an earlier round.
        if st.extras.get(PHASE_END_SKIPPED_KEY) == name:
            return
        # ADR 0112 §4 (increment C): promote any out-of-set repo this phase just
        # dirtied BEFORE its ``after_phase`` verification gate runs, so the gate
        # covers the new participant. Same thin seam / strict no-op as the
        # pre-phase site; logic lives in participant_promotion.
        from pipeline.participant_promotion import evaluate_scope_expansion_promotion
        evaluate_scope_expansion_promotion(self, name)
        from pipeline.project.gate_repair import evaluate_post_phase_gates
        before = len(_gate_events_list(self))
        evaluate_post_phase_gates(self, name)
        # F2: print the scheduled-gate decisions this after_phase seam recorded.
        _print_scheduled_gate_live_blocks(self, _gate_events_list(self)[before:])

    def _fsm_metrics(self, name: str, st: PipelineState) -> None:
        """Phase 5e-5 substep 4: ``ctx.on_metrics`` callback. Records
        per-phase metrics — extracted from former ``_on_phase_end``.
        Reads timer start from ``state.extras[_phase_t0_<name>]``
        (set by ``_on_phase_start``).

        Skipped handlers (``phase_log[name][skipped]`` truthy) — FSM
        already filters these out before calling ``ctx.on_metrics``.
        This method is invoked for COMPLETED and HALTED phases: a halt may
        happen after an agent call, and its usage still belongs in summary."""
        t0 = st.extras.get(f"_phase_t0_{name}")
        duration_s = (time.monotonic() - t0) if t0 is not None else 0.0
        log_entry = st.phase_log.get(name) or {}
        output = log_entry.get("output", "") if isinstance(log_entry, dict) else ""
        agent = self._agent_for_phase(name)
        agent_duration = (
            getattr(agent, "last_duration_s", None) if agent else None
        )
        if agent_duration is not None:
            duration_s = max(duration_s, float(agent_duration))
        prompt = getattr(agent, "last_prompt", "") if agent else ""
        outcome = (
            getattr(agent, "last_invocation_outcome", None) if agent else None
        )
        tokens_exact_override: bool | None = None
        composite_usage = (
            log_entry.get("_metrics_usage")
            if isinstance(log_entry, dict) else None
        )
        reconcile_total = False
        if isinstance(composite_usage, dict):
            cost_usd = composite_usage.get("cost_usd_equivalent")
            tokens_in = composite_usage.get("tokens_in")
            tokens_out = composite_usage.get("tokens_out")
            tokens_total = composite_usage.get("tokens_total")
            tokens_in_cache_read = composite_usage.get("tokens_in_cache_read")
            tokens_in_cache_create = composite_usage.get("tokens_in_cache_create")
            tokens_exact_override = bool(composite_usage.get("tokens_exact"))
            reconcile_total = True
        elif outcome is not None:
            cost_usd     = outcome.cost_usd_equivalent
            tokens_in    = outcome.tokens_in
            tokens_out   = outcome.tokens_out
            tokens_total = outcome.tokens_total
            tokens_in_cache_read = outcome.tokens_in_cache_read
            tokens_in_cache_create = outcome.tokens_in_cache_create
            tokens_exact_override = outcome.tokens_exact
            reconcile_total = True
            # Estimate-only outcome: no provider numbers at all. Feed the
            # wire estimate as a total (avoids the misleading in=0/out=N
            # tail) and explicitly mark the record NOT exact.
            if (
                tokens_in is None
                and tokens_out is None
                and tokens_total is None
                and outcome.wire_tokens_estimate is not None
            ):
                tokens_total = outcome.wire_tokens_estimate
                tokens_exact_override = False
        else:
            cost_usd     = getattr(agent, "last_cost_usd",     None) if agent else None
            tokens_in    = getattr(agent, "last_tokens_in",    None) if agent else None
            tokens_out   = getattr(agent, "last_tokens_out",   None) if agent else None
            tokens_total = getattr(agent, "last_tokens_total", None) if agent else None
            tokens_in_cache_read = (
                getattr(agent, "last_tokens_in_cache_read", None)
                if agent else None
            )
            tokens_in_cache_create = (
                getattr(agent, "last_tokens_in_cache_create", None)
                if agent else None
            )
        context_growth = (
            log_entry.get("context_growth", {})
            if isinstance(log_entry, dict) else {}
        )
        tool_calls = (
            int(context_growth.get("tool_use_count") or 0)
            if isinstance(context_growth, dict) else 0
        )
        if isinstance(composite_usage, dict):
            tool_calls = max(
                tool_calls,
                int(composite_usage.get("tool_calls") or 0),
            )
        self._metrics.record_phase(
            name,
            prompt=prompt,
            output=output,
            duration_s=duration_s,
            model=self._model_for_phase(name),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_total=tokens_total,
            tool_calls=tool_calls,
            cost_usd=cost_usd,
            tokens_exact=tokens_exact_override,
            runtime=self._runtime_for_phase(name),
            # Outcome carries a normalized provider total that may exceed the
            # in/out split (e.g. Codex reasoning tokens). Honor it so the
            # recorded total matches the outcome; the last_* fallback path
            # keeps its historical total-ignored-on-split behavior.
            reconcile_total=reconcile_total,
            tokens_in_cache_read=tokens_in_cache_read,
            tokens_in_cache_create=tokens_in_cache_create,
        )
        # Additive per-subtask usage breakdown (subtask_dag implement only).
        # Observe-only: stored under ``metrics.json["subtasks"]`` and NEVER fed
        # to ``record_phase`` — the phase rollup above stays the only total.
        subtask_records = (
            log_entry.get("subtask_metrics")
            if isinstance(log_entry, dict) else None
        )
        if isinstance(subtask_records, list) and subtask_records:
            self._metrics.record_subtask_usage(name, subtask_records)
        # Observe-only handoff-advice usage attribution (T4). The advisor runs
        # OUTSIDE the FSM phase loop, so its usage is not a pipeline phase and
        # must never reach ``record_phase`` (that would double-count it into
        # ``total_*``). Re-derive the aggregate from the durable advice artifacts
        # and push it to the observe-only ``handoff_advice`` metrics slot.
        _push_handoff_advice_usage(self)

    def _fsm_checkpoint(self, name: str, st: PipelineState) -> None:
        """Phase 5e-5 substep 4: ``ctx.on_checkpoint`` callback. Saves
        the phase entry to checkpoint DB — extracted from former
        ``_on_phase_end``.

        FSM filters out skipped phases before invoking this. Defensive
        try/except: checkpoint write failure is observability concern,
        must not crash the run.
        """
        if self._ckpt is None:
            return
        if name not in self.session.get("phases", {}):
            return
        try:
            self._ckpt.save_phase(name, self.session["phases"][name])
        except Exception as exc:  # pragma: no cover — defensive
            # ADR 0046 Phase C (site 17): defensive observability —
            # checkpoint save_phase failure is rare but real. Under
            # SILENT the library caller has no expectation of stderr
            # noise; gate the warn so the "zero stdout/stderr"
            # contract holds even on this rare path. The exception is
            # swallowed either way (already the contract — checkpoint
            # writes must not crash the run); silent callers can
            # diagnose via the absence of phase entries in the
            # checkpoint DB.
            if self._presentation is PresentationPolicy.TERMINAL:
                warn(
                    f"v2 dispatch: checkpoint save_phase({name!r}) "
                    f"failed: {exc}"
                )

    def _round_n_for_adapter(self, name: str, st: PipelineState) -> int | None:
        """Return the loop round relevant to the adapter for ``name``.

        plan and review_changes/repair_changes loops can both run in one profile. Their
        counters remain in ``state.extras`` for legacy/debug parity, so a
        simple ``plan_round or repair_round`` fallback can leak the PLAN round
        into a later repair_changes adapter call. Prefer the active LoopStep key
        stamped by ``runtime._run_loop_step``; fall back to known phase
        conventions for direct tests / legacy helper paths.
        """
        active_key = st.extras.get("_active_loop_round_key")
        if isinstance(active_key, str) and active_key:
            value = st.extras.get(active_key)
            if value is not None:
                return int(value)

        key_by_phase = {
            "plan":           ("plan_round", "loop_round"),
            "validate_plan":  ("plan_round", "loop_round"),
            "review_changes": ("repair_round",  "loop_round"),
            "repair_changes": ("repair_round",  "loop_round"),
        }
        for key in key_by_phase.get(name, ("loop_round",)):
            value = st.extras.get(key)
            if value is not None:
                return int(value)
        return None

    def _record_phase_failure(self, exc: Exception, fallback_phase: str) -> None:
        """Persist a structured failure record so meta.json captures *why*
        the run died. Idempotent — guards against double-write.
        """
        if self.session.get("status") == "failed":
            return
        from core.observability import events as _events
        failed_phase = getattr(exc, "_orcho_failed_phase", None) or fallback_phase
        # ADR 0101 sanitary boundary: for a terminal provider-access failure
        # every operator-visible text field (``failure['error']`` and the
        # ``run.end`` error below) must draw from the sanitized provider-access
        # channel, never raw ``str(exc)`` / stderr — those can carry the
        # provider's init JSON / JSONL plumbing. Other exception classes keep
        # the historical raw-text source unchanged.
        if isinstance(exc, AgentAccessError):
            err_text = provider_access_detail(exc) or str(exc) or repr(exc)
        else:
            # A stalled command carries a sanitized, bounded message of its
            # own; ``str(exc)`` is safe (no raw provider plumbing).
            err_text = str(exc) or repr(exc)
        first_line = err_text.splitlines()[0] if err_text else type(exc).__name__
        # ADR 0046 Phase C (site 14): the red FAILED block + the
        # first-line ``warn`` are terminal courtesy renderings of an
        # error that is already signalled structurally — the
        # ``session["failure"]`` block below + the ``run.end`` event
        # + the raised exception propagating out of dispatch are what
        # SILENT callers consume. Skip the prints under SILENT;
        # the structural mutations + event emission are unchanged.
        if self._presentation is PresentationPolicy.TERMINAL:
            _rule = "=" * 62
            print(f"\n{paint(_rule, C.RED, C.BOLD)}")
            print(paint(f"  FAILED in {failed_phase}", C.RED, C.BOLD))
            print(paint(_rule, C.RED, C.BOLD))
            warn(f"  {type(exc).__name__}: {first_line}")
        # Top-level ``halt_reason`` so every non-``done`` terminal status
        # honours the ADR 0035 invariant (downstream consumers — SDK
        # resume-gate, MCP wire, dashboards — that key off
        # ``meta.halt_reason`` see something on ``failed`` runs too).
        # The exception's class name is the most useful short tag for
        # the rollup view; the structured ``failure`` block below
        # carries the full diagnostic detail. ``failed`` preserves any
        # active ``phase_handoff`` — a failure does not resolve an
        # outstanding handoff decision.
        # A terminal stalled command (idle-timeout escalation) flips the run to
        # ``failed`` via the named stall writer; every other exception keeps the
        # generic ``phase_failure`` reason. Both preserve an active handoff.
        if isinstance(exc, AgentCommandStalledError):
            mark_run_stalled(
                self.session,
                halt_reason=f"stalled_command:{exc.stalled.reason}",
            )
        else:
            mark_run_failed(
                self.session, halt_reason=f"phase_failure:{type(exc).__name__}",
            )
        failure_meta = _failure_metadata_for_exception(
            exc,
            failed_phase=failed_phase,
            model_resolver=getattr(self, "_model_for_phase", None),
        )
        self.session["failure"] = {
            "phase": failed_phase,
            "error": err_text[:2000],
            "type":  type(exc).__name__,
            "ts":    datetime.now().isoformat(),
            **failure_meta,
        }
        if self.output_dir:
            with contextlib.suppress(Exception):
                save_session(self.output_dir, self.session)
        if self._ckpt:
            with contextlib.suppress(Exception):
                self._ckpt.set_status(PipelineStatus.FAILED)
                self._ckpt.close()
        # Terminal stall observability: emit ``agent.command_stalled`` with
        # ``terminal=True`` before the closing ``run.end`` so the evidence /
        # status projections can distinguish the terminal hang from the live
        # non-terminal risk-flag path (which emits the same kind with
        # ``terminal=False`` from the stream monitor, never here).
        if isinstance(exc, AgentCommandStalledError):
            with contextlib.suppress(Exception):
                _events.emit(
                    "agent.command_stalled",
                    **exc.stalled.event_payload(terminal=True),
                )
        with contextlib.suppress(Exception):
            _events.emit(
                "run.end",
                status="failed",
                phase=failed_phase,
                error_type=type(exc).__name__,
                error=err_text[:500],
                **failure_meta,
            )

    def _safe_phase(self, phase_names: tuple[str, ...], *, label: str | None = None) -> None:
        """Run one phase via runtime; on any exception, persist a structured
        failure record and re-raise. Mutates ``self.state`` in place.
        """
        try:
            self.state = _run_one_phase(
                phase_names, self.state,
                registry=self.registry,
                on_phase_start=self._on_phase_start,
                on_phase_end=self._on_phase_end,
            )
        except Exception as exc:
            self._record_phase_failure(
                exc,
                label or (phase_names[0] if phase_names else "?"),
            )
            raise

    def _effective_diff_cwd(self) -> Path:
        if self.worktree_context is not None and self.worktree_context.is_isolated:
            return self.worktree_context.path
        return self.project_path

    def _commit_delivery_baseline(self) -> str:
        pre_run_dirty = self.session.get("pre_run_dirty")
        if isinstance(pre_run_dirty, dict):
            seed_tree = pre_run_dirty.get("seed_tree_sha")
            if isinstance(seed_tree, str) and seed_tree.strip():
                return seed_tree.strip()
        if self.worktree_context is not None:
            base_ref = getattr(self.worktree_context, "base_ref", None)
            if isinstance(base_ref, str) and base_ref.strip():
                return base_ref.strip()
        return "HEAD"

    def _run_commit_delivery(self, diff_cwd: Path) -> None:
        if (
            self.output_dir is None
            or not _session_allows_commit_delivery(self.session)
            or self.parent_run_id
            or self.project_alias
        ):
            return
        from pipeline.commit_message_parser import (
            CommitMessageParseError,
            CommitMessageSchemaError,
            parse_commit_message,
        )
        from pipeline.engine.commit_delivery import (
            COMMIT_DELIVERY_HALT_REASONS,
            apply_commit_delivery,
            render_commit_message_prompt,
            render_delivery_outcome,
            resolve_commit_delivery,
        )

        app_cfg = config.AppConfig.load()

        def commit_message_generator(decision):
            # Strategy is decided upstream by ``resolve_commit_delivery`` /
            # ``_resolve_final_commit_message`` (configured llm_generate OR a
            # forced publish-outward path); the generator no longer self-disables
            # on ``default_strategy`` — it only needs an available agent.
            agent = getattr(self.phase_config, "final_acceptance_agent", None)
            if agent is None:
                return None
            prompt = render_commit_message_prompt(
                decision,
                body_language=app_cfg.content_language,
            )
            try:
                raw = agent.invoke(
                    prompt,
                    str(decision.source_path),
                    mutates_artifacts=False,
                    continue_session=False,
                )
                return parse_commit_message(raw).render()
            except (CommitMessageParseError, CommitMessageSchemaError):
                return None
            except Exception as exc:
                warn(f"commit message generation failed; using summary: {exc}")
                return None

        # Stage 6 delivery verification gate (ADR 0083). The pure assessment
        # module reads untracked/changed paths and checkout identity from
        # ``diff_cwd`` itself — run.py passes only the subject checkout and the
        # baseline, never git output. ``None`` contract → assessment is None →
        # resolve_commit_delivery behaves exactly as before.
        extras = getattr(getattr(self, "state", None), "extras", None) or {}
        assessment = assess_delivery_verification(
            contract=extras.get("verification_contract"),
            run_dir=self.output_dir,
            ctx=extras.get("verification_placeholders"),
            extras=extras,
            diff_cwd=diff_cwd,
            baseline_ref=self._commit_delivery_baseline(),
        )
        # Persisted (NON-wire) delivery evidence: when a required gate was excused
        # by an exact durable waiver, record it on the session under a dedicated
        # key. ``save_session`` persists it to ``meta.json`` (best-effort — a
        # failure here must never break delivery), and it is deliberately kept off
        # the SDK/MCP wire (CommitDeliveryDecision.to_dict()). Downstream
        # presentation (T4) reads it from meta/evidence.
        _record_commit_delivery_waived(self.session, assessment)
        decision_mode = str(app_cfg.commit.get("decision_mode", "auto"))
        decision = resolve_commit_delivery(
            project_dir=self.project_path,
            source_worktree=diff_cwd,
            run_dir=self.output_dir,
            run_id=self.session_ts,
            session=self.session,
            commit_config=app_cfg.commit,
            no_interactive=self.no_interactive,
            baseline_ref=self._commit_delivery_baseline(),
            commit_message_generator=commit_message_generator,
            verification_gate=assessment,
            decision_mode=decision_mode,
        )
        # Stage C delivery-scope enforcement (T4): a strict-mono sibling-repo
        # violation comes back as a ``pending`` decision carrying a
        # ``scope_blocker`` (action ``none``). ``apply_commit_delivery`` would
        # leave it parked without shipping, so the run must HALT here at a
        # recoverable delivery-scope pause — otherwise a real run finalizes as a
        # clean DONE while hiding a parked, undecided scope gate. Persist the
        # full context and halt with a typed reason; the operator resolves it
        # through ``decide_delivery`` (skip / halt) or re-runs with an expanded
        # scope. The project checkout is never touched. Checked BEFORE the defer
        # park so the scope reason is never masked by ``commit_delivery_pending``.
        if decision.status == "pending" and decision.scope_blocker:
            self.session["commit_delivery"] = decision.to_dict()
            _record_multi_project_delivery(self.session, decision)
            mark_run_halted(
                self.session, halt_reason="commit_delivery_scope_blocked",
            )
            return
        # ADR 0100 — defer mode parks the decision: ``resolve`` returns a
        # ``pending`` decision (action unresolved) instead of an applicable one.
        # Persist the full context and halt the run at a recoverable delivery /
        # correction gate WITHOUT touching the project checkout; an operator
        # resolves it later through ``decide_delivery``.
        if (
            decision_mode == "defer"
            and self.no_interactive
            and decision.status == "pending"
        ):
            self.session["commit_delivery"] = decision.to_dict()
            _record_multi_project_delivery(self.session, decision)
            mark_run_halted(self.session, halt_reason="commit_delivery_pending")
            return
        if decision.status == "pending":
            decision = apply_commit_delivery(
                decision,
                run_dir=self.output_dir,
                commit_config=app_cfg.commit,
                no_interactive=self.no_interactive,
            )
        # A ``not_applicable`` decision is normally a true non-delivery case
        # (no run dir, diff unavailable, APPROVED-but-nothing-to-ship) that we
        # drop without persisting. The one exception is the auto/
        # non-interactive *rejected-release* block (``release_blocked`` above):
        # it returns ``not_applicable`` but represents a real, decidable
        # outcome — the release was rejected and automatic delivery refused.
        # Persist it (carrying release_verdict / release_summary) so meta,
        # evidence, and the SDK can surface the rejection instead of leaving a
        # rejected run with no ``commit_delivery`` record at all. ``disabled``
        # and ``no_diff`` stay dropped; ``not_applicable`` with an empty or
        # APPROVED verdict stays dropped.
        rejected_release = (
            decision.status == "not_applicable"
            and is_release_blocked(decision.release_verdict, empty_blocks=False)
        )
        if (
            decision.status in {"disabled", "not_applicable", "no_diff"}
            and not rejected_release
        ):
            return
        self.session["commit_delivery"] = decision.to_dict()
        _record_multi_project_delivery(self.session, decision)
        # Halt-reason mapping for halting delivery statuses (fix_requested,
        # halted, target_dirty, commit_failed/apply_failed, verification_blocked)
        # lives in the shared COMMIT_DELIVERY_HALT_REASONS map. Non-halting
        # terminal statuses (committed / applied_uncommitted / skipped) are not
        # in the map and fall through without halting. ``target_dirty`` (ADR
        # 0032 / B1.2) carries a halt_reason distinct from operator-chosen halt
        # and executor failure so resume / dashboards route to the cleanup
        # decision.
        halt_reason = COMMIT_DELIVERY_HALT_REASONS.get(decision.status)
        if halt_reason is not None:
            mark_run_halted(self.session, halt_reason=halt_reason)
        presentation = getattr(
            self, "_presentation", PresentationPolicy.TERMINAL,
        )
        if presentation is PresentationPolicy.TERMINAL:
            from core.observability.logging import get_output_mode

            if (
                get_output_mode() == "summary"
                and decision.status == "committed"
            ):
                # Summary mode collapses the multi-line committed outcome to
                # the presenter's one-line ``✓ delivery · committed <sha>``.
                # live/debug keep ``render_delivery_outcome`` byte-identical;
                # non-committed terminal statuses fall through to it in every
                # mode (they carry no summary grammar line).
                from core.io import summary_lines

                print(summary_lines.delivery_line(
                    (decision.commit_sha or "")[:7],
                    decision.delivery_branch or None,
                    pr_url=decision.pr_url,
                ))
            else:
                render_delivery_outcome(
                    decision, output_fn=print, run_dir=self.output_dir,
                )

    # ── Phase blocks ────────────────────────────────────────────────────────
    # Phase execution happens declaratively via ``dispatch_via_v2_profile`` →
    # ``run_profile(...)`` walking the v2 ``Profile`` schema. The hypothesis
    # pre-plan helper is controlled by the profile's plan PhaseStep
    # (``hypothesis: <attempts>``), while execution remains a dispatcher
    # prelude so PLAN can receive the approved/negative context.


    def finalize(self) -> dict:
        """Thin delegator: build a :class:`FinalizationContext` and call
        the right finalization variant per :attr:`_presentation`.

        ADR 0042 Phase G split the legacy finalize body into a silent
        service (writes files / mutates session / emits events / closes
        checkpoint with **no terminal output**) plus a terminal wrapper
        that adds the DONE banner + success chips + Session/Usage/etc.
        lines on top.

        ADR 0046 Phase C (site 16): the delegator now picks between
        the two variants based on ``self._presentation``. Default
        ``TERMINAL`` preserves CLI / SDK back-compat byte-identical;
        ``SILENT`` (set from ``request.presentation`` by
        ``pipeline.project.app``) routes through the silent service
        directly so no DONE banner / success chip / mirror notice /
        worktree-teardown line reaches stdout.

        Returns ``self.session`` (mutated in place by either variant)
        so existing callers stay byte-identical with the pre-ADR-0046
        return shape.
        """
        from pipeline.project.finalization import (
            FinalizationContext,
            finalize_project_run,
            finalize_with_terminal_output,
        )

        ctx = FinalizationContext(run=self)
        if self._presentation is PresentationPolicy.SILENT:
            finalize_project_run(ctx)
        else:
            finalize_with_terminal_output(ctx)
        return self.session
