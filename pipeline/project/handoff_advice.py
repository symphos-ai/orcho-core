# SPDX-License-Identifier: Apache-2.0
"""Advisory recommendation for a paused phase handoff (read-only advisor).

When a handoff pauses on a rejected reviewer verdict or an incomplete implement
delivery, the operator menu can offer an advisory pass: a read-only advisor
recommends the smallest honest way forward (retry with feedback / continue /
halt / waiver). It never decides and never mutates artifacts; an accepted
``retry_feedback`` recommendation flows through the existing durable decision
path. Owns the advisor data shapes, prompt assembly, read-only invocation,
parsing/normalisation, a safety classifier (no automatic waiver), eligibility
(trigger AND verdict), the durable advice artifact, and the provenance note —
modelled on ``correction_triage`` (a ``[handoff_advice]`` mock marker, a
code-owned response contract here, a tolerant JSON extractor).

Layering: invoked from the project orchestrator (not ``pipeline.control``);
must NOT import ``pipeline.project.app``. Usage accounting reuses
``pipeline.cross_project.usage`` — the ``pipeline.project ->
pipeline.cross_project`` direction already has a precedent
(``pipeline.project.profile_setup`` imports ``pipeline.cross_project.handoff``)
and no AST/isolation test bans it, so the shared helper is reused.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from core.infra.paths import PROMPTS_DIR

# Durable artifact + provenance live in a focused leaf module; re-exported here
# so the advisor module stays the stable public facade for them.
from pipeline.project.handoff_advice_artifact import (
    build_provenance_note,
    load_advice_artifact,
    write_advice_artifact,
)

if TYPE_CHECKING:
    from pipeline.runtime.handoff import PhaseHandoffRequested

#: Prompt marker the mock provider keys its advisor branch on.
_ADVICE_MARKER = "[handoff_advice]"
#: Phase label for the advisor invoke — a separate ``handoff_advice`` usage slot
#: surfaced in ``metrics.json`` distinct from the pipeline phases.
_ADVICE_PHASE = "handoff_advice"
#: Extras key carrying the in-memory advisor-usage aggregate (alongside the
#: durable ``handoff_advice`` metrics phase).
_ADVICE_USAGE_EXTRAS_KEY = "_phase_handoff_advice_usage"
#: Triggers / verdicts an advisory pass is meant for. ``build_phase_handoff_signal``
#: emits REJECTED; ``subtask_dag_handoff`` emits INCOMPLETE (exhausted implement).
_ADVISORY_TRIGGERS: frozenset[str] = frozenset(
    {
        "rejected",
        "incomplete",
        "verification_gate_failed",
    }
)
_ADVISORY_VERDICTS: frozenset[str] = frozenset({"REJECTED", "INCOMPLETE"})
#: ADR 0112 §5 scope-expansion handoff trigger family (participant_add /
#: out_of_plan). Recognised as advisory-eligible via prefix since the tail is
#: opaque (a repo identity), so it cannot be enumerated like the fixed triggers.
_SCOPE_EXPANSION_TRIGGER_PREFIX = "scope_expansion:"
_VALID_ACTIONS: tuple[str, ...] = (
    "continue",
    "retry_feedback",
    "halt",
    "continue_with_waiver",
)
_VALID_CONFIDENCE: tuple[str, ...] = ("high", "medium", "low")
#: Compactness caps so the advisor context never copies a whole transcript.
_LAST_OUTPUT_CHAR_LIMIT = 4000
_FINDING_BODY_CHAR_LIMIT = 400
_DIFF_SUMMARY_CHAR_LIMIT = 2000
_MAX_FINDINGS = 20
_GIT_STATUS_TIMEOUT_S = 5.0
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")

#: Code-owned response contract appended to the composed prompt (machine output
#: shape is a code-only surface per the prompt-boundary discipline).
_RESPONSE_CONTRACT = (
    "Respond with exactly one JSON object and nothing else. Use this shape:\n"
    "{\n"
    '  "recommended_action": "<one of: continue | retry_feedback | halt | '
    'continue_with_waiver>",\n'
    '  "confidence": "<one of: high | medium | low>",\n'
    '  "rationale": "<one or two sentences on why this path is the smallest '
    'honest way forward>",\n'
    '  "retry_feedback": "<required when recommended_action is retry_feedback: '
    'the corrective feedback the retry round should act on; otherwise empty>",\n'
    '  "risks": ["<concrete risks of the recommended path>"],\n'
    '  "expected_files": ["<files a retry would most likely touch>"],\n'
    '  "operator_note": "<optional short note to the operator>"\n'
    "}\n"
    "Do not act on your own recommendation. An operator reviews it first."
)


@dataclass(frozen=True, slots=True)
class AdviceContext:
    """Compact, read-only context handed to the advisor (built truncated)."""

    run_id: str
    handoff_id: str
    phase: str
    trigger: str
    verdict: str
    available_actions: tuple[str, ...]
    findings: tuple[Mapping[str, Any], ...] = ()
    last_output: str = ""
    last_phase_summary: str = ""
    task_title: str = ""
    diff_summary: str = ""
    prior_retry_round: int = 1
    loop_max_rounds: int = 1
    correction_context: str = ""
    response_language: str = ""


@dataclass(frozen=True, slots=True)
class HandoffAdvice:
    """The advisor's normalised recommendation."""

    recommended_action: Literal[
        "continue",
        "retry_feedback",
        "halt",
        "continue_with_waiver",
    ]
    confidence: Literal["high", "medium", "low"]
    rationale: str
    retry_feedback: str
    risks: tuple[str, ...] = ()
    expected_files: tuple[str, ...] = ()
    operator_note: str = ""
    parse_warnings: tuple[str, ...] = ()
    raw_output: str = ""


@dataclass(frozen=True, slots=True)
class AdviceSafety:
    """Safety classification: ``auto_apply_ok`` only for non-low-confidence
    ``retry_feedback``; ``needs_confirmation`` flags low confidence;
    ``blocked_reason`` set for any non-retry recommendation; ``waiver_blocked``
    records a render-only waiver, flagged when blocking-severity findings exist.
    """

    auto_apply_ok: bool
    needs_confirmation: bool
    blocked_reason: str = ""
    waiver_blocked: bool = False


@dataclass(frozen=True, slots=True)
class AdvisorResult:
    """Return value of :func:`invoke_advisor`."""

    advice: HandoffAdvice
    raw: str
    usage: Mapping[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0


def advice_actions_available(signal: PhaseHandoffRequested) -> bool:
    """Return True when an advisory pass should be offered for ``signal``.

    All four must hold: (a) ``trigger`` is ``rejected``/``incomplete`` /
    ``verification_gate_failed`` OR a
    ``scope_expansion:*`` sanction; (b) verdict is rejected/incomplete-equivalent
    — ``approved is not True`` and the normalised verdict is
    ``REJECTED``/``INCOMPLETE`` (an empty verdict with ``approved=False`` counts; a
    matching trigger with an APPROVED verdict is NOT eligible); (c) ``retry_feedback``
    is in ``available_actions`` — EXCEPT for a scope-expansion sanction, whose
    terminal final_acceptance seam offers no retry (continue / continue_with_waiver
    / halt only), so advice still recommends among those; (d) a non-empty
    ``last_output`` or a finding. advice / retry_with_advice stay UI-only.
    """
    trigger = getattr(signal, "trigger", "") or ""
    # ADR 0112 §5: a scope-expansion handoff carries an opaque
    # ``scope_expansion:*`` trigger (participant_add / out_of_plan). It is
    # advisory-eligible like rejected/incomplete — the operator can ask for the
    # smallest honest way forward on a paused out-of-plan sanction too.
    is_scope_expansion = trigger.startswith(_SCOPE_EXPANSION_TRIGGER_PREFIX)
    is_hygiene_gate = hygiene_gate_failure_kind(signal) is not None
    if trigger not in _ADVISORY_TRIGGERS and not is_scope_expansion:
        return False
    if getattr(signal, "approved", None) is True:
        return False
    verdict_label = str(getattr(signal, "verdict", "") or "").strip().upper()
    if verdict_label and verdict_label not in _ADVISORY_VERDICTS:
        return False
    available = tuple(getattr(signal, "available_actions", ()) or ())
    # The retry-availability gate is the contract for rejected/incomplete advice
    # (advice's job there is to propose a retry). A scope-expansion sanction has
    # no retry at the terminal seam, so it is exempt: advice still recommends the
    # smallest honest route among continue / continue_with_waiver / halt.
    if not is_scope_expansion and not is_hygiene_gate and "retry_feedback" not in available:
        return False
    last_output = str(getattr(signal, "last_output", "") or "").strip()
    findings = _extract_findings(getattr(signal, "artifacts", {}) or {})
    return bool(last_output or findings)


def hygiene_gate_failure_kind(signal: PhaseHandoffRequested) -> str | None:
    """Return the typed hygiene kind on a verification-gate handoff, if any."""
    if getattr(signal, "trigger", "") != "verification_gate_failed":
        return None
    artifacts = getattr(signal, "artifacts", {}) or {}
    if not isinstance(artifacts, Mapping):
        return None
    findings = artifacts.get("findings")
    if not isinstance(findings, (list, tuple)):
        return None
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        kind = str(finding.get("failure_kind") or "").strip()
        if kind in {"provenance_failure", "env_failure"}:
            return kind
    return None


def hygiene_gate_advice(signal: PhaseHandoffRequested) -> HandoffAdvice | None:
    """Return the deterministic waiver recommendation for a hygiene handoff."""
    kind = hygiene_gate_failure_kind(signal)
    if kind is None:
        return None
    return HandoffAdvice(
        recommended_action="continue_with_waiver",
        confidence="high",
        rationale=(
            f"{kind} is verification-environment evidence, not an agent repair "
            "task; an operator must explicitly waive or halt."
        ),
        retry_feedback="",
        operator_note="No repair retry is available for a hygiene verification failure.",
    )


def _extract_findings(artifacts: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Compact findings (id/severity/title/required_fix + trimmed body)."""
    raw = artifacts.get("findings")
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        finding = {
            "id": str(item.get("id") or "").strip(),
            "severity": str(item.get("severity") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "required_fix": str(item.get("required_fix") or "").strip(),
        }
        body = str(item.get("body") or "").strip()
        if body:
            finding["body"] = _truncate(body, _FINDING_BODY_CHAR_LIMIT)
        out.append(finding)
        if len(out) >= _MAX_FINDINGS:
            break
    return tuple(out)


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars with a visible elision marker."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…[truncated]"


def _git_status_summary(git_cwd: str) -> str:
    """Best-effort ``git status --short`` in ``git_cwd``; '' on any error."""
    if not git_cwd:
        return ""
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=git_cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_STATUS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return _truncate(proc.stdout.strip(), _DIFF_SUMMARY_CHAR_LIMIT)


def _task_title(task: Any) -> str:
    """First non-empty line of the run task, trimmed."""
    if not isinstance(task, str):
        return ""
    for line in task.splitlines():
        stripped = line.strip()
        if stripped:
            return _truncate(stripped, 200)
    return ""


def _text_from_findings(findings: tuple[Mapping[str, Any], ...]) -> str:
    """Flatten compact finding fields for language detection only."""
    parts: list[str] = []
    for finding in findings:
        for key in ("id", "severity", "title", "required_fix", "body"):
            value = finding.get(key)
            if value:
                parts.append(str(value))
    return "\n".join(parts)


def _infer_response_language(*parts: str) -> str:
    """Infer the advisor's natural-language output language from handoff text.

    This is intentionally narrow. The advisor should mirror the operator-facing
    handoff surface when it clearly uses Russian, while protocol keys and enum
    values stay in English. Otherwise keep the previous default with no extra
    language directive.
    """
    surface = "\n".join(part for part in parts if part)
    if _CYRILLIC_RE.search(surface):
        return "Russian"
    return ""


def build_advice_context(run: Any, signal: PhaseHandoffRequested) -> AdviceContext:
    """Assemble a compact :class:`AdviceContext` from a run + paused signal:
    findings from ``signal.artifacts`` (bodies truncated), last output to ~4000
    chars, a best-effort ``git status --short`` diff in ``run.git_cwd``, and the
    first line of ``run.state.task``."""
    artifacts = getattr(signal, "artifacts", {}) or {}
    last_output = _truncate(
        str(getattr(signal, "last_output", "") or "").strip(),
        _LAST_OUTPUT_CHAR_LIMIT,
    )
    state = getattr(run, "state", None)
    task = getattr(state, "task", "") if state is not None else ""
    task_title = _task_title(task)
    findings = _extract_findings(artifacts)
    last_phase_summary = str(artifacts.get("short_summary") or "").strip()
    correction_context = _truncate(
        str(artifacts.get("correction_context") or "").strip(),
        _FINDING_BODY_CHAR_LIMIT,
    )
    return AdviceContext(
        run_id=str(getattr(run, "session_ts", "") or ""),
        handoff_id=str(getattr(signal, "handoff_id", "") or ""),
        phase=str(getattr(signal, "phase", "") or ""),
        trigger=str(getattr(signal, "trigger", "") or ""),
        verdict=str(getattr(signal, "verdict", "") or ""),
        available_actions=tuple(getattr(signal, "available_actions", ()) or ()),
        findings=findings,
        last_output=last_output,
        last_phase_summary=last_phase_summary,
        task_title=task_title,
        diff_summary=_git_status_summary(str(getattr(run, "git_cwd", "") or "")),
        prior_retry_round=int(getattr(signal, "round", 1) or 1),
        loop_max_rounds=int(getattr(signal, "loop_max_rounds", 1) or 1),
        correction_context=correction_context,
        response_language=_infer_response_language(
            task_title,
            last_phase_summary,
            _text_from_findings(findings),
            last_output,
            correction_context,
        ),
    )


def _advice_task_body() -> str:
    """Load the user-editable advisor procedure part."""
    path = PROMPTS_DIR / "tasks" / "handoff_advice.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _render_context_block(ctx: AdviceContext) -> str:
    """Render the compact recorded-context section embedded in the prompt."""
    lines: list[str] = [
        "# Recorded handoff context",
        "",
        f"- phase: {ctx.phase}",
        f"- trigger: {ctx.trigger}",
        f"- verdict: {ctx.verdict}",
        f"- round: {ctx.prior_retry_round}/{ctx.loop_max_rounds}",
    ]
    if ctx.task_title:
        lines.append(f"- task: {ctx.task_title}")
    if ctx.available_actions:
        lines.append(f"- available actions: {', '.join(ctx.available_actions)}")
    if ctx.last_phase_summary:
        lines += ["", "## Reviewer summary", ctx.last_phase_summary]
    if ctx.findings:
        lines += ["", "## Findings"]
        for f in ctx.findings:
            head = " ".join(
                p for p in (f.get("severity"), f.get("id"), f.get("title")) if p
            ).strip()
            lines.append(f"- {head or '(finding)'}")
            if f.get("required_fix"):
                lines.append(f"    required_fix: {f['required_fix']}")
            if f.get("body"):
                lines.append(f"    {f['body']}")
    if ctx.last_output:
        lines += ["", "## Last output", ctx.last_output]
    if ctx.correction_context:
        lines += ["", "## Correction context", ctx.correction_context]
    if ctx.diff_summary:
        lines += ["", "## Working tree (git status --short)", ctx.diff_summary]
    return "\n".join(lines)


def _render_language_policy(ctx: AdviceContext) -> str:
    """Render the natural-language policy for advisor JSON string values."""
    if not ctx.response_language:
        return ""
    return "\n".join(
        (
            "# Response language",
            (
                "Write human-readable JSON string values "
                f"(`rationale`, `retry_feedback`, `risks`, `operator_note`) "
                f"in {ctx.response_language}."
            ),
            (
                "JSON keys, protocol enum values, file paths, identifiers, command "
                "names, and code symbols stay in their original language."
            ),
        )
    )


def build_advice_prompt(ctx: AdviceContext) -> str:
    """Compose the read-only advisor prompt (marker, task, context, contract)."""
    sections = [_ADVICE_MARKER]
    body = _advice_task_body()
    if body:
        sections.append(body)
    sections.append(_render_context_block(ctx))
    language_policy = _render_language_policy(ctx)
    if language_policy:
        sections.append(language_policy)
    sections.append(_RESPONSE_CONTRACT)
    return "\n\n".join(sections)


def _build_advice_turn(ctx: AdviceContext):
    """Per-turn prompt turn for the advisor call (embeds per-handoff findings)."""
    from pipeline.prompts.composer import assemble_cache_first_segments
    from pipeline.prompts.types import (
        PromptCacheScope,
        PromptLayer,
        PromptPart,
        PromptStability,
    )

    return assemble_cache_first_segments(
        (
            PromptPart(
                kind="task",
                name="handoff_advice",
                source="builtin",
                body=build_advice_prompt(ctx),
                layer=PromptLayer.TURN,
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="handoff advice embeds per-handoff findings",
            ),
        )
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Tolerant extraction of one JSON object (mirrors correction_triage)."""
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


def _as_str_list(value: Any) -> tuple[str, ...]:
    """Normalize a scalar / list / mapping field into a tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, Mapping):
        text = str(value.get("title") or value.get("summary") or value).strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_as_str_list(item))
        return tuple(out)
    text = str(value).strip()
    return (text,) if text else ()


def _clamp_advice_to_available(
    advice: HandoffAdvice,
    available_actions: tuple[str, ...],
) -> HandoffAdvice:
    """Normalise a recommendation the operator cannot take down to ``halt``.

    The advisor prompt lists the offered actions, but a model can still recommend
    one outside the set — e.g. ``retry_feedback`` for a scope-expansion sanction
    whose terminal final_acceptance seam offers only continue / continue_with_waiver
    / halt. Surfacing an unavailable action is misleading and ``phase_handoff_decide``
    would reject it, so clamp to ``halt`` (the same conservative fallback
    :func:`parse_advice` uses for an unknown action) and drop any retry feedback so
    nothing auto-applies. An empty ``available_actions`` (no constraint known) is
    left untouched.
    """
    if not available_actions or advice.recommended_action in available_actions:
        return advice
    return replace(
        advice,
        recommended_action="halt",
        retry_feedback="",
        parse_warnings=advice.parse_warnings
        + (
            f"recommended_action {advice.recommended_action!r} not in "
            f"available_actions {available_actions!r}; normalized to 'halt'",
        ),
    )


def parse_advice(raw: str) -> HandoffAdvice:
    """Parse + normalise advisor output into a :class:`HandoffAdvice`.

    Unparseable → ``halt``/``low`` + warning; unknown ``recommended_action`` →
    ``halt``; unknown ``confidence`` → ``low``; an empty ``retry_feedback`` under
    a ``retry_feedback`` action downgrades to ``halt`` (a non-actionable retry
    must never be auto-applied).
    """
    data = _extract_json_object(raw)
    if data is None:
        return HandoffAdvice(
            recommended_action="halt",
            confidence="low",
            rationale=("Advisor output could not be parsed as a structured recommendation."),
            retry_feedback="",
            parse_warnings=("advice_unparseable",),
            raw_output=raw,
        )
    warnings: list[str] = []
    action = str(data.get("recommended_action") or "").strip().lower()
    if action not in _VALID_ACTIONS:
        warnings.append(f"unsupported recommended_action {action!r} normalized to 'halt'")
        action = "halt"
    confidence = str(data.get("confidence") or "").strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        warnings.append(f"unsupported confidence {confidence!r} normalized to 'low'")
        confidence = "low"
    rationale = str(data.get("rationale") or "").strip()
    retry_feedback = str(data.get("retry_feedback") or "").strip()
    operator_note = str(data.get("operator_note") or "").strip()
    if action == "retry_feedback" and not retry_feedback:
        warnings.append("retry_feedback recommendation carried no feedback; downgraded to 'halt'")
        action = "halt"
    return HandoffAdvice(
        recommended_action=action,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        rationale=rationale,
        retry_feedback=retry_feedback,
        risks=_as_str_list(data.get("risks")),
        expected_files=_as_str_list(data.get("expected_files")),
        operator_note=operator_note,
        parse_warnings=tuple(warnings),
        raw_output=raw,
    )


def _has_blocking_severity(findings: Any) -> bool:
    """True unless every finding is a positively-recognised low severity.

    A waiver is never auto-applied, so anything not provably non-blocking —
    P1/P2, blank, or an unrecognised label — counts as blocking; only ``P<n>``
    with n>=3 is non-blocking.
    """
    if not isinstance(findings, (list, tuple)):
        return True
    if not findings:
        return False
    for item in findings:
        sev = ""
        if isinstance(item, Mapping):
            sev = str(item.get("severity") or "").strip().upper()
        m = re.fullmatch(r"P(\d+)", sev)
        if not (m and int(m.group(1)) >= 3):
            return True
    return False


def classify_advice_safety(
    advice: HandoffAdvice,
    findings: Any = (),
) -> AdviceSafety:
    """Classify whether a recommendation may be auto-applied: ``auto_apply_ok``
    only for ``retry_feedback`` at non-``low`` confidence (the only durable
    action this path generates); any ``low`` sets ``needs_confirmation``;
    non-retry recommendations set ``blocked_reason``; ``continue_with_waiver`` is
    ALWAYS render-only (``waiver_blocked``) — never auto-applied.
    """
    confidence_low = advice.confidence == "low"
    action = advice.recommended_action
    if action == "continue_with_waiver":
        return AdviceSafety(
            auto_apply_ok=False,
            needs_confirmation=confidence_low,
            blocked_reason=(
                "continue_with_waiver is advisory only; a waiver is never "
                "applied automatically from the advice path"
            ),
            waiver_blocked=True,
        )
    if action != "retry_feedback":
        return AdviceSafety(
            auto_apply_ok=False,
            needs_confirmation=confidence_low,
            blocked_reason=(
                f"{action!r} is not an auto-appliable advisory action; only "
                "retry_feedback generates a durable decision"
            ),
            waiver_blocked=_has_blocking_severity(findings),
        )
    return AdviceSafety(
        auto_apply_ok=not confidence_low,
        needs_confirmation=confidence_low,
        blocked_reason=""
        if not confidence_low
        else ("low-confidence retry_feedback requires explicit operator confirmation"),
        waiver_blocked=_has_blocking_severity(findings),
    )


def invoke_advisor(
    run: Any,
    ctx: AdviceContext,
    *,
    agent: Any = None,
) -> AdvisorResult:
    """Call the read-only advisor and return its parsed recommendation.

    Resolves ``review_changes_agent`` off ``run.state.phase_config`` unless an
    ``agent`` is injected (tests); invokes through the session-aware boundary
    with ``mutates_artifacts=False``, captures usage into the ``handoff_advice``
    extras slot (failure non-fatal).
    """
    state = run.state
    if agent is None:
        from pipeline.phases.builtin.registry import _require_agent

        agent = _require_agent(state, "review_changes_agent")
    from pipeline.phases.builtin.session_invoke import _session_aware_invoke

    cwd = str(getattr(run, "git_cwd", "") or "")
    prompt = build_advice_prompt(ctx)
    turn = _build_advice_turn(ctx)
    start = time.perf_counter()
    raw = _session_aware_invoke(
        agent,
        state,
        phase=_ADVICE_PHASE,
        turn=turn,
        cwd=cwd,
        mutates_artifacts=False,
    )
    duration = round(time.perf_counter() - start, 3)
    advice = _clamp_advice_to_available(parse_advice(raw), ctx.available_actions)
    usage = _capture_advice_usage(run, agent, duration, prompt=prompt, output=raw)
    return AdvisorResult(advice=advice, raw=raw, usage=usage, duration_s=duration)


def _capture_advice_usage(
    run: Any,
    agent: Any,
    duration_s: float,
    *,
    prompt: str,
    output: str,
) -> dict[str, Any]:
    """Capture the advisor invoke's usage into the in-memory extras aggregate.

    The usage is accumulated into a ``run.state.extras`` aggregate for in-memory
    inspection. It is DELIBERATELY NOT recorded as a ``handoff_advice``
    ``MetricsCollector`` phase: the advisor runs outside the FSM phase loop, and
    folding its usage into a phase double-counts it into ``metrics.json``'s
    ``total_*`` (ADR 0093 / T4). The durable advice usage instead reaches
    ``metrics.json`` as an OBSERVE-ONLY ``handoff_advice`` slot — re-derived from
    the durable advice artifacts by the upper layer
    (``pipeline.project.run._push_handoff_advice_usage`` →
    ``MetricsCollector.record_advice_usage``) and never summed into the totals.
    Reuses the cross-project usage helpers (layering precedent in the module
    docstring); any failure is swallowed — usage accounting must never break the
    advisory pass.
    """
    try:
        from pipeline.cross_project.usage import (
            _capture_invoke_usage,
            accumulate_phase_usage,
        )

        usage = _capture_invoke_usage(
            agent,
            duration_s,
            prompt=prompt,
            output=output,
        )
        state = getattr(run, "state", None)
        extras = getattr(state, "extras", None)
        if isinstance(extras, dict):
            target = extras.setdefault(_ADVICE_USAGE_EXTRAS_KEY, {})
            if isinstance(target, dict):
                accumulate_phase_usage(target, _ADVICE_PHASE, usage)
        return usage
    except Exception:
        return {}


__all__ = [
    "AdviceContext",
    "AdviceSafety",
    "AdvisorResult",
    "HandoffAdvice",
    "advice_actions_available",
    "build_advice_context",
    "build_advice_prompt",
    "build_provenance_note",
    "classify_advice_safety",
    "invoke_advisor",
    "load_advice_artifact",
    "parse_advice",
    "write_advice_artifact",
]
