# SPDX-License-Identifier: Apache-2.0
"""Run-scoped inputs for scheduled-verification selection.

The lifecycle hooks and pre-final boundary must resolve path rules against the
same checkout: the run worktree, never an ambient source checkout.  This small
module is the sole owner of that best-effort git read.
"""

from __future__ import annotations

from typing import Any

from pipeline.verification_selection import SelectionContext, selection_context_from_extras


def selection_context_for_run(run: Any, contract: Any) -> SelectionContext:
    """Return current selection inputs for ``run``'s effective checkout.

    Changed paths are intentionally best-effort.  Task kind and operator sets
    remain the durable declarations in ``state.extras`` and are normalized by
    ``selection_context_from_extras``.
    """
    state = getattr(run, "state", None)
    extras = getattr(state, "extras", {}) or {}
    placeholders = extras.get("verification_placeholders")
    checkout = getattr(placeholders, "checkout", "") or ""
    if not checkout:
        effective_diff_cwd = getattr(run, "_effective_diff_cwd", None)
        if callable(effective_diff_cwd):
            try:
                checkout = str(effective_diff_cwd())
            except Exception:  # noqa: BLE001 - selection remains best-effort
                checkout = ""

    touched_paths: tuple[str, ...] = ()
    if checkout:
        try:
            from core.io.git_helpers import git_changed_files

            touched_paths = tuple(git_changed_files(checkout))
        except Exception:  # noqa: BLE001 - selection remains best-effort
            pass
    return selection_context_from_extras(extras, contract, touched_paths=touched_paths)


__all__ = ["selection_context_for_run"]
