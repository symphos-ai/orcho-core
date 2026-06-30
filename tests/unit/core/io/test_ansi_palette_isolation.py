"""Enforcement test for the Terminal color discipline rule.

Production renderers must route every ANSI insertion through
:func:`core.io.ansi.paint` / :func:`core.io.ansi.strip_ansi`.
Prompts use :mod:`core.io.journey_prompt` helpers. Raw ``\\033[…m``
escapes and inline ``f"{C.X}…{C.RESET}"`` patterns belong only in
:mod:`core.io.ansi` (the palette module) and in tests that assert on
the colored path (force_color fixtures).

See the **Terminal color discipline** section of
``orcho-core/CLAUDE.md`` for the full rule.

This test scans every production ``.py`` file under the repo root and
fails with an actionable list of offenders if the discipline slips.
The allowlist is intentionally tiny — just the palette module
itself — so adding any new exception requires a deliberate edit to
this file.
"""
from __future__ import annotations

import re
from pathlib import Path

# ── policy ────────────────────────────────────────────────────────────

# Repo root: this file lives at tests/unit/core/io/test_ansi_palette_isolation.py,
# four levels under the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

# Production code under these top-level directories is in scope.
_PRODUCTION_ROOTS: tuple[str, ...] = (
    "agents",
    "cli",
    "core",
    "pipeline",
    "sdk",
    "tools",
    "shell",
    "examples",
)

# Files that may contain raw ANSI escapes / inline ``{C.X}…{C.RESET}``
# patterns. Tests/ is excluded as a whole (test fixtures may assert on
# the colored path under set_color_enabled(True)); the palette module
# itself is allowed because it owns the escape codes.
_ALLOWLIST_FILES: frozenset[str] = frozenset({
    "core/io/ansi.py",
})

# Directories skipped entirely (caches, build outputs, virtualenvs).
_SKIP_DIRS: frozenset[str] = frozenset({
    "__pycache__",
    ".venv",
    "build",
    "dist",
    "htmlcov",
    "orcho_core.egg-info",
    ".git",
})

# Two patterns flag production ANSI leaks:
#
# 1. Raw CSI escape literals — ``\033[`` or ``\x1b[`` anywhere in source.
#    These are always escape sequences; there is no legitimate reason
#    for production code to embed them outside the palette module.
_RAW_ANSI_RE = re.compile(r"\\033\[|\\x1b\[")

# 2. Inline f-string color interpolation — ``{C.RED}`` style. The
#    canonical pattern is ``paint(text, C.RED)`` instead. Plain
#    references to ``C.RED`` outside braces (e.g. as a function
#    argument: ``paint(text, C.RED)`` or a dict default:
#    ``palette.get(name, C.GREY)``) are fine; only the braced form
#    indicates an f-string wrap.
_INLINE_COLOR_RE = re.compile(r"\{C\.\w+\}")


def _iter_production_files() -> list[Path]:
    files: list[Path] = []
    for root in _PRODUCTION_ROOTS:
        root_path = _REPO_ROOT / root
        if not root_path.is_dir():
            continue
        for candidate in root_path.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in candidate.parts):
                continue
            rel = candidate.relative_to(_REPO_ROOT).as_posix()
            if rel in _ALLOWLIST_FILES:
                continue
            files.append(candidate)
    return files


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(line_no, kind, snippet)`` for every offending line."""
    offenses: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return offenses
    for line_no, line in enumerate(text.splitlines(), start=1):
        if _RAW_ANSI_RE.search(line):
            offenses.append((line_no, "raw_ansi", line.strip()[:120]))
        if _INLINE_COLOR_RE.search(line):
            offenses.append((line_no, "inline_color", line.strip()[:120]))
    return offenses


def _format_offenses(
    by_file: dict[str, list[tuple[int, str, str]]],
) -> str:
    lines = [
        "",
        "ANSI policy violation in production code "
        "(see orcho-core/CLAUDE.md → Terminal color discipline).",
        "",
        "Production renderers must route ANSI through "
        "core.io.ansi.paint() / strip_ansi() — or, for prompts, "
        "through core.io.journey_prompt helpers. Raw `\\033[…m` "
        "escapes and inline `f\"{C.X}…{C.RESET}\"` patterns are "
        "only allowed in `core/io/ansi.py` (the palette module) "
        "and in tests/.",
        "",
        "Offending files:",
    ]
    for rel in sorted(by_file):
        lines.append(f"  {rel}")
        for line_no, kind, snippet in by_file[rel]:
            label = (
                "raw \\033[ escape"
                if kind == "raw_ansi"
                else "inline `{C.X}` interpolation"
            )
            lines.append(f"    L{line_no}  {label}: {snippet}")
    lines.append("")
    lines.append(
        "Fix: replace the inline form with "
        "`paint(text, C.X, [color=color], [stream=sys.stderr])`."
        " Stderr-bound output must pass `stream=sys.stderr` so the "
        "shared policy auto-detects against stderr's TTY status."
    )
    return "\n".join(lines)


def test_no_raw_ansi_or_inline_color_in_production_code() -> None:
    """Fail with an actionable list when production code leaks ANSI."""
    by_file: dict[str, list[tuple[int, str, str]]] = {}
    for path in _iter_production_files():
        offenses = _scan_file(path)
        if offenses:
            rel = path.relative_to(_REPO_ROOT).as_posix()
            by_file[rel] = offenses
    assert not by_file, _format_offenses(by_file)


def test_allowlist_is_minimal() -> None:
    """The allowlist must stay tiny — every entry is a deliberate
    exception. Adding a new one requires a code edit to this test
    so reviewers see the policy widening explicitly.
    """
    assert frozenset({"core/io/ansi.py"}) == _ALLOWLIST_FILES


def test_palette_module_actually_exists() -> None:
    """If the palette module moves the allowlist must move with it.
    Keeps the rule honest against an accidental rename.
    """
    ansi_path = _REPO_ROOT / "core/io/ansi.py"
    assert ansi_path.is_file(), (
        f"core/io/ansi.py not found at {ansi_path}; "
        "update _ALLOWLIST_FILES in this test if the palette moved."
    )
