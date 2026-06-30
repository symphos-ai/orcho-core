"""file-path → Attachment factory.

Pins kind detection (extension whitelist > mimetypes), mime fallback,
sha256 + size population, error handling for missing / non-file paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.attachment_loader import (
    load_attachment,
    load_attachments_from_paths,
)
from pipeline.runtime import AttachmentKind


def _write(tmp_path: Path, name: str, content: str = "hello\n") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ── Kind auto-detection ───────────────────────────────────────────────────────

class TestKindDetection:
    def test_md_text(self, tmp_path: Path) -> None:
        a = load_attachment(_write(tmp_path, "spec.md"))
        assert a.kind is AttachmentKind.TEXT

    def test_log_text(self, tmp_path: Path) -> None:
        a = load_attachment(_write(tmp_path, "error.log"))
        assert a.kind is AttachmentKind.TEXT

    def test_py_text(self, tmp_path: Path) -> None:
        a = load_attachment(_write(tmp_path, "src.py"))
        assert a.kind is AttachmentKind.TEXT

    def test_json_text(self, tmp_path: Path) -> None:
        a = load_attachment(_write(tmp_path, "data.json", '{"k":1}'))
        assert a.kind is AttachmentKind.TEXT

    def test_png_image(self, tmp_path: Path) -> None:
        # Real PNG header bytes so mimetypes definitely recognises.
        p = tmp_path / "mockup.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        a = load_attachment(p)
        assert a.kind is AttachmentKind.IMAGE
        assert a.mime_type and a.mime_type.startswith("image/")

    def test_unknown_extension_binary(self, tmp_path: Path) -> None:
        p = tmp_path / "blob.xyz"
        p.write_bytes(b"\x00\x01\x02")
        a = load_attachment(p)
        assert a.kind is AttachmentKind.BINARY

    def test_explicit_kind_overrides(self, tmp_path: Path) -> None:
        # .md would auto-detect TEXT, but caller forces BINARY.
        p = _write(tmp_path, "weird.md")
        a = load_attachment(p, kind=AttachmentKind.BINARY)
        assert a.kind is AttachmentKind.BINARY


# ── Mime + size + content_hash ────────────────────────────────────────────────

class TestMimeAndMetadata:
    def test_size_populated(self, tmp_path: Path) -> None:
        p = _write(tmp_path, "x.txt", "abc")
        a = load_attachment(p)
        assert a.size_bytes == 3

    def test_content_hash_is_sha256(self, tmp_path: Path) -> None:
        import hashlib
        p = _write(tmp_path, "x.txt", "hello")
        a = load_attachment(p)
        expected = hashlib.sha256(b"hello").hexdigest()
        assert a.content_hash == expected

    def test_mime_text_default(self, tmp_path: Path) -> None:
        a = load_attachment(_write(tmp_path, "x.unknown_text_ext"))
        # Falls into BINARY because the ext isn't whitelisted; mime is
        # application/octet-stream fallback.
        assert a.mime_type == "application/octet-stream"

    def test_mime_image_required(self, tmp_path: Path) -> None:
        p = tmp_path / "img.png"
        p.write_bytes(b"\x89PNG")
        a = load_attachment(p)
        assert a.mime_type is not None
        assert "image" in a.mime_type

    def test_name_defaults_to_filename(self, tmp_path: Path) -> None:
        a = load_attachment(_write(tmp_path, "spec.md"))
        assert a.name == "spec.md"

    def test_name_override(self, tmp_path: Path) -> None:
        a = load_attachment(_write(tmp_path, "spec.md"), name="ProductSpec")
        assert a.name == "ProductSpec"


# ── Error paths ───────────────────────────────────────────────────────────────

class TestLoadAttachmentErrors:
    def test_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_attachment(tmp_path / "nope.md")

    def test_directory_path_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a regular file"):
            load_attachment(tmp_path)


# ── load_attachments_from_paths ───────────────────────────────────────────────

class TestLoadMultiple:
    def test_multiple_paths(self, tmp_path: Path) -> None:
        a = _write(tmp_path, "a.md")
        b = _write(tmp_path, "b.txt")
        out = load_attachments_from_paths([str(a), str(b)])
        assert len(out) == 2
        assert all(att.kind is AttachmentKind.TEXT for att in out)

    def test_kind_overrides(self, tmp_path: Path) -> None:
        # .md would auto-detect TEXT, but path-specific override forces BINARY.
        a = _write(tmp_path, "a.md")
        out = load_attachments_from_paths(
            [str(a)],
            kind_overrides={str(a): AttachmentKind.BINARY},
        )
        assert out[0].kind is AttachmentKind.BINARY
