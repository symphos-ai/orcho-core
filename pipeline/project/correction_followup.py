"""Auto-correction follow-up loop (ADR 0070).

When an interactive run halts at the correction gate because the operator
chose ``fix`` (``status == "halted"`` and
``halt_reason == "commit_decision_fix"``), the CLI re-enters the pipeline
as a follow-up run with a concise remediation task, writes the full rejection
context as a run artifact, and reuses the parent's retained worktree.

The loop is **operator-gated**: every correction round again ends at the
correction gate (when acceptance still rejects), so it continues only while
the operator keeps choosing ``fix`` and terminates the moment they pick
``approve`` / ``apply`` / ``skip`` / ``halt`` or acceptance approves. There
is therefore no artificial round ceiling — a human decision sits between
every round.

This module owns only the sequencing + correction-task synthesis. It does
not own pipeline execution: the caller injects ``run_pipeline`` so the CLI
and tests share one driver. Non-interactive transports (CI, MCP, piped)
never call this — they leave the run ``halted`` for an external controller
to resume, which keeps the wire contract unchanged.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.control import (
    extract_followup_session_seeds,
    is_terminal_commit_decision_fix,
)
from pipeline.evidence.verification_receipt import (
    COMMAND_RECEIPTS_DIRNAME,
    command_receipt_passed,
)
from pipeline.project.correction_fixed_point import (
    evaluate_fixed_point,
    render_non_convergence_block,
)

__all__ = [
    "CORRECTION_PROFILE_NAME",
    "CorrectionDecision",
    "compose_correction_context",
    "compose_correction_task",
    "decide_correction_followup",
    "drive_correction_followups",
    "is_correction_fix_halt",
]


#: The internal profile every correction follow-up dispatches (ADR 0085).
#: It opens at ``correction_triage`` and omits plan / validate_plan, so the
#: driver pins it over whatever profile the parent run used.
CORRECTION_PROFILE_NAME = "correction"


# Framing prepended to every correction-round task so the implement /
# repair phases know they are remediating a rejected release rather than
# starting unrelated work.
_TASK_HEADER = (
    "Correction follow-up: final acceptance rejected the previous run. "
    "Continue in the same retained worktree, resolve only the listed blockers, "
    "and do not start unrelated work."
)

_GENERIC_REMEDIATION = (
    "Inspect the correction context artifact and resolve the release blockers "
    "before the change can ship."
)


@dataclass(frozen=True)
class _CorrectionSummary:
    task: str
    context: str


def is_correction_fix_halt(session: Mapping[str, Any] | None) -> bool:
    """True when a finished run halted at the operator's ``fix`` choice."""
    return session is not None and is_terminal_commit_decision_fix(session)


def _final_acceptance_entry(session: Mapping[str, Any]) -> Mapping[str, Any]:
    phases = session.get("phases")
    if isinstance(phases, Mapping):
        entry = phases.get("final_acceptance")
        if isinstance(entry, Mapping):
            return entry
    return {}


def _render_gaps(gaps: Any) -> str:
    """Render structured ``verification_gaps`` into a task checklist.

    Each gap is a ``{risk, missing_evidence, required_check}`` mapping
    (see :class:`pipeline.release_parser.VerificationGap`). The
    ``required_check`` is the actionable remediation; ``missing_evidence``
    is kept as context so the agent knows what evidence to produce.
    """
    if not isinstance(gaps, (list, tuple)):
        return ""
    lines: list[str] = []
    for index, gap in enumerate(gaps, start=1):
        if not isinstance(gap, Mapping):
            continue
        required = str(gap.get("required_check") or "").strip()
        missing = str(gap.get("missing_evidence") or "").strip()
        risk = str(gap.get("risk") or "").strip()
        headline = required or risk
        if not headline:
            continue
        lines.append(f"{index}. {headline}")
        if missing:
            lines.append(f"   Missing evidence: {missing}")
    return "\n".join(lines)


def compose_correction_context(session: Mapping[str, Any]) -> str:
    """Render durable correction context for the next follow-up run.

    This may include the full release critique. It is intentionally separate
    from the child run's ``task`` field so run headers, meta summaries, MCP
    cards, and follow-up lineage remain concise.
    """
    fa = _final_acceptance_entry(session)
    parts: list[str] = ["# Correction Context"]

    summary = str(fa.get("short_summary") or "").strip()
    if summary:
        parts.append(f"## Release Summary\n\n{summary}")

    rendered_gaps = _render_gaps(fa.get("verification_gaps"))
    if rendered_gaps:
        parts.append("## Verification Gaps\n\n" + rendered_gaps)

    critique = str(fa.get("critique") or "").strip()
    if critique:
        parts.append("## Full Final Acceptance Critique\n\n" + critique)

    if len(parts) == 1:
        parts.append(_GENERIC_REMEDIATION)
    return "\n\n".join(parts).strip()


def _compose_correction_summary(
    session: Mapping[str, Any],
    *,
    context_path: Path | None = None,
) -> _CorrectionSummary:
    """Build concise child task plus full context body."""
    fa = _final_acceptance_entry(session)
    parts: list[str] = [_TASK_HEADER]

    summary = str(fa.get("short_summary") or "").strip()
    if summary:
        parts.append(f"Summary: {summary}")

    rendered_gaps = _render_gaps(fa.get("verification_gaps"))
    if rendered_gaps:
        parts.append("Blockers to resolve:\n" + rendered_gaps)
    else:
        parts.append(_GENERIC_REMEDIATION)

    if context_path is not None:
        parts.append(f"Detailed rejection context: {context_path}")

    return _CorrectionSummary(
        task="\n\n".join(parts).strip(),
        context=compose_correction_context(session),
    )


def compose_correction_task(session: Mapping[str, Any]) -> str:
    """Build the next correction round's concise task from the rejection.

    The task is deliberately bounded. Full critique markdown belongs in the
    correction context artifact written by :func:`drive_correction_followups`,
    not in ``meta.task`` or terminal run headers.
    """
    return _compose_correction_summary(session).task


@dataclass(frozen=True)
class CorrectionDecision:
    """Pure decision for one step of the correction loop.

    ``should_continue`` is True when the latest run is still a
    ``commit_decision_fix`` halt — i.e. the operator picked ``fix`` again
    and a fresh correction round is warranted. The loop **stops**
    (``should_continue=False``) the moment that is no longer true (operator
    chose approve / apply / skip / halt, or acceptance approved).

    When continuing, ``task`` and ``context`` carry the derived metadata for
    the round: a concise child ``task`` (with the rejection-context path when
    one is supplied) and the full ``context`` body for the artifact. When
    stopping, both are empty — there is nothing to compose.
    """

    should_continue: bool
    task: str = ""
    context: str = ""


def decide_correction_followup(
    session: Mapping[str, Any] | None,
    *,
    context_path: Path | None = None,
) -> CorrectionDecision:
    """Decide whether to run another correction round and derive its metadata.

    Pure: it neither launches a child run, calls a provider, nor touches the
    filesystem — it only classifies the prior ``session`` and (when a round
    is warranted) composes the concise task plus full context body. The
    driver owns minting the run dir, writing ``correction_context.md``, and
    dispatching the child run.

    ``context_path`` is the artifact path the driver has minted for the
    round; when supplied it is referenced from the concise task so the agent
    can find the full rejection context. It does not affect the
    continue/stop decision.
    """
    if not is_correction_fix_halt(session):
        return CorrectionDecision(should_continue=False)
    assert session is not None  # narrowed by is_correction_fix_halt
    summary = _compose_correction_summary(session, context_path=context_path)
    return CorrectionDecision(
        should_continue=True,
        task=summary.task,
        context=summary.context,
    )


@contextmanager
def _pinned_run_id(run_id: str):
    """Pin ``$ORCHO_RUN_ID`` to ``run_id`` for one follow-up round.

    ``resolve_run_id_and_setup_logging`` ranks ``$ORCHO_RUN_ID`` above the
    minted directory name (bootstrap P2.5 priority chain). Without this, a
    parent run launched with ``--run-id`` or an ambient ``$ORCHO_RUN_ID``
    leaks its stale id into every follow-up's ``session_ts`` while the round
    writes into a freshly minted sibling dir — splitting run identity across
    meta / events / checkpoints / worktree attachment. Pinning keeps
    ``session_ts`` equal to ``output_dir.name`` and restores the prior value
    so the override never escapes the round.
    """
    prev = os.environ.get("ORCHO_RUN_ID")
    os.environ["ORCHO_RUN_ID"] = run_id
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("ORCHO_RUN_ID", None)
        else:
            os.environ["ORCHO_RUN_ID"] = prev


#: Suggested operator actions surfaced when the fixed-point guard fires. The
#: agent itself cannot legally take any of these (expand scope, waive a
#: blocker, change the contract), so the non-converging outcome hands the
#: decision back to a human.
_NON_CONVERGING_SUGGESTED_ACTIONS: tuple[str, ...] = (
    "retry with new instructions",
    "approve/waive",
    "halt",
)


def _read_run_diff(run_dir: Path) -> str | None:
    """Read a round's captured ``diff.patch``; ``None`` when absent/unreadable."""
    try:
        return (run_dir / "diff.patch").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _diff_changed(parent_dir: Path, child_dir: Path) -> bool:
    """True when the child round's diff differs from the parent's.

    Conservative: a missing/unreadable patch on either side is ambiguous, so it
    is reported as *changed* (progress present) — the guard must never fire on
    incomplete evidence.
    """
    parent = _read_run_diff(parent_dir)
    child = _read_run_diff(child_dir)
    if parent is None or child is None:
        return True
    return parent != child


def _stable_json(value: Any) -> str:
    """Stable, order-insensitive serialization for fingerprint sub-fields."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def _receipts_fingerprint(run_dir: Path) -> frozenset[tuple[Any, ...]] | None:
    """Stable fingerprint of a round's command receipts (timestamp-free).

    Keyed on the fields that drive a receipt's authoritative pass/fresh
    classification, not on noise (``duration_s`` / ``stdout_tail`` / ``log_path``):

    * the ``command_receipt_passed`` rollup — exit code AND every assertion
      passing AND empty ``detail`` (the same rollup readiness / the delivery gate
      use), so a receipt that flips failed -> passing fingerprints differently;
    * ``exit_code``, ``assertions``, and ``detail`` individually, so any change
      in the evidence behind that rollup is caught even when the boolean is
      unchanged;
    * the staleness/provenance fields — git ``changed_files_fingerprint`` /
      ``checkout_head`` / ``baseline_head`` and the cross-repo ``dependencies``
      block — so a re-run that refreshes a stale receipt fingerprints differently.

    Returns an empty set when no receipts were written, or ``None`` when the
    directory is unreadable (ambiguous).
    """
    receipts_dir = run_dir / COMMAND_RECEIPTS_DIRNAME
    if not receipts_dir.is_dir():
        return frozenset()
    try:
        out: set[tuple[Any, ...]] = set()
        for path in sorted(receipts_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, Mapping):
                out.add(("", _stable_json(data)))
                continue
            git = data.get("git")
            git = git if isinstance(git, Mapping) else {}
            out.add((
                str(data.get("command")),
                command_receipt_passed(data),
                data.get("exit_code"),
                _stable_json(data.get("assertions")),
                str(data.get("detail") or ""),
                str(git.get("changed_files_fingerprint")),
                str(git.get("checkout_head")),
                str(git.get("baseline_head")),
                _stable_json(data.get("dependencies")),
            ))
        return frozenset(out)
    except (OSError, ValueError, TypeError):
        return None


def _gate_rerun_made_progress(child_session: Mapping[str, Any]) -> bool:
    """True when the child's correction gate-rerun produced fresh passing receipts.

    A ``gate_rerun`` that re-ran stale/missing required receipts and went green
    (``attempted`` + ``required_passed``) is real progress — never a fixed
    point (Stage 1 criterion 5).
    """
    phases = child_session.get("phases")
    if not isinstance(phases, Mapping):
        return False
    triage = phases.get("correction_triage")
    if not isinstance(triage, Mapping):
        return False
    execution = triage.get("gate_rerun_execution")
    if not isinstance(execution, Mapping):
        return False
    return execution.get("attempted") is True and execution.get(
        "required_passed",
    ) is True


def _receipts_changed(
    parent_dir: Path, child_dir: Path, child_session: Mapping[str, Any],
) -> bool:
    """True when the child round's verification receipts moved relative to parent.

    Conservative: fresh passing gate-rerun receipts, a differing receipt
    fingerprint, or an unreadable receipt directory all count as *changed*
    (progress present), so the guard stays suppressed under any ambiguity.
    """
    if _gate_rerun_made_progress(child_session):
        return True
    parent_fp = _receipts_fingerprint(parent_dir)
    child_fp = _receipts_fingerprint(child_dir)
    if parent_fp is None or child_fp is None:
        return True
    return parent_fp != child_fp


def _apply_non_converging_outcome(
    child_session: dict[str, Any],
    *,
    output_dir: Path,
    seed_dir: Path,
    repeated: tuple[str, ...],
    reason: str,
    announce: Callable[[str], None],
) -> None:
    """Re-mark a fixed-point child as a halted non-converging run.

    The child already finalized (its meta.json carries a ``done``/``halted``
    outcome from its own run). This re-stamps the durable outcome: flips it to
    ``halted`` with ``halt_reason='correction_not_converging'``, records the
    durable ``correction_fixed_point`` block, rewrites meta.json, best-effort
    emits a ``correction.fixed_point`` event, and prints the operator block.
    """
    from pipeline.engine import save_session
    from pipeline.run_state.terminal import mark_run_halted

    mark_run_halted(child_session, halt_reason="correction_not_converging")
    child_session["correction_fixed_point"] = {
        "repeated": list(repeated),
        "parent_run_id": seed_dir.name,
        "child_run_id": output_dir.name,
        "suggested_actions": list(_NON_CONVERGING_SUGGESTED_ACTIONS),
        "reason": reason,
    }
    save_session(output_dir, child_session)

    # Best-effort observability; never load-bearing.
    try:
        from core.observability import events

        events.emit(
            "correction.fixed_point",
            run_id=output_dir.name,
            parent_run_id=seed_dir.name,
            repeated=list(repeated),
        )
    except Exception:  # noqa: BLE001 — telemetry must never break finalization
        pass

    announce(render_non_convergence_block(
        repeated=repeated,
        parent_run_id=seed_dir.name,
        child_run_id=output_dir.name,
    ))


def drive_correction_followups(
    *,
    prev_session: Mapping[str, Any],
    prev_output_dir: Path,
    base_task: str | None,
    stable_kwargs: Mapping[str, Any],
    run_pipeline: Callable[..., Mapping[str, Any] | None],
    mint_run_id: Callable[[], str],
    announce: Callable[[str], None],
) -> Mapping[str, Any] | None:
    """Re-run the pipeline as a correction follow-up until it stops asking.

    Loops while the most recent run halted with ``commit_decision_fix``.
    Each round mints a fresh run dir as a sibling of ``prev_output_dir``,
    reuses the parent run as follow-up context (worktree continuity is
    handled by the follow-up machinery in ``run_project_pipeline``), and
    seeds the task from :func:`compose_correction_task`.

    Returns the session of the final round (the one whose status is no
    longer a ``commit_decision_fix`` halt), or ``prev_session`` unchanged
    when no correction was requested.

    Before starting the next round, each child is compared against the
    session that seeded it (ADR 0098): when the child repeats the same
    final-acceptance blocker identities with no relevant diff/receipt
    progress, the loop deterministically stops with a non-converging
    outcome (``halt_reason='correction_not_converging'``) instead of
    spawning another identical round.

    The caller is responsible for the interactive guard — this driver
    assumes it is only invoked on a TTY.
    """
    runs_dir = prev_output_dir.parent
    session: Mapping[str, Any] | None = prev_session
    parent_dir = prev_output_dir
    round_index = 0

    while decide_correction_followup(session).should_continue:
        assert session is not None  # narrowed by the decision above
        # The session + dir that seed THIS round are its fixed-point parent.
        seed_session = session
        seed_dir = parent_dir
        round_index += 1
        seeds = extract_followup_session_seeds(session)
        parent_status = session.get("status")

        run_id = mint_run_id()
        output_dir = runs_dir / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        context_path = output_dir / "correction_context.md"
        # Re-derive the round metadata now that the artifact path is known;
        # the decision body stays pure — the write below is the driver's IO.
        correction = decide_correction_followup(
            session, context_path=context_path,
        )
        context_path.write_text(correction.context + "\n", encoding="utf-8")

        announce(
            f"  ↳ correction follow-up (round {round_index}): re-running with "
            f"profile {CORRECTION_PROFILE_NAME!r} against rejection feedback in "
            f"a fresh run {run_id!r}, reusing the retained worktree.",
        )

        # ADR 0085 (T3): a correction follow-up always dispatches the internal
        # ``correction`` profile (starts at correction_triage, no plan /
        # validate_plan). This driver is invoked exclusively for correction
        # follow-ups (ADR 0070), so it pins the profile here over whatever the
        # parent used — the parent's profile_name never leaks into the child.
        child_kwargs = dict(stable_kwargs)
        child_kwargs["profile_name"] = CORRECTION_PROFILE_NAME

        with _pinned_run_id(run_id):
            session = run_pipeline(
                task=correction.task,
                output_dir=output_dir,
                resume_mode="followup",
                resume_from=None,
                followup_parent_run_id=parent_dir.name,
                followup_parent_run_dir=str(parent_dir),
                followup_parent_status=(
                    parent_status if isinstance(parent_status, str) else None
                ),
                followup_base_task=base_task,
                followup_session_seeds=seeds,
                hypothesis_enabled=False,
                **child_kwargs,
            )

        # Fixed-point guard (ADR 0098): compare the child against the session
        # that seeded this round. Progress facts (diff + receipts) are read here
        # — the IO seam — and the verdict is computed by the pure evaluator.
        verdict = evaluate_fixed_point(
            seed_session,
            session,
            code_changed=_diff_changed(seed_dir, output_dir),
            receipts_changed=_receipts_changed(seed_dir, output_dir, session)
            if isinstance(session, Mapping)
            else True,
        )
        if verdict.is_fixed_point and isinstance(session, dict):
            _apply_non_converging_outcome(
                session,
                output_dir=output_dir,
                seed_dir=seed_dir,
                repeated=verdict.repeated,
                reason=verdict.reason,
                announce=announce,
            )
            # halt_reason is no longer ``commit_decision_fix``, so the loop would
            # stop on its own; the break is the explicit non-converging exit.
            break

        parent_dir = output_dir

    return session
