"""Attachment dataclass.

Already covered minimally in test_runtime_redesign_types.py. This file
adds edge cases around real filesystem-style scenarios that surface in
the multimodal CLI / MCP loaders.
"""
import dataclasses

import pytest

from pipeline.runtime import Attachment, AttachmentKind


def test_text_with_b64_payload() -> None:
    a = Attachment(
        kind=AttachmentKind.TEXT,
        name="inline_spec",
        content_b64="SGVsbG8=",
    )
    assert a.content_path is None
    assert a.content_b64 == "SGVsbG8="


def test_image_with_path_and_mime() -> None:
    a = Attachment(
        kind=AttachmentKind.IMAGE,
        name="mockup",
        content_path="/tmp/mockup.png",
        mime_type="image/png",
    )
    assert a.mime_type == "image/png"


def test_binary_with_mime() -> None:
    a = Attachment(
        kind=AttachmentKind.BINARY,
        name="archive",
        content_path="/tmp/archive.zip",
        mime_type="application/zip",
    )
    assert a.kind is AttachmentKind.BINARY


def test_size_bytes_below_limit() -> None:
    a = Attachment(
        kind=AttachmentKind.TEXT,
        name="big_but_ok",
        content_path="/tmp/log.txt",
        size_bytes=9 * 1024 * 1024,  # 9 MB — under 10 MB default
    )
    assert a.size_bytes == 9 * 1024 * 1024


def test_content_hash_optional() -> None:
    a = Attachment(
        kind=AttachmentKind.TEXT,
        name="x",
        content_path="/p/x.md",
    )
    assert a.content_hash is None


def test_frozen() -> None:
    a = Attachment(
        kind=AttachmentKind.TEXT,
        name="x",
        content_path="/p/x.md",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.name = "y"  # type: ignore[misc]
