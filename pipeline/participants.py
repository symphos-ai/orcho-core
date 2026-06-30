# SPDX-License-Identifier: Apache-2.0
"""
pipeline/participants.py — typed participant substrate for a run's repos
(ADR 0112 §1, increment B).

A run edits and verifies one or more repos. Each is a *participant*: a repo with
exactly one editable/verification root (its isolated per-run worktree under
isolation, or the canonical checkout in degraded single-checkout mode), a base
ref, and a delivery target. This module is the **mono-importable home** for that
typed value (:class:`Participant`) and the run-scoped container
(:class:`ParticipantSet`) the fail-closed source resolver (ADR 0112 §3) reads by
repo identity.

Substrate home & mono-importability invariant
----------------------------------------------
This module is **pure domain**: it imports only stdlib (``os`` / ``dataclasses``
/ ``typing``) and :mod:`pipeline.engine.worktree_source`. It MUST NOT import
anything from the cross-project package. The mono (single-project) path can seed
and read a :class:`ParticipantSet` without pulling the cross-project graph into
its import closure — that one-way independence is what makes the substrate shared
by both the mono and cross seams (ADR 0112 §1). The seeding into mono and cross,
and the cross post-dispatch ``editable_checkout`` bind, are deliberately NOT done
here (they live in the focused setup modules — out of this scope).

In-memory / durable split
-------------------------
A :class:`ParticipantSet` is **in-memory, run-scoped** (it lives on
``state.extras`` for the run, not in ``meta`` or ``session``). It is NEVER
serialized: the durable form stays ``session['worktree']`` / ``meta``. On resume
or any cold path the resolver re-seeds the set from that durable form (e.g. via
:func:`pipeline.engine.worktree_source.isolated_source_from_meta`); the set
itself adds no new persisted shape, so durable reproducibility is unchanged.

The bridge to the A-resolver is :meth:`ParticipantSet.isolated_source_for`, which
maps a participant to a :class:`pipeline.engine.worktree_source.IsolatedSource`
(``editable_checkout`` → ``worktree_path``, ``delivery_target`` →
``source_repo_path``, ``isolation`` from the set's run isolation regime). It is
the single production derivation the fail-closed resolver reads — byte-identical
to the pre-migration meta/path-gap derivation for both single-checkout (``None``)
and isolated runs.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, replace

from pipeline.engine.worktree_source import (
    IsolatedSource,
    isolated_source_from_meta,
)

# Isolation regimes that mean "no isolated worktree for this run" — the
# participant's editable_checkout collapses onto its delivery_target and the
# resolver derives no IsolatedSource (single-checkout / degraded local dev).
_OFF_ISOLATION: frozenset[str] = frozenset({"", "off"})

# Alias used for the single participant of a mono (single-project) run.
PRIMARY_ALIAS = "primary"


def _real(path: str) -> str:
    """Realpath-normalise ``path`` for identity comparison (``""`` for empty)."""
    if not path:
        return ""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


@dataclass(frozen=True, slots=True)
class Participant:
    """One repo taking part in a run, as a frozen value object.

    * ``alias`` — the run-local handle for this repo (``"primary"`` for a mono
      run; the cross alias otherwise).
    * ``repo`` — the participant's identity path (its canonical repo root). The
      :class:`ParticipantSet` keys on this via realpath normalisation.
    * ``editable_checkout`` — the SINGLE root of edits and verification for this
      participant: the isolated per-run worktree under isolation, or the
      ``delivery_target`` itself in degraded single-checkout mode. Empty string
      marks a *provisional* participant whose isolated checkout is not yet bound
      (cross seeding before child dispatch).
    * ``base_ref`` — the git ref the run's diff is measured from.
    * ``delivery_target`` — the canonical checkout the diff is delivered to (the
      source the worktree was forked from / where the commit lands).
    * ``isolation`` — per-participant isolation regime override, or ``None`` to
      inherit the set's run-level regime. The cross seam binds it from each
      child's own ``session['worktree']['isolation']`` post-dispatch, so a
      degraded isolation-off child stays off even inside a per_run cross set
      (no spurious fail-closed for a child that legitimately ran in-place).

    Frozen and slotted: rebinding ``editable_checkout`` replaces the entry rather
    than mutating it (see :meth:`ParticipantSet.bind_editable_checkout`).
    """

    alias: str
    repo: str
    editable_checkout: str
    base_ref: str
    delivery_target: str
    isolation: str | None = None

    @property
    def is_bound(self) -> bool:
        """True when an ``editable_checkout`` has been bound (not provisional)."""
        return bool(self.editable_checkout)


class ParticipantSet:
    """Run-scoped, in-memory, ordered set of :class:`Participant`, keyed by repo
    identity (realpath-normalised).

    Mutable container of frozen members. Construct a mono seed with
    :meth:`for_mono`, add provisional cross members with :meth:`add_provisional`,
    bind their real isolated checkout post-dispatch with
    :meth:`bind_editable_checkout`, and bridge to the A-resolver with
    :meth:`isolated_source_for`.

    The ``isolation`` regime is a run-level property (the worktree isolation mode
    of the run/profile). It governs how :meth:`isolated_source_for` reads each
    participant: an off regime yields no isolated source; a declared regime yields
    one bound to the participant's ``editable_checkout`` (and the resolver fails
    closed when that checkout is unbound or collapses onto the sibling).

    NOT serialized — see the module docstring's in-memory / durable split.
    """

    def __init__(
        self,
        *,
        isolation: str = "off",
        participants: list[Participant] | None = None,
    ) -> None:
        self._isolation = str(isolation or "")
        self._by_repo: OrderedDict[str, Participant] = OrderedDict()
        for participant in participants or []:
            self._by_repo[_real(participant.repo)] = participant

    # -- construction ----------------------------------------------------------

    @classmethod
    def for_mono(
        cls,
        *,
        checkout: str,
        project: str,
        base_ref: str = "",
        delivery_target: str | None = None,
        worktree: object | None = None,
        alias: str = PRIMARY_ALIAS,
    ) -> ParticipantSet:
        """Seed a single-participant set from a mono run's resolved paths.

        ``checkout`` is the run's resolved edit/verify root, ``project`` the
        canonical source. ``delivery_target`` defaults to ``project``. When a
        ``session['worktree']``-shaped ``worktree`` block is threaded it takes
        precedence (matching :func:`...verification_contract.placeholder_context_for`):
        its recorded isolation mode and paths seed the participant. Otherwise the
        isolation regime is *derived* from the path gap — a per-run worktree
        always lands on a ``checkout`` distinct from its ``project`` source, while
        a single-checkout run (``checkout`` == ``project``) derives an off regime
        so its resolution stays byte-identical.
        """
        target = delivery_target if delivery_target is not None else project
        from_meta = isolated_source_from_meta(
            worktree if isinstance(worktree, dict) else None,
        )
        if from_meta is not None:
            isolation = from_meta.isolation
            editable = from_meta.worktree_path or checkout
            source = from_meta.source_repo_path or target
        else:
            isolation = "off" if _real(checkout) == _real(project) else "per_run"
            editable = checkout
            source = target
        participant = Participant(
            alias=alias,
            repo=project,
            editable_checkout=editable,
            base_ref=base_ref,
            delivery_target=source,
        )
        return cls(isolation=isolation, participants=[participant])

    # -- mutation --------------------------------------------------------------

    def add_provisional(
        self,
        *,
        alias: str,
        repo: str,
        base_ref: str = "",
        delivery_target: str = "",
    ) -> Participant:
        """Add a PROVISIONAL participant whose isolated checkout is not yet bound.

        Used by the cross seam in run setup: ``alias`` / ``repo`` / ``base_ref`` /
        ``delivery_target`` are known, but the real ``editable_checkout`` is only
        the child's isolated worktree path — bound post-dispatch via
        :meth:`bind_editable_checkout`. Until then ``editable_checkout`` is the
        empty string, so the resolver fails closed rather than silently using the
        canonical sibling.
        """
        participant = Participant(
            alias=alias,
            repo=repo,
            editable_checkout="",
            base_ref=base_ref,
            delivery_target=delivery_target,
        )
        self._by_repo[_real(repo)] = participant
        return participant

    def add_participant(self, participant: Participant) -> Participant:
        """Register a fully-formed :class:`Participant`, idempotent by repo identity.

        Discovery-time promotion (ADR 0112 §4, increment C) registers an
        ALREADY-resolved participant whose isolated ``editable_checkout`` is bound
        at construction — unlike the two-step provisional cross path
        (:meth:`add_provisional` + :meth:`bind_editable_checkout`). Idempotent on
        the realpath-normalised ``repo`` identity (the same key :meth:`get` and the
        fail-closed resolver read by): when a participant for ``repo`` is already
        present it is returned unchanged — no second entry, no ``editable_checkout``
        overwrite, no mutation — so a repeated promotion of the same repo is an
        early no-op. Stays pure-domain: no git / FS I/O happens here (the worktree
        is resolved by the promotion module before the participant is built).
        """
        repo_key = _real(participant.repo)
        existing = self._by_repo.get(repo_key)
        if existing is not None:
            return existing
        self._by_repo[repo_key] = participant
        return participant

    def bind_editable_checkout(
        self, key: str, path: str, *, isolation: str | None = None,
    ) -> Participant:
        """Bind ``path`` as the real isolated ``editable_checkout`` of ``key``.

        ``key`` is an alias or any path identifying the participant (repo,
        editable_checkout, or delivery_target). Replaces the frozen entry with a
        new one carrying the bound checkout (cross post-dispatch bind to the
        child's actual isolated worktree). Raises :class:`KeyError` for an unknown
        participant.

        ``isolation`` records the child's own isolation regime on the participant
        (overriding the set's run-level regime). The cross seam threads
        ``session['worktree']['isolation']`` here so a degraded isolation-off
        child collapses ``editable_checkout`` onto its ``delivery_target`` and
        :meth:`isolated_source_for` returns ``None`` — preserving the in-place
        degraded contract instead of fail-closing on ``worktree == source``.
        """
        repo_key = self._resolve_key(key)
        if repo_key is None:
            raise KeyError(f"no participant matches {key!r}")
        bound = replace(self._by_repo[repo_key], editable_checkout=path)
        if isolation is not None:
            bound = replace(bound, isolation=isolation)
        self._by_repo[repo_key] = bound
        return bound

    # -- lookup ----------------------------------------------------------------

    def _resolve_key(self, key: str) -> str | None:
        """Resolve ``key`` (alias or any participant path) to its repo key."""
        for repo_key, participant in self._by_repo.items():
            if participant.alias == key:
                return repo_key
        target = _real(key)
        if not target:
            return None
        for repo_key, participant in self._by_repo.items():
            if target in (
                repo_key,
                _real(participant.editable_checkout),
                _real(participant.delivery_target),
            ):
                return repo_key
        return None

    def get(self, key: str) -> Participant | None:
        """Return the participant matching ``key`` (alias or path), else ``None``."""
        repo_key = self._resolve_key(key)
        return self._by_repo[repo_key] if repo_key is not None else None

    def __iter__(self) -> Iterator[Participant]:
        return iter(self._by_repo.values())

    def __len__(self) -> int:
        return len(self._by_repo)

    @property
    def isolation(self) -> str:
        """The run-level worktree isolation regime of this set."""
        return self._isolation

    # -- A-resolver bridge -----------------------------------------------------

    def isolated_source_for(
        self,
        key: str | Participant,
    ) -> IsolatedSource | None:
        """Return the :class:`IsolatedSource` for a participant, or ``None``.

        The bridge to the fail-closed A-resolver: ``editable_checkout`` →
        ``worktree_path``, ``delivery_target`` → ``source_repo_path``, isolation
        from the participant's own regime when bound, else the set's run regime.
        Returns ``None`` for an off (not declared) regime — single-checkout /
        degraded — keeping that resolution byte-identical to the pre-migration
        meta/path-gap derivation.

        Under a declared regime the source is returned even when the
        ``editable_checkout`` is unbound or collapses onto the sibling; that is
        the fail-closed case the resolver raises on, NOT a silent sibling
        fallback.
        """
        participant = key if isinstance(key, Participant) else self.get(key)
        if participant is None:
            return None
        regime = (
            participant.isolation
            if participant.isolation is not None
            else self._isolation
        )
        if regime in _OFF_ISOLATION:
            return None
        return IsolatedSource(
            isolation=regime,
            worktree_path=participant.editable_checkout,
            source_repo_path=participant.delivery_target,
        )


__all__ = [
    "PRIMARY_ALIAS",
    "Participant",
    "ParticipantSet",
]
