"""Typed prompt-part records (ADR 0026 / ADR 0060).

:class:`PromptPart` is the unit of prompt composition.  It carries the
rendered body text for a labelled chunk of a prompt, plus ADR-0026 metadata
(``id``, ``layer``, ``stability``, ``cache_scope``, ``volatile_reason``) and
an optional ``artifact_path`` evidence pointer.

The canonical render surface is :class:`~pipeline.prompts.turn.PromptTurn`
(ADR 0060): an ordered tuple of :class:`~pipeline.prompts.turn.PromptSegment`
objects that together form the exact wire bytes passed to the agent runtime.
The debug renderer in :mod:`core.io.transcript` consumes a
:class:`~pipeline.prompts.turn.PromptTraceView` derived from the effective
turn.

ADR 0026 metadata fields default conservatively so existing callers that only
pass ``kind/name/source/body[/version]`` keep working unchanged — the
rendered prompt string is byte-identical to the pre-ADR-0026 output.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PromptLayer(StrEnum):
    """Structural layer a part contributes to.

    Layer values are ordered (cheapest cache scope first):

    - :data:`BOOTSTRAP` — Orcho-wide invariants. Typically
      static.
    - :data:`ROLE` — agent persona / posture. Typically static.
    - :data:`PHASE` — phase-specific procedure or task body.
      Typically static or profile-dependent.
    - :data:`CONTRACT` — parser/schema contract or protected
      policy. Typically static or profile-dependent.
    - :data:`CONTEXT` — per-run project facts (codemap,
      attachments). Typically run-stable.
    - :data:`TURN` — per-round payload (task text, artifact body,
      feedback). Always volatile.
    """

    BOOTSTRAP = "bootstrap"
    ROLE = "role"
    PHASE = "phase"
    CONTRACT = "contract"
    CONTEXT = "context"
    TURN = "turn"


class PromptStability(StrEnum):
    """How often the part body changes.

    - :data:`STATIC` — same across runs until the part version
      bumps.
    - :data:`PROFILE` — depends on profile / handoff mode, not
      task.
    - :data:`RUN` — depends on project, task, attachments, run
      config.
    - :data:`TURN` — depends on the current artifact, diff,
      feedback, or round.
    """

    STATIC = "static"
    PROFILE = "profile"
    RUN = "run"
    TURN = "turn"


class PromptCacheScope(StrEnum):
    """How widely the part body can be cached on a runtime session.

    Tiers are ordered broadest first (more reuse → narrower):

    - :data:`GLOBAL` — safe stable prefix across projects and
      runs (code/templated invariants).
    - :data:`WORKSPACE` — stable across runs inside the same
      workspace (workspace-level prompt overrides, workspace
      AGENTS / injections).
    - :data:`PROJECT` — stable across runs targeting the same
      project (project_dir, project-level AGENTS / plugin config,
      project-level prompt overrides). Per ADR 0028 (M10.5) cross
      runs that touch multiple projects let GLOBAL + WORKSPACE
      bytes survive a project switch while PROJECT bytes swap.
    - :data:`SESSION` — stable only inside one physical runtime
      session.
    - :data:`NONE` — must always be re-sent.
    """

    GLOBAL = "global"
    WORKSPACE = "workspace"
    PROJECT = "project"
    SESSION = "session"
    NONE = "none"


# Layer-order index used by :func:`sort_parts_by_layer`. Earlier
# layers belong in the stable prefix; later layers belong in the
# turn payload.
_LAYER_ORDER: tuple[PromptLayer, ...] = (
    PromptLayer.BOOTSTRAP,
    PromptLayer.ROLE,
    PromptLayer.PHASE,
    PromptLayer.CONTRACT,
    PromptLayer.CONTEXT,
    PromptLayer.TURN,
)
_LAYER_INDEX: dict[PromptLayer, int] = {
    layer: idx for idx, layer in enumerate(_LAYER_ORDER)
}


# Default :class:`PromptLayer` for legacy ``kind`` values, used when
# a caller constructs a :class:`PromptPart` without passing
# ``layer`` explicitly. Keeps every pre-ADR-0026 construction site
# in the right partition without a code change.
_DEFAULT_LAYER_BY_KIND: dict[str, PromptLayer] = {
    "role": PromptLayer.ROLE,
    "task": PromptLayer.PHASE,
    "format": PromptLayer.PHASE,
    "minimal_intent": PromptLayer.PHASE,
    "system_tail": PromptLayer.CONTRACT,
}


def _default_layer_for_kind(kind: str) -> PromptLayer:
    """Return the conservative default layer for a legacy ``kind``."""
    return _DEFAULT_LAYER_BY_KIND.get(kind, PromptLayer.CONTEXT)


@dataclass(frozen=True)
class PromptPart:
    """One labelled chunk of a composed prompt.

    ``kind`` is the structural slot:

    - ``"role"`` / ``"task"`` / ``"format"`` — composable parts from
      ``_prompts/`` resolved through the project → workspace → core
      chain.
    - ``"minimal_intent"`` — code-owned fallback rendered by
      :mod:`pipeline.prompts.minimal_intents` when the professional
      prompt mode ablation knob is off.
    - ``"system_tail"`` — code-owned block from
      :mod:`pipeline.prompts.contracts` appended after user input.

    ``source`` records where the body came from: ``project`` /
    ``workspace`` / ``core`` for composable parts, ``code-owned``
    for contracts and minimal intents, ``artifact`` for content read
    from a persisted prior-phase artifact (reviewer critique, test
    output, repair diff, reviewed file body), and ``operator`` for
    content read from an operator-supplied decision artifact (e.g.
    ``phase_handoff_decide(retry_feedback)`` feedback text).

    ADR-0026 metadata (``id``, ``layer``, ``stability``,
    ``cache_scope``, ``volatile_reason``) defaults conservatively to
    ``static`` + ``global`` with the layer derived from ``kind``.
    Callers that need session-aware behaviour set the metadata
    explicitly; legacy callers that only pass
    ``kind/name/source/body[/version]`` get a part that renders the
    same way as before and lives in the stable prefix.

    ``artifact_path`` is an optional trace/evidence pointer for parts
    whose body was sourced from a persisted on-disk artifact (reviewer
    file under validate_plan / validate_hypothesis). It is **metadata
    only**: never embedded in the wire body sent to the model, never
    included in ``_part_digest``, and therefore never hash-bearing. For
    identical ``body`` two parts with different ``artifact_path`` values
    produce identical wire bytes and identical payload_hash. Debug
    transcripts and evidence dumps surface the path as a separate
    field/heading segment so operators can correlate the part with the
    artifact on disk without polluting the model-facing prompt.
    """

    kind: str
    name: str
    source: str
    body: str
    version: int | None = None
    id: str = ""
    layer: PromptLayer | None = None
    stability: PromptStability = PromptStability.STATIC
    cache_scope: PromptCacheScope = PromptCacheScope.GLOBAL
    volatile_reason: str | None = None
    artifact_path: str | None = None

    def __post_init__(self) -> None:
        if self.layer is None:
            object.__setattr__(self, "layer", _default_layer_for_kind(self.kind))
        if not self.id:
            # Derived id is useless when both kind and name are empty;
            # ``f"{'':}{':'}{''}"`` collapses to ``":"`` and slips past
            # a plain truthiness check. Require at least one segment
            # to be non-empty before deriving.
            if not self.kind and not self.name:
                raise ValueError(
                    "PromptPart requires a non-empty id (or non-empty "
                    "kind/name for derivation).",
                )
            object.__setattr__(self, "id", f"{self.kind}:{self.name}")

        # Validate volatility coupling. ADR 0026 §"Validation":
        # volatile parts (anything that is not static-and-global) must
        # carry a ``volatile_reason``; static-global parts must not.
        is_volatile = (
            self.stability is not PromptStability.STATIC
            or self.cache_scope is PromptCacheScope.NONE
        )
        if is_volatile and not self.volatile_reason:
            raise ValueError(
                f"PromptPart {self.id!r} is volatile "
                f"(stability={self.stability.value}, "
                f"cache_scope={self.cache_scope.value}) and requires a "
                "volatile_reason.",
            )
        is_static_global = (
            self.stability is PromptStability.STATIC
            and self.cache_scope is PromptCacheScope.GLOBAL
        )
        if is_static_global and self.volatile_reason is not None:
            raise ValueError(
                f"PromptPart {self.id!r} is static + global and must not "
                "carry a volatile_reason.",
            )

    @classmethod
    def from_legacy(
        cls,
        *,
        kind: str,
        name: str,
        source: str,
        body: str,
        version: int | None = None,
    ) -> PromptPart:
        """Construct a part from the pre-ADR-0026 field set.

        Equivalent to calling :class:`PromptPart` with the same
        positional fields and letting every ADR-0026 field take its
        default. Exists so callers that explicitly want the legacy
        adapter path read clearly at the call site.
        """
        return cls(
            kind=kind,
            name=name,
            source=source,
            body=body,
            version=version,
        )



def sort_parts_by_layer(
    parts: tuple[PromptPart, ...] | list[PromptPart],
) -> tuple[PromptPart, ...]:
    """Return ``parts`` ordered by :class:`PromptLayer` index.

    The sort is stable: parts that share a layer keep their original
    relative order. Used by the prefix/turn-payload partitioner that
    M2 builds on top of these records.
    """
    return tuple(
        sorted(
            parts,
            key=lambda p: _LAYER_INDEX.get(p.layer or PromptLayer.CONTEXT, 99),
        ),
    )


__all__ = [
    "PromptCacheScope",
    "PromptLayer",
    "PromptPart",
    "PromptStability",
    "sort_parts_by_layer",
]
