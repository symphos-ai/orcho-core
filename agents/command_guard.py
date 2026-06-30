"""Runtime guardrails for agent-issued shell commands.

The prompts tell write-capable agents not to discard user-owned work, but
prompts are advisory. This module is the runtime backstop for the specific
class of failure that live repeated-run testing exposed: destructive git
rollback commands issued through Claude's Bash tool.

A second, *diagnostic-only* guard flags unsafe free-text process polling
(``pgrep -f`` / ``pkill -f``). Unlike the destructive-git guard — which aborts
the call — the process-polling guard never aborts and never kills: it carries
the :class:`agents.stall_protocol.StallReason` ``unsafe_process_polling``
category so the runtime can emit a ``warn`` diagnostic and keep running.
"""
from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from agents.stall_protocol import StallReason

ORCHO_GUARDRAIL_BLOCKED = "ORCHO_GUARDRAIL_BLOCKED"

#: Guardrail category for the destructive-git guard (abort semantics).
GUARDRAIL_DESTRUCTIVE_GIT = "destructive_git"
#: Guardrail category for the unsafe free-text process-polling guard
#: (diagnostic ``warn`` only — never abort/kill). Equal to the
#: ``StallReason.unsafe_process_polling`` value so the two layers agree.
GUARDRAIL_UNSAFE_PROCESS_POLLING = str(StallReason.UNSAFE_PROCESS_POLLING)


@dataclass(frozen=True)
class BlockedCommand:
    """A shell command flagged by a runtime guardrail.

    ``guardrail`` names the category so callers can route the verdict: the
    default ``destructive_git`` is an abort, while ``unsafe_process_polling``
    is a diagnostic warn that must NOT abort or kill the subprocess.
    """

    command: str
    reason: str
    guardrail: str = GUARDRAIL_DESTRUCTIVE_GIT


_SEGMENT_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\n)")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TEXT_PREFIX_RE = re.compile(
    r"^(?:tool\s+Bash:|Bash:|command:|cmd:|shell:|run:|exec:)\s*",
    re.IGNORECASE,
)


def blocked_destructive_git_command(
    command: str,
    *,
    worktree_cwd_path: str | Path | None = None,
) -> BlockedCommand | None:
    """Return a block reason when ``command`` contains risky git mutation.

    The guard is intentionally small and targeted. It does not try to be a
    general shell sandbox; it catches the rollback/switch commands that can
    erase or hide pre-existing working-tree state during Orcho write phases.

    When ``worktree_cwd_path`` is set, the agent's effective cwd is an
    orcho-managed isolated worktree (ADR 0033). Destructive git inside the
    worktree cannot harm user-owned work, so the block is lifted — the agent
    has full git freedom inside its own sandbox. Outside the worktree (or
    when the parameter is None) the existing detection stands unchanged.
    """
    if worktree_cwd_path is not None:
        return None
    for segment in _SEGMENT_SPLIT_RE.split(command or ""):
        argv = _safe_split(segment)
        if not argv:
            continue
        argv = _strip_prefixes(argv)
        if len(argv) < 2 or argv[0] != "git":
            continue
        subcommand = argv[1]
        if subcommand in {"reset", "restore", "clean", "revert", "switch"}:
            return BlockedCommand(
                command=command,
                reason=f"git {subcommand} can discard or hide working-tree state",
            )
        if subcommand == "checkout":
            return BlockedCommand(
                command=command,
                reason="git checkout can discard file changes or switch context",
            )
    return None


# ── unsafe free-text process polling guard ───────────────────────────────────

# A pgrep/pkill invocation, even when wrapped in command substitution or
# backticks. ``\b`` matches at the start of the binary name when preceded by
# ``$(`` or `` ` `` (both non-word) just as well as at a line/whitespace start.
_PGREP_PKILL_RE = re.compile(r"\b(?:pgrep|pkill)\b")
# A short-option cluster that includes the ``-f`` (full-command-line match)
# flag — ``-f``, ``-fl``, ``-lf``, ``-af`` … A leading boundary keeps this a
# flag rather than a substring of an argument.
_F_FLAG_RE = re.compile(r"(?:^|\s)-[A-Za-z]*f[A-Za-z]*\b")
# Characters that end the pgrep/pkill argument scope, so an unrelated ``-f``
# elsewhere in a compound line is not mis-attributed to the pgrep call.
_CMD_BOUNDARY_RE = re.compile(r"[;&|)`\n]")


def blocked_unsafe_process_polling(
    command: str,
    *,
    worktree_cwd_path: str | Path | None = None,
) -> BlockedCommand | None:
    """Flag unsafe free-text process polling (``pgrep -f`` / ``pkill -f``).

    Detects the full-command-line, free-text process matching an agent reaches
    for when it wants to wait on a backgrounded command, including the compound
    and loop forms:

    * ``pgrep -f "pytest -q -m"`` / ``pkill -f ...``
    * ``kill -0 $(pgrep -f "pytest -q -m")``
    * ``while kill -0 $(pgrep -f ...); do sleep 1; done``

    Free-text argv matching is unsafe because it can match *unrelated* host
    processes — the dogfood hazard is a real ``pytest -q -m`` running elsewhere
    on the machine being mistaken for the run's own command. Polling the run's
    OWN child by PID (``kill -0 <pid>``) matches nothing here and is correctly
    left alone.

    Unlike :func:`blocked_destructive_git_command`, an orcho-managed worktree
    does **not** relax this check — matching foreign processes by argv is a
    hazard regardless of cwd. ``worktree_cwd_path`` is accepted only for
    call-site symmetry and is intentionally ignored.

    The verdict is derived purely from the command TEXT; this guard never
    scans, signals, or kills any process. The returned ``BlockedCommand``
    carries ``guardrail=unsafe_process_polling`` so the runtime routes it as a
    diagnostic ``warn``, never an abort.
    """
    text = command or ""
    for match in _PGREP_PKILL_RE.finditer(text):
        tail = text[match.end():]
        boundary = _CMD_BOUNDARY_RE.search(tail)
        scope = tail[: boundary.start()] if boundary else tail
        if _F_FLAG_RE.search(scope):
            return BlockedCommand(
                command=command,
                reason=(
                    "free-text process polling (pgrep/pkill -f) can match "
                    "unrelated processes by command line; poll the run's own "
                    "child by PID instead"
                ),
                guardrail=GUARDRAIL_UNSAFE_PROCESS_POLLING,
            )
    return None


def claude_bash_commands_from_jsonl(line: str) -> Iterator[str]:
    """Yield Bash command strings from one Claude stream-json line."""
    line = (line or "").strip()
    if not line.startswith("{"):
        return
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return
    if payload.get("type") != "assistant":
        return
    message = payload.get("message")
    if not isinstance(message, dict):
        return
    for block in message.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use" or block.get("name") != "Bash":
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            yield command


def blocked_claude_write_tool_use(
    line: str,
    *,
    worktree_cwd_path: str | Path | None = None,
) -> BlockedCommand | None:
    """Detect blocked Bash tool use in one Claude stream-json line."""
    for command in claude_bash_commands_from_jsonl(line):
        blocked = blocked_destructive_git_command(command, worktree_cwd_path=worktree_cwd_path)
        if blocked is not None:
            return blocked
    return None


def gemini_shell_commands_from_jsonl(line: str) -> Iterator[str]:
    """Yield shell command strings from one Gemini stream-json line.

    Gemini CLI 0.40 emits ``tool_use`` events as top-level JSONL records
    (not wrapped in an assistant content array like Claude). The
    shell-executing tool is ``run_shell_command`` and the command lands
    under ``parameters.command``. Tool names may change across CLI
    releases — ``run_shell_command`` is the documented 0.40 name. The
    yield is empty for every other tool (``read_file``, ``glob``, ...)
    and for malformed input.
    """
    line = (line or "").strip()
    if not line.startswith("{"):
        return
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict) or payload.get("type") != "tool_use":
        return
    if payload.get("tool_name") != "run_shell_command":
        return
    params = payload.get("parameters")
    if not isinstance(params, dict):
        return
    command = params.get("command")
    if isinstance(command, str) and command.strip():
        yield command


def blocked_gemini_write_tool_use(
    line: str,
    *,
    worktree_cwd_path: str | Path | None = None,
) -> BlockedCommand | None:
    """Detect blocked shell tool use in one Gemini stream-json line."""
    for command in gemini_shell_commands_from_jsonl(line):
        blocked = blocked_destructive_git_command(
            command, worktree_cwd_path=worktree_cwd_path,
        )
        if blocked is not None:
            return blocked
    return None


def blocked_agent_stream_line(
    line: str,
    *,
    worktree_cwd_path: str | Path | None = None,
) -> BlockedCommand | None:
    """Detect blocked commands in a write-agent stream line.

    Claude and Gemini emit structured stream-json with tool calls;
    Codex prints human-readable command lines. This function supports
    every shape so provider wrappers can share one guardrail.
    """
    # Destructive-git guard first: it aborts the call and is worktree-relaxable.
    structured = blocked_claude_write_tool_use(line, worktree_cwd_path=worktree_cwd_path)
    if structured is not None:
        return structured
    structured = blocked_gemini_write_tool_use(line, worktree_cwd_path=worktree_cwd_path)
    if structured is not None:
        return structured
    for candidate in shell_command_candidates_from_text(line):
        blocked = blocked_destructive_git_command(candidate, worktree_cwd_path=worktree_cwd_path)
        if blocked is not None:
            return blocked
    # Unsafe free-text process polling: a diagnostic warn (never abort/kill),
    # provider-neutral over every command shape, and never worktree-relaxed.
    for command in agent_commands_from_stream_line(line):
        unsafe = blocked_unsafe_process_polling(
            command, worktree_cwd_path=worktree_cwd_path,
        )
        if unsafe is not None:
            return unsafe
    return None


def _clean_stream_text(line: str) -> str:
    """Normalise one human-readable stream line into a bare command string.

    Strips ANSI, surrounding backticks, the ``tool Bash:`` / ``command:`` …
    prefixes, and a leading shell-prompt sigil (``$`` / ``>`` / ``+``). Returns
    ``""`` for an empty / whitespace-only line.
    """
    text = _ANSI_RE.sub("", line or "").strip()
    if not text:
        return ""
    text = text.strip("`")
    text = _TEXT_PREFIX_RE.sub("", text).strip()
    for prefix in ("$", ">", "+"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text.startswith("tool Bash:"):
        text = text.removeprefix("tool Bash:").strip()
    return text


def shell_command_candidates_from_text(line: str) -> Iterator[str]:
    """Yield git-command-looking strings from one human-readable stream line."""
    text = _clean_stream_text(line)
    if text.startswith("git ") or text.startswith("command git ") or text.startswith("sudo git "):
        yield text


def agent_commands_from_stream_line(line: str) -> Iterator[str]:
    """Yield every candidate shell command from one stream line, any provider.

    Unions the structured tool-use commands (Claude Bash, Gemini
    ``run_shell_command``) with the cleaned human-readable text (Codex). Unlike
    :func:`shell_command_candidates_from_text`, the text candidate is yielded
    regardless of leading binary, because process-polling forms
    (``pgrep -f`` / ``kill -0 $(pgrep -f ...)``) do not start with ``git``.
    """
    yield from claude_bash_commands_from_jsonl(line)
    yield from gemini_shell_commands_from_jsonl(line)
    text = _clean_stream_text(line)
    if text:
        yield text


def _safe_split(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.strip().split()


def _strip_prefixes(argv: list[str]) -> list[str]:
    out = list(argv)
    while out and _ENV_ASSIGN_RE.match(out[0]):
        out.pop(0)
    while out and out[0] in {"command", "sudo"}:
        out.pop(0)
    return out
