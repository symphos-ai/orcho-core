# Attachment Reference

> Phase 4.5 reference — TEXT injection end-to-end. <!-- TODO(orcho-phase-7):
> expand once IMAGE / BINARY per-runtime CLI translation lands with the
> IAgentRuntime rename. -->

Prompt-context attachments thread external files (specs, mockups,
error logs) into the agent's context. Phase 4.5 ships the data model,
CLI plumbing, and TEXT injection. IMAGE / BINARY data flow through the
same path but their per-runtime CLI translation (Claude `--image`,
Codex multimodal flag, Gemini native) ships in Phase 7 alongside the
runtime rename.

## CLI flags

```bash
orcho run --task "implement login per mockup" --project ./api \
  --attach docs/spec.md \
  --attach mockups/login.png \
  --attach-text error.log \
  --attach-image flow.svg
```

| Flag | Behaviour |
|------|-----------|
| `--attach <path>` | Auto-detect kind from extension. Repeatable. |
| `--attach-text <path>` | Force `TEXT` kind regardless of extension. |
| `--attach-image <path>` | Force `IMAGE` kind. |
| `--attach-binary <path>` | Force `BINARY` kind. |

All four are repeatable. Paths may be absolute, relative to cwd, or
contain `~` (expanded). Invalid paths fail fast with exit code 2 and a
single diagnostic line; the run never starts.

## Auto-detect rules

`pipeline/attachment_loader.py:_detect_kind` decides:

1. **Extension whitelist** wins first:
   - TEXT: `.md`, `.txt`, `.log`, `.py`, `.js`, `.ts`, `.json`, `.yaml`,
     `.toml`, `.html`, `.xml`, `.css`, `.cs`, `.java`, `.go`, `.rs`,
     `.sh`, `.sql`, `.csv`, and ~30 more
   - IMAGE: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.tiff`
2. **Mimetypes fallback**: anything not whitelisted falls through to
   Python's `mimetypes.guess_type`. `text/*` → TEXT, `image/*` → IMAGE.
3. **Default**: BINARY.

Forcing the kind via `--attach-text foo.bin` (etc.) bypasses detection.

## Data model

```python
@dataclass(frozen=True)
class Attachment:
    kind: AttachmentKind             # TEXT | IMAGE | BINARY
    name: str
    content_path: str | None         # filesystem path (loader-set)
    content_b64: str | None          # inline payload (MCP / API path)
    mime_type: str | None            # required for IMAGE / BINARY
    description: str | None
    size_bytes: int | None           # populated by loader
    content_hash: str | None         # sha256, populated by loader
```

Invariant: exactly one of `content_path` / `content_b64` (xor), enforced
in `__post_init__`. IMAGE / BINARY require `mime_type` non-None. Default
size limit is 10 MB (`_ATTACHMENT_DEFAULT_SIZE_LIMIT`); oversized files
raise at construction.

## Threading into state

`pipeline/project_orchestrator.py:run_pipeline` accepts
`attachments: tuple = ()`. The orchestrator's `main()` loads the CLI
flags via `pipeline.attachment_loader.load_attachment` and passes the
tuple through. `run_pipeline` writes them to `state.attachments`
(field added in Phase 1).

## Handler injection (TEXT only — Phase 4.5)

The PLAN handler renders TEXT attachments as an XML-style block
prepended to the plan prompt:

```
ATTACHMENTS:

<attachment name="spec.md" desc="Product spec v3">
{content of spec.md}
</attachment>

<attachment name="error.log">
{content of error.log}
</attachment>

[then the regular plan prompt]
```

Other handlers (BUILD, FIX, REVIEW, FINAL_ACCEPTANCE) follow the same pattern —
opt-in is one line:

```python
prompt_prefix = ""
if state.attachments:
    from pipeline.attachment_inject import render_text_block
    prompt_prefix = render_text_block(state.attachments)
```

The render is deterministic; XML special characters in name /
description are escaped (snapshot test pinned).

## IMAGE / BINARY

`state.attachments` keeps all three kinds and `IAgentRuntime.invoke`
accepts an `attachments` kwarg. TEXT attachments are rendered into the
prompt before the runtime call; passing TEXT through
`invoke(attachments=...)` is rejected by built-in runtimes to prevent
double injection.

IMAGE / BINARY delivery is runtime-specific:

- Claude can translate supported IMAGE attachments to CLI arguments.
- Codex support depends on the selected model and CLI surface.
- Gemini currently accepts IMAGE / BINARY at the `IAgentRuntime`
  boundary but does not translate them to CLI flags yet.

Future profile work may add per-phase `attachments_filter` on
`PhaseStep` for narrowing which attachments a phase sees (token cost
control for vision models).

Until a selected runtime supports a concrete IMAGE / BINARY CLI surface,
those attachments may be carried through state without being delivered
to the model. The CLI accepts them so users can author commands that work
end-to-end as runtime support improves, without a flag rename.

## See also

- `pipeline/runtime/__init__.py:Attachment` — frozen dataclass + invariants
- `pipeline/attachment_loader.py` — file → Attachment factory
- `pipeline/attachment_inject.py` — text rendering + kind partitioning
- `tests/unit/test_attachment_loader.py`, `test_attachment_inject.py`
