"""
pipeline/engine/hypothesis.py — Phase 0.hypothesis: hypothesis-driven loop.

Fast pre-PLAN gut-check: ask Architect for a 3–5 line hypothesis, ask QA to
validate it, retry up to N times. NOT to be confused with the upcoming
``research`` mode (deeper, /unity-research-skill-driven, separate phase).

Extracted from orchestrator.py (_validate_hypothesis + _run_hypothesis_loop).
Used by both orchestrate_project and orchestrate_cross_projects — previously
copy-pasted with ~80% similarity.

Profile schema: plan ``PhaseStep.hypothesis`` stores the attempt count and
optional format. The future ``research`` mode (deeper, skill-driven) will own
its own config block when it lands.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from agents.protocols import IAgentRuntime
from core.infra import config as _config
from core.observability import events as _events
from core.observability.logging import (
    C,
    preview_body,
    preview_heading,
    success,
    warn,
)

if TYPE_CHECKING:
    pass


def format_validated_hypothesis_context(hypothesis: str) -> str:
    """Render the planning context appended after hypothesis QA approval.

    A validated hypothesis is a directional input for PLAN/CROSS_PLAN, not
    authorization to skip planning or implementation review.
    """
    return (
        "\n\nVALIDATED HYPOTHESIS (QA-approved planning context):\n"
        "This is a planning input, not execution approval. The plan must "
        "either incorporate this direction, verify/falsify its riskiest "
        "assumption early, or explain why it diverges.\n\n"
        f"{hypothesis}\n"
    )


def format_rejected_hypothesis_feedback(attempts: list[dict]) -> str:
    """Render the planning context appended after hypothesis QA REJECTED
    every attempt.

    A rejected hypothesis is *not* an approved direction. The reviewer's
    findings — risks they raised, missing assumptions they flagged,
    falsification probes they demanded — are still useful planning
    input: they tell the planner what's been ruled out and what
    deserves explicit verification. Surfacing them as **negative**
    context (not direction) lets PLAN/CROSS_PLAN avoid the dead ends
    the reviewer already named, address the missing assumptions, or
    explicitly plan from first principles.

    Use the typed ``attempt["review"]`` dict (verdict / short_summary /
    findings / risks / checks) — REA-3.5 contract surface. Attempts without
    a structured review are ignored; the hypothesis gate does not consume
    prose critique as a fallback contract.
    """
    parts: list[str] = [
        "\n\nREJECTED HYPOTHESIS FEEDBACK (not validated direction):",
        "This is not an approved hypothesis and must not be treated as "
        "implementation direction. Use it only to avoid rejected "
        "reasoning, address reviewer concerns, verify missing "
        "assumptions, or plan from first principles.",
        "",
    ]
    for entry in attempts:
        attempt_no = entry.get("attempt", "?")
        hyp_text = (entry.get("hypothesis") or "").strip()
        review = entry.get("review") or {}
        parts.append(f"--- Rejected attempt #{attempt_no} ---")
        if hyp_text:
            parts.append("Hypothesis text:")
            parts.append(hyp_text)
            parts.append("")
        if review:
            summary = (review.get("short_summary") or "").strip()
            if summary:
                parts.append(f"QA short summary: {summary}")
            findings = review.get("findings") or []
            if findings:
                parts.append("Findings:")
                for f in findings:
                    fid = f.get("id") or f.get("severity") or "F"
                    title = (f.get("title") or "").strip()
                    body = (f.get("body") or "").strip()
                    fix = (f.get("required_fix") or "").strip()
                    head = f"  [{fid}] {title}".rstrip()
                    parts.append(head)
                    if body:
                        parts.append(f"    {body}")
                    if fix:
                        parts.append(f"    Required fix: {fix}")
            risks = review.get("risks") or []
            if risks:
                parts.append("Risks:")
                for r in risks:
                    parts.append(f"  - {r}")
            checks = review.get("checks") or []
            if checks:
                parts.append("Checks the reviewer performed:")
                for c in checks:
                    parts.append(f"  - {c}")
        parts.append("")
    return "\n".join(parts)


def _critique_is_empty(text: str) -> bool:
    """Return True only for an APPROVED reviewer JSON object.

    The typed JSON reviewer contract is the only accepted gate surface.
    """
    from core.contracts.review_schema import ReviewSchemaError
    from pipeline.review_parser import ReviewParseError, parse_review
    try:
        return parse_review(text or "").approved
    except (ReviewSchemaError, ReviewParseError):
        return False


def _hypothesis_temp_dir(cwd: str) -> str | None:
    """Pick a directory for the throwaway hypothesis .md.

    Order:
      1. The active run_dir (runspace/runs/<ts>/) if event-store is alive —
         keeps artefacts inside the workspace, easy to clean up after.
      2. System tempdir (None → tempfile picks $TMPDIR / /tmp).

    NEVER ``cwd`` of the project. Writing into a project tree is harmful:
    Unity / Xcode / hot-reload watchers see "new file", spawn ``.meta``
    siblings, taint git status. The hypothesis artifact-review prompt embeds
    the temp file content inline, so the file location is irrelevant to
    validation.

    ``cwd`` is kept in the signature for backward compat with callers but
    is intentionally ignored.
    """
    del cwd  # noqa: F841 — kept for signature compatibility
    try:
        run_dir = _events.current_run_dir()
    except Exception:
        run_dir = None
    if run_dir and run_dir.is_dir():
        return str(run_dir)
    return None  # tempfile.mkstemp picks $TMPDIR


def _validate_hypothesis(
    qa_agent: IAgentRuntime,
    hypothesis: str,
    task: str,
    cwd: str,
    *,
    review_format: str | None = "detailed",
    continue_session: bool = False,
) -> tuple[bool, str, dict]:
    """Ask the QA reviewer whether ``hypothesis`` is plausible for ``task``.

    Writes the hypothesis to a temp .md file (in run_dir or system tempdir,
    never the project's cwd — see ``_hypothesis_temp_dir``) and submits a
    file-review prompt to the runtime. Returns
    ``(approved, critique_markdown, review_dict)``. ``review_dict`` is the
    structured contract surface the CLI transcript renderer can consume
    directly (``verdict`` / ``short_summary`` / ``findings`` / ``risks`` /
    ``checks``, plus ``parse_error`` on a malformed JSON contract).

    ``continue_session=True`` resumes the QA reviewer's prior bridge —
    caller passes it for hypothesis attempt 2+ so the reviewer keeps
    its memory of the round-1 critique it produced.
    """
    fd, tmp_path = tempfile.mkstemp(
        suffix=".md", prefix="hypothesis_",
        dir=_hypothesis_temp_dir(cwd),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                f"# Hypothesis\n\nTask: {task}\n\n"
                f"## Proposed approach\n\n{hypothesis}\n"
            )
        from pipeline.prompts.builders import hypothesis_file_review_prompt
        # The verifiable text still lives in the temp file and is embedded
        # inline so the reviewer does not need filesystem access to it.
        file_content = Path(tmp_path).read_text(encoding="utf-8")
        review_turn = hypothesis_file_review_prompt(
            tmp_path, file_content, task,
            project_dir=cwd,
            format_name=review_format,
        )
        from core.observability.prompt_trace import set_last_prompt_turn as _set_turn
        _set_turn(review_turn)
        review_prompt = review_turn.text
        try:
            from core.io.stdout_render import defer_assistant_json
            with defer_assistant_json():
                critique = qa_agent.invoke(
                    review_prompt, cwd,
                    continue_session=continue_session,
                )
        except RuntimeError as e:
            err = f"[validate_hypothesis error: {e}]"
            return False, err, {
                "verdict":       "REJECTED",
                "short_summary": err,
                "findings":      [],
                "parse_error":   str(e),
            }
    finally:
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink()
    return _parse_hypothesis_verdict(critique)


def _parse_hypothesis_verdict(critique: str) -> tuple[bool, str, dict]:
    """Parse hypothesis QA output into ``(approved, critique_body, review_dict)``.

    JSON contract path (REA-3.5): the reviewer emits a typed
    :class:`ParsedReview` and we render markdown for human display.
    Malformed structured output (raw or fenced JSON that fails the schema)
    is treated as a hard rejection — the contract is the gate, not prose.
    Non-JSON outputs are treated as hard contract failures.
    """
    from core.contracts.review_schema import ReviewSchemaError
    from pipeline.review_markdown import render_review_markdown
    from pipeline.review_parser import ReviewParseError, parse_review

    raw = critique or ""
    try:
        parsed = parse_review(raw)
    except (ReviewSchemaError, ReviewParseError) as e:
        body = f"validate_hypothesis parse error: {e}\n\nRaw output:\n{raw}"
        return False, body, {
            "verdict":       "REJECTED",
            "short_summary": f"validate_hypothesis parse error: {e}",
            "findings":      [],
            "parse_error":   str(e),
        }
    return (
        parsed.approved,
        render_review_markdown(parsed, title="Hypothesis QA"),
        {
            "verdict":       parsed.verdict,
            "short_summary": parsed.short_summary,
            "findings":      parsed.findings_as_dicts(),
            "risks":         list(parsed.risks),
            "checks":        list(parsed.checks),
        },
    )


def run_hypothesis_loop(
    plan_agent: IAgentRuntime,
    qa_agent: IAgentRuntime,
    task: str,
    cwd: str,
    codemap: str,
    max_hypotheses: int,
    prompt_spec=None,
    hypothesis_format: str | None = None,
) -> tuple[str | None, list[dict]]:
    """Run hypothesis-driven gut-check before the PLAN phase.

    Fires only when ``max_hypotheses > 0``. Returns
    ``(approved_hypothesis, attempts)``.
    ``approved_hypothesis`` is None when every attempt was rejected.

    Shared by:
      - orchestrator.run_pipeline()           (single project)
      - cross_orchestrator.run_cross_pipeline() (cross-project)
    """
    if max_hypotheses <= 0:
        return None, []

    from pipeline.prompts.builders import hypothesis_prompt

    resolved_format = (
        hypothesis_format
        if hypothesis_format is not None
        else (prompt_spec.format if prompt_spec else "detailed")
    )

    attempts: list[dict] = []
    for attempt in range(1, max_hypotheses + 1):
        warn(f"Hypothesis attempt {attempt}/{max_hypotheses}: hypothesizing...")
        # Attempt 2+ resumes both runtime bridges: architect keeps
        # memory of round-1 hypothesis + critique; QA keeps memory of
        # its own verdict so it doesn't repeat the same findings.
        resume_attempt = attempt > 1
        # Compose a single read-only hypothesis prompt and invoke the runtime.
        # Pre-Phase-7 this lived inside ``ClaudeAgent.hypothesize``; the
        # builder is now the canonical entry point.
        _hypo_turn = hypothesis_prompt(
            task,
            cwd,
            codemap=codemap,
            prompt_spec=prompt_spec,
            format_name=resolved_format,
        )
        # ADR 0060: publish the effective PromptTurn at the invoke boundary
        # (debug/transcript honesty) and send the wire string ``turn.text``.
        from core.observability.prompt_trace import set_last_prompt_turn as _set_turn
        _set_turn(_hypo_turn)
        # Print the logical output label before the runtime invocation line so
        # terminal readers see "what phase result is being generated" before
        # the lower-level agent call details.
        preview_heading(f"Hypothesis (attempt {attempt})", C.MAGENTA)
        hypothesis = plan_agent.invoke(
            _hypo_turn.text, cwd,
            continue_session=resume_attempt,
        )
        plan_usage = {
            "tokens_in":   int(getattr(plan_agent, "last_tokens_in", 0) or 0),
            "tokens_out":  int(getattr(plan_agent, "last_tokens_out", 0) or 0),
            "tokens_total": int(getattr(plan_agent, "last_tokens_total", 0) or 0),
        }
        if _config.accounting_enabled():
            plan_usage["cost_usd"] = getattr(plan_agent, "last_cost_usd", None)
        # Emit с полным текстом ДО валидации — UI сразу покажет hypothesis-card
        # "in flight" (ещё без verdict).
        _events.emit(
            "hypothesis.proposed",
            attempt=attempt,
            max=max_hypotheses,
            text=hypothesis,
        )
        # Surface the architect's hypothesis text in stdout — without this
        # the operator only saw the binary verdict, with no idea what was
        # actually proposed (CLI feedback 2026-05-06).
        preview_heading(f"Hypothesis output (attempt {attempt})", C.CYAN)
        preview_body(hypothesis)
        preview_heading(f"Hypothesis QA (attempt {attempt})", C.YELLOW)
        approved, critique, review_dict = _validate_hypothesis(
            qa_agent, hypothesis, task, cwd,
            review_format=resolved_format,
            continue_session=resume_attempt,
        )
        qa_usage = {
            "tokens_in":   int(getattr(qa_agent, "last_tokens_in", 0) or 0),
            "tokens_out":  int(getattr(qa_agent, "last_tokens_out", 0) or 0),
            "tokens_total": int(getattr(qa_agent, "last_tokens_total", 0) or 0),
        }
        if _config.accounting_enabled():
            qa_usage["cost_usd"] = getattr(qa_agent, "last_cost_usd", None)
        attempts.append({
            "attempt": attempt,
            "hypothesis": hypothesis,
            "approved": approved,
            "critique": critique,
            "review":   review_dict,
            # Per-call token/cost snapshots so the caller can roll them
            # up into a cross-run metrics rollup. ``plan_usage`` covers
            # the architect invoke; ``qa_usage`` covers the validator.
            # Each agent's ``last_*`` attrs are read RIGHT after its
            # invoke — they're overwritten on the next call, so per-attempt
            # capture is the only way to preserve them across retries.
            "plan_usage": plan_usage,
            "qa_usage":   qa_usage,
        })
        # Verdict event — UI обновит badge/critique карточки гипотезы.
        _events.emit(
            "hypothesis.verdict",
            attempt=attempt,
            approved=approved,
            critique=critique[:2000] if critique else "",
            reviewer_provider=type(qa_agent).__name__,
        )
        # REA-3.6 follow-up: render the typed reviewer contract
        # structurally instead of dumping the rendered-markdown blob.
        # The transcript renderer surfaces verdict, short_summary,
        # findings, risks, and checks as scannable rows; full bodies
        # are still preserved.
        if review_dict:
            from core.io.transcript import render_review_block
            print(render_review_block(
                review_dict, title=None,
            ))
        elif critique:
            preview_body(critique)
        if approved:
            success(f"Planning direction validated on attempt {attempt}")
            return hypothesis, attempts
        warn(f"Hypothesis attempt {attempt} rejected by QA")

    warn(
        f"All {max_hypotheses} hypotheses rejected -- planning with "
        "rejection feedback, no validated direction"
    )
    _events.emit("hypothesis.exhausted", attempts=len(attempts), max=max_hypotheses)
    return None, attempts


def maybe_run_hypothesis(
    *,
    task: str,
    cwd: str,
    codemap: str,
    dry_run: bool,
    plan_agent: IAgentRuntime,
    qa_agent: IAgentRuntime,
    prompt_spec=None,
    hypothesis_format: str | None = None,
    override_enabled: bool | None = None,
    override_max_attempts: int | None = None,
) -> tuple[str | None, list[dict]]:
    """Gate + run the hypothesis loop for direct callers.

    Profile dispatch passes explicit overrides from the owning plan
    ``PhaseStep.hypothesis``. ``None`` still falls through to
    ``AppConfig.hypothesis`` for narrow direct/unit-test callers.

    Returns ``(hypothesis | None, attempts)``. Convenience wrapper so
    orchestrators don't repeat the config-loading try/except.
    """
    if dry_run:
        return None, []
    try:
        hyp_cfg: dict = _config.AppConfig.load().hypothesis or {}
    except Exception:
        hyp_cfg = {}

    enabled = (
        override_enabled
        if override_enabled is not None
        else bool(hyp_cfg.get("enabled", False))
    )
    if not enabled:
        return None, []

    max_attempts = (
        override_max_attempts
        if override_max_attempts is not None
        else int(hyp_cfg.get("max_attempts", 3))
    )
    return run_hypothesis_loop(
        plan_agent=plan_agent,
        qa_agent=qa_agent,
        task=task,
        cwd=cwd,
        codemap=codemap,
        max_hypotheses=max_attempts,
        prompt_spec=prompt_spec,
        hypothesis_format=hypothesis_format,
    )
