"""
agents/runtimes/codex.py — CodexAgent (thin wrapper around the Codex CLI).

Implements :class:`IAgentRuntime`. Every invocation runs through
``codex exec --json --dangerously-bypass-approvals-and-sandbox
--skip-git-repo-check``. The combined flag's two effects are both
load-bearing for the orchestrator: approvals bypass keeps the
non-interactive ``exec --json`` process from blocking on stdin, and
sandbox bypass lets reviewer phases run verification subprocesses
(``pytest``, ``ruff``, ``git log``) that ``--sandbox read-only``
forbids entirely. Destructive git operations are caught regardless
by the streaming guardrail (``blocked_agent_stream_line`` in
:func:`_invoke_exec`). The runtime captures the Codex thread handle
from JSONL so later calls with ``continue_session=True`` resume via
``codex exec resume``.

Each child line is parsed by
:func:`agents.stream_parsers.parse_codex_line` into Orcho events
(``agent.tool_use``, ``agent.text``, ``agent.summary``) and formatted by
:func:`format_codex_line_for_stdout` for both live stdout and ``output.log``:
known Codex JSONL events are either formatted into a compact human-readable
line or suppressed; unknown / non-JSON lines pass through verbatim so
diagnostic output from the CLI stays visible. The agent result is read from
``--output-last-message`` first; when that file is empty the runtime falls
back to the last ``agent_message.text`` extracted from the JSONL stdout via
:func:`_extract_codex_assistant_text`.

Reaches ``_stream_run`` and ``subprocess`` through the ``agents`` package
namespace so test monkey-patches (``agents._stream_run`` /
``agents.subprocess.run``) take effect at call time.

NOTE on Codex trusted-directory requirement
-------------------------------------------
Codex CLI v0.125+ checks that it runs from inside a Git repository or a
directory explicitly listed as ``trust_level = "trusted"`` in
``~/.codex/config.toml``.  Some project roots (e.g. ``mag_unity_new-copy``)
are SVN-primary and are **not** Git repositories at their top level — only a
sub-tree is in Git.  When ``subprocess.run`` receives ``cwd=<non-git-root>``,
Codex fails with:
    Not inside a trusted directory and --skip-git-repo-check was not specified.

The helper ``_safe_cwd()`` resolves this by walking *up* the given path until
it finds a ``.git`` marker.  If the walk exhausts without finding one it falls
back to the workspace-orchestrator directory (parent of ``multiagent-core/``)
which is always a Git repo and always trusted.
"""

import json
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from agents.command_guard import (
    GUARDRAIL_UNSAFE_PROCESS_POLLING,
    ORCHO_GUARDRAIL_BLOCKED,
    blocked_agent_stream_line,
)
from agents.owned_child import OwnedChildRegistry
from agents.runtimes.codex_skills import CodexSkillScope
from agents.runtimes.codex_telemetry import load_codex_telemetry
from agents.stall_protocol import EventStallDiagnosticSink
from agents.stream import StreamAbort
from core.infra import config
from core.infra.lazy import LazyValue, lazy_cli_binary
from core.infra.platform import engine_home as _engine_home, workspace_dir as _ws_dir
from pipeline.runtime.roles import AttachmentKind

if TYPE_CHECKING:
    from agents.runtimes.identity import RuntimeIdentity
    from pipeline.runtime.steps import Attachment

# Codex CLI prints its token count on a line like:
#     tokens used
#     9,498
# The number is the total (codex doesn't split input/output the way
# Claude does). Allow comma thousand-separators and optional whitespace
# around the line so different CLI versions and merged stdout+stderr
# streams both match.
_CODEX_TOKENS_RE = re.compile(
    r"tokens\s+used\s*\n\s*([0-9][0-9,]*)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_CODEX_SESSION_KEYS = ("session_id", "conversation_id", "thread_id")
_CODEX_SESSION_RE = re.compile(
    r'"(?:session_id|conversation_id|thread_id)"\s*:\s*"([^"]+)"'
)


def _extract_codex_tokens(output: str) -> int | None:
    """Parse the ``tokens used\\nN`` trailer from a codex CLI invocation.

    Returns the integer token total (commas stripped) or ``None`` when
    the trailer isn't present (codex version too old / call killed
    mid-stream / the line was rewritten by an upstream change).
    Callers must accept None and fall back to ``estimate_tokens``.
    """
    if not output:
        return None
    m = _CODEX_TOKENS_RE.search(output)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_codex_usage(output: str) -> dict | None:
    """Return the final ``turn.completed.usage`` object from Codex JSONL."""
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "turn.completed":
            usage = obj.get("usage")
            if isinstance(usage, dict):
                return usage
    return None


def _optional_usage_int(usage: dict, key: str) -> int | None:
    """Return an integer usage field, preserving missing as ``None``."""
    if key not in usage:
        return None
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return None


def _count_codex_tool_uses(output: str) -> int:
    """Count completed built-in/MCP tool calls in Codex JSONL output."""
    total = 0
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"command_execution", "mcp_tool_call"}:
            total += 1
    return total


def _extract_codex_assistant_text(output: str) -> str:
    """Last-write-wins fallback for the phase result when ``-o`` is empty.

    Returns the most recent ``item.completed`` ``agent_message.text`` seen in
    the JSONL stdout, mirroring ``--output-last-message`` semantics. Live
    transcript, ``output.log``, and ``events.jsonl`` still carry the finalized
    assistant/tool messages — only the phase result fallback is reduced to the
    last assistant message so the reviewer parser sees one envelope, not a
    preamble plus the final answer.
    """
    if not output:
        return ""
    last_text = ""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "item.completed":
            continue
        item = obj.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            last_text = text
    return last_text.strip()


def _extract_codex_session_id(output: str) -> str | None:
    """Parse a resumable Codex session handle from JSONL output.

    Codex CLI event names can shift across releases, but the bridge handle is
    consistently emitted as one of a small set of id keys. Scan every JSON line
    recursively and keep a regex fallback for colorized/noisy lines.
    """
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        sid = _find_codex_session_id(obj)
        if sid:
            return sid
    match = _CODEX_SESSION_RE.search(output)
    return match.group(1) if match else None


def _find_codex_session_id(obj: object) -> str | None:
    if isinstance(obj, dict):
        for key in _CODEX_SESSION_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
        for value in obj.values():
            sid = _find_codex_session_id(value)
            if sid:
                return sid
    elif isinstance(obj, list):
        for value in obj:
            sid = _find_codex_session_id(value)
            if sid:
                return sid
    return None


_PROVIDER = "codex"


# ---------------------------------------------------------------------------
# Trusted-directory resolver
# ---------------------------------------------------------------------------
# Codex requires a Git-rooted or trusted CWD. Fallback to the workspace root
# (or the engine home if no workspace is configured) — both are always Git repos.

_FALLBACK_TRUSTED_CWD: str = str(_ws_dir() or _engine_home())


def _safe_cwd(requested: str | None) -> str:
    """Return a Codex-trusted working directory given the caller's preferred path.

    Codex CLI v0.125+ requires its CWD to be inside a Git repository or
    explicitly listed as trusted in ``~/.codex/config.toml``.  This function
    resolves the nearest Git root so every Codex subprocess gets a valid CWD
    regardless of the project's VCS layout.

    Search order
    ------------
    1. Walk **up** from ``requested`` — covers the common case where the path
       is already inside a Git working tree (workspace-orchestrator, API,
       stats, and Unity's Assets/_Match-Three-Common/ subtree when the caller
       passes that subdirectory directly).

    2. Walk **down** up to two levels from ``requested`` — covers SVN-primary
       projects like ``mag_unity_new-copy/`` where the real Git checkout lives
       *inside* the project root (e.g. ``Assets/_Match-Three-Common/``).

    3. **Fallback** to ``_FALLBACK_TRUSTED_CWD`` (workspace-orchestrator)
       which is always a Git repo and always trusted.  Used for stdin-only
       calls (review / plan / hypothesis validation) where Codex
       does not need to inspect a specific working tree.

    Args:
        requested: The cwd the caller wants to use.  May be None, a Git root,
                   a subdirectory of one, or an SVN root containing a Git
                   subtree (Unity case).

    Returns:
        An absolute path string that Codex will accept.
    """
    if requested:
        p = Path(requested).resolve()

        # 1. Upward search — O(depth), stops at filesystem root
        for candidate in [p, *p.parents]:
            if (candidate / ".git").exists():
                return str(candidate)

        # 2. Downward search — two levels deep (Unity: Assets/_Match-Three-Common)
        try:
            for child in p.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    return str(child)
                if child.is_dir():
                    for grandchild in child.iterdir():
                        if grandchild.is_dir() and (grandchild / ".git").exists():
                            return str(grandchild)
        except (PermissionError, OSError):
            pass

    # 3. Fallback — workspace-orchestrator is always trusted
    return _FALLBACK_TRUSTED_CWD


def _reject_text_attachments(attachments: "tuple[Attachment, ...]") -> None:
    """Defensive contract: TEXT attachments are rendered outside the runtime.

    See the matching helper in :mod:`agents.runtimes.claude` for the
    rationale. Codex doesn't currently consume multimodal attachments via
    CLI flags either, so for now non-TEXT attachments are accepted silently
    — they'll be wired up when the Codex CLI exposes a multimodal surface.
    """
    bad = [a for a in attachments if a.kind is AttachmentKind.TEXT]
    if bad:
        names = ", ".join(repr(a.name) for a in bad)
        raise ValueError(
            f"IAgentRuntime.invoke(attachments=...) received TEXT attachments "
            f"({names}). TEXT must be rendered into the prompt outside the "
            f"runtime via render_text_block(); only IMAGE/BINARY may be passed."
        )


class CodexAgent:
    """Runs the Codex CLI in non-interactive mode."""

    # Identifies the runtime backend at the instance surface; see the
    # equivalent annotation on :class:`ClaudeAgent` for why this lives
    # on the class instead of being written by every instantiation path.
    runtime: str = "codex"

    def __init__(self, model: str = config.CODEX_MODEL, *, effort: str | None = None):
        self._bin: LazyValue[str] = lazy_cli_binary("codex", config.get_codex_bin)
        self.model = model
        self.effort = effort
        self._owned_children = OwnedChildRegistry()
        self._skill_scope = CodexSkillScope()
        self.session_id: str | None = None
        self._followup_resume_pending: bool = False
        self._last_continue_session: bool = False
        self._last_resumed_session_id: str | None = None
        self._last_followup_parent_session_id: str | None = None
        # Populated by every call from ``turn.completed.usage`` when present.
        # Older Codex CLI builds only emit the legacy ``tokens used\n<N>``
        # trailer; in that fallback path only ``last_tokens_total`` is known.
        # ``cached_input_tokens`` and ``reasoning_output_tokens`` are subset
        # breakdowns of input/output respectively and must not be added to
        # ``last_tokens_total``.
        self.last_tokens_in: int | None = None
        self.last_tokens_in_fresh: int | None = None
        self.last_tokens_in_cache_read: int | None = None
        self.last_tokens_out: int | None = None
        self.last_tokens_out_reasoning: int | None = None
        self.last_tokens_total: int | None = None
        self.last_cost_usd: float | None = None
        self.last_tool_use_count: int = 0
        # Live context-pressure telemetry sourced from the Codex CLI's
        # rollout JSONL ($CODEX_HOME/sessions/.../rollout-*.jsonl). Both
        # window and used must be present for the runtime-reported branch
        # of resolve_context_pressure() to fire; otherwise the resolver
        # falls back to ORCHO_ESTIMATED. The rate-limits and source-path
        # attrs are populated independently for in-process debug — they
        # must NOT be emitted via core.observability.events, prompt
        # evidence, or run metrics writers.
        self.last_context_window_tokens: int | None = None
        self.last_context_used_tokens: int | None = None
        self.last_context_remaining_tokens: int | None = None
        self.last_codex_rate_limits: dict | None = None
        self.last_codex_telemetry_source: str | None = None

    @property
    def bin(self) -> str:
        return self._bin.get()

    @bin.setter
    def bin(self, value: str) -> None:
        self._bin.set(value)

    def configure_skill_scope(self, *, include_user_skills: bool) -> None:
        """Project Orcho's source scope onto Codex-native discovery."""
        self._skill_scope = CodexSkillScope(
            include_user_skills=include_user_skills,
        )

    def _config_args(self) -> list[str]:
        """Build common Codex config overrides."""
        cmd = ["-c", f'model="{self.model}"']
        if self.effort:
            cmd += ["-c", f'model_reasoning_effort="{self.effort}"']
        cmd += self._skill_scope.config_args()
        return cmd

    def _exec_cmd(self, *, mutates_artifacts: bool, resume: bool = False) -> list[str]:
        """Build the ``codex exec`` / ``codex exec resume`` invocation.

        Every Orcho-issued codex call uses
        ``--dangerously-bypass-approvals-and-sandbox`` regardless of
        ``mutates_artifacts``. The flag's name advertises two things —
        approvals bypass and sandbox bypass — and Orcho needs both:

        * **Approvals**: codex's interactive approval prompts have
          nowhere to go inside ``codex exec --json``. Without the
          bypass, the exec process blocks forever waiting for stdin
          that orcho never writes.
        * **Sandbox**: codex's ``--sandbox read-only`` is not "no
          writes to project files" — it forbids subprocess execution
          entirely (no ``pytest``, no ``ruff``, no ``git log``).
          That blinds reviewer phases (``validate_plan``,
          ``review_changes``, ``final_acceptance``) to any
          verification command. The marginal safety of strict
          read-only does not buy enough to justify the operator cost
          of a verdict gate that cannot run tests.

        Destructive git operations are caught regardless of sandbox
        mode by the streaming guardrail in :func:`_invoke_exec`'s
        ``_on_line`` handler (``blocked_agent_stream_line``). The
        guardrail is the real defence; the codex sandbox flag is
        belt-and-suspenders that turns out to also disable belts.

        ``mutates_artifacts`` is still threaded through to inform
        the runtime label / observability (``mode=read`` vs
        ``mode=write`` in stdout banners). It no longer changes the
        codex CLI flags.
        """
        cmd = [self.bin, "exec"]
        if resume:
            cmd.append("resume")
        cmd += [*self._config_args(), "--skip-git-repo-check", "--json"]
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
        return cmd

    def _capture_tokens(self, stdout: str, stderr: str = "") -> None:
        """Stash per-call Codex usage on the standard runtime attributes.

        Total is ``input_tokens + output_tokens`` from the JSONL
        ``turn.completed.usage`` record emitted by ``codex exec --json``.
        Breakdown fields (``cached_input_tokens``, ``reasoning_output_tokens``)
        are copied separately for live/debug cards and metrics enrichment.
        Falls back to the legacy ``tokens used\\nN`` trailer parser when the
        JSONL usage line is absent (older CLI builds, aborted turns).
        """
        haystack = f"{stdout}\n{stderr}"
        self.last_tool_use_count = _count_codex_tool_uses(haystack)
        self.last_tokens_in = None
        self.last_tokens_in_fresh = None
        self.last_tokens_in_cache_read = None
        self.last_tokens_out = None
        self.last_tokens_out_reasoning = None
        self.last_tokens_total = None
        usage = _extract_codex_usage(haystack)
        if usage is not None:
            tokens_in = _optional_usage_int(usage, "input_tokens")
            tokens_out = _optional_usage_int(usage, "output_tokens")
            cached_in = _optional_usage_int(usage, "cached_input_tokens")
            reasoning_out = _optional_usage_int(
                usage, "reasoning_output_tokens",
            )
            self.last_tokens_in = tokens_in
            self.last_tokens_out = tokens_out
            self.last_tokens_in_cache_read = cached_in
            self.last_tokens_out_reasoning = reasoning_out
            if tokens_in is not None and cached_in is not None:
                self.last_tokens_in_fresh = max(0, tokens_in - cached_in)
            self.last_tokens_total = _sum_codex_usage_tokens(usage)
            return
        self.last_tokens_total = _extract_codex_tokens(haystack)

    def _reset_brain_telemetry(self) -> None:
        """Clear all rollout-sourced telemetry attrs. Pure reset, no I/O."""
        self.last_context_window_tokens = None
        self.last_context_used_tokens = None
        self.last_context_remaining_tokens = None
        self.last_codex_rate_limits = None
        self.last_codex_telemetry_source = None

    def _capture_brain_telemetry(self) -> None:
        """Populate rollout-sourced telemetry attrs from the current session.

        Context-pressure attrs follow a both-or-neither rule: the runtime
        only reports `last_context_window_tokens`/`_used`/`_remaining` when
        both window and used are present. Debug attrs (rate limits, source
        path) are populated independently of context completeness so an
        operator can still see plan/limit state when the window is unknown.
        """
        self._reset_brain_telemetry()
        if self.session_id is None:
            return
        snapshot = load_codex_telemetry(self.session_id)
        if snapshot is None:
            return
        if snapshot.rate_limits is not None:
            self.last_codex_rate_limits = snapshot.rate_limits
        if snapshot.raw_source_path is not None:
            self.last_codex_telemetry_source = snapshot.raw_source_path
        if (
            snapshot.context_window_tokens is not None
            and snapshot.context_used_tokens is not None
        ):
            self.last_context_window_tokens = snapshot.context_window_tokens
            self.last_context_used_tokens = snapshot.context_used_tokens
            self.last_context_remaining_tokens = (
                snapshot.context_remaining_tokens
            )

    def _effort_label(self) -> str:
        """Suffix for human-readable labels: `` effort=high`` or empty.

        Surfaces the per-call reasoning effort in stdout so an operator can
        see at a glance which budget each phase is actually running with —
        without this the only place effort lands is the codex stderr banner.
        """
        return f" effort={self.effort}" if self.effort else ""

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: "tuple[Attachment, ...]" = (),
    ) -> str:
        """Run Codex with the given prompt. See :class:`IAgentRuntime`.

        Both read and write calls use ``codex exec --json`` so the runtime can
        capture a bridge handle and resume it on later ``continue_session``
        calls. Read calls run in a read-only sandbox; write calls keep the
        existing write-capable Orcho flags.
        """
        _reject_text_attachments(attachments)
        return self._invoke_exec(
            prompt, cwd,
            mutates_artifacts=mutates_artifacts,
            continue_session=continue_session,
        )

    def _invoke_review(self, prompt: str, cwd: str, *, continue_session: bool = False) -> str:
        """Read-only path kept for call-site compatibility."""
        return self._invoke_exec(
            prompt, cwd,
            mutates_artifacts=False,
            continue_session=continue_session,
        )

    def _invoke_exec(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = True,
        continue_session: bool = False,
    ) -> str:
        """Run ``codex exec`` with optional bridge continuation."""
        # Reset rollout-sourced telemetry *before* anything that might raise
        # (including _safe_cwd's filesystem walk). Guarantees every exit
        # path — happy, guardrail-blocked, auth-failure, unhandled — leaves
        # the attrs cleared so no stale window/used leaks into the next call.
        self._reset_brain_telemetry()
        import agents as _agents

        safe = _safe_cwd(cwd)
        import uuid as _uuid

        from core.observability import events as _events
        if self._followup_resume_pending and self.session_id:
            continue_session = True
            self._last_followup_parent_session_id = self.session_id
            self._followup_resume_pending = False
        else:
            self._last_followup_parent_session_id = None
        resumed_session_id = (
            self.session_id if (continue_session and self.session_id) else None
        )
        self._last_resumed_session_id = resumed_session_id
        # ``_last_continue_session`` mirrors the actual resume, not the
        # caller's intent. See ClaudeAgent.invoke for the rationale.
        self._last_continue_session = resumed_session_id is not None

        last_message_path = _codex_last_message_path()
        cmd = self._exec_cmd(
            mutates_artifacts=mutates_artifacts,
            resume=resumed_session_id is not None,
        )
        cmd += ["-o", str(last_message_path)]
        if resumed_session_id:
            cmd += [resumed_session_id, prompt]
        else:
            cmd += ["--cd", safe, prompt]
        from core.io.transcript import render_agent_invocation, render_incoming_prompt
        from core.observability.logging import get_verbose
        from core.observability.prompt_trace import take_last_prompt_turn as _take_turn
        mode_label = "write" if mutates_artifacts else "read"
        _last_turn = _take_turn()  # single take, clears slot even if debug is off
        _trace_view = _last_turn.trace_view() if _last_turn is not None else None
        print(render_agent_invocation(
            runtime="codex",
            model=self.model,
            effort=self.effort,
            prompt=prompt,
            trace_view=_trace_view,
            mode=mode_label,
            session_supported=True,
            session_id=resumed_session_id,
            continue_session=continue_session,
            cwd=cwd,
        ))
        if get_verbose():
            print(render_incoming_prompt(
                prompt, trace_view=_trace_view,
                model=self.model,
            ))
        # Internal label for ``_stream_run`` observability (kept stable
        # across the UX-line redesign so existing log greps don't break).
        label = f"codex exec --model {self.model}{self._effort_label()} ({mode_label})"
        from agents.stream_parsers.skill_registry import (
            active_registered_skill_names,
            discover_registered_skill_names,
        )
        skill_names = discover_registered_skill_names(str(safe))

        def _on_line(line: str) -> None:
            from agents.stream_parsers import parse_codex_line

            parse_codex_line(line, agent_label="invoke")
            # Relax destructive-git guard only when cwd matches the
            # validated orcho-managed worktree checkout path (ADR 0033).
            # The ContextVar is set by the orchestrator; None means
            # isolation is off, so the guard stays active.
            from pipeline.engine.worktree import get_active_worktree_checkout
            _wt_path = get_active_worktree_checkout()
            _wt = cwd if (_wt_path is not None and cwd == _wt_path) else None
            blocked = blocked_agent_stream_line(line, worktree_cwd_path=_wt)
            if blocked is None:
                return
            if blocked.guardrail == GUARDRAIL_UNSAFE_PROCESS_POLLING:
                # Diagnostic-only risk flag: warn and keep streaming. Unsafe
                # free-text process polling never aborts the call and never
                # kills the subprocess.
                _events.emit(
                    "agent.guardrail",
                    agent="codex",
                    label="invoke",
                    guardrail=GUARDRAIL_UNSAFE_PROCESS_POLLING,
                    action="warn",
                    command=blocked.command,
                    reason=blocked.reason,
                )
                return
            _events.emit(
                "agent.guardrail",
                agent="codex",
                label="invoke",
                guardrail="destructive_git",
                action="abort",
                command=blocked.command,
                reason=blocked.reason,
            )
            raise StreamAbort(
                f"{ORCHO_GUARDRAIL_BLOCKED}: {blocked.reason}: "
                f"{blocked.command}"
            )

        from agents.runtimes import _failures
        from agents.stream_parsers import format_codex_line_for_stdout
        from core.io.output_elision import (
            elide_text_for_model,
            elide_tool_result_line_for_model,
        )
        from pipeline.sandbox import get_active_sandbox_policy

        _sandbox_policy = get_active_sandbox_policy()
        try:
            def _attempt() -> tuple[str, str, str]:
                # Each attempt is a separate subprocess → its own start→end
                # event pair with a fresh ``agent_call_id`` (a retry must not
                # reuse the prior id, which would pair one start with N ends).
                attempt_call_id = f"call_{_uuid.uuid4().hex[:16]}"
                self._last_call_id = attempt_call_id
                _events.emit(
                    "agent.start", agent="codex", model=self.model,
                    label="invoke", mutates_artifacts=mutates_artifacts, cwd=cwd,
                    continue_session=bool(continue_session),
                    resumed_session_id=resumed_session_id,
                    agent_call_id=attempt_call_id,
                )
                with active_registered_skill_names(skill_names):
                    stdout, returncode, stderr, duration = _agents._stream_run(
                        cmd, cwd=safe, timeout=config.agent_timeout(_PROVIDER),
                        idle_timeout=config.agent_idle_timeout(_PROVIDER),
                        label=label,
                        on_line=_on_line,
                        stdout_filter=format_codex_line_for_stdout,
                        log_filter=format_codex_line_for_stdout,
                        return_filter=elide_tool_result_line_for_model,
                        sandbox_policy=_sandbox_policy,
                        stall_sink=EventStallDiagnosticSink(),
                        stall_phase=_events.current_phase() or "",
                        owned_child_owner=self._owned_children,
                        agent_call_id=attempt_call_id,
                    )
                stderr = elide_text_for_model(stderr)
                new_sid = _extract_codex_session_id(stdout)
                if new_sid:
                    self.session_id = new_sid
                _events.emit(
                    "agent.end", agent="codex",
                    return_code=returncode, duration=round(duration, 2),
                    captured_session_id=self.session_id,
                    agent_call_id=attempt_call_id,
                )
                # Guardrail block is an intentional stop, not a failure.
                if returncode != 0 and ORCHO_GUARDRAIL_BLOCKED in (stderr or ""):
                    self._capture_tokens(stdout, stderr)
                    return ("guardrail", stdout, stderr)
                if returncode != 0 and stderr:
                    print(f"  ! codex stderr: {stderr[:300]}")
                # Translate API-client failures (auth, connection, rate limit,
                # any non-zero exit) into typed errors so the run halts with a
                # clear cause. Transient transport shapes retry first.
                # ``reply_text`` is the answer we'd return — the exit-0
                # transport check scans only this, not stderr log noise (Codex
                # routinely logs "failed to record rollout" on a clean exit).
                reply_text = (
                    _read_codex_last_message(last_message_path)
                    or _extract_codex_assistant_text(stdout)
                )
                _failures.raise_on_runtime_failure(
                    runtime="codex", model=self.model, cli=self.bin,
                    returncode=returncode, stdout=stdout, stderr=stderr,
                    reply_text=reply_text,
                )
                self._capture_tokens(stdout, stderr)
                return ("ok", reply_text, stderr)

            tag, reply_text, stderr = _failures.run_invoke_with_retry(
                _attempt, runtime="codex", owned_children=self._owned_children,
            )
            if tag == "guardrail":
                return f"{ORCHO_GUARDRAIL_BLOCKED}\n{stderr}"
            self._capture_brain_telemetry()
            return reply_text
        finally:
            _cleanup_codex_last_message(last_message_path)

    def reset_session(self) -> None:
        """Drop the captured session id. The next ``invoke()`` starts fresh."""
        self.session_id = None
        self._followup_resume_pending = False
        self._last_resumed_session_id = None
        self._last_followup_parent_session_id = None
        self._reset_brain_telemetry()

    def probe_identity(self) -> "RuntimeIdentity":
        """No safe account-identity surface on the current Codex CLI.

        Falsifier (evaluated against Codex CLI ``login`` subcommands): the only
        non-interactive status surface is ``codex login status``, which reports
        the *auth method* (e.g. "Logged in using ChatGPT") on stderr and emits
        no user-facing account label, organization, or email. An auth method
        alone cannot disambiguate which account / quota bucket a run uses — the
        whole point of this diagnostic — so there is nothing safe to surface.

        Rather than fire a subprocess that yields no account signal, return an
        unavailable identity immediately. If a future Codex CLI exposes a
        stable account field in its status surface, parse it here the way
        :meth:`ClaudeAgent.probe_identity` parses ``claude auth status``.
        """
        from agents.runtimes.identity import RuntimeIdentity
        return RuntimeIdentity.unavailable("codex", "no_account_surface")


def _codex_last_message_path() -> Path:
    with tempfile.NamedTemporaryFile(
        prefix="orcho_codex_last_", suffix=".txt", delete=False,
    ) as tmp:
        return Path(tmp.name)


def _read_codex_last_message(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cleanup_codex_last_message(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def _sum_codex_usage_tokens(usage: dict) -> int:
    """Aggregate token total = ``input_tokens + output_tokens``.

    ``cached_input_tokens`` is a subset breakdown of ``input_tokens`` and
    ``reasoning_output_tokens`` is a subset breakdown of ``output_tokens``
    under the OpenAI Responses usage shape Codex follows. Summing all four
    would double-count. The breakdown fields stay on the ``agent.summary``
    event so a future cost calculator can apply the cached-input rate and
    any reasoning-output accounting separately.
    """
    total = 0
    for key in ("input_tokens", "output_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total
