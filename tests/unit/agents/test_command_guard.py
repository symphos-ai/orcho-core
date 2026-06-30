"""Runtime guardrails for agent-issued shell commands."""
from __future__ import annotations

import json
import sys

from agents.command_guard import (
    blocked_agent_stream_line,
    blocked_claude_write_tool_use,
    blocked_destructive_git_command,
    shell_command_candidates_from_text,
)
from agents.stream import StreamAbort, _stream_run


def _claude_bash_line(command: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": command},
            }],
        },
    })


def test_destructive_git_checkout_is_blocked() -> None:
    blocked = blocked_destructive_git_command("git checkout -- test_calc.py")

    assert blocked is not None
    assert "git checkout" in blocked.reason


def test_destructive_git_with_env_prefix_is_blocked() -> None:
    blocked = blocked_destructive_git_command(
        "GIT_TRACE=1 command git reset --hard HEAD"
    )

    assert blocked is not None
    assert "git reset" in blocked.reason


def test_read_only_git_commands_are_allowed() -> None:
    assert blocked_destructive_git_command(
        "git diff HEAD -- calc.py && git status --short"
    ) is None


def test_claude_bash_tool_use_is_inspected() -> None:
    blocked = blocked_claude_write_tool_use(
        _claude_bash_line("git restore test_calc.py")
    )

    assert blocked is not None
    assert blocked.command == "git restore test_calc.py"


def test_plain_text_tool_line_is_inspected_for_non_claude_runtimes() -> None:
    blocked = blocked_agent_stream_line("tool Bash: git checkout -- test_calc.py")

    assert blocked is not None
    assert blocked.command == "git checkout -- test_calc.py"


def test_shell_command_candidates_strip_common_prefixes() -> None:
    assert list(shell_command_candidates_from_text("$ git clean -fd")) == [
        "git clean -fd"
    ]


# ── GWT-1: worktree_cwd_path relaxes destructive-git guard ───────────────────


def test_destructive_git_allowed_inside_worktree() -> None:
    assert blocked_destructive_git_command(
        "git checkout -- test_calc.py",
        worktree_cwd_path="/run/checkout",
    ) is None


def test_destructive_git_still_blocked_without_worktree_path() -> None:
    assert blocked_destructive_git_command("git checkout -- test_calc.py") is not None


def test_write_tool_use_allowed_inside_worktree() -> None:
    line = json.dumps({
        "type": "assistant",
        "message": {
            "content": [{
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "git reset --hard HEAD"},
            }],
        },
    })
    assert blocked_claude_write_tool_use(line, worktree_cwd_path="/run/checkout") is None


def test_agent_stream_line_allowed_inside_worktree() -> None:
    assert blocked_agent_stream_line(
        "tool Bash: git restore test_calc.py",
        worktree_cwd_path="/run/checkout",
    ) is None


# ── F2 regression: basename-collision guard (ADR 0033 review) ─────────────────
# A user project whose real cwd basename happens to be "checkout" must NOT
# silently bypass the destructive-git guard.  The ContextVar-based approach
# that replaced the old Path(cwd).name=="checkout" heuristic prevents this.
# These tests verify the guard contracts at the guard-function level; the
# ContextVar tests live in test_worktree_resolution.py.

def test_guard_still_blocks_when_worktree_path_is_none() -> None:
    """worktree_cwd_path=None → guard must block destructive git."""
    assert blocked_destructive_git_command(
        "git reset --hard HEAD", worktree_cwd_path=None,
    ) is not None


def test_guard_still_blocks_stream_line_when_worktree_path_is_none() -> None:
    assert blocked_agent_stream_line(
        "tool Bash: git checkout -- file.py", worktree_cwd_path=None,
    ) is not None


def test_stream_abort_kills_subprocess_quickly() -> None:
    cmd = [
        sys.executable,
        "-c",
        "import time; print('ready', flush=True); time.sleep(30)",
    ]

    def on_line(line: str) -> None:
        if "ready" in line:
            raise StreamAbort("test guard")

    stdout, returncode, stderr, duration = _stream_run(
        cmd,
        timeout=10,
        on_line=on_line,
    )

    assert "ready" in stdout
    assert returncode != 0
    assert "ABORTED by stream guard: test guard" in stderr
    assert duration < 5


# ── T2: unsafe free-text process-polling guard (diagnostic warn) ─────────────

from agents.command_guard import (  # noqa: E402
    GUARDRAIL_UNSAFE_PROCESS_POLLING,
    agent_commands_from_stream_line,
    blocked_unsafe_process_polling,
)
from agents.stall_protocol import StallReason  # noqa: E402


def _codex_text_line(command: str) -> str:
    """A Codex-style human-readable command line (no structured JSON)."""
    return f"tool Bash: {command}"


# (a) the dogfood loop form is detected, carrying the StallReason category.


def test_kill_zero_pgrep_dogfood_form_is_flagged() -> None:
    blocked = blocked_unsafe_process_polling(
        'kill -0 $(pgrep -f "pytest -q -m")'
    )
    assert blocked is not None
    assert blocked.guardrail == GUARDRAIL_UNSAFE_PROCESS_POLLING
    assert blocked.guardrail == StallReason.UNSAFE_PROCESS_POLLING


def test_while_kill_pgrep_loop_is_flagged() -> None:
    blocked = blocked_unsafe_process_polling(
        'while kill -0 $(pgrep -f "pytest -q -m"); do sleep 1; done'
    )
    assert blocked is not None
    assert blocked.guardrail == GUARDRAIL_UNSAFE_PROCESS_POLLING


def test_plain_pgrep_f_and_pkill_f_are_flagged() -> None:
    assert blocked_unsafe_process_polling('pgrep -f "pytest -q -m"') is not None
    assert blocked_unsafe_process_polling("pkill -f pytest") is not None
    # Combined short-option clusters that include -f.
    assert blocked_unsafe_process_polling("pgrep -lf pytest") is not None


# (b) safe commands do not false-positive.


def test_safe_commands_are_not_flagged() -> None:
    safe = [
        "pytest -q -m 'not e2e and not packaging'",
        "git status --short",
        "ls -la",
        "grep -f patterns.txt results.log",   # -f belongs to grep, not pgrep
        "kill -0 12345",                      # poll OWN child by PID — fine
        "pgrep mydaemon",                     # name match (no -f) — not free-text
        "ps aux | grep pytest",               # no pgrep/pkill at all
        "echo done",
    ]
    for command in safe:
        assert blocked_unsafe_process_polling(command) is None, command


# (c) verdict is text-only; a foreign argv elsewhere on the host is irrelevant,
#     and the guard never scans or signals processes.


def test_verdict_is_text_only_no_process_scan() -> None:
    # A real ``pytest -q -m`` process running elsewhere cannot influence the
    # verdict: the guard only inspects the command TEXT it is handed. A bare
    # pytest invocation (the thing such a process is running) is never flagged.
    assert blocked_unsafe_process_polling("pytest -q -m 'not e2e'") is None
    # And the unsafe poll is flagged purely from its own text, regardless of
    # what is or isn't running on the machine.
    assert blocked_unsafe_process_polling(
        'kill -0 $(pgrep -f "pytest -q -m")'
    ) is not None


def test_worktree_does_not_relax_process_polling_guard() -> None:
    # Unlike the destructive-git guard, an orcho-managed worktree must NOT
    # relax this check — matching foreign processes by argv is a hazard
    # regardless of cwd.
    assert blocked_unsafe_process_polling(
        'kill -0 $(pgrep -f "pytest")', worktree_cwd_path="/run/checkout",
    ) is not None
    assert blocked_agent_stream_line(
        _codex_text_line('kill -0 $(pgrep -f "pytest")'),
        worktree_cwd_path="/run/checkout",
    ) is not None


def test_stream_line_flags_polling_across_providers() -> None:
    poll = 'kill -0 $(pgrep -f "pytest -q -m")'
    # Claude structured tool_use
    claude = blocked_agent_stream_line(_claude_bash_line(poll))
    assert claude is not None and claude.guardrail == GUARDRAIL_UNSAFE_PROCESS_POLLING
    # Codex human-readable text
    codex = blocked_agent_stream_line(_codex_text_line(poll))
    assert codex is not None and codex.guardrail == GUARDRAIL_UNSAFE_PROCESS_POLLING


def test_agent_commands_from_stream_line_yields_non_git_text() -> None:
    poll = 'kill -0 $(pgrep -f "pytest")'
    assert poll in list(agent_commands_from_stream_line(_codex_text_line(poll)))


def test_destructive_git_takes_precedence_over_polling() -> None:
    """A line carrying both a destructive git command and a poll aborts on git
    (the more severe verdict), not the diagnostic warn."""
    line = _claude_bash_line('git reset --hard HEAD && pgrep -f "pytest"')
    blocked = blocked_agent_stream_line(line)
    assert blocked is not None
    assert blocked.guardrail == "destructive_git"


# (d) a warn-only verdict does not terminate the subprocess.


def test_process_polling_warn_does_not_abort_subprocess() -> None:
    """Mirror the runtime _on_line policy: a poll line warns (no StreamAbort),
    so the subprocess runs to normal completion."""
    poll = 'kill -0 $(pgrep -f "pytest -q -m")'
    cmd = [
        sys.executable,
        "-c",
        f"print({poll!r}, flush=True); print('finished', flush=True)",
    ]
    warned: list[str] = []

    def on_line(line: str) -> None:
        blocked = blocked_agent_stream_line(line)
        if blocked is None:
            return
        if blocked.guardrail == GUARDRAIL_UNSAFE_PROCESS_POLLING:
            warned.append(blocked.command)
            return  # diagnostic only — never raise
        raise StreamAbort("destructive")

    stdout, returncode, stderr, _duration = _stream_run(cmd, timeout=10, on_line=on_line)

    assert warned, "the poll line should have produced a warn"
    assert "finished" in stdout
    assert returncode == 0
    assert "ABORTED by stream guard" not in (stderr or "")
