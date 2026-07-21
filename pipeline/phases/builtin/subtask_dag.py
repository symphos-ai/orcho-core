# SPDX-License-Identifier: Apache-2.0
"""Subtask-DAG implement path for the builtin ``implement`` handler.

When ``implementation_execution=subtask_dag``, the implement phase runs the
parsed plan's subtasks through the policy-owned DAG runner instead of a
single whole-plan invoke. This module owns that path: per-subtask
session-aware dispatch, the phase-level rollup of the real per-subtask
render records, and the integrated text/receipt aggregation the handler
records back on ``phase_log["implement"]``.

Depends on already-extracted leaves (session invocation/addressing,
lifecycle resolvers, the implement-summary printer) and the DAG runner;
none of them import back here, so there is no cycle.
"""

from __future__ import annotations

import fnmatch
import hashlib
from typing import TYPE_CHECKING, Any

from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _change_handoff_for,
    _ensure_lifecycle_ctx,
)
from pipeline.phases.builtin.prompt_parts import _verification_contract_part
from pipeline.phases.builtin.review_support import _print_implement_summary
from pipeline.phases.builtin.session_invoke import _session_aware_invoke
from pipeline.phases.builtin.session_keys import (
    _resolve_session_split_for_step,
    _should_continue_prompt_session,
)
from pipeline.runtime.roles import PhaseHandoffType, SessionInvocationRole

if TYPE_CHECKING:
    from pipeline.observability.invocation_outcome import AgentInvocationOutcome
    from pipeline.runtime import PipelineState


def _integrated_subtask_output(result) -> str:
    completed_count = len(result.completed)
    failed_count = len(result.failed)
    skipped_count = len(result.skipped)
    if completed_count == 0 and failed_count == 0 and skipped_count == 0:
        return "[subtask_dag implement: no subtasks executed]"

    sections: list[str] = []
    for sub_result in result.completed:
        sections.append(
            f"## subtask {sub_result.subtask_id} (ok)\n\n{sub_result.output}"
        )
    for sub_result in result.failed:
        if sub_result.attestation_error and not sub_result.error:
            body = sub_result.output.rstrip()
            if body:
                body += "\n\n"
            body += f"attestation_error: {sub_result.attestation_error}"
            sections.append(
                f"## subtask {sub_result.subtask_id} (incomplete)\n\n{body}"
            )
            continue
        sections.append(
            f"## subtask {sub_result.subtask_id} (failed)\n\n"
            f"error: {sub_result.error}"
        )
    for subtask_id in result.skipped:
        sections.append(f"## subtask {subtask_id} (skipped)")
    return "\n\n".join(sections)


def _aggregate_subtask_prompt_render(state: PipelineState, result) -> dict[str, Any]:
    """Honest phase-level rollup of the real per-subtask render records.

    Replaces the old synthetic ``runtime="subtask_dag"`` fake. The single
    ``phase_log["implement"]["prompt_render"]`` slot is the surface the
    evidence extractor (``_extract_implement``) reads, so it stays one dict;
    per-subtask detail lives in ``subtask_prompt_renders`` (in-memory).

    Fields are derived from the real records threaded back on
    ``SubTaskResult.prompt_render``:

    - ``render_mode`` is a LOSSY rollup — ``"delta"`` iff at least one
      subtask rendered delta, else ``"full"``. A mixed DAG collapses to
      ``"delta"``; exact per-subtask modes are in ``subtask_prompt_renders``.
    - ``session_key`` is the first real per-subtask key (subtasks sharing
      runtime+model share it); ``provider_session_id`` is the last seen.
    - ``part_ids`` / ``selected_part_keys`` carry one ``subtask:<id>`` marker
      per executed subtask so the durable evidence reflects N prompt surfaces.
    """
    records = [
        r.prompt_render for r in (*result.completed, *result.failed)
        if getattr(r, "prompt_render", None)
    ]
    ids = [r.subtask_id for r in (*result.completed, *result.failed)]
    part_ids = [f"subtask:{sid}" for sid in ids]
    digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()

    split = _resolve_session_split_for_step(state).value
    render_mode = (
        "delta" if any(r.get("render_mode") == "delta" for r in records)
        else "full"
    )
    session_key = next(
        (r.get("session_key") for r in records if r.get("session_key")), None,
    )
    provider_session_id = next(
        (r.get("provider_session_id") for r in reversed(records)
         if r.get("provider_session_id")),
        None,
    )

    def _wire_chars(r) -> int:
        # Real wire size from the per-subtask render record; fall back to the
        # source prompt size only when no record exists (direct/dry/failed).
        pr = getattr(r, "prompt_render", None)
        if isinstance(pr, dict) and isinstance(pr.get("wire_chars"), int):
            return pr["wire_chars"]
        return int(getattr(r, "prompt_chars", 0))

    return {
        "render_mode": render_mode,
        "session_split": split,
        "session_key": session_key,
        "provider_session_id": provider_session_id,
        "part_ids": part_ids,
        "selected_part_keys": part_ids,
        "omitted_part_keys": [],
        "delta_dropped_part_keys": [],
        "prefix_hash": digest,
        "payload_hash": digest,
        "wire_chars": sum(
            _wire_chars(r) for r in (*result.completed, *result.failed)
        ),
        "execution_mode": "subtask_dag",
        "surface_count": len(part_ids),
        "phase_key": "implement",
        "round": None,
        "continue_session": any(
            bool(r.get("continue_session")) for r in records
        ),
    }


def _subtask_prompt_render_list(result) -> list[dict[str, Any]]:
    """Per-subtask real render records (in-memory P1 sibling of the rollup)."""
    return [
        {"subtask_id": r.subtask_id, "prompt_render": r.prompt_render}
        for r in (*result.completed, *result.failed)
    ]


def _aggregate_outcome_usage(
    outcomes: list[AgentInvocationOutcome],
) -> dict[str, Any] | None:
    """Sum provider usage across a list of invocation outcomes.

    The single source of truth for both the phase-level rollup
    (:func:`_aggregate_subtask_metrics_usage`) and the per-subtask durable
    records (:func:`_build_subtask_usage_records`). Because every per-subtask
    group and the flat phase list run through identical arithmetic — exact
    provider tokens summed verbatim, non-exact outcomes folding only their
    wire estimate into the total — the invariant "sum of per-subtask groups
    == phase rollup" holds *by construction*, never by re-derivation.

    Returns ``None`` when no outcome carried usable usage (so an all-estimate
    group is omitted rather than reported as a hollow zero record). The
    ``*_known`` flags let the caller decide whether to surface a cache split
    or cost at all: a summed ``0`` is ambiguous (genuinely zero vs. never
    reported), so the flag distinguishes "known" from "unknown".
    """
    if not outcomes:
        return None

    tokens_in = 0
    tokens_out = 0
    tokens_total = 0
    tokens_in_cache_read = 0
    tokens_in_cache_create = 0
    tool_calls = 0
    cost_usd: float | None = None
    tokens_exact = True
    has_any_usage = False
    wire_estimate_total = 0
    cache_read_known = False
    cache_create_known = False

    for outcome in outcomes:
        tool_calls += int(outcome.tool_calls or 0)
        if outcome.cost_usd_equivalent is not None:
            cost_usd = (cost_usd or 0.0) + float(outcome.cost_usd_equivalent)
        if outcome.tokens_in_cache_read is not None:
            cache_read_known = True
        if outcome.tokens_in_cache_create is not None:
            cache_create_known = True

        if outcome.tokens_exact:
            has_any_usage = True
            tokens_in += int(outcome.tokens_in or 0)
            tokens_out += int(outcome.tokens_out or 0)
            tokens_in_cache_read += int(outcome.tokens_in_cache_read or 0)
            tokens_in_cache_create += int(outcome.tokens_in_cache_create or 0)
            if outcome.tokens_total is not None:
                tokens_total += int(outcome.tokens_total)
            else:
                tokens_total += (
                    int(outcome.tokens_in or 0) + int(outcome.tokens_out or 0)
                )
            continue

        tokens_exact = False
        if outcome.wire_tokens_estimate is not None:
            has_any_usage = True
            wire_estimate_total += int(outcome.wire_tokens_estimate)

    if not has_any_usage:
        return None

    if not tokens_exact and wire_estimate_total:
        tokens_total += wire_estimate_total

    return {
        "invocations": len(outcomes),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_total,
        "tokens_in_cache_read": tokens_in_cache_read,
        "tokens_in_cache_create": tokens_in_cache_create,
        "tool_calls": tool_calls,
        "cost_usd_equivalent": cost_usd,
        "tokens_exact": tokens_exact,
        "cache_read_known": cache_read_known,
        "cache_create_known": cache_create_known,
    }


def _aggregate_subtask_metrics_usage(
    outcomes: list[AgentInvocationOutcome],
) -> dict[str, Any] | None:
    """Roll per-subtask provider usage into one implement-phase usage dict.

    The authoritative phase total. The per-subtask breakdown
    (:func:`_build_subtask_usage_records`) *explains* this number; it never
    adds to it — both are derived from the same outcome list via
    :func:`_aggregate_outcome_usage`.
    """
    agg = _aggregate_outcome_usage(outcomes)
    if agg is None:
        return None
    return {
        "source": "subtask_dag",
        "invocations": agg["invocations"],
        "tokens_in": agg["tokens_in"],
        "tokens_out": agg["tokens_out"],
        "tokens_total": agg["tokens_total"],
        "tokens_in_cache_read": agg["tokens_in_cache_read"],
        "tokens_in_cache_create": agg["tokens_in_cache_create"],
        "tool_calls": agg["tool_calls"],
        "cost_usd_equivalent": agg["cost_usd_equivalent"],
        "tokens_exact": agg["tokens_exact"],
    }


def _build_subtask_usage_records(
    captures: list[tuple[Any, AgentInvocationOutcome, float | None]],
    latest_receipt_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Durable per-subtask usage records, grouped by subtask id.

    ``captures`` is the ordered ``(subtask, outcome, duration)`` list the
    invoke hook appends one entry per real ``agent.invoke``. The same closure
    is reused by the within-subtask attestation-repair turn AND by the ADR
    0073 substance-repair passes, so a single ``subtask_id`` can appear more
    than once; we group by id (in first-appearance order, which is DAG
    execution order) and aggregate each group via the shared
    :func:`_aggregate_outcome_usage` so ``sum(records) == phase rollup``.

    ``latest_receipt_by_id`` carries the LATEST receipt seen for each subtask
    across the main pass and every repair pass — the caller overlays repair
    receipts as they complete so ``state`` reflects the final delivery state,
    not a stale first-pass ``incomplete`` for a subtask that was repaired in
    an earlier multi-attempt pass.

    Honesty rules (acceptance contract): unknown values are omitted, never
    estimated by dividing a phase total. ``duration_s``/``tokens_*``/
    ``tool_calls``/``tokens_exact``/``invocations`` are always present;
    cache splits, ``cost_usd_equivalent``, ``state``, and ``declared_files``
    appear only when actually known.
    """
    if not captures:
        return []

    order: list[str] = []
    groups: dict[str, list[tuple[Any, AgentInvocationOutcome, float | None]]] = {}
    for subtask, outcome, duration in captures:
        sid = subtask.id
        if sid not in groups:
            groups[sid] = []
            order.append(sid)
        groups[sid].append((subtask, outcome, duration))

    records: list[dict[str, Any]] = []
    for sid in order:
        group = groups[sid]
        outcomes = [outcome for (_s, outcome, _d) in group]
        agg = _aggregate_outcome_usage(outcomes)
        if agg is None:
            continue

        subtask = group[0][0]
        first_outcome = group[0][1]
        receipt = latest_receipt_by_id.get(sid)

        # duration_s: sum the per-invocation wall-clock when the runtime
        # stamped it; otherwise fall back to the receipt's recorded duration
        # (a single-pass measurement). Never divide a phase total.
        captured = [d for (_s, _o, d) in group if isinstance(d, (int, float))]
        if captured:
            duration_s = round(float(sum(captured)), 3)
        elif receipt is not None and isinstance(
            receipt.get("duration"), (int, float)
        ):
            duration_s = round(float(receipt["duration"]), 3)
        else:
            duration_s = 0.0

        record: dict[str, Any] = {
            "subtask_id": sid,
            "runtime": first_outcome.runtime,
            "model": first_outcome.model,
            "invocations": agg["invocations"],
            "duration_s": duration_s,
            "tokens_in": agg["tokens_in"],
            "tokens_out": agg["tokens_out"],
            "total_tokens": agg["tokens_total"],
            "tool_calls": agg["tool_calls"],
            "tokens_exact": agg["tokens_exact"],
        }
        if agg["cache_read_known"]:
            record["tokens_in_cache_read"] = agg["tokens_in_cache_read"]
        if agg["cache_create_known"]:
            record["tokens_in_cache_create"] = agg["tokens_in_cache_create"]
        if agg["cost_usd_equivalent"] is not None:
            record["cost_usd_equivalent"] = agg["cost_usd_equivalent"]
        if receipt is not None and receipt.get("state"):
            record["state"] = receipt["state"]
        declared_files = list(getattr(subtask, "files", ()) or ())
        if declared_files:
            record["declared_files"] = declared_files
        records.append(record)

    return records


def _append_receipt_state_records(
    records: list[dict[str, Any]],
    latest_receipt_by_id: dict[str, dict[str, Any]],
) -> None:
    """Complete the per-subtask breakdown into a full receipt state mirror.

    :func:`_build_subtask_usage_records` is driven off real invocation captures,
    so a subtask produces a record ONLY when its runtime published a usable
    ``last_invocation_outcome``. Two classes of subtask are therefore missing
    from it: skipped subtasks (unsatisfied dependency / stop-on-failure
    short-circuit — no agent invocation at all), and any run subtask whose
    runtime did not surface metered usage (unmetered failed/done invocation).

    ``metrics["subtasks"]["implement"]`` is the SINGLE authoritative source the
    finalization rollup counts completed/failed/skipped from, so it must be a
    COMPLETE state slice — not a partial one seeded with only some states. A
    partial slice is worse than none: its mere presence makes finalization treat
    it as authoritative and drop the meta/receipt fallback, so a state present in
    the receipts but absent from the slice (e.g. an unmetered ``failed``) would
    be miscounted as ``incomplete``.

    Fill every subtask the usage breakdown did not already cover with a
    state-only marker (``{"subtask_id", "state": <receipt state>}``) carrying NO
    usage fields — honoring the acceptance contract's honesty rule (unknown
    values are omitted, never invented) and keeping ``sum(records) == phase
    rollup`` intact (a stateless marker contributes zero usage). A subtask that
    produced a real usage record keeps that record's terminal state.
    """
    recorded_ids = {rec.get("subtask_id") for rec in records}
    for receipt in latest_receipt_by_id.values():
        sid = receipt.get("subtask_id")
        if sid in recorded_ids:
            continue
        state = receipt.get("state")
        marker: dict[str, Any] = {"subtask_id": sid}
        if state:
            marker["state"] = str(state)
        records.append(marker)
        recorded_ids.add(sid)


def _resolve_implement_handoff_policy(state: PipelineState):
    """Return the active implement step's ``PhaseHandoffPolicy`` or ``None``.

    ADR 0073: when the implement step declares a non-bypass handoff policy the
    subtask_dag path delegates an incomplete delivery to the repair→handoff
    handler; otherwise it keeps the legacy hard stop (lite / unconfigured
    profiles). Direct unit-test dispatch with no active step → ``None``.
    """
    ctx = _ensure_lifecycle_ctx(state)
    step = getattr(ctx, "active_step", None)
    return getattr(step, "handoff", None) if step is not None else None


def _retry_incomplete_ids(payload: Any) -> list[str] | None:
    """Extract the incomplete ids carried by ``state.extras['implement_retry']``.

    ADR 0073 retry-mode: an operator ``retry_feedback`` resume seeds the ids to
    re-run. Accepts a dict (``ids`` / ``incomplete_ids``) or a bare sequence;
    returns ``None`` when no ids are present (no retry narrowing).
    """
    if not payload:
        return None
    if isinstance(payload, dict):
        ids = payload.get("ids") or payload.get("incomplete_ids")
    else:
        ids = payload
    if not ids:
        return None
    return [str(i) for i in ids]


def _truncate_banner_value(text: Any, limit: int = 240) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _log_implement_retry_banner(
    *,
    retry_ids: list[str],
    retry_payload: Any,
    run_plan,
) -> None:
    """Render an explicit operator-facing banner before retry redispatch.

    Without this marker, a resumed ``retry_feedback`` run jumps straight into
    ``ORCHO subtask 1/N START`` with a narrowed count, which looks like an
    unexplained new DAG rather than a deliberate retry of blocked subtasks.
    """
    from agents.stream_log import write_agent_log_section
    from core.io.ansi import C

    lines = [
        "mode: retry_feedback",
        f"retry_subtasks: {', '.join(retry_ids) if retry_ids else '(none)'}",
        f"scheduled_subtasks: {len(getattr(run_plan, 'subtasks', ()))}",
    ]
    if isinstance(retry_payload, dict):
        prior = retry_payload.get("prior_context")
        if isinstance(prior, dict):
            lines.append(f"prior_done_context: {len(prior)}")
        feedback = retry_payload.get("feedback")
        if feedback:
            lines.append(
                f"operator_feedback: {_truncate_banner_value(feedback)}"
            )
    write_agent_log_section(
        "ORCHO implement retry: re-running incomplete subtasks",
        "\n".join(lines),
        label_codes=(C.YELLOW, C.BOLD),
        content_key_codes=(C.GREY,),
        separator_codes=(C.GREY,),
        exit_codes=(C.GREY,),
    )


_GENERIC_MISSING_PLAN_STOP = (
    "implementation_execution=subtask_dag requires a parsed plan "
    "with at least one required subtask"
)


def _missing_plan_stop_message(state: PipelineState) -> str:
    """Operator-facing stop message for the empty/absent parsed-plan guard.

    Default is the generic line. But when ``state.parsed_plan`` is absent AND
    the owned ``RESUME_PLAN_REQUIRED_KEY`` marker is set — meaning a
    checkpoint/handoff resume left the PLAN phase behind (the marker is stamped
    by the resume-artifact bootstrap in ``state_setup`` or by the in-process
    handoff strip/retry sites) — emit an instructive message that names the run
    dir + ``parsed_plan.json`` and distinguishes a *missing* artifact from an
    *unreadable* (corrupt) one via the recorded resume-artifacts provenance.
    This is the authoritative place for that error; do not hide a required
    resume artifact behind the bare ``None`` check.

    Generalization point: a future phase that requires its own typed durable
    output reads the same marker + ``state.extras['resume_artifacts']``
    provenance (or registers its own ``ResumeArtifactSpec.required_when``),
    rather than re-deriving "required but absent" here.
    """
    if state.parsed_plan is not None:
        # Plan present but carries no subtasks — the generic line is correct.
        return _GENERIC_MISSING_PLAN_STOP

    # Lazy import to avoid a phases <- project import cycle at module load.
    from pipeline.project.resume_artifacts import RESUME_PLAN_REQUIRED_KEY

    if not state.extras.get(RESUME_PLAN_REQUIRED_KEY):
        return _GENERIC_MISSING_PLAN_STOP

    output_dir = getattr(state, "output_dir", None)
    artifact = (
        f"{output_dir / 'parsed_plan.json'}"
        if output_dir is not None
        else "parsed_plan.json"
    )
    status = (
        (state.extras.get("resume_artifacts") or {})
        .get("parsed_plan", {})
        .get("status")
    )
    if status == "corrupt":
        detail = f"{artifact} is present but unreadable (corrupt JSON / schema / DAG)"
    elif status == "missing":
        detail = f"{artifact} is missing"
    else:
        detail = f"{artifact} is missing or unreadable"
    return (
        "Cannot resume into implement: a checkpoint/handoff resume left the "
        f"PLAN phase behind, but its durable plan artifact {detail}. The "
        "parsed plan could not be recovered, so the implement DAG has nothing "
        "to run. Re-run the plan profile against the same task to regenerate "
        "parsed_plan.json, or start a fresh run."
    )


def _subtask_write_zone(subtask: Any) -> frozenset[str]:
    """The write zone of a subtask: its ``owned_files``, else its ``files``.

    ``owned_files`` is the wave planner's write-scope primitive; ``files`` is
    the looser declared-touch list used as a fallback when a plan omits the
    owned-files glob. An empty result means the subtask declared no write zone,
    so same-zone continuation cannot be *demonstrated* (ADR 0113 fresh-by-
    default) and the classifier below treats it as new-zone work.
    """
    return frozenset(getattr(subtask, "owned_files", ()) or
                     getattr(subtask, "files", ()) or ())


def _zones_overlap(seeded_zone: frozenset[str], zone: frozenset[str]) -> bool:
    """Whether two declared write zones provably share at least one path.

    Same-write-zone continuation requires *demonstrated* overlap (ADR 0113
    fresh-by-default). Both zones must be non-empty — an empty zone declares no
    write scope, so overlap cannot be proven and the caller must treat the
    follow-on as fresh/companion. Returns ``False`` whenever either zone is
    empty.

    Overlap honours glob semantics in both directions so a declared
    ``owned_files`` glob and a concrete ``files`` path are matched: a pattern
    like ``src/**`` overlaps the path ``src/foo.py`` even though a bare
    set-intersection would call them disjoint. Equality, ``a`` matching ``b``
    as a glob, and ``b`` matching ``a`` as a glob each count as overlap.
    """
    if not seeded_zone or not zone:
        return False
    for a in seeded_zone:
        for b in zone:
            if a == b or fnmatch.fnmatchcase(b, a) or fnmatch.fnmatchcase(a, b):
                return True
    return False


def _subtask_invocation_role(
    seeded_zone: frozenset[str] | None, subtask: Any
) -> SessionInvocationRole:
    """Classify a subtask invoke as a same-write-zone implement follow-on or
    new-zone/companion work (ADR 0113).

    The first subtask invoked on an agent seeds that agent's implement session
    (``seeded_zone is None``) → edit-shaped ``implement`` role; the policy
    starts it FRESH because no session is stored yet. A later subtask on the
    same agent stays an ``implement`` follow-on (the policy may resume the
    seeded session) ONLY when same-write-zone is *demonstrated*: both the
    seeded zone and this subtask's declared write zone are non-empty and
    overlap (with glob semantics — see :func:`_zones_overlap`). Every other
    later subtask is ``companion`` role, which the policy resolves FRESH
    unconditionally.

    This is fresh-by-default for the unprovable cases the reviewer flagged: a
    disjoint declared zone, an undeclared (empty) seed zone, or an undeclared
    current zone all fail to demonstrate same-write-zone, so the follow-on goes
    companion/FRESH and its compact handoff (plan contract + upstream receipts)
    rides its own prompt instead of dragging the seed's transcript across.
    """
    if seeded_zone is None:
        return SessionInvocationRole.IMPLEMENT
    zone = _subtask_write_zone(subtask)
    if _zones_overlap(seeded_zone, zone):
        return SessionInvocationRole.IMPLEMENT
    return SessionInvocationRole.COMPANION


def _partial_subtask_dag_stop_message(unfinished: set[str]) -> str:
    """Operator-facing stop message for a torn (partial) subtask-DAG resume.

    Names the unfinished subtask id(s) and points at the supported recovery
    paths. Deterministic ordering so the message is stable across runs.
    """
    ids = ", ".join(sorted(unfinished))
    return (
        "Cannot resume IMPLEMENT from partial subtask DAG state: subtask "
        f"{ids} started but has no DONE/ATTESTATION event. Start a follow-up "
        "or rerun implement after repair."
    )


def _partial_subtask_dag_stub(parsed_plan: Any) -> dict[str, Any]:
    """Entry stub returned alongside a partial-DAG ``state.stop`` (mirrors the
    missing-plan stop return shape)."""
    return {
        "output": "",
        "meta": {
            "execution_mode": "subtask_dag",
            "concurrency": 1,
            "subtask_count": (
                len(parsed_plan.subtasks) if parsed_plan is not None else 0
            ),
        },
        "implementation_receipts": [],
        "delivery_clean": False,
    }


def _run_subtask_dag_implement(
    state: PipelineState,
    agent: Any,
    implement_baseline,
) -> dict[str, Any]:
    """Execute parsed-plan subtasks through the policy-owned DAG path."""
    parsed_plan = state.parsed_plan
    if state.dry_run:
        return {
            "output": "[DRY RUN]",
            "meta": {
                "execution_mode": "subtask_dag",
                "concurrency": 1,
                "subtask_count": (
                    len(parsed_plan.subtasks)
                    if parsed_plan is not None else 0
                ),
            },
            "implementation_receipts": [],
            "delivery_clean": True,
        }
    # Resume guard: a torn subtask DAG (a subtask started but never reached a
    # successful DONE/ATTESTATION terminal) must not silently continue to the
    # DAG/review — that marches the run on against partial work. Skip on the
    # supported ADR 0073 ``implement_retry`` path, which deliberately re-runs
    # the incomplete ids with the prior partial state as context.
    if not state.extras.get("implement_retry"):
        output_dir = getattr(state, "output_dir", None)
        if output_dir is not None:
            from pipeline.run_state import unfinished_subtask_ids_in_run_dir
            unfinished = unfinished_subtask_ids_in_run_dir(output_dir)
            if unfinished:
                state.stop(_partial_subtask_dag_stop_message(unfinished))
                return _partial_subtask_dag_stub(parsed_plan)
    if parsed_plan is None or not getattr(parsed_plan, "subtasks", ()):
        state.stop(_missing_plan_stop_message(state))
        return {
            "output": "",
            "meta": {
                "execution_mode": "subtask_dag",
                "concurrency": 1,
                "subtask_count": 0,
            },
            "implementation_receipts": [],
            "delivery_clean": False,
        }
    if state.registry is None:
        state.stop(
            "implementation_execution=subtask_dag requires state.registry "
            "so subtask agents can be resolved"
        )
        return {
            "output": "",
            "meta": {
                "execution_mode": "subtask_dag",
                "concurrency": 1,
                "subtask_count": len(parsed_plan.subtasks),
            },
            "implementation_receipts": [],
            "delivery_clean": False,
        }

    from pipeline.dag_runner import run_dag_sequential

    fallback_runtime = getattr(agent, "runtime", None) or "claude"
    fallback_model = (
        state.extras.get("fallback_model")
        or getattr(agent, "model", None)
        or ""
    )
    subtask_invocation_outcomes: list[AgentInvocationOutcome] = []
    # Per-subtask usage capture (observe-only). One entry per real invoke,
    # tying the outcome back to its SubTask so the durable breakdown can group
    # by subtask id. Parallel to ``subtask_invocation_outcomes`` (which stays
    # the flat phase-rollup input) so the existing ``_metrics_usage`` is
    # untouched.
    subtask_usage_captures: list[
        tuple[Any, AgentInvocationOutcome, float | None]
    ] = []
    # ADR 0113: per-agent seeded write zone. The first subtask invoked on a
    # given agent seeds that agent's implement session and records its write
    # zone here; later subtasks on the same agent are classified against it so
    # a new-zone/companion subtask reusing the agent does not continue the
    # prior subtask's session. Local to this DAG run (no cross-run leak).
    agent_seeded_zone: dict[int, frozenset[str]] = {}

    def _invoke_subtask(
        sub_agent,
        turn,
        project_dir,
        subtask,
        mutates_artifacts: bool = True,
    ):
        """Session-aware subtask invoke (P1).

        Routes each subtask through the canonical ``_session_aware_invoke``
        seam under ``phase="implement"`` so continuation is policy/state
        driven (never subtask index) and the trace is real. ``trace_phase``
        is a per-subtask temp slot, popped here so it never collides with the
        phase aggregate or leaks into the persisted session.
        """
        trace_slot = f"implement:subtask:{subtask.id}"
        # ADR 0113: subtasks are edit-shaped implement work in the run
        # worktree, but only a *same-write-zone* follow-on may continue the
        # seeded implement session. ``_subtask_invocation_role`` resolves the
        # explicit role from the per-agent seeded zone: a same-zone follow-on
        # stays ``implement`` (the policy may resume), while new-zone/companion
        # work becomes ``companion`` (the policy forces FRESH regardless of any
        # stored session id) so a reused agent never drags a prior subtask's
        # transcript across zones — the per-subtask prompt carries the compact
        # handoff (plan contract + upstream receipts) instead.
        seeded_zone = agent_seeded_zone.get(id(sub_agent))
        invocation_role = _subtask_invocation_role(seeded_zone, subtask)
        if seeded_zone is None:
            agent_seeded_zone[id(sub_agent)] = _subtask_write_zone(subtask)
        # ADR 0113 (F1): a new-zone/companion invoke shares the implement
        # physical session key (same delta/trace plumbing) but must not seed
        # that shared slot with its own fresh transcript — otherwise a later
        # same-write-zone implement follow-on on the reused agent would resume
        # the companion session instead of the original implement chain. Only
        # the same-write-zone implement role commits the shared session.
        is_implement = invocation_role is SessionInvocationRole.IMPLEMENT
        out = _session_aware_invoke(
            sub_agent, state,
            phase="implement",
            trace_phase=trace_slot,
            turn=turn,
            cwd=project_dir,
            mutates_artifacts=mutates_artifacts,
            continue_session=_should_continue_prompt_session(
                state, sub_agent, phase="implement",
                role=invocation_role,
            ),
            commit_session=is_implement,
        )
        outcome = getattr(sub_agent, "last_invocation_outcome", None)
        if outcome is not None:
            subtask_invocation_outcomes.append(outcome)
            subtask_usage_captures.append((
                subtask,
                outcome,
                getattr(sub_agent, "last_duration_s", None),
            ))
        popped = state.phase_log.pop(trace_slot, {})
        return out, popped.get("prompt_render")

    # ADR 0073 retry-mode: an operator ``retry_feedback`` resume sets
    # ``state.extras['implement_retry']`` with the incomplete ids to re-run.
    # Narrow the executed DAG to just those ids; the already-done subtasks ride
    # along as read-only prior context so they are NOT re-invoked or re-mutated.
    run_plan = parsed_plan
    prior_results = None
    retry_payload = state.extras.get("implement_retry")
    retry_ids = _retry_incomplete_ids(retry_payload)
    if retry_ids:
        from pipeline.dag_runner import PriorSubtaskContext
        from pipeline.subtask_substance_repair import build_repair_plan
        retry_id_set = set(retry_ids)
        run_plan = build_repair_plan(parsed_plan, retry_id_set)
        # Degraded upstream context for the done (non-retry) subtasks, seeded
        # by the resume arm from the persisted receipts (attestation summary /
        # error). Falls back to a bare context if a receipt was unavailable.
        prior_ctx_data = (
            retry_payload.get("prior_context")
            if isinstance(retry_payload, dict) else None
        ) or {}
        prior_results = {}
        for s in parsed_plan.subtasks:
            if s.id in retry_id_set:
                continue
            data = prior_ctx_data.get(s.id) or {}
            prior_results[s.id] = PriorSubtaskContext(
                subtask_id=s.id,
                attestation_summary=str(data.get("attestation_summary", "")),
                attestation_error=data.get("attestation_error"),
            )
        _log_implement_retry_banner(
            retry_ids=retry_ids,
            retry_payload=retry_payload,
            run_plan=run_plan,
        )

    result = run_dag_sequential(
        run_plan,
        state.plugin,
        state.registry,
        project_dir=_agent_project_dir(state),
        fallback_runtime=fallback_runtime,
        fallback_model=fallback_model,
        step_overrides=None,
        change_handoff=_change_handoff_for(state),
        stop_on_failure=True,
        dry_run=state.dry_run,
        invoke_subtask=_invoke_subtask,
        prior_results=prior_results,
        verification_part=_verification_contract_part(state, "implement"),
    )
    state.dag_result = result

    if result.skill_bindings:
        bucket = state.extras.setdefault("skill_bindings", [])
        bucket.extend(result.skill_bindings)

    receipts = [r.to_dict() for r in result.receipts]
    # Latest receipt per subtask across the main pass AND every repair pass.
    # The durable per-subtask usage records read ``state`` from here; repair
    # passes overlay their receipts as they complete (below) so a subtask
    # repaired in an earlier multi-attempt pass shows its final ``done``
    # state rather than a stale first-pass ``incomplete``. Built from the
    # main-pass receipts in plan order; repair overlays only mutate states.
    latest_receipt_by_id: dict[str, dict[str, Any]] = {
        r["subtask_id"]: r for r in receipts
    }
    planned_ids = {s.id for s in run_plan.subtasks}
    receipt_ids = {r["subtask_id"] for r in receipts}
    missing_ids = sorted(planned_ids - receipt_ids)
    not_done = sorted(
        r["subtask_id"] for r in receipts
        if r.get("state") != "done"
    )
    # P7: subtasks blocked specifically by the done-criteria attestation gate
    # (invocation succeeded, but the typed self-attestation was missing /
    # malformed / mismatched / not-all-met). Distinct from a hard ``failed``
    # exec error and surfaced with its gate reason so a reviewer/human sees
    # WHY the criteria contract did not close.
    attestation_incomplete = {
        r["subtask_id"]: r.get("attestation_error") or "criteria not closed"
        for r in receipts
        if r.get("state") == "incomplete"
    }
    unmet_done_criteria = tuple(
        {
            "subtask_id": receipt["subtask_id"],
            "index": criterion.get("index"),
            "criterion": criterion.get("criterion", ""),
            "evidence": criterion.get("evidence", ""),
        }
        for receipt in receipts
        if receipt.get("state") == "incomplete"
        for criterion in receipt.get("criteria_report", ())
        if criterion.get("met") is False
    )
    delivery_clean = not missing_ids and not not_done

    entry: dict[str, Any] = {
        "output": "[DRY RUN]" if state.dry_run else _integrated_subtask_output(result),
        "meta": {
            "execution_mode": "subtask_dag",
            "concurrency": 1,
            "subtask_count": len(run_plan.subtasks),
            "completed_count": len(result.completed),
            "failed_count": len(result.failed),
            "skipped_count": len(result.skipped),
        },
        "implementation_receipts": receipts,
        "prompt_render": _aggregate_subtask_prompt_render(state, result),
        "subtask_prompt_renders": _subtask_prompt_render_list(result),
        "delivery_clean": delivery_clean,
    }
    if implement_baseline:
        entry["change_baseline_ref"] = implement_baseline
    if missing_ids:
        entry["missing_subtask_receipts"] = missing_ids
    if not_done:
        entry["incomplete_subtasks"] = not_done
    if attestation_incomplete:
        entry["attestation_incomplete"] = attestation_incomplete

    if run_plan.subtasks:
        entry["progress"] = {
            "kind": "subtasks",
            "completed": len(result.completed),
            "total": len(run_plan.subtasks),
        }

    if not delivery_clean:
        policy = _resolve_implement_handoff_policy(state)
        if policy is not None and policy.type is not PhaseHandoffType.HUMAN_BYPASS:
            # ADR 0073: a configured implement handoff routes an incomplete
            # delivery through the repair→handoff handler (T6) instead of a
            # hard stop. The repair logic lives in subtask_dag_handoff.py; here
            # we only bind the DAG executor and record the outcome on ``entry``.
            from pipeline.phases.builtin.subtask_dag_handoff import (
                handle_subtask_dag_handoff,
            )

            def _repair_pass(repair_plan, prior):
                pass_result = run_dag_sequential(
                    repair_plan,
                    state.plugin,
                    state.registry,
                    project_dir=_agent_project_dir(state),
                    fallback_runtime=fallback_runtime,
                    fallback_model=fallback_model,
                    step_overrides=None,
                    change_handoff=_change_handoff_for(state),
                    stop_on_failure=True,
                    dry_run=state.dry_run,
                    invoke_subtask=_invoke_subtask,
                    prior_results=prior,
                    verification_part=_verification_contract_part(
                        state, "implement",
                    ),
                )
                # F1: overlay THIS pass's receipts immediately. The substance
                # repair engine keeps only the final pass's receipts, so a
                # subtask that reaches ``done`` in an earlier pass drops out of
                # ``remaining`` and never reappears in the final-pass receipts.
                # Recording every pass's latest receipt here preserves the
                # final state per repaired subtask across all attempts.
                for r in pass_result.receipts:
                    latest_receipt_by_id[r.subtask_id] = r.to_dict()
                return pass_result

            done_context: dict[str, Any] = dict(prior_results or {})
            done_context.update({r.subtask_id: r for r in result.completed})
            outcome = handle_subtask_dag_handoff(
                state,
                policy=policy,
                parsed_plan=run_plan,
                incomplete_ids=tuple(not_done),
                missing_ids=tuple(missing_ids),
                attestation_incomplete=attestation_incomplete,
                findings=None,
                done_context=done_context,
                repair_pass=_repair_pass,
                last_output=entry["output"],
                unmet_done_criteria=unmet_done_criteria,
            )
            entry["delivery_status"] = outcome.delivery_status
            if outcome.delivery_status != "incomplete":
                entry["delivery_clean"] = True
            if outcome.waiver_id is not None:
                entry["delivery_waived"] = True
                entry["waiver_id"] = outcome.waiver_id
                entry["action"] = outcome.action
                entry["decided_by"] = outcome.decided_by
            # On an ineligible exhaustion the handler set
            # ``state.phase_handoff_request``; the orchestrator pauses the run.
            # We must NOT ``state.stop`` here — pause ≠ hard stop.
        else:
            reason_bits: list[str] = []
            if missing_ids:
                reason_bits.append(f"missing receipts: {', '.join(missing_ids)}")
            if not_done:
                reason_bits.append(f"incomplete subtasks: {', '.join(not_done)}")
            for sid, why in attestation_incomplete.items():
                reason_bits.append(f"{sid} attestation: {why}")
            state.stop(
                "subtask_dag delivery blocked; "
                + "; ".join(reason_bits)
            )

    metrics_usage = _aggregate_subtask_metrics_usage(
        subtask_invocation_outcomes,
    )
    if metrics_usage is not None:
        entry["_metrics_usage"] = metrics_usage

    # Durable per-subtask breakdown (observe-only). Explains the phase rollup
    # above without altering it; omitted entirely when no metered usage was
    # captured (e.g. a mock with no provider numbers).
    subtask_records = _build_subtask_usage_records(
        subtask_usage_captures,
        latest_receipt_by_id,
    )
    # Complete the breakdown into a full receipt state mirror. Subtasks with no
    # metered usage capture — skipped ones (never invoked) AND run subtasks whose
    # runtime surfaced no usable outcome — are absent from the usage breakdown
    # above. The metrics slice is the single authoritative source the
    # finalization rollup counts completed/failed/skipped from, so it must carry
    # EVERY subtask's final state (a partial slice would make finalization drop
    # its meta/receipt fallback and miscount a missing state as incomplete).
    # Append a state-only marker (no invented usage) per uncovered receipt id,
    # preserving DAG (receipt) order.
    _append_receipt_state_records(subtask_records, latest_receipt_by_id)
    if subtask_records:
        entry["subtask_metrics"] = subtask_records

    if not state.dry_run:
        _print_implement_summary(
            state, entry, title="Implementation",
            phase_name="implement", baseline_ref=implement_baseline,
        )
    return entry
