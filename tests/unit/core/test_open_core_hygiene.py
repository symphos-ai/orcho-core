"""Public-package hygiene scan.

Sensitive split terms must not appear in code, docs, or comments of the
public packages. Reasoning is in CLAUDE.md ("Open-core hygiene" section):
the public surface must read as a self-contained open-source project,
with extension points described as generic third-party mechanisms.

Scan scope:
* The host package (orcho-core) is always scanned.
* Sibling public packages (orcho-web, orcho-mcp, orcho-ui-kit) are
 scanned when they're present alongside in a workspace checkout. If
 they're absent (standalone CI for orcho-core), they're silently
 skipped — each sibling repo runs the same check on its own tree.

The token-list documentation files (CLAUDE.md, AGENTS.md) are excluded
because they legitimately enumerate the sensitive terms. SPDX headers
are excluded so normal license identifiers do not trip the scan.
"""
from __future__ import annotations

from pathlib import Path


def _token(*parts: str) -> str:
    return "".join(parts)


BANNED_TOKENS: tuple[str, ...] = (
    _token("desk", "top"),
    _token("orcho", "-", "desk", "top"),
    _token("py", "web", "view"),
    _token("commer", "cial"),
    _token("propriet", "ary"),
    _token("pa", "id"),
    _token("pre", "mium"),
    _token("license", "-", "gate"),
    _token("enterprise", " tier"),
)

SCAN_SUFFIXES: tuple[str, ...] = (".py", ".md", ".toml", ".txt")

EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({
    "__pycache__", ".venv", "node_modules", ".git", ".pytest_cache",
    ".agents", ".orcho", "implement", "dist", ".egg-info",
})

# Filenames that legitimately enumerate the sensitive terms. Matched by
# basename, case-insensitively.
EXCLUDED_FILENAMES: frozenset[str] = frozenset({
    "claude.md",
    "agents.md",
    # Guard test that enumerates the banned terms precisely to assert
    # client-facing text (the MCP server-instructions string) avoids them.
    "test_server_instructions.py",
})

PUBLIC_PACKAGES: tuple[str, ...] = (
    "orcho-core",
    "orcho-web",
    "orcho-mcp",
    "orcho-ui-kit",
)


def _iter_scannable_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info")
               for part in path.parts):
            continue
        if path.name.lower() in EXCLUDED_FILENAMES:
            continue
        yield path


def _scan_for_violations(roots: list[Path]) -> list[str]:
    violations: list[str] = []
    lowered = [t.lower() for t in BANNED_TOKENS]
    for root in roots:
        for path in _iter_scannable_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "SPDX-License-Identifier" in line:
                    continue
                low = line.lower()
                for tok in lowered:
                    if tok in low:
                        violations.append(
                            f"{path}:{lineno}:{tok}: {line.strip()[:200]}"
                        )
                        break
    return violations


def _resolve_scan_roots() -> list[Path]:
    """orcho-core's own tree, plus sibling public packages when present."""
    here = Path(__file__).resolve()
    core_root = here.parents[3]  # tests/unit/core/<this> → orcho-core/
    workspace = core_root.parent

    roots = [core_root]
    for pkg in PUBLIC_PACKAGES:
        if pkg == core_root.name:
            continue
        sibling = workspace / pkg
        if sibling.is_dir():
            roots.append(sibling)
    return roots


def test_no_banned_tokens_in_public_packages() -> None:
    roots = _resolve_scan_roots()
    violations = _scan_for_violations(roots)
    assert not violations, (
        "Banned open-core tokens detected (see CLAUDE.md → Open-core hygiene):\n  "
        + "\n  ".join(violations[:50])
        + (f"\n  ... ({len(violations) - 50} more)" if len(violations) > 50 else "")
    )
