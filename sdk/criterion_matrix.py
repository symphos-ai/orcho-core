"""Public SDK projection of the criterion matrix (ADR 0188).

The matrix is returned as a plain JSON-shaped mapping rather than a dataclass
on purpose: the durable evidence object, this projection, and the MCP wire must
be **byte-identical** under canonical JSON, and several keys are meaningfully
*absent* rather than empty (``method.gate_refs`` outside ``gates``,
``method.instructions`` outside ``manual``). A dataclass projection would emit
those keys as ``null``/``[]`` and silently break the contract.

Absent vs empty:

* a bundle with no ``criterion_matrix`` key — a run predating this contract, or
  one with no accepted plan — yields ``None``;
* a new-format plan with no criteria yields the explicit empty matrix.

``null`` is never written and never returned.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.criterion_evidence import criterion_matrix_for_run
from pipeline.criterion_matrix import (
    CRITERION_STATE_ORDER,
    EXECUTABLE_STATE_PRECEDENCE,
)
from pipeline.evidence.collector import project_findings
from sdk.errors import EvidenceInvalid
from sdk.runs import _CWD_DEFAULT, find_run, load_meta

__all__ = [
    "CRITERION_MATRIX_KEY",
    "CRITERION_STATE_ORDER",
    "EXECUTABLE_STATE_PRECEDENCE",
    "canonical_criterion_json",
    "get_criterion_matrix",
]

#: The additive evidence key this slice projects.
CRITERION_MATRIX_KEY = "criterion_matrix"


def get_criterion_matrix(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> dict[str, Any] | None:
    """Return the run's criterion matrix, or ``None`` when the key is absent.

    This narrow reader composes only the durable facts that can affect the
    criterion contract. In particular, a corrupt unrelated evidence section
    cannot make a legacy run with no accepted plan unreadable. Once a plan is
    present, every criterion authority remains strict and malformed state
    raises :class:`sdk.errors.EvidenceInvalid`.
    """
    ref = find_run(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )
    try:
        matrix = criterion_matrix_for_run(
            ref.run_dir,
            findings=project_findings(load_meta(ref.run_dir)),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise EvidenceInvalid(
            f"Failed to compose criterion matrix for {ref.run_id}: {exc}"
        ) from exc
    if matrix is None:
        return None
    return matrix.to_dict()


def canonical_criterion_json(value: Any) -> str:
    """Canonical JSON text for byte-equivalence comparisons.

    Key order is **preserved**, never sorted: ``counts_by_state`` carries the
    canonical state order as data, and sorting would destroy it.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
