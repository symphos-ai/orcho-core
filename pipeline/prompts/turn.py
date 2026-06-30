"""PromptTurn — the canonical prompt render surface (ADR 0060).

A prompt is an ordered typed segment stream.  Every wire string, debug trace,
session-selection envelope, and delta surface is a projection of that stream:

- ``PromptTurn.text``        — ``"".join(s.text for s in segments)``
- ``PromptTurn.parts``       — segment parts in wire order
- ``PromptTurn.envelope()``  — :class:`~pipeline.prompts.envelope.PromptRenderEnvelope`
- ``PromptTurn.trace_view()``— :class:`PromptTraceView` for debug transcript

Use :class:`PromptTurnEditor` to build or modify turns without raw string
manipulation.  Call ``turn.render_selected(selected_parts)`` to produce a
rebased delta whose ``.text`` is byte-identical to the old
``"\\n\\n".join(p.body for p in selected if p.body).strip()`` output.

See ``docs/adr/0060-prompt-turn-canonical-render-surface.md``.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pipeline.prompts.types import (
    PromptCacheScope,
    PromptLayer,
    PromptPart,
    PromptStability,
)

if TYPE_CHECKING:
    from pipeline.prompts.envelope import PromptRenderEnvelope


@dataclass(frozen=True)
class PromptSegment:
    """One wire segment in a :class:`PromptTurn`.

    ``text`` is the exact bytes contributed to the wire prompt (including any
    leading separator glue for non-first segments).  ``part`` is the
    :class:`~pipeline.prompts.types.PromptPart` that owns those bytes.

    ``source_segment_id`` links a segment back to its origin in the source
    turn.  For freshly built (non-rebased) segments it equals ``segment_id``;
    for segments produced by :meth:`PromptTurn.render_selected` it carries the
    original ``segment_id`` of the source segment so trace/debug/evidence
    consumers can correlate the rebased segment with its source without
    parsing strings.  When omitted, it defaults to ``segment_id``.

    Structural invariants (enforced in ``__post_init__``):

    - If ``part.body`` is non-empty, ``part.body`` occurs in ``text``
      **exactly once**; the remainder is separator glue only
      (``text == prefix_glue + body + suffix_glue``).
    - If ``part.body`` is empty, ``text`` must be the empty string
      (zero wire bytes, no glue).
    - ``segment_id`` must be unique within a :class:`PromptTurn`.
    """

    text: str
    part: PromptPart
    segment_id: str
    source_segment_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_segment_id is None:
            object.__setattr__(self, "source_segment_id", self.segment_id)
        if self.part.body:
            count = self.text.count(self.part.body)
            if count != 1:
                raise ValueError(
                    f"PromptSegment invariant violated for {self.segment_id!r}: "
                    f"part.body must appear exactly once in segment.text "
                    f"(found {count} occurrences). "
                    f"body={self.part.body!r:.60}, text={self.text!r:.80}"
                )
        else:
            if self.text != "":
                raise ValueError(
                    f"PromptSegment invariant violated for {self.segment_id!r}: "
                    f"empty-body part must have text='' (got {self.text!r:.60})"
                )


@dataclass(frozen=True)
class PromptTraceView:
    """Debug-transcript projection of a :class:`PromptTurn`.

    ``segments`` carries the **effective wire segments** after full or delta
    rendering.  ``segment.text`` is the real wire bytes per segment (including
    separator glue); ``segment.part`` carries classification metadata.  The
    debug renderer iterates ``segments`` for frame bodies and uses
    ``segment.part`` for manifest / totals.

    ``text`` equals ``"".join(s.text for s in segments)`` — the effective wire
    string (pre-computed for callers that only need the string).
    """

    text: str
    segments: tuple[PromptSegment, ...]

    @property
    def parts(self) -> tuple[PromptPart, ...]:
        """Part sequence in wire order."""
        return tuple(s.part for s in self.segments)


@dataclass(frozen=True)
class PromptTurn:
    """Canonical prompt object returned by every runtime builder.

    A turn is an ordered tuple of :class:`PromptSegment` instances.  Every
    meaningful byte in the wire prompt belongs to a segment, and every segment
    owns its separator glue (the ``"\\n\\n"`` between adjacent content blocks).

    Projections:

    - :attr:`text`            — wire-identical string for ``agent.invoke``
    - :attr:`parts`           — ordered :class:`PromptPart` sequence (metadata)
    - :meth:`envelope`        — :class:`PromptRenderEnvelope` for cache selection
    - :meth:`trace_view`      — :class:`PromptTraceView` for debug transcript
    - :meth:`render_selected` — rebased delta subset
    """

    segments: tuple[PromptSegment, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Structural invariant (see PromptSegment docstring): segment_id is
        # the positional identity within a turn and must be unique. Parts may
        # legitimately repeat across segments (id@version is a cache key, not
        # a positional id), so uniqueness is enforced on segment_id only.
        ids = [s.segment_id for s in self.segments]
        if len(ids) != len(set(ids)):
            from collections import Counter
            dupes = sorted(sid for sid, n in Counter(ids).items() if n > 1)
            raise ValueError(
                f"PromptTurn: duplicate segment_id(s) {dupes!r}; "
                f"segment_id must be unique within a turn."
            )

    @property
    def text(self) -> str:
        """Wire-identical joined string of all segment texts."""
        return "".join(s.text for s in self.segments)

    @property
    def parts(self) -> tuple[PromptPart, ...]:
        """Ordered part sequence in physical wire order."""
        return tuple(s.part for s in self.segments)

    def envelope(self) -> PromptRenderEnvelope:
        """Build a :class:`PromptRenderEnvelope` from this turn.

        Cache/session selection always uses the **source** turn's envelope,
        never the effective (delta) turn's envelope.
        """
        from pipeline.prompts.envelope import make_render_envelope
        return make_render_envelope(text=self.text, parts=self.parts)

    def trace_view(self) -> PromptTraceView:
        """Return a :class:`PromptTraceView` for the debug transcript."""
        return PromptTraceView(text=self.text, segments=self.segments)

    def render_selected(
        self,
        selected_parts: Sequence[PromptPart],
    ) -> PromptTurn:
        """Return a rebased :class:`PromptTurn` containing only *selected_parts*.

        Maps each entry in *selected_parts* to its corresponding segment by
        **object identity + occurrence count**: the Nth occurrence of a given
        ``id(part)`` in *selected_parts* matches the Nth segment in this turn
        whose ``.part`` is that same object.

        The returned turn's segments carry rebased separator glue so that::

            effective_turn.text
            == "\\n\\n".join(p.body for p in selected_parts if p.body).strip()

        Rebased segments carry a new unique ``segment_id`` of the form
        ``"rebased:<original-id>"`` plus a structured ``source_segment_id``
        field pointing at the original segment_id; trace/debug/evidence
        consumers should use ``source_segment_id`` rather than parsing the
        rebased name.
        Empty-body selected parts contribute zero wire bytes (text="").
        """
        # Build index: id(part_obj) -> [segment, ...] in wire order
        segs_by_part_obj: dict[int, list[PromptSegment]] = defaultdict(list)
        for seg in self.segments:
            segs_by_part_obj[id(seg.part)].append(seg)

        # Match each selected part to its occurrence-indexed segment.
        # Fail fast on a selected part with no matching segment: silently
        # dropping it would ship an incomplete delta wire prompt while the
        # cache/session bookkeeping still believed the part was sent — the
        # exact silent-truncation hazard the delta path must never have.
        occurrence: dict[int, int] = defaultdict(int)
        selected_segs: list[PromptSegment] = []
        for part in selected_parts:
            pid = id(part)
            idx = occurrence[pid]
            occurrence[pid] += 1
            candidates = segs_by_part_obj.get(pid, [])
            if idx >= len(candidates):
                raise ValueError(
                    f"PromptTurn.render_selected: selected part "
                    f"{getattr(part, 'id', part)!r} (occurrence #{idx}) has no "
                    f"matching segment in the source turn. Selected parts must "
                    f"be a subset of this turn's parts, by object identity."
                )
            selected_segs.append(candidates[idx])

        if not selected_segs:
            return PromptTurn(segments=())

        # Rebase: rebuild with fresh separator glue
        rebased: list[PromptSegment] = []
        non_empty_count = 0
        for seg in selected_segs:
            new_id = f"rebased:{seg.segment_id}"
            if not seg.part.body:
                rebased.append(PromptSegment(
                    text="",
                    part=seg.part,
                    segment_id=new_id,
                    source_segment_id=seg.segment_id,
                ))
            else:
                prefix = "" if non_empty_count == 0 else "\n\n"
                rebased.append(PromptSegment(
                    text=prefix + seg.part.body,
                    part=seg.part,
                    segment_id=new_id,
                    source_segment_id=seg.segment_id,
                ))
                non_empty_count += 1

        return PromptTurn(segments=tuple(rebased))


@dataclass(frozen=True)
class PromptDelta:
    """Metadata record for a full-vs-delta invocation decision.

    ``source_turn`` is the full prompt as built by the builder.
    ``effective_turn`` is what actually goes on the wire (equals
    ``source_turn`` for full renders; a rebased subset for delta renders).

    Cache/session selection uses ``source_turn.envelope()``.
    Debug transcript uses ``effective_turn.trace_view()``.
    M12 trace records source prefix/payload hashes + effective wire text.
    """

    source_turn: PromptTurn
    effective_turn: PromptTurn
    selected_segment_ids: tuple[str, ...]
    selected_part_keys: tuple[str, ...]
    omitted_part_keys: tuple[str, ...]
    render_mode: str  # "full" | "delta"


class PromptTurnEditor:
    """Mutable builder/editor for :class:`PromptTurn` objects.

    Accepts a base turn (or ``None`` for a fresh build) and accumulates
    :class:`PromptPart` additions via :meth:`prepend`, :meth:`append`, and
    :meth:`inject_mid`.  Call :meth:`build` to produce the final
    :class:`PromptTurn` with separator glue baked correctly.

    All editing methods return ``self`` for fluency::

        turn = (
            PromptTurnEditor(base_turn)
            .prepend(prefix_part)
            .append(codemap_part)
            .build()
        )
    """

    def __init__(self, base: PromptTurn | None = None) -> None:
        self._entries: list[tuple[PromptPart, str]] = []
        if base is not None:
            for seg in base.segments:
                self._entries.append((seg.part, seg.segment_id))

    def _next_id(self, part: PromptPart, prefix: str = "edit") -> str:
        return f"{prefix}:{part.id}:{len(self._entries)}"

    def prepend(
        self,
        part: PromptPart,
        *,
        segment_id: str | None = None,
    ) -> PromptTurnEditor:
        """Insert *part* before all existing entries.

        Empty-body parts are kept (they carry envelope/cache identity and
        build into a ``text=""`` segment with zero wire bytes), matching
        :meth:`inject_mid` and :meth:`build`.
        """
        sid = segment_id or self._next_id(part, "prepend")
        self._entries.insert(0, (part, sid))
        return self

    def append(
        self,
        part: PromptPart,
        *,
        segment_id: str | None = None,
    ) -> PromptTurnEditor:
        """Insert *part* after all existing entries.

        Empty-body parts are kept (they carry envelope/cache identity and
        build into a ``text=""`` segment with zero wire bytes), matching
        :meth:`inject_mid` and :meth:`build`.
        """
        sid = segment_id or self._next_id(part, "append")
        self._entries.append((part, sid))
        return self

    def inject_mid(
        self,
        part: PromptPart,
        *,
        after_segment_id: str,
        segment_id: str | None = None,
    ) -> PromptTurnEditor:
        """Insert *part* immediately after the segment with *after_segment_id*.

        Raises :class:`ValueError` when *after_segment_id* is not found.
        Empty-body parts are inserted unconditionally (they contribute no wire bytes
        but may carry envelope identity).
        """
        sid = segment_id or f"inject:{part.id}:{after_segment_id}"
        for i, (_, esid) in enumerate(self._entries):
            if esid == after_segment_id:
                self._entries.insert(i + 1, (part, sid))
                return self
        raise ValueError(
            f"PromptTurnEditor.inject_mid: segment_id {after_segment_id!r} not found. "
            f"Available: {[sid for _, sid in self._entries]}"
        )

    def build(self) -> PromptTurn:
        """Build the final :class:`PromptTurn` with separator glue.

        Empty-body parts produce ``text=""`` segments (zero wire bytes).
        The first non-empty part gets no leading separator.
        Each subsequent non-empty part gets a ``"\\n\\n"`` prefix.
        """
        segments: list[PromptSegment] = []
        non_empty_count = 0
        for part, sid in self._entries:
            if not part.body:
                segments.append(PromptSegment(text="", part=part, segment_id=sid))
            else:
                prefix = "" if non_empty_count == 0 else "\n\n"
                segments.append(PromptSegment(
                    text=prefix + part.body,
                    part=part,
                    segment_id=sid,
                ))
                non_empty_count += 1
        return PromptTurn(segments=tuple(segments))


def hypothesis_suffix_part(body: str) -> PromptPart:
    """Wrap a validated/rejected hypothesis context as a :class:`PromptPart`.

    Per-round volatile — always re-sent (TURN/NONE).  Shared factory for both
    the mono plan handler (``pipeline.phases.builtin``) and the cross-plan loop
    (``pipeline.cross_project.planning_loop``).

    ``body`` may be the raw output of :func:`format_validated_hypothesis_context`
    or :func:`format_rejected_hypothesis_feedback`, which bake a leading ``"\\n\\n"``
    separator into the string.  That separator is stripped here because
    :class:`PromptTurnEditor` adds the canonical ``"\\n\\n"`` separator
    automatically when building the turn — keeping the leading whitespace in
    the body would double the separator on the wire.
    """
    return PromptPart(
        kind="hypothesis_suffix",
        name="hypothesis_context",
        source="artifact",
        body=body.lstrip("\n"),
        layer=PromptLayer.TURN,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="per-round hypothesis context (validated or feedback)",
        id="hypothesis_suffix:hypothesis_context",
    )


__all__ = [
    "PromptDelta",
    "PromptSegment",
    "PromptTraceView",
    "PromptTurn",
    "PromptTurnEditor",
    "hypothesis_suffix_part",
]
