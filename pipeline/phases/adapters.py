"""
pipeline/phases.py — Isolated phase runners.

Wave 3 extracts the per-phase work out of ``run_pipeline`` into small,
side-effect-free functions. Each runner takes the agent it should drive plus
just enough context to do its job and returns a structured result. The
orchestrator stitches them together; tests can call them in isolation.

Goals:
  * No I/O surprises — every runner accepts ``dry_run`` and respects it.
  * No global state — agents and the plugin are passed in.
  * Composable — swap a runner in tests without monkey-patching the module.

The orchestrator still owns the high-level control flow (rounds, session
mode selection, progress logging). These runners are deliberately thin.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agents.protocols import IAgentRuntime
from pipeline import prompts
from pipeline.plugins import PluginConfig
from pipeline.prompts.turn import (
    PromptTurn,
    PromptTurnEditor,
    hypothesis_suffix_part,
)
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)

if TYPE_CHECKING:
    from pipeline.runtime import PromptSpec
    from pipeline.runtime.steps import Attachment


# ── Result types ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PhaseResult:
    """Generic result envelope for a single phase.

    ``output`` is the raw text the phase produced (plan markdown, fix output,
    review critique, ...). ``meta`` holds phase-specific extras the caller
    wants in the session log.
    """
    name: str
    output: str
    meta: dict


def _runtime_session_meta(
    agent: IAgentRuntime, *, continue_session: bool
) -> dict[str, object]:
    """Build the runtime session metadata for a phase result.

    ``continue_session`` is the disposition the runner actually passed to the
    invoke — the result of the session-disposition policy at the call site —
    reflected here verbatim rather than independently re-derived from
    ``agent._last_resumed_session_id``. The runtime's resume primitive can
    no-op (e.g. burned bridge / no captured session) while the policy intent
    was still "continue", so the policy disposition is the truthful value for
    this metadata.
    """
    meta: dict[str, object] = {
        "session_id": getattr(agent, "session_id", None),
        "continue_session": continue_session,
    }
    parent_session_id = getattr(agent, "_last_followup_parent_session_id", None)
    if parent_session_id is not None:
        meta["followup_parent_session_id"] = parent_session_id
    return meta


def _invoke_turn(
    agent: IAgentRuntime,
    turn: PromptTurn,
    cwd: str,
    **kwargs,
) -> str:
    """Publish *turn* for transcript/debug and invoke with its wire text."""
    from core.observability.prompt_trace import set_last_prompt_turn

    set_last_prompt_turn(turn)
    return agent.invoke(turn.text, cwd, **kwargs)


def _text_prefix_part(body: str) -> PromptPart:
    sig = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return PromptPart(
        kind="text_prefix",
        name="attachments",
        source="artifact",
        body=body,
        layer=PromptLayer.BOOTSTRAP,
        stability=PromptStability.PROFILE,
        cache_scope=PromptCacheScope.WORKSPACE,
        volatile_reason="depends on per-run TEXT attachments",
        id=f"text_prefix:attachments:v1:{sig}",
    )


def _codemap_part(body: str) -> PromptPart:
    return PromptPart(
        kind="codemap",
        name="repo_map",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="repo map content varies per run; adapter-local injection",
        id="codemap:repo_map",
    )


def _generic_suffix_part(body: str) -> PromptPart:
    return PromptPart(
        kind="prompt_suffix",
        name="phase_extension",
        source="artifact",
        body=body.lstrip("\n"),
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="phase adapter suffix varies by invocation",
        id="prompt_suffix:phase_extension",
    )


def _hybrid_codemap_part(repo_map: str) -> PromptPart:
    body = (
        "--- REPO MAP (best-effort outline; full file access still available) ---\n"
        f"{repo_map}\n"
        "--- END REPO MAP ---"
    )
    return PromptPart(
        kind="codemap",
        name="hybrid_repo_map",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="hybrid repair repo map varies by invocation",
        id="codemap:hybrid_repo_map",
    )


def _approved_review_json(short_summary: str) -> str:
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": short_summary,
        "findings":      [],
        "risks":         [],
        "checks":        [],
    })


def _approved_release_json(short_summary: str) -> str:
    """Dry-run synthesis for the release gate (ADR 0025).

    Mirrors :func:`_approved_review_json` but emits the release-shape
    contract so ``parse_release`` accepts the dry-run output without
    halting the handler on a shape mismatch.
    """
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      short_summary,
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "sufficient",
        },
    })


# ── Runners ───────────────────────────────────────────────────────────────────
def run_plan(
    agent: IAgentRuntime,
    task: str,
    project_dir: str,
    plugin: PluginConfig,
    *,
    codemap: str = "",
    prompt_suffix: str = "",
    prompt_prefix: str = "",
    change_handoff: str = "uncommitted",
    dry_run: bool = False,
    prompt_spec: PromptSpec | None = None,
    attachments: tuple[Attachment, ...] = (),
    continue_session: bool = False,
    verification_part: PromptPart | None = None,
) -> PhaseResult:
    """Phase 0: PLAN.

    Returns the full plan markdown in ``output``. ``codemap`` is appended to
    the prompt when non-empty so the architect doesn't burn tokens
    rediscovering the project layout. ``prompt_suffix`` is a free-form
    extension hook (e.g. validated hypothesis, project conventions) appended
    after the base prompt and codemap. ``prompt_prefix`` (Phase 4.5) is
    prepended before the base prompt — used to inject TEXT attachments
    via ``pipeline.attachment_inject.render_text_block``.

    ``continue_session=True`` resumes the architect's prior bridge —
    handler passes it for round 2+ of the plan/replan loop so the agent
    keeps its memory of round-1 attempt and critique.
    """
    if dry_run:
        return PhaseResult(name="plan", output="[DRY RUN]", meta={"dry_run": True})

    turn = prompts.plan_prompt(
        task, project_dir, plugin,
        change_handoff=change_handoff,
        prompt_spec=prompt_spec,
        verification_part=verification_part,
    )
    editor = PromptTurnEditor(turn)
    if codemap:
        editor.append(_codemap_part(
            f"--- REPO MAP ---\n{codemap}\n--- END REPO MAP ---",
        ))
    if prompt_suffix:
        if "hypothesis" in prompt_suffix.lower():
            editor.append(hypothesis_suffix_part(prompt_suffix))
        else:
            editor.append(_generic_suffix_part(prompt_suffix))
    if prompt_prefix:
        editor.prepend(_text_prefix_part(prompt_prefix))
    turn = editor.build()

    # Plan is a read-only invocation. ``mutates_artifacts`` stays False
    # (default) so the runtime drops the Claude write flags. The fully-
    # composed prompt already includes any TEXT prefix/suffix.
    output = _invoke_turn(
        agent, turn, project_dir,
        continue_session=continue_session,
        attachments=attachments,
    )
    return PhaseResult(
        name="plan",
        output=output,
        meta=_runtime_session_meta(agent, continue_session=continue_session),
    )


def run_build(
    agent: IAgentRuntime,
    task: str,
    project_dir: str,
    plugin: PluginConfig,
    *,
    plan_contract: str = "",
    plan_tasks: str = "",
    handoff_contract: str = "",
    change_handoff: str = "uncommitted",
    dry_run: bool = False,
    prompt_spec: PromptSpec | None = None,
    attachments: tuple[Attachment, ...] = (),
    verification_part: PromptPart | None = None,
) -> PhaseResult:
    """implement runner. First call in the chain — never resumes.

    ``plan_contract`` and ``plan_tasks`` are the typed plan handoff
    views rendered from ``ParsedPlan``. The developer must see both
    the acceptance contract and the concrete subtask decomposition.
    """
    if dry_run:
        return PhaseResult(name="implement", output="[DRY RUN]", meta={"dry_run": True})

    turn = prompts.build_prompt(
        task, project_dir, plugin,
        plan_contract=plan_contract,
        plan_tasks=plan_tasks,
        handoff_contract=handoff_contract,
        change_handoff=change_handoff,
        prompt_spec=prompt_spec,
        verification_part=verification_part,
    )
    output = _invoke_turn(
        agent, turn, project_dir,
        mutates_artifacts=True, attachments=attachments,
    )
    # implement is the first call in the chain — it never resumes.
    return PhaseResult(
        name="implement",
        output=output,
        meta=_runtime_session_meta(agent, continue_session=False),
    )


def run_review(
    agent: IAgentRuntime,
    task: str,
    cwd: str,
    plugin: PluginConfig,
    *,
    plan_contract: str = "",
    plan_tasks: str = "",
    handoff_contract: str = "",
    dry_run: bool = False,
    label: str = "review_changes",
    require_verdict: bool = False,
    change_handoff: str = "uncommitted",
    prompt_spec: PromptSpec | None = None,
    attachments: tuple[Attachment, ...] = (),
    continue_session: bool = False,
    repair_receipt: str = "",
    current_review_subject: str = "",
    operator_waiver: str = "",
    output_contract: Literal["review", "release"] = "review",
    verification_part: PromptPart | None = None,
    readiness_summary: str = "",
) -> PhaseResult:
    """review_changes / final_acceptance / validate_plan runner.

    ``readiness_summary`` (ADR 0082, final_acceptance only) carries the
    pre-rendered Stage 5 verification-readiness block; empty adds no part.

    The reviewer receives both typed plan handoff views: the
    plan-level contract and the concrete subtask decomposition.

    ADR 0009 / A5.2a: ``prompt_spec`` flows down from the active
    ``PhaseStep.prompt`` so profiles can override the composed review
    prompt without forking ``review_focus``. The spec must carry an
    explicit prompt role — profile steps no longer carry the old
    runtime-role fallback. ``None`` means the builder's prompt-taxonomy
    default kicks in.

    ``continue_session=True`` resumes the reviewer's prior bridge —
    handler passes it for round 2+ of the validate_plan / review_changes
    loops so the reviewer doesn't re-explain context from round 1 and
    can focus on "did you fix what I asked".
    """
    if dry_run:
        # ADR 0025: when the caller selected the release contract, emit a
        # release-shaped dry-run payload so the downstream parser
        # (``pipeline.release_parser.parse_release``) accepts it.
        # Otherwise the original review payload — every existing caller
        # path is on review_json by default.
        synth = (
            _approved_release_json if output_contract == "release"
            else _approved_review_json
        )
        return PhaseResult(
            name=label,
            output=synth(f"{label} dry run skipped reviewer invocation."),
            meta={"dry_run": True, "output_contract": output_contract},
        )

    focus = prompts.review_focus(
        task,
        plugin,
        require_verdict=require_verdict,
        change_handoff=change_handoff,
        prompt_spec=prompt_spec,
        output_contract=output_contract,
        verification_part=verification_part,
    )
    # Reviewer invocation: caller delivers the fully-composed focus/prompt;
    # the agent still does its own filesystem inspection (git diff, file reads)
    # via its tool surface — we don't pre-feed the diff.
    from pipeline.prompts.builders import runtime_review_uncommitted_prompt
    focus_text = focus.text
    review_turn = runtime_review_uncommitted_prompt(
        focus_text,
        project_dir=cwd,
        plan_contract=plan_contract,
        plan_tasks=plan_tasks,
        handoff_contract=handoff_contract,
        change_handoff=change_handoff,
        repair_receipt=repair_receipt,
        current_review_subject=current_review_subject,
        verification_readiness=readiness_summary,
        operator_waiver=operator_waiver,
        output_contract=output_contract,
    )
    output = _invoke_turn(
        agent, review_turn, cwd,
        continue_session=continue_session,
        attachments=attachments,
    )
    return PhaseResult(
        name=label,
        output=output,
        meta=_runtime_session_meta(agent, continue_session=continue_session),
    )


def run_fix(
    agent: IAgentRuntime,
    task: str,
    critique: str,
    project_dir: str,
    plugin: PluginConfig,
    *,
    test_failures: str = "",
    write_style: str = "",
    continue_session: bool = False,
    hybrid_codemap: str = "",
    plan_contract: str = "",
    plan_tasks: str = "",
    handoff_contract: str = "",
    change_handoff: str = "uncommitted",
    dry_run: bool = False,
    prompt_spec: PromptSpec | None = None,
    attachments: tuple[Attachment, ...] = (),
    verification_part: PromptPart | None = None,
) -> PhaseResult:
    """repair_changes runner.

    ``continue_session=True`` reuses the agent's previous session via
    ``--resume`` (CHAIN mode). The agent silently falls back to stateless
    if no session was captured upstream.

    ``hybrid_codemap`` re-primes the prompt with a repo outline when running
    HYBRID (different model than implement, can't reuse session). Empty
    string is a no-op.
    """
    if dry_run:
        return PhaseResult(name="repair_changes", output="[DRY RUN]", meta={"dry_run": True})

    turn = prompts.fix_prompt(
        task, critique, project_dir, plugin,
        test_failures=test_failures,
        write_style=write_style,
        plan_contract=plan_contract,
        plan_tasks=plan_tasks,
        handoff_contract=handoff_contract,
        change_handoff=change_handoff,
        prompt_spec=prompt_spec,
        verification_part=verification_part,
    )
    if hybrid_codemap:
        turn = PromptTurnEditor(turn).append(
            _hybrid_codemap_part(hybrid_codemap),
        ).build()
    output = _invoke_turn(
        agent, turn, project_dir,
        mutates_artifacts=True,
        continue_session=continue_session,
        attachments=attachments,
    )
    return PhaseResult(
        name="repair_changes",
        output=output,
        meta=_runtime_session_meta(agent, continue_session=continue_session),
    )
