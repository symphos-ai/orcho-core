"""prompt-prefix renderer.

Pins the rendered XML-style block format (snapshot stable for customers
who script around prompt output) + skip non-TEXT attachments.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.attachment_inject import render_text_block, split_by_kind
from pipeline.runtime import Attachment, AttachmentKind


def _text_att(tmp_path: Path, name: str, content: str, **kw) -> Attachment:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return Attachment(
        kind=AttachmentKind.TEXT,
        name=name,
        content_path=str(p),
        **kw,
    )


# ── render_text_block ────────────────────────────────────────────────────────

class TestRenderTextBlock:
    def test_empty_returns_empty_string(self) -> None:
        assert render_text_block(()) == ""

    def test_single_attachment_no_description(self, tmp_path: Path) -> None:
        a = _text_att(tmp_path, "spec.md", "# Spec\n\nDo X.")
        out = render_text_block((a,))
        assert out.startswith("ATTACHMENTS:\n\n")
        assert '<attachment name="spec.md">' in out
        assert "# Spec\n\nDo X." in out
        assert "</attachment>" in out

    def test_attachment_with_description(self, tmp_path: Path) -> None:
        a = _text_att(tmp_path, "spec.md", "body", description="Product spec v3")
        out = render_text_block((a,))
        assert 'desc="Product spec v3"' in out

    def test_multiple_attachments_in_order(self, tmp_path: Path) -> None:
        a = _text_att(tmp_path, "a.md", "alpha")
        b = _text_att(tmp_path, "b.md", "beta")
        out = render_text_block((a, b))
        assert out.index('name="a.md"') < out.index('name="b.md"')

    def test_image_attachment_skipped(self, tmp_path: Path) -> None:
        """Non-TEXT goes to runtime kwarg, not into the prompt body."""
        img = Attachment(
            kind=AttachmentKind.IMAGE,
            name="mockup.png",
            content_path="/tmp/mockup.png",
            mime_type="image/png",
        )
        text = _text_att(tmp_path, "spec.md", "see image")
        out = render_text_block((img, text))
        assert "mockup.png" not in out
        assert "spec.md" in out

    def test_only_images_yields_empty_string(self) -> None:
        img = Attachment(
            kind=AttachmentKind.IMAGE,
            name="m.png",
            content_path="/tmp/m.png",
            mime_type="image/png",
        )
        assert render_text_block((img,)) == ""

    def test_xml_special_chars_escaped_in_attributes(self, tmp_path: Path) -> None:
        a = _text_att(tmp_path, 'weird "name".md', 'body',
                      description='has "quotes" & <tags>')
        out = render_text_block((a,))
        # Name in attribute slot escaped:
        assert 'name="weird &quot;name&quot;.md"' in out
        # Description escaped:
        assert "&quot;quotes&quot;" in out
        assert "&amp;" in out

    def test_missing_file_yields_placeholder(self, tmp_path: Path) -> None:
        """Runtime read errors don't crash prompt build — they surface
 as a placeholder line so the agent sees something explainable."""
        a = Attachment(
            kind=AttachmentKind.TEXT,
            name="ghost.md",
            content_path=str(tmp_path / "does-not-exist.md"),
        )
        out = render_text_block((a,))
        assert "[orcho:" in out
        assert "ghost.md" in out

    def test_b64_payload_decoded(self) -> None:
        import base64
        encoded = base64.b64encode(b"inline body").decode("ascii")
        a = Attachment(
            kind=AttachmentKind.TEXT,
            name="inline.md",
            content_b64=encoded,
        )
        out = render_text_block((a,))
        assert "inline body" in out


# ── split_by_kind ─────────────────────────────────────────────────────────────

class TestSplitByKind:
    def test_partitions_correctly(self, tmp_path: Path) -> None:
        text = _text_att(tmp_path, "x.md", "body")
        img = Attachment(
            kind=AttachmentKind.IMAGE, name="m.png",
            content_path="/tmp/m.png", mime_type="image/png",
        )
        binary = Attachment(
            kind=AttachmentKind.BINARY, name="z.bin",
            content_path="/tmp/z.bin", mime_type="application/octet-stream",
        )
        text_out, multi_out = split_by_kind((text, img, binary))
        assert text_out == (text,)
        assert multi_out == (img, binary)

    def test_empty(self) -> None:
        assert split_by_kind(()) == ((), ())
