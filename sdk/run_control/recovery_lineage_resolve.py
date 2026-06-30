# SPDX-License-Identifier: Apache-2.0
"""Inspected-meta failure resolution + the ``unknown`` / ``stop_unknown`` dead-end.

A focused leaf split of :mod:`sdk.run_control.recovery_lineage` (it imports only
stdlib + the data type, never the ladder). It owns the *single source* both the
public ``recovery_lineage`` and the ``run_diagnosis`` attachment use to read the
inspected run's ``meta.json`` strictly and to project the one defensive dead-end
that read failure degrades to — so the standalone and attached recovery
read-models can never diverge on a corrupt / missing / non-object meta.

Unlike the tolerant :func:`sdk.runs.load_meta` (which returns ``{}`` so
status/history rows still render), the recovery read-model must *know* when the
inspected meta is unreadable so it can degrade to ``unknown`` / ``stop_unknown``
rather than misclassifying a corrupt run as a bare non-terminal ``none``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sdk.run_control.types import RecoveryLineage

# The closed dead-end vocabulary that an unreadable / unresolved lineage emits.
# Kept here with the strict loader and the projection so the failure-resolution
# concern lives in one place; the recovery_lineage ladder imports these.
SUBJECT_UNKNOWN = "unknown"
ACTION_STOP_UNKNOWN = "stop_unknown"

# missing_facts labels — the exact durable facts a stop_unknown dead-end lacks.
_MISSING_SOURCE = "no source/parent run id"
_MISSING_PLAN = "no plan artifact"
_MISSING_GATE = "no delivery gate"
_MISSING_CHILD = "no active child"

# Every durable fact is absent when the inspected meta itself cannot be read.
_ALL_MISSING_FACTS = (_MISSING_SOURCE, _MISSING_PLAN, _MISSING_GATE, _MISSING_CHILD)


def _strict_load_meta(run_dir: Path) -> dict[str, Any]:
    """Strictly read the inspected run's ``meta.json``, raising on any failure.

    Raises ``OSError`` / :class:`json.JSONDecodeError` (a missing / corrupt file)
    or :class:`ValueError` (a readable but non-object payload). The caller's
    defensive ``except`` maps that to :func:`_unreadable_meta_lineage`.
    """
    parsed = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("meta.json is not a JSON object")
    return parsed


def _unreadable_meta_lineage(run_id: str, exc_name: str) -> RecoveryLineage:
    """The single ``unknown`` / ``stop_unknown`` dead-end for an unreadable meta.

    Both :func:`sdk.run_control.recovery_lineage.recovery_lineage` and the
    ``run_diagnosis`` attachment build the inspected-meta read failure through
    this one helper, so the standalone and attached recovery read-models are
    byte-identical for the same run id: all four ``missing_facts`` and a
    fact-built ``reason`` naming the failure type.
    """
    return RecoveryLineage(
        run_id=run_id,
        is_terminal_or_rejected=False,
        continuation_subject=SUBJECT_UNKNOWN,
        recommended_next_action=ACTION_STOP_UNKNOWN,
        missing_facts=_ALL_MISSING_FACTS,
        reason=f"could not read run meta: {exc_name}",
    )
