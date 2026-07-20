"""
agents/runtimes/claude.py — ClaudeAgent (thin wrapper around the Claude Code CLI).

Implements :class:`IAgentRuntime` — a single ``invoke()`` entry point that
receives a fully-composed prompt and a few per-call flags. Phase 7 collapsed
the historical ``plan()`` / ``run()`` / ``hypothesize()`` / ``review_*()``
methods into this one; prompt building moved out to ``pipeline.prompts``
builders and lives in the caller.

Universal stream-json capture: every invocation uses
``--output-format stream-json --verbose`` so the session id lands in stdout
and the runtime can ``--resume`` later regardless of whether the previous
call mutated artifacts. ``--verbose`` is required by Claude Code to emit
stream-json from a non-interactive ``--print`` invocation.

``mutates_artifacts=True`` adds the Claude write flags
``--permission-mode acceptEdits`` + ``--dangerously-skip-permissions``. Default
``False`` runs without them — Claude has filesystem read access via its tools
but cannot mutate the repo.

``continue_session=True`` with a previously captured ``self.session_id`` adds
``--resume <session_id>`` so the call inherits the bridge's accumulated
context. When ``session_id`` is ``None`` the flag is silently ignored — the
call runs fresh and seeds ``self.session_id`` for the next one.

Looks up ``_stream_run`` dynamically through the ``agents`` package namespace
so tests that patch ``agents._stream_run`` (or the underlying
``agents.stream._stream_run``) take effect at call time.
"""

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from agents.command_guard import (
    GUARDRAIL_UNSAFE_PROCESS_POLLING,
    ORCHO_GUARDRAIL_BLOCKED,
    blocked_agent_stream_line,
)
from agents.owned_child import OwnedChildRegistry
from agents.stall_protocol import EventStallDiagnosticSink
from agents.stream import StreamAbort
from core.infra import config
from core.infra.config import _wrap_windows_cmd
from core.infra.lazy import LazyValue, lazy_cli_binary
from pipeline.runtime.roles import AttachmentKind
from pipeline.runtime.steps import Attachment

if TYPE_CHECKING:
    from agents.runtimes.identity import RuntimeIdentity

_DEFAULT_RUNTIME = "claude"

# Account-identity probe (diagnostic only). ``claude auth status`` is the
# non-interactive, user-facing status surface the CLI already exposes; it
# prints a small JSON object describing the logged-in account. We read ONLY
# the sanitized, user-visible ``email`` / ``orgName`` fields and discard
# everything else — no tokens, org ids, auth-file paths, or raw JSON are kept.
# A short timeout keeps the probe non-disruptive; any failure → unavailable.
_IDENTITY_PROBE_TIMEOUT_S = 3
#: Sanitized fields read from ``claude auth status``. Deliberately excludes
#: ``orgId``, ``authMethod``, ``subscriptionType``, and anything token-shaped.
_IDENTITY_STATUS_ALLOWLIST = ("email", "orgName")


def _run_identity_status(cmd: list[str]) -> str | None:
    """Run a read-only status command and return stdout, or ``None`` on any
    failure. Reached through the ``agents`` namespace so tests monkeypatch
    ``agents.subprocess.run``. Never raises — the probe is best-effort.
    """
    import agents as _agents  # late import: pick up monkeypatched subprocess
    try:
        result = _agents.subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_IDENTITY_PROBE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — timeout, missing binary, OS error → unavailable
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    return getattr(result, "stdout", None) or None


def _parse_claude_identity(
    stdout: str,
    *,
    runtime: str = _DEFAULT_RUNTIME,
    provider: str = "anthropic",
) -> "RuntimeIdentity":
    """Extract the sanitized account label / email from ``claude auth status``
    JSON. Returns an unavailable identity when the output is not parseable or
    carries no user-facing account fields."""
    from agents.runtimes.identity import RuntimeIdentity
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return RuntimeIdentity.unavailable(runtime, "unparsable_status")
    if not isinstance(data, dict):
        return RuntimeIdentity.unavailable(runtime, "unparsable_status")
    # Copy ONLY the allowlisted, user-facing fields out of the status blob.
    # Everything else (org id, auth method, subscription type, anything
    # token-shaped) is dropped here and never reaches the value object.
    safe = {
        key: data[key]
        for key in _IDENTITY_STATUS_ALLOWLIST
        if isinstance(data.get(key), str) and data[key].strip()
    }
    email = safe["email"].strip() if "email" in safe else None
    label = safe["orgName"].strip() if "orgName" in safe else None
    if not email and not label:
        return RuntimeIdentity.unavailable(runtime, "no_account_in_status")
    return RuntimeIdentity(
        runtime=runtime,
        source="runtime_status",
        available=True,
        provider=provider,
        account_label=label,
        email=email,
    )


# Pre-compiled regex for the legacy ``session_id: "..."`` text format Claude
# Code occasionally emits when stream-json parsing isn't possible.
_SESSION_ID_RE = re.compile(r'"session_id"\s*:\s*"([^"]+)"')


def _extract_assistant_text(stdout: str) -> str:
    """Return the final assistant text emitted by Claude Code stream-json.

    Claude's stream-json is a JSONL of mixed event types (system / assistant /
    tool_use / result). Only ``assistant`` events with ``type=text`` content
    blocks carry the natural-language reply the user actually wants to read;
    everything else is plumbing. Without this, ``preview()`` of the raw
    stdout shows the first 300 chars of ``{"type":"system","subtype":"init"}``
    — the init banner — and the actual plan/build/fix output stays buried.

    Returns the final assistant message's joined text blocks. Earlier assistant
    messages may be progress chatter before tool calls; using the last message
    mirrors ``--output-last-message`` semantics and keeps phase summaries from
    showing the first progress note instead of the final handoff. When the
    input has no extractable assistant text (call killed mid-stream, output
    wasn't stream-json, etc.) returns an empty string — callers can fall back
    to the raw blob.
    """
    if not stdout:
        return ""
    last_chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        chunks: list[str] = []
        for c in msg.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "text":
                txt = c.get("text") or ""
                if txt:
                    chunks.append(txt)
        if chunks:
            last_chunks = chunks
    return "\n".join(last_chunks)


def _extract_last_result(stdout: str) -> dict | None:
    """Find the final ``{"type":"result",…}`` JSONL line emitted by Claude
    Code's ``--output-format stream-json``. Walks the buffer in reverse
    so we touch only the tail when the call was long. Returns ``None``
    if no result line was found (call was killed mid-stream, output
    wasn't stream-json, etc.) — callers must accept that.
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
    """Best-effort integer coercion for provider usage counters."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_input_scope(usage: Mapping[str, object] | None) -> int:
    """Return fresh + cache-create + cache-read input tokens."""
    if not isinstance(usage, Mapping):
        return 0
    return (
        _safe_int(usage.get("input_tokens"))
        + _safe_int(usage.get("cache_creation_input_tokens"))
        + _safe_int(usage.get("cache_read_input_tokens"))
    )


def _assistant_message_input_scopes(stdout: str) -> list[int]:
    """Per-assistant-message input scopes from Claude stream-json.

    Claude's final ``result.usage`` is an aggregate for the whole CLI
    invocation. That value is correct for provider usage/cost, but it is not
    the live context-window fill. The UI-style context counter is the input
    scope on each assistant message; the last such value is the current
    context and the max is the peak reached during the invocation.
    """
    scopes: list[int] = []
    if not stdout:
        return scopes
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            continue
        scope = _usage_input_scope(usage)
        if scope > 0:
            scopes.append(scope)
    return scopes


def _normalize_model_name(name: str) -> str:
    """Strip Claude's variant suffixes (``[1m]``, ``[200k]``, etc.) so
    a configured model name like ``claude-opus-4-8`` matches the
    ``modelUsage`` key ``claude-opus-4-8[1m]`` that the CLI emits."""
    return re.sub(r"\[[^\]]*\]$", "", name or "").strip().lower()


def _pick_primary_model_usage(
    model_usage: dict, configured_model: str,
) -> dict | None:
    """Pick the ``modelUsage`` entry that corresponds to the call's
    primary reasoning model.

    Claude Code's ``result.modelUsage`` is a dict keyed by full model
    id (e.g. ``claude-opus-4-8[1m]`` for the 1M-context variant,
    ``claude-haiku-4-5-20251001`` for the background helper). Most
    calls show two entries: the configured/invoked model plus a
    smaller background model.

    Selection rules, in order:

    1. Prefix match against ``configured_model`` (after stripping
       variant suffixes from both sides). Most direct mapping.
    2. Fallback: pick the entry with the largest total input
       contribution (``inputTokens + cacheReadInputTokens +
       cacheCreationInputTokens``). The "model that did the heavy
       lifting" — proxy for the primary model when the configured
       name and the CLI key don't share a clean prefix.

    Returns the chosen entry dict, or ``None`` when the input is
    empty/malformed.
    """
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    cfg = _normalize_model_name(configured_model)
    if cfg:
        for key, entry in model_usage.items():
            if not isinstance(entry, dict):
                continue
            if _normalize_model_name(str(key)).startswith(cfg):
                return entry
    # Fallback: largest total input contribution.
    def _total_input(entry: object) -> int:
        if not isinstance(entry, dict):
            return -1
        return (
            int(entry.get("inputTokens") or 0)
            + int(entry.get("cacheReadInputTokens") or 0)
            + int(entry.get("cacheCreationInputTokens") or 0)
        )
    best_key = max(model_usage, key=lambda k: _total_input(model_usage.get(k)))
    chosen = model_usage.get(best_key)
    return chosen if isinstance(chosen, dict) else None


def _count_claude_tool_uses(stdout: str) -> int:
    """Count tool-use blocks in Claude stream-json stdout."""
    total = 0
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        total += sum(
            1 for item in content
            if isinstance(item, dict) and item.get("type") == "tool_use"
        )
    return total


def _capture_usage(agent: "ClaudeAgent", stdout: str) -> None:
    """Pull ``total_cost_usd`` + exact input/output token counts from the
    result line and stash on the agent for the orchestrator to read.
    Idempotent — overwrites every call so the *last* invocation wins
    (matches the per-phase contract).

    Claude Code's stream-json ``usage`` object splits input into three
    buckets:

      * ``input_tokens``                — fresh (uncached) prompt tokens
      * ``cache_creation_input_tokens`` — written into cache this turn
      * ``cache_read_input_tokens``     — read from cache this turn

    A long prompt that's mostly cached (typical for resumed sessions or
    multi-turn calls) shows tiny ``input_tokens`` and huge
    ``cache_read_input_tokens`` — the visible delta vs. the actual
    input scope. The user-facing ``tokens_in`` total must reflect the
    full input volume (all three buckets summed), otherwise a ~$0.10
    cross-plan call shows ``in=9`` and looks wrong.

    The cache_read tokens are billed at ~10% rate and cache_create at
    ~1.25×, but cost is already authoritative via ``total_cost_usd``
    (Claude computes it server-side). We don't need to re-derive it.

    The per-bucket breakdown is stashed on the agent for evidence
    consumers that want to surface cache hit-rate or similar.

    Runtime-reported context fullness is separate from that aggregate.
    The result envelope carries ``modelUsage[<model>].contextWindow`` and
    assistant events carry per-message ``usage``. The last assistant-message
    input scope is the current live context-window fill; the max is the peak.
    When either piece is missing, context fields stay ``None`` so the resolver
    can fall back instead of presenting aggregate provider usage as context.
    """
    agent.last_tool_use_count = _count_claude_tool_uses(stdout)
    result = _extract_last_result(stdout)
    if result is None:
        agent.last_cost_usd = None
        agent.last_tokens_in = None
        agent.last_tokens_out = None
        agent.last_tokens_in_fresh = None
        agent.last_tokens_in_cache_create = None
        agent.last_tokens_in_cache_read = None
        agent.last_context_window_tokens = None
        agent.last_context_used_tokens = None
        agent.last_context_peak_tokens = None
        return
    usage = result.get("usage") or {}
    agent.last_cost_usd = result.get("total_cost_usd")
    fresh = _safe_int(usage.get("input_tokens"))
    cache_create = _safe_int(usage.get("cache_creation_input_tokens"))
    cache_read = _safe_int(usage.get("cache_read_input_tokens"))
    agent.last_tokens_in_fresh = fresh
    agent.last_tokens_in_cache_create = cache_create
    agent.last_tokens_in_cache_read = cache_read
    agent.last_tokens_in = fresh + cache_create + cache_read
    agent.last_tokens_out = usage.get("output_tokens")

    # M14.4.1 runtime-reported context fullness.
    primary = _pick_primary_model_usage(
        result.get("modelUsage") or {}, agent.model,
    )
    window = None
    if primary is not None:
        raw_window = primary.get("contextWindow")
        if isinstance(raw_window, int) and raw_window > 0:
            window = raw_window
    agent.last_context_window_tokens = window
    context_scopes = _assistant_message_input_scopes(stdout)
    if window is not None and context_scopes:
        agent.last_context_used_tokens = context_scopes[-1]
        agent.last_context_peak_tokens = max(context_scopes)
    else:
        agent.last_context_used_tokens = None
        agent.last_context_peak_tokens = None


def _extract_session_id(stdout: str) -> str | None:
    """Pull the Claude session id out of stream-json output.

    Claude Code's ``--output-format stream-json`` emits one JSON object per
    line. Most lines carry ``"session_id": "<uuid>"`` (the ``init`` event is
    the canonical source, but later events repeat it). We scan for the first
    occurrence and return it. Returns ``None`` if no session id is found —
    callers should preserve any previously captured id rather than blanking it.

    Tolerates non-JSON noise interleaved with JSON lines (the CLI sometimes
    prints banners before the first event).
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

    # Fallback: substring match in case the line wasn't valid JSON on its own
    # (e.g. wrapped in ANSI colour codes).
    m = _SESSION_ID_RE.search(stdout)
    return m.group(1) if m else None


def _reject_text_attachments(attachments: tuple[Attachment, ...]) -> None:
    """Defensive contract: TEXT attachments are rendered outside the runtime.

    ``invoke(attachments=...)`` only accepts multimodal kinds (IMAGE / BINARY).
    A TEXT entry here is a caller bug — it means TEXT will be double-injected
    (once by ``builtin._plan_prompt_prefix`` via ``render_text_block`` and
    again, attempted, by the runtime). Fail fast instead of silently doubling.
    """
    bad = [a for a in attachments if a.kind is AttachmentKind.TEXT]
    if bad:
        names = ", ".join(repr(a.name) for a in bad)
        raise ValueError(
            f"IAgentRuntime.invoke(attachments=...) received TEXT attachments "
            f"({names}). TEXT must be rendered into the prompt outside the "
            f"runtime via render_text_block(); only IMAGE/BINARY may be passed."
        )


class ClaudeAgent:
    """Runs Claude Code CLI in non-interactive print mode."""

    # Identifies the runtime backend at the instance surface so callers
    # (e.g. the Pipeline-block ``[Claude]`` chip) can recognise the
    # agent without sniffing the class name. ``agents.registry.resolve``
    # writes the same value as an instance attribute, but anyone who
    # constructs the agent through ``RealAgentProvider.claude(...)``
    # (mono ``_synthesize_phase_config`` does) bypasses that path —
    # carrying the constant on the class makes the attribute available
    # either way.
    runtime: str = _DEFAULT_RUNTIME
    identity_provider: str = "anthropic"

    @staticmethod
    def _resolve_cli_binary() -> str:
        return config.get_claude_bin()

    def __init__(self, model: str = "", *, effort: str | None = None):
        self._bin: LazyValue[str] = lazy_cli_binary(
            self.runtime,
            self._resolve_cli_binary,
        )
        self.model = model or config.phase_model(
            "implement", "claude-opus-4-8[1m]",
        )
        self.effort = effort
        self._owned_children = OwnedChildRegistry()
        self.session_id: str | None = None
        self._followup_resume_pending: bool = False
        self._last_continue_session: bool = False
        self._last_resumed_session_id: str | None = None
        self._last_followup_parent_session_id: str | None = None
        # Populated by every call from the final ``{"type":"result",…}`` line
        # of Claude's stream-json. ``last_cost_usd`` is a cost reference
        # reported by the active runtime/endpoint, not a billing receipt.
        # ``last_tokens_in/out`` are the exact API-side counts —
        # estimate_tokens() is only used as a fallback when these are None.
        self.last_cost_usd: float | None = None
        # ``last_tokens_in`` is the **total** input scope this call —
        # fresh prompt tokens + cache-creation + cache-read — so a
        # heavily-cached resume shows the real volume the user is paying
        # for (cache_read is billed at ~10% rate but is still real input).
        # The per-bucket breakdown is exposed for cache hit-rate UIs.
        self.last_tokens_in: int | None = None
        self.last_tokens_in_fresh: int | None = None
        self.last_tokens_in_cache_create: int | None = None
        self.last_tokens_in_cache_read: int | None = None
        self.last_tokens_out: int | None = None
        # Runtime-reported context fullness. Window comes from
        # ``result.modelUsage[<model>].contextWindow``; used/peak come from
        # per-assistant-message usage in stream-json. The final result usage
        # is an aggregate for provider metrics/cost and must not be used as
        # the live context-window fill.
        self.last_context_window_tokens: int | None = None
        self.last_context_used_tokens: int | None = None
        self.last_context_peak_tokens: int | None = None
        self.last_tool_use_count: int = 0

    @property
    def bin(self) -> str:
        return self._bin.get()

    @bin.setter
    def bin(self, value: str) -> None:
        self._bin.set(value)

    def _effort_args(self) -> list[str]:
        """Claude Code accepts ``--effort low|medium|high|xhigh|max`` to
        control reasoning depth. Returns the flag pair when set, else [].
        """
        return ["--effort", self.effort] if self.effort else []

    def _effort_label(self) -> str:
        """Suffix for human-readable labels: `` --effort high`` or empty."""
        return f" --effort {self.effort}" if self.effort else ""

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple[Attachment, ...] = (),
    ) -> str:
        """Run Claude Code with the given prompt. See :class:`IAgentRuntime`."""
        _reject_text_attachments(attachments)
        if self._followup_resume_pending and self.session_id:
            continue_session = True
            self._last_followup_parent_session_id = self.session_id
            self._followup_resume_pending = False
        else:
            self._last_followup_parent_session_id = None

        import agents as _agents  # late import: pick up monkeypatched _stream_run

        cmd: list[str] = [
            *_wrap_windows_cmd(self.bin),
            "--print",
        ]
        if mutates_artifacts:
            # Claude write path: pre-accept edits and skip per-tool permission
            # prompts so the agent can mutate the repo non-interactively.
            cmd += ["--permission-mode", "acceptEdits",
                    "--dangerously-skip-permissions"]
        cmd += [
            "--model", self.model,
            *self._effort_args(),
            "--output-format", "stream-json",
            "--verbose",
        ]
        if continue_session and self.session_id:
            cmd += ["--resume", self.session_id]
        cmd += [prompt]

        from core.io.transcript import render_agent_invocation, render_incoming_prompt
        from core.observability.logging import get_verbose
        from core.observability.prompt_trace import take_last_prompt_turn as _take_turn
        # ``resumed_session_id`` is the id we actually pass to --resume,
        # not the one stored on ``self`` (which is captured fresh each
        # call). Surface the full id so a reviewer grepping output.log
        # against checkpoints.db can match without truncation.
        resumed_session_id = (
            self.session_id if (continue_session and self.session_id) else None
        )
        self._last_resumed_session_id = resumed_session_id
        # ``_last_continue_session`` mirrors the "actual resume happened"
        # truth, not the caller's requested flag. Phase meta derives the
        # persisted ``continue_session`` field from this so reset / burn-
        # bridge flows can't surface a misleading ``true``.
        self._last_continue_session = resumed_session_id is not None
        mode_label = "write" if mutates_artifacts else "read"
        _last_turn = _take_turn()  # single take, clears slot even if debug is off
        _trace_view = _last_turn.trace_view() if _last_turn is not None else None
        print(render_agent_invocation(
            runtime=self.runtime,
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
        # Internal label for ``_stream_run`` observability — short, no
        # cwd repetition, kept stable across the UX-line redesign so
        # existing log greps don't break.
        label = (
            f"{self.runtime} --print --model {self.model}{self._effort_label()} "
            f"({mode_label})"
        )

        # Hand the JSONL parser to the streamer so each line lands in the
        # event-store as it's read from the child. This is what powers the
        # web dashboard's chip-log and the rewritten orcho-watch.
        # Phase 7.10: surface the bridge edge in the event stream so a
        # UI / decision-provenance graph can draw round-N→round-N-1
        # resume edges without parsing stdout. ``continue_session`` is
        # the policy decision the caller made; ``resumed_session_id``
        # is the actual id we passed to ``--resume`` (None when no
        # resume happens, even if continue_session was True).
        # ``agent_call_id`` is a per-invocation UUID linking agent.start
        # / agent.end / phase-log meta so DAG fan-outs and cross-project
        # sub-runs can match starts to ends without relying on order or
        # phase tag.
        import uuid as _uuid

        from agents.stream_parsers import (
            format_claude_line_for_stdout,
            parse_claude_line,
        )
        from agents.stream_parsers.skill_registry import (
            active_registered_skill_names,
            discover_registered_skill_names,
        )
        from core.observability import events as _events
        skill_names = discover_registered_skill_names(cwd)

        def _on_line(line: str) -> None:
            parse_claude_line(line, agent_label="invoke")
            # Guardrail is meaningful for write calls (where the agent can
            # actually execute destructive commands). We still run the check
            # on read calls — defense in depth, cost is negligible.
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
                # Diagnostic-only risk flag: emit a warn and keep streaming.
                # Unsafe free-text process polling never aborts the call and
                # never kills the subprocess (the stream monitor's scoped kill
                # in T3 remains gated on idle-timeout alone). The reason
                # category rides on the event for the T3 monitor/sink.
                _events.emit(
                    "agent.guardrail",
                    agent=self.runtime,
                    label="invoke",
                    guardrail=GUARDRAIL_UNSAFE_PROCESS_POLLING,
                    action="warn",
                    command=blocked.command,
                    reason=blocked.reason,
                )
                return
            _events.emit(
                "agent.guardrail",
                agent=self.runtime,
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
            # Each attempt is a separate subprocess, so it is its own
            # start→end event pair with a fresh ``agent_call_id``. A retry
            # must not reuse the previous attempt's id (that would pair one
            # start with N ends). ``_last_call_id`` tracks the most recent
            # attempt for callers that read it after invoke() returns.
            attempt_call_id = f"call_{_uuid.uuid4().hex[:16]}"
            self._last_call_id = attempt_call_id
            _events.emit(
                "agent.start", agent=self.runtime, model=self.model,
                label="invoke", mutates_artifacts=mutates_artifacts, cwd=cwd,
                continue_session=bool(continue_session),
                resumed_session_id=resumed_session_id,
                agent_call_id=attempt_call_id,
            )
            with active_registered_skill_names(skill_names):
                stdout, returncode, stderr, duration = _agents._stream_run(
                    cmd, cwd=cwd, timeout=config.agent_timeout(self.runtime),
                    idle_timeout=config.agent_idle_timeout(self.runtime), label=label,
                    on_line=_on_line,
                    stdout_filter=format_claude_line_for_stdout,
                    log_filter=format_claude_line_for_stdout,
                    return_filter=elide_tool_result_line_for_model,
                    sandbox_policy=_sandbox_policy,
                    stall_sink=EventStallDiagnosticSink(),
                    stall_phase=_events.current_phase() or "",
                    owned_child_owner=self._owned_children,
                    agent_call_id=attempt_call_id,
                )
            stderr = elide_text_for_model(stderr)
            if returncode != 0 and stderr:
                print(f"  ! {self.runtime} stderr: {stderr[:300]}")

            # Capture session_id for future --resume calls. Don't blank it on a
            # parse miss — a transient parsing failure shouldn't drop the bridge.
            new_sid = _extract_session_id(stdout)
            if new_sid:
                self.session_id = new_sid
            _capture_usage(self, stdout)
            # Phase 7.10: emit agent.end AFTER session capture so the event
            # payload carries the bridge state at the end of the call —
            # ``captured_session_id`` is the id the next ``--resume`` would
            # use, or unchanged if this call didn't yield a new one.
            # ``agent_call_id`` mirrors the start-side id so consumers can
            # pair events without relying on order.
            _events.emit(
                "agent.end", agent=self.runtime,
                return_code=returncode, duration=round(duration, 2),
                captured_session_id=self.session_id,
                agent_call_id=attempt_call_id,
            )
            # Guardrail block is an intentional stop, not a failure — surface
            # the sentinel text without classifying it as an API error.
            if ORCHO_GUARDRAIL_BLOCKED in (stderr or ""):
                return ("guardrail", stdout, stderr)
            # Translate API-client failures (auth, connection refused, stream
            # disconnect, rate limit, non-zero exit) into typed errors so the
            # run halts with a clear cause instead of returning the error text
            # as a normal response. Transient transport shapes retry first.
            # ``reply_text`` is the answer we'd return — the exit-0 transport
            # check scans only this, not stderr log noise.
            reply_text = _extract_assistant_text(stdout) or stdout
            _failures.raise_on_runtime_failure(
                runtime=self.runtime, model=self.model, cli=self.bin,
                returncode=returncode, stdout=stdout, stderr=stderr,
                reply_text=reply_text,
            )
            return ("ok", reply_text, stderr)

        tag, reply_text, stderr = _failures.run_invoke_with_retry(
            _attempt, runtime=self.runtime, owned_children=self._owned_children,
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

    def probe_identity(self) -> "RuntimeIdentity":
        """Best-effort, read-only account identity for this run (diagnostic).

        Runs ``claude auth status`` — the non-interactive surface the CLI
        already shows users — with a short timeout, and parses ONLY the
        sanitized ``email`` / ``orgName`` fields. Never reads auth files or
        tokens, never persists raw status JSON. Any failure (missing binary,
        timeout, non-zero exit, unparsable output, no account field) returns an
        unavailable identity; this method never raises and never blocks a run.

        Lazy: resolving ``self.bin`` is the first real binary use — fine here
        because the orchestrator only calls this during real run setup, never
        from the constructor, profile listing, or dry-run rendering.
        """
        from agents.runtimes.identity import RuntimeIdentity
        try:
            bin_path = self.bin
        except Exception:  # noqa: BLE001 — CLI not installed → unavailable, not an error
            return RuntimeIdentity.unavailable(self.runtime, "no_binary")
        cmd = [*_wrap_windows_cmd(bin_path), "auth", "status"]
        stdout = _run_identity_status(cmd)
        if not stdout:
            return RuntimeIdentity.unavailable(self.runtime, "no_status_surface")
        return _parse_claude_identity(
            stdout,
            runtime=self.runtime,
            provider=self.identity_provider,
        )
