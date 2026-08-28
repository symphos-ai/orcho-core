"""
core/platform.py — Engine and workspace path resolution.

Two-tier engine model:
  STABLE ($ORCHO_CORE)     — production engine, used by all workspaces.
                             Default: ~/.local/share/multiagent-core
  DEV    ($ORCHO_CORE_DEV) — development copy for engine self-improvement.
                             Default: ~/www/mae/multiagent-core (if it exists)

Only one engine is ever *running* at a time (whichever Python process is active).
The env vars tell the running engine where *other* things (workspaces, output dirs)
live — they don't change which engine is running.

Env vars (in priority order):
  ORCHO_CORE        — path to the stable engine root
  ORCHO_CORE_DEV    — path to the dev engine copy
  ORCHO_WORKSPACE   — current workspace root (prompt overrides, runspace)
  ORCHO_RUNSPACE    — override for pipeline output directory

All functions return Path objects. All paths are resolved (absolute, no symlinks
unless the env var itself is a symlink — we honour the user's choice there).

Usage:
    from core.infra.platform import engine_home, workspace_dir, runspace_dir
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Runtime platform flag — use instead of repeating sys.platform == "win32" checks.
_IS_WINDOWS: bool = sys.platform == "win32"

# The directory containing THIS file is core/, so parent is multiagent-core/
# This is the "currently running" engine regardless of whether it is stable or dev.
_SELF_CORE_DIR: Path = Path(__file__).parent.parent


def venv_python_subpath() -> str:
    """Relative POSIX-style path to a venv's Python interpreter for this OS.

    ``.venv/Scripts/python.exe`` on Windows, ``.venv/bin/python`` elsewhere.
    Forward slashes so the result composes cleanly both into ``Path`` joins
    and into ``{checkout}``/``{project}`` placeholder templates.
    """
    if _IS_WINDOWS:
        return ".venv/Scripts/python.exe"
    return ".venv/bin/python"


def is_windows_apps_alias(path: str | os.PathLike[str] | None) -> bool:
    """True when *path* contains a ``WindowsApps`` segment (either separator).

    ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe`` is the Windows
    app-execution alias for the Store Python: launching it runs a virtualized
    process whose redirected ``LOCALAPPDATA`` cannot see this engine's managed
    workspace tree, so an interpreter resolving there is quietly unusable even
    though version/exists probes of it succeed. Pure path predicate
    (case-insensitive, both separators) so callers on any host can skip or
    flag such candidates without touching the filesystem.
    """
    if not path:
        return False
    segments = str(path).replace("\\", "/").split("/")
    return any(segment.lower() == "windowsapps" for segment in segments)


# ── Engine roots ──────────────────────────────────────────────────────────────

def engine_home() -> Path:
    """Return the root of the currently-configured stable engine.

    Priority:
      1. $ORCHO_CORE env var  (explicit override — takes precedence always)
      2. Fallback to the directory that contains this running code.

    Note: the *running* engine is always ``_SELF_CORE_DIR``.  ``engine_home()``
    tells callers where to point users / where to write shared state — this is
    useful when the engine is invoked directly from a dev copy but still wants
    to report/use the stable path.
    """
    if env := os.environ.get("ORCHO_CORE"):
        return Path(env)
    return _SELF_CORE_DIR


def engine_dev_home() -> Path | None:
    """Return the root of the dev engine copy, or None if not configured.

    Priority:
      1. $ORCHO_CORE_DEV env var
      2. None

    Used by the ``orcho develops orcho`` dogfooding workflow:
      - Stable engine runs pipeline on the dev copy as its project.
      - ``orcho-dev`` alias overrides $ORCHO_CORE to point to the dev copy so
        the dev engine itself handles the next invocation.
    """
    if env := os.environ.get("ORCHO_CORE_DEV"):
        return Path(env)
    return None


# ── Workspace ────────────────────────────────────────────────────────────────

def workspace_dir() -> Path | None:
    """Return the current workspace root (prompt overrides and runspace).

    The only source is the ``$ORCHO_WORKSPACE`` env var. The legacy
    embedded mode (where the orcho engine sat as a sibling of the
    workspace and resolved it via walk-up) was removed deliberately: the
    engine repo itself has ``.orcho/multiagent/`` and ``worktree/`` (it
    runs on itself), which made walk-up unable to tell "engine" from
    "workspace" — the pipeline silently wrote artifacts into the engine
    repo.

    Callers must check for None and fail with a clear error
    (see ``runspace_dir`` → WorkspaceNotResolvedError).

    A workspace is a thin directory alongside the engine that holds:
      - ``.orcho/multiagent/prompts/``   — prompt overrides
      - ``runspace/``                    — pipeline output (runs/, …)
      - ``orcho-env.sh``                 — workspace env script
    """
    if env := os.environ.get("ORCHO_WORKSPACE"):
        return Path(env)
    return None


# ── Runspace (pipeline output) ───────────────────────────────────────────────

class WorkspaceNotResolvedError(RuntimeError):
    """Raised when no workspace can be resolved.

    Pipeline runtime MUST never write into the orcho engine repo. If the
    caller didn't pass --workspace and didn't set $ORCHO_WORKSPACE and
    no workspace env can be resolved, we fail loudly so the user
    notices and provides one explicitly.
    """


_WORKSPACE_HINT = (
    "No orcho workspace resolved. Pass --workspace /path/to/workspace-orchestrator "
    "or set $ORCHO_WORKSPACE. Run output must live in your workspace, never in "
    "the orcho engine itself."
)


def runspace_dir() -> Path:
    """Return the pipeline output directory.

    Priority:
      1. $ORCHO_RUNSPACE env var     (explicit override)
      2. $ORCHO_WORKSPACE/runspace   (workspace-local output)

    When neither of the above resolves — raise
    WorkspaceNotResolvedError. There used to be a fallback to
    ``engine_home()/runspace`` that littered the engine repo with
    artifacts; it was removed deliberately.
    """
    if env := os.environ.get("ORCHO_RUNSPACE"):
        return Path(env)

    ws = workspace_dir()
    if ws:
        return ws / "runspace"

    raise WorkspaceNotResolvedError(_WORKSPACE_HINT)


# ── Default engine home ──────────────────────────────────────────────────────

def default_engine_home() -> Path:
    """Platform-appropriate default path for the stable engine install.

    - macOS / Linux: ``~/.local/share/orcho-core``
    - Windows:       ``%LOCALAPPDATA%\\orcho-core``

    This is a *recommendation* for installers and docs, not enforced at runtime.
    The actual engine home is always resolved via ``engine_home()``.
    """
    if _IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
    else:
        base = Path.home() / ".local" / "share"
    return base / "orcho-core"


# ── Binary candidates ─────────────────────────────────────────────────────────

def claude_candidates() -> list[str]:
    """Ordered list of candidate paths for the Claude CLI binary.

    On Windows the Node.js CLI is installed as a ``.cmd`` shim that must be
    launched via ``cmd /c`` (handled in the agent layer).  We return the
    ``.cmd`` path here so ``shutil.which`` / ``Path.exists`` can find it.
    """
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        return [
            rf"{appdata}\npm\claude.cmd",
            rf"{localappdata}\Programs\claude\claude.exe",
            r"C:\Program Files\Claude\claude.exe",
        ]
    return [
        "~/.local/bin/claude",
        "~/.nvm/versions/node/v22.12.0/bin/claude",
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]


def codex_candidates() -> list[str]:
    """Ordered list of candidate paths for the Codex CLI binary."""
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        return [
            rf"{appdata}\npm\codex.cmd",
            rf"{localappdata}\Programs\codex\codex.exe",
            r"C:\Program Files\Codex\codex.exe",
        ]
    return [
        "~/.nvm/versions/node/v22.12.0/bin/codex",
        "~/.local/bin/codex",
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
    ]


def gemini_candidates() -> list[str]:
    """Ordered list of candidate paths for the Google Gemini CLI binary.

    Targets the ``@google/gemini-cli`` Node.js package. On Windows the
    npm install lands as a ``.cmd`` shim that must be launched via
    ``cmd /c`` (handled in the agent layer)."""
    if _IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        return [
            rf"{appdata}\npm\gemini.cmd",
            rf"{localappdata}\Programs\gemini\gemini.exe",
            r"C:\Program Files\Gemini\gemini.exe",
        ]
    return [
        "~/.nvm/versions/node/v22.12.0/bin/gemini",
        "~/.local/bin/gemini",
        "/usr/local/bin/gemini",
        "/opt/homebrew/bin/gemini",
    ]
