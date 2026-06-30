"""pipeline/attachment_inject.py — Render Attachments as prompt prefix (Phase 4.5).

TEXT attachments are rendered as an XML-style block prepended to the
agent's prompt. IMAGE / BINARY ride to the runtime as the
``attachments`` kwarg on :meth:`agents.protocols.IAgentRuntime.invoke`;
each runtime adapter is responsible for translating them to its CLI's
multimodal flags.

The render format is stable (snapshot-tested) so customers who script
around prompt output don't see drift between releases.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.runtime import Attachment, AttachmentKind


def render_text_block(attachments: tuple[Attachment, ...]) -> str:
    """Render TEXT attachments as a single XML-style block.

    Format::

        ATTACHMENTS:

        <attachment name="spec.md" desc="Product spec v3">
        {content of spec.md}
        </attachment>

        <attachment name="error.log">
        {content of error.log}
        </attachment>

    Returns ``""`` when there are no TEXT attachments — handlers can
    safely concatenate without an empty prefix appearing.
    Non-TEXT attachments are silently skipped (they go through the
    multimodal kwarg, not the prompt body).
    """
    text_atts = [a for a in attachments if a.kind is AttachmentKind.TEXT]
    if not text_atts:
        return ""

    blocks: list[str] = ["ATTACHMENTS:", ""]
    for a in text_atts:
        body = _read_text(a)
        desc_attr = f' desc="{_escape_attr(a.description)}"' if a.description else ""
        blocks.append(f'<attachment name="{_escape_attr(a.name)}"{desc_attr}>')
        blocks.append(body)
        blocks.append("</attachment>")
        blocks.append("")
    return "\n".join(blocks)


def _read_text(attachment: Attachment) -> str:
    """Read the attachment body as text. Prefers ``content_path``
    (lazy filesystem read) over ``content_b64`` (already-loaded
    inline payload).

    Errors during read surface as a placeholder line in the rendered
    block so the agent sees something explainable instead of a
    crashed prompt build. The CLI loader catches missing files
    upfront — runtime read errors here would be very rare (file
    deleted between load and prompt-build).
    """
    if attachment.content_path:
        try:
            return Path(attachment.content_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return f"[orcho: failed to read attachment {attachment.name!r}: {e}]"
    if attachment.content_b64:
        import base64
        try:
            return base64.b64decode(attachment.content_b64).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as e:
            return f"[orcho: failed to decode b64 attachment {attachment.name!r}: {e}]"
    return f"[orcho: empty attachment {attachment.name!r}]"


def _escape_attr(value: str) -> str:
    """Minimal XML attribute escape — quotes + ampersands. Names and
    descriptions come from CLI args / API input, so we sanitise even
    though the rendered block is consumed by an LLM, not a parser."""
    return (
        value.replace("&", "&amp;")
             .replace('"', "&quot;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
    )


def split_by_kind(
    attachments: tuple[Attachment, ...],
) -> tuple[tuple[Attachment, ...], tuple[Attachment, ...]]:
    """Partition into ``(text, multimodal)`` tuples.

    Helper for runtime providers (Phase 7): TEXT goes into the prompt
    via ``render_text_block``, IMAGE / BINARY go to the runtime kwarg.
    """
    text = tuple(a for a in attachments if a.kind is AttachmentKind.TEXT)
    multimodal = tuple(a for a in attachments if a.kind is not AttachmentKind.TEXT)
    return text, multimodal
