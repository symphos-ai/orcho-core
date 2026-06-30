"""M14.2 — Re-fetchability classification for tool / runtime outputs.

ADR 0029 §"Tool-result clearing" splits Orcho-visible outputs into
four lifecycle classes. M14.3 will use the classification to decide
what tool clearing is allowed to drop; M14.4 will use it to decide
what compaction must preserve; M14.5 will use it to decide what
memory must persist. M14.2 itself is **pure taxonomy** — it does
not clear, compact, or persist anything. It only labels.

This narrowness is deliberate: clearing without a reliable taxonomy
is dangerous (silently dropping a decision-bearing finding is worse
than not clearing at all). M14.2 ships the labeling layer first so
later slices can build on a tested invariant.

## The four classes (verbatim from ADR 0029)

| Class | Examples | Default lifecycle |
|---|---|---|
| ``RE_FETCHABLE`` | file reads, search results, repeatable local command output, repeatable API reads | eligible for clearing after a bounded keep window |
| ``PERSISTED_ARTIFACT`` | plan files, review JSON, release gate JSON, saved patch/diff artifacts | active payload can be cleared after artifact path + digest are recorded |
| ``EPHEMERAL`` | upload-only data, non-repeatable external state, transient command output not saved elsewhere | not cleared unless first summarized or persisted |
| ``DECISION_BEARING`` | accepted plans, review findings, final gate blockers, unresolved assumptions | never dropped silently; summarize or persist before clearing |

## Safe-default rule

Unknown / unrecognized inputs classify as :data:`OutputClass.EPHEMERAL`.
EPHEMERAL means "not cleared unless first summarized or persisted",
so the M14.3 clearing primitive will leave unrecognized surfaces
alone instead of silently dropping them. This is the strictest
safe-by-default behaviour — never the most permissive.

## API surface

- :class:`OutputClass` — the enum.
- :class:`OutputDescriptor` — structured input for the generic
  :func:`classify_output` dispatch.
- :func:`classify_output` — generic entry point. Reads
  ``descriptor.surface`` and routes to the right rule table.
- :func:`classify_phase_output` — convenience for the common case
  "what class is this phase's output?".
- :func:`classify_prompt_part` — convenience for prompt-part
  classification used by M14.1 ``context_growth`` part-id review.

The rule tables (:data:`PHASE_CLASS_RULES`, :data:`PROMPT_PART_CLASS_RULES`)
are module-level constants so tests can inspect them directly.
M14.2 does not mutate them at runtime; future M14.x slices can
extend by appending new keys, never by reclassifying an existing
key without an ADR bump.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class OutputClass(StrEnum):
    """Lifecycle class for a tool/runtime output (ADR 0029)."""

    #: Regeneratable from a deterministic source (disk file,
    #: subprocess re-run, code-rendered constant). Safe to clear
    #: after a bounded keep window — re-reading recovers the
    #: payload.
    RE_FETCHABLE = "re_fetchable"

    #: Payload recorded on disk as an artifact (path + digest
    #: tracked). The active in-context copy can be cleared after
    #: the artifact reference is captured; the disk artifact
    #: remains the recoverable source.
    PERSISTED_ARTIFACT = "persisted_artifact"

    #: Upload-only data, non-repeatable external state, transient
    #: command output not saved elsewhere. Never cleared by
    #: policy unless first summarized or persisted to disk /
    #: memory. The safe default for unknown surfaces.
    EPHEMERAL = "ephemeral"

    #: Accepted plans, review findings, final gate blockers,
    #: unresolved assumptions. Never dropped silently — every
    #: clearing event must summarize or persist the content
    #: first. The most-protected class.
    DECISION_BEARING = "decision_bearing"


@dataclass(frozen=True)
class OutputDescriptor:
    """Structured input for :func:`classify_output`.

    ``surface`` names the producer layer (``"phase"``,
    ``"prompt_part"``, future ``"tool_result"`` / ``"runtime_event"``).

    ``name`` identifies the producer within that surface — phase
    name for ``"phase"``, prompt-part kind for ``"prompt_part"``.

    ``detail`` carries optional sub-identification (prompt-part
    name, tool name, command argv signature) the rule table can
    consult when ``name`` alone is ambiguous.
    """

    surface: str
    name: str = ""
    detail: str = ""


# ── Rule tables ──────────────────────────────────────────────────────────────


#: Class per phase name. M14.3 reads this to decide which phase
#: outputs are eligible for clearing or must be summarized first.
#:
#: Rationale per row:
#:
#: - ``plan`` / ``replan`` / ``decompose`` produce the accepted
#:   plan markdown — the load-bearing decision artefact for
#:   every downstream phase.
#: - ``validate_plan`` / ``review_changes`` / ``final_acceptance``
#:   emit reviewer findings, blockers, and ship verdicts that
#:   downstream slices anchor on.
#: - ``hypothesis`` / ``validate_hypothesis`` emit / approve
#:   architectural assumptions; dropping them silently risks
#:   re-deriving an assumption that the architect already weighed.
#: - ``implement`` / ``repair_changes`` write changes to the
#:   working tree; the diff itself is the recoverable artefact
#:   on disk, the agent's narrative output is summary text.
#: - Cross-project surfaces mirror their single-project counterparts.
#: - ``contract_check`` / ``compliance_check`` are gates; their
#:   verdict is decision-bearing.
PHASE_CLASS_RULES: Final[dict[str, OutputClass]] = {
    # Architect surfaces — output is the accepted/proposed plan.
    "plan":                    OutputClass.DECISION_BEARING,
    "replan":                  OutputClass.DECISION_BEARING,
    "decompose":               OutputClass.DECISION_BEARING,
    "readonly_plan":           OutputClass.DECISION_BEARING,
    "hypothesis":              OutputClass.DECISION_BEARING,

    # Reviewer surfaces — output is findings / verdict.
    "validate_plan":           OutputClass.DECISION_BEARING,
    "validate_hypothesis":     OutputClass.DECISION_BEARING,
    "review_changes":          OutputClass.DECISION_BEARING,
    "final_acceptance":        OutputClass.DECISION_BEARING,
    "contract_check":          OutputClass.DECISION_BEARING,
    "compliance_check":        OutputClass.DECISION_BEARING,

    # Implementer surfaces — diff on disk is the artefact.
    "implement":               OutputClass.PERSISTED_ARTIFACT,
    "repair_changes":          OutputClass.PERSISTED_ARTIFACT,

    # Cross-project surfaces mirror single-project semantics.
    "cross_plan":              OutputClass.DECISION_BEARING,
    "cross_replan":            OutputClass.DECISION_BEARING,
    "cross_validate_plan":     OutputClass.DECISION_BEARING,
    "cross_final_acceptance":  OutputClass.DECISION_BEARING,
}


#: Class per ``PromptPart.kind``. Reflects the M10.5 / M14.1
#: typed-part taxonomy.
#:
#: Rationale per row:
#:
#: - ``role`` / ``task`` / ``format`` resolve from
#:   ``_prompts/`` via the override chain. Re-rendering through
#:   the same composer recovers identical bytes.
#: - ``system_tail`` blocks render from code-owned templates
#:   (``pipeline.prompts.contracts``); re-rendering recovers.
#: - ``context`` (project_dir + plugin) and ``codemap`` are
#:   regenerated from runtime project facts; not durable but
#:   reproducible.
#: - ``handoff_contract`` re-renders from cross-run state.
#: - ``minimal_intent`` is the code-owned ablation fallback;
#:   regenerated from a fixed template.
#: - ``text_prefix`` wraps per-run TEXT attachments — re-read from
#:   disk.
#: - ``artifact`` carries reviewed-file payload (M4); the file
#:   exists on disk under the project's artefact path.
#: - ``plan_contract`` is the typed plan rendered into the
#:   prompt — the accepted plan, decision-bearing.
#: - ``hypothesis_suffix`` carries a validated/rejected
#:   hypothesis — an architectural decision.
#: - ``feedback`` carries reviewer critique / repair body —
#:   findings the next round must address.
#: - ``turn_input`` carries the user task framing — the
#:   load-bearing decision context every phase anchors on.
PROMPT_PART_CLASS_RULES: Final[dict[str, OutputClass]] = {
    # Editable composable parts (re-rendered via composer).
    "role":              OutputClass.RE_FETCHABLE,
    "task":              OutputClass.RE_FETCHABLE,
    "format":            OutputClass.RE_FETCHABLE,
    # Code-owned and regenerated-from-runtime parts.
    "system_tail":       OutputClass.RE_FETCHABLE,
    "context":           OutputClass.RE_FETCHABLE,
    "codemap":           OutputClass.RE_FETCHABLE,
    "handoff_contract":  OutputClass.RE_FETCHABLE,
    "minimal_intent":    OutputClass.RE_FETCHABLE,
    "text_prefix":       OutputClass.RE_FETCHABLE,
    # File-backed artifacts (M4 reviewed file body).
    "artifact":          OutputClass.PERSISTED_ARTIFACT,
    # Decision-bearing typed parts.
    "plan_contract":     OutputClass.DECISION_BEARING,
    "plan_tasks":        OutputClass.DECISION_BEARING,
    "hypothesis_suffix": OutputClass.DECISION_BEARING,
    "feedback":          OutputClass.DECISION_BEARING,
    "reviewer_critique": OutputClass.DECISION_BEARING,
    "human_feedback":    OutputClass.DECISION_BEARING,
    "turn_input":        OutputClass.DECISION_BEARING,
    # ADR 0066 repair-receipt protocol: the repairer's claim and the fresh
    # review subject the next reviewer must verify against. Contract-bearing
    # — the reviewer decides on these — so they must never be cleared as
    # ephemeral noise.
    "repair_receipt":         OutputClass.DECISION_BEARING,
    "current_review_subject": OutputClass.DECISION_BEARING,
    # subtask_dag (session-aware) executable instruction. The current
    # subtask is the one decision-bearing turn the developer must act on —
    # the sibling of ``current_review_subject`` on the build side. It must
    # never be cleared as re-fetchable noise (it is not regeneratable from
    # disk like the plain ``task`` part). The skill block, by contrast, is
    # reference guidance recoverable from SKILL.md, so it is RE_FETCHABLE.
    "current_subtask":        OutputClass.DECISION_BEARING,
    "skill":                  OutputClass.RE_FETCHABLE,
    # subtask_dag P2 reshape: the compact DAG map is navigation (regeneratable
    # from the plan), and the background notice framing the plan contract is
    # likewise reference text — both RE_FETCHABLE, not the live instruction.
    "execution_plan_context": OutputClass.RE_FETCHABLE,
    "execution_scope_notice": OutputClass.RE_FETCHABLE,
    # subtask_dag P3: bounded quoted output from declared upstream deps. A claim
    # the downstream subtask reasons about (sibling of repair_receipt) — never
    # silently cleared, even though it is framed as a hint, not proof.
    "upstream_receipt":       OutputClass.DECISION_BEARING,
}


_KNOWN_SURFACES: Final[frozenset[str]] = frozenset({"phase", "prompt_part"})


# ── Classifier API ───────────────────────────────────────────────────────────


def classify_phase_output(phase: str) -> OutputClass:
    """Classify a phase's main output by phase name.

    Unknown phases fall back to :data:`OutputClass.EPHEMERAL` — the
    M14.3 clearing primitive will not touch them, which is the
    correct behaviour for any phase we have not explicitly
    examined yet.
    """
    if not isinstance(phase, str):
        return OutputClass.EPHEMERAL
    return PHASE_CLASS_RULES.get(phase.strip(), OutputClass.EPHEMERAL)


def classify_prompt_part(
    *,
    kind: str,
    name: str = "",
    source: str = "",
) -> OutputClass:
    """Classify a :class:`pipeline.prompts.types.PromptPart` by kind.

    ``name`` and ``source`` are accepted for future
    sub-classification (e.g. distinguishing an attachments
    ``text_prefix`` from a future tool-result ``text_prefix``).
    M14.2 only consults ``kind`` — the additional arguments are
    plumbed now so callers do not change in M14.3 when
    sub-discrimination starts.

    Unknown kinds fall back to :data:`OutputClass.EPHEMERAL`.
    """
    if not isinstance(kind, str):
        return OutputClass.EPHEMERAL
    # ``name`` / ``source`` are intentionally unused in M14.2; the
    # signature reserves them for future sub-rule extensions.
    _ = name
    _ = source
    return PROMPT_PART_CLASS_RULES.get(kind.strip(), OutputClass.EPHEMERAL)


def classify_output(descriptor: OutputDescriptor) -> OutputClass:
    """Generic dispatch over :class:`OutputDescriptor`.

    Routes to :func:`classify_phase_output` when
    ``descriptor.surface == "phase"`` and to
    :func:`classify_prompt_part` when
    ``descriptor.surface == "prompt_part"``. Unknown surfaces fall
    back to :data:`OutputClass.EPHEMERAL` so a future surface
    landing without a matching rule does not silently inherit a
    clearing-eligible class.
    """
    if not isinstance(descriptor, OutputDescriptor):
        return OutputClass.EPHEMERAL
    surface = (descriptor.surface or "").strip()
    if surface == "phase":
        return classify_phase_output(descriptor.name)
    if surface == "prompt_part":
        return classify_prompt_part(
            kind=descriptor.name,
            name=descriptor.detail,
        )
    return OutputClass.EPHEMERAL


__all__ = [
    "PHASE_CLASS_RULES",
    "PROMPT_PART_CLASS_RULES",
    "OutputClass",
    "OutputDescriptor",
    "classify_output",
    "classify_phase_output",
    "classify_prompt_part",
]
