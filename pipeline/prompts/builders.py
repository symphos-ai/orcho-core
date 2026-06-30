"""
pipeline/prompts/builders.py — High-level prompt builders for each phase.

Prompt text lives in ``_prompts/`` as composable parts:
``roles/<role>.md`` + ``tasks/<task>.md`` + ``formats/<format>.md``.
ADR 0009 Phase 8 removed the legacy flat templates; every shipped
builder renders through ``pipeline.prompts.composer.render_composed_prompt``.

This module is responsible only for:
  - collecting variables from PluginConfig / caller arguments
  - calling render_composed_prompt() with the right PromptSpec
  - returning the rendered string

Do NOT put prompt text here. If you need to change wording, edit
``_prompts/<layer>/<name>.md``.
"""

from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pipeline.plan_parser import ParsedPlan

from core.infra.config import AppConfig
from core.observability import prompt_trace
from pipeline.allowed_modifications import render_allowed_modifications
from pipeline.plugins import PluginConfig
from pipeline.prompts import minimal_intents
from pipeline.prompts.composer import (
    PromptSpec,
    assemble_cache_first_segments,
    render_composed_prompt,
)
from pipeline.prompts.contracts import (
    SystemPromptBlock,
    advisory_critique_reconciliation_text,
    authoring_language_strategy,
    change_handoff_strategy,
    operator_waiver_reconciliation_text,
    plan_artifact_boundary_contract,
    plan_json_contract,
    release_json_contract,
    review_json_contract,
    review_target_strategy,
    skill_routing_strategy,
)
from pipeline.prompts.modes import (
    ProfessionalPromptMode,
    coerce_professional_prompt_mode,
)
from pipeline.prompts.turn import PromptTurn
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)

# ADR 0025: explicit kwarg threading replaces heuristic inference.
# The release gate (project ``final_acceptance``) selects
# ``release_json_contract``; every other reviewer surface
# (validate_plan, review_changes, hypothesis QA, validate_cross_plan,
# contract_check) stays on ``review_json_contract``.
OutputContract = Literal["review", "release"]


def _select_json_contract(
    output_contract: "OutputContract",
    *,
    body_language: str | None,
) -> SystemPromptBlock:
    """Single source of truth for routing builder system-tails between
    review and release contracts. Heuristic inference is forbidden by
    the guardrail in ADR 0025; only the explicit kwarg selects."""
    if output_contract == "release":
        return release_json_contract(body_language=body_language)
    if output_contract == "review":
        return review_json_contract(body_language=body_language)
    raise ValueError(
        f"output_contract must be 'review' or 'release', got {output_contract!r}"
    )


def _plugin_context_block(plugin: PluginConfig) -> str:
    """Builds a project context block from plugin data."""
    parts = []
    if plugin.language:
        parts.append(f"Language: {plugin.language}")
    if plugin.architecture:
        parts.append(f"Architecture: {plugin.architecture}")
    if plugin.file_hints:
        parts.append(f"Key directories/files: {', '.join(plugin.file_hints)}")
    if not parts:
        return ""
    return "Project context:\n" + "\n".join(f"  - {p}" for p in parts)


def _same_path(left: str, right: str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def _project_dir_is_active_worktree(project_dir: str) -> bool:
    from pipeline.engine.worktree import get_active_worktree_checkout

    active = get_active_worktree_checkout()
    return bool(active and _same_path(project_dir, active))


def _project_context_body(
    project_dir: str | None, plugin: PluginConfig | None,
) -> str:
    """Render the project_context body from runtime project facts.

    ADR 0028 / M10.5: the "you are working on project at: X" anchor +
    plugin context block used to live inside the role file as
    ``$project_dir`` / ``$context`` substitutions. The substitution
    demoted the role to RUN/WORKSPACE and put run-specific bytes in
    the first part of the wire prompt. M10.5 lifts the run facts out
    of the role and serves them as a typed :class:`PromptPart` with
    ``stability=STATIC`` and ``cache_scope=PROJECT`` — body/hash
    invalidation, scope-stable across runs within the same project.
    """
    lines: list[str] = []
    if project_dir:
        if _project_dir_is_active_worktree(project_dir):
            lines.extend((
                f"You are working in an isolated git worktree checkout at: {project_dir}",
                "This checkout was created from the source repository; make task changes here, not in the source checkout.",
                "Review and test against this checkout unless the task explicitly says otherwise.",
            ))
        else:
            lines.extend((
                f"You are working directly in the project directory at: {project_dir}",
                "Make task changes in this directory and review/test against this directory.",
            ))
    if plugin is not None:
        ctx = _plugin_context_block(plugin)
        if ctx:
            lines.append(ctx)
    return "\n".join(lines)


def _project_context_part(
    project_dir: str | None, plugin: PluginConfig | None,
) -> PromptPart | None:
    """Return a typed PromptPart for project_context, or ``None`` when empty.

    Metadata: ``layer=CONTEXT``, ``stability=STATIC``,
    ``cache_scope=PROJECT``. ``source="code-owned"`` because the
    body layout (anchor sentence + plugin block) is a code-defined
    template; the embedded values are run inputs, not earlier-phase
    artifacts. The ADR-0026 validation rule allows STATIC + non-NONE
    cache_scope without ``volatile_reason``, so the part lives in
    the stable prefix and invalidates via body bytes when project /
    plugin facts change.
    """
    body = _project_context_body(project_dir, plugin)
    if not body:
        return None
    return PromptPart(
        kind="context",
        name="project",
        source="code-owned",
        body=body,
        layer=PromptLayer.CONTEXT,
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.PROJECT,
        id="context:project",
    )


def _workspace_context_part() -> PromptPart | None:
    """Scaffold for workspace-level context.

    ADR 0028 / M10.5: reserved slot for workspace-level AGENTS /
    injection content. There is no consumer in this milestone, so
    the helper returns ``None`` and the part is suppressed. The
    builder gateway calls this for symmetry with
    :func:`_project_context_part` so the assembler ordering pins a
    fixed slot — a future workspace-context source plugs in here
    without per-builder changes.
    """
    return None


def _prepend_block(block: str, prompt: str) -> str:
    """Prepend a rendered context block before the user-level prompt."""
    return f"{block}\n{prompt}" if block else prompt


def _with_verification_part(
    extra_parts: tuple[PromptPart, ...],
    verification_part: PromptPart | None,
) -> tuple[PromptPart, ...]:
    """Append the optional verification-contract part to a builder's extras.

    ``verification_part`` is the typed dynamic RUN-scoped block prepared by the
    phase handler (it owns ``state``; builders do not). When ``None`` the extras
    are returned unchanged so the prompt bytes stay byte-identical to the
    no-contract path.
    """
    if verification_part is None:
        return extra_parts
    return extra_parts + (verification_part,)


def _render_prompt_output(
    user_prompt: str,
    *,
    system_tail: tuple[SystemPromptBlock | None, ...] = (),
    extra_upper_parts: tuple[PromptPart, ...] = (),
    prefix_upper_parts: tuple[PromptPart, ...] = (),
    project_dir: str | None = None,
    plugin: PluginConfig | None = None,
) -> PromptTurn:
    """Single output gateway for every prompt builder in this module.

    ADR 0028 / M10.5 / ADR 0060: every call routes through
    :func:`pipeline.prompts.composer.assemble_cache_first_segments`.
    The assembler is the single ordering authority. The wire byte order is:

    1. Prefix-eligible parts, sorted by cache breadth
       (GLOBAL → WORKSPACE → PROJECT → SESSION) and then kind sub-order
       (protected contracts → role → format → task method → context).
    2. Non-eligible parts, sorted the same way.

    Returns a :class:`PromptTurn` whose ``.text`` is the wire-identical
    prompt string.  Callers use ``turn.text`` only at the
    ``agent.invoke`` boundary; all other surfaces (session selection,
    debug transcript) consume the turn object directly.

    Builders never publish the prompt-trace contextvar; only the invoke
    boundary (:func:`pipeline.phases.builtin._session_aware_invoke` /
    :func:`pipeline.cross_project.session_invoke.session_aware_invoke`)
    calls :func:`~core.observability.prompt_trace.set_last_prompt_turn`.
    """
    context_parts: list[PromptPart] = []
    ws_part = _workspace_context_part()
    if ws_part is not None:
        context_parts.append(ws_part)
    proj_part = _project_context_part(project_dir, plugin)
    if proj_part is not None:
        context_parts.append(proj_part)

    # Composer-rendered editable parts (role / task method / format).
    # Empty when the builder ran a MINIMAL ablation path; wrap
    # ``user_prompt`` as a ``minimal_intent`` PromptPart in that case.
    composer_upper = prompt_trace.take_last_upper() or ()
    minimal_intent_parts: tuple[PromptPart, ...] = ()
    if not composer_upper:
        minimal_intent_parts = (
            PromptPart(
                kind="minimal_intent",
                name="(minimal)",
                source="code-owned",
                body=user_prompt,
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="minimal-intent fallback embeds user prompt",
            ),
        )

    # System-tail (protected contracts) as typed PromptParts.
    blocks = tuple(b for b in system_tail if b is not None)
    tail_parts: tuple[PromptPart, ...] = tuple(
        PromptPart(
            kind="system_tail",
            name=block.name,
            source="code-owned",
            body=block.render(),
            version=block.version,
            id=block.id,
            layer=block.layer,
            stability=block.stability,
            cache_scope=block.cache_scope,
            volatile_reason=block.volatile_reason,
        )
        for block in blocks
    )

    all_parts: list[PromptPart] = []
    all_parts.extend(context_parts)
    all_parts.extend(prefix_upper_parts)
    all_parts.extend(composer_upper)
    all_parts.extend(minimal_intent_parts)
    all_parts.extend(extra_upper_parts)
    all_parts.extend(tail_parts)

    return assemble_cache_first_segments(all_parts)


def _default_change_handoff() -> str:
    """Global prompt fallback when no profile-owned strategy is provided."""
    value = AppConfig.load().pipeline.get("change_handoff", "uncommitted")
    return str(value or "uncommitted")


def _split_embedded_system_tail(prompt: str) -> tuple[str, str]:
    """Remove every ``<orcho:system-block ...>...</orcho:system-block>``
    block from *prompt* and return ``(non_contract_text, contracts_text)``.

    Used by the runtime review wrapper to dedupe protected contracts
    when the wrapper's focus input already carries some (because the
    upstream ``review_focus`` builder ran end-to-end and emitted the
    fully composed wire).

    Pre-Step-5 the wrapper assumed contracts trailed the focus, so a
    single ``prompt[:idx]`` split was enough. ADR 0028 / M10.5 Step 5
    moves protected contracts to the leading edge of the wire and
    interleaves them across cache tiers — there may now be multiple
    ``<orcho:system-block ...>`` regions in the focus. Strip them
    all so the wrapper's own contract attachment remains the single
    source of truth.
    """
    import re

    block_pattern = re.compile(
        r"<orcho:system-block\b[^>]*>.*?</orcho:system-block>",
        re.DOTALL,
    )
    contracts = "\n\n".join(m.group(0) for m in block_pattern.finditer(prompt))
    cleaned = block_pattern.sub("", prompt)
    # Collapse the blank lines that the removed blocks left behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, contracts.strip()


def _review_target_mode_from_tail(system_tail: str) -> str | None:
    for mode in ("uncommitted", "commit", "commit_set"):
        if f"Review target mode: {mode}." in system_tail:
            return mode
    return None


def _render_format_only(
    format_name: str | None,
    *,
    project_dir: str | None,
    variables: dict[str, object],
) -> str:
    """Render just the ``formats/<name>.md`` part for MINIMAL_WITH_FORMAT.

    The mode keeps the format preset attached to the rendered prompt
    while skipping the role/task professional method. Returns an
    empty string when ``format_name`` is ``None`` — at the call site
    the helper acts as a no-op append, so MINIMAL_WITH_FORMAT degrades
    cleanly to MINIMAL when the builder's ``PromptSpec`` has no
    format set.
    """
    if not format_name:
        return ""
    from core.io import prompt_loader

    return prompt_loader.render_prompt(
        f"formats/{format_name}",
        project_dir=project_dir,
        **variables,
    ).strip()


def _append_format(intent: str, format_part: str) -> str:
    """Join a minimal intent with a rendered format preset.

    Empty ``format_part`` leaves the intent untouched.
    """
    if not format_part:
        return intent
    return f"{intent.rstrip()}\n\n{format_part}"


def hypothesis_prompt(
    task: str,
    project_dir: str,
    codemap: str = "",
    *,
    prompt_spec: PromptSpec | None = None,
    format_name: str | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
) -> PromptTurn:
    """HYPOTHESIS: architect emits a short pre-plan root-cause guess.

    ADR 0009 Phase 6A: composed from ``roles/systems_architect`` +
    ``tasks/hypothesis`` + ``formats/terse`` (3-5 line response, no
    preamble). A plan step may pass its ``prompt_spec`` and optional
    hypothesis-specific ``format_name`` so the prelude keeps the same
    persona while selecting the profile-owned detail style.

    ``professional_prompt_mode`` is an internal ablation knob (see
    :mod:`pipeline.prompts.modes`). ``MINIMAL`` swaps the composed
    parts for a code-owned minimal intent; system-tail unchanged.
    """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    # ADR 0028 / M10.5 Step 2: task file no longer substitutes $task /
    # $codemap_section. The runtime invocation values arrive as a
    # typed turn_input part in FULL mode; MINIMAL paths still bake
    # them into the code-owned intent string (Guard B).
    variables = {"task_language": cfg.task_language}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        spec = (
            _dc_replace(
                prompt_spec,
                task="hypothesis",
                format=format_name if format_name is not None else prompt_spec.format,
            )
            if prompt_spec is not None
            else PromptSpec(
                role="systems_architect",
                task="hypothesis",
                format=format_name or "terse",
            )
        )
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir,
            variables=variables,
        )
        task_body = (
            f"TASK:\n{task}\n\nREPO MAP:\n{codemap}"
            if codemap else f"TASK:\n{task}"
        )
        extra_parts = tuple(
            p for p in (_turn_input_part("hypothesis_task", task_body),)
            if p is not None
        )
    else:
        intent = minimal_intents.hypothesis_intent(task, codemap=codemap)
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    "terse", project_dir=project_dir, variables=variables,
                ),
            )
        else:
            rendered = intent
    return _render_prompt_output(
        rendered,
        system_tail=(
            authoring_language_strategy(task_language=cfg.task_language),
        ),
        extra_upper_parts=extra_parts,
        project_dir=project_dir,
        plugin=PluginConfig(),
    )


def hypothesis_review_focus(
    task: str,
    project_dir: str = "",
    *,
    format_name: str | None = "detailed",
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
) -> PromptTurn:
    """HYPOTHESIS QA: reviewer validates a short architect hypothesis.

    ADR 0009 Phase 6B: composed from ``roles/code_reviewer`` +
    ``tasks/validate_hypothesis`` + a caller-selected format. The
    reviewer JSON output contract stays code-owned via
    ``review_json_contract`` in the system-tail. """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    # ADR 0028 / M10.5 Step 2: task file static; task text arrives as
    # a typed turn_input part in FULL mode.
    variables: dict[str, object] = {}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            PromptSpec(
                role="code_reviewer",
                task="validate_hypothesis",
                format=format_name,
            ),
            project_dir=project_dir or None,
            variables=variables,
        )
        extra_parts = tuple(
            p for p in (
                _turn_input_part(
                    "validate_hypothesis_task", f"TASK:\n{task}",
                ),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.hypothesis_review_focus_intent(task)
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    format_name,
                    project_dir=project_dir or None,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    return _render_prompt_output(
        rendered,
        system_tail=(review_json_contract(body_language=cfg.task_language),),
        extra_upper_parts=extra_parts,
        project_dir=project_dir or None,
        plugin=PluginConfig(),
    )


def _turn_input_part(name: str, body: str) -> PromptPart | None:
    """Wrap a builder-supplied invocation parameter as a TURN/NONE part.

    ADR 0028 / M10.5 Step 2: ``tasks/*.md`` files are now pure static
    method prose. Runtime values that used to be ``$task`` / ``$focus``
    / ``$body`` / ``$critique`` / etc. substitutions inside the task
    file arrive as typed ``turn_input`` parts emitted by the builder.

    Metadata: ``layer=TURN``, ``stability=TURN``, ``cache_scope=NONE``.
    The part always re-sends on every round; it is not cacheable and
    is excluded from ``stable_prefix_parts`` by
    :func:`envelope.is_prefix_eligible`.

    ``source="code-owned"`` because the body framing string (e.g.
    ``"TASK TO PLAN: <task>"``) is composed in code; the embedded
    value is the user/runtime invocation parameter. Step 3 will
    decide whether builder-composed framings deserve a distinct
    ``"artifact"`` provenance label.

    Empty / whitespace-only ``body`` returns ``None`` so callers
    naturally suppress the part (e.g. an optional ``$extra_step``
    that the plugin did not configure).
    """
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="turn_input",
        name=name,
        source="code-owned",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="builder-supplied invocation parameter; per-turn",
        id=f"turn_input:{name}",
    )


def _reviewer_critique_part(body: str) -> PromptPart | None:
    """Wrap reviewer findings from a prior ``validate_plan`` gate as a TURN/NONE part.

    Distinct from generic ``feedback`` (which still carries repair diffs,
    test output, etc.). A separate ``kind`` keeps the seam trace honest
    and lets ``tasks/replan.md`` reconcile reviewer critique against
    human feedback explicitly.

    Empty body returns ``None`` (no reviewer critique → no part).
    """
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="reviewer_critique",
        name="validate_plan_findings",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="reviewer critique; per-turn",
        id="reviewer_critique:validate_plan_findings",
    )


def _human_feedback_part(body: str) -> PromptPart | None:
    """Wrap operator-supplied feedback from a phase-handoff decision as a TURN/NONE part.

    Operator instruction is authoritative guidance — not a reviewer
    artifact. A dedicated ``kind`` + ``source="operator"`` keeps it
    distinct from reviewer critique in the wire prompt and trace, so
    ``tasks/replan.md`` can teach reconciliation without the framing
    string lying about origin.

    Empty body returns ``None`` (no operator feedback → no part).
    """
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="human_feedback",
        name="operator_feedback",
        source="operator",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="operator phase-handoff feedback; per-turn",
        id="human_feedback:operator_feedback",
    )


def _operator_waiver_part(body: str) -> PromptPart | None:
    """Wrap a ``continue_with_waiver`` operator verdict as a TURN/NONE part.

    The operator accepted a REJECTED verdict with a durable waiver; the
    waived findings must not be reopened as blocking by downstream review
    gates. A dedicated ``kind="operator_waiver"`` + ``source="operator"``
    keeps it distinct from reviewer critique and ordinary operator
    feedback in the wire prompt and trace. The reconciliation policy text
    (how the reviewer must treat the waiver) is composed by the caller
    from the code-owned contract; this part only carries the body.

    Empty body returns ``None`` (no waiver → no part).
    """
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="operator_waiver",
        name="operator_waiver",
        source="operator",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="operator phase-handoff waiver; per-turn",
        id="operator_waiver:operator_waiver",
    )


def _feedback_part(name: str, body: str) -> PromptPart | None:
    """Wrap reviewer critique / test output / repair diff as a TURN/NONE part.

    ADR 0028 / M10.5 Step 2: feedback content used to ride inside
    ``tasks/<replan|repair_changes|cross_replan>.md`` via ``$critique``
    / ``$body`` substitutions, which forced the entire task method
    into TURN classification and reorder-friendly bytes into the
    payload. After Step 2 the task file carries only the static
    method; feedback arrives as a separate typed part.

    ADR 0028 / M10.5 Step 3 provenance rule: the body is by
    definition output from a prior runtime phase (reviewer agent
    critique, test runner output, repair diff, round notes assembled
    from earlier-phase data). ``source="artifact"`` reflects that
    truthfully — the body is NOT a code/template constant and
    ``code-owned`` would lie about its origin to the debug renderer
    and M12 trace.

    Empty body returns ``None`` (no feedback this round → no part).
    """
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="feedback",
        name=name,
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason=(
            "reviewer feedback / test output / repair diff; per-turn"
        ),
        id=f"feedback:{name}",
    )


def _repair_receipt_part(body: str) -> PromptPart | None:
    """Wrap the repairer's structured response for the next review pass."""
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="repair_receipt",
        name="latest",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="post-repair receipt; must be re-sent to re-review",
        id="repair_receipt:latest",
    )


def _verification_receipt_part(body: str) -> PromptPart | None:
    """Wrap the developer-side verification-environment receipt summary.

    ADR 0076: a brief digest of what the implement / repair phases
    verified (interpreter, cwd, checks, commands, temp-env location) so
    the reviewer can trust substantiated checks instead of re-deriving the
    environment. Empty body → no part (reviewer verifies manually).
    """
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="verification_receipt",
        name="latest",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="developer-side verification receipt summary for review",
        id="verification_receipt:latest",
    )


def _verification_readiness_part(body: str) -> PromptPart | None:
    """Wrap the final-acceptance verification-readiness summary (ADR 0082).

    Stage 5: a read-only digest of declared-receipt readiness (env status,
    scheduled delivery gates, required receipts classified present / missing /
    failed / stale, exploratory-command count) so the final reviewer reasons
    from official proof instead of ad-hoc host commands. Distinct from
    ``verification_receipt`` (the developer-side env probe digest used by
    ``review_changes``). Empty body → no part (no contract / nothing to show).
    """
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="verification_readiness",
        name="final_acceptance",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="final-acceptance readiness digest; per-turn",
        id="verification_readiness:final_acceptance",
    )


def _current_review_subject_part(body: str) -> PromptPart | None:
    """Wrap the fresh subject that the next reviewer must verify."""
    if not body or not body.strip():
        return None
    return PromptPart(
        kind="current_review_subject",
        name="latest",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="fresh post-repair review subject; per-turn",
        id="current_review_subject:latest",
    )


def _plan_contract_part(body: str) -> PromptPart:
    """Wrap the rendered typed-plan contract as a turn-payload PromptPart.

    M11.5: ``plan_contract`` was previously prepended as raw text via
    :func:`_prepend_block`, which left it in the wire prompt but not
    in :attr:`PromptRenderEnvelope.parts`. The M6 delta selector would
    then re-stitch the wire on a resumed turn from
    :attr:`selected_parts` alone and drop the plan-contract block.
    Modeling it as a TURN/NONE part keeps wire text byte-identical on
    a full render and forces always-send behavior on a delta render —
    the contract is per plan/replan round and never cacheable.

    ADR 0028 / M10.5 Step 3 provenance rule: the body is the typed
    plan markdown generated by a prior planning phase (architect
    agent output projected through :mod:`pipeline.plan_contract`).
    ``source="artifact"`` makes that origin truthful — the part name
    used to be ``plan_contract:typed_plan(code-owned)`` in the debug
    transcript, which mis-attributed an architect-produced body to a
    code constant.
    """
    return PromptPart(
        kind="plan_contract",
        name="typed_plan",
        source="artifact",
        body=body,
        layer=PromptLayer.CONTEXT,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason=(
            "rendered typed-plan contract; varies across plan/replan rounds"
        ),
        id="plan_contract:typed_plan",
    )


def _plan_tasks_part(body: str) -> PromptPart:
    """Wrap the rendered task-decomposition view as a turn-payload PromptPart.

    Sibling of :func:`_plan_contract_part`. The decomposition view is
    produced by
    :func:`pipeline.plan_markdown.render_validate_plan_tasks` from
    :class:`~pipeline.plan_parser.ParsedPlan` and carries the per-subtask
    body downstream phases need to audit and execute (id / goal / spec /
    files / depends_on / skill / model / done_criteria). It is the
    execution-plan counterpart to the typed contract part:

    * ``plan_contract:typed_plan`` — *what* the plan must achieve
      (goal, acceptance criteria, risks, review focus).
    * ``plan_tasks:execution_plan`` — *how* the plan decomposes
      (subtasks + execution order).

    Both parts are TURN/NONE because the plan can change every
    replan round; the M6 delta selector always re-sends them. They
    replace the previous monolithic ``artifact:validate_plan`` part
    on the validate_plan normal path: the reviewer reads two typed
    views instead of one inline markdown blob, and the on-disk
    ``plan_*.md`` is presentation-only evidence rather than the wire
    surface (see the "ParsedPlan is canonical, plan.md is projection"
    invariants in the over-run-plan follow-up and change-semantics planning record (internal)).

    ADR 0028 / M10.5 Step 3 provenance rule: the body is the
    architect's decomposition rendered through a code-owned view —
    body bytes are runtime data, not a code/template constant.
    ``source="artifact"`` reflects that.
    """
    return PromptPart(
        kind="plan_tasks",
        name="execution_plan",
        source="artifact",
        body=body,
        layer=PromptLayer.CONTEXT,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason=(
            "rendered task-decomposition view; varies across "
            "plan/replan rounds"
        ),
        id="plan_tasks:execution_plan",
    )


def _allowed_modifications_part(plugin: PluginConfig | None) -> PromptPart | None:
    """Return the project's allowed-companion-modifications block, or ``None``.

    Renders ``plugin.allowed_modifications`` through
    :func:`pipeline.allowed_modifications.render_allowed_modifications`
    (the code-owned policy renderer). An empty / missing list yields an
    empty body, in which case no part is emitted so the wire stays
    byte-identical to the no-field path.

    Classification mirrors :func:`_project_context_part`: the entries are
    a project-stable fact (not an earlier-phase artifact), so the part is
    ``layer=CONTEXT``, ``stability=STATIC``, ``cache_scope=PROJECT`` with
    ``source="code-owned"`` — the block lives in the cacheable prefix and
    invalidates by body bytes when the plugin's list changes. The
    semantics preamble is owned by the renderer, not a shipped prompt
    part (Prompt Boundary Discipline).
    """
    if plugin is None:
        return None
    body = render_allowed_modifications(plugin.allowed_modifications).strip()
    if not body:
        return None
    return PromptPart(
        kind="allowed_modifications",
        name="project",
        source="code-owned",
        body=body,
        layer=PromptLayer.CONTEXT,
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.PROJECT,
        id="allowed_modifications:project",
    )


def _handoff_contract_part(body: str) -> PromptPart:
    """Wrap the cross-aware handoff block as a run-scoped PromptPart.

    M11.5 companion to :func:`_plan_contract_part`. ``handoff_contract``
    is rendered per cross run and prepended as raw text by review /
    repair / implement builders. Modeling it as a RUN/WORKSPACE part
    keeps envelope.parts honest without changing wire bytes; the M6
    delta selector keeps it on the wire under the M11.5
    prefix-contiguity rule (RUN parts live in the payload partition).

    ADR 0028 / M10.5 Step 3 provenance rule: the body is rendered
    per cross run from cross-run state (alias map, per-project plan
    pointers, handoff mode). Although the rendering helpers are
    code-owned, the body's content is runtime cross-run state, not a
    code/template constant. ``source="artifact"`` reflects that.
    """
    return PromptPart(
        kind="handoff_contract",
        name="cross_handoff",
        source="artifact",
        body=body,
        layer=PromptLayer.CONTEXT,
        stability=PromptStability.RUN,
        cache_scope=PromptCacheScope.WORKSPACE,
        volatile_reason="cross-aware handoff block; varies per run",
        id="handoff_contract:cross_handoff",
    )


def _artifact_turn_part(
    *,
    phase: str,
    file_path: str,
    file_content: str,
) -> PromptPart:
    """Dynamic artifact payload for file-validation phases.

    M4: phases that review an inline file artifact
    (``validate_hypothesis``, ``validate_plan``) embed the reviewed
    file content as a turn-payload :class:`PromptPart` rather than
    baking it into the user-editable task markdown. The part is
    classified ``TURN`` / ``NONE`` so the M2 envelope partitioner
    keeps it out of the cacheable prefix and the M6 delta selector
    always re-sends it on every round.

    The body carries **only the reviewed file content** — no markdown
    headings that could collide with role/task prose, no embedded
    prior feedback, and crucially **no filesystem path**. The
    artifact's on-disk location rides as the metadata-only
    ``artifact_path`` field on the PromptPart so debug transcripts and
    evidence dumps can correlate the part with the file, without the
    path leaking into the wire bytes the model sees (which would
    create a false affordance to tool-call ``Read(path)`` and
    duplicate the content already inline).

    ADR 0028 / M10.5 Step 3 provenance rule: the file content is
    runtime data read from disk (the architect's plan / hypothesis
    artifact under review). ``source="artifact"`` — the framing
    string is code-owned but the body bytes are not.
    """
    return PromptPart(
        kind="artifact",
        name=phase,
        source="artifact",
        body=file_content,
        artifact_path=file_path,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="carries reviewed file artifact content",
        id=f"artifact:{phase}",
    )


def hypothesis_file_review_prompt(
    file_path: str,
    file_content: str,
    task: str,
    project_dir: str = "",
    *,
    format_name: str | None = "detailed",
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
) -> PromptTurn:
    """HYPOTHESIS QA: reviewer validates an inline hypothesis artifact.

    This is the phase-specific artifact surface for hypothesis QA. It keeps
    the validator prompt aligned with the HYPOTHESIS gate while preserving
    the same code-owned ``review_json`` output contract.

    M4: the wrapper now composes the normal ``validate_hypothesis`` task
    with the same reviewer/role/format triple, then attaches the reviewed
    file as its own ``TURN`` / ``NONE`` :class:`PromptPart` via the
    builder gateway's ``extra_upper_parts``. The previous
    ``validate_hypothesis_file`` markdown task is gone; its only delta
    over the normal task — the inline file path + content — now lives in
    the dynamic artifact part instead.
    """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    # ADR 0028 / M10.5 Step 2: task file static; task text rides as a
    # typed turn_input part. Artifact remains its own typed part.
    variables: dict[str, object] = {}
    task_input: PromptPart | None = None
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            PromptSpec(
                role="code_reviewer",
                task="validate_hypothesis",
                format=format_name,
            ),
            project_dir=project_dir or None,
            variables=variables,
        )
        task_input = _turn_input_part(
            "validate_hypothesis_task", f"TASK:\n{task}",
        )
    else:
        intent = minimal_intents.hypothesis_review_focus_intent(task)
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    format_name,
                    project_dir=project_dir or None,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    artifact = _artifact_turn_part(
        phase="validate_hypothesis",
        file_path=file_path,
        file_content=file_content,
    )
    extra_parts = tuple(p for p in (task_input, artifact) if p is not None)
    return _render_prompt_output(
        rendered,
        system_tail=(review_json_contract(body_language=cfg.task_language),),
        extra_upper_parts=extra_parts,
        project_dir=project_dir or None,
        plugin=PluginConfig(),
    )


def readonly_plan_prompt(
    task: str,
    project_dir: str,
    codemap: str = "",
    *,
    change_handoff: str | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
) -> PromptTurn:
    """READONLY PLAN: Protocol-level architect planning surface.

    ADR 0009 Phase 6A: composed from ``roles/systems_architect`` +
    ``tasks/readonly_plan`` + ``formats/detailed``. Code-default
    builder — no profile-driven ``prompt_spec`` because READONLY
    PLAN is a runtime/protocol surface, not a profile PhaseStep. """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    # ADR 0028 / M10.5 Step 2.
    variables: dict[str, object] = {"task_language": cfg.task_language}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            PromptSpec(role="systems_architect", task="readonly_plan", format="detailed"),
            project_dir=project_dir,
            variables=variables,
        )
        task_body = (
            f"TASK:\n{task}\n\nREPO MAP:\n{codemap}"
            if codemap else f"TASK:\n{task}"
        )
        extra_parts = tuple(
            p for p in (_turn_input_part("readonly_plan_task", task_body),)
            if p is not None
        )
    else:
        intent = minimal_intents.readonly_plan_intent(task, codemap=codemap)
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    "detailed",
                    project_dir=project_dir,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    return _render_prompt_output(
        rendered,
        system_tail=(
            change_handoff_strategy(mode=change_handoff or _default_change_handoff()),
            authoring_language_strategy(task_language=cfg.task_language),
        ),
        extra_upper_parts=extra_parts,
        project_dir=project_dir,
        plugin=PluginConfig(),
    )


def runtime_review_uncommitted_prompt(
    focus: "str | PromptTurn" = "",
    project_dir: str = "",
    *,
    plan_contract: str = "",
    plan_tasks: str = "",
    handoff_contract: str = "",
    change_handoff: str | None = None,
    repair_receipt: str = "",
    current_review_subject: str = "",
    verification_receipt: str = "",
    verification_readiness: str = "",
    operator_waiver: str = "",
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
    output_contract: "OutputContract" = "review",
) -> PromptTurn:
    """Protocol-level configured-change-target review surface for runtimes.

    ADR 0009 Phase 6B: composed from ``roles/code_reviewer`` +
    ``tasks/review_uncommitted`` + ``formats/detailed``. Code-default
    builder — runtime protocol surface, not a profile PhaseStep. The
    ``review_target_strategy`` block tells the reviewer WHICH change
    surface to inspect (working tree / commit / commit_set); the
    selected JSON contract enforces output shape. Both stay code-owned
    in the system-tail; the user-editable task carries only the review
    procedure.

    ADR 0025: ``output_contract`` selects ``review_json_contract``
    (default — every existing caller, including validate_plan,
    review_changes, contract_check) or
    ``release_json_contract`` (project ``final_acceptance``). The
    wrapper strips any embedded system-tail block from ``focus`` and
    attaches its OWN contract block — the kwarg is the only source of
    truth. If a caller passes a focus carrying ``release_json`` and
    ``output_contract="review"``, the wrapper strips the embedded
    release block and attaches review; this is intentional (parameter
    over body) and locked by tests.

    ``plan_contract`` / ``plan_tasks`` are accepted here as top-level
    typed parts so runtime review traces expose the same plan handoff
    views that are on the wire. Callers that already pass a
    ``PromptTurn`` focus should keep those views out of the nested
    focus to avoid duplicating them as opaque ``turn_input`` text.

    ``operator_waiver`` carries the compact operator verdict + waived
    findings text for a ``continue_with_waiver`` phase-handoff decision.
    When non-empty it is composed with the code-owned reconciliation
    policy (:func:`operator_waiver_reconciliation_text`) into a typed
    TURN ``operator_waiver`` part so the reviewer does not reopen the
    waived findings; empty string adds no part.
    """
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    if isinstance(focus, PromptTurn):
        focus = focus.text
    focus, embedded_tail = _split_embedded_system_tail(focus)
    re_review_parts = tuple(
        p for p in (
            _repair_receipt_part(repair_receipt),
            _verification_receipt_part(verification_receipt),
            _verification_readiness_part(verification_readiness),
            _current_review_subject_part(current_review_subject),
        )
        if p is not None
    )
    # ADR 0028 / M10.5 Step 2: task file static; focus rides as a
    # typed turn_input part in FULL mode.
    variables: dict[str, object] = {}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            PromptSpec(role="code_reviewer", task="review_uncommitted", format="detailed"),
            project_dir=project_dir or None,
            variables=variables,
        )
        extra_parts = tuple(
            p for p in (
                _turn_input_part(
                    "review_focus", f"Focus area:\n{focus}",
                ),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.runtime_review_uncommitted_intent(focus)
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    "detailed",
                    project_dir=project_dir or None,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    cfg = AppConfig.load()
    # ``continue_with_waiver`` operator waiver: prepend the code-owned
    # reconciliation policy to the operator verdict body so the reviewer
    # does not reopen the waived findings. JSON contract / output schema
    # is unchanged — the review gate stays JSON-only.
    waiver_parts: tuple[PromptPart, ...] = ()
    if operator_waiver and operator_waiver.strip():
        waiver_body = (
            operator_waiver_reconciliation_text(
                body_language=cfg.task_language,
            )
            + "\n\n"
            + operator_waiver
        )
        waiver_part = _operator_waiver_part(waiver_body)
        if waiver_part is not None:
            waiver_parts = (waiver_part,)
    target_mode = (
        change_handoff
        or _review_target_mode_from_tail(embedded_tail)
        or _default_change_handoff()
    )
    tail = (
        review_target_strategy(mode=target_mode),
        _select_json_contract(
            output_contract, body_language=cfg.task_language,
        ),
    )
    prefix_parts: list[PromptPart] = []
    if handoff_contract:
        prefix_parts.append(_handoff_contract_part(handoff_contract))
    if plan_contract:
        prefix_parts.append(_plan_contract_part(plan_contract))
    if plan_tasks:
        prefix_parts.append(_plan_tasks_part(plan_tasks))
    return _render_prompt_output(
        rendered,
        system_tail=tail,
        extra_upper_parts=extra_parts + re_review_parts + waiver_parts,
        prefix_upper_parts=tuple(prefix_parts),
        project_dir=project_dir or None,
        plugin=PluginConfig(),
    )


def plan_prompt(
    task: str,
    project_dir: str,
    plugin: PluginConfig,
    *,
    change_handoff: str | None = None,
    prompt_spec: PromptSpec | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
    verification_part: PromptPart | None = None,
) -> PromptTurn:
    """PLAN: architect creates MD plan artifacts.

    PLAN prompt is composed from
    ``roles/systems_architect`` + ``tasks/plan`` + ``formats/detailed``
    (default builder spec). ``prompt_spec`` from ``PhaseStep.prompt``
    overrides the default triple; it must carry an explicit prompt
    role (workspace / profile authors choose the persona). Runtime
    role names are not threaded into rendering — that boundary is
    enforced by callers supplying prompt-taxonomy roles. The JSON
    output shape stays code-owned via ``plan_json_contract`` in the
    system-tail.
    """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    extra = plugin.plan_prompt_extra
    # Plugin-level custom template override (legacy path, still supported).
    # The plan_json contract is attached unconditionally so a custom
    # template that drops the JSON schema still gets it from system-tail.
    # Minimal mode bypasses both the custom template and the composed parts —
    # the ablation surface always renders the code-owned intent.
    if mode is ProfessionalPromptMode.FULL and plugin.custom_plan_prompt_file:
        from pathlib import Path
        tmpl = Path(project_dir) / plugin.custom_plan_prompt_file
        if tmpl.exists():
            return _render_prompt_output(
                tmpl.read_text(encoding="utf-8").replace("{task}", task),
                system_tail=(
                    change_handoff_strategy(
                        mode=change_handoff or _default_change_handoff(),
                    ),
                    plan_json_contract(
                        body_language=cfg.plan_language,
                        input_language=cfg.task_language,
                    ),
                    plan_artifact_boundary_contract(),
                    authoring_language_strategy(task_language=cfg.task_language),
                ),
                extra_upper_parts=_with_verification_part((), verification_part),
                project_dir=project_dir,
                plugin=plugin,
            )

    spec = prompt_spec or PromptSpec(
        role="systems_architect", task="plan", format="detailed",
    )
    # ADR 0028 / M10.5 Step 2: task file static.
    variables: dict[str, object] = {}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir,
            variables=variables,
        )
        extra_parts = tuple(
            p for p in (
                _turn_input_part("plan_task", f"TASK TO PLAN:\n{task}"),
                _turn_input_part(
                    "plan_extra_step",
                    f"4. {extra}" if extra else "",
                ),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.plan_intent(
            task,
            ma_artifacts_dir=plugin.ma_artifacts_dir,
            extra_step=f"4. {extra}" if extra else "",
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    spec.format,
                    project_dir=project_dir,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    return _render_prompt_output(
        rendered,
        system_tail=(
            change_handoff_strategy(mode=change_handoff or _default_change_handoff()),
            plan_json_contract(
                body_language=cfg.plan_language,
                input_language=cfg.task_language,
            ),
            plan_artifact_boundary_contract(),
            authoring_language_strategy(task_language=cfg.task_language),
        ),
        extra_upper_parts=_with_verification_part(extra_parts, verification_part),
        project_dir=project_dir,
        plugin=plugin,
    )


def decompose_plan_prompt(
    task: str,
    project_dir: str,
    plugin: PluginConfig,
    *,
    change_handoff: str | None = None,
    prompt_spec: PromptSpec | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
) -> PromptTurn:
    """DECOMPOSE: architect emits a DAG of subtasks.

    ADR 0009 Phase 5: DECOMPOSE prompt is composed from
    ``roles/systems_architect`` + ``tasks/decompose`` + ``formats/detailed``
    (generic role-agnostic preset). The JSON output shape (including
    DAG invariants — unique ids, depends_on validity, acyclicity)
    stays code-owned via ``plan_json_contract`` in the system-tail —
    project overrides of the user-editable parts cannot drop the
    parser contract.

    Used by the dag-runner path when the project either has discovered
    skills in its plugin folder or explicitly opts into structured
    decomposition. ``plan_prompt`` is kept untouched so projects
    without skills keep working exactly as before — opting into
    decomposition is a separate flow that the orchestrator gates on
    the presence of subtasks.
    """
    from pipeline.skills import render_roster

    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    extra = plugin.plan_prompt_extra
    roster = render_roster(plugin.skill_registry)
    skill_block = (
        f"AVAILABLE SKILLS\n{roster}\n"
        if roster
        else "AVAILABLE SKILLS: (none registered for this project; omit `skill` on subtasks)\n"
    )
    spec = prompt_spec or PromptSpec(
        role="systems_architect", task="decompose", format="detailed",
    )
    # ADR 0028 / M10.5 Step 2: task file static; skill roster +
    # extra-step + task arrive as typed turn_input parts in FULL mode.
    variables: dict[str, object] = {}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir,
            variables=variables,
        )
        extra_parts = tuple(
            p for p in (
                _turn_input_part(
                    "decompose_task", f"TASK TO DECOMPOSE:\n{task}",
                ),
                _turn_input_part("decompose_skill_roster", skill_block),
                _turn_input_part(
                    "decompose_extra_step",
                    f"Project rule: {extra}" if extra else "",
                ),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.decompose_intent(
            task,
            skill_roster_block=skill_block,
            extra_step=f"Project rule: {extra}" if extra else "",
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    spec.format,
                    project_dir=project_dir,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    return _render_prompt_output(
        rendered,
        system_tail=(
            change_handoff_strategy(mode=change_handoff or _default_change_handoff()),
            skill_routing_strategy(),
            plan_json_contract(
                body_language=cfg.plan_language,
                input_language=cfg.task_language,
            ),
            authoring_language_strategy(task_language=cfg.task_language),
        ),
        extra_upper_parts=extra_parts,
        project_dir=project_dir,
        plugin=plugin,
    )


def plan_review_focus(
    task: str,
    plugin: PluginConfig,
    project_dir: str = "",
    *,
    plan_contract: str = "",
    prompt_spec: PromptSpec | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
    verification_part: PromptPart | None = None,
) -> PromptTurn:
    """validate_plan: reviewer validates the plan document.

    REA-1: ``plan_contract`` (rendered) prepends so the reviewer can
    verify the architect's typed contract is coherent with the plan body
    (e.g. acceptance criteria match what the tasks actually deliver).

    The prompt is composed from ``roles/plan_reviewer`` +
    ``tasks/validate_plan`` + ``formats/detailed`` (default builder
    spec). ``prompt_spec`` from ``PhaseStep.prompt`` overrides the
    default triple; it must carry an explicit prompt role. The JSON
    output shape stays code-owned via ``review_json_contract`` in the
    system-tail — no user-editable role/task/format part owns parser
    behavior.
    """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    extra = plugin.review_focus_extra
    extra_checks = f"\nProject-specific checks:\n{extra}" if extra else ""
    spec = prompt_spec or PromptSpec(
        role="plan_reviewer", task="validate_plan", format="detailed",
    )
    # ADR 0028 / M10.5 Step 2: task file static; task + extra_checks
    # arrive as typed turn_input parts.
    variables: dict[str, object] = {}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir or None,
            variables=variables,
        )
        extra_parts = tuple(
            p for p in (
                _turn_input_part(
                    "validate_plan_task", f"TASK:\n{task}",
                ),
                _turn_input_part(
                    "validate_plan_extra_checks", extra_checks,
                ),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.plan_review_focus_intent(
            task, extra_checks=extra_checks,
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    spec.format,
                    project_dir=project_dir or None,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    prompt = _prepend_block(plan_contract, rendered)
    prefix_list: list[PromptPart] = []
    allowed_part = _allowed_modifications_part(plugin)
    if allowed_part is not None:
        prefix_list.append(allowed_part)
    if plan_contract:
        prefix_list.append(_plan_contract_part(plan_contract))
    return _render_prompt_output(
        prompt,
        system_tail=(review_json_contract(body_language=cfg.task_language),),
        extra_upper_parts=_with_verification_part(extra_parts, verification_part),
        prefix_upper_parts=tuple(prefix_list),
        project_dir=project_dir or None,
        plugin=plugin,
    )


def plan_file_review_prompt(
    parsed_plan: "ParsedPlan",
    task: str,
    plugin: PluginConfig,
    project_dir: str = "",
    *,
    repair_receipt: str = "",
    current_review_subject: str = "",
    prompt_spec: PromptSpec | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
) -> PromptTurn:
    """validate_plan: reviewer validates the typed plan views.

    The normal validate_plan path no longer ships the on-disk plan
    markdown as a monolithic ``artifact:validate_plan`` PromptPart.
    Instead it emits two typed views rendered directly from
    :class:`~pipeline.plan_parser.ParsedPlan` (the canonical machine
    contract):

    * ``plan_contract:typed_plan`` (via :func:`_plan_contract_part`,
      body from :func:`pipeline.plan_contract.render_plan_contract`)
      — the *what*: goal, acceptance criteria, owned files,
      commands, risks, review focus, mcp_context.
    * ``plan_tasks:execution_plan`` (via :func:`_plan_tasks_part`,
      body from
      :func:`pipeline.plan_markdown.render_validate_plan_tasks`)
      — the *how*: per-subtask decomposition + DAG.

    Both parts are TURN/NONE and classified DECISION_BEARING (see
    :data:`pipeline.observability.output_class.PROMPT_PART_CLASS_RULES`).
    The on-disk ``plan_*.md`` is unchanged in PR3 — it stays as a
    human-readable evidence artefact for dashboards / debugging,
    but the reviewer no longer reads it back: bytes the reviewer
    needs come from typed views, not a markdown round-trip.

    The diff-only fallback ``plan_review_focus`` is untouched —
    it has no parsed plan to render from and continues to ship the
    plan-contract prefix it built before. The caller
    (``_review_plan_artifact``) routes to this builder only when
    ``state.parsed_plan`` is present, and hard-fails otherwise.
    """
    from pipeline.plan_contract import render_plan_contract
    from pipeline.plan_markdown import render_validate_plan_tasks

    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    extra = plugin.review_focus_extra
    extra_checks = f"\nProject-specific checks:\n{extra}" if extra else ""
    spec = prompt_spec or PromptSpec(
        role="plan_reviewer", task="validate_plan", format="detailed",
    )
    # ADR 0028 / M10.5 Step 2: task file static.
    variables: dict[str, object] = {}
    task_input: PromptPart | None = None
    extra_checks_part: PromptPart | None = None
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir or None,
            variables=variables,
        )
        task_input = _turn_input_part(
            "validate_plan_task", f"TASK:\n{task}",
        )
        extra_checks_part = _turn_input_part(
            "validate_plan_extra_checks", extra_checks,
        )
    else:
        intent = minimal_intents.plan_review_focus_intent(
            task,
            extra_checks=extra_checks,
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    spec.format,
                    project_dir=project_dir or None,
                    variables=variables,
                ),
            )
        else:
            rendered = intent

    # Typed views rendered from the parsed plan. The contract body
    # may be empty for legacy / pre-REA-1 plans; in that case the
    # contract part is omitted so the wire does not carry an empty
    # ``## Plan Contract`` heading. The tasks view is always emitted
    # (an empty subtask list is itself an architectural fact the
    # reviewer should see — render_validate_plan_tasks degrades
    # gracefully to just the ``## Tasks`` header).
    plan_contract_body = render_plan_contract(parsed_plan).strip()
    contract_part = (
        _plan_contract_part(plan_contract_body)
        if plan_contract_body else None
    )
    plan_tasks_part = _plan_tasks_part(
        render_validate_plan_tasks(parsed_plan),
    )

    extra_parts = tuple(
        p for p in (
            task_input,
            extra_checks_part,
            _allowed_modifications_part(plugin),
            contract_part,
            plan_tasks_part,
            _repair_receipt_part(repair_receipt),
            _current_review_subject_part(current_review_subject),
        ) if p is not None
    )
    return _render_prompt_output(
        rendered,
        system_tail=(review_json_contract(body_language=cfg.task_language),),
        extra_upper_parts=extra_parts,
        project_dir=project_dir or None,
        plugin=plugin,
    )


def replan_prompt(
    task: str,
    critique: str,
    human_feedback: str,
    project_dir: str,
    plugin: PluginConfig,
    *,
    change_handoff: str | None = None,
    prompt_spec: PromptSpec | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
) -> PromptTurn:
    """REPLAN: architect revises plan after QA rejection or operator retry.

    REPLAN prompt is composed from
    ``roles/systems_architect`` + ``tasks/replan`` + ``formats/detailed``
    (default builder spec). REPLAN is not its own ``PhaseStep`` —
    ``_phase_plan`` chooses plan vs replan per round and derives the
    replan spec from the active plan PhaseStep's spec (swapping
    ``task="plan"`` → ``task="replan"``). The builder stays dumb:
    when called with ``prompt_spec=None`` it defaults to a fully
    populated spec including the prompt role.

    ``critique`` carries reviewer findings from a prior ``validate_plan``
    gate (machine artifact); ``human_feedback`` carries operator
    instruction from ``phase_handoff_decide(retry_feedback)``. Either
    may be empty; at least one must be non-empty for the replan branch
    to fire (enforced by the caller). When both are non-empty the
    ``tasks/replan.md`` body teaches the architect how to reconcile
    them — operator feedback is authoritative scope guidance, reviewer
    critique is advisory.
    """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    spec = prompt_spec or PromptSpec(
        role="systems_architect", task="replan", format="detailed",
    )
    # ADR 0028 / M10.5 Step 2: task file static; task, reviewer critique
    # and human feedback arrive as typed turn_input / reviewer_critique /
    # human_feedback parts in FULL mode.
    variables: dict[str, object] = {}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir,
            variables=variables,
        )
        extra_parts = tuple(
            p for p in (
                _turn_input_part("replan_task", f"TASK:\n{task}"),
                _reviewer_critique_part(critique),
                _human_feedback_part(human_feedback),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.replan_intent(task, critique, human_feedback)
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    spec.format,
                    project_dir=project_dir,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    return _render_prompt_output(
        rendered,
        system_tail=(
            change_handoff_strategy(mode=change_handoff or _default_change_handoff()),
            plan_json_contract(
                body_language=cfg.plan_language,
                input_language=cfg.task_language,
            ),
            plan_artifact_boundary_contract(),
            authoring_language_strategy(task_language=cfg.task_language),
        ),
        extra_upper_parts=extra_parts,
        project_dir=project_dir,
        plugin=plugin,
    )


def build_prompt(
    task: str,
    project_dir: str,
    plugin: PluginConfig,
    *,
    plan_contract: str = "",
    plan_tasks: str = "",
    handoff_contract: str = "",
    change_handoff: str | None = None,
    advisory_critique: str = "",
    prompt_spec: PromptSpec | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
    verification_part: PromptPart | None = None,
) -> PromptTurn:
    """implement: developer implements the planned task.

    The downstream plan handoff is the pair of typed plan views:
    ``plan_contract`` carries the run goal / acceptance criteria /
    risks, while ``plan_tasks`` carries the subtask decomposition the
    implementation engineer must execute. Both are rendered from
    ``state.parsed_plan`` by the caller and emitted as artifact
    PromptParts; plan markdown files remain observability artefacts,
    not the runtime source of truth.

    implement prompt is composed from
    ``roles/implementation_engineer`` + ``tasks/build`` +
    ``formats/handoff`` (default builder spec). ``prompt_spec`` from
    ``PhaseStep.prompt`` overrides the default triple; it must carry
    an explicit prompt role. Runtime role names are not threaded into
    prompt rendering — execution-routing and persona-selection are
    independent concerns.

    ``advisory_critique`` carries plan-reviewer findings from a prior
    ``validate_plan`` gate that returned REJECTED but was bypassed (the
    profile proceeded to implement without a replan loop). When non-empty
    it is composed with the code-owned reconciliation policy
    (:func:`advisory_critique_reconciliation_text`) into a typed
    ``reviewer_critique`` TURN part so the agent treats the findings as
    advisory and keeps implementing rather than replanning; empty string
    adds no part. The part is appended mode-independently so it survives
    the FULL / minimal ablation paths alike.
    """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    extra = plugin.build_prompt_extra
    extra_step = f"5. {extra}" if extra else ""
    spec = prompt_spec or PromptSpec(
        role="implementation_engineer", task="implement", format="handoff",
    )
    # ADR 0028 / M10.5 Step 2: task file static; task + extra_step
    # arrive as typed turn_input parts. ma_artifacts_dir is now a
    # generic reference in the static method prose (no longer
    # project-specific in the template); plugins that need the
    # specific path can still surface it through extra_step.
    variables: dict[str, object] = {"task_language": cfg.task_language}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir,
            variables=variables,
        )
        # ma_artifacts_dir is project-stable runtime config; the
        # static task method only mentions "the project artifacts
        # directory" generically. Surface the concrete path here so
        # the agent can locate the plan documents.
        task_body = f"TASK:\n{task}"
        if plugin.ma_artifacts_dir:
            task_body = (
                f"{task_body}\n\n"
                f"Refer to the plan documents in {plugin.ma_artifacts_dir}/ "
                "if they exist."
            )
        extra_parts = tuple(
            p for p in (
                _turn_input_part("implement_task", task_body),
                _turn_input_part("implement_extra_step", extra_step),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.build_intent(
            task,
            ma_artifacts_dir=plugin.ma_artifacts_dir,
            extra_step=extra_step,
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    spec.format,
                    project_dir=project_dir,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    # Mode-independent: forward bypassed plan-review findings as an advisory
    # reviewer_critique part so it survives both FULL and minimal/fast paths
    # (where ``extra_parts`` would otherwise be empty). Reuses the same typed
    # part as replan; the framing is the code-owned advisory policy.
    if advisory_critique and advisory_critique.strip():
        advisory_body = (
            advisory_critique_reconciliation_text(
                body_language=cfg.task_language,
            )
            + "\n\n"
            + advisory_critique
        )
        advisory_part = _reviewer_critique_part(advisory_body)
        if advisory_part is not None:
            extra_parts = extra_parts + (advisory_part,)
    prefix_parts: list[PromptPart] = []
    if handoff_contract:
        prefix_parts.append(_handoff_contract_part(handoff_contract))
    if plan_contract:
        prefix_parts.append(_plan_contract_part(plan_contract))
    if plan_tasks:
        prefix_parts.append(_plan_tasks_part(plan_tasks))
    return _render_prompt_output(
        _prepend_block(
            handoff_contract,
            _prepend_block(plan_contract, rendered),
        ),
        system_tail=(
            change_handoff_strategy(mode=change_handoff or _default_change_handoff()),
            authoring_language_strategy(task_language=cfg.task_language),
        ),
        extra_upper_parts=_with_verification_part(extra_parts, verification_part),
        prefix_upper_parts=tuple(prefix_parts),
        project_dir=project_dir,
        plugin=plugin,
    )


def review_focus(
    task: str,
    plugin: PluginConfig,
    project_dir: str = "",
    *,
    plan_contract: str = "",
    plan_tasks: str = "",
    handoff_contract: str = "",
    require_verdict: bool = False,
    change_handoff: str | None = None,
    prompt_spec: PromptSpec | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
    output_contract: "OutputContract" = "review",
    verification_part: PromptPart | None = None,
) -> PromptTurn:
    """review_changes / final_acceptance: reviewer inspects the configured change target.

    Downstream review receives the same typed plan handoff as
    implementation: ``plan_contract`` for acceptance criteria and
    ``plan_tasks`` for the subtask/DAG decomposition. Reviewers must
    inspect the change against both the outcome contract and the work
    slices the planner asked the implementer to execute.

    review prompt defaults to ``roles/code_reviewer`` +
    ``tasks/code_review`` + ``formats/detailed``. ``prompt_spec`` from
    ``PhaseStep.prompt`` overrides the triple; shipped final_acceptance
    uses ``roles/release_manager`` + ``tasks/final_acceptance`` so the
    closing gate keeps release-readiness framing. Runtime role names are
    not threaded into prompt rendering.

    ADR 0025: ``output_contract`` selects ``review_json_contract``
    (default — review_changes, validate_plan when this builder is used)
    or ``release_json_contract`` (project ``final_acceptance``). The
    handler chooses; this builder appends exactly one contract block.
    """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    extra = plugin.review_focus_extra
    extra_checks = f"\nProject-specific checks:\n{extra}" if extra else ""
    spec = prompt_spec or PromptSpec(
        role="code_reviewer",
        task="code_review",
        format="detailed",
    )
    # ADR 0028 / M10.5 Step 2: task file static.
    variables: dict[str, object] = {}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir or None,
            variables=variables,
        )
        extra_parts = tuple(
            p for p in (
                _turn_input_part(
                    "review_focus_task", f"TASK:\n{task}",
                ),
                _turn_input_part("review_focus_extra_checks", extra_checks),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.review_focus_intent(
            task, extra_checks=extra_checks,
        )
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    spec.format,
                    project_dir=project_dir or None,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    prompt = _prepend_block(
        handoff_contract,
        _prepend_block(plan_contract, rendered),
    )
    # The reviewer always emits the structured JSON contract — review_changes
    # and final_acceptance both gate on it. ``require_verdict`` is preserved for callers
    # that still pass it but no longer changes the output shape.
    _ = require_verdict
    system_tail = (
        review_target_strategy(mode=change_handoff or _default_change_handoff()),
        _select_json_contract(
            output_contract, body_language=cfg.task_language,
        ),
    )
    prefix_parts: list[PromptPart] = []
    if handoff_contract:
        prefix_parts.append(_handoff_contract_part(handoff_contract))
    allowed_part = _allowed_modifications_part(plugin)
    if allowed_part is not None:
        prefix_parts.append(allowed_part)
    if plan_contract:
        prefix_parts.append(_plan_contract_part(plan_contract))
    if plan_tasks:
        prefix_parts.append(_plan_tasks_part(plan_tasks))
    return _render_prompt_output(
        prompt,
        system_tail=system_tail,
        extra_upper_parts=_with_verification_part(extra_parts, verification_part),
        prefix_upper_parts=tuple(prefix_parts),
        project_dir=project_dir or None,
        plugin=plugin,
    )


def fix_prompt(
    task: str,
    critique: str,
    project_dir: str,
    plugin: PluginConfig,
    *,
    test_failures: str = "",
    write_style: str = "",
    plan_contract: str = "",
    plan_tasks: str = "",
    handoff_contract: str = "",
    change_handoff: str | None = None,
    prompt_spec: PromptSpec | None = None,
    professional_prompt_mode: "ProfessionalPromptMode | str | None" = None,
    verification_part: PromptPart | None = None,
) -> PromptTurn:
    """repair_changes: developer addresses reviewer critique and test failures.

    Repair receives both typed plan views so fixes stay anchored to
    the original acceptance criteria and the concrete subtask
    decomposition, not only to the reviewer's latest critique.

    repair_changes prompt is composed from
    ``roles/implementation_engineer`` + ``tasks/fix`` +
    ``formats/handoff`` (default builder spec). The reviewer-critique
    + test-failure body is built by ``build_fix_prompt`` (unchanged
    helper) and threaded into the ``$body`` placeholder of
    ``tasks/fix``. ``prompt_spec`` from ``PhaseStep.prompt`` overrides
    the default triple; it must carry an explicit prompt role.
    """
    cfg = AppConfig.load()
    mode = coerce_professional_prompt_mode(professional_prompt_mode)
    body = build_fix_prompt(
        review=critique,
        test_failures=test_failures,
        write_style=write_style,
    )
    spec = prompt_spec or PromptSpec(
        role="implementation_engineer", task="repair_changes", format="handoff",
    )
    # ADR 0028 / M10.5 Step 2: task file static; task + repair body
    # (reviewer findings + test failures) arrive as typed turn_input
    # / feedback parts in FULL mode.
    variables: dict[str, object] = {"task_language": cfg.task_language}
    extra_parts: tuple[PromptPart, ...] = ()
    if mode is ProfessionalPromptMode.FULL:
        rendered = render_composed_prompt(
            spec,
            project_dir=project_dir,
            variables=variables,
        )
        extra_parts = tuple(
            p for p in (
                _turn_input_part("repair_task", f"TASK:\n{task}"),
                _feedback_part("repair_body", body),
            )
            if p is not None
        )
    else:
        intent = minimal_intents.fix_intent(task, body)
        if mode is ProfessionalPromptMode.MINIMAL_WITH_FORMAT:
            rendered = _append_format(
                intent,
                _render_format_only(
                    spec.format,
                    project_dir=project_dir,
                    variables=variables,
                ),
            )
        else:
            rendered = intent
    prefix_parts: list[PromptPart] = []
    if handoff_contract:
        prefix_parts.append(_handoff_contract_part(handoff_contract))
    if plan_contract:
        prefix_parts.append(_plan_contract_part(plan_contract))
    if plan_tasks:
        prefix_parts.append(_plan_tasks_part(plan_tasks))
    return _render_prompt_output(
        _prepend_block(
            handoff_contract,
            _prepend_block(plan_contract, rendered),
        ),
        system_tail=(
            change_handoff_strategy(mode=change_handoff or _default_change_handoff()),
            authoring_language_strategy(task_language=cfg.task_language),
        ),
        extra_upper_parts=_with_verification_part(extra_parts, verification_part),
        prefix_upper_parts=tuple(prefix_parts),
        project_dir=project_dir,
        plugin=plugin,
    )


def build_fix_prompt(
    review: str,
    test_failures: str = "",
    write_style: str = "",
) -> str:
    """Compose the body section of a repair_changes prompt from review + test output.

    Kept as a separate helper so unit tests can verify formatting independently
    of project context. Callers (fix_prompt) wrap this in the full template.
    """
    sections: list[str] = []

    if review:
        sections.append(f"A code review found these issues:\n{review}")

    if test_failures:
        sections.append(
            "The test suite is FAILING. Output below:\n"
            f"{test_failures}\n\n"
            "Make the failing tests pass without weakening their assertions."
        )

    if write_style:
        style_hint = {
            "behat_gherkin":
                "When adding tests, follow Behat/Gherkin conventions: "
                ".feature files with Given/When/Then steps and matching "
                "context classes.",
            "pytest":
                "When adding tests, use pytest style: plain `test_*` "
                "functions, fixtures over setUp/tearDown, parametrize where "
                "useful.",
            "nunit_csharp":
                "When adding tests, use NUnit attributes: [Test] / [TestCase] "
                "in EditMode test assemblies, Assert.That with constraints.",
            "xunit_csharp":
                "When adding tests, use xUnit attributes: [Fact] / [Theory] "
                "with [InlineData], constructor for setup, IDisposable for "
                "teardown.",
        }.get(write_style, f"Test style hint: {write_style}")
        sections.append(style_hint)

    sections.append(
        "Please fix all issues above. Do not leave anything unaddressed. "
        "Keep working code intact and follow the project's existing style."
    )

    return "\n\n".join(sections)
