"""ADR 0047 Phase G — AST guards for the cross CLI leaf.

After Phase G the cross CLI surface (``main`` + ``print_error`` +
``_resolve_cross_resume_latest``) lives in
:mod:`pipeline.cross_project.cli`. These guards pin the direction:

  * **No non-CLI module under ``pipeline.cross_project`` imports
    from ``pipeline.cross_project.cli``.** The CLI is the leaf — any
    reverse edge would mean a non-CLI peer reaches into argparse /
    process-exit code, breaking the typed-boundary contract that
    ADR 0047 Phase D introduced (the body in ``app.py`` must not
    reach ``sys.exit``).

  * **``app.py`` does not call ``sys.exit``** (re-pinned from Phase
    D — Phase G added a new ``cli`` peer module so the guard is
    re-checked against the post-G layout). Status decisions live in
    the silent service / ``CrossFinalizationResult``; exit codes are
    the CLI's responsibility.

  * **No cross module imports ``pipeline.project.cli``.** ADR 0042
    stop #9 inherited: cross-project must not reach into the
    single-project CLI module (it's a peer, not a parent).

  * **``rendering.py`` does not import ``cli``.** The render surface
    is a leaf peer (Phase B); the CLI module is also a leaf in the
    other direction. The render module must not pull in CLI deps.

These tests parse the candidate modules with the stdlib :mod:`ast`
module so the guard does not require importing the module under
test (which would by definition load every import edge it pins
against).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_CROSS_DIR = Path(__file__).resolve().parents[4] / "pipeline" / "cross_project"


def _imports_from(tree: ast.AST, prefix: str) -> list[str]:
    """Return every ``import``/``from … import …`` target under ``tree``
    whose module name starts with ``prefix`` (matches the module
    exactly OR a sub-package). Mirrors the helper in
    ``test_cross_app_isolation.py``."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == prefix or alias.name.startswith(f"{prefix}."):
                    out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == prefix or mod.startswith(f"{prefix}."):
                out.append(mod)
    return out


def _parse(name: str) -> ast.Module:
    """Parse ``pipeline/cross_project/<name>.py`` into an AST tree."""
    return ast.parse((_CROSS_DIR / f"{name}.py").read_text(encoding="utf-8"))


# Every non-CLI module under ``pipeline/cross_project/`` that must
# stay isolated from the CLI leaf. Update this list when new
# non-CLI peers ship (Phase H end-to-end tests audit the full set).
#
# ``orchestrator`` is intentionally NOT in this list: it is the
# back-compat shim that re-exports ``main`` + ``print_error`` from
# :mod:`pipeline.cross_project.cli` for the legacy patch surface
# (~30 tests under ``tests/unit/cli/test_cross_orchestrator_main.py``
# + the ``sdk.runner.run_cross_from_args`` bridge during the Phase
# G → Phase I transition). The orchestrator module's role is to
# host that re-export; pinning it under the leaf-isolation guard
# would create a deliberate contradiction. The dedicated
# :func:`test_cli_main_is_callable_from_canonical_module` guard
# pins identity for the re-export so legacy patches keep landing on
# the actual callee.
_NON_CLI_PEERS = (
    "app",
    "app_types",
    "artifact_bundle",
    "checkpoint",
    "constants",
    "final_acceptance",
    "finalization",
    "gate_decisions",
    "gate_entries",
    "handoff",
    "handoff_payloads",
    "plan_parser",
    "planning_loop",
    "profile_projection",
    "project_dispatch",
    "prompts",
    "rendering",
    "terminal",
    "types",
    "usage",
)


@pytest.mark.parametrize("module", _NON_CLI_PEERS)
def test_non_cli_peer_does_not_import_cross_cli(module: str) -> None:
    """The cross CLI is the leaf. Non-CLI peers must not reach into
    ``pipeline.cross_project.cli`` — that would let process-exit
    logic flow back into the typed-boundary body or the render
    surface."""
    if not (_CROSS_DIR / f"{module}.py").exists():
        pytest.skip(f"{module}.py not present in this checkout")
    tree = _parse(module)
    violations = _imports_from(tree, "pipeline.cross_project.cli")
    assert violations == [], (
        f"pipeline.cross_project.{module} imports from "
        f"pipeline.cross_project.cli: {violations!r}. The CLI is the "
        f"leaf — non-CLI peers must NOT pull in argparse / sys.exit "
        f"logic. Move the helper to a non-CLI module, or extract a "
        f"non-CLI surface the CLI then wraps."
    )


def test_app_does_not_reach_sys_exit() -> None:
    """ADR 0047 stop #8 — ``run_cross_project_pipeline`` must NOT
    call ``sys.exit``. Status decisions live in the silent service
    (`CrossFinalizationResult.status`); exit codes are the CLI's
    job. A `sys.exit` reachable from the body would also break
    embedded callers (SDK, MCP)."""
    tree = _parse("app")
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sys"
            and node.func.attr == "exit"
        ):
            violations.append(f"sys.exit at line {node.lineno}")
    assert violations == [], (
        f"pipeline.cross_project.app reaches sys.exit: {violations!r}. "
        f"That belongs in pipeline.cross_project.cli (status → exit "
        f"code mapping). Move the call, or surface the failure via "
        f"the structured result instead."
    )


def test_no_cross_module_imports_project_cli() -> None:
    """ADR 0042 stop #9 inherited — cross-project must not reach
    into the single-project CLI module. The cross CLI is a peer
    leaf, not a child of project CLI."""
    for module_path in _CROSS_DIR.glob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        violations = _imports_from(tree, "pipeline.project.cli")
        assert violations == [], (
            f"pipeline.cross_project.{module_path.stem} imports from "
            f"pipeline.project.cli: {violations!r}. Cross is a peer "
            f"of project — not a child. If you need a shared helper, "
            f"extract it to a non-CLI module both can import."
        )


def test_rendering_does_not_import_cli() -> None:
    """The render surface (Phase B leaf) must stay free of CLI
    dependencies. Adding a CLI edge here would couple every silent
    render path to argparse / sys.exit / process-exit logic."""
    tree = _parse("rendering")
    violations = _imports_from(tree, "pipeline.cross_project.cli")
    assert violations == [], (
        f"pipeline.cross_project.rendering imports from "
        f"pipeline.cross_project.cli: {violations!r}. Rendering is a "
        f"leaf peer (Phase B invariant); it MUST NOT pull in the "
        f"CLI module."
    )


def test_package_root_import_does_not_load_cli_module() -> None:
    """ADR 0047 Phase G r1 — eager-load tail closed.

    ``import pipeline.cross_project`` and ``from pipeline.cross_project
    import orchestrator`` MUST NOT pull
    :mod:`pipeline.cross_project.cli` into :data:`sys.modules`. The
    orchestrator back-compat shim re-exports ``main`` / ``print_error``
    lazily via PEP 562 ``__getattr__`` so consumers that only need
    the run-body or prompt builders do not pay for argparse +
    ``sys.exit`` import cost. Drift back to eager
    ``from pipeline.cross_project.cli import main, print_error`` at
    module top would re-introduce the ``RuntimeWarning: ... found in
    sys.modules`` the reviewer flagged after Phase G r0.

    Subprocess-isolated so module-import caching from earlier tests
    does not mask a regression."""
    import subprocess
    import sys
    rc = subprocess.run(
        [
            sys.executable, "-c",
            "import sys, pipeline.cross_project;"
            " assert 'pipeline.cross_project.cli' not in sys.modules,"
            " f'cli loaded by package-root import: {sorted(k for k in sys.modules if k.startswith(\"pipeline.cross_project\"))}';"
            " from pipeline.cross_project import orchestrator;"
            " assert 'pipeline.cross_project.cli' not in sys.modules,"
            " f'cli loaded by orchestrator import: {sorted(k for k in sys.modules if k.startswith(\"pipeline.cross_project\"))}';"
            " _ = orchestrator.main;"
            " assert 'pipeline.cross_project.cli' in sys.modules,"
            " 'orchestrator.main attribute access must trigger the lazy cli import'",
        ],
        capture_output=True, text=True, check=False,
    )
    assert rc.returncode == 0, (
        f"package-root import smoke failed.\nstdout: {rc.stdout}\n"
        f"stderr: {rc.stderr}"
    )


def test_cli_main_is_callable_from_canonical_module() -> None:
    """Sanity: the canonical home for ``main`` is now
    :mod:`pipeline.cross_project.cli`. Existing back-compat
    re-export through ``orchestrator`` must resolve to the same
    object — otherwise patches on the legacy path silently miss the
    actual callee (the trap that broke ``sdk.runner`` before the
    Phase G hash backfill)."""
    from pipeline.cross_project import cli as xcli, orchestrator as xo

    assert callable(xcli.main)
    assert xo.main is xcli.main, (
        "orchestrator.main must be the very same object as cli.main "
        "(re-export, not a wrapper). Any divergence breaks 30+ "
        "existing patches under tests/unit/cli/."
    )
    assert xo.print_error is xcli.print_error
