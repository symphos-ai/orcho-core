# SPDX-License-Identifier: Apache-2.0
"""Review, repair and implementation-summary support for builtin phases."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.io.transcript import (
    render_implement_summary as _render_implement_summary,
    render_review_block as _render_review_block,
)
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _ensure_lifecycle_ctx,
)
from pipeline.phases.builtin.plan_artifact import _suppress_phase_output_preview

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


_LAST_REPAIR_RECEIPT_KEY = "_last_repair_receipt"
_PHASE_HANDOFF_WAIVER_KEY = "phase_handoff_waiver"
_AUTO_CHAIN_DEFAULT_MAX_TOKENS_IN = 1_000_000
_AUTO_CHAIN_DEFAULT_MAX_TOOL_CALLS = 30
_AUTO_CHAIN_SUPPRESSED_CONTEXT_PRESSURE = (
    "auto_chain_suppressed_context_pressure"
)


def _format_waived_finding(item: Any) -> str | None:
    """Render one waived finding as a compact single line."""
    if isinstance(item, dict):
        sev = item.get("severity") or item.get("level")
        title = (
            item.get("title")
            or item.get("body")
            or item.get("message")
            or item.get("summary")
            or ""
        )
        title = str(title).replace("\n", " ").strip()
        if not title and sev is None:
            return None
        return f"[{sev}] {title}" if sev else title
    text = str(item).replace("\n", " ").strip()
    return text or None


def _operator_waiver_text(state: PipelineState) -> str:
    """Build the compact operator-waiver block for downstream review gates.

    Reads the runtime waiver copy that ``continue_with_waiver`` stored in
    ``state.extras['phase_handoff_waiver']`` (durably persisted to meta and
    rehydrated on fresh-process resume by ``pipeline.project.state_setup``)
    and renders the operator verdict plus the waived findings / prior
    reviewer critique. Returns ``""`` when no waiver is active — the
    builder then adds no operator_waiver part.
    """
    waiver = state.extras.get(_PHASE_HANDOFF_WAIVER_KEY)
    if not isinstance(waiver, dict):
        return ""
    verdict = str(waiver.get("waiver_text") or "").strip()
    if not verdict:
        return ""
    lines = [f"Operator verdict: {verdict}"]
    findings = waiver.get("findings")
    if isinstance(findings, list):
        rendered = [
            line
            for item in findings
            if (line := _format_waived_finding(item)) is not None
        ]
        if rendered:
            lines.append("Waived findings:")
            lines.extend(f"- {line}" for line in rendered)
    critique = str(waiver.get("critique") or "").strip()
    if critique:
        lines.append(f"Prior reviewer critique:\n{critique}")
    return "\n".join(lines)


def _store_repair_receipt(state: PipelineState, receipt: Any) -> dict[str, Any]:
    from pipeline.repair_protocol import repair_receipt_to_dict

    payload = repair_receipt_to_dict(receipt)
    state.extras[_LAST_REPAIR_RECEIPT_KEY] = payload
    return payload


def _config_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _auto_chain_limits() -> tuple[int, int]:
    from core.infra import config

    try:
        session = config.AppConfig.load().session
    except Exception:  # noqa: BLE001
        session = {}
    return (
        _config_int(
            session.get("auto_chain_max_tokens_in"),
            _AUTO_CHAIN_DEFAULT_MAX_TOKENS_IN,
        ),
        _config_int(
            session.get("auto_chain_max_tool_calls"),
            _AUTO_CHAIN_DEFAULT_MAX_TOOL_CALLS,
        ),
    )


def _implement_metrics_usage(state: PipelineState) -> dict[str, Any]:
    implement_log = state.phase_log.get("implement")
    if not isinstance(implement_log, dict):
        return {}
    usage = implement_log.get("_metrics_usage")
    return dict(usage) if isinstance(usage, dict) else {}


def _auto_chain_context_pressure(state: PipelineState) -> dict[str, Any] | None:
    usage = _implement_metrics_usage(state)
    if not usage:
        return None
    max_tokens_in, max_tool_calls = _auto_chain_limits()
    tokens_in = _config_int(usage.get("tokens_in"), 0)
    tool_calls = _config_int(usage.get("tool_calls"), 0)
    if tokens_in <= max_tokens_in and tool_calls <= max_tool_calls:
        return None
    return {
        "tokens_in": tokens_in,
        "tool_calls": tool_calls,
        "max_tokens_in": max_tokens_in,
        "max_tool_calls": max_tool_calls,
    }


def _repair_receipt_text(state: PipelineState) -> str:
    from pipeline.repair_protocol import render_repair_receipt

    payload = state.extras.get(_LAST_REPAIR_RECEIPT_KEY)
    return render_repair_receipt(payload if isinstance(payload, dict) else None)


def _verification_receipt_text(state: PipelineState) -> str:
    """Render a brief verification-receipt summary for the reviewer (ADR 0076).

    Reads the durable verification-environment receipts written under the
    run output dir by the implement / repair phases (T6) and renders a
    compact block: interpreter, cwd, the checks/import-invariants that
    were verified, the commands run by that interpreter, and whether any
    temporary venv lived outside the checkout. Returns ``""`` when there
    are no receipts so the prompt builder adds no empty block (the
    reviewer may verify manually).
    """
    output_dir = getattr(state, "output_dir", None)
    if output_dir is None:
        return ""
    from pipeline.evidence.verification_receipt import (
        load_verification_receipts,
    )

    receipts = load_verification_receipts(output_dir)
    if not receipts:
        return ""

    lines: list[str] = [
        "Developer-side verification environment (substantiated checks; "
        "do not re-derive — verify only what is missing):",
    ]
    for r in receipts:
        phase = r.get("phase", "?")
        round_n = r.get("round", "?")
        lines.append(
            f"- {phase} round {round_n}: python={r.get('python', '?')}; "
            f"cwd={r.get('cwd', '?')}"
        )
        checks = r.get("checks") if isinstance(r.get("checks"), list) else []
        if checks:
            for c in checks:
                if not isinstance(c, dict):
                    continue
                status = "pass" if c.get("passed") else "FAIL"
                lines.append(
                    f"    check {c.get('name', '?')}: {status} "
                    f"(expected={c.get('expected')!r}, "
                    f"actual={c.get('actual')!r})"
                )
        else:
            lines.append("    checks: none recorded")
        commands = (
            r.get("commands") if isinstance(r.get("commands"), list) else []
        )
        for cmd in commands:
            if not isinstance(cmd, dict):
                continue
            argv = cmd.get("argv")
            argv_str = " ".join(argv) if isinstance(argv, list) else str(argv)
            lines.append(
                f"    command: {argv_str} (exit={cmd.get('exit_code')})"
            )
        lines.append(
            "    temp venv outside checkout: "
            f"{bool(r.get('temp_env_outside_checkout', True))}"
        )
    return "\n".join(lines)


def _verification_readiness_text(state: PipelineState) -> str:
    """Render the Stage 5 readiness block for ``final_acceptance`` (ADR 0082).

    Dry-run short-circuits FIRST — before any receipt loader is touched — so
    a dry run never reads the run directory. The no-contract path returns
    ``""`` (the builder then adds no part and the wire prompt stays
    byte-identical to the pre-Stage-5 prompt). The delivery gate plan is
    resolved inside :func:`build_final_acceptance_readiness` from the
    executable ``before_delivery`` routing epoch or a fresh selection over the
    current checkout — never from the advisory prompt-preview cache.
    """
    if getattr(state, "dry_run", False):
        return ""
    output_dir = getattr(state, "output_dir", None)
    if output_dir is None:
        return ""
    contract = state.extras.get("verification_contract")
    if contract is None:
        return ""
    from pipeline.verification_contract import PlaceholderContext
    from pipeline.verification_readiness import (
        build_final_acceptance_readiness,
        render_readiness_block,
    )

    ctx = state.extras.get("verification_placeholders")
    if ctx is None:
        ctx = PlaceholderContext()
    summary = build_final_acceptance_readiness(
        contract, output_dir, ctx, extras=state.extras,
    )
    return render_readiness_block(summary) or ""


def _required_receipt_backstop(
    state: PipelineState, *, language: str | None = None,
) -> list[dict[str, Any]]:
    """Engine gaps for unproven required delivery gates (ADR 0090).

    The deterministic closing-gate backstop: when the declared contract has
    required delivery commands whose receipts classify missing / failed /
    stale, return one release-gap dict per command so ``final_acceptance``
    can merge them and force a REJECTED verdict — a ``require`` gate that was
    silently skipped must never end in a green acceptance. Empty under
    dry-run, without a contract / run dir, or when an operator waiver is
    active (``continue_with_waiver`` IS the explicit human decision the
    backstop must respect).
    """
    if getattr(state, "dry_run", False):
        return []
    output_dir = getattr(state, "output_dir", None)
    if output_dir is None:
        return []
    contract = state.extras.get("verification_contract")
    if contract is None:
        return []
    if _operator_waiver_text(state):
        return []
    from pipeline.verification_contract import PlaceholderContext
    from pipeline.verification_readiness import required_receipt_gaps

    ctx = state.extras.get("verification_placeholders")
    if ctx is None:
        ctx = PlaceholderContext()
    return required_receipt_gaps(
        contract, output_dir, ctx, extras=state.extras, language=language,
    )


def _scope_expansion_assessment(state: PipelineState):
    """Facade: build the scope-expansion assessment from durable artefacts.

    Thin wrapper over :mod:`pipeline.phases.builtin.scope_expansion_support`
    (the focused I/O home) so the handler and tests reach it through the same
    ``review_support`` surface as the readiness/backstop facades. Pure
    classification lives in :mod:`pipeline.engine.scope_expansion`.
    """
    from pipeline.phases.builtin.scope_expansion_support import (
        scope_expansion_assessment,
    )

    return scope_expansion_assessment(state)


def _render_scope_expansion(assessment) -> str:
    """Render the compact scope-expansion block (``""`` when empty)."""
    from pipeline.phases.builtin.scope_expansion_support import (
        render_scope_expansion_text,
    )

    return render_scope_expansion_text(assessment)


def _scope_expansion_text(state: PipelineState) -> str:
    """Facade: gather + render the scope-expansion prompt block."""
    from pipeline.phases.builtin.scope_expansion_support import (
        scope_expansion_text,
    )

    return scope_expansion_text(state)


def _route_scope_expansion_sanction(
    assessment, *, operating_mode, has_active_waiver: bool,
):
    """Facade: project a scope-expansion assessment onto its §5 sanction route.

    Thin wrapper over
    :func:`pipeline.phases.builtin.scope_expansion_support.route_scope_expansion_sanction`
    so the handler reaches the mode-projected routing (release-gap / handoff /
    alert) through the same ``review_support`` surface as the other facades.
    """
    from pipeline.phases.builtin.scope_expansion_support import (
        route_scope_expansion_sanction,
    )

    return route_scope_expansion_sanction(
        assessment,
        operating_mode=operating_mode,
        has_active_waiver=has_active_waiver,
    )


def _raise_scope_expansion_handoff(state: PipelineState, routing, *, last_output: str = ""):
    """Facade: open the out-of-plan phase-handoff pause for a HANDOFF route.

    Thin wrapper over
    :func:`pipeline.phases.builtin.scope_expansion_support.raise_scope_expansion_handoff`
    so the final_acceptance handler raises the runtime scope-expansion handoff
    (ADR 0112 §5 T3) through the same ``review_support`` surface as the routing
    facade. Returns the built :class:`PhaseHandoffRequested` (or ``None``).
    """
    from pipeline.phases.builtin.scope_expansion_support import (
        raise_scope_expansion_handoff,
    )

    return raise_scope_expansion_handoff(state, routing, last_output=last_output)


def _current_plan_review_subject(state: PipelineState) -> str:
    from pipeline.repair_protocol import render_current_plan_subject

    return render_current_plan_subject(getattr(state, "parsed_plan", None))


def _current_change_review_subject(state: PipelineState) -> str:
    from pipeline.repair_protocol import render_current_change_subject

    return render_current_change_subject(_agent_project_dir(state))


def _capture_phase_baseline(state: PipelineState) -> str | None:
    """Snapshot the worktree before a mutating phase invokes its runtime.

    Returns an immutable tree-ish SHA via ``snapshot_worktree`` when
    the agent cwd resolves to a git root; otherwise ``None``. The
    caller passes the result through to :func:`_print_implement_summary`
    as ``baseline_ref`` — ``None`` falls back to the cumulative
    diff block, which is the right degradation when there's no git to
    diff against.
    """
    if state.dry_run:
        return None
    from pipeline.engine.run_diff import resolve_git_root, snapshot_worktree
    try:
        git_root = resolve_git_root(Path(_agent_project_dir(state)))
    except Exception:  # noqa: BLE001 — never raise from a transparency hook
        return None
    if git_root is None:
        return None
    try:
        return snapshot_worktree(git_root)
    except Exception:  # noqa: BLE001
        return None


def _print_implement_summary(
    state: PipelineState,
    entry: dict,
    *,
    title: str,
    phase_name: str | None = None,
    baseline_ref: str | None = None,
) -> None:
    """Emit a human-readable outcome block after an IMPLEMENT-style
    phase (``implement`` or ``repair_changes``).

    Goal: someone scanning the terminal can answer "did anything
    actually happen and what?" without opening the run dir. We pull
    cheap facts that already exist:

    * ``progress`` — a small typed progress chip (planned files for
      ``whole_plan`` or completed subtasks for ``subtask_dag``).
    * git working-tree state — list of files this phase mutated.
      When ``baseline_ref`` is passed, the list and the printed diff
      are scoped to that snapshot (per-phase); otherwise both fall
      back to the cumulative ``git status`` / cumulative diff.
    * agent session id (so the operator knows the bridge handle).
    * first line of the agent's final text as a chip-style summary.

    The block is intentionally degradable: when none of the above is
    available the renderer prints a single ``(no observable changes)``
    note so phase transparency is still preserved.

    ADR 0046 Phase F follow-up — suppress under SILENT (in addition to
    ``state.dry_run``) unless the typed request explicitly opts into
    parsed phase output previews. The structured ``progress`` /
    session_id / files-touched data is already in ``session.json`` +
    the per-phase ``state.phase_log`` entry; this block is pure CLI
    courtesy.
    """
    if _suppress_phase_output_preview(state):
        return
    from core.io.ansi import C, paint
    from core.io.git_helpers import git_changed_files
    cwd = _agent_project_dir(state)

    # Per-phase mode: derive both the files list and the diff preview
    # from a snapshot diff so the summary chip and the "Files Diff"
    # block describe the same phase delta. Done before
    # ``_render_implement_summary`` so the chip's ``files`` slot can
    # use the parser's normalized path list.
    phase_preview: str | None = None
    phase_files: tuple[str, ...] | None = None
    if baseline_ref is not None and phase_name and state.output_dir is not None:
        from pipeline.engine.run_diff import capture_phase_diff
        try:
            captured = capture_phase_diff(
                Path(cwd),
                Path(state.output_dir),
                baseline_ref=baseline_ref,
                phase_name=phase_name,
            )
        except Exception:  # noqa: BLE001 — never raise from a transparency block
            captured = None
        if captured is None:
            phase_preview = ""
            phase_files = ()
        else:
            phase_preview, phase_files = captured

    if phase_files is not None:
        files_touched: list[str] = list(phase_files)
    else:
        try:
            files_touched = git_changed_files(cwd)
        except Exception:  # noqa: BLE001 — never raise from a transparency block
            files_touched = []

    meta = entry.get("meta") or {}
    session_id = meta.get("session_id")
    followup_parent_session_id = meta.get("followup_parent_session_id")
    output_text = (entry.get("output") or "").strip()
    # First non-empty line — agents conventionally end with a one-line
    # summary; if not, the first line is still a useful chip.
    first_line = next(
        (ln for ln in output_text.splitlines() if ln.strip()), "",
    )

    print(_render_implement_summary(
        title=title,
        files_touched=files_touched,
        progress=entry.get("progress"),
        session_id=session_id,
        followup_parent_session_id=followup_parent_session_id,
        summary=first_line,
    ))

    # P4: transient per-subtask render rollup. PRINT-ONLY — read from the
    # in-memory ``subtask_prompt_renders`` (subtask_dag only) and write nothing
    # back to ``entry``/``meta`` so the durable session shape is unchanged.
    _subtask_renders = entry.get("subtask_prompt_renders")
    if _subtask_renders:
        print(paint("  Subtask renders", C.CYAN, C.BOLD))
        for _rec in _subtask_renders:
            _pr = _rec.get("prompt_render") or {}
            _mode = _pr.get("render_mode", "?")
            _cont = _pr.get("continue_session")
            _cont_s = (
                "" if _cont is None
                else f" (continue_session={str(_cont).lower()})"
            )
            print(f"    {_rec.get('subtask_id')}: {_mode}{_cont_s}")

    if phase_preview is not None:
        # Per-phase rendering path: print only what this phase mutated,
        # never the cumulative diff under a per-phase header.
        if phase_preview:
            print(paint("  Files Diff (this phase)", C.CYAN, C.BOLD))
            print(phase_preview, end="")
        else:
            print(
                f"{paint('  Files Diff (this phase):', C.CYAN, C.BOLD)} "
                f"(no changes)",
            )
        return

    if state.output_dir is not None:
        from pipeline.engine.run_diff import capture_and_render_run_diff

        preview = capture_and_render_run_diff(
            Path(cwd), Path(state.output_dir),
        )
        if preview:
            print(paint("  Files Diff", C.CYAN, C.BOLD))
            print(preview, end="")


def _print_review_preview(state: PipelineState, phase_log_key: str, title: str) -> None:
    """Emit the structured reviewer block on stdout after a reviewer
    phase finishes.

    Pulls the typed contract fields written by the phase handler
    (``verdict``, ``short_summary``, ``findings``, plus optional
    ``parse_error``) and renders them via
    :func:`core.io.transcript.render_review_block`. ``state.dry_run``
    suppresses output so dry-run flows stay quiet.

    For ``final_acceptance`` the phase handler also writes release-
    gate fields (``ship_ready``, ``release_blockers``,
    ``verification_gaps``, ``contract_status``) into ``phase_log``
    alongside the review-shape mirror — ADR 0025 dual-shape. Those
    extras are forwarded to the renderer when present so the CLI
    surfaces the full release contract (``why_blocks_release``,
    contract-status block, ship_ready chip) instead of silently
    dropping it to the review subset.

    ADR 0046 Phase F follow-up — also suppress under SILENT unless the
    typed request explicitly opts into parsed phase output previews.
    The verdict + findings list lives on ``state.phase_log[...]``
    (which finalize persists to ``session.json``); the printed block is
    pure CLI transparency.
    """
    if _suppress_phase_output_preview(state):
        return
    log = state.phase_log.get(phase_log_key) or {}
    if not log.get("verdict"):
        return
    payload: dict[str, object] = {
        "verdict":       log.get("verdict"),
        "short_summary": log.get("short_summary"),
        "findings":      log.get("findings") or [],
        "risks":         log.get("risks") or [],
        "checks":        log.get("checks") or [],
    }
    for key in (
        "ship_ready", "release_blockers", "verification_gaps",
        "contract_status",
    ):
        if key in log:
            payload[key] = log[key]
    if "parse_error" in log:
        payload["parse_error"] = log["parse_error"]
    print(_render_review_block(
        payload, title=title, round=_review_round(state, phase_log_key, log),
    ))


def _review_round(
    state: PipelineState, phase_log_key: str, log: dict,
) -> int | None:
    """Resolve the reviewer round number for the summary ``R<n>`` token.

    Only the round-bearing reviewer loops carry a meaningful round:
    ``validate_plan`` mirrors ``plan_round`` as ``attempt`` in its phase
    log, and ``review_changes`` tracks ``repair_round`` in ``state.extras``.
    ``final_acceptance`` has no round concept, so it stays ``None`` (never
    an ``R<n>``). The summary renderer only surfaces the token when
    ``round > 1``.
    """
    if phase_log_key == "validate_plan":
        raw = log.get("attempt")
    elif phase_log_key == "review_changes":
        raw = log.get("attempt") or state.extras.get("repair_round")
    else:
        return None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_fix_runtime_config(state: PipelineState) -> dict:
    """Per-round repair_changes agent + session mode resolution.

    Reads run-level config from ``state.extras`` (orchestrator stuffs at
    run start) and the current round counter (``repair_round`` for the
    review/fix loop). Returns a dict with the resolved values that the
    handler then uses + plugs into ``state.extras`` / ``state.phase_config``:

      * ``repair_model_for_round`` — escalates to repair_escalation_model on an
        automatic round > 1; a human-directed ``retry_feedback`` round is a
        continuation, so it keeps the base ``repair_model``.
      * ``effective_mode`` — resolves AUTO via _resolve_session_mode on every
        round (no hardcoded STATELESS). Round 1 and the human-directed
        continuation chain from the implement turn; an automatic round > 1
        chains from the previous repair turn, so cross-model escalation
        resolves to HYBRID (re-prime), not a cold STATELESS.
      * ``human_directed`` — True on a human-directed ``retry_feedback`` round;
        the handler then skips the escalation swap and continues the
        implementer session.

    Phase 5e-5 substep 6b: ctx is always populated by the FSM; direct-call
    fallback for non-FSM callers removed.
    """

    from agents.protocols import SessionMode
    from pipeline.runtime.handoff import HUMAN_DIRECTED_FLAG_KEY

    ctx = _ensure_lifecycle_ctx(state)
    _resolve_session_mode = ctx.session_mode_resolver

    repair_round = int(
        state.extras.get("repair_round")
        or state.extras.get("loop_round")
        or 1
    )
    human_directed = bool(state.extras.get(HUMAN_DIRECTED_FLAG_KEY))

    requested_raw = state.extras.get("session_mode_initial", "auto")
    try:
        requested = SessionMode(requested_raw)
    except ValueError:
        requested = SessionMode.AUTO

    implement_model = state.extras.get("implement_model", "")
    repair_model = state.extras.get("repair_model", "")
    repair_escalation_model = state.extras.get("repair_escalation_model", repair_model)
    chain_same_model_only = bool(
        state.extras.get("chain_same_model_only", False)
    )

    # An automatic round > 1 escalates to the more capable model. A
    # human-directed ``retry_feedback`` round is a continuation of the
    # implementer thread, so it stays on the base repair model and resumes.
    escalating = repair_round > 1 and not human_directed
    repair_model_for_round = repair_escalation_model if escalating else repair_model

    # Continuation rounds (round 1, or any human-directed retry) chain from
    # the implement turn; an automatic round > 1 chains from the previous
    # repair turn. Feeding the prior turn's model as the "from" side keeps the
    # same-model CHAIN fast path, while a real model change on an automatic
    # escalation resolves to HYBRID per ``chain_same_model_only`` — never a
    # cold STATELESS.
    from_side_model = repair_model if escalating else implement_model
    effective_mode = _resolve_session_mode(
        requested,
        repair_round=repair_round,
        implement_model=from_side_model,
        repair_model=repair_model_for_round,
        chain_same_model_only=chain_same_model_only,
    )
    session_mode_reason = None
    session_mode_context_pressure = None
    if requested is SessionMode.AUTO and effective_mode is SessionMode.CHAIN:
        session_mode_context_pressure = _auto_chain_context_pressure(state)
        if session_mode_context_pressure is not None:
            effective_mode = (
                SessionMode.HYBRID
                if str(state.extras.get("codemap") or "").strip()
                else SessionMode.STATELESS
            )
            session_mode_context_pressure["fallback_mode"] = effective_mode.value
            session_mode_reason = _AUTO_CHAIN_SUPPRESSED_CONTEXT_PRESSURE

    return {
        "repair_round":           repair_round,
        "repair_model_for_round": repair_model_for_round,
        "effective_mode":         effective_mode,
        "human_directed":         human_directed,
        "session_mode_reason":    session_mode_reason,
        "session_mode_context_pressure": session_mode_context_pressure,
    }
