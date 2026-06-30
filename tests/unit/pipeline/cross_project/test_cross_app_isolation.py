"""ADR 0047 Phase D — AST guards on the typed cross app boundary.

Three load-bearing invariants the typed boundary depends on:

1. **`app.py` does NOT import from `orchestrator`** — the typed boundary
   owns the body; the legacy 23-kwarg wrapper in
   :mod:`pipeline.cross_project.orchestrator` routes THROUGH
   :func:`pipeline.cross_project.app.run_cross_project_pipeline`. The
   reverse direction would re-create the cycle Phase D exists to break.

2. **`app_types.py` does NOT import from `orchestrator`** — the reviewer
   pre-flight check for Phase D. Phase C r1 broke the
   ``CROSS_DEFAULT_PROFILE`` edge via
   :mod:`pipeline.cross_project.constants`; this test pins the
   absence of that edge so a "convenient" re-import doesn't quietly
   re-introduce it.

3. **`run_cross_project_pipeline` never reaches `sys.exit`** — the
   typed boundary returns / raises; it does NOT translate failures
   into exit codes. That's the CLI's job (today in
   ``orchestrator.main``, in Phase G in ``cli.py``). AST guard
   walks the body and forbids any ``sys.exit`` call.

Plus the inherited ADR 0042 stop #9 invariant: cross modules never
import from ``pipeline.project.cli`` (the project CLI is a leaf).
"""

from __future__ import annotations

import ast
from pathlib import Path

CROSS_PROJECT_DIR = Path("pipeline/cross_project")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports_from(tree: ast.Module, *banned_modules: str) -> list[str]:
    """Return the offending import lines if the module imports from any
    of ``banned_modules`` (full dotted path match)."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in banned_modules:
                violations.append(
                    f"line {node.lineno}: from {node.module} import "
                    f"{', '.join(a.name for a in node.names)}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in banned_modules:
                    violations.append(
                        f"line {node.lineno}: import {alias.name}"
                    )
    return violations


# ── Test 1: app.py is a leaf (no orchestrator import) ─────────────────


def test_app_does_not_import_orchestrator() -> None:
    """ADR 0047 D2 — typed boundary owns the body; the orchestrator
    routes THROUGH the typed boundary. A reverse-import would
    recreate the cycle Phase D exists to break.

    Phase F — the run coordinator moved to ``session_run.py``; the same
    no-orchestrator-import invariant must hold there too (the coordinator
    is the body's new home and an orchestrator import would re-create the
    cycle just the same)."""
    for module in ("app.py", "session_run.py"):
        tree = _parse(CROSS_PROJECT_DIR / module)
        violations = _imports_from(tree, "pipeline.cross_project.orchestrator")
        assert not violations, (
            f"pipeline/cross_project/{module} must not import from "
            "pipeline.cross_project.orchestrator. Direction (Phase D D2):\n"
            "  orchestrator (legacy wrapper) → app (typed boundary) → "
            "session_run (coordinator) → app_types / rendering / "
            "planning_loop / project_dispatch / terminal / checkpoint / "
            "usage / constants.\n"
            "Violations:\n  " + "\n  ".join(violations)
        )


# ── Test 2: app_types.py is a leaf (no orchestrator import) ───────────


def test_app_types_does_not_import_orchestrator() -> None:
    """Reviewer pre-flight check before the Phase D green-light. Phase
    C r1 broke the ``CROSS_DEFAULT_PROFILE`` edge from app_types →
    orchestrator via the new ``constants.py`` leaf. This test pins
    that absence so a future "convenient" import doesn't silently
    bring the cycle back."""
    tree = _parse(CROSS_PROJECT_DIR / "app_types.py")
    violations = _imports_from(tree, "pipeline.cross_project.orchestrator")
    assert not violations, (
        "pipeline/cross_project/app_types.py must not import from "
        "pipeline.cross_project.orchestrator. The DTO types are a "
        "leaf peer; orchestrator-side constants live in "
        "pipeline.cross_project.constants.\n"
        "Violations:\n  " + "\n  ".join(violations)
    )


# ── Test 3: rendering.py is a leaf (no orchestrator import) ───────────


def test_rendering_does_not_import_orchestrator() -> None:
    """Phase B established this invariant; pin it alongside the Phase D
    AST guards so all four leaf peer modules (rendering, app_types,
    constants, app itself) are checked in one place."""
    tree = _parse(CROSS_PROJECT_DIR / "rendering.py")
    violations = _imports_from(tree, "pipeline.cross_project.orchestrator")
    assert not violations, (
        "pipeline/cross_project/rendering.py must not import from "
        "pipeline.cross_project.orchestrator (ADR 0047 D3 — rendering "
        "is a leaf).\n"
        "Violations:\n  " + "\n  ".join(violations)
    )


# ── Test 4: constants.py is a stdlib-only leaf ────────────────────────


def test_constants_is_stdlib_only_leaf() -> None:
    """``constants.py`` must depend on nothing internal — that's the
    whole reason it exists. If any orcho-internal import appears
    here, the cycle precondition Phase C r1 fixed is back."""
    tree = _parse(CROSS_PROJECT_DIR / "constants.py")
    forbidden_prefixes = (
        "pipeline.", "agents.", "core.", "sdk.",
    )
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod.startswith(p) for p in forbidden_prefixes):
                violations.append(f"line {node.lineno}: from {mod} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in forbidden_prefixes):
                    violations.append(
                        f"line {node.lineno}: import {alias.name}"
                    )
    assert not violations, (
        "pipeline/cross_project/constants.py must be a stdlib-only "
        "leaf — the cycle-break precondition relies on it.\n"
        "Violations:\n  " + "\n  ".join(violations)
    )


# ── Test 5: no sys.exit reachable from typed boundary ─────────────────


def test_run_cross_project_pipeline_does_not_call_sys_exit() -> None:
    """ADR 0047 stop #7 — the typed boundary never calls ``sys.exit``.
    That's the CLI's job (today ``orchestrator.main``, Phase G's
    ``cli.py``). The typed boundary returns / raises; exit codes are
    the legacy CLI wrapper's concern.

    Walks the AST of ``pipeline.cross_project.app`` AND the Phase F run
    coordinator ``pipeline.cross_project.session_run`` looking for any
    ``sys.exit(...)`` call inside the module (helpers + boundary +
    private body)."""
    for module in ("app.py", "session_run.py"):
        tree = _parse(CROSS_PROJECT_DIR / module)
        bad_lines: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Pattern: ``sys.exit(...)``
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "sys"
                    and func.attr == "exit"
                ) or isinstance(func, ast.Name) and func.id == "exit":
                    bad_lines.append(node.lineno)
        assert not bad_lines, (
            f"pipeline/cross_project/{module} must not reach sys.exit / "
            f"exit. Found sys.exit calls at lines: {bad_lines}. "
            "The typed boundary returns CrossRunResult or raises; exit-code "
            "mapping belongs to the CLI leaf (orchestrator.main today, "
            "cli.py after Phase G)."
        )


# ── Test 6: no pipeline.project.cli reach (inherited ADR 0042 stop #9) ─


def test_no_pipeline_project_cli_reach_from_cross() -> None:
    """ADR 0042 stop #9 — cross MUST NOT depend on the project CLI
    leaf. This guard re-runs across every cross-project module to
    catch a regression at any level."""
    cross_modules = [
        p for p in CROSS_PROJECT_DIR.glob("*.py")
        if p.name not in {"__init__.py"}
    ]
    violations: list[str] = []
    for path in cross_modules:
        tree = _parse(path)
        for v in _imports_from(tree, "pipeline.project.cli"):
            violations.append(f"{path}:{v}")
    assert not violations, (
        "Cross modules must not import from pipeline.project.cli "
        "(ADR 0042 stop #9 — project CLI is a leaf).\n"
        "Violations:\n  " + "\n  ".join(violations)
    )


# ── Test 7: no cross peer module back-imports app (Stage 2) ───────────


def test_no_cross_peer_imports_app() -> None:
    """Stage 2 layering — the typed boundary ``app`` depends on the
    focused setup/domain leaves (``profile_setup`` / ``run_setup`` /
    ``agent_setup`` / ``contract_check`` / ``usage`` / ``planning_loop`` /
    ``project_dispatch`` / ``cfa_gate`` / ``final_acceptance`` / …), never
    the reverse. A peer importing ``pipeline.cross_project.app`` recreates
    an ``app → peer → app`` cycle and re-couples a leaf to the boundary.

    The only sanctioned importer is ``orchestrator`` — the legacy
    back-compat wrapper whose whole job is to route THROUGH
    ``run_cross_project_pipeline`` (direction asserted by Test 1)."""
    allowed = {"app.py", "orchestrator.py", "__init__.py"}
    peer_modules = [
        p for p in CROSS_PROJECT_DIR.glob("*.py")
        if p.name not in allowed
    ]
    violations: list[str] = []
    for path in peer_modules:
        tree = _parse(path)
        for v in _imports_from(tree, "pipeline.cross_project.app"):
            violations.append(f"{path.name}: {v}")
    assert not violations, (
        "Cross peer modules must not import from "
        "pipeline.cross_project.app — that re-creates an app → peer → "
        "app back-import. Import shared helpers from their leaf home "
        "(e.g. usage wrappers from pipeline.cross_project.usage).\n"
        "Violations:\n  " + "\n  ".join(violations)
    )
