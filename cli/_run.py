"""Shared adapter that maps SDK calls onto CLI exit codes.

Read-only/report handlers (`cmd_history`, `cmd_status`, `cmd_metrics`,
`cmd_cost`, `cmd_prompts_list`, `cmd_pricing_show`) route through
`_run_cli`. Side-effect handlers (`cmd_evidence` with `--out`,
`cmd_pricing_refresh`) own their branching but reuse `format_error`
for stderr.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from cli._formatters import format_error
from sdk import OrchoError


def _run_cli(
    call: Callable[[], Any],
    formatter: Callable[[Any], str],
) -> int:
    """Call `call`, format the result, print, return 0/exit-code."""
    try:
        result = call()
    except OrchoError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code
    print(formatter(result))
    return 0
