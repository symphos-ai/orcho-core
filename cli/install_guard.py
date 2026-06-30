"""CLI install-provenance guards.

These checks run before the main CLI imports the orchestration stack.  They
must stay stdlib-only and side-effect free except for the intentional
operator-facing stop path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ALLOW_ENV = "ORCHO_ALLOW_RETAINED_WORKTREE_INSTALL"

# Console-script entrypoints declared in ``pyproject.toml``. The guard only
# stops these — importing the package for tests, tooling, or ``python -m``
# debugging must never trip it.
_CONSOLE_SCRIPTS = frozenset({"orcho", "orcho-run", "orcho-cross"})


def _is_truthy_env(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in {
        "", "0", "false", "no", "off",
    }


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _looks_like_retained_worktree_checkout(path: Path) -> bool:
    """Return True for Orcho-retained run worktree checkouts.

    The check is structural, not machine-specific:

    ``.../runspace/worktrees/<any-name>/checkout``

    It intentionally does not require ``workspace-orchestrator`` in the path so
    custom workspace names and future embedders keep the same protection.
    """
    resolved = path.resolve()
    if resolved.name != "checkout":
        return False
    parts = resolved.parts
    for idx, part in enumerate(parts):
        if part != "runspace":
            continue
        if idx + 3 >= len(parts):
            continue
        if parts[idx + 1] == "worktrees" and idx + 3 == len(parts) - 1:
            return True
    return False


def _looks_like_console_entrypoint(executable: Path) -> bool:
    """Return True when ``executable`` is an Orcho console script.

    The guard targets the global ``orcho`` / ``orcho-run`` / ``orcho-cross``
    console scripts that import a retained worktree. A test runner (``pytest``),
    interpreter (``python``), or ``python -m`` invocation must not trip it, so
    we match the entrypoint basename rather than firing for any import.
    """
    name = executable.name
    # Tolerate platform suffixes such as Windows' ``orcho.exe``.
    return name in _CONSOLE_SCRIPTS or executable.stem in _CONSOLE_SCRIPTS


def _retained_worktree_install_message(core_dir: Path) -> str:
    return "\n".join([
        "ORCHO_RETAINED_WORKTREE_INSTALL",
        "",
        "The `orcho` executable is importing Orcho from a retained run "
        "worktree:",
        f"  {core_dir}",
        "",
        "Retained run worktrees are delivery artifacts, not durable install "
        "roots. This usually means a global Python environment was previously "
        "`pip install -e`'d against an Orcho run checkout, so the CLI can run "
        "stale code after that run is over.",
        "",
        "Fix the Python environment by reinstalling Orcho from the intended "
        "stable or development checkout, for example:",
        "  python -m pip install --force-reinstall --no-deps -e "
        "/path/to/orcho-core",
        "",
        "Diagnose the current editable target with:",
        "  python -m pip show orcho-core",
        "",
        "To intentionally debug this retained checkout, run the module from "
        "inside the checkout instead:",
        f"  cd {core_dir}",
        "  python -m cli.orcho --help",
        "",
        f"Emergency bypass: set {_ALLOW_ENV}=1.",
    ])


def guard_against_retained_worktree_install(
    core_dir: Path,
    *,
    argv0: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Stop global console scripts that import a retained run worktree.

    ``python -m cli.orcho`` from inside the retained checkout is allowed: that
    is an explicit local debug action and does not depend on a stale global
    entrypoint. A console script from another environment importing this path is
    almost always an accidental editable-install leak.
    """
    env_map = os.environ if env is None else env
    if _is_truthy_env(env_map.get(_ALLOW_ENV)):
        return

    resolved_core = core_dir.resolve()
    if not _looks_like_retained_worktree_checkout(resolved_core):
        return

    executable = Path(argv0 if argv0 is not None else sys.argv[0])
    if _is_under(executable, resolved_core):
        return

    # Only the global console scripts are accidental stale-install leaks. Plain
    # imports (pytest collection, ``python -m`` debugging, tooling) must pass.
    if not _looks_like_console_entrypoint(executable):
        return

    print(_retained_worktree_install_message(resolved_core), file=sys.stderr)
    raise SystemExit(2)


__all__ = [
    "guard_against_retained_worktree_install",
]
