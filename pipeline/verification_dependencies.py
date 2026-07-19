# SPDX-License-Identifier: Apache-2.0
"""Typed verification-subject capture for declared command dependencies."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.io.git_helpers import git_changed_files
from pipeline.verification_contract import PlaceholderContext
from pipeline.verification_subject import (
    VerificationSubjectAvailable,
    VerificationSubjectCapture,
    capture_verification_subject,
)

__all__ = [
    "capture_dependency_provenance",
    "current_dependency_subjects",
    "changed_files_fingerprint",
]


def changed_files_fingerprint(subject_checkout: str) -> str:
    """Legacy diagnostic helper; never used as schema-v3 freshness proof."""
    return hashlib.sha256("\n".join(sorted(git_changed_files(subject_checkout))).encode()).hexdigest()[:16]


def capture_dependency_provenance(
    ctx: PlaceholderContext,
    *,
    argv: list[str],
    eff_cwd: str,
    python: str,
    env_overrides: dict[str, str],
) -> list[dict[str, Any]]:
    """Capture each dependency's subject, in deterministic name order.

    ``head`` and ``dirty`` remain diagnostics only.  The typed ``subject`` is
    the sole proof used for freshness, and unavailable captures are retained so
    effective dependencies fail closed instead of disappearing.
    """
    records: list[dict[str, Any]] = []
    for name in sorted(ctx.dependencies):
        path = ctx.dependencies[name]
        capture = capture_verification_subject(Path(path)) if path else None
        identity = capture.identity if isinstance(capture, VerificationSubjectAvailable) else None
        records.append(
            {
                "name": name,
                "path": path,
                "depends_on": _depends_on(path, argv=argv, eff_cwd=eff_cwd, python=python, env_overrides=env_overrides),
                "subject": capture,
                "head": identity.observed_head_oid if identity else None,
                "dirty": bool(git_changed_files(path)) if identity else None,
            },
        )
    return records


def current_dependency_subjects(ctx: PlaceholderContext | None) -> dict[str, VerificationSubjectCapture | None]:
    """Capture current identities for every declared dependency, never raising."""
    if ctx is None:
        return {}
    return {
        name: capture_verification_subject(Path(path)) if path else None
        for name, path in (getattr(ctx, "dependencies", None) or {}).items()
    }


def _depends_on(
    dep_path: str,
    *,
    argv: list[str],
    eff_cwd: str,
    python: str,
    env_overrides: Mapping[str, str],
) -> bool:
    if not dep_path:
        return False
    candidates = [*map(str, argv), eff_cwd, python, *map(str, env_overrides.values())]
    return any(_is_path_prefix(dep_path, candidate) for candidate in candidates)


def _is_path_prefix(prefix: str, candidate: str) -> bool:
    pref = prefix.rstrip(os.sep)
    return bool(pref and candidate and (candidate == pref or candidate.startswith(pref + os.sep)))
