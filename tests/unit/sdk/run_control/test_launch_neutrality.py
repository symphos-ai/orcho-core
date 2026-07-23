"""AST neutrality guard for :mod:`sdk.run_control.launch`.

The detached-launch surface is framework-neutral *by construction*: any
embedder (an MCP server, a TUI, a plain CLI wrapper) must be able to wrap
it without inheriting an event loop or a terminal framework. Concretely,
``sdk/run_control/launch.py`` must not import:

* ``asyncio`` — concurrency / reaping is the embedder's policy, not core's;
* ``textual`` — no terminal-UI framework may leak into the launch core;
* anything under ``orcho_mcp`` — core owns the protocol, the MCP server is
  one downstream embedder and the edge only points core → nothing.

Mirrors the AST-guard style of
``tests/unit/pipeline/cross_project/test_cross_cli_isolation.py::
test_no_cross_module_imports_project_cli``: the module is parsed with the
stdlib :mod:`ast` so the guard never has to *import* the file (which would
by definition load every edge it pins against).
"""
from __future__ import annotations

import ast
from pathlib import Path

_LAUNCH_PATH = (
    Path(__file__).resolve().parents[4] / "sdk" / "run_control" / "launch.py"
)

# Top-level module names (or dotted prefixes) the neutral launch surface
# must never import.
_BANNED_EXACT = frozenset({"asyncio", "textual"})
_BANNED_PREFIXES = ("orcho_mcp",)


def _banned_imports(tree: ast.AST) -> list[str]:
    """Return every ``import`` / ``from … import …`` target under ``tree``
    that names a banned module (exact match or dotted sub-package)."""
    out: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        # Only absolute imports carry a module we can classify; relative
        # imports (level > 0) stay inside the sdk package by definition.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for name in names:
            head = name.split(".", 1)[0]
            if head in _BANNED_EXACT or any(
                name == p or name.startswith(f"{p}.") for p in _BANNED_PREFIXES
            ):
                out.append(name)
    return out


def test_launch_module_is_framework_neutral() -> None:
    """``launch.py`` imports no asyncio, no textual, nothing from orcho_mcp."""
    tree = ast.parse(_LAUNCH_PATH.read_text(encoding="utf-8"))
    violations = _banned_imports(tree)
    assert violations == [], (
        f"sdk/run_control/launch.py imports banned neutrality-breaking "
        f"modules: {violations!r}. The detached-launch surface must stay "
        f"framework-neutral — concurrency (asyncio), terminal UI (textual), "
        f"and the MCP server (orcho_mcp) are downstream embedder concerns. "
        f"Move any such dependency into the embedder."
    )


def test_guard_detects_an_injected_asyncio_import() -> None:
    """The guard actually bites: if someone adds ``import asyncio`` to
    ``launch.py`` the detector flags it.

    Run against the real module source with a synthetic ``import asyncio``
    prepended so this test proves the guard would fail the module — not a
    hand-rolled toy AST that could drift from the real check.
    """
    injected = "import asyncio\n" + _LAUNCH_PATH.read_text(encoding="utf-8")
    violations = _banned_imports(ast.parse(injected))
    assert "asyncio" in violations, (
        "neutrality guard failed to detect an injected `import asyncio` — "
        "the guard is not actually protecting launch.py."
    )
