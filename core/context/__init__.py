"""
core/context.py — Repository code-map builder for prompt injection.

Two public helpers:

    build_repo_map(project_dir, plugin)
        Walk *project_dir* and produce a compact text outline of class /
        function declarations across the supported languages. Intended to be
        injected into PLAN and HYBRID prompts so the LLM doesn't burn tokens
        re-discovering the project layout on every call.

    inject_context(prompt, repo_map)
        Append a clearly delimited ``REPO MAP`` block to *prompt*. Returns
        *prompt* unchanged when *repo_map* is empty, so callers can pass an
        unconditional codemap result through without an ``if`` guard.

Tree-sitter is loaded lazily so the package keeps importing without the
optional dependency. When a parser isn't available we fall back to a
regex-based outline that catches the common declarations in C# / Python /
PHP. The fallback is deliberately conservative — better to skip a line than
emit a misleading entry.

The codemap is **never** authoritative — it's a hint for the LLM, the agent
still has full file access. Treat the output as best-effort.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# ── Language detection ────────────────────────────────────────────────────────
# Maps file extension to a language key used in plugin.codemap.languages and
# in our internal parser registry.
_EXT_TO_LANG: dict[str, str] = {
    ".cs":  "c_sharp",
    ".py":  "python",
    ".php": "php",
}


# Directories we never descend into. Keeping the list short and broad so we
# stay correct across Unity/PHP/Python projects without per-plugin config.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".svn", ".hg",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env",
    "node_modules", "vendor",
    "Library", "Temp", "Logs", "obj", "bin",  # Unity/.NET build dirs
    "dist", "build", ".tox",
})


@dataclass(frozen=True)
class CodemapEntry:
    """Single line in the repo map.

    Example: ``ClassName.MethodName`` (kind="method")
             ``module_function``      (kind="function")
             ``ClassName``            (kind="class")
    """
    file: str       # repo-relative POSIX path
    kind: str       # "class" | "function" | "method"
    name: str       # symbol name (qualified for methods: "Class.method")


# ── Regex fallback parsers ────────────────────────────────────────────────────
# These intentionally err on the side of false negatives rather than picking
# up things that aren't really declarations (e.g. type aliases, decorated
# variables). Tree-sitter takes over when available.

# C#: `class Foo`, `public sealed class Foo<T>`, `record Foo`, etc.
# Method heuristic: signature line ending in `{` or `;`, with parens.
_CS_CLASS_RE  = re.compile(
    r"^\s*(?:public|private|protected|internal|abstract|sealed|static|partial|\s)*"
    r"\s*(?:class|struct|interface|record)\s+([A-Za-z_][\w]*)",
    re.MULTILINE,
)
_CS_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|internal|static|virtual|override|"
    r"async|sealed|abstract|extern|unsafe|new|\s)+"
    r"(?:[\w<>,\[\]\?\.\s]+?)\s+"
    r"([A-Z][A-Za-z_0-9]*)\s*\([^;{]*\)\s*(?:where[^{;]*)?[{;]",
    re.MULTILINE,
)

# Python: top-level / nested `class Foo` or `def foo`. We track indent to
# attach methods to the most recent enclosing class.
_PY_CLASS_RE = re.compile(r"^(\s*)class\s+([A-Za-z_][\w]*)")
_PY_DEF_RE   = re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_][\w]*)")

# PHP: `class Foo`, `interface Foo`, `function bar`, `public function bar`.
_PHP_CLASS_RE  = re.compile(
    r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+([A-Za-z_][\w]*)",
    re.MULTILINE,
)
_PHP_MODIFIER = r"(?:public|private|protected|static|final|abstract)"
_PHP_METHOD_RE = re.compile(
    # Require at least ONE real modifier — bare ``function`` is treated as a
    # top-level free function. Whitespace alone shouldn't qualify a method.
    rf"^\s*{_PHP_MODIFIER}(?:\s+{_PHP_MODIFIER})*\s+function\s+"
    r"&?([A-Za-z_][\w]*)\s*\(",
    re.MULTILINE,
)
_PHP_FUNC_RE   = re.compile(
    r"^\s*function\s+&?([A-Za-z_][\w]*)\s*\(",
    re.MULTILINE,
)


def _parse_csharp(text: str) -> Iterable[tuple[str, str]]:
    """Yield ``(kind, name)`` tuples from a C# source string."""
    classes = [m.group(1) for m in _CS_CLASS_RE.finditer(text)]
    for cls in classes:
        yield ("class", cls)
    # Methods: prefix with the last class seen at the same level. We don't
    # parse braces here — use the nearest class above each match.
    class_positions = [(m.start(), m.group(1)) for m in _CS_CLASS_RE.finditer(text)]
    for m in _CS_METHOD_RE.finditer(text):
        # Skip constructors / matches that collide with a class declaration line
        line_start = text.rfind("\n", 0, m.start()) + 1
        line = text[line_start:m.end()]
        if "class " in line or "struct " in line or "interface " in line:
            continue
        cls_name = ""
        for pos, name in class_positions:
            if pos < m.start():
                cls_name = name
            else:
                break
        qualified = f"{cls_name}.{m.group(1)}" if cls_name else m.group(1)
        yield ("method", qualified)


def _parse_python(text: str) -> Iterable[tuple[str, str]]:
    """Yield ``(kind, name)`` tuples from a Python source string.

    Tracks indentation levels to attach methods to their enclosing class.
    """
    # Stack of (indent, class_name) — only the deepest class at each indent
    # is relevant when we encounter a `def` at a deeper level.
    class_stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        m_class = _PY_CLASS_RE.match(line)
        if m_class:
            indent = len(m_class.group(1))
            # pop classes that have ended
            while class_stack and class_stack[-1][0] >= indent:
                class_stack.pop()
            class_stack.append((indent, m_class.group(2)))
            yield ("class", m_class.group(2))
            continue
        m_def = _PY_DEF_RE.match(line)
        if m_def:
            indent = len(m_def.group(1))
            # discard classes whose indent is >= this def's indent
            while class_stack and class_stack[-1][0] >= indent:
                class_stack.pop()
            if class_stack:
                yield ("method", f"{class_stack[-1][1]}.{m_def.group(2)}")
            else:
                yield ("function", m_def.group(2))


def _parse_php(text: str) -> Iterable[tuple[str, str]]:
    """Yield ``(kind, name)`` tuples from a PHP source string."""
    classes = [(m.start(), m.group(1)) for m in _PHP_CLASS_RE.finditer(text)]
    for _, name in classes:
        yield ("class", name)

    method_positions = {m.start() for m in _PHP_METHOD_RE.finditer(text)}
    for m in _PHP_METHOD_RE.finditer(text):
        cls_name = ""
        for pos, name in classes:
            if pos < m.start():
                cls_name = name
            else:
                break
        qualified = f"{cls_name}.{m.group(1)}" if cls_name else m.group(1)
        yield ("method", qualified)
    # Top-level functions: PHP_FUNC_RE matches both methods and free functions,
    # so we filter out anything already covered by PHP_METHOD_RE.
    for m in _PHP_FUNC_RE.finditer(text):
        if m.start() in method_positions:
            continue
        yield ("function", m.group(1))


_PARSERS = {
    "c_sharp": _parse_csharp,
    "python":  _parse_python,
    "php":     _parse_php,
}


# ── Public API ────────────────────────────────────────────────────────────────
def build_repo_map(
    project_dir: str | Path,
    plugin: object | None = None,
    *,
    max_depth: int = 3,
    max_entries: int = 400,
    languages: list[str] | None = None,
) -> str:
    """Walk *project_dir* and emit a compact text outline.

    ``plugin`` is optional and duck-typed (we only read ``plugin.file_hints``
    and ``plugin.codemap`` if present) so this stays decoupled from
    ``pipeline.plugins``.

    Returns an empty string when nothing parseable is found — callers should
    treat that as "no codemap, do not inject".

    Output format (stable, line-oriented):

        # Repo map (project_dir/, depth=3, entries=N)
        path/to/file.cs
          class FooBar
          method FooBar.DoStuff
        path/to/other.py
          function helper
    """
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return ""

    langs = set(languages) if languages else None
    if plugin is not None:
        cm = getattr(plugin, "codemap", None)
        if isinstance(cm, dict):
            if "languages" in cm and not langs:
                langs = set(cm["languages"])
            md = cm.get("max_depth")
            if isinstance(md, int) and md > 0:
                max_depth = md

    # Default: every supported language. Plugins narrow this down.
    if not langs:
        langs = set(_PARSERS)

    by_file: dict[str, list[str]] = {}
    total_entries = 0

    for path in _walk(root, max_depth):
        if total_entries >= max_entries:
            break
        ext = path.suffix.lower()
        lang = _EXT_TO_LANG.get(ext)
        if lang is None or lang not in langs:
            continue
        parser = _PARSERS.get(lang)
        if parser is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        lines: list[str] = []
        for kind, name in parser(text):
            lines.append(f"  {kind} {name}")
            total_entries += 1
            if total_entries >= max_entries:
                break
        if lines:
            by_file[rel] = lines

    if not by_file:
        return ""

    header = (
        f"# Repo map ({root.name}/, depth={max_depth}, "
        f"entries={total_entries})"
    )
    body_lines: list[str] = [header]
    for rel in sorted(by_file):
        body_lines.append(rel)
        body_lines.extend(by_file[rel])
    return "\n".join(body_lines)


def inject_context(prompt: str, repo_map: str) -> str:
    """Append *repo_map* to *prompt* under a fenced ``REPO MAP`` block.

    Returns *prompt* unchanged when *repo_map* is falsy. Idempotent only by
    convention — callers shouldn't pipe a previously-injected prompt back
    through.
    """
    if not repo_map:
        return prompt
    block = (
        "\n\n--- REPO MAP (best-effort outline; full file access still available) ---\n"
        f"{repo_map}\n"
        "--- END REPO MAP ---"
    )
    return prompt + block


# ── Internals ─────────────────────────────────────────────────────────────────
def _walk(root: Path, max_depth: int) -> Iterable[Path]:
    """Depth-limited iter of files under *root*, skipping known build dirs."""
    yield from _walk_inner(root, root, max_depth)


def _walk_inner(root: Path, current: Path, depth_left: int) -> Iterable[Path]:
    if depth_left < 0:
        return
    try:
        entries = sorted(current.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                # Allow plugin file_hints like "_docs/" — we filter top-level
                # dotted dirs to keep .git/.venv out without listing them all.
                # _docs starts with underscore, not dot, so it's still walked.
                continue
            if depth_left == 0:
                continue
            yield from _walk_inner(root, entry, depth_left - 1)
        elif entry.is_file():
            yield entry
