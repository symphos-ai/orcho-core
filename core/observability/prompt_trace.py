"""Side-channel slots for prompt-engine handoff.

Two independent channels:

``_LAST_UPPER`` — composer → builder gateway handoff.  Set by
:func:`pipeline.prompts.composer.render_prompt_parts` and consumed by
:func:`pipeline.prompts.builders._render_prompt_output`.  Stays untouched by
this refactor; it is the composer→builder internal plumbing, separate from the
invoke-boundary slot below.

``_LAST_PROMPT_TURN`` — builder/invoke-boundary → runtime adapter.  The code
path that is about to call ``agent.invoke`` sets this to the **effective**
:class:`~pipeline.prompts.turn.PromptTurn` (full or delta) immediately before
the call.  The runtime adapter (claude / codex / gemini) does exactly one
:func:`take_last_prompt_turn` per invocation and clears the slot even when
debug output is off.

Both channels are :class:`contextvars.ContextVar` so they are async-safe and
give each value a single-take lifecycle.
"""

from __future__ import annotations

from contextvars import ContextVar

from pipeline.prompts.types import PromptPart

_LAST_UPPER: ContextVar[tuple[PromptPart, ...] | None] = ContextVar(
    "orcho_last_prompt_upper", default=None,
)
# Lazy import type to avoid circular dependency at module load time.
# The actual type is pipeline.prompts.turn.PromptTurn.
_LAST_PROMPT_TURN: ContextVar[object] = ContextVar(
    "orcho_last_prompt_turn", default=None,
)


# ---------------------------------------------------------------------------
# Composer → builder handoff (_LAST_UPPER)
# ---------------------------------------------------------------------------

def set_last_upper(parts: tuple[PromptPart, ...]) -> None:
    """Record the upper composable parts the composer just rendered."""
    _LAST_UPPER.set(parts)


def take_last_upper() -> tuple[PromptPart, ...] | None:
    """Consume the last upper parts, clearing the slot."""
    value = _LAST_UPPER.get()
    _LAST_UPPER.set(None)
    return value


# ---------------------------------------------------------------------------
# Invoke-boundary → adapter handoff (_LAST_PROMPT_TURN)
# ---------------------------------------------------------------------------

def set_last_prompt_turn(turn: object) -> None:
    """Record the effective :class:`~pipeline.prompts.turn.PromptTurn`.

    Called by the code path that is about to invoke the agent — immediately
    before ``agent.invoke(effective_turn.text, …)``.  Builders must not call
    this; the slot is adapter-transcript plumbing only.
    """
    _LAST_PROMPT_TURN.set(turn)


def take_last_prompt_turn() -> object:
    """Consume the last prompt turn, clearing the slot.

    Returns ``None`` when no turn was set since the last take.  The runtime
    adapter calls this once per invocation and derives the trace view from the
    returned turn.  The slot is cleared on take even when debug output is off.
    """
    value = _LAST_PROMPT_TURN.get()
    _LAST_PROMPT_TURN.set(None)
    return value


def peek_last_prompt_turn() -> object:
    """Return the last prompt turn WITHOUT clearing the slot.

    Read-only inspection for callers that must not disturb the single-take
    lifecycle the runtime adapter relies on (e.g. tests, a legend line printed
    before the adapter's own consuming ``take``).
    """
    return _LAST_PROMPT_TURN.get()


def clear_last_prompt_turn() -> None:
    """Clear the prompt-turn slot without returning its value.

    Used by error paths that must drain the slot without consuming the turn.
    """
    _LAST_PROMPT_TURN.set(None)


__all__ = [
    "clear_last_prompt_turn",
    "peek_last_prompt_turn",
    "set_last_prompt_turn",
    "set_last_upper",
    "take_last_prompt_turn",
    "take_last_upper",
]
