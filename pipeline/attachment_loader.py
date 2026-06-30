"""pipeline/attachment_loader.py — File-path → Attachment factory (Phase 4.5).

Loads ``Attachment`` instances from filesystem paths with mime-type
auto-detection, size validation, and content-hash population. Used by
the CLI ``--attach`` flag and (Phase 7) by orcho-mcp's
``attachments_validate`` resource.

Phase 4.5 minimum scope:
  * TEXT auto-detected from text/* mimes (.md, .txt, .log, .json, .py, ...)
  * IMAGE auto-detected from image/* mimes (.png, .jpg, .jpeg, .gif, .webp)
  * BINARY for everything else

IMAGE / BINARY load through the same factory; their per-runtime CLI
translation rides as the ``attachments`` kwarg on
:meth:`agents.protocols.IAgentRuntime.invoke`. Each runtime decides how to
hand the multimodal payload to its CLI (Claude ``--image`` etc.).
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from pipeline.runtime import Attachment, AttachmentKind

# Extensions we treat as TEXT regardless of what mimetypes returns.
# mimetypes.guess_type often returns None for these (.md, .log, .yaml).
_TEXT_EXTENSIONS = frozenset({
    ".md", ".markdown", ".txt", ".log", ".rst",
    ".py", ".pyi", ".pyx",
    ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".html", ".htm", ".xml", ".svg",
    ".css", ".scss", ".less",
    ".cs", ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".php",
    ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".csv", ".tsv",
})

# Extensions we treat as IMAGE regardless of mimetypes.
_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
})


def _detect_kind(path: Path) -> AttachmentKind:
    """Decide ``AttachmentKind`` from extension + ``mimetypes`` fallback.

    Extension whitelist wins over mime — ``foo.md`` always loads as TEXT
    even on systems where ``mimetypes`` returns ``application/octet-stream``.
    """
    ext = path.suffix.lower()
    if ext in _TEXT_EXTENSIONS:
        return AttachmentKind.TEXT
    if ext in _IMAGE_EXTENSIONS:
        return AttachmentKind.IMAGE

    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        if mime.startswith("text/"):
            return AttachmentKind.TEXT
        if mime.startswith("image/"):
            return AttachmentKind.IMAGE
    return AttachmentKind.BINARY


def _detect_mime(path: Path, kind: AttachmentKind) -> str | None:
    """Return a best-effort mime type. Required for IMAGE / BINARY,
    optional for TEXT."""
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    # Fallback per kind (covers extension-only matches above).
    if kind is AttachmentKind.TEXT:
        return "text/plain"
    if kind is AttachmentKind.IMAGE:
        return f"image/{path.suffix.lstrip('.').lower() or 'octet-stream'}"
    return "application/octet-stream"


def _file_sha256(path: Path) -> str:
    """Compute sha256 of the file contents (streaming, low memory)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_attachment(
    path: str | Path,
    *,
    kind: AttachmentKind | None = None,
    name: str | None = None,
    description: str | None = None,
) -> Attachment:
    """Load one ``Attachment`` from a filesystem path.

    Auto-detects ``kind`` from extension + mimetypes when not passed
    explicitly. Populates ``size_bytes`` (filesystem stat) and
    ``content_hash`` (sha256 stream) so downstream code (cache,
    audit log, multimodal API request) doesn't have to re-read the
    file. Construction-time invariants in ``Attachment`` raise on
    oversize files (>10 MB by default) and missing mime for IMAGE /
    BINARY.

    Raises ``FileNotFoundError`` if the path doesn't exist; the CLI
    catches and reports rather than tracebacking.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"attachment path not found: {p}")
    if not p.is_file():
        raise ValueError(f"attachment path is not a regular file: {p}")

    if kind is None:
        kind = _detect_kind(p)
    mime = _detect_mime(p, kind)
    size = p.stat().st_size

    return Attachment(
        kind=kind,
        name=name or p.name,
        content_path=str(p),
        mime_type=mime,
        description=description,
        size_bytes=size,
        content_hash=_file_sha256(p),
    )


def load_attachments_from_paths(
    paths: list[str],
    *,
    kind_overrides: dict[str, AttachmentKind] | None = None,
) -> tuple[Attachment, ...]:
    """Load multiple attachments. ``kind_overrides`` maps a path to a
    forced kind (e.g. ``--attach-image foo.bin`` → force IMAGE).

    Errors on any path bubble up — CLI surface catches and prints a
    single diagnostic per failure rather than aborting the whole batch.
    Callers that need partial-success semantics should iterate
    ``load_attachment`` directly.
    """
    overrides = kind_overrides or {}
    return tuple(
        load_attachment(p, kind=overrides.get(p))
        for p in paths
    )
