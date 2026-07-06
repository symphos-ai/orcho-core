"""Help text for the ``orcho`` CLI.

The command parser stays in ``cli.orcho``; this module owns the
onboarding copy and epilogs so the CLI wiring remains readable.
"""
from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Iterator

from core.io.ansi import C, paint

TAGLINE = "Orcho — local-first control plane for AI software delivery"

# Single source of truth for subcommand categorization. Every subcommand
# registered by ``build_parser`` (cli/orcho.py) — except the service-only
# ``help`` command, which is surfaced in the "More help" section — must appear
# here exactly once. A test in tests/unit/cli/test_cli_orcho.py asserts the set
# of names below equals ``set(sub.choices) - {"help"}`` so this catalog cannot
# drift from the parser.
COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Workflows", [
        ("run", "run a single-project delivery pipeline"),
        ("cross", "run one delivery workflow across several projects"),
    ]),
    ("Inspect a run", [
        ("status", "show status of the latest run or a specific run id"),
        ("history", "list recent runs"),
        ("metrics", "show token/time metrics for recent runs"),
        ("cost", "API-equivalent cost report over a window of runs"),
        ("evidence", "compose a run evidence bundle"),
        ("diff", "print the run's captured diff.patch artifact"),
    ]),
    ("Workspace & config", [
        ("workspace", "initialise and manage Orcho workspaces"),
        ("profiles", "list execution profiles"),
        ("workflows", "list workflow profiles"),
        ("prompts", "show the prompt resolution chain"),
        ("pricing", "inspect / refresh pricing data used by cost"),
        ("verify", "execute declared verification-contract checks"),
        ("quality-gates", "show the declared verification gate matrix"),
    ]),
    ("Maintenance", [
        ("repair-state", "inspect and safely apply known run-state repairs"),
    ]),
    # NOTE: the ``tui`` and ``web`` interface commands are intentionally
    # omitted from the advertised listing until their packages ship on PyPI —
    # advertising them here would point a new user at an uninstallable
    # ``pip install``. The subcommands remain registered (hidden) in
    # ``cli/orcho.py`` so anyone who already has the package can still call them.
]


def _plain(text: str) -> str:
    """Header renderer that leaves the text untouched."""
    return text


def _color_header(text: str) -> str:
    """Header renderer that paints via the shared color policy.

    ``color=None`` lets :func:`paint` apply the documented resolution
    order (explicit color -> process override -> auto-detect against
    ``stream``); we never pre-gate on ``is_color_active`` (that would
    ignore ``set_color_enabled``) and never flip the override here
    (CLI-startup wiring only, per ``core/io/AGENTS.md``).
    """
    return paint(text, C.BOLD, C.CYAN, color=None, stream=sys.stdout)


def _command_section(header: Callable[[str], str]) -> str:
    """Render COMMAND_GROUPS as a command listing, ``header`` per category."""
    width = max(
        len(name) for _, commands in COMMAND_GROUPS for name, _ in commands
    )
    blocks: list[str] = []
    for title, commands in COMMAND_GROUPS:
        lines = [header(f"{title}:")]
        lines.extend(f"  {name:<{width}}  {desc}" for name, desc in commands)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _compose_quick_help(header: Callable[[str], str]) -> str:
    """Assemble the onboarding text; ``header`` decides plain vs colored.

    With ``header=_plain`` the result is the canonical ANSI-free text used
    as the argparse epilog; with ``header=_color_header`` the tagline and
    section/category headers route through :func:`paint`. The non-header
    body is identical in both, so ``strip_ansi(render_quick_help())`` equals
    the plain :data:`QUICK_HELP`.
    """
    return f"""\
{header(TAGLINE)}

{header("Start here:")}
  orcho run   --task "Add health endpoint" --project ./api --mock
  orcho cross --task "Add telemetry" --projects api:./api web:./web --mock

{_command_section(header)}

{header("Output modes:")}
  summary   compact progress, default
  live      summary + live agent transcript
  debug     live + trace/vdump/full previews

{header("Aliases:")}
  --stream-output  same as --output live
  --verbose, -v    same as --output debug

{header("Run state:")}
  Runs are written under the active Orcho workspace.
  Use `orcho status`, `orcho history`, or `orcho evidence`.

{header("More help:")}
  orcho run --help
  orcho cross --help
  orcho help            full command help (also: orcho help --verbose)
"""


# Static, ANSI-free onboarding text. Used as the argparse epilog, so it must
# stay deterministic plain text for ``parser.format_help()``.
QUICK_HELP = _compose_quick_help(_plain)


def render_quick_help() -> str:
    """Return the onboarding text with colored headers per the color policy."""
    return _compose_quick_help(_color_header)


def iter_command_groups(
    choices: Iterable[str],
) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(category title, command names)`` for the verbose help dump.

    Walks :data:`COMMAND_GROUPS` in order, keeping only the command names
    present in ``choices`` (the parser's registered subcommands). Any choice
    not categorized — e.g. the service-only ``help`` command — is collected
    into a trailing ``"Other"`` group in ``choices`` order, so a verbose dump
    over the yielded groups covers every subcommand.
    """
    order = list(choices)
    available = set(order)
    categorized: set[str] = set()
    for title, commands in COMMAND_GROUPS:
        names = [name for name, _ in commands if name in available]
        if names:
            categorized.update(names)
            yield title, names
    leftover = [name for name in order if name not in categorized]
    if leftover:
        yield "Other", leftover


def render_verbose_header(title: str) -> str:
    """Return a category title painted per the shared color policy.

    Same policy as :func:`render_quick_help`: routes through
    ``paint(color=None, stream=sys.stdout)`` so the override / auto-detect
    order decides; no ``set_color_enabled`` and no ``is_color_active``
    pre-gate inside the renderer.
    """
    return _color_header(title)

RUN_OUTPUT_HELP = """\
Output modes:
  --output summary   compact progress, default
  --output live      summary + live agent transcript
  --output debug     live + trace/vdump/full previews

Aliases:
  --stream-output    same as --output live
  --verbose, -v      same as --output debug

When multiple output-mode flags are present, the last one on the CLI wins.
"""

RUN_EPILOG = RUN_OUTPUT_HELP + """
Examples:
  orcho run --task "Add health endpoint" --project ./api --mock
  orcho run --task-file task.md --project ./api --output live
  orcho run --resume 20260514_120000 --project ./api
  orcho run --resume                                  # bare: resume latest run
"""

CROSS_EPILOG = RUN_OUTPUT_HELP + """
Examples:
  orcho cross --task "Add telemetry" --projects api:./api web:./web --mock
  orcho cross --task "Add telemetry" --projects api:./api web:./web --mode plan
  orcho cross --task-file task.md --projects api:./api web:./web --output debug
"""


def print_quick_help() -> None:
    print(render_quick_help())
