# SPDX-License-Identifier: Apache-2.0
"""Thin CLI adapter for the run-scoped managed-command protocol."""

from __future__ import annotations

import argparse
import json
import sys

from agents.managed_command import (
    DuplicateManagedCommandError,
    ManagedCommandIdentity,
    ManagedCommandStore,
    run_managed_command,
)


def cmd_managed_command_run(args: argparse.Namespace) -> int:
    """Run a long command behind durable duplicate admission."""
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv.pop(0)
    if not argv:
        print("managed command requires argv after '--'", file=sys.stderr)
        return 2
    try:
        return run_managed_command(
            run_dir=args.run_dir,
            phase=args.phase,
            cwd=args.cwd,
            argv=argv,
        )
    except DuplicateManagedCommandError as exc:
        print(f"managed command refused: {exc}", file=sys.stderr)
        return 75
    except OSError as exc:
        print(f"managed command could not start: {exc}", file=sys.stderr)
        return 126


def cmd_managed_command_status(args: argparse.Namespace) -> int:
    """Print the durable state for one normalized command identity."""
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv.pop(0)
    if not argv:
        print("managed command requires argv after '--'", file=sys.stderr)
        return 2
    identity = ManagedCommandIdentity.build(
        run_dir=args.run_dir,
        phase=args.phase,
        cwd=args.cwd,
        argv=argv,
    )
    observed = ManagedCommandStore(args.run_dir).observe(identity)
    print(json.dumps({
        "identity": identity.key,
        "state": observed.state,
        "attempt_id": observed.attempt_id,
        "exit_code": observed.exit_code,
    }, sort_keys=True))
    return 0


def add_managed_command_parser(
    subparsers: argparse._SubParsersAction,
) -> None:
    """Register ``orcho command`` without growing the main CLI facade."""
    command = subparsers.add_parser(
        "command",
        help="Run and inspect long agent commands with duplicate admission",
    )
    actions = command.add_subparsers(dest="command_action", required=True)
    for action, handler, help_text in (
        ("run", cmd_managed_command_run, "Run and durably settle one command"),
        ("status", cmd_managed_command_status, "Inspect one command identity"),
    ):
        parser = actions.add_parser(action, help=help_text)
        parser.add_argument("--run-dir", required=True)
        parser.add_argument("--phase", required=True)
        parser.add_argument("--cwd", required=True)
        parser.add_argument("argv", nargs=argparse.REMAINDER)
        parser.set_defaults(func=handler)
