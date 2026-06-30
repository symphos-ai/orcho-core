"""Render envelope: prefix vs turn-payload partitioning of a composed prompt.

ADR 0026 splits the rendered prompt string into two regions for
session-aware caching:

- **Stable prefix** — code-owned and editable parts that are safe to
  cache across runs/turns. Subsequent rounds on a resumed runtime
  session can omit these parts because the provider already has them.
- **Turn payload** — dynamic content (task text, artifact body,
  feedback, diff, round number, file paths) that must be re-sent on
  every round.

This module provides :class:`PromptRenderEnvelope` — a metadata-rich
view of one rendered prompt — together with the partitioning rule
and stable-hash helpers used by later milestones (M6 delta selector,
M7 wiring, M12 trace persistence).

The envelope is a sidecar: ``text`` is the byte-identical wire
prompt that was passed to the agent. The string-returning composer
APIs are unchanged; the envelope is published through
:mod:`core.observability.prompt_trace`.

The partition is purely metadata-driven: each :class:`PromptPart`
carries declared :class:`~pipeline.prompts.types.PromptStability` and
:class:`~pipeline.prompts.types.PromptCacheScope`, and the envelope
trusts those declarations. The composer is responsible for declaring
TURN metadata when a part renders run/turn substitutions; M2 does
not perform body-content scanning at the envelope layer (exhaustive
prefix-leak guards are deferred to M12).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from pipeline.prompts.types import (
    PromptCacheScope,
    PromptPart,
    PromptStability,
)


def is_prefix_eligible(part: PromptPart) -> bool:
    """Return ``True`` when *part* may live in the stable prefix.

    Per ADR 0026:

    - ``STATIC`` parts may live in the prefix unless their
      ``cache_scope`` is ``NONE`` (always-resend).
    - ``PROFILE`` parts may live in the prefix only when
      ``cache_scope`` is ``GLOBAL`` or ``WORKSPACE`` (i.e. the
      profile dependence does not require re-sending per session).
    - ``RUN`` and ``TURN`` parts are excluded.
    """
    if part.cache_scope is PromptCacheScope.NONE:
        return False
    if part.stability is PromptStability.STATIC:
        # STATIC + any non-NONE scope is prefix-eligible. ADR 0028
        # adds PROJECT and re-confirms STATIC + WORKSPACE/PROJECT as
        # scope-stable bytes whose invalidation is body/hash-driven,
        # not stability-driven.
        return True
    if part.stability is PromptStability.PROFILE:
        return part.cache_scope in {
            PromptCacheScope.GLOBAL,
            PromptCacheScope.WORKSPACE,
            PromptCacheScope.PROJECT,
        }
    return False


def _split_at_first_payload(
    parts: tuple[PromptPart, ...] | list[PromptPart],
) -> tuple[tuple[PromptPart, ...], tuple[PromptPart, ...]]:
    """Split *parts* in render order at the first payload-only part.

    Conservative M2 rule: the cacheable prefix must be a **contiguous
    leading run** of the rendered prompt. As soon as render order
    produces a non-prefix-eligible part, every subsequent part — even
    if its own metadata is prefix-eligible — is treated as payload.
    Otherwise omitting payload on a resumed session in M6/M7 would
    leave the agent with an interleaved prompt the prefix never
    actually represented on the wire.

    Reordering for tighter packing is out of scope for M2 (would
    change wire text); finer template / body splitting is M10.
    """
    parts = tuple(parts)
    cut = len(parts)
    for idx, p in enumerate(parts):
        if not is_prefix_eligible(p):
            cut = idx
            break
    return parts[:cut], parts[cut:]


def stable_prefix_parts(
    parts: tuple[PromptPart, ...] | list[PromptPart],
) -> tuple[PromptPart, ...]:
    """Return the contiguous leading prefix of *parts* in render order.

    See :func:`_split_at_first_payload` for the conservative M2 rule.
    """
    prefix, _ = _split_at_first_payload(parts)
    return prefix


def turn_payload_parts(
    parts: tuple[PromptPart, ...] | list[PromptPart],
) -> tuple[PromptPart, ...]:
    """Return the parts following the first payload part in render order.

    See :func:`_split_at_first_payload` for the conservative M2 rule.
    """
    _, payload = _split_at_first_payload(parts)
    return payload


def _part_digest(part: PromptPart) -> str:
    """SHA-256 over ``id|version|body`` of *part*.

    Includes ``version`` so a bumped part (same id, new version) yields
    a different digest. Excludes ``layer/stability/cache_scope`` so
    metadata-only changes do not invalidate the cache when the body
    text and identity are unchanged. Also excludes ``artifact_path``:
    it is trace/evidence metadata, not part of the wire content the
    model receives, so two parts with identical body but different
    artifact_path produce identical digests (load-bearing invariant
    tested by ``test_artifact_path_is_not_hash_bearing``).
    """
    raw = f"{part.id}|{part.version or 0}|{part.body}".encode()
    return hashlib.sha256(raw).hexdigest()


def _concat_hash(parts: tuple[PromptPart, ...]) -> str:
    """SHA-256 over the per-part digests in render order.

    Stable across re-renders that produce the same parts in the same
    order; changes when any part's id, version, or body changes, or
    when parts are added, removed, or reordered.
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(_part_digest(p).encode())
        h.update(b"\n")
    return h.hexdigest()


@dataclass(frozen=True)
class PromptRenderEnvelope:
    """Metadata view of one rendered prompt.

    ``text`` is the wire-identical string that was passed to the
    agent runtime — the rendered prompt callers receive today is
    unchanged. ``parts`` is the full ordered list of selected
    :class:`PromptPart` instances (upper composable parts followed
    by code-owned system-tail blocks).

    ``stable_prefix_parts`` and ``turn_payload_parts`` partition
    ``parts`` based on declared metadata. ``prefix_hash`` and
    ``payload_hash`` are stable SHA-256 digests over each partition
    in order; the M6 delta selector and the M12 trace use them to
    detect part changes without re-comparing bodies.

    Construction enforces three invariants:

    1. The two partitions are disjoint and exhaust ``parts``.
    2. Every part placed in ``stable_prefix_parts`` is
       prefix-eligible per :func:`is_prefix_eligible`.
    3. ``stable_prefix_parts`` is a **contiguous leading prefix** of
       ``parts`` in render order, and ``turn_payload_parts`` is the
       contiguous remainder. A prefix-eligible part that the
       composer rendered after a payload part is moved into the
       payload partition (see :func:`_split_at_first_payload`).
    """

    text: str
    parts: tuple[PromptPart, ...] = field(default_factory=tuple)
    stable_prefix_parts: tuple[PromptPart, ...] = field(default_factory=tuple)
    turn_payload_parts: tuple[PromptPart, ...] = field(default_factory=tuple)
    prefix_hash: str = ""
    payload_hash: str = ""

    def __post_init__(self) -> None:
        # Partitions disjoint and exhaust ``parts``: identity-based
        # checks since two distinct PromptPart instances may compare
        # equal but represent different positional slots.
        prefix_ids = [id(p) for p in self.stable_prefix_parts]
        payload_ids = [id(p) for p in self.turn_payload_parts]
        all_ids = [id(p) for p in self.parts]
        if set(prefix_ids) & set(payload_ids):
            raise ValueError(
                "PromptRenderEnvelope: stable_prefix_parts and "
                "turn_payload_parts overlap by identity.",
            )
        if set(prefix_ids) | set(payload_ids) != set(all_ids):
            raise ValueError(
                "PromptRenderEnvelope: prefix + payload partitions do "
                "not exhaust parts.",
            )

        # Every prefix part must be prefix-eligible.
        for p in self.stable_prefix_parts:
            if not is_prefix_eligible(p):
                raise ValueError(
                    f"PromptRenderEnvelope: part {p.id!r} is not "
                    f"prefix-eligible (stability={p.stability.value}, "
                    f"cache_scope={p.cache_scope.value}).",
                )

        # ``stable_prefix_parts`` must be the contiguous leading
        # slice of ``parts``. This is the wire-layout invariant that
        # M6/M7 rely on: omitting payload on a resumed session must
        # leave a prompt the agent already saw, not a re-stitched
        # prefix the prompt never actually contained.
        prefix_len = len(self.stable_prefix_parts)
        leading = tuple(self.parts[:prefix_len])
        trailing = tuple(self.parts[prefix_len:])
        if leading != self.stable_prefix_parts:
            raise ValueError(
                "PromptRenderEnvelope: stable_prefix_parts is not the "
                "contiguous leading slice of parts.",
            )
        if trailing != self.turn_payload_parts:
            raise ValueError(
                "PromptRenderEnvelope: turn_payload_parts is not the "
                "contiguous trailing slice of parts.",
            )


def make_render_envelope(
    *,
    text: str,
    parts: tuple[PromptPart, ...] | list[PromptPart],
) -> PromptRenderEnvelope:
    """Build a :class:`PromptRenderEnvelope` from a rendered prompt.

    ``text`` is the wire-identical string the agent receives.
    ``parts`` is the ordered list of selected parts that produced
    that text. Partitioning is metadata-driven — see
    :func:`is_prefix_eligible` — and stable hashes are computed over
    each partition.
    """
    parts_tuple = tuple(parts)
    prefix = stable_prefix_parts(parts_tuple)
    payload = turn_payload_parts(parts_tuple)
    return PromptRenderEnvelope(
        text=text,
        parts=parts_tuple,
        stable_prefix_parts=prefix,
        turn_payload_parts=payload,
        prefix_hash=_concat_hash(prefix),
        payload_hash=_concat_hash(payload),
    )


__all__ = [
    "PromptRenderEnvelope",
    "is_prefix_eligible",
    "make_render_envelope",
    "stable_prefix_parts",
    "turn_payload_parts",
]
