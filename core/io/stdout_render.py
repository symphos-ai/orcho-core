"""core/io/stdout_render.py — scoped stdout rendering toggles.

Some phases (PLAN, reviewer phases) emit a typed JSON contract as the
agent's primary output. Streaming that JSON live to stdout dominates
the run transcript and forces the operator to read machine output —
exactly what the structured renderers in :mod:`core.io.transcript`
were built to avoid.

This module exposes a small thread-local toggle the orchestrator can
flip around an agent invocation. Stream filters check it and drop
assistant-text blocks that look like a JSON contract; tool-use lines
keep streaming live so the operator still sees what the agent did.
The full raw output continues to land in ``output.log`` and the agent
runtime's return value, so nothing is lost — the orchestrator picks up
the JSON, parses it, and renders the structured block deterministically
afterwards.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_state = threading.local()

_EMBEDDED_BRACE_RE = re.compile(r"\{")


def _get() -> bool:
    return bool(getattr(_state, "suppress_assistant_json", False))


def is_assistant_json_suppressed() -> bool:
    """Return True when the active phase asked for assistant-JSON
    blocks to be skipped from live stdout."""
    return _get()


def _reset_json_block_state() -> None:
    _state.assistant_json_depth = 0
    _state.assistant_json_fence = False
    _state.assistant_json_notice_emitted = False


def _begin_json_block() -> None:
    _state.assistant_json_notice_emitted = False


def classify_assistant_json_contract_chunk(text: str) -> tuple[bool, bool]:
    """Classify assistant text for event-stream contract markers.

    Returns ``(is_contract_chunk, is_first_chunk)`` using state separate
    from the stdout suppressor. Event parsers use this to emit a single
    ``agent.contract_ready`` marker and skip raw JSON ``agent.text``
    chunks, while still preserving prose ``agent.text`` events.
    """
    stripped = (text or "").lstrip()
    if not stripped:
        return False, False

    if bool(getattr(_state, "agent_contract_json_fence", False)):
        if "```" in stripped:
            _state.agent_contract_json_fence = False
        return True, False

    depth = int(getattr(_state, "agent_contract_json_depth", 0) or 0)
    if depth > 0:
        delta, _in_string = _json_depth_delta(text)
        _state.agent_contract_json_depth = max(0, depth + delta)
        return True, False

    if stripped.startswith("```"):
        first_line = stripped.splitlines()[0].lower()
        if "json" in first_line:
            rest = stripped[len(stripped.splitlines()[0]):]
            _state.agent_contract_json_fence = "```" not in rest
            return True, True

    if stripped.startswith(("{", "[")):
        delta, _in_string = _json_depth_delta(stripped)
        _state.agent_contract_json_depth = max(0, delta)
        return True, True

    return False, False


def consume_assistant_json_notice() -> str | None:
    """Return the one-line placeholder for a suppressed JSON answer.

    The JSON contract itself is rendered later from parsed data. This
    marker keeps the live transcript from looking like the assistant
    returned nothing while still avoiding raw contract duplication.
    """
    if bool(getattr(_state, "assistant_json_notice_emitted", False)):
        return None
    _state.assistant_json_notice_emitted = True
    return "Contracted answer prepared."


def _json_depth_delta(text: str) -> tuple[int, bool]:
    """Return bracket-depth delta for JSON-ish text.

    The scanner is intentionally tiny but string-aware, so braces inside
    quoted body text do not prematurely close a streamed JSON contract.
    """
    delta = 0
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            delta += 1
        elif ch in "}]":
            delta -= 1
    return delta, in_string


def should_suppress_assistant_text(text: str, *, drop_json: bool) -> bool:
    """Return True when a streamed assistant text chunk is JSON contract body.

    Provider streams can split pretty-printed JSON across multiple text
    chunks. A one-shot ``text.lstrip().startswith("{")`` only drops the
    opening chunk and lets ``"verdict": ...`` leak into the live transcript.
    This helper keeps a thread-local bracket/fence state so the whole JSON
    object/array is hidden until the closing brace/fence arrives.
    """
    if not drop_json:
        _reset_json_block_state()
        return False

    stripped = (text or "").lstrip()
    if not stripped:
        return False

    if bool(getattr(_state, "assistant_json_fence", False)):
        if "```" in stripped:
            _state.assistant_json_fence = False
        return True

    depth = int(getattr(_state, "assistant_json_depth", 0) or 0)
    if depth > 0:
        delta, _in_string = _json_depth_delta(text)
        _state.assistant_json_depth = max(0, depth + delta)
        return True

    if stripped.startswith("```"):
        first_line = stripped.splitlines()[0].lower()
        if "json" in first_line:
            _begin_json_block()
            rest = stripped[len(stripped.splitlines()[0]):]
            _state.assistant_json_fence = "```" not in rest
            return True

    if stripped.startswith(("{", "[")):
        _begin_json_block()
        delta, _in_string = _json_depth_delta(stripped)
        _state.assistant_json_depth = max(0, delta)
        return True

    return False


def _embedded_suffix_is_terminal(suffix: str) -> bool:
    """Return True when nothing meaningful follows an embedded JSON object.

    Whitespace and a trailing ```` ``` ```` fence close (when the contract
    sat inside a ``json`` fence) are tolerated; any remaining prose means
    the JSON is mid-text, not the agent's terminal contract, so it is left
    visible.
    """
    rest = suffix.strip()
    if not rest:
        return True
    return not rest.strip("`").strip()


def split_embedded_json_contract(text: str) -> tuple[str, bool]:
    """Split prose-then-contract assistant text into ``(prose, found)``.

    The leading-contract case (a block that *starts* with ``{`` / ``[`` /
    a ``json`` fence) is owned by :func:`should_suppress_assistant_text`
    and :func:`classify_assistant_json_contract_chunk`; this helper
    handles the *recovery* shape Orcho parses post-hoc — a human summary
    followed by a trailing JSON contract object, e.g. a ``## Summary``
    block trailed by a ``subtask_attestation`` payload.

    Detection mirrors :func:`pipeline.json_contract._recover_embedded_object`:
    scan left-to-right for a ``{`` whose content json-decodes to a mapping,
    accepting it only when nothing but whitespace (or a closing fence)
    follows — i.e. the contract is genuinely terminal. Returns the prose
    before the contract and ``True`` when one is found; otherwise the text
    unchanged and ``False``. Pure: no thread-local state is touched.
    """
    stripped = text.lstrip()
    if not stripped or stripped[0] in "{[" or stripped.startswith("```json"):
        return text, False

    decoder = json.JSONDecoder()
    for match in _EMBEDDED_BRACE_RE.finditer(text):
        start = match.start()
        try:
            decoded, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict) or not decoded:
            continue
        if not _embedded_suffix_is_terminal(text[start + end:]):
            continue
        # Drop a trailing ```json fence opener so the prose ends cleanly
        # before the suppressed contract rather than dangling a fence.
        prose = re.sub(r"\s*```[A-Za-z0-9_-]*\s*$", "", text[:start])
        return prose, True

    return text, False


def render_assistant_text_to_stdout(
    text: str, *, drop_json: bool
) -> tuple[str | None, str | None]:
    """Decide how one assistant text block renders to the live transcript.

    Returns ``(visible_text, notice_line)``:

    * ``(None, notice)`` — a leading JSON contract (or a continuation
      chunk of one); show only the one-line marker.
    * ``(prose, notice)`` — prose followed by a trailing JSON contract
      (the recovery shape); show the prose, drop the JSON guts, then the
      marker.
    * ``(text, None)`` — ordinary text (or ``drop_json`` off, e.g.
      ``--output debug``); show verbatim, no marker.

    ``visible_text`` is passed to the transcript formatter by the caller;
    ``notice_line`` is the raw marker string (already de-duplicated, so it
    is ``None`` when the marker was emitted earlier for the same block).
    """
    if should_suppress_assistant_text(text, drop_json=drop_json):
        return None, consume_assistant_json_notice()
    if drop_json:
        prose, found = split_embedded_json_contract(text)
        if found:
            _begin_json_block()
            visible = prose.rstrip()
            return (visible or None), consume_assistant_json_notice()
    return text, None


@contextmanager
def defer_assistant_json() -> Iterator[None]:
    """Suppress assistant-text blocks that look like JSON in live
    stdout for the duration of the ``with`` block.

    Tool-use lines and prose still pass through. The full raw assistant
    output remains available to the calling phase via the agent
    runtime's return value and to post-mortem debugging via
    ``output.log``.
    """
    prev = _get()
    prev_depth = int(getattr(_state, "assistant_json_depth", 0) or 0)
    prev_fence = bool(getattr(_state, "assistant_json_fence", False))
    prev_notice = bool(getattr(_state, "assistant_json_notice_emitted", False))
    _state.suppress_assistant_json = True
    _reset_json_block_state()
    try:
        yield
    finally:
        _state.suppress_assistant_json = prev
        _state.assistant_json_depth = prev_depth
        _state.assistant_json_fence = prev_fence
        _state.assistant_json_notice_emitted = prev_notice
