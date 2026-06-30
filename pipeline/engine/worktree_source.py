"""
pipeline/engine/worktree_source.py — fail-closed source resolution for a repo
that may own an isolated per-run worktree (GWT-1 / ADR 0033, ADR 0108, ADR 0112).

A repo run under per-run isolation edits and must *verify* against its worktree
checkout, never the canonical sibling. When the verification env / ``{dependency:X}``
source silently falls back to the sibling, a clean tree passes vacuously while the
run's undelivered diff lives only in the worktree — the false-green ADR 0112 §3
calls out. This helper centralizes the one decision so both the ``{dependency:X}``
resolution (:mod:`pipeline.verification_contract`) and the verify-env cwd selection
(:mod:`pipeline.verification_env`) agree on a single source path and fail closed
when an isolated repo would otherwise resolve to its sibling.

A run carries a *participant set* of repos (ADR 0112 §1,
:mod:`pipeline.participants`), each with its own isolated checkout, and the
resolver reads that set by repo identity: the production seam
(:func:`pipeline.verification_contract.placeholder_context_for`) builds a
one-participant mono set and derives the run's :class:`IsolatedSource` from it
(:meth:`pipeline.participants.ParticipantSet.isolated_source_for`) instead of
calling the meta/path derivations here as parallel paths. :class:`IsolatedSource`
remains the per-participant value this module resolves; a mono run carries exactly
one participant (its primary checkout). The set is IN-MEMORY and run-scoped — the
durable form stays ``meta.worktree``, from which the set is re-seeded on resume.

Pure domain — no subprocess, no git, only path normalisation via ``os.path``.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Isolation modes that mean "no isolated worktree for this repo" — the ambient /
# sibling fallback is then legal (single-checkout local development).
_OFF_ISOLATION: frozenset[str] = frozenset({"", "off"})


class IsolatedSourceError(RuntimeError):
    """Fail-closed: an isolated repo's verify/edit source could not be bound to
    its per-run worktree and would otherwise fall back to the canonical sibling.

    Carries a human-readable reason naming the repo, the expected worktree path,
    and the actual sibling path so the preflight (T3) and any surfaced error stay
    diagnosable rather than a bare traceback.
    """


@dataclass(frozen=True, slots=True)
class IsolatedSource:
    """One repo's isolated per-run worktree, as seen by source resolution.

    * ``isolation`` — the worktree mode (``"off"`` / ``"per_run"`` / ...); the
      empty string and ``"off"`` both mean "not isolated".
    * ``worktree_path`` — the per-run checkout: the ONLY valid verify/edit root
      for an isolated repo.
    * ``source_repo_path`` — the canonical sibling / delivery-target checkout the
      worktree was forked from. A source that resolves here under isolation is the
      fail-closed case, not a valid root.

    Mirrors the persisted ``meta.worktree`` / :class:`pipeline.engine.worktree.WorktreeContext`
    wire shape (``isolation`` / ``path`` / ``source_repo_path``). Construct one via
    :func:`isolated_source_from_meta` from a ``session['worktree']`` dict.
    """

    isolation: str
    worktree_path: str
    source_repo_path: str

    @property
    def is_declared(self) -> bool:
        """True when isolation is *declared* for this repo (mode is not off).

        Independent of whether the worktree path is usable: a repo can be declared
        isolated (``isolation != off``) yet carry an unbound — empty or
        degenerate — worktree path. That is the fail-closed case, NOT a silent
        no-isolation fallback: it must reach the resolver's hard-error branch
        rather than resolving to the canonical sibling (see
        :func:`resolve_isolated_repo_source` and :func:`identifies_isolated_repo`).
        """
        return self.isolation not in _OFF_ISOLATION

    @property
    def is_isolated(self) -> bool:
        """True when this repo has a usable per-run worktree distinct from source.

        Isolation must be declared (:attr:`is_declared`) AND a worktree path must
        be recorded. ``is_isolated`` answers "can this be redirected to a bound
        worktree?"; :attr:`is_declared` answers "is isolation in force at all?".
        A declared-but-unbound repo is ``is_declared`` yet not ``is_isolated`` —
        the resolver raises for it rather than falling back to the sibling.
        """
        return self.is_declared and bool(self.worktree_path)


def isolated_source_from_meta(
    worktree: Mapping[str, Any] | None,
) -> IsolatedSource | None:
    """Build an :class:`IsolatedSource` from a ``session['worktree']``-shaped dict.

    Returns ``None`` when ``worktree`` is missing or carries neither an isolation
    mode nor a path (nothing to reason about — single-checkout). A present-but-off
    block still yields an :class:`IsolatedSource` (``is_isolated == False``) so the
    resolver's no-isolation branch is exercised explicitly rather than via ``None``.
    """
    if not isinstance(worktree, Mapping):
        return None
    isolation = str(worktree.get("isolation") or "")
    path = str(worktree.get("path") or "")
    source = str(worktree.get("source_repo_path") or "")
    if not isolation and not path:
        return None
    return IsolatedSource(
        isolation=isolation,
        worktree_path=path,
        source_repo_path=source,
    )


def _same_path(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` name the same location (realpath-normalised)."""
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a == b


def identifies_isolated_repo(candidate: str, isolated: IsolatedSource | None) -> bool:
    """True when ``candidate`` (a canonical path) names the isolated repo itself.

    The bridge between a generic source path and the run's isolated worktree: a
    candidate that resolves to the worktree's ``source_repo_path`` IS that repo,
    so it must be redirected to the worktree (or hard-error when the worktree is
    unbound). A genuine external dependency (a different repo) does not match and
    keeps its sibling path. Always ``False`` when nothing is isolated.

    Gated on :attr:`IsolatedSource.is_declared`, NOT ``is_isolated``: a repo whose
    isolation is declared but whose worktree path is empty/degenerate still
    identifies itself here, so :func:`resolve_isolated_repo_source` reaches its
    fail-closed branch instead of silently returning the canonical sibling.
    """
    if isolated is None or not isolated.is_declared:
        return False
    return _same_path(candidate, isolated.source_repo_path)


def resolve_isolated_repo_source(
    *,
    repo_name: str,
    candidate: str,
    isolated: IsolatedSource | None,
) -> str:
    """Resolve the effective source path for ``repo_name``, fail-closed under isolation.

    Contract:

    1. **Isolated repo → worktree path.** When ``isolated`` is isolated and
       ``candidate`` identifies that repo (resolves to its ``source_repo_path``),
       return the worktree path — never the sibling.
    2. **Isolated but unbindable → hard error.** When the repo is isolated and the
       worktree path is unresolved, or itself degenerates to the canonical sibling
       (which would verify a clean tree and pass vacuously), raise
       :class:`IsolatedSourceError` with a reason naming the repo, the expected
       worktree path, and the actual sibling path.
    3. **No isolation → ambient/sibling fallback.** When nothing is isolated, or
       ``candidate`` is a genuine external dependency that does not identify the
       isolated repo, return ``candidate`` unchanged. This keeps single-checkout
       runs byte-identical.
    """
    # (3) Nothing isolated, or this candidate is an external dependency — the
    # ambient/sibling fallback stays legal and behaviour is unchanged.
    if not identifies_isolated_repo(candidate, isolated):
        return candidate

    # ``isolated`` is non-None and isolated here (identifies_isolated_repo gate).
    assert isolated is not None
    worktree_path = isolated.worktree_path
    sibling = isolated.source_repo_path

    # (2) The worktree checkout is the only valid verify/edit root. Fail closed if
    # it is unresolved or points back at the canonical sibling — refusing to
    # silently verify the clean source tree.
    if not worktree_path or _same_path(worktree_path, sibling):
        raise IsolatedSourceError(
            f"repo {repo_name!r} runs in an isolated per-run worktree but its "
            f"verify/edit source could not be bound to that worktree: expected "
            f"worktree path {worktree_path or '<unresolved>'}, actual canonical "
            f"sibling {sibling or '<unknown>'}. Refusing to fall back to the "
            f"sibling checkout (a clean tree would pass verification vacuously)."
        )

    # (1) Redirect the isolated repo to its worktree checkout.
    return worktree_path


__all__ = [
    "IsolatedSource",
    "IsolatedSourceError",
    "identifies_isolated_repo",
    "isolated_source_from_meta",
    "resolve_isolated_repo_source",
]
