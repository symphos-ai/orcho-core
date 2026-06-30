# ADR 0060 — PromptTurn as the Canonical Prompt Render Surface

**Status:** Accepted
**Supersedes:** ADR 0026 (prompt part metadata), ADR 0028 (cache-first wire layout) — both remain valid for their classification rules; this ADR supersedes only the *render surface* they implied.

---

## Context

Before this change, the prompt engine had a **copy-sync antipattern**: every
runtime builder returned a raw `str` for `agent.invoke` while simultaneously
maintaining two parallel sidecar objects — a typed render record (debug/trace)
and `PromptRenderEnvelope` (session/cache selection) — that were derived from
the same part list but managed independently.

This led to **Bug 3**: the rejected-hypothesis context block appended by
`_hypothesis_suffix_part` was invisible to `--output debug` transcripts because
the sidecar render record was populated at builder time (before the hypothesis
append), while the actual wire string received the hypothesis text only after
the builder returned.

The root cause: there was no single authoritative object that captured both
*what was sent on the wire* and *why each byte belongs there*.

---

## Decision

Introduce **`PromptTurn`** (`pipeline/prompts/turn.py`) as the single
authoritative prompt render surface.  All runtime builders return `PromptTurn`;
the raw `str` is extracted only at the `agent.invoke` boundary via
`turn.text`.

### Core types

**`PromptSegment(text, part, segment_id, source_segment_id=None)`** — one wire
segment.  `text` is the exact bytes contributed to the wire prompt (including
`"\n\n"` separator glue for non-first segments).  `part` is the owning
`PromptPart`.  `source_segment_id` links a rebased segment back to its source;
when omitted it defaults to `segment_id` (source segments are their own source).
Invariants:

- Non-empty body: `part.body` occurs exactly once in `text`
- Empty body: `text == ""`
- `segment_id` is unique within a turn
- `source_segment_id` points at the originating source-turn `segment_id`
  (equals `segment_id` for non-rebased segments)

**`PromptTurn(segments)`** — ordered tuple of segments.  Projections:

| Property / Method     | Returns                                        |
|-----------------------|------------------------------------------------|
| `.text`               | Wire-identical joined string for `agent.invoke`|
| `.parts`              | Ordered `PromptPart` tuple (metadata)          |
| `.envelope()`         | `PromptRenderEnvelope` for cache/session select|
| `.trace_view()`       | `PromptTraceView` for debug transcript         |
| `.render_selected(p)` | Rebased delta `PromptTurn` for session delta   |

**`PromptTurnEditor`** — mutable builder for constructing or extending turns.
Accepts `prepend / append / inject_mid` operations; produces a turn with
separator glue baked correctly on `build()`.

**`PromptTraceView(text, segments)`** — debug-transcript projection.  The
renderer iterates `segments` for frame bodies and uses `segment.part` for
metadata.  `text == "".join(s.text for s in segments)`.

### Separator invariant

Separator glue (`"\n\n"`) is baked into `segment.text` rather than joined at
render time:

- First non-empty segment: `text = part.body` (no prefix)
- Subsequent non-empty segments: `text = "\n\n" + part.body`
- Empty-body segments: `text = ""`

This makes `turn.text` byte-identical to the old
`"\n\n".join(p.body for p in ordered if p.body).strip()` output.

### Assembler

`assemble_cache_first_segments(parts) -> PromptTurn` is the new single
ordering authority, replacing the old assembler gateway function that returned
`(str, tuple[PromptPart, ...])`.  Same sort order (cache-breadth tier + kind
sub-order from ADR 0028); returns a `PromptTurn` with separator glue baked in.

### Trace slot

`core.observability.prompt_trace` now exposes a single
`_LAST_PROMPT_TURN: ContextVar[PromptTurn | None]` slot, replacing the two
former context vars (one for the render record, one for the render envelope).
The slot is set **immediately before `agent.invoke`** (at the invoke boundary),
not at builder time.  This guarantees that whatever goes on the wire is exactly
what the adapter reads from the trace slot.

### Delta rendering

`PromptTurn.render_selected(selected_parts)` maps each selected part by object
identity + occurrence count to its segment, then builds a rebased turn with
fresh separator glue.  The effective wire bytes are:

```
effective_turn.text
    == "\n\n".join(p.body for p in selected_parts if p.body).strip()
```

Session/cache selection always uses `source_turn.envelope()`; debug transcript
uses `effective_turn.trace_view()`.

---

## Consequences

### Bug 3 fixed

The hypothesis context block (`hypothesis_suffix_part`) is appended via
`PromptTurnEditor` *before* the turn is published to the trace slot.  The
adapter reads the effective turn (full or delta) and its `trace_view().segments`
includes every segment that was actually sent — including hypothesis context.

### Removed

- The typed render-record dataclass that lived in `pipeline/prompts/types.py`
- The old assembler gateway function in `pipeline/prompts/composer.py`
- The composition-slot accessors (set/take/peek) in `core/observability/prompt_trace.py`
- The envelope-slot accessors (set/take) in `core/observability/prompt_trace.py`

### Preserved

- All ADR 0026 `PromptPart` metadata fields and validation rules
- All ADR 0028 cache-breadth tier ordering and kind sub-order
- All ADR 0055 session-aware delta selection semantics
- Wire byte output for every existing builder (byte-identical)

### Risks

- `part.body` strings that contain `"\n\n"` as a prefix (e.g. from
  `format_validated_hypothesis_context`) must be `lstrip("\n")`-ed before
  creating the part — `PromptTurnEditor` adds the canonical separator and a
  leading `"\n\n"` in the body would double it.  `hypothesis_suffix_part`
  encodes this requirement in its factory function.

---

## Implementation notes

- `pipeline/prompts/turn.py` — all new types
- `pipeline/prompts/composer.py` — `assemble_cache_first_segments`
- `core/observability/prompt_trace.py` — single `_LAST_PROMPT_TURN` slot
- `pipeline/phases/builtin.py` — `_session_aware_invoke` accepts `PromptTurn`
- `pipeline/cross_project/session_invoke.py` — same
- `pipeline/prompts/builders.py` — all public builders return `PromptTurn`
- `agents/runtimes/{claude,codex,gemini}.py` — use `take_last_prompt_turn()`
- `core/io/transcript.py` — `render_agent_invocation` / `render_incoming_prompt`
  accept `PromptTraceView` (replaces old typed render-record parameter)

Regression tests: `tests/unit/pipeline/prompts/test_prompt_turn.py`.
