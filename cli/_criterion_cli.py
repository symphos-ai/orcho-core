# SPDX-License-Identifier: Apache-2.0
"""``orcho criterion`` — read the criterion matrix, record a human decision.

The CLI is a read model plus one narrow write. It never computes a criterion
state: ``matrix`` prints what the reducer already decided, and ``decide``
delegates every validation (including supersession) to the durable writer.
"""
from __future__ import annotations

import argparse
import json
import sys

from sdk.criterion_decisions import (
    list_criterion_decisions,
    record_criterion_decision,
)
from sdk.criterion_matrix import get_criterion_matrix
from sdk.errors import CriterionDecisionRejected, OrchoError

__all__ = ["cmd_criterion_decide", "cmd_criterion_matrix", "register_criterion_cli"]


def register_criterion_cli(sub: argparse._SubParsersAction) -> None:
    """Register ``orcho criterion matrix`` and ``orcho criterion decide``."""
    parent = sub.add_parser(
        "criterion",
        help="Which acceptance criterion is proven, advisory, or open?",
        description=(
            "Read the run's criterion matrix and record typed per-criterion "
            "human decisions. States are produced by the engine's criterion "
            "reducer; this command never re-derives them."
        ),
    )
    child = parent.add_subparsers(dest="criterion_cmd", required=True)

    p_matrix = child.add_parser(
        "matrix", help="Print the criterion matrix as JSON",
    )
    p_matrix.add_argument("run_id", nargs="?", default=None)
    p_matrix.add_argument("--workspace", default=None)
    p_matrix.set_defaults(func=cmd_criterion_matrix)

    p_decide = child.add_parser(
        "decide", help="Record an operator decision for a human criterion",
    )
    p_decide.add_argument("run_id", nargs="?", default=None)
    p_decide.add_argument("--criterion", required=True, help="Criterion id, e.g. C3")
    p_decide.add_argument(
        "--decision", required=True, choices=("accept", "reject"),
    )
    p_decide.add_argument("--note", default=None)
    p_decide.add_argument("--actor", default=None)
    p_decide.add_argument(
        "--supersedes",
        default=None,
        help=(
            "decision_id of the current chain head; required when the "
            "criterion already has a decision"
        ),
    )
    p_decide.add_argument("--workspace", default=None)
    p_decide.set_defaults(func=cmd_criterion_decide)

    p_list = child.add_parser(
        "decisions", help="Print the run's append-only decision log",
    )
    p_list.add_argument("run_id", nargs="?", default=None)
    p_list.add_argument("--workspace", default=None)
    p_list.set_defaults(func=cmd_criterion_decisions)


def cmd_criterion_matrix(args: argparse.Namespace) -> int:
    try:
        matrix = get_criterion_matrix(
            getattr(args, "run_id", None),
            workspace=getattr(args, "workspace", None),
        )
    except OrchoError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    if matrix is None:
        print(
            "This run has no criterion matrix (no accepted plan artifact).",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(json.dumps(matrix, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_criterion_decide(args: argparse.Namespace) -> int:
    try:
        record = record_criterion_decision(
            getattr(args, "run_id", None),
            criterion_id=args.criterion,
            decision=args.decision,
            note=getattr(args, "note", None),
            actor=getattr(args, "actor", None),
            supersedes=getattr(args, "supersedes", None),
            workspace=getattr(args, "workspace", None),
        )
    except CriterionDecisionRejected as exc:
        print(f"decision rejected: {exc}", file=sys.stderr)
        return exc.exit_code
    except OrchoError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    sys.stdout.write(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return 0


def cmd_criterion_decisions(args: argparse.Namespace) -> int:
    try:
        records = list_criterion_decisions(
            getattr(args, "run_id", None),
            workspace=getattr(args, "workspace", None),
        )
    except OrchoError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    sys.stdout.write(json.dumps(records, indent=2, ensure_ascii=False) + "\n")
    return 0
