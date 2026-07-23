"""
pipeline/cross_project/final_acceptance.py — system release gate
(ADR 0025 Phase 3).

The cross runner invokes ``run_cross_final_acceptance`` after the
contract_check block. The gate runs in two paths:

* **Precondition path** — if any upstream signal blocks ship
  (missing/crashed child, missing release verdict, child release
  REJECTED, contract_check REJECTED, parse error upstream), the gate
  synthesises a REJECTED :class:`pipeline.release_parser.ParsedRelease`
  from observable signals. No agent call.
* **Agent path** — preconditions pass → compose the prompt via
  Phase 1's explicit threading
  (``review_focus(output_contract="release")`` →
  ``runtime_review_uncommitted_prompt(output_contract="release")``),
  invoke the cross reviewer, parse via ``parse_release``.

Both paths produce the same :class:`CrossFinalAcceptanceResult`
shape; the ``source`` discriminator (``agent`` / ``precondition`` /
``parse_error``) tells consumers which path produced the verdict.

The cross runner uses the result to:

1. Write a dual-shape singleton dict into
   ``session["phases"]["cross_final_acceptance"]`` (review-shape
   mirror + release fields, matching Phase 1's
   :class:`pipeline.session_adapters.FinalAcceptanceAdapter`).
2. Emit ``cross_final_acceptance.verdict`` on the event spine.
3. Decide the final ``session.status`` — done iff
   ``result.parsed.approved`` AND ``source != "parse_error"``.

All three of those steps happen AFTER this function returns. Status
is written in exactly one place (the cross runner), after the gate's
phase entry is recorded.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.infra import config
from pipeline.release_parser import (
    ContractStatus,
    ParsedRelease,
    ReleaseBlocker,
    ReleaseParseError,
    ReleaseSchemaError,
    VerificationGap,
    parse_release,
)
from pipeline.run_state.cross_parent import ChildState, ReleaseDisposition

if TYPE_CHECKING:
    from pipeline.prompts.turn import PromptTurn

__all__ = [
    "CrossFinalAcceptanceContext",
    "CrossFinalAcceptanceResult",
    "CrossFinalPreconditions",
    "build_context",
    "run_cross_final_acceptance",
]


# Blocker id prefixes are the wire contract for precondition causes —
# consumers (Web / MCP / SDK / log greppers) match on them.
_BLOCKER_MISSING_CHILD = "CFA_MISSING_CHILD"
_BLOCKER_MISSING_RELEASE = "CFA_MISSING_RELEASE"
_BLOCKER_CHILD_REJECTED = "CFA_CHILD_REJECTED"
_BLOCKER_CONTRACT_REJECTED = "CFA_CONTRACT_REJECTED"
_BLOCKER_PARSE_ERROR = "CFA_PARSE_ERROR"
# Skipped-contract gate IDs. When ``contract_check`` is skipped, the
# release gate carries forward ``on_skip``: block → release blocker;
# allow_with_gap → verification gap; allow → no entry. Wire-visible
# discriminators so consumers can distinguish "I skipped" from
# "I rejected".
_BLOCKER_CONTRACT_SKIPPED_BLOCK = "CFA_CONTRACT_SKIPPED_BLOCK"
_GAP_CONTRACT_SKIPPED = "CFA_CONTRACT_SKIPPED_GAP"

_VERDICT_RE = re.compile(
    r"^\*\*(?:Verdict|Вердикт):\*\*\s*(APPROVED|REJECTED)\s*$",
    re.MULTILINE,
)
_SHIP_READY_RE = re.compile(
    r"^\*\*(?:Ship-ready|Готово к релизу):\*\*\s*(yes|no|да|нет|true|false)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SUMMARY_RE = re.compile(
    r"^\*\*(?:Short summary|Кратко):\*\*\s*(.+?)\s*$",
    re.MULTILINE,
)
_BLOCKER_HEADING_RE = re.compile(
    r"^###\s+(\S+)\s+\[(P[0-2])\]\s+(.+?)\s*$",
    re.MULTILINE,
)
_SECTION_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)
_FILE_RE = re.compile(r"^(?:File|Файл):\s*`([^`]+)`\s*$", re.MULTILINE)
_REQUIRED_FIX_RE = re.compile(
    r"^\*\*(?:Required fix|Что исправить):\*\*\s*(.+?)\s*$",
    re.MULTILINE,
)
_WHY_BLOCKS_RE = re.compile(
    r"^\*\*(?:Why this blocks release|Почему это блокирует релиз):\*\*\s*(.+?)\s*$",
    re.MULTILINE,
)
_STATUS_KEYS = {
    "task contract": "task_contract",
    "контракт задачи": "task_contract",
    "interfaces": "interfaces",
    "интерфейсы": "interfaces",
    "persistence": "persistence",
    "хранение": "persistence",
    "tests": "tests",
    "тесты": "tests",
}
_GAP_RE = re.compile(
    r"^-\s+\*\*(?:Risk|Риск):\*\*\s*(?P<risk>.*?)\n"
    r"\s+\*\*(?:Missing evidence|Недостающее подтверждение):\*\*"
    r"\s*(?P<missing>.*?)\n"
    r"\s+\*\*(?:Required check|Нужная проверка):\*\*"
    r"\s*(?P<check>.*?)(?=\n-\s+\*\*(?:Risk|Риск):|\n##\s+|\Z)",
    re.DOTALL | re.MULTILINE,
)


@dataclass(frozen=True)
class CrossFinalPreconditions:
    """Bundled output of the precondition pass.

    Separating ``blockers`` from ``verification_gaps`` lets the runner
    distinguish "skipped contract_check under allow_with_gap" — which is
    *uncertainty*, not a release failure — from the existing
    ``REJECTED`` / ``parse_error`` paths that genuinely block release.
    """
    blockers: tuple[ReleaseBlocker, ...] = field(default_factory=tuple)
    verification_gaps: tuple[VerificationGap, ...] = field(
        default_factory=tuple,
    )


@dataclass(frozen=True)
class CrossFinalAcceptanceContext:
    """Inputs the gate reads from the cross-run session dict.

    Built once by :func:`build_context` after contract_check returns;
    consumed by precondition checks and (on the agent path) by the
    prompt body assembler.
    """
    cross_plan_markdown: str
    aliases: tuple[str, ...]
    child_sessions: Mapping[str, Mapping[str, Any]]
    contract_results: Mapping[str, Mapping[str, Any]]
    common_cwd: str
    output_language: str = "English"
    # Variant 1 — per-alias working-tree paths the reviewer must inspect
    # (the run worktree checkouts, where the uncommitted changes live).
    # Rendered into the focus so the agent reviews the change surface,
    # not the pristine source. Empty ⇒ fall back to plan-embedded paths.
    review_paths: Mapping[str, str] = field(default_factory=dict)
    # Canonical reducer projections are supplied by the runtime boundary.  The
    # session snapshots remain only prompt/audit material, never a second
    # status-classification authority.
    child_states: Mapping[str, ChildState] = field(default_factory=dict)
    parent_blocked: bool = False


@dataclass(frozen=True)
class CrossFinalAcceptanceResult:
    """Outcome of the gate: parsed release verdict, source discriminator,
    raw model output (when applicable), rendered markdown, and
    invocation duration. The runner translates this into the dual-shape
    phase_log entry and the event payload."""
    parsed: ParsedRelease
    source: str            # "agent" | "precondition" | "parse_error"
    raw_output: str
    rendered: str
    duration_s: float
    parse_error: str | None = None
    # Precondition blockers materialised independently of ``parsed`` so
    # the runner can fold their messages into ``failure_reason`` text
    # without re-deriving them from the parsed shape.
    precondition_blockers: tuple[ReleaseBlocker, ...] = field(default_factory=tuple)
    # Reviewer prompt text — populated on the ``agent``/``parse_error``
    # paths so the cross orchestrator can feed prompt+output into
    # ``_capture_invoke_usage`` for token-split estimation on runtimes
    # that only surface ``last_tokens_total`` (e.g. Codex). Empty string
    # on the ``precondition`` path (no model call was made).
    prompt_text: str = ""


def build_context(
    *,
    cross_plan_markdown: str,
    aliases: tuple[str, ...],
    session_phases: Mapping[str, Any],
    common_cwd: str,
    review_paths: Mapping[str, str] | None = None,
    child_states: Mapping[str, ChildState] | None = None,
    parent_blocked: bool = False,
) -> CrossFinalAcceptanceContext:
    """Snapshot the inputs from ``session["phases"]`` after
    contract_check has written its results.

    ``session_phases["projects"]`` and ``session_phases["contract_check"]``
    are the two slots the gate reads from. Both may be partial
    (missing aliases) — preconditions handle the gap. ``review_paths``
    maps each alias to the working-tree path the reviewer must inspect
    (the run worktree checkout); when omitted the focus carries no
    explicit review-target section.
    """
    projects = session_phases.get("projects")
    if not isinstance(projects, Mapping):
        projects = {}
    contract_results = session_phases.get("contract_check")
    if not isinstance(contract_results, Mapping):
        contract_results = {}
    output_language = _configured_output_language()
    return CrossFinalAcceptanceContext(
        cross_plan_markdown=cross_plan_markdown,
        aliases=tuple(aliases),
        child_sessions=dict(projects),
        contract_results=dict(contract_results),
        common_cwd=common_cwd,
        output_language=output_language,
        review_paths=dict(review_paths or {}),
        child_states=dict(child_states or {}),
        parent_blocked=parent_blocked,
    )


def _configured_output_language() -> str:
    cfg = config.AppConfig.load()
    return str(
        getattr(cfg, "task_language", None)
        or getattr(cfg, "plan_language", None)
        or "English"
    )


def _is_russian(language: str | None) -> bool:
    normalized = (language or "").strip().lower()
    return normalized.startswith(("ru", "rus", "russian", "рус"))


def _system_release_title(language: str | None) -> str:
    if _is_russian(language):
        return "Системная проверка релиза"
    return "System release gate"


# ─────────────────────────────────────────────────────────────────────────────
# Precondition collection
# ─────────────────────────────────────────────────────────────────────────────


def _collect_preconditions(
    ctx: CrossFinalAcceptanceContext,
) -> CrossFinalPreconditions:
    """Walk all preconditions in a fixed order.

    Each violation produces either one :class:`ReleaseBlocker` or one
    :class:`VerificationGap` depending on the cause. Empty
    :class:`CrossFinalPreconditions` means preconditions pass and the
    gate proceeds to the agent path.

    Order is fixed for determinism (alias loop + check order); changes
    to this list are wire-visible in ``failure_reason`` strings and in
    evidence release_blockers / verification_gaps, so don't reorder
    lightly.
    """
    blockers: list[ReleaseBlocker] = []
    gaps: list[VerificationGap] = []
    if ctx.parent_blocked:
        # A runtime boundary observed an active, pending, or inconsistent
        # parent.  This is a CFA precondition and must not reach the provider.
        blockers.extend(
            _blocker_missing_child(alias, language=ctx.output_language)
            for alias in ctx.aliases
        )
        return CrossFinalPreconditions(blockers=tuple(blockers))
    for alias in ctx.aliases:
        child = ctx.child_sessions.get(alias)
        cc_entry = ctx.contract_results.get(alias)
        child_state = ctx.child_states.get(alias)
        if child_state is not None:
            if not child_state.contract_evaluable:
                blocker = next(iter(child_state.blockers), None)
                blockers.append(
                    _blocker_missing_child(
                        alias,
                        child=child,
                        child_status=child_state.status,
                        child_reason=blocker.code if blocker else "child_not_evaluable",
                        language=ctx.output_language,
                    )
                )
                continue
            if child_state.release_disposition is ReleaseDisposition.REJECTED:
                fa_entry = _child_final_acceptance(child)
                blockers.append(
                    _blocker_child_rejected(
                        alias,
                        fa_entry or {},
                        language=ctx.output_language,
                    )
                )
                # A rejected child remains contract-evaluable: retain the
                # existing contract-check policy below.
        # Dispatch readiness takes precedence over all compatibility handling.
        # NOT_EVALUABLE means no contract review was possible, so it must not be
        # reinterpreted as REJECTED or policy SKIPPED below.
        if (
            isinstance(cc_entry, Mapping)
            and cc_entry.get("not_evaluable") is True
            and cc_entry.get("source") == "precondition"
            and cc_entry.get("reason") == "child_readiness"
        ):
            blockers.append(
                _blocker_missing_child(
                    alias,
                    child=child,
                    child_status=cc_entry.get("child_status"),
                    child_reason=cc_entry.get("child_reason"),
                    language=ctx.output_language,
                )
            )
            continue
        # Crashed child first — the runner's exception catcher wrote
        # {"status": "failed", "error": ...} for this alias, so the
        # blocker body carries the error hint. Order matters: a missing
        # entry (None) and a failed entry both fall under
        # MISSING_CHILD, but only the failed one has actionable detail
        # to surface.
        if child_state is None and isinstance(child, Mapping) and child.get("status") == "failed":
            blockers.append(
                _blocker_missing_child(
                    alias, child=child, language=ctx.output_language,
                )
            )
            continue
        if child_state is None and not _child_present(child):
            blockers.append(
                _blocker_missing_child(alias, language=ctx.output_language)
            )
            continue
        # From here the child object is expected to be a sub-session
        # dict with a ``phases`` map containing final_acceptance.
        fa_entry = _child_final_acceptance(child)
        if fa_entry is None:
            blockers.append(
                _blocker_missing_release(alias, language=ctx.output_language)
            )
            continue
        fa_parse_error = fa_entry.get("parse_error")
        if fa_parse_error:
            blockers.append(_blocker_parse_error(
                alias,
                source="final_acceptance",
                detail=str(fa_parse_error),
                language=ctx.output_language,
            ))
            continue
        ship_ready = fa_entry.get("ship_ready")
        if not isinstance(ship_ready, bool):
            blockers.append(
                _blocker_missing_release(alias, language=ctx.output_language)
            )
            continue
        if child_state is None and (ship_ready is False or fa_entry.get("verdict") == "REJECTED"):
            blockers.append(
                _blocker_child_rejected(
                    alias, fa_entry, language=ctx.output_language,
                )
            )
            # Don't `continue` — still check contract_check below.
        # Contract check for this alias.
        if isinstance(cc_entry, Mapping):
            cc_parse_error = cc_entry.get("parse_error")
            if cc_parse_error:
                blockers.append(_blocker_parse_error(
                    alias,
                    source="contract_check",
                    detail=str(cc_parse_error),
                    language=ctx.output_language,
                ))
                continue
            if cc_entry.get("skipped") is True:
                # Skipped gate carries forward ``on_skip`` policy
                # ONLY when the skip was operator-driven. A
                # ``source="policy"`` skip (policy_disabled /
                # policy_never) means the profile never asked the
                # gate to run; the ``on_skip`` value on such an
                # entry is artefactual fallback, not a profile-
                # author decision, and must not feed the release
                # verdict. For operator skips: ``block`` → release
                # blocker, ``allow_with_gap`` → verification gap,
                # ``allow`` → no entry. Unknown ``on_skip`` values
                # fall back to ``block`` so a typo never silently
                # green-lights a release.
                if cc_entry.get("source") != "operator":
                    continue
                on_skip = cc_entry.get("on_skip", "block")
                if on_skip == "allow":
                    pass
                elif on_skip == "allow_with_gap":
                    gaps.append(_gap_contract_skipped(alias, cc_entry))
                else:
                    blockers.append(
                        _blocker_contract_skipped(
                            alias, cc_entry, language=ctx.output_language,
                        ),
                    )
                continue
            if cc_entry.get("verdict") == "REJECTED":
                blockers.append(
                    _blocker_contract_rejected(
                        alias, cc_entry, language=ctx.output_language,
                    )
                )
    return CrossFinalPreconditions(
        blockers=tuple(blockers),
        verification_gaps=tuple(gaps),
    )


def _child_present(child: Any) -> bool:
    return isinstance(child, Mapping) and child.get("status") != "failed"


def _child_final_acceptance(child: Any) -> Mapping[str, Any] | None:
    if not isinstance(child, Mapping):
        return None
    phases = child.get("phases")
    if not isinstance(phases, Mapping):
        return None
    entry = phases.get("final_acceptance")
    if not isinstance(entry, Mapping):
        return None
    # The Phase 1 dual-shape mirror requires these release fields;
    # absence means the child ran an older path or skipped the gate.
    for required in ("ship_ready", "release_blockers", "verification_gaps",
                     "contract_status"):
        if required not in entry:
            return None
    return entry


# ── Blocker builders ─────────────────────────────────────────────────────────


def _blocker(
    *,
    id: str,
    title: str,
    body: str,
    required_fix: str,
    why: str,
    severity: str = "P0",
    file: str | None = None,
    line: int | None = None,
) -> ReleaseBlocker:
    return ReleaseBlocker(
        id=id,
        severity=severity,
        title=title,
        body=body,
        required_fix=required_fix,
        why_blocks_release=why,
        file=file,
        line=line,
    )


def _blocker_missing_child(
    alias: str,
    *,
    child: Any = None,
    child_status: Any = None,
    child_reason: Any = None,
    language: str | None = None,
) -> ReleaseBlocker:
    status = child_status if isinstance(child_status, str) else None
    reason = child_reason if isinstance(child_reason, str) else None
    if isinstance(child, Mapping):
        status = status or (
            child.get("status") if isinstance(child.get("status"), str) else None
        )
        reason = reason or str(child.get("halt_reason") or child.get("error") or "")
    detail_hint = ""
    if status:
        detail_hint += f" Status: {status}."
    if reason:
        detail_hint += f" Reason: {reason[:200]}."
    if _is_russian(language):
        return _blocker(
            id=f"{_BLOCKER_MISSING_CHILD}_{alias}",
            title=f"Отсутствует или упал sub-pipeline для [{alias}]",
            body=(
                f"Cross-runner не создал завершенную sub-session для "
                f"alias {alias!r}.{detail_hint}"
            ),
            required_fix=(
                f"Разобрать, почему sub-pipeline [{alias}] не завершился "
                "(crash, неверная конфигурация или отсутствующий project), "
                "исправить причину и перезапустить cross pipeline."
            ),
            why=(
                "Согласованное изменение нельзя выпускать без успешного "
                "sub-pipeline для каждого alias из cross-plan."
            ),
        )
    return _blocker(
        id=f"{_BLOCKER_MISSING_CHILD}_{alias}",
        title=f"Missing or failed sub-pipeline for [{alias}]",
        body=(
            f"The cross runner did not produce a complete sub-session "
            f"for alias {alias!r}.{detail_hint}"
        ),
        required_fix=(
            f"Investigate why the [{alias}] sub-pipeline did not "
            f"complete (crash, mis-config, or missing project), fix "
            f"the cause, and re-run the cross pipeline."
        ),
        why=(
            "The coordinated change cannot ship without a successful "
            "sub-pipeline for every alias in the cross plan."
        ),
    )


def _blocker_missing_release(
    alias: str, *, language: str | None = None,
) -> ReleaseBlocker:
    if _is_russian(language):
        return _blocker(
            id=f"{_BLOCKER_MISSING_RELEASE}_{alias}",
            title=f"Нет per-project release verdict для [{alias}]",
            body=(
                f"Sub-session для alias {alias!r} не создала "
                "final_acceptance release verdict: release-shape "
                "surface (ship_ready / release_blockers / "
                "verification_gaps / contract_status) отсутствует."
            ),
            required_fix=(
                f"Запустить sub-pipeline [{alias}] с профилем, где есть "
                "final_acceptance, чтобы system release gate получил "
                "per-project release signal."
            ),
            why=(
                "System release gate не может принять решение о "
                "ship-readiness согласованного изменения, если у одного "
                "из alias нет per-project release verdict."
            ),
        )
    return _blocker(
        id=f"{_BLOCKER_MISSING_RELEASE}_{alias}",
        title=f"Missing per-project release verdict for [{alias}]",
        body=(
            f"Sub-session for alias {alias!r} did not produce a "
            f"final_acceptance release verdict — the release-shape "
            f"surface (ship_ready / release_blockers / "
            f"verification_gaps / contract_status) is absent."
        ),
        required_fix=(
            f"Run the [{alias}] sub-pipeline with a profile that "
            f"includes a final_acceptance phase, so the system release "
            f"gate has a per-project release signal to compose."
        ),
        why=(
            "The system release gate cannot decide ship-readiness for "
            "a coordinated change when any participating alias has no "
            "per-project release verdict to fold in."
        ),
    )


def _blocker_child_rejected(
    alias: str, fa_entry: Mapping[str, Any], *, language: str | None = None,
) -> ReleaseBlocker:
    summary = str(fa_entry.get("short_summary") or "release rejected")
    # Carry through the first underlying release blocker's file/line
    # when available, so the system-level blocker points at a concrete
    # location instead of just naming the alias.
    file_hint: str | None = None
    line_hint: int | None = None
    child_blockers = fa_entry.get("release_blockers")
    if isinstance(child_blockers, list) and child_blockers:
        first = child_blockers[0]
        if isinstance(first, Mapping):
            f = first.get("file")
            if isinstance(f, str):
                file_hint = f
            line_val = first.get("line")
            if isinstance(line_val, int):
                line_hint = line_val
    if _is_russian(language):
        return _blocker(
            id=f"{_BLOCKER_CHILD_REJECTED}_{alias}",
            severity="P1",
            title=f"Проект [{alias}] не готов к релизу",
            body=(
                f"Release gate sub-pipeline [{alias}] отклонил изменение: "
                f"{summary}"
            ),
            required_fix=(
                f"Исправить блокеры релиза в final_acceptance entry для "
                f"[{alias}], затем перезапустить cross pipeline."
            ),
            why=(
                "Согласованное multi-repo изменение нельзя выпускать, пока "
                "release gate любого участвующего alias отклоняет его."
            ),
            file=file_hint,
            line=line_hint,
        )
    return _blocker(
        id=f"{_BLOCKER_CHILD_REJECTED}_{alias}",
        severity="P1",
        title=f"Project [{alias}] is not ship-ready",
        body=(
            f"The [{alias}] sub-pipeline's release gate rejected the "
            f"change: {summary}"
        ),
        required_fix=(
            f"Address the [{alias}] release blockers in that project's "
            f"final_acceptance entry, then re-run the cross pipeline."
        ),
        why=(
            "The coordinated multi-repo change cannot ship while any "
            "participating alias's own release gate rejects."
        ),
        file=file_hint,
        line=line_hint,
    )


def _blocker_contract_rejected(
    alias: str, cc_entry: Mapping[str, Any], *, language: str | None = None,
) -> ReleaseBlocker:
    summary = str(cc_entry.get("short_summary") or "contract mismatch")
    if _is_russian(language):
        return _blocker(
            id=f"{_BLOCKER_CONTRACT_REJECTED}_{alias}",
            severity="P1",
            title=f"Cross-project contract check отклонил [{alias}]",
            body=(
                f"Cross contract check отклонил diff [{alias}]: {summary}"
            ),
            required_fix=(
                f"Исправить cross-project contract mismatch в [{alias}] "
                "(producer/consumer interface drift, schema regression "
                "или несинхронизированные constants), затем перезапустить."
            ),
            why=(
                f"В согласованном изменении сломан interface между "
                f"[{alias}] и sibling repo; выпуск сломает callers."
            ),
        )
    return _blocker(
        id=f"{_BLOCKER_CONTRACT_REJECTED}_{alias}",
        severity="P1",
        title=f"Cross-project contract check rejected for [{alias}]",
        body=(
            f"The cross contract check rejected [{alias}]'s diff: "
            f"{summary}"
        ),
        required_fix=(
            f"Resolve the cross-project contract mismatch in [{alias}] "
            f"(producer/consumer interface drift, schema regression, "
            f"or unsynchronised constants), then re-run."
        ),
        why=(
            f"The coordinated change has a broken interface between "
            f"[{alias}] and a sibling repo; shipping would break "
            f"production callers."
        ),
    )


def _blocker_contract_skipped(
    alias: str, cc_entry: Mapping[str, Any], *, language: str | None = None,
) -> ReleaseBlocker:
    """Block release when ``contract_check`` was skipped under
    ``on_skip=block``. The cross gate refuses to ship without the
    upstream interface verdict the profile required."""
    source = str(cc_entry.get("source") or "unknown")
    reason = str(cc_entry.get("skip_reason") or "unspecified")
    feedback = str(cc_entry.get("operator_feedback") or "")
    body = (
        f"The cross contract check for [{alias}] was skipped "
        f"({source}/{reason}). The release gate is configured to block "
        f"ship under on_skip=block, so the missing verdict cannot be "
        f"folded in."
    )
    if feedback:
        body += (
            f" Operator feedback: {feedback[:200]}"
            if not _is_russian(language)
            else f" Feedback оператора: {feedback[:200]}"
        )
    if _is_russian(language):
        return _blocker(
            id=f"{_BLOCKER_CONTRACT_SKIPPED_BLOCK}_{alias}",
            severity="P1",
            title=f"Cross-project contract check пропущен для [{alias}]",
            body=(
                f"Cross contract check для [{alias}] был пропущен "
                f"({source}/{reason}). Release gate настроен блокировать "
                "ship при on_skip=block, поэтому отсутствующий verdict "
                "нельзя учесть как безопасный."
            ),
            required_fix=(
                f"Перезапустить contract check для [{alias}] "
                "(ослабить on_skip до allow_with_gap / allow в профиле "
                "или явно запустить gate через --decision "
                "contract_check=run), чтобы release gate получил verdict."
            ),
            why=(
                "Release gate не может сертифицировать согласованное "
                "изменение, когда upstream interface signal пропущен "
                "под blocking policy."
            ),
        )
    return _blocker(
        id=f"{_BLOCKER_CONTRACT_SKIPPED_BLOCK}_{alias}",
        severity="P1",
        title=f"Cross-project contract check skipped for [{alias}]",
        body=body,
        required_fix=(
            f"Re-run [{alias}]'s contract check (relax on_skip to "
            f"allow_with_gap / allow on the profile, or run the gate "
            f"explicitly with --decision contract_check=run) so the "
            f"release gate has the verdict it needs to fold in."
        ),
        why=(
            "The release gate cannot certify a coordinated change when "
            "the upstream interface signal it depends on was skipped "
            "under a blocking policy."
        ),
    )


def _gap_contract_skipped(
    alias: str, cc_entry: Mapping[str, Any],
) -> VerificationGap:
    """Record a verification gap when ``contract_check`` was skipped
    under ``on_skip=allow_with_gap``. The release proceeds but carries
    explicit uncertainty about the cross-project interface."""
    source = str(cc_entry.get("source") or "unknown")
    reason = str(cc_entry.get("skip_reason") or "unspecified")
    return VerificationGap(
        risk=(
            f"Cross-project contract for [{alias}] was not verified "
            f"({source}/{reason})."
        ),
        missing_evidence=(
            f"contract_check verdict for [{alias}] was skipped before "
            f"the cross-run produced it."
        ),
        required_check=(
            f"Run the cross contract check for [{alias}] in a follow-up "
            f"pass (or rerun the cross pipeline without --decision "
            f"contract_check=skip) to confirm the interface holds."
        ),
    )


def _blocker_parse_error(
    alias: str, *, source: str, detail: str, language: str | None = None,
) -> ReleaseBlocker:
    if _is_russian(language):
        return _blocker(
            id=f"{_BLOCKER_PARSE_ERROR}_{alias}",
            title=f"Parse error в upstream gate для [{alias}] ({source})",
            body=(
                f"Reviewer {source} для alias {alias!r} вернул output, "
                "который не распарсился как JSON contract: "
                f"{detail[:240]}"
            ),
            required_fix=(
                f"Перезапустить step [{alias}] {source} с reviewer, "
                "который корректно эмитит JSON contract."
            ),
            why=(
                "Parse error в upstream reviewer означает, что system gate "
                "не может доверять этому сигналу; ship с unparseable verdict "
                "является protocol break."
            ),
        )
    return _blocker(
        id=f"{_BLOCKER_PARSE_ERROR}_{alias}",
        title=f"Parse error in upstream gate for [{alias}] ({source})",
        body=(
            f"The {source} reviewer for alias {alias!r} produced "
            f"output that did not parse as JSON contract: "
            f"{detail[:240]}"
        ),
        required_fix=(
            f"Re-run the [{alias}] {source} step with a reviewer that "
            f"emits the JSON contract correctly."
        ),
        why=(
            "A parse error in an upstream reviewer means the system "
            "gate cannot trust that signal; shipping under an "
            "unparseable verdict is a protocol break."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Precondition synthesis (no agent call)
# ─────────────────────────────────────────────────────────────────────────────


def _synthesize_rejected(
    blockers: tuple[ReleaseBlocker, ...] | list[ReleaseBlocker],
    ctx: CrossFinalAcceptanceContext,
    *,
    verification_gaps: tuple[VerificationGap, ...] = (),
) -> ParsedRelease:
    """Build a REJECTED :class:`ParsedRelease` from precondition
    blockers. Contract_status fields are filled from observable
    upstream signals so consumers (evidence release_summary, Web,
    MCP) see a coherent system-level diagnostic without a model
    invocation.
    """
    # Derive contract_status from the kinds of blockers present.
    has_contract_reject = any(
        b.id.startswith(_BLOCKER_CONTRACT_REJECTED) for b in blockers
    )
    has_parse_error = any(
        b.id.startswith(_BLOCKER_PARSE_ERROR) for b in blockers
    )
    has_child_reject = any(
        b.id.startswith(_BLOCKER_CHILD_REJECTED) for b in blockers
    )
    has_missing = any(
        b.id.startswith(_BLOCKER_MISSING_CHILD)
        or b.id.startswith(_BLOCKER_MISSING_RELEASE)
        for b in blockers
    )

    if has_parse_error:
        task_contract = "unclear"
    elif has_missing or has_child_reject:
        task_contract = "incomplete"
    else:
        task_contract = "satisfied"

    interfaces = "broken" if has_contract_reject else "not_applicable"
    # When children rejected, persistence / tests may be implicated;
    # without a model invocation we can't know, so mark them
    # not_applicable rather than fabricate a state.
    persistence = "not_applicable"
    tests = "weak" if (has_child_reject or has_contract_reject) else "missing"

    contract_status = ContractStatus(
        task_contract=task_contract,
        interfaces=interfaces,
        persistence=persistence,
        tests=tests,
    )
    short_summary = _format_short_summary(blockers, ctx)
    return ParsedRelease(
        verdict="REJECTED",
        ship_ready=False,
        short_summary=short_summary,
        release_blockers=tuple(blockers),
        verification_gaps=tuple(verification_gaps),
        contract_status=contract_status,
        source="json",
    )


def _synthesize_approved_with_gaps(
    verification_gaps: tuple[VerificationGap, ...],
    ctx: CrossFinalAcceptanceContext,
) -> ParsedRelease:
    """Approve the release on the precondition path while carrying
    forward upstream verification gaps.

    Used when ``contract_check`` was skipped under ``allow_with_gap`` —
    no blockers exist, but the gate must record that the cross-project
    interface verdict is missing rather than synthesise a clean
    APPROVED with no audit trail."""
    n = len(verification_gaps)
    if _is_russian(ctx.output_language):
        short_summary = (
            f"Системный релиз одобрен с {n} verification gap, "
            "унаследованным из пропущенного contract_check, "
            f"по {len(ctx.aliases)} alias."
        )
    else:
        short_summary = (
            f"System release approved with {n} verification gap"
            f"{'s' if n != 1 else ''} carried forward from skipped "
            f"contract_check across {len(ctx.aliases)} alias"
            f"{'es' if len(ctx.aliases) != 1 else ''}."
        )
    if len(short_summary) > 280:
        short_summary = short_summary[:277] + "..."
    return ParsedRelease(
        verdict="APPROVED",
        ship_ready=True,
        short_summary=short_summary,
        release_blockers=(),
        verification_gaps=tuple(verification_gaps),
        contract_status=ContractStatus(
            task_contract="satisfied",
            interfaces="not_applicable",
            persistence="not_applicable",
            tests="not_applicable",
        ),
        source="json",
    )


def _format_short_summary(
    blockers: tuple[ReleaseBlocker, ...] | list[ReleaseBlocker],
    ctx: CrossFinalAcceptanceContext,
) -> str:
    if not blockers:
        # Should never happen — precondition synth is only called with
        # at least one blocker. Defensive default keeps the schema
        # invariant (short_summary non-empty).
        if _is_russian(ctx.output_language):
            return "System release gate отклонил релиз без деталей blocker."
        return "System release gate rejected (no blocker details)."
    n = len(blockers)
    first = blockers[0]
    if _is_russian(ctx.output_language):
        summary = (
            f"Системный релиз заблокирован: {n} нарушение предусловий "
            f"по {len(ctx.aliases)} alias. Первый блокер: {first.title}"
        )
    else:
        summary = (
            f"System release blocked: {n} precondition violation"
            f"{'s' if n != 1 else ''} across {len(ctx.aliases)} alias"
            f"{'es' if len(ctx.aliases) != 1 else ''}. First: {first.title}"
        )
    # Schema caps short_summary at 280 chars; trim aggressively if needed.
    if len(summary) > 280:
        summary = summary[:277] + "..."
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Agent path
# ─────────────────────────────────────────────────────────────────────────────


def _build_agent_focus_task(ctx: CrossFinalAcceptanceContext) -> str:
    """Assemble the focus body for the cross-level release reviewer.

    Carries the cross plan, per-project release verdicts, and
    contract_check verdicts in deterministic order so the agent reads
    the same context the preconditions inspected. The Phase 1 prompt
    threading (review_focus + runtime_review_uncommitted_prompt with
    output_contract="release") attaches the release_json contract to
    this body; no machine contract text appears in this string.
    """
    lines: list[str] = []
    lines.append("# System release acceptance context")
    lines.append("")
    if ctx.review_paths:
        lines.append("## Review targets (inspect these working trees)")
        lines.append("")
        lines.append(
            "The coordinated change lives UNCOMMITTED in the per-alias "
            "working trees below — review these paths, not the original "
            "project sources. Run git status/diff in each:"
        )
        lines.append("")
        for alias in ctx.aliases:
            path = ctx.review_paths.get(alias)
            if path:
                lines.append(f"- **[{alias}]** `{path}`")
        lines.append("")
    lines.append("## Approved cross plan")
    lines.append("")
    lines.append(ctx.cross_plan_markdown.rstrip() or "(empty)")
    lines.append("")
    lines.append("## Per-project release verdicts")
    lines.append("")
    for alias in ctx.aliases:
        child = ctx.child_sessions.get(alias)
        fa = _child_final_acceptance(child) if child else None
        if fa is None:
            lines.append(f"### [{alias}] — missing release verdict")
            lines.append("")
            continue
        verdict = str(fa.get("verdict") or "?")
        ship_ready = fa.get("ship_ready")
        summary = str(fa.get("short_summary") or "")
        lines.append(f"### [{alias}] — {verdict}")
        lines.append(f"- ship_ready: {ship_ready}")
        if summary:
            lines.append(f"- short_summary: {summary}")
        cs = fa.get("contract_status")
        if isinstance(cs, Mapping):
            cs_render = ", ".join(f"{k}={cs[k]}" for k in sorted(cs.keys()))
            lines.append(f"- contract_status: {cs_render}")
        lines.append("")
    lines.append("## Contract check verdicts")
    lines.append("")
    for alias in ctx.aliases:
        cc = ctx.contract_results.get(alias)
        if not isinstance(cc, Mapping):
            lines.append(f"### [{alias}] — no contract check entry")
            lines.append("")
            continue
        verdict = str(cc.get("verdict") or "?")
        summary = str(cc.get("short_summary") or "")
        lines.append(f"### [{alias}] — {verdict}")
        if summary:
            lines.append(f"- short_summary: {summary}")
        lines.append("")
    lines.append("## Decision")
    lines.append("")
    lines.append(
        "Decide whether the coordinated change ships as one system. "
        "See the role + task prompts above for what to check."
    )
    return "\n".join(lines).rstrip() + "\n"


def _agent_prompt(ctx: CrossFinalAcceptanceContext) -> PromptTurn:
    """Compose the final prompt through Phase 1's threading layer.

    Strictly:
      review_focus(prompt_spec=PromptSpec(release_manager,
        cross_final_acceptance, detailed), output_contract="release")
      → runtime_review_uncommitted_prompt(..., output_contract="release")

    The wrapper's strip-tail discards any embedded contract block from
    the focus and re-attaches release_json based on the output_contract
    kwarg (Phase 1 invariant — locked by
    ``test_wrapper_strip_tail_then_reattaches_release``).
    """
    from pipeline.prompts.builders import runtime_review_uncommitted_prompt
    from pipeline.prompts.composer import PromptSpec, render_composed_prompt

    task_body = _build_agent_focus_task(ctx)
    # Render release_manager + cross_final_acceptance + detailed
    # directly via ``render_composed_prompt``. We cannot route through
    # ``review_focus`` because that builder truncates its ``task``
    # input to 300 chars (see builders.py: ``task[:300]``) — fine for
    # single-paragraph review focuses, fatal for the multi-section
    # cross context that includes the cross plan + per-project release
    # verdicts + contract check results. Composing the role/task/format
    # directly preserves the full context body.
    #
    # ADR 0028 / M10.5 Step 2: role/task/format files no longer
    # reference $task / $extra_checks; the cross context arrives via
    # ``task_body`` prepended to the focus below.
    composed = render_composed_prompt(
        PromptSpec(
            role="release_manager",
            task="cross_final_acceptance",
            format="detailed",
        ),
        project_dir=(ctx.common_cwd or None),
        variables={},
    )
    focus = f"{task_body}\n\n{composed}"
    _turn = runtime_review_uncommitted_prompt(
        focus,
        project_dir=ctx.common_cwd or "",
        change_handoff=None,
        output_contract="release",
    )
    return _turn


# ─────────────────────────────────────────────────────────────────────────────
# Agent output parsing
# ─────────────────────────────────────────────────────────────────────────────


def _parse_cross_agent_release(raw: str) -> ParsedRelease:
    """Parse the cross-final agent verdict.

    The protected prompt still asks for release JSON. In practice, cross
    system gates can receive the deterministic markdown release rendering
    back from a reviewer session; accepting that rendering here prevents
    a good REJECTED/APPROVED verdict from being downgraded to a protocol
    parse failure while keeping the relaxed path local to cross mode.
    """
    try:
        return parse_release(raw)
    except (ReleaseSchemaError, ReleaseParseError) as original:
        if not _looks_like_release_markdown(raw):
            raise
        try:
            return _parse_release_markdown(raw)
        except (ReleaseSchemaError, ReleaseParseError) as markdown_error:
            raise ReleaseParseError(
                f"{original}; markdown fallback failed: {markdown_error}"
            ) from markdown_error


def _looks_like_release_markdown(raw: str) -> bool:
    return bool(_VERDICT_RE.search(raw or ""))


def _parse_release_markdown(raw: str) -> ParsedRelease:
    """Parse ``render_release_markdown``-style output.

    This intentionally recognises only the stable English/Russian
    renderer labels. It is not a broad natural-language parser.
    """
    text = raw or ""
    verdict = _required_match(_VERDICT_RE, text, "verdict").group(1)
    ship_ready_text = _required_match(
        _SHIP_READY_RE, text, "ship_ready",
    ).group(1)
    summary = _strip_markdown_emphasis(
        _required_match(_SUMMARY_RE, text, "short_summary").group(1)
    )
    blockers = _parse_markdown_blockers(text)
    gaps = _parse_markdown_gaps(text)
    contract_status = _parse_markdown_contract_status(text)

    payload: dict[str, Any] = {
        "verdict": verdict,
        "ship_ready": ship_ready_text.strip().lower() in {"yes", "да", "true"},
        "short_summary": summary,
        "release_blockers": [b.to_dict() for b in blockers],
        "verification_gaps": [g.to_dict() for g in gaps],
        "contract_status": contract_status.to_dict(),
    }
    from core.contracts.release_schema import validate_release_dict
    validate_release_dict(payload)
    return ParsedRelease(
        verdict=str(payload["verdict"]),
        ship_ready=bool(payload["ship_ready"]),
        short_summary=str(payload["short_summary"]),
        release_blockers=blockers,
        verification_gaps=gaps,
        contract_status=contract_status,
        source="markdown",
    )


def _required_match(pattern: re.Pattern[str], text: str, field: str) -> re.Match[str]:
    match = pattern.search(text)
    if match is None:
        raise ReleaseParseError(f"release markdown missing {field}")
    return match


def _strip_markdown_emphasis(value: str) -> str:
    out = value.strip()
    while out.startswith("**") and out.endswith("**") and len(out) >= 4:
        out = out[2:-2].strip()
    return out


def _parse_markdown_blockers(text: str) -> tuple[ReleaseBlocker, ...]:
    matches = list(_BLOCKER_HEADING_RE.finditer(text))
    blockers: list[ReleaseBlocker] = []
    for index, match in enumerate(matches):
        chunk_start = match.end()
        next_blocker = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )
        next_section = _SECTION_HEADING_RE.search(text, chunk_start)
        chunk_end = (
            min(next_blocker, next_section.start())
            if next_section is not None
            else next_blocker
        )
        chunk = text[chunk_start:chunk_end]
        required_fix_match = _required_match(
            _REQUIRED_FIX_RE, chunk, f"release_blockers[{index}].required_fix",
        )
        why_match = _required_match(
            _WHY_BLOCKS_RE, chunk, f"release_blockers[{index}].why_blocks_release",
        )
        body_region = chunk[:required_fix_match.start()]
        file_path, line = _parse_markdown_location(body_region)
        body = _FILE_RE.sub("", body_region).strip()
        if not body:
            raise ReleaseParseError(f"release_blockers[{index}].body is empty")
        blockers.append(
            ReleaseBlocker(
                id=match.group(1),
                severity=match.group(2),
                title=match.group(3).strip(),
                body=body,
                required_fix=required_fix_match.group(1).strip(),
                why_blocks_release=why_match.group(1).strip(),
                file=file_path,
                line=line,
            )
        )
    return tuple(blockers)


def _parse_markdown_location(chunk: str) -> tuple[str | None, int | None]:
    file_match = _FILE_RE.search(chunk)
    if file_match is None:
        return None, None
    location = file_match.group(1).strip()
    if ":" not in location:
        return location, None
    path, maybe_line = location.rsplit(":", 1)
    if maybe_line.isdigit():
        return path, int(maybe_line)
    return location, None


def _parse_markdown_gaps(text: str) -> tuple[VerificationGap, ...]:
    section = _markdown_section(
        text, ("Verification gaps", "Пробелы в проверке"),
    )
    if not section:
        return ()
    gaps: list[VerificationGap] = []
    for match in _GAP_RE.finditer(section):
        gaps.append(
            VerificationGap(
                risk=match.group("risk").strip(),
                missing_evidence=match.group("missing").strip(),
                required_check=match.group("check").strip(),
            )
        )
    if not gaps and section.strip():
        raise ReleaseParseError("release markdown verification gaps malformed")
    return tuple(gaps)


def _parse_markdown_contract_status(text: str) -> ContractStatus:
    section = _markdown_section(text, ("Contract status", "Статус контракта"))
    if not section:
        raise ReleaseParseError("release markdown missing contract_status")
    values: dict[str, str] = {}
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        label, value = line[2:].split(":", 1)
        key = _STATUS_KEYS.get(label.strip().lower())
        if key is not None:
            values[key] = value.strip()
    missing = [
        key for key in ("task_contract", "interfaces", "persistence", "tests")
        if key not in values
    ]
    if missing:
        raise ReleaseParseError(
            f"release markdown contract_status missing keys: {missing}"
        )
    return ContractStatus(
        task_contract=values["task_contract"],
        interfaces=values["interfaces"],
        persistence=values["persistence"],
        tests=values["tests"],
    )


def _markdown_section(text: str, titles: tuple[str, ...]) -> str:
    for title in titles:
        pattern = re.compile(rf"^##\s+{re.escape(title)}\s*$", re.MULTILINE)
        match = pattern.search(text)
        if match is None:
            continue
        next_section = _SECTION_HEADING_RE.search(text, match.end())
        end = next_section.start() if next_section is not None else len(text)
        return text[match.end():end].strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────────────────────────────


def run_cross_final_acceptance(
    ctx: CrossFinalAcceptanceContext,
    *,
    codex: Any,
    dry_run: bool,
) -> CrossFinalAcceptanceResult:
    """Run the gate. Returns :class:`CrossFinalAcceptanceResult` with
    a ``source`` discriminator (``agent`` / ``precondition`` /
    ``parse_error``).

    The caller (cross orchestrator) is responsible for writing the
    dual-shape phase_log entry, emitting the verdict event, and
    setting ``session.status`` — this function does not touch
    session state.

    When ``dry_run`` is True and preconditions pass, the gate emits an
    APPROVED release-shape synth payload and routes it through
    ``parse_release`` so the same parser contract holds on the dry-
    run path (mirroring Phase 1's adapter behaviour).
    """
    from pipeline.release_markdown import render_release_markdown

    pre = _collect_preconditions(ctx)
    if pre.blockers:
        parsed = _synthesize_rejected(
            pre.blockers, ctx,
            verification_gaps=pre.verification_gaps,
        )
        rendered = render_release_markdown(
            parsed,
            title=_system_release_title(ctx.output_language),
            language=ctx.output_language,
        )
        return CrossFinalAcceptanceResult(
            parsed=parsed,
            source="precondition",
            raw_output="",
            rendered=rendered,
            duration_s=0.0,
            precondition_blockers=pre.blockers,
        )

    if pre.verification_gaps:
        # No blockers, but the upstream contract_check was skipped under
        # ``allow_with_gap``. Approve the release with the gaps recorded
        # — the system gate signals "ship is allowed but uncertainty is
        # recorded" rather than failing or pretending the contract gate
        # produced a clean verdict.
        parsed = _synthesize_approved_with_gaps(pre.verification_gaps, ctx)
        rendered = render_release_markdown(
            parsed,
            title=_system_release_title(ctx.output_language),
            language=ctx.output_language,
        )
        return CrossFinalAcceptanceResult(
            parsed=parsed,
            source="precondition",
            raw_output="",
            rendered=rendered,
            duration_s=0.0,
            precondition_blockers=(),
        )

    if dry_run:
        raw = _dry_run_approved_release_json()
        parsed = parse_release(raw)
        rendered = render_release_markdown(
            parsed,
            title=_system_release_title(ctx.output_language),
            language=ctx.output_language,
        )
        return CrossFinalAcceptanceResult(
            parsed=parsed,
            source="agent",
            raw_output=raw,
            rendered=rendered,
            duration_s=0.0,
        )

    prompt_turn = _agent_prompt(ctx)
    t0 = time.time()
    from core.io.stdout_render import defer_assistant_json
    from core.observability.prompt_trace import set_last_prompt_turn

    set_last_prompt_turn(prompt_turn)
    with defer_assistant_json():
        raw = codex.invoke(prompt_turn.text, ctx.common_cwd)
    duration_s = time.time() - t0
    try:
        parsed = _parse_cross_agent_release(raw)
    except (ReleaseSchemaError, ReleaseParseError) as e:
        # Parse error path: synthesise a REJECTED ParsedRelease with a
        # real CFA_PARSE_ERROR blocker so the release-shape entry
        # validates against ``validate_release_dict`` (which requires
        # REJECTED → at least one blocker or gap), and surface the
        # parse error on the result so the runner can mark
        # status=failed.
        if _is_russian(ctx.output_language):
            parse_blocker = ReleaseBlocker(
                id=f"{_BLOCKER_PARSE_ERROR}_cross_final_acceptance",
                severity="P1",
                title="Output reviewer system release gate не распарсился",
                body=(
                    "Reviewer cross_final_acceptance вернул output, который "
                    "не удалось распарсить по release contract: "
                    f"{str(e)[:400]}"
                ),
                required_fix=(
                    "Проверить raw_output, исправить prompt или поведение "
                    "reviewer и перезапустить cross-run, чтобы gate выдал "
                    "parseable release verdict."
                ),
                why_blocks_release=(
                    "Parse error в system release gate означает, что нет "
                    "доверенного verdict о готовности coordinated change; "
                    "ship с unparseable gate output является protocol break."
                ),
            )
            short_summary = (
                f"Parse error в system release gate: {str(e)[:200]}"
            )
        else:
            parse_blocker = ReleaseBlocker(
                id=f"{_BLOCKER_PARSE_ERROR}_cross_final_acceptance",
                severity="P1",
                title="System release gate reviewer output failed to parse",
                body=(
                    "The cross_final_acceptance reviewer returned output that "
                    "could not be parsed against the release contract: "
                    f"{str(e)[:400]}"
                ),
                required_fix=(
                    "Inspect raw_output, fix the prompt or reviewer behavior, "
                    "and rerun the cross run so the gate produces a parseable "
                    "release verdict."
                ),
                why_blocks_release=(
                    "A parse error in the system release gate means there is "
                    "no trusted verdict on whether the coordinated change "
                    "ships; shipping under an unparseable gate output is a "
                    "protocol break."
                ),
            )
            short_summary = (
                f"System release gate parse error: {str(e)[:200]}"
            )
        stub = ParsedRelease(
            verdict="REJECTED",
            ship_ready=False,
            short_summary=short_summary,
            release_blockers=(parse_blocker,),
            verification_gaps=(),
            contract_status=ContractStatus(
                task_contract="unclear",
                interfaces="not_applicable",
                persistence="not_applicable",
                tests="missing",
            ),
            source="json",
        )
        rendered = render_release_markdown(
            stub,
            title=_system_release_title(ctx.output_language),
            language=ctx.output_language,
        )
        rendered = (
            f"cross_final_acceptance parse error: {e}\n\n"
            f"Raw output:\n{raw}\n\n{rendered}"
        )
        return CrossFinalAcceptanceResult(
            parsed=stub,
            source="parse_error",
            raw_output=raw,
            rendered=rendered,
            duration_s=duration_s,
            parse_error=str(e),
            prompt_text=prompt_turn.text,
        )
    rendered = render_release_markdown(
        parsed,
        title=_system_release_title(ctx.output_language),
        language=ctx.output_language,
    )
    return CrossFinalAcceptanceResult(
        parsed=parsed,
        source="agent",
        raw_output=raw,
        rendered=rendered,
        duration_s=duration_s,
        prompt_text=prompt_turn.text,
    )


def _dry_run_approved_release_json() -> str:
    """Synthesise an APPROVED release-shape JSON for the dry-run path.

    Mirrors :func:`pipeline.phases.adapters._approved_release_json` —
    same coherence guarantees so ``parse_release`` accepts the output
    without halting the gate."""
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      (
            "System release gate dry run: synthesised APPROVED payload."
        ),
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "sufficient",
        },
    })


def result_to_phase_log_entry(
    result: CrossFinalAcceptanceResult,
) -> dict[str, Any]:
    """Project the gate result onto the dual-shape phase_log dict the
    runner writes into ``session["phases"]["cross_final_acceptance"]``.

    Same shape :class:`pipeline.session_adapters.FinalAcceptanceAdapter`
    produces for project final_acceptance, plus the ``source``
    discriminator and the ``raw_output`` / ``output`` book-keeping.
    Review-shape mirror fields are projected from release blockers via
    :meth:`ReleaseBlocker.to_finding_dict` so existing findings
    consumers (Web phase card, SDK ``list_findings``, MCP
    ``orcho_run_evidence``) see the cross-gate blockers without API
    changes.
    """
    parsed = result.parsed
    findings_mirror = [
        b.to_finding_dict() for b in parsed.release_blockers
    ]
    entry: dict[str, Any] = {
        # Review-shape mirror.
        "output":         result.rendered,
        "raw_output":     result.raw_output,
        "approved":       parsed.approved,
        "verdict":        parsed.verdict,
        "short_summary":  parsed.short_summary,
        "findings":       findings_mirror,
        # Release-shape (ADR 0025).
        "ship_ready":         parsed.ship_ready,
        "release_blockers":   parsed.blockers_as_dicts(),
        "verification_gaps":  parsed.gaps_as_dicts(),
        "contract_status":    parsed.contract_status.to_dict(),
        # Cross-gate-only book-keeping.
        "source":         result.source,
        "duration_s":     result.duration_s,
    }
    if result.parse_error is not None:
        entry["parse_error"] = result.parse_error
    return entry


def collect_for_evidence(
    entry: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Helper for evidence collectors: pulls the canonical release
    fields out of the phase_log entry. The collector's existing
    ``_build_release_summary`` already does this for project
    final_acceptance; the same logic applies to cross_final_acceptance
    via :data:`pipeline.evidence.collector._RELEASE_BEARING_PHASES`.

    Kept here as documentation of the projection contract — the
    collector reads ``entry`` directly without calling this helper.
    """
    return {
        "phase":              "cross_final_acceptance",
        "verdict":            entry.get("verdict"),
        "ship_ready":         entry.get("ship_ready"),
        "short_summary":      entry.get("short_summary"),
        "release_blockers":   entry.get("release_blockers") or [],
        "verification_gaps":  entry.get("verification_gaps") or [],
        "contract_status":    entry.get("contract_status"),
    }


# Re-export Path for type-checkers that may consume this module from
# legacy import sites; the canonical pathlib is the one from stdlib.
_Path = Path  # noqa: F841
