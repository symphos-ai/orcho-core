"""Prepare an isolated worktree before agent phases run.

Worktree bootstraps cover project-local, gitignored prerequisites that a
fresh git worktree cannot contain: copied dependency folders, generated
native libraries, or package-manager installs. Core owns the portable action
contract; project plugins own the concrete commands.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class WorktreeBootstrapError(RuntimeError):
    """Raised when a configured worktree bootstrap step fails."""


def run_worktree_bootstrap(
    config: Any,
    *,
    source_root: Path,
    worktree_path: Path,
) -> dict[str, Any]:
    """Run a plugin-declared bootstrap against ``worktree_path``.

    Accepted shapes:

    * ``[]`` / ``None`` / ``False``: disabled.
    * ``{"enabled": false}``: disabled.
    * ``{"steps": [...]}``: list of steps.
    * ``[...]``: list of steps directly.

    Step examples:

    * ``{"copy": "libs"}`` copies ``source_root/libs`` to
      ``worktree_path/libs``.
    * ``{"run": ["composer", "install"]}`` runs a portable argv command in
      the worktree.
    * ``{"python": "scripts/bootstrap.py"}`` runs a tracked Python script.
    * ``{"shell": "composer install"}`` uses the platform shell explicitly.
    """
    steps = _normalise_steps(config)
    if not steps:
        return {"status": "skipped", "steps": []}

    source_root = source_root.resolve()
    worktree_path = worktree_path.resolve()
    records: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps, start=1):
        step = _require_mapping(raw_step, index)
        if not _platform_matches(step):
            records.append({
                "index": index,
                "action": _action_name(step),
                "status": "skipped",
                "reason": "platform mismatch",
            })
            continue
        records.append(
            _run_step(
                step,
                index=index,
                source_root=source_root,
                worktree_path=worktree_path,
            ),
        )
    return {"status": "ok", "steps": records}


def run_worktree_teardown(
    config: Any,
    *,
    source_root: Path,
    worktree_path: Path,
) -> dict[str, Any]:
    """Run a plugin-declared teardown against ``worktree_path`` (ADR 0131).

    Symmetric to :func:`run_worktree_bootstrap` — same step shapes — but
    **best-effort**: this runs at run finalization when the run is already
    terminal, so a failing step is recorded (``status="failed"``) and surfaced,
    never raised. A teardown failure must not mask the run's real outcome.

    The caller (finalization) is responsible for the lifecycle guarantee: invoke
    this only for a terminal run (never on a resumable pause, whose worktree —
    and external stack — must survive for resume), before the git worktree is
    released.
    """
    steps = _normalise_steps(config, key="worktree_teardown")
    if not steps:
        return {"status": "skipped", "steps": []}

    source_root = source_root.resolve()
    worktree_path = worktree_path.resolve()
    records: list[dict[str, Any]] = []
    failures = 0
    for index, raw_step in enumerate(steps, start=1):
        try:
            step = _require_mapping(raw_step, index)
            if not _platform_matches(step):
                records.append({
                    "index": index,
                    "action": _action_name(step),
                    "status": "skipped",
                    "reason": "platform mismatch",
                })
                continue
            records.append(
                _run_step(
                    step,
                    index=index,
                    source_root=source_root,
                    worktree_path=worktree_path,
                ),
            )
        except WorktreeBootstrapError as exc:
            # Best-effort: record and continue, never raise into finalization.
            failures += 1
            records.append({
                "index": index,
                "action": _action_name(raw_step) if isinstance(raw_step, Mapping) else "?",
                "status": "failed",
                "error": str(exc),
            })
    return {"status": "failed" if failures else "ok", "steps": records}


def _normalise_steps(config: Any, *, key: str = "worktree_bootstrap") -> list[Any]:
    if config in (None, False):
        return []
    if isinstance(config, list):
        return config
    if isinstance(config, tuple):
        return list(config)
    if isinstance(config, Mapping):
        if config.get("enabled") is False:
            return []
        if "steps" in config:
            steps = config["steps"]
            if not isinstance(steps, list | tuple):
                raise WorktreeBootstrapError(
                    f"{key}.steps must be a list",
                )
            return list(steps)
        return [dict(config)]
    raise WorktreeBootstrapError(
        f"{key} must be a list, dict, false, or null",
    )


def _require_mapping(raw_step: Any, index: int) -> Mapping[str, Any]:
    if not isinstance(raw_step, Mapping):
        raise WorktreeBootstrapError(
            f"worktree_bootstrap step {index} must be a dict",
        )
    return raw_step


def _run_step(
    step: Mapping[str, Any],
    *,
    index: int,
    source_root: Path,
    worktree_path: Path,
) -> dict[str, Any]:
    action = _action_name(step)
    if action == "copy":
        return _copy_step(
            step, index=index, source_root=source_root,
            worktree_path=worktree_path,
        )
    if action == "run":
        return _command_step(
            step, index=index, source_root=source_root,
            worktree_path=worktree_path,
        )
    if action == "python":
        return _python_step(
            step, index=index, source_root=source_root,
            worktree_path=worktree_path,
        )
    if action == "shell":
        return _shell_step(
            step, index=index, source_root=source_root,
            worktree_path=worktree_path,
        )
    raise WorktreeBootstrapError(
        f"worktree_bootstrap step {index} has no supported action",
    )


def _action_name(step: Mapping[str, Any]) -> str:
    raw = step.get("type")
    if isinstance(raw, str) and raw:
        if raw == "command":
            return "run"
        return raw
    for key, action in (
        ("copy", "copy"),
        ("run", "run"),
        ("command", "run"),
        ("python", "python"),
        ("shell", "shell"),
    ):
        if key in step:
            return action
    return "unknown"


def _copy_step(
    step: Mapping[str, Any],
    *,
    index: int,
    source_root: Path,
    worktree_path: Path,
) -> dict[str, Any]:
    raw_from = step.get("from", step.get("copy"))
    if not isinstance(raw_from, str) or not raw_from.strip():
        raise WorktreeBootstrapError(
            f"worktree_bootstrap copy step {index} needs a non-empty source",
        )
    raw_to = step.get("to") or raw_from
    if not isinstance(raw_to, str) or not raw_to.strip():
        raise WorktreeBootstrapError(
            f"worktree_bootstrap copy step {index} needs a non-empty target",
        )

    src = _resolve_under(source_root, raw_from, label="copy source")
    dst = _resolve_under(worktree_path, raw_to, label="copy target")
    overwrite = bool(step.get("overwrite", True))
    if not src.exists():
        raise WorktreeBootstrapError(
            f"worktree_bootstrap copy step {index} source does not exist: {src}",
        )
    if dst.exists() and not overwrite:
        raise WorktreeBootstrapError(
            f"worktree_bootstrap copy step {index} target exists: {dst}",
        )

    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=overwrite)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return {
        "index": index,
        "action": "copy",
        "status": "ok",
        "from": str(src),
        "to": str(dst),
    }


def _command_step(
    step: Mapping[str, Any],
    *,
    index: int,
    source_root: Path,
    worktree_path: Path,
) -> dict[str, Any]:
    raw_cmd = step.get("run", step.get("command", step.get("cmd")))
    cmd = _coerce_argv(raw_cmd, index=index)
    cwd = _resolve_command_cwd(
        step, source_root=source_root, worktree_path=worktree_path,
    )
    timeout = _timeout(step)
    _run_subprocess(cmd, cwd=cwd, timeout=timeout, index=index)
    return {
        "index": index,
        "action": "run",
        "status": "ok",
        "cmd": cmd,
        "cwd": str(cwd),
    }


def _python_step(
    step: Mapping[str, Any],
    *,
    index: int,
    source_root: Path,
    worktree_path: Path,
) -> dict[str, Any]:
    raw_script = step.get("python", step.get("script"))
    if not isinstance(raw_script, str) or not raw_script.strip():
        raise WorktreeBootstrapError(
            f"worktree_bootstrap python step {index} needs a script path",
        )
    root = source_root if step.get("root") == "source" else worktree_path
    script = _resolve_under(root, raw_script, label="python script")
    args = step.get("args", [])
    if not isinstance(args, Sequence) or isinstance(args, str | bytes):
        raise WorktreeBootstrapError(
            f"worktree_bootstrap python step {index} args must be a list",
        )
    cmd = [sys.executable, str(script), *(str(arg) for arg in args)]
    cwd = _resolve_command_cwd(
        step, source_root=source_root, worktree_path=worktree_path,
    )
    timeout = _timeout(step)
    _run_subprocess(cmd, cwd=cwd, timeout=timeout, index=index)
    return {
        "index": index,
        "action": "python",
        "status": "ok",
        "script": str(script),
        "cwd": str(cwd),
    }


def _shell_step(
    step: Mapping[str, Any],
    *,
    index: int,
    source_root: Path,
    worktree_path: Path,
) -> dict[str, Any]:
    raw_cmd = step.get("shell", step.get("cmd"))
    if not isinstance(raw_cmd, str) or not raw_cmd.strip():
        raise WorktreeBootstrapError(
            f"worktree_bootstrap shell step {index} needs a command string",
        )
    cwd = _resolve_command_cwd(
        step, source_root=source_root, worktree_path=worktree_path,
    )
    timeout = _timeout(step)
    try:
        result = subprocess.run(
            raw_cmd,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeBootstrapError(
            f"worktree_bootstrap shell step {index} timed out",
        ) from exc
    if result.returncode != 0:
        raise WorktreeBootstrapError(
            f"worktree_bootstrap shell step {index} failed "
            f"with exit code {result.returncode}",
        )
    return {
        "index": index,
        "action": "shell",
        "status": "ok",
        "cwd": str(cwd),
    }


def _coerce_argv(raw_cmd: Any, *, index: int) -> list[str]:
    if isinstance(raw_cmd, str):
        parts = shlex.split(raw_cmd, posix=os.name != "nt")
        if parts:
            return parts
    if (
        isinstance(raw_cmd, Sequence)
        and not isinstance(raw_cmd, str | bytes)
        and raw_cmd
    ):
        return [str(part) for part in raw_cmd]
    raise WorktreeBootstrapError(
        f"worktree_bootstrap run step {index} needs a non-empty command",
    )


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    index: int,
) -> None:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorktreeBootstrapError(
            f"worktree_bootstrap run step {index} command not found: {cmd[0]}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeBootstrapError(
            f"worktree_bootstrap run step {index} timed out",
        ) from exc
    if result.returncode != 0:
        raise WorktreeBootstrapError(
            f"worktree_bootstrap run step {index} failed "
            f"with exit code {result.returncode}",
        )


def _resolve_command_cwd(
    step: Mapping[str, Any],
    *,
    source_root: Path,
    worktree_path: Path,
) -> Path:
    raw = step.get("cwd", "worktree")
    if raw == "worktree":
        return worktree_path
    if raw == "source":
        return source_root
    if not isinstance(raw, str) or not raw.strip():
        raise WorktreeBootstrapError("worktree_bootstrap cwd must be a string")
    return _resolve_under(worktree_path, raw, label="command cwd")


def _resolve_under(root: Path, raw: str, *, label: str) -> Path:
    rel = Path(raw)
    if rel.is_absolute():
        raise WorktreeBootstrapError(
            f"worktree_bootstrap {label} must be relative: {raw}",
        )
    resolved = (root / rel).resolve()
    if not resolved.is_relative_to(root):
        raise WorktreeBootstrapError(
            f"worktree_bootstrap {label} escapes its root: {raw}",
        )
    return resolved


def _timeout(step: Mapping[str, Any]) -> int:
    raw = step.get("timeout", 600)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise WorktreeBootstrapError(
            "worktree_bootstrap timeout must be an integer",
        ) from exc
    if value <= 0:
        raise WorktreeBootstrapError(
            "worktree_bootstrap timeout must be positive",
        )
    return value


def _platform_matches(step: Mapping[str, Any]) -> bool:
    raw = step.get("platforms")
    if raw in (None, []):
        return True
    if isinstance(raw, str):
        values = {raw}
    elif isinstance(raw, Sequence) and not isinstance(raw, bytes):
        values = {str(item) for item in raw}
    else:
        raise WorktreeBootstrapError(
            "worktree_bootstrap platforms must be a string or list",
        )
    aliases = _platform_aliases()
    return bool(values & aliases)


def _platform_aliases() -> set[str]:
    aliases = {sys.platform}
    if os.name == "nt":
        aliases.update({"windows", "win32"})
    else:
        aliases.add("posix")
    if sys.platform.startswith("linux"):
        aliases.add("linux")
    if sys.platform == "darwin":
        aliases.add("darwin")
        aliases.add("macos")
    return aliases


__all__ = ["WorktreeBootstrapError", "run_worktree_bootstrap"]
