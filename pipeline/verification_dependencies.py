# SPDX-License-Identifier: Apache-2.0
"""verification_dependencies.py — low-level cross-repo dependency provenance.

Captures per-declared-dependency git provenance for command receipts and
classifies dependency-HEAD staleness for the Stage 5 readiness layer and the
Stage 6 delivery gate.

ARCHITECTURAL CONSTRAINT (F1): this is the *low-level* verification module. It
imports only stdlib, :mod:`core.io.git_helpers`, and the typed
:class:`pipeline.verification_contract.PlaceholderContext` (which itself imports
neither :mod:`pipeline.verification_command` nor
:mod:`pipeline.verification_readiness`). It must NEVER import
``verification_command`` or ``verification_readiness`` — neither top-level nor
lazily — so that ``verification_command`` can import it top-level without an
import cycle.

Single source of the stale-classification fingerprint: ``changed_files_fingerprint``
lives here. Both the Stage 3 receipt *writer*
(:func:`pipeline.verification_command.run_command`) and the Stage 5 readiness
*reader* (:mod:`pipeline.verification_readiness`) import it from here, so writer
and reader hash identically — a valid receipt is never falsely classified as
stale due to hash drift.

This module never raises outward: every git / IO failure degrades (a field is
``None``, staleness is simply not asserted), mirroring the Stage 2–6 discipline.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping

from core.io.git_helpers import git_changed_files, git_head
from pipeline.verification_contract import PlaceholderContext

__all__ = [
    "capture_dependency_provenance",
    "changed_files_fingerprint",
    "current_dependency_heads",
    "dependency_stale_reason",
]

# Truncated length of the changed-files fingerprint hex digest.
_FINGERPRINT_LEN = 16


def changed_files_fingerprint(subject_checkout: str) -> str:
    """Truncated sha256 of the sorted changed-file list at ``subject_checkout``.

    Identical change sets fingerprint identically; the digest is purely a
    change-set identity, not a content hash.

    Public single source of the stale-classification fingerprint: the Stage 3
    receipt writer (:func:`pipeline.verification_command.run_command`) and the
    Stage 5 readiness reader (:mod:`pipeline.verification_readiness`) both import
    and call this one helper, or valid receipts would be falsely classified as
    stale.
    """
    files = sorted(git_changed_files(subject_checkout))
    digest = hashlib.sha256("\n".join(files).encode("utf-8")).hexdigest()
    return digest[:_FINGERPRINT_LEN]


def capture_dependency_provenance(
    ctx: PlaceholderContext,
    *,
    argv: list[str],
    eff_cwd: str,
    python: str,
    env_overrides: dict[str, str],
) -> list[dict]:
    """Capture read-only git provenance for every declared dependency.

    For each ``ctx.dependencies`` entry (name -> resolved absolute path) returns
    a dict ``{name, path, head, dirty, changed_files_count,
    changed_files_fingerprint, depends_on}``, in deterministic order by name.

    ``head`` is :func:`core.io.git_helpers.git_head` of the path (``None`` when
    the path is not a git repo / git fails). When ``head is None`` all three
    dirty fields are ``None`` (the dependency could not be inspected safely);
    otherwise ``dirty`` / ``changed_files_count`` / ``changed_files_fingerprint``
    summarise the working tree WITHOUT recording the list of changed files (a
    dependency's file names never enter this repo's receipts).

    ``depends_on`` is ``True`` exactly when the dependency's resolved path is a
    path-prefix (os.sep boundary, not a bare substring) of at least one of: an
    ``argv`` token, ``eff_cwd``, the ``python`` interpreter, or an
    ``env_overrides`` value. Never raises.
    """
    records: list[dict] = []
    for name in sorted(ctx.dependencies):
        path = ctx.dependencies[name]
        head = git_head(path) if path else None
        if head is None:
            dirty: bool | None = None
            changed_count: int | None = None
            fingerprint: str | None = None
        else:
            changed = git_changed_files(path)
            dirty = bool(changed)
            changed_count = len(changed)
            fingerprint = changed_files_fingerprint(path)
        records.append(
            {
                "name": name,
                "path": path,
                "head": head,
                "dirty": dirty,
                "changed_files_count": changed_count,
                "changed_files_fingerprint": fingerprint,
                "depends_on": _depends_on(
                    path,
                    argv=argv,
                    eff_cwd=eff_cwd,
                    python=python,
                    env_overrides=env_overrides,
                ),
            },
        )
    return records


def current_dependency_heads(
    ctx: PlaceholderContext | None,
) -> dict[str, str | None]:
    """Current ``HEAD`` per declared dependency (one ``git`` call each).

    ``ctx is None`` or no declared dependencies → ``{}``. Otherwise maps each
    dependency name to :func:`core.io.git_helpers.git_head` of its resolved path
    (``None`` when unavailable). Never raises.
    """
    if ctx is None:
        return {}
    deps = getattr(ctx, "dependencies", None) or {}
    return {name: git_head(path) for name, path in deps.items()}


def dependency_stale_reason(
    receipt: Mapping,
    current_heads: Mapping[str, str | None],
) -> str | None:
    """Reason a receipt is stale because a depended-on dependency's HEAD moved.

    Reads ``receipt['dependencies']`` tolerantly (absence / non-list / junk →
    ``None``). Returns ``"dependency <name> HEAD moved <old> -> <new>"`` for the
    first dependency entry that (a) has a truthy ``depends_on``, (b) records a
    non-``None`` ``head``, and (c) whose current HEAD in ``current_heads`` is
    non-``None`` and differs from the recorded one. Otherwise ``None``. The
    dirty fields never participate in this decision. Never raises.
    """
    deps = receipt.get("dependencies") if isinstance(receipt, Mapping) else None
    if not isinstance(deps, list):
        return None
    for entry in deps:
        if not isinstance(entry, Mapping):
            continue
        if not entry.get("depends_on"):
            continue
        name = entry.get("name")
        old = entry.get("head")
        if not name or old is None:
            continue
        new = current_heads.get(name)
        if new is None or new == old:
            continue
        return f"dependency {name} HEAD moved {old} -> {new}"
    return None


def _depends_on(
    dep_path: str,
    *,
    argv: list[str],
    eff_cwd: str,
    python: str,
    env_overrides: Mapping[str, str],
) -> bool:
    """True when ``dep_path`` is a path-prefix of any resolved command input."""
    if not dep_path:
        return False
    candidates: list[str] = [str(tok) for tok in argv]
    if eff_cwd:
        candidates.append(eff_cwd)
    if python:
        candidates.append(python)
    candidates.extend(str(value) for value in env_overrides.values())
    return any(_is_path_prefix(dep_path, candidate) for candidate in candidates)


def _is_path_prefix(prefix: str, candidate: str) -> bool:
    """True when ``candidate`` equals ``prefix`` or is below it (os.sep boundary).

    Boundary-aware so ``/repo/dep`` is a prefix of ``/repo/dep/bin/tool`` and of
    ``/repo/dep`` itself, but NOT of ``/repo/department`` (bare substring).
    """
    if not prefix or not candidate:
        return False
    pref = prefix.rstrip(os.sep)
    if not pref:
        return False
    return candidate == pref or candidate.startswith(pref + os.sep)
