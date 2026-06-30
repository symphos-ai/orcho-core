# SPDX-License-Identifier: Apache-2.0
"""``correction_triage`` phase handler — entry gate for the correction profile.

When the closing acceptance gate rejects a change and the operator chooses
``fix``, a correction follow-up re-enters the pipeline starting at this
phase (ADR 0070 wrote the rejection context as ``correction_context.md``
in the run's output dir). The triage phase is read-only: it reads that
recorded context and classifies the smallest honest way to close the
listed release blockers before any code change runs.

It is deliberately narrow. Triage does **not** replan the original task,
does **not** widen scope beyond the recorded blockers, and treats the
retained worktree as the subject of the correction. The machine output
shape (``kind`` enum + fields) is code-owned here, not in the
user-editable task part, so the prompt-boundary contract stays intact.

Stage 0: every supported ``kind`` continues the pipeline into
``implement``; triage records a verdict but does not branch the route —
no FSM lives here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from core.infra.paths import PROMPTS_DIR
from pipeline.phases.builtin.lifecycle import (
    _agent_project_dir,
    _carry_trace_metadata,
)
from pipeline.phases.builtin.registry import _require_agent
from pipeline.phases.builtin.session_invoke import _session_aware_invoke
from pipeline.prompts.composer import assemble_cache_first_segments
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


#: Supported triage classifications. ``code_fix`` — a narrow code change in
#: the retained worktree; ``contract_ack`` — a contract/documentation
#: acknowledgement with no code change; ``gate_rerun`` — the blockers are
#: stale and re-running the gate should clear them; ``blocked`` — no safe
#: path forward (``blockers`` then names what is missing).
_VALID_KINDS: tuple[str, ...] = ("code_fix", "contract_ack", "gate_rerun", "blocked")

#: Prompt marker the mock provider keys its triage branch on, mirroring the
#: ``[final_acceptance]`` precedent in ``adapters.run_review``.
_TRIAGE_MARKER = "[correction_triage]"

#: Halt reason emitted when a correction profile is started without any
#: correction context — guards against a direct fresh run of the internal
#: profile.
_MISSING_CONTEXT_REASON = "correction_triage_missing_context"

#: Halt reason emitted when triage classifies (or normalizes) the verdict
#: as ``blocked`` — there is no safe remediation path, so the run stops
#: here before any code-change phase can burn tokens.
_BLOCKED_REASON = "correction_triage_blocked"

_CONTEXT_FILENAME = "correction_context.md"

#: Code-owned response contract appended to the composed prompt. Lives here
#: (not in ``tasks/correction_triage.md``) because machine output shape is a
#: code-only surface per the prompt-boundary discipline.
_RESPONSE_CONTRACT = (
    "Respond with exactly one JSON object and nothing else. Use this shape:\n"
    "{\n"
    '  "kind": "<one of: code_fix | contract_ack | gate_rerun | blocked>",\n'
    '  "summary": "<one or two sentences on how to close the listed blockers>",\n'
    '  "allowed_scope": ["<files or areas you may touch — keep this narrow>"],\n'
    '  "required_checks": ["<verification each remediation must pass>"],\n'
    '  "blockers": ["<only when kind is blocked: what prevents remediation>"]\n'
    "}\n"
    "Meaning of each kind:\n"
    "- code_fix: the blockers need a narrow code change in the retained worktree.\n"
    "- contract_ack: the blockers are a contract/documentation acknowledgement, "
    "no code change.\n"
    "- gate_rerun: the blockers are stale; re-running the checks should clear them.\n"
    "- blocked: remediation cannot proceed; name what is missing in blockers.\n"
    "Do not replan the original task. Do not expand scope beyond the listed blockers."
)


def _load_correction_context(state: PipelineState) -> str | None:
    """Return the recorded correction context, or ``None`` when absent.

    Reads ``<output_dir>/correction_context.md`` — the artifact the
    correction follow-up driver writes before dispatching the child run.
    This file is the *only* accepted correction context: run lineage such
    as ``plan_source_run_id`` (set by ``--from-run-plan``) does not carry
    the rejection blockers and must not stand in for it. An empty file
    counts as absent.
    """
    output_dir = state.output_dir
    if output_dir is None:
        return None
    path = output_dir / _CONTEXT_FILENAME
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _triage_task_body() -> str:
    """Load the user-editable triage procedure part."""
    path = PROMPTS_DIR / "tasks" / "correction_triage.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _build_triage_prompt(task: str, context: str) -> str:
    """Compose the read-only triage prompt.

    Marker first (mock detection + reviewer focus), then the procedure
    part, the recorded rejection context, and the code-owned response
    contract.
    """
    sections = [f"{_TRIAGE_MARKER} {task}".strip()]
    body = _triage_task_body()
    if body:
        sections.append(body)
    if context:
        sections.append("# Recorded correction context\n\n" + context)
    sections.append(_RESPONSE_CONTRACT)
    return "\n\n".join(sections)


def _build_triage_turn(task: str, context: str):
    """Return the prompt turn for the read-only correction triage call.

    The triage body embeds recorded release blockers and the current
    correction task, so it is per-turn payload. Keep it out of stable
    cache scopes while still flowing through the standard session-aware
    invoke boundary for usage accounting and prompt/session evidence.
    """
    return assemble_cache_first_segments((
        PromptPart(
            kind="task",
            name="correction_triage",
            source="builtin",
            body=_build_triage_prompt(task, context),
            layer=PromptLayer.TURN,
            stability=PromptStability.TURN,
            cache_scope=PromptCacheScope.NONE,
            volatile_reason="correction triage embeds release blockers",
        ),
    ))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of one JSON object from model output.

    Tolerant by design (mirrors the lenient posture of the release
    parser): strips a leading code fence, tries a whole-string parse,
    then scans for the first balanced ``{...}`` object. Returns ``None``
    when nothing parses.
    """
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z0-9_]*\n", "", cleaned)
        cleaned = re.sub(r"\n```\s*$", "", cleaned).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                    except (ValueError, TypeError):
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = cleaned.find("{", start + 1)
    return None


def _as_str_list(value: Any) -> list[str]:
    """Normalize a scalar / list / mapping field into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Mapping):
        text = str(
            value.get("summary")
            or value.get("title")
            or value.get("blocker")
            or value
        ).strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_as_str_list(item))
        return out
    text = str(value).strip()
    return [text] if text else []


def _normalize_triage(data: dict[str, Any] | None, *, raw: str) -> dict[str, Any]:
    """Coerce parsed (or unparsed) triage output into the durable record.

    Always returns the full field set ``{kind, summary, allowed_scope,
    required_checks, blockers}``. An unparseable response or an
    out-of-set ``kind`` normalizes to ``blocked`` with an explanatory
    blocker so downstream evidence never sees a bare/invalid verdict.
    """
    if data is None:
        return {
            "kind": "blocked",
            "summary": (
                "Correction triage output could not be parsed as a "
                "structured record."
            ),
            "allowed_scope": [],
            "required_checks": [],
            "blockers": [
                "Triage response was not valid JSON; manual triage required."
            ],
            "raw_output": raw,
            "parse_warnings": ["triage_unparseable"],
        }

    parse_warnings: list[str] = []
    kind = str(data.get("kind") or "").strip().lower()
    summary = str(data.get("summary") or "").strip()
    allowed_scope = _as_str_list(data.get("allowed_scope"))
    required_checks = _as_str_list(data.get("required_checks"))
    blockers = _as_str_list(data.get("blockers"))

    if kind not in _VALID_KINDS:
        parse_warnings.append(
            f"unsupported kind {kind!r} normalized to 'blocked'"
        )
        if not blockers:
            blockers = [
                f"Triage returned an unsupported kind {kind!r}; "
                "manual triage required."
            ]
        kind = "blocked"

    if kind == "blocked" and not blockers:
        blockers = [
            summary or "Triage marked the run blocked without naming a blocker."
        ]

    return {
        "kind": kind,
        "summary": summary,
        "allowed_scope": allowed_scope,
        "required_checks": required_checks,
        "blockers": blockers,
        "raw_output": raw,
        "parse_warnings": parse_warnings,
    }


def _dry_run_record() -> dict[str, Any]:
    """Canonical triage record for ``dry_run`` — no agent invocation."""
    return {
        "kind": "code_fix",
        "summary": "[DRY RUN] correction triage skipped agent invocation.",
        "allowed_scope": [],
        "required_checks": [],
        "blockers": [],
        "raw_output": "",
        "parse_warnings": [],
        "meta": {"dry_run": True},
    }


def _phase_correction_triage(state: PipelineState) -> PipelineState:
    """Classify how to close the recorded release blockers (read-only).

    Fail-fast when started without correction context (guards a direct
    fresh run of the internal ``correction`` profile). Otherwise invoke
    the read-only reviewer slot and record the structured triage verdict
    into ``state.phase_log['correction_triage']``.
    """
    context = _load_correction_context(state)
    if context is None:
        state.phase_log["correction_triage"] = {
            "kind": "blocked",
            "summary": (
                "Correction profile started without correction context."
            ),
            "allowed_scope": [],
            "required_checks": [],
            "blockers": [
                "No non-empty correction_context.md in the run output dir; "
                "refusing to triage a context-less correction run."
            ],
            "halted": True,
            "reason": _MISSING_CONTEXT_REASON,
            "parse_warnings": [],
        }
        state.stop(_MISSING_CONTEXT_REASON)
        return state

    if state.dry_run:
        state.phase_log["correction_triage"] = _dry_run_record()
        return state

    agent = _require_agent(state, "review_changes_agent")
    cwd = _agent_project_dir(state)
    raw = _session_aware_invoke(
        agent,
        state,
        phase="correction_triage",
        turn=_build_triage_turn(state.task, context),
        cwd=cwd,
        mutates_artifacts=False,
    )
    trace = _carry_trace_metadata(state, "correction_triage")
    record = _normalize_triage(_extract_json_object(raw), raw=raw)
    record.update(trace)
    state.phase_log["correction_triage"] = record

    # A ``blocked`` verdict (including one normalized from an unparseable or
    # unsupported response) has no safe remediation path: halt here before
    # any code-change phase runs, mirroring the context-less fail-fast above.
    if record["kind"] == "blocked":
        record["halted"] = True
        record["reason"] = _BLOCKED_REASON
        state.stop(_BLOCKED_REASON)
    return state
