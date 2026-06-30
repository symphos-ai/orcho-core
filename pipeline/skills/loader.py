"""pipeline/skills/loader.py — Phase 7d-1: canonical SKILL.md loader.

Parses Agent Skills packages from a directory layout:

    skill-name/
      SKILL.md           # canonical entry — frontmatter + markdown body
      scripts/           # optional executable assets (subprocess via agent)
      references/        # optional supporting docs (loaded on demand)
      assets/            # optional images / data files

This module owns the directory-bound loader. Multi-source discovery
(project / workspace / user / entry_points / compat readers), trust
policy enforcement, and prompt injection live in companion modules
(Phase 7d-2 .. 7d-4). The loader stays narrow and side-effect free:

    pkg = load_skill_package(skill_dir, source="project")

Parsing rules
=============

* SKILL.md MUST start with a YAML frontmatter fenced by ``---``.
* ``name`` and ``description`` are required (Agent Skills standard).
  Missing/empty ``description`` raises — the architect roster needs it.
  Missing ``name`` falls back to the directory name (lenient on typos).
* Frontmatter supports a small YAML subset (stdlib-only — orcho-core has
  no PyYAML dep): top-level scalars, folded/plain multiline scalars,
  block / inline lists, pipe block
  scalars, and **nested mappings** for ``metadata.orcho.*`` (R9 keeps
  orcho-specific routing hints under that subtree).
* Resources under ``scripts/``, ``references/``, ``assets/`` are
  enumerated for the manifest. Bodies are NOT hashed at discovery —
  progressive disclosure preserves token efficiency. ``SkillPackage.
  checksum`` covers the canonical SKILL.md plus per-resource
  (relative_path, size, mtime) tuples.
* Resource paths that escape ``root_dir`` (symlink farm, ``..``) are
  rejected loudly.

Errors
======

The loader raises ``SkillParseError`` on malformed packages. Multi-source
discovery (Phase 7d-2) catches per-package failures and logs them so
**one bad skill doesn't block the rest of the registry**.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.skills.types import ResourceManifestEntry, SkillPackage

__all__ = [
    "SkillParseError",
    "parse_skill_md",
    "load_skill_package",
    "discover_skills_in_root",
]


# Resource subdirectories enumerated for the manifest, in canonical order
# (matters for checksum stability).
_RESOURCE_SUBDIRS: tuple[str, ...] = ("scripts", "references", "assets")


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)

# Slug rule for fallback names — Agent Skills convention is kebab-case
# alphanumerics (no spaces, no path separators).
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


class SkillParseError(ValueError):
    """Raised when a SKILL.md package is malformed.

    Multi-source discovery catches this and logs the offending package
    rather than aborting registry construction.
    """


# ── Public API ────────────────────────────────────────────────────────


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Split SKILL.md text into (frontmatter dict, body markdown).

    Raises ``SkillParseError`` if the frontmatter fence is missing or
    cannot be parsed. The body is returned with leading/trailing
    whitespace trimmed.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillParseError(
            "missing or malformed YAML frontmatter (expected '---' fences)"
        )
    fm_text = match.group("fm")
    body = match.group("body").strip()
    try:
        frontmatter = _parse_yaml_subset(fm_text)
    except SkillParseError:
        raise
    except Exception as exc:  # noqa: BLE001 — wrap as parse error
        raise SkillParseError(f"frontmatter YAML parse error: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise SkillParseError(
            f"frontmatter must be a mapping, got {type(frontmatter).__name__}"
        )
    return frontmatter, body


def load_skill_package(
    skill_dir: Path,
    *,
    source: str = "unknown",
) -> SkillPackage:
    """Load one skill package from ``skill_dir``.

    The directory must contain a ``SKILL.md`` file. Resources under the
    canonical subdirectories (``scripts/``, ``references/``, ``assets/``)
    are enumerated for the manifest. Path traversal is rejected.

    The returned :class:`SkillPackage` is frozen and reproducibility-
    safe: ``checksum`` is computed over the canonical SKILL.md bytes
    plus the sorted resource manifest tuples.

    Raises ``SkillParseError`` on malformed packages. Multi-source
    discovery catches and logs.
    """
    skill_md_path = skill_dir / "SKILL.md"
    if not skill_md_path.is_file():
        raise SkillParseError(f"missing SKILL.md in {skill_dir}")

    raw_text = skill_md_path.read_text(encoding="utf-8")
    frontmatter, body = parse_skill_md(raw_text)

    # Name: explicit frontmatter > directory basename. Lenient on typos —
    # we slug-normalise but don't fail the package; the architect roster
    # recovers as long as `description` is present.
    raw_name = str(frontmatter.get("name") or "").strip() or skill_dir.name
    name = _slugify(raw_name)
    if not name:
        raise SkillParseError(
            f"skill {skill_dir.name!r}: name is empty after slug-normalisation"
        )
    if name != raw_name:
        # Surface drift loudly so plugin authors can see it; not fatal.
        print(
            f"  ! skill {skill_dir!s}: normalised name {raw_name!r} → {name!r}"
        )

    description = str(frontmatter.get("description") or "").strip()
    if not description:
        raise SkillParseError(
            f"skill {name!r}: description required (Agent Skills "
            "mandates description for selection)"
        )

    resource_manifest, resources = _scan_resources(skill_dir)
    checksum = _compute_checksum(raw_text, resource_manifest)

    return SkillPackage(
        name=name,
        description=description,
        root_dir=skill_dir.resolve(),
        skill_md_path=skill_md_path.resolve(),
        body=body,
        frontmatter=frontmatter,
        resources=resources,
        source=source,
        checksum=checksum,
        resource_manifest=resource_manifest,
    )


def discover_skills_in_root(
    root: Path,
    *,
    source: str,
) -> dict[str, SkillPackage]:
    """Scan one root for ``<name>/SKILL.md`` packages.

    Returns ``{name: SkillPackage}``. Per-package failures are logged
    and skipped — one malformed package must not abort discovery.
    Used by Phase 7d-2 multi-source dispatcher; tests can also call it
    directly.

    The ``root`` need not exist; missing roots return an empty dict
    (``~/.agents/skills/`` is commonly absent).
    """
    if not root.is_dir():
        return {}

    out: dict[str, SkillPackage] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            # Loose files (README.md, etc.) are not packages; ignore.
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            # Directory without SKILL.md is not a skill; ignore (avoids
            # complaining about scripts/ assets/ alongside packages).
            continue
        try:
            pkg = load_skill_package(entry, source=source)
        except SkillParseError as exc:
            print(f"  ! skills: skipping {entry!s}: {exc}")
            continue
        if pkg.name in out:
            # Same root, same name twice — inconsistent layout. Keep
            # first to match canonical-priority semantics; log loudly.
            print(
                f"  ! skills: duplicate name {pkg.name!r} in {root!s} "
                f"(second source: {entry!s}); keeping first"
            )
            continue
        out[pkg.name] = pkg
    return out


# ── Internals ─────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    """Normalise a skill name to kebab-case alphanumerics.

    Whitespace and unsafe path characters collapse to ``-``. Empty
    result returns empty string (caller raises). The transform is
    idempotent and matches how `.agents/skills/<name>/` paths land on
    disk.
    """
    lowered = name.strip().lower()
    slug = _SLUG_RE.sub("-", lowered).strip("-")
    return slug


def _scan_resources(
    skill_dir: Path,
) -> tuple[tuple[ResourceManifestEntry, ...], tuple[Path, ...]]:
    """Enumerate resources under canonical subdirectories.

    Returns ``(manifest, paths)`` where ``manifest`` is the metadata
    tuple for checksum, and ``paths`` are absolute paths the runtime
    can stat / read on demand.
    """
    skill_root = skill_dir.resolve()
    manifest: list[ResourceManifestEntry] = []
    paths: list[Path] = []

    for sub in _RESOURCE_SUBDIRS:
        subdir = skill_dir / sub
        if not subdir.is_dir():
            continue
        for path in sorted(subdir.rglob("*")):
            if not path.is_file():
                continue
            # Reject path traversal: resolve and verify the target
            # is still inside skill_root (catches ``..`` symlinks
            # and absolute symlink farms).
            resolved = path.resolve()
            try:
                resolved.relative_to(skill_root)
            except ValueError as exc:
                raise SkillParseError(
                    f"resource {path!s} escapes skill root {skill_root!s}"
                ) from exc
            try:
                stat = resolved.stat()
            except OSError as exc:
                raise SkillParseError(
                    f"resource {path!s} stat failed: {exc}"
                ) from exc
            relative = resolved.relative_to(skill_root).as_posix()
            manifest.append(
                ResourceManifestEntry(
                    relative_path=relative,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
            )
            paths.append(resolved)

    # Sort by relative_path so checksum is stable across filesystem
    # iteration orderings.
    manifest.sort(key=lambda e: e.relative_path)
    paths.sort()
    return tuple(manifest), tuple(paths)


def _compute_checksum(
    skill_md_text: str,
    manifest: tuple[ResourceManifestEntry, ...],
) -> str:
    """sha256 over canonical SKILL.md + manifest tuples.

    Resource bodies are intentionally NOT hashed (progressive disclosure).
    The manifest captures (relative_path, size, mtime) which catches
    add/remove/replace at near-zero cost; ``SkillResourceBinding`` (Phase
    7d-4) records full body hashes only when the agent actually loads a
    resource.
    """
    h = hashlib.sha256()
    h.update(skill_md_text.encode("utf-8"))
    h.update(b"\x1f")  # ascii unit-separator between sections
    for entry in manifest:
        h.update(entry.relative_path.encode("utf-8"))
        h.update(b"\x1e")
        h.update(str(entry.size_bytes).encode("ascii"))
        h.update(b"\x1e")
        h.update(str(entry.mtime_ns).encode("ascii"))
        h.update(b"\x1f")
    return h.hexdigest()


# ── YAML subset (stdlib-only) ─────────────────────────────────────────
#
# Supports what Agent Skills frontmatter actually uses:
#   * ``key: scalar`` (with ``"`` / ``'`` quoting)
#   * ``key: |`` / ``key: >-`` block scalars
#   * ``key: scalar`` followed by indented continuation lines
#   * ``key:`` followed by indented ``- item`` block list
#   * ``key: [a, b, c]`` inline lists
#   * ``key:`` followed by indented child mapping (nested dicts), used
#     by ``metadata.orcho.{applicable_phases,file_patterns}`` per R9.
#
# We intentionally avoid full YAML 1.2 (anchors, aliases, type tags) —
# orcho-core has no PyYAML dependency, and adding one for this surface
# would invert the cost-benefit. If a plugin author needs richer YAML
# they can ship a Python factory via ``orcho.skills`` entry_points.

_INDENT_RE = re.compile(r"^([ \t]*)")


@dataclass
class _Line:
    indent: int
    raw: str
    content: str
    line_no: int


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    """Parse a YAML subset sufficient for Agent Skills frontmatter."""
    lines: list[_Line] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent_match = _INDENT_RE.match(raw)
        indent = len(indent_match.group(1).expandtabs(4)) if indent_match else 0
        lines.append(_Line(indent=indent, raw=raw, content=stripped, line_no=i))

    if not lines:
        return {}

    # All keys at the topmost indent are the root mapping.
    root_indent = lines[0].indent
    result, consumed = _parse_mapping(lines, 0, root_indent)
    if consumed != len(lines):
        leftover = lines[consumed]
        raise SkillParseError(
            f"unexpected line {leftover.line_no}: {leftover.raw!r}"
        )
    return result


def _parse_mapping(
    lines: list[_Line], start: int, indent: int,
) -> tuple[dict[str, Any], int]:
    """Parse an indented mapping starting at ``lines[start]``.

    Stops when a line with indent < ``indent`` is encountered, or at EOF.
    Returns ``(mapping, next_index)``.
    """
    out: dict[str, Any] = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise SkillParseError(
                f"line {line.line_no}: unexpected indent (expected {indent}, "
                f"got {line.indent})"
            )

        if ":" not in line.content:
            raise SkillParseError(
                f"line {line.line_no}: expected 'key: value', got {line.raw!r}"
            )

        key, _, value = line.content.partition(":")
        key = key.strip()
        value = value.strip()

        if not key:
            raise SkillParseError(f"line {line.line_no}: empty key")

        # Literal/folded block scalar.
        if _is_block_scalar_marker(value):
            i += 1
            block: list[str] = []
            block_indent: int | None = None
            while i < len(lines) and lines[i].indent > indent:
                if block_indent is None:
                    block_indent = lines[i].indent
                # Reconstruct the trimmed body relative to block_indent.
                expanded = lines[i].raw.expandtabs(4)
                block.append(
                    expanded[block_indent:]
                    if len(expanded) >= block_indent
                    else expanded.lstrip()
                )
                i += 1
            out[key] = _format_block_scalar(value, block)
            continue

        # Inline list.
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            out[key] = [
                _strip_quotes(p.strip())
                for p in _split_inline_list(inner)
                if p.strip()
            ]
            i += 1
            continue

        # Empty value → either nested mapping or block list, decided by
        # what follows.
        if value == "":
            # Look ahead for the first deeper line.
            j = i + 1
            while j < len(lines) and lines[j].indent <= indent:
                # Same-or-shallower indent immediately after means an
                # empty value (treated as null / empty string).
                break  # noqa: B007 — single-iteration sentinel
            if j >= len(lines) or lines[j].indent <= indent:
                out[key] = ""
                i += 1
                continue

            child_indent = lines[j].indent
            if lines[j].content.startswith("- "):
                items, end = _parse_block_list(lines, j, child_indent)
                out[key] = items
                i = end
                continue

            child, end = _parse_mapping(lines, j, child_indent)
            out[key] = child
            i = end
            continue

        # Plain scalar. YAML allows indented continuation lines; fold them
        # with spaces so multiline descriptions remain valid Agent Skills
        # frontmatter without requiring PyYAML.
        scalar_parts = [_strip_quotes(value)]
        i += 1
        while i < len(lines) and lines[i].indent > indent:
            scalar_parts.append(lines[i].content)
            i += 1
        out[key] = " ".join(part for part in scalar_parts if part).strip()

    return out, i


def _parse_block_list(
    lines: list[_Line], start: int, indent: int,
) -> tuple[list[Any], int]:
    """Parse ``- item`` block list starting at ``lines[start]``."""
    items: list[Any] = []
    i = start
    while i < len(lines) and lines[i].indent == indent:
        line = lines[i]
        if not line.content.startswith("-"):
            break
        item_text = line.content[1:].strip()
        items.append(_strip_quotes(item_text))
        i += 1
    return items, i


def _split_inline_list(text: str) -> list[str]:
    """Split ``a, "b, c", d`` respecting quoted commas."""
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            buf.append(ch)
            quote = ch
            continue
        if ch == ",":
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _is_block_scalar_marker(value: str) -> bool:
    return value in {"|", "|-", "|+", ">", ">-", ">+"}


def _format_block_scalar(marker: str, block: list[str]) -> str:
    if marker.startswith(">"):
        rendered = " ".join(line.strip() for line in block if line.strip())
    else:
        rendered = "\n".join(block)

    if marker.endswith("+"):
        return rendered
    return rendered.rstrip()


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value
