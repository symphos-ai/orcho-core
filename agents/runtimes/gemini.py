"""
agents/runtimes/gemini.py — GeminiAgent (thin wrapper around the
``@google/gemini-cli`` Node CLI).

Implements :class:`IAgentRuntime`. Each invocation runs through
``gemini -p <prompt> -m <model> -o stream-json --skip-trust
--approval-mode <plan|yolo>`` so the CLI emits one JSON event per line.
The runtime captures ``init.session_id`` from the stream and resumes
later calls with ``-r <session_id>``.

Approval-mode mapping mirrors the read/write split in
:class:`agents.runtimes.claude.ClaudeAgent`:

* ``mutates_artifacts=False`` → ``--approval-mode plan`` — Gemini's
  read-only mode. Tool calls are restricted to non-mutating tools
  (``read_file``, ``glob``, etc.), so reviewer phases can still inspect
  the working tree without the CLI prompting for permission.
* ``mutates_artifacts=True``  → ``--approval-mode yolo`` — equivalent
  to Claude's ``--dangerously-skip-permissions``. Auto-approves every
  tool the model invokes. Destructive git operations are caught by the
  streaming guardrail (``blocked_agent_stream_line``); the approval
  bypass is necessary for non-interactive operation.

``--skip-trust`` is set on every call so the CLI does not prompt for
the workspace trust dialog. The Orcho worktree / sandbox layer is the
authoritative trust surface.

Looks up ``_stream_run`` dynamically through the ``agents`` package
namespace so tests that patch ``agents._stream_run`` take effect at
call time — same convention as Claude and Codex.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from agents.command_guard import (
    GUARDRAIL_UNSAFE_PROCESS_POLLING,
    ORCHO_GUARDRAIL_BLOCKED,
    blocked_agent_stream_line,
)
from agents.stall_protocol import EventStallDiagnosticSink
from agents.stream import StreamAbort
from core.infra import config
from core.infra.config import _wrap_windows_cmd
from core.infra.lazy import LazyValue, lazy_cli_binary
from pipeline.runtime.roles import AttachmentKind

if TYPE_CHECKING:
    from pipeline.runtime.steps import Attachment

_PROVIDER = "gemini"


# Pre-compiled regex for the textual session-id form Gemini occasionally
# leaks (e.g. inside error banners). Stream-json parsing is the primary
# path; this is a fallback for malformed lines.
_SESSION_ID_RE = re.compile(r'"session_id"\s*:\s*"([^"]+)"')

# Flag value names — kept as constants so tests can refer to them.
APPROVAL_MODE_READ = "plan"
APPROVAL_MODE_WRITE = "yolo"


def _extract_session_id(stdout: str) -> str | None:
    """Pull the Gemini session id out of stream-json output.

    The CLI emits one JSON object per line; the canonical source is the
    first ``init`` event but later events occasionally repeat the id.
    Returns ``None`` when no id is found — callers preserve the
    previously captured id rather than blanking it.
    """
    if not stdout:
        return None
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            sid = obj.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
    m = _SESSION_ID_RE.search(stdout)
    return m.group(1) if m else None


def _extract_assistant_text(stdout: str) -> str:
    """Return the concatenated assistant text from a Gemini stream.

    The CLI emits each chunk as ``{"type":"message","role":"assistant",
    "content":"...","delta":true}``. We join every assistant chunk so a
    multi-message reply (e.g. tool-mediated review) lands as one block
    in the phase result. Returns empty string when the call produced no
    assistant text — callers fall back to raw stdout.
    """
    if not stdout:
        return ""
    chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") != "message" or d.get("role") != "assistant":
            continue
        content = d.get("content")
        if isinstance(content, str) and content:
            chunks.append(content)
    return "".join(chunks)


def _extract_last_result(stdout: str) -> dict | None:
    """Find the final ``{"type":"result",...}`` line emitted by Gemini's
    stream-json. Walks the buffer in reverse so we touch only the tail
    of a long call. Returns ``None`` when no result line was found
    (call killed mid-stream, output wasn't stream-json, etc.).
    """
    if not stdout:
        return None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict) and d.get("type") == "result":
            return d
    return None


def _safe_int(value: object) -> int:
    """Best-effort integer coercion for stats fields."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _count_gemini_tool_uses(stdout: str) -> int:
    """Count ``tool_use`` events in Gemini stream-json stdout."""
    total = 0
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type") == "tool_use":
            total += 1
    return total


def _capture_usage(agent: GeminiAgent, stdout: str) -> None:
    """Stash per-call token counts on the runtime instance.

    Gemini's ``result.stats`` shape:

        {
          "total_tokens": int,   # full input + output rollup
          "input_tokens": int,   # input incl. cached
          "output_tokens": int,
          "cached": int,         # subset of input_tokens (cache hits)
          "input": int,          # fresh-input proxy (input - cached)
          "duration_ms": int,
          "tool_calls": int,
        }

    ``cached`` is a subset of ``input_tokens`` (not additive). We expose
    ``last_tokens_in`` as the full input scope (cached + fresh), with
    the per-bucket breakdown alongside, mirroring ``ClaudeAgent``'s
    interface so downstream metrics consumers don't have to branch on
    runtime id.

    Cost is left ``None`` — the Gemini CLI does not report a USD figure;
    cost computation lives in the rate-card layer downstream.
    """
    agent.last_tool_use_count = _count_gemini_tool_uses(stdout)
    result = _extract_last_result(stdout)
    if result is None:
        agent.last_cost_usd = None
        agent.last_tokens_in = None
        agent.last_tokens_out = None
        agent.last_tokens_in_fresh = None
        agent.last_tokens_in_cache_read = None
        agent.last_tokens_total = None
        return
    stats = result.get("stats") or {}
    if not isinstance(stats, Mapping):
        stats = {}
    input_tokens = _safe_int(stats.get("input_tokens"))
    cached = _safe_int(stats.get("cached"))
    output_tokens = _safe_int(stats.get("output_tokens"))
    # ``cached`` is a subset of ``input_tokens``. ``max(0, ...)`` clamps the
    # degenerate ``cached > input_tokens`` case to zero fresh input rather
    # than overstating it — do not reintroduce an ``else input_tokens`` branch.
    fresh = max(0, input_tokens - cached)
    agent.last_tokens_in = input_tokens
    agent.last_tokens_in_fresh = fresh
    agent.last_tokens_in_cache_read = cached
    agent.last_tokens_out = output_tokens
    agent.last_tokens_total = (
        _safe_int(stats.get("total_tokens")) or (input_tokens + output_tokens)
    )
    agent.last_cost_usd = None


def _reject_text_attachments(attachments: tuple[Attachment, ...]) -> None:
    """Defensive contract: TEXT attachments are rendered outside the runtime.

    Mirrors the matching helper in :mod:`agents.runtimes.claude`. Gemini
    CLI 0.40 does not consume multimodal attachments via flags, so
    non-TEXT attachments are accepted silently for now — they'll be
    wired up when the CLI exposes a multimodal surface.
    """
    bad = [a for a in attachments if a.kind is AttachmentKind.TEXT]
    if bad:
        names = ", ".join(repr(a.name) for a in bad)
        raise ValueError(
            f"IAgentRuntime.invoke(attachments=...) received TEXT attachments "
            f"({names}). TEXT must be rendered into the prompt outside the "
            f"runtime via render_text_block(); only IMAGE/BINARY may be passed."
        )


# Kept for backwards-compat with any external probe that read the
# module-level flag (the stub used to expose ``GEMINI_AVAILABLE = False``).
# The runtime is now a real implementation — registry availability is
# governed by entry-point discovery + the lazy CLI binary lookup.
GEMINI_AVAILABLE: bool = True


class GeminiAgent:
    """Runs the ``@google/gemini-cli`` Node CLI in non-interactive mode."""

    # Identifies the runtime backend at the instance surface so callers
    # (e.g. the Pipeline-block ``[Gemini]`` chip) can recognise the agent
    # without sniffing the class name.
    runtime: str = "gemini"

    def __init__(self, model: str = "", *, effort: str | None = None):
        self._bin: LazyValue[str] = lazy_cli_binary(
            "gemini", config.get_gemini_bin,
        )
        self.model = model or config.phase_model("plan", "gemini-2.5-pro")
        # Gemini CLI 0.40 has no public reasoning-effort flag; the
        # value is accepted for API parity (and surfaced in stdout
        # banners) but does not currently change the CLI invocation.
        self.effort = effort
        self.session_id: str | None = None
        self._followup_resume_pending: bool = False
        self._last_continue_session: bool = False
        self._last_resumed_session_id: str | None = None
        self._last_followup_parent_session_id: str | None = None
        # Populated by every call from the final ``result.stats`` line of
        # Gemini's stream-json. ``last_cost_usd`` stays ``None`` because
        # the CLI does not report a USD figure — cost lands via the
        # downstream rate-card lookup.
        self.last_cost_usd: float | None = None
        self.last_tokens_in: int | None = None
        self.last_tokens_in_fresh: int | None = None
        self.last_tokens_in_cache_read: int | None = None
        self.last_tokens_out: int | None = None
        self.last_tokens_total: int | None = None
        self.last_tool_use_count: int = 0

    @property
    def bin(self) -> str:
        return self._bin.get()

    @bin.setter
    def bin(self, value: str) -> None:
        self._bin.set(value)

    def _effort_label(self) -> str:
        """Suffix for human-readable labels: `` effort=high`` or empty.

        Surfaces the per-call reasoning effort in stdout so an operator
        can see what budget each phase ran with. The CLI itself does
        not consume the value as of 0.40.
        """
        return f" effort={self.effort}" if self.effort else ""

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple[Attachment, ...] = (),
    ) -> str:
        """Run Gemini CLI with the given prompt. See :class:`IAgentRuntime`."""
        _reject_text_attachments(attachments)
        if self._followup_resume_pending and self.session_id:
            continue_session = True
            self._last_followup_parent_session_id = self.session_id
            self._followup_resume_pending = False
        else:
            self._last_followup_parent_session_id = None

        import agents as _agents  # late import for monkey-patched _stream_run

        approval_mode = (
            APPROVAL_MODE_WRITE if mutates_artifacts else APPROVAL_MODE_READ
        )
        cmd: list[str] = [
            *_wrap_windows_cmd(self.bin),
            "-p", prompt,
            "-m", self.model,
            "-o", "stream-json",
            "--skip-trust",
            "--approval-mode", approval_mode,
        ]
        resumed_session_id = (
            self.session_id if (continue_session and self.session_id) else None
        )
        if resumed_session_id:
            cmd += ["-r", resumed_session_id]
        self._last_resumed_session_id = resumed_session_id
        self._last_continue_session = resumed_session_id is not None

        from core.io.transcript import (
            render_agent_invocation,
            render_incoming_prompt,
        )
        from core.observability.logging import get_verbose
        from core.observability.prompt_trace import take_last_prompt_turn as _take_turn
        mode_label = "write" if mutates_artifacts else "read"
        _last_turn = _take_turn()  # single take, clears slot even if debug is off
        _trace_view = _last_turn.trace_view() if _last_turn is not None else None
        print(render_agent_invocation(
            runtime="gemini",
            model=self.model,
            effort=self.effort,
            prompt=prompt,
            trace_view=_trace_view,
            mode=mode_label,
            session_id=resumed_session_id,
            continue_session=continue_session,
            session_supported=True,
            cwd=cwd,
        ))
        if get_verbose():
            print(render_incoming_prompt(
                prompt, trace_view=_trace_view,
                model=self.model,
            ))
        label = (
            f"gemini -p --model {self.model}{self._effort_label()} "
            f"({mode_label})"
        )

        import uuid as _uuid

        from agents.stream_parsers import (
            format_gemini_line_for_stdout,
            parse_gemini_line,
        )
        from agents.stream_parsers.skill_registry import (
            active_registered_skill_names,
            discover_registered_skill_names,
        )
        from core.observability import events as _events
        skill_names = discover_registered_skill_names(cwd)

        def _on_line(line: str) -> None:
            parse_gemini_line(line, agent_label="invoke")
            # Relax destructive-git guard only when cwd matches the
            # validated orcho-managed worktree checkout path (ADR 0033).
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
                    agent="gemini",
                    label="invoke",
                    guardrail=GUARDRAIL_UNSAFE_PROCESS_POLLING,
                    action="warn",
                    command=blocked.command,
                    reason=blocked.reason,
                )
                return
            _events.emit(
                "agent.guardrail",
                agent="gemini",
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
        from core.io.output_elision import (
            elide_text_for_model,
            elide_tool_result_line_for_model,
        )
        from pipeline.sandbox import get_active_sandbox_policy
        _sandbox_policy = get_active_sandbox_policy()

        def _attempt() -> tuple[str, str, str]:
            # Each attempt is a separate subprocess → its own start→end event
            # pair with a fresh ``agent_call_id`` (a retry must not reuse the
            # prior id). ``_last_call_id`` tracks the most recent attempt.
            attempt_call_id = f"call_{_uuid.uuid4().hex[:16]}"
            self._last_call_id = attempt_call_id
            _events.emit(
                "agent.start", agent="gemini", model=self.model,
                label="invoke", mutates_artifacts=mutates_artifacts, cwd=cwd,
                continue_session=bool(continue_session),
                resumed_session_id=resumed_session_id,
                agent_call_id=attempt_call_id,
            )
            with active_registered_skill_names(skill_names):
                stdout, returncode, stderr, duration = _agents._stream_run(
                    cmd, cwd=cwd, timeout=config.agent_timeout(_PROVIDER),
                    idle_timeout=config.agent_idle_timeout(_PROVIDER), label=label,
                    on_line=_on_line,
                    stdout_filter=format_gemini_line_for_stdout,
                    log_filter=format_gemini_line_for_stdout,
                    return_filter=elide_tool_result_line_for_model,
                    sandbox_policy=_sandbox_policy,
                    stall_sink=EventStallDiagnosticSink(),
                    stall_phase=_events.current_phase() or "",
                )
            stderr = elide_text_for_model(stderr)
            if returncode != 0 and stderr:
                print(f"  ! gemini stderr: {stderr[:300]}")

            new_sid = _extract_session_id(stdout)
            if new_sid:
                self.session_id = new_sid
            _capture_usage(self, stdout)
            _events.emit(
                "agent.end", agent="gemini",
                return_code=returncode, duration=round(duration, 2),
                captured_session_id=self.session_id,
                agent_call_id=attempt_call_id,
            )
            # Guardrail block is an intentional stop, not a failure.
            if ORCHO_GUARDRAIL_BLOCKED in (stderr or ""):
                return ("guardrail", stdout, stderr)
            # Translate API-client failures into typed errors so the run halts
            # with a clear cause instead of returning the error text as output.
            # ``reply_text`` is the answer we'd return — the exit-0 transport
            # check scans only this, not stderr log noise.
            reply_text = _extract_assistant_text(stdout) or stdout
            _failures.raise_on_runtime_failure(
                runtime="gemini", model=self.model, cli=self.bin,
                returncode=returncode, stdout=stdout, stderr=stderr,
                reply_text=reply_text,
            )
            return ("ok", reply_text, stderr)

        tag, reply_text, stderr = _failures.run_invoke_with_retry(
            _attempt, runtime="gemini",
        )
        if tag == "guardrail":
            return f"{ORCHO_GUARDRAIL_BLOCKED}\n{stderr}"
        return reply_text

    def reset_session(self) -> None:
        """Drop the captured session id. The next ``invoke()`` starts fresh."""
        self.session_id = None
        self._followup_resume_pending = False
        self._last_resumed_session_id = None
        self._last_followup_parent_session_id = None
