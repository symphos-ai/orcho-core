# Multimodal Runtime Support

> <!-- TODO(orcho-multimodal): expand with end-to-end runtime-specific
> IMAGE / BINARY examples once every built-in CLI exposes a stable
> multimodal surface. -->

A *multimodal* agent runtime accepts non-text inputs (images, binary
data) alongside the text prompt. orcho's data model already represents
all three kinds (`TEXT` / `IMAGE` / `BINARY`); this guide covers what
runtime authors need to do to consume them.

## Current status

| Capability | Status |
|------------|--------|
| TEXT attachment injection into prompt | ✅ Phase 4.5 (PLAN handler) |
| IMAGE / BINARY data on `state.attachments` | ✅ Carried through runtime state |
| `IAgentRuntime.invoke(..., attachments=())` Protocol | ✅ Runtime contract |
| Per-runtime IMAGE / BINARY translation | Partial; depends on CLI support |
| Per-phase `attachments_filter` | Future profile control |

TEXT attachments are rendered into `prompt` before the runtime call.
Passing TEXT through `invoke(attachments=...)` is a caller bug; built-in
runtimes raise `ValueError` to prevent double injection.

## Runtime contract

Every runtime implements `IAgentRuntime.invoke` with an `attachments`
kwarg. The default is empty; IMAGE / BINARY handling is runtime-specific.

```python
@runtime_checkable
class IAgentRuntime(Protocol):
    model: str
    session_id: str | None
    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple[Attachment, ...] = (),
    ) -> str: ...
```

## Per-runtime translation

| Runtime | Translation |
|---------|-------------|
| Claude CLI | `claude --image <path>` per IMAGE attachment (Sonnet 4+ vision). TEXT inline via `render_text_block`. |
| Codex CLI | Multimodal flag depends on model (`gpt-5.4-vision` supports, `gpt-5.5` may not). |
| Gemini CLI | IMAGE / BINARY are accepted by the runtime surface but not yet translated to CLI flags; TEXT is still rejected. |
| Forge / others | Best-effort: prompt injection via `render_text_block` + warn for unsupported IMAGE. |

## Backend invariant

Runtimes MUST NOT self-select skills or attachments based on prompt
content. orcho passes the explicit `attachments` kwarg; the runtime
delivers exactly that set to the model. A runtime that has its own
vision path (e.g. Forge auto-attaches images from clipboard) MUST
ignore that path when running under orcho.

## Helper API

Authors writing handler integrations today (TEXT only) use:

```python
from pipeline.attachment_inject import render_text_block, split_by_kind

# Inside a handler:
text_block = render_text_block(state.attachments)  # prepend to prompt
text_atts, multimodal_atts = split_by_kind(state.attachments)
# text_atts → render into prompt body
# multimodal_atts → pass to runtime.invoke(attachments=...)
```

## Token cost considerations

IMAGE attachments are expensive — Claude Sonnet 4 charges per image
tile. Phase 7's per-phase `attachments_filter` lets profile authors
narrow which attachments each phase sees:

```json
{"phase": "build",
 "attachments_filter": ["mockup", "spec"]}
```

Glob match against `Attachment.name` — the architect sees mockup +
spec, the developer agent doesn't pay for the mockup tokens it doesn't
need.

## See also

- [`docs/reference/attachments.md`](../reference/attachments.md) — data
  model + CLI flags
- `pipeline/attachment_inject.py` — render + partition helpers
