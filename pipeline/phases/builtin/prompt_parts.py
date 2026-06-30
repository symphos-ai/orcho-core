# SPDX-License-Identifier: Apache-2.0
"""Prompt-part composition helpers for the builtin phase handlers.

Pure leaf helpers that wrap out-of-builder content (architect prefix,
repo codemap, hypothesis suffix) as typed ``PromptPart`` objects and
select the multimodal subset of run attachments. No dependency on the
phase handlers — heavy prompt imports stay lazy so this module is cheap
to import and free of cycles back into ``pipeline.phases.builtin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


def _multimodal_attachments(state: PipelineState) -> tuple:
    """Return the subset of ``state.attachments`` the runtime accepts via
    its ``invoke(attachments=...)`` kwarg.

    TEXT attachments are rendered into the prompt outside the runtime by
    ``_plan_prompt_prefix`` / ``render_text_block``; passing them through
    ``invoke`` would double-inject. Multimodal kinds (IMAGE, BINARY) are
    runtime-owned and ride as a kwarg.
    """
    if not state.attachments:
        return ()
    from pipeline.runtime.roles import AttachmentKind
    return tuple(
        a for a in state.attachments if a.kind is not AttachmentKind.TEXT
    )


def _text_prefix_part(body: str) -> Any:
    """Wrap an out-of-builder ``prompt_prefix`` block as a PromptPart.

    The architect prefix carries Phase 4.5 TEXT attachments — content
    that varies by run but stays stable across rounds within one run.
    Classify ``PROFILE`` / ``WORKSPACE`` so the M2 envelope partitioner
    keeps it in the cacheable prefix when wire layout permits, and bake
    a content hash into the part id so a mid-run prefix edit (rare but
    possible) flips the M5 composite key and forces a resend through the
    M6 selector.
    """
    import hashlib

    from pipeline.prompts.types import (
        PromptCacheScope,
        PromptLayer,
        PromptPart,
        PromptStability,
    )

    sig = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    # ADR 0028 / M10.5 Step 3 provenance rule: body is workspace
    # runtime data (per-run attachments), not a code or template
    # constant. ``source="artifact"`` is the truthful label.
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


def _codemap_part(body: str) -> Any:
    """Wrap the architect repo-map block as a PromptPart.

    The repo map content is stable across rounds within a run, but
    M8 keeps it ``TURN`` / ``NONE`` to avoid premature optimization —
    the wire savings vs the cache-correctness risk are the wrong
    trade for the first architect wiring. M10 may promote this to
    ``RUN`` / ``SESSION`` once tests prove cache-key stability is
    safe across legitimate codemap regeneration mid-run.
    """
    from pipeline.prompts.types import (
        PromptCacheScope,
        PromptLayer,
        PromptPart,
        PromptStability,
    )

    # ADR 0028 / M10.5 Step 3 provenance rule: body is the
    # runtime-generated repo map (file tree / hot spots) — not a
    # code constant. ``source="artifact"`` reflects that.
    return PromptPart(
        kind="codemap",
        name="repo_map",
        source="artifact",
        body=body,
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="repo map content varies per run; M8 conservative",
        id="codemap:repo_map",
    )


def _hypothesis_suffix_part(body: str) -> Any:
    """Wrap a validated/rejected hypothesis suffix as a PromptPart.

    Delegates to :func:`pipeline.prompts.turn.hypothesis_suffix_part`
    which is the canonical implementation shared across the mono plan
    handler and the cross-plan loop.
    """
    from pipeline.prompts.turn import hypothesis_suffix_part as _hsp
    return _hsp(body)


# Prompt-only *preview* of the gate plan. Deliberately distinct from the
# executable routing plan key (``gate_repair.VERIFICATION_GATE_ROUTING_PLAN_KEY``):
# the preview is advisory, may be built early (e.g. at the ``plan`` prompt before
# ``implement`` mutates the tree, so its path-based selection can be empty), and
# is used ONLY for prompt text. Gate routing never reads it — prompt timing must
# not affect which gates actually run.
_GATE_PROMPT_PREVIEW_KEY = "verification_gate_prompt_preview"


def _resolve_gate_plan(state: PipelineState, contract: Any) -> Any:
    """Build (once, memoized) the **prompt-preview** gate plan.

    The Stage 4 ``ScheduledGatePlan`` is built from the declared contract +
    a :class:`SelectionContext` derived from the contract's ``work_mode``, the
    run worktree's changed files at prompt-build time (best-effort), and the
    operator/profile ``task_kind`` / ``operator_sets`` (declared on the contract,
    overridable via ``state.extras`` — see ``selection_context_from_extras``).

    This preview is **advisory only** and is cached under a key distinct from the
    executable routing plan — the prompt may be built before ``implement`` runs,
    so its path-based selection can be incomplete. The executable gate routing
    (``pipeline.project.gate_repair``) builds and caches its own plan at the
    routing point and never reads this preview, so prompt timing cannot suppress
    a gate that becomes relevant only after ``implement``.
    """
    cached = state.extras.get(_GATE_PROMPT_PREVIEW_KEY)
    if cached is not None:
        return cached

    from pipeline.verification_selection import (
        build_scheduled_gate_plan,
        selection_context_from_extras,
    )

    ctx = state.extras.get("verification_placeholders")
    checkout = getattr(ctx, "checkout", "") or ""
    touched: tuple[str, ...] = ()
    if checkout:
        try:
            from core.io.git_helpers import git_changed_files

            touched = tuple(git_changed_files(checkout))
        except Exception:  # noqa: BLE001 — selection touched-paths is best-effort
            touched = ()
    plan = build_scheduled_gate_plan(
        contract,
        selection_context_from_extras(
            state.extras, contract, touched_paths=touched,
        ),
    )
    state.extras[_GATE_PROMPT_PREVIEW_KEY] = plan
    return plan


def _verification_contract_part(state: PipelineState, phase: str) -> Any:
    """Wrap the phase-limited verification-contract block as a PromptPart.

    Reads the validated contract + resolved ``PlaceholderContext`` that the
    run coordinator stored in ``state.extras`` (keys ``verification_contract``
    / ``verification_placeholders``). When the contract declares ``gate_sets`` /
    ``selection`` the block is projected from the resolved ``ScheduledGatePlan``
    (effective policy/action after the work_mode transform) via
    :func:`pipeline.verification_contract.render_phase_gate_block`; otherwise it
    falls back to the Stage 1 schedule projection
    (:func:`pipeline.verification_contract.render_phase_block`). Returns ``None``
    when no contract is declared OR the phase has nothing to surface — callers
    then leave the prompt bytes byte-identical to the no-contract path.

    Read-only projection: the block is informational only; nothing here executes
    a command or blocks a transition. It is a RUN-scoped dynamic part (stability
    ``RUN``) that rides in the turn payload (``cache_scope`` SESSION), never the
    cacheable cross-run prefix — the block depends on per-run project config and
    the phase, so it must not pollute a stable cache prefix.
    """
    contract = state.extras.get("verification_contract")
    if contract is None:
        return None

    from pipeline.verification_contract import (
        PlaceholderContext,
        render_phase_block,
        render_phase_gate_block,
    )

    ctx = state.extras.get("verification_placeholders")
    if ctx is None:
        ctx = PlaceholderContext()

    has_plan = bool(getattr(contract, "gate_sets", None)) or bool(
        getattr(contract, "selection", ()),
    )
    if has_plan:
        plan = _resolve_gate_plan(state, contract)
        body = render_phase_gate_block(contract, plan, phase, ctx)
    else:
        body = render_phase_block(contract, phase, ctx)
    if not body:
        return None

    import hashlib

    from pipeline.prompts.types import (
        PromptCacheScope,
        PromptLayer,
        PromptPart,
        PromptStability,
    )

    sig = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return PromptPart(
        kind="verification_contract",
        name=phase,
        source="code-owned",
        body=body,
        layer=PromptLayer.CONTEXT,
        stability=PromptStability.RUN,
        cache_scope=PromptCacheScope.SESSION,
        volatile_reason="per-run verification contract block; phase-scoped",
        id=f"verification_contract:{phase}:{sig}",
    )
