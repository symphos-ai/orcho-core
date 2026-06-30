"""
pipeline/prompts/contracts.py — prompt envelopes with system-tail blocks.

User/project/workspace prompts are editable input. Some phases also need
non-user system instructions appended after that input: parser contracts,
output-shape constraints, safety rails, or runtime-owned metadata. Those
blocks are appended by code after prompt resolution so project-level prompt
overrides cannot accidentally remove or translate parser-critical text.

The prose for each system-tail block lives in
:mod:`pipeline.prompts.contract_templates` as a frozen template constant.
This module owns the public API and the policy branching that picks which
template applies (mode-keyed handoff, optional language directive, etc.).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from html import escape

from pipeline.prompts.contract_templates import (
    AUTHORING_LANGUAGE,
    CHANGE_HANDOFF_TEMPLATES,
    CODING_AGENT_COMPACTION,
    COMMIT_MESSAGE_JSON,
    CROSS_PLAN_JSON,
    PLAN_ARTIFACT_BOUNDARY,
    PLAN_JSON,
    RELEASE_JSON,
    REVIEW_JSON,
    REVIEW_TARGET_TEMPLATES,
    SKILL_ROUTING,
    SUBTASK_ATTESTATION,
    SUBTASK_EXECUTION_RULES,
)
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptStability,
)

_CHANGE_HANDOFF_MODES = frozenset({"uncommitted", "commit", "commit_set"})


def _normalize_change_handoff_mode(mode: object) -> str:
    value = getattr(mode, "value", mode)
    if not isinstance(value, str):
        raise ValueError(f"unsupported change handoff mode: {mode!r}")
    normalized = value.strip().lower()
    if normalized not in _CHANGE_HANDOFF_MODES:
        raise ValueError(f"unsupported change handoff mode: {mode!r}")
    return normalized


def _normalize_language(value: str | None) -> str:
    """Lowercase / strip / underscore-collapse a language name.

    Used to build readable but stable id segments for contract blocks
    whose rendered body depends on a configured language. ``None`` and
    empty strings collapse to ``""`` so callers can distinguish the
    "no language directive" path from "language directive present".
    """
    if not value:
        return ""
    return value.strip().lower().replace(" ", "_")


def _language_signature(*languages: str | None) -> str:
    """Build a deterministic id segment for one or more languages.

    Duplicates collapse (so passing the same value twice produces a
    single segment) but order is preserved across distinct values.
    Returns ``""`` when no language is supplied — callers use that as
    the "no directive, fall back to STATIC/GLOBAL" signal.
    """
    seen: list[str] = []
    for raw in languages:
        norm = _normalize_language(raw)
        if not norm or norm in seen:
            continue
        seen.append(norm)
    return "_".join(seen)


@dataclass(frozen=True)
class SystemPromptBlock:
    """A system-owned prompt block appended after user-level prompt input.

    ADR-0026 metadata (``stability``, ``cache_scope``, ``volatile_reason``,
    ``layer``, ``id``) flows through the M2 envelope partitioner via the
    builder gateway: each block becomes a :class:`PromptPart` carrying
    the same metadata, which decides whether the block lives in the
    stable cacheable prefix or the per-turn payload.

    Defaults are M1-safe — ``STATIC`` / ``GLOBAL`` / ``CONTRACT`` —
    so existing constructors and any third-party caller that only
    passes ``name`` / ``body`` / ``version`` / ``kind`` keep working
    and the resulting part lands in the stable prefix unchanged.
    Factories that need richer metadata (profile-keyed handoff mode,
    workspace-scoped language signature, etc.) set the fields
    explicitly.
    """

    name: str
    body: str
    version: int = 1
    kind: str = "contract"
    stability: PromptStability = PromptStability.STATIC
    cache_scope: PromptCacheScope = PromptCacheScope.GLOBAL
    volatile_reason: str | None = None
    layer: PromptLayer = PromptLayer.CONTRACT
    id: str = ""

    def render(self) -> str:
        kind = escape(self.kind, quote=True)
        name = escape(self.name, quote=True)
        return (
            f'<orcho:system-block kind="{kind}" name="{name}" version="{self.version}">\n'
            f"{self.body.strip()}\n"
            "</orcho:system-block>"
        )


@dataclass(frozen=True)
class PromptEnvelope:
    """A full prompt split into user input and system-owned tail blocks."""

    user_prompt: str
    system_tail: tuple[SystemPromptBlock, ...] = ()

    def render(self) -> str:
        return compose_prompt(self.user_prompt, system_tail=self.system_tail)


# Backward-compatible alias: a "contract" is now just one kind of system block.
SystemPromptContract = SystemPromptBlock


def compose_prompt(
    user_prompt: str,
    *,
    system_tail: Iterable[SystemPromptBlock] = (),
) -> str:
    """Render a prompt with all system blocks appended last.

    This is the generic join point every phase can use. It deliberately does
    not know about QA, build, review, or any particular prompt template.
    """
    rendered_blocks = [b.render() for b in system_tail]
    if not rendered_blocks:
        return user_prompt.strip()
    rendered_tail = "\n\n".join(rendered_blocks)
    prompt = user_prompt.rstrip()
    if not prompt.strip():
        return rendered_tail
    return f"{prompt}\n\n{rendered_tail}"


def append_system_contract(prompt: str, contract: SystemPromptContract) -> str:
    """Append one system contract as the final annotated prompt block."""
    return compose_prompt(prompt, system_tail=(contract,))


def _block_from(
    template,
    body: str,
    *,
    stability: PromptStability = PromptStability.STATIC,
    cache_scope: PromptCacheScope = PromptCacheScope.GLOBAL,
    volatile_reason: str | None = None,
    id: str = "",
) -> SystemPromptBlock:
    """Wrap a rendered template body in a versioned :class:`SystemPromptBlock`.

    ``stability`` / ``cache_scope`` / ``volatile_reason`` / ``id``
    default to the M1-safe values so callers that only need an
    immutable contract block (``plan_artifact_boundary``, the JSON
    contracts when no language directive is present, etc.) keep
    working without explicit metadata kwargs. Factories that need
    profile- or workspace-keyed cache identity pass the fields
    explicitly so the M2 envelope partitioner sees the right
    classification at the seam.
    """
    return SystemPromptBlock(
        name=template.name,
        body=body,
        version=1,
        kind=template.kind,
        stability=stability,
        cache_scope=cache_scope,
        volatile_reason=volatile_reason,
        id=id,
    )


def plan_json_contract(
    *,
    body_language: str | None = None,
    input_language: str | None = None,
) -> SystemPromptBlock:
    """Contract for architect phases that emit a typed plan JSON.

    Covers PLAN, REPLAN, and DECOMPOSE. The block forces JSON-only output
    shape and embeds ``PLAN_SCHEMA_DOC`` so the architect agent does not
    need any prompt-template support to know what to emit. Project
    overrides of the user-editable ``tasks/plan`` /
    ``tasks/decompose`` / ``tasks/replan`` parts cannot
    silently drop the parser contract — it is attached by the builder.

    ``body_language`` selects the project language for human-readable
    JSON body fields (typically ``cfg.plan_language``). ``input_language``
    disambiguates when the task description and plan body should be in
    different languages (typically ``cfg.task_language``).
    """
    from core.contracts.plan_schema import PLAN_SCHEMA_DOC

    if body_language:
        if input_language and input_language != body_language:
            directive = (
                "The task description may be in "
                f"{input_language}; understand it but write the JSON "
                "body fields (short_summary, planning_context, goal, "
                "acceptance_criteria, risks, review_focus, and task "
                f"spec / goal / done_criteria) in {body_language}."
            )
        else:
            directive = (
                "Write the human-readable JSON body fields "
                "(short_summary, planning_context, goal, "
                "acceptance_criteria, risks, review_focus, and task "
                f"spec / goal / done_criteria) in {body_language}."
            )
        language_directive = "\n" + directive
    else:
        language_directive = ""

    body = PLAN_JSON.render(
        language_directive=language_directive,
        plan_schema_doc=PLAN_SCHEMA_DOC,
    )
    # M3: when a language directive is embedded in the rendered body,
    # the block's cache identity must include the normalized language
    # signature so two projects with different languages cannot share
    # a cached prefix. Both variants stay in the stable prefix —
    # PROFILE/WORKSPACE is prefix-eligible per ``is_prefix_eligible``.
    sig = _language_signature(body_language, input_language)
    if sig:
        return _block_from(
            PLAN_JSON,
            body,
            stability=PromptStability.PROFILE,
            cache_scope=PromptCacheScope.WORKSPACE,
            volatile_reason="depends on project/workspace language configuration",
            id=f"contract:plan_json:v1:{sig}",
        )
    return _block_from(PLAN_JSON, body)


def skill_routing_strategy() -> SystemPromptBlock:
    """Strategy for assigning DAG subtasks to discovered skills.

    The roster itself is dynamic task input, but the expectation to use
    matching skills is part of the runtime prompt contract.
    """
    body = SKILL_ROUTING.render()
    return _block_from(SKILL_ROUTING, body)


def review_json_contract(*, body_language: str | None = None) -> SystemPromptBlock:
    """Contract for reviewer phases that emit a structured JSON review.

    The block forces a JSON-only output shape and embeds the schema so the
    reviewer agent does not need any prompt-template support to know what to
    emit. Project/task language is allowed in human-readable fields
    (``short_summary``, finding bodies, ``risks``, ``checks``); enum values
    stay English exactly as listed.
    """
    from core.contracts.review_schema import REVIEW_SCHEMA_DOC

    if body_language:
        directive = (
            "Write the human-readable JSON fields (short_summary, "
            f"finding bodies, risks, checks) in {body_language}."
        )
        language_directive = "\n" + directive
    else:
        language_directive = ""

    body = REVIEW_JSON.render(
        language_directive=language_directive,
        review_schema_doc=REVIEW_SCHEMA_DOC,
    )
    sig = _language_signature(body_language)
    if sig:
        return _block_from(
            REVIEW_JSON,
            body,
            stability=PromptStability.PROFILE,
            cache_scope=PromptCacheScope.WORKSPACE,
            volatile_reason="depends on project/workspace language configuration",
            id=f"contract:review_json:v1:{sig}",
        )
    return _block_from(REVIEW_JSON, body)


def release_json_contract(*, body_language: str | None = None) -> SystemPromptBlock:
    """Contract for the release gate (project ``final_acceptance``,
    future ``cross_final_acceptance``) — emits structured JSON with
    ``ship_ready`` / ``release_blockers`` / ``verification_gaps`` /
    ``contract_status``. See ADR 0025.

    Distinct from :func:`review_json_contract`: the release gate
    answers "can this ship?" rather than "is this review-clean?"; the
    machine contract is different and the parser is
    :func:`pipeline.release_parser.parse_release`.

    Project/task language is allowed in human-readable fields
    (``short_summary``, blocker bodies, gap fields,
    ``why_blocks_release``); protocol enum values stay English exactly.
    """
    from core.contracts.release_schema import RELEASE_SCHEMA_DOC

    if body_language:
        directive = (
            "Write the human-readable JSON fields (short_summary, "
            "release_blockers[].body, release_blockers[].why_blocks_release, "
            "verification_gaps[].risk / .missing_evidence / "
            f".required_check) in {body_language}."
        )
        language_directive = "\n" + directive
    else:
        language_directive = ""

    body = RELEASE_JSON.render(
        language_directive=language_directive,
        release_schema_doc=RELEASE_SCHEMA_DOC,
    )
    sig = _language_signature(body_language)
    if sig:
        return _block_from(
            RELEASE_JSON,
            body,
            stability=PromptStability.PROFILE,
            cache_scope=PromptCacheScope.WORKSPACE,
            volatile_reason="depends on project/workspace language configuration",
            id=f"contract:release_json:v1:{sig}",
        )
    return _block_from(RELEASE_JSON, body)


def commit_message_json_contract(
    *, body_language: str | None = None,
) -> SystemPromptBlock:
    """Contract for the commit-decision gate's ``llm_generate`` strategy.

    The runtime emits exactly one JSON object — ``subject`` /
    ``body`` / ``type`` / optional ``scope`` / ``breaking`` — parsed by
    :func:`pipeline.commit_message_parser.parse_commit_message`. The
    composed ``git commit -m`` text is rendered from that object; project
    overrides of the user-editable ``tasks/commit_message`` part
    cannot drop or rewrite the JSON contract — it is attached by the
    builder.

    Distinct from :func:`release_json_contract`: the release gate decides
    "can this ship?"; this contract drives the message text the executor
    feeds to ``git commit`` after the operator has chosen to commit.

    Project/task language is allowed in human-readable fields
    (``subject``, ``body``); the ``type`` enum stays English exactly as
    listed.
    """
    from core.contracts.commit_decision_schema import COMMIT_MESSAGE_SCHEMA_DOC

    if body_language:
        directive = (
            "Write the human-readable JSON fields (subject, body) "
            f"in {body_language}."
        )
        language_directive = "\n" + directive
    else:
        language_directive = ""

    body = COMMIT_MESSAGE_JSON.render(
        language_directive=language_directive,
        commit_message_schema_doc=COMMIT_MESSAGE_SCHEMA_DOC,
    )
    sig = _language_signature(body_language)
    if sig:
        return _block_from(
            COMMIT_MESSAGE_JSON,
            body,
            stability=PromptStability.PROFILE,
            cache_scope=PromptCacheScope.WORKSPACE,
            volatile_reason="depends on project/workspace language configuration",
            id=f"contract:commit_message_json:v1:{sig}",
        )
    return _block_from(COMMIT_MESSAGE_JSON, body)


def change_handoff_strategy(*, mode: str = "uncommitted") -> SystemPromptBlock:
    """Development strategy for how authoring agents hand code changes back.

    This policy is profile/config owned. User-level templates describe the task;
    this system-tail block describes how code changes must be handed to later
    phases.

    M3: classified ``PROFILE`` / ``GLOBAL`` with the mode encoded in
    the part id (``contract:change_handoff:mode={normalized}``). The
    M6 selector treats a mode change as an ``id`` change and resends
    the part naturally; profile-bound stability keeps the body in the
    stable prefix while a runtime profile flip remains traceable.
    """
    normalized = _normalize_change_handoff_mode(mode)
    template = CHANGE_HANDOFF_TEMPLATES[normalized]
    body = template.render()
    return _block_from(
        template,
        body,
        stability=PromptStability.PROFILE,
        cache_scope=PromptCacheScope.GLOBAL,
        volatile_reason="depends on profile-configured handoff mode",
        id=f"contract:change_handoff:mode={normalized}",
    )


def subtask_execution_rules_strategy() -> SystemPromptBlock:
    """Current-only execution policy for subtask_dag (P2).

    Code-owned orchestration policy: the developer agent must execute ONLY
    its current subtask, treating the plan contract and DAG map as background
    delivery context — never as extra work. Boundary discipline keeps this in
    ``contracts`` / ``contract_templates`` rather than a user-editable part.

    Static / global: the body is a code constant, identical across runs, so
    it caches in the stable prefix.
    """
    template = SUBTASK_EXECUTION_RULES
    return _block_from(
        template,
        template.render(),
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.GLOBAL,
        id="contract:subtask_execution_rules",
    )


def subtask_attestation_contract() -> SystemPromptBlock:
    """Contract: the developer appends a typed done-criteria self-attestation.

    Code-owned parser contract (P7 / ADR 0068). The developer keeps its normal
    human-readable build output and appends exactly one ``subtask_attestation``
    JSON object reporting, per ``done_criteria`` index, whether it was met plus
    a one-sentence evidence claim. Orcho gates on the SHAPE + completeness of
    this object (``pipeline.subtask_attestation_parser``); the truth of the
    evidence stays with the downstream quality gates. Boundary discipline keeps
    the JSON shape here, not in a user-editable part.

    Static / global: the body (incl. the embedded schema doc) is a code
    constant, identical across runs, so it caches in the stable prefix. Only
    appended for subtasks that actually declare done-criteria.
    """
    from core.contracts.subtask_attestation_schema import ATTESTATION_SCHEMA_DOC

    template = SUBTASK_ATTESTATION
    body = template.render(attestation_schema_doc=ATTESTATION_SCHEMA_DOC)
    return _block_from(
        template,
        body,
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.GLOBAL,
        id="contract:subtask_attestation",
    )


def review_target_strategy(*, mode: str = "uncommitted") -> SystemPromptBlock:
    """Review strategy describing which change surface the reviewer inspects.

    M3: classified ``PROFILE`` / ``GLOBAL`` with mode-keyed id, mirror
    of :func:`change_handoff_strategy`.
    """
    normalized = _normalize_change_handoff_mode(mode)
    template = REVIEW_TARGET_TEMPLATES[normalized]
    body = template.render()
    return _block_from(
        template,
        body,
        stability=PromptStability.PROFILE,
        cache_scope=PromptCacheScope.GLOBAL,
        volatile_reason="depends on profile-configured review target",
        id=f"contract:review_target:mode={normalized}",
    )


def plan_artifact_boundary_contract() -> SystemPromptBlock:
    """Contract: planning agents emit plan content as their response;
    they never persist the plan file themselves.

    Architectural boundary: the plan file (``plan_*.md``,
    ``cross_plan.md``) is a run-level artifact materialized by the
    persistence layer from the agent's response text. This is the
    counterpart to ``mutates_artifacts``: that flag governs writes to
    the **project tree** (user code), this contract governs the
    **run-artifact** layer — distinct concerns that the previous
    user-editable wording in ``tasks/plan`` / ``tasks/cross_plan``
    conflated.

    Lives in a system-tail block so a project override of either task
    file cannot silently flip the agent into "write the file yourself"
    mode, which would either trip a Write-permission denial on
    read-only calls or produce a duplicate of the saved artifact.
    """
    body = PLAN_ARTIFACT_BOUNDARY.render()
    return _block_from(PLAN_ARTIFACT_BOUNDARY, body)


def coding_agent_compaction_contract() -> SystemPromptBlock:
    """Protected compaction contract for coding-agent summaries (ADR 0029).

    Defines the minimum information a compaction summary must
    preserve so a long-running coding agent can resume after a
    compaction event without losing decisions, evidence, or
    pending-work pointers. The canonical field list is the
    :data:`pipeline.prompts.contract_templates.REQUIRED_COMPACTION_PRESERVE_FIELDS`
    tuple — callers that operate on summaries (the future M14.4+
    compaction primitive, lab probes) import it from there to
    iterate the same canonical order the rendered body uses.

    M14.4 ships the contract as a code-owned ``SystemPromptBlock``
    so a future compaction phase can attach it via the standard
    ``_render_prompt_output`` ``system_tail`` slot. M14.4 itself
    does not invoke compaction; the contract is the load-bearing
    shape M14.4+ slices honour when they do.

    Boundary discipline: lives in code, never in editable
    role/task/format markdown. The prompt-boundary test suite
    pins this (no ``coding_agent_compaction.md`` may appear under
    ``_prompts/roles/`` / ``tasks/`` / ``formats/``).
    """
    body = CODING_AGENT_COMPACTION.render()
    return _block_from(CODING_AGENT_COMPACTION, body)


def cross_plan_json_contract(
    *,
    body_language: str | None = None,
    input_language: str | None = None,
) -> SystemPromptBlock:
    """Contract for the cross architect that emits a typed cross-plan JSON.

    ADR 0054: the cross architect now speaks JSON like the mono architect
    (cf. :func:`plan_json_contract`). The block forces JSON-only output and
    embeds ``CROSS_PLAN_SCHEMA_DOC`` so the architect needs no prompt-template
    support to know what to emit. Project overrides of the user-editable
    ``tasks/cross_plan`` / ``tasks/cross_replan`` parts cannot silently drop
    the parser contract — it is attached by the builder.

    The cross parser (``pipeline.cross_project.plan_parser.parse_cross_plan``)
    validates the object against ``core.contracts.cross_plan_schema`` and
    renders ``cross_plan.md`` from it; the marker grammar is gone.

    ``body_language`` selects the project language for human-readable JSON
    body fields (typically ``cfg.plan_language``). ``input_language``
    disambiguates when the task description and plan body should be in
    different languages (typically ``cfg.task_language``).
    """
    from core.contracts.cross_plan_schema import CROSS_PLAN_SCHEMA_DOC

    if body_language:
        if input_language and input_language != body_language:
            directive = (
                "The task description may be in "
                f"{input_language}; understand it but write the JSON body "
                "fields (short_summary, interface_contract, "
                "implementation_order, and each subtask's goal / spec / "
                f"produces / consumes) in {body_language}."
            )
        else:
            directive = (
                "Write the human-readable JSON body fields (short_summary, "
                "interface_contract, implementation_order, and each subtask's "
                f"goal / spec / produces / consumes) in {body_language}."
            )
        language_directive = "\n" + directive
    else:
        language_directive = ""

    body = CROSS_PLAN_JSON.render(
        language_directive=language_directive,
        cross_plan_schema_doc=CROSS_PLAN_SCHEMA_DOC,
    )
    sig = _language_signature(body_language, input_language)
    if sig:
        return _block_from(
            CROSS_PLAN_JSON,
            body,
            stability=PromptStability.PROFILE,
            cache_scope=PromptCacheScope.WORKSPACE,
            volatile_reason="depends on project/workspace language configuration",
            id=f"contract:cross_plan_json:v1:{sig}",
        )
    return _block_from(CROSS_PLAN_JSON, body)


def authoring_language_strategy(
    *, task_language: str | None = None,
) -> SystemPromptBlock | None:
    """Language posture for authoring/planning agents.

    Covers every non-JSON authoring surface (implement, repair_changes,
    plan, replan, decompose, hypothesis, readonly plan, runtime utility
    prompts).
    Covers natural-language response text, hypotheses, plan markdown, and
    handoff prose.

    Returns ``None`` when ``task_language`` is unset — callers filter
    ``None`` from their ``system_tail`` tuple so empty config produces
    no block at all (no trivial "use the default language" injection).

    This block is the system-tail counterpart of
    ``review_json_contract(body_language=...)``: the latter covers
    reviewer JSON body fields; this one covers everything else an
    authoring agent emits. User-editable role/task/format parts MUST
    NOT carry this directive — a project override of a role/task file
    could otherwise silently drop the configured language and the
    authoring agent would default to English without any error.
    """
    if not task_language or not task_language.strip():
        return None
    normalized = task_language.strip()
    body = AUTHORING_LANGUAGE.render(task_language=normalized)
    sig = _normalize_language(normalized)
    # M3: workspace-scoped policy — the developer picks a working
    # language for the project and keeps using it across rounds. The
    # block lives in the stable prefix; the M6 selector resends only
    # when the normalized language signature flips (id changes).
    return _block_from(
        AUTHORING_LANGUAGE,
        body,
        stability=PromptStability.PROFILE,
        cache_scope=PromptCacheScope.WORKSPACE,
        volatile_reason="depends on project/workspace language configuration",
        id=f"contract:authoring_language:v1:{sig}",
    )


def operator_waiver_reconciliation_text(
    *, body_language: str | None = None,
) -> str:
    """Code-owned reconciliation policy for a ``continue_with_waiver`` waiver.

    When the operator resolves a phase handoff with
    ``continue_with_waiver``, the machine verdict stays REJECTED but the
    operator durably accepted the listed findings. Downstream review gates
    must NOT reopen those findings as blocking. This policy text is the
    authoritative instruction injected (alongside the operator verdict) as
    the ``operator_waiver`` prompt part; it is code-owned — not a
    user-editable role/task/format part — so a project prompt override
    cannot weaken or drop the reconciliation rule. The review gate stays
    JSON-only; this changes only how the reviewer must weigh the waived
    findings, not the output schema.

    Returns a plain string (the builder wraps it in a typed TURN part with
    the operator verdict body). ``body_language`` appends a directive so
    the reviewer reasons in the configured project language.
    """
    text = (
        "OPERATOR WAIVER (phase-handoff continue_with_waiver): the "
        "findings listed below were reviewed and explicitly accepted by "
        "the operator with a reasoned verdict. The machine verdict is "
        "left REJECTED on the record, but you MUST NOT reopen the waived "
        "findings as blocking. Treat them as accepted-known and, absent "
        "other independent blockers, return APPROVED / ship_ready."
    )
    if body_language and body_language.strip():
        text += (
            "\nWrite the human-readable JSON fields in "
            f"{body_language.strip()}."
        )
    return text


def advisory_critique_reconciliation_text(
    *, body_language: str | None = None,
) -> str:
    """Code-owned framing for plan-reviewer findings forwarded into implement.

    When a ``validate_plan`` gate returned REJECTED but the profile proceeded
    to ``implement`` without a replan loop (a bypass), the rejected findings
    must still reach the implementation agent — but as *advisory* reviewer
    feedback, never as an operator command or a blocking gate. This policy text
    is the authoritative framing prepended to the forwarded findings body; it is
    code-owned — not a user-editable role/task/format part — so a project prompt
    override cannot weaken or drop the authority boundary. The findings stay
    advisory: the agent keeps implementing and is not asked to replan.

    Returns a plain string (the builder wraps it together with the findings in a
    typed reviewer_critique TURN part). ``body_language`` appends a directive so
    the agent reasons in the configured project language.
    """
    text = (
        "REVIEWER ADVISORY (plan-review findings, bypassed replan): treat "
        "this as reviewer advisory feedback. The plan reviewer rejected the "
        "plan, but the profile proceeded without a replan loop. Do not replan. "
        "Address applicable findings while implementing. If a finding is "
        "intentionally not applicable, say so in the implementation handoff."
    )
    if body_language and body_language.strip():
        text += (
            "\nWrite the human-readable response in "
            f"{body_language.strip()}."
        )
    return text
