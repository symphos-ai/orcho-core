"""Launch a fresh cross-project run from a mono auto-detect directive.

When ``orcho run`` auto-detect recommends a cross topology and the operator
picks 'Start cross run', the current mono invocation must not convert itself
into a cross run in place (the F2 invariant: the mono run never starts). Instead
it resolves the projected project aliases to on-disk repo paths and launches a
*fresh* cross process, carrying the task through automatically. Replacing the
process — rather than mutating the current one — is what preserves F2.

Alias → path resolution is deliberately provider-neutral and config-free: repo
checkouts live next to each other, so an alias resolves to a sibling directory
of the current project (``<current>/../<alias>``). The current project's own
alias resolves to its actual path. Any alias that does not resolve to an
existing directory is prompted for once on an interactive run; a run that cannot
resolve every path stops with a manual-launch hint instead of guessing.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path

from core.io.ansi import C, get_color_enabled, is_color_active, paint
from core.io.journey_prompt import bold

# How many characters of the task text to echo in the launch banner before
# eliding. The full task is always forwarded to the cross process unchanged.
_TASK_PREVIEW = 60

# Keep this helper import-light: ``pipeline.project.auto_detect`` owns the same
# env name, but importing project dispatch here would make the launch helper less
# leaf-like than it needs to be.
_WORK_MODE_ENV = "ORCHO_WORK_MODE"


def resolve_project_paths(
    projects: Sequence[str],
    current_project: str,
    *,
    interactive: bool,
    prompt_fn: Callable[[str], str],
) -> dict[str, Path] | None:
    """Resolve each projected alias to an on-disk repo path, or ``None``.

    The current project's own alias (the basename of ``current_project``)
    resolves to its actual path. Every other alias resolves to a sibling
    directory of the current project. A missing sibling is prompted for once via
    ``prompt_fn`` on an interactive run. Returns ``None`` as soon as any alias
    cannot be resolved to an existing directory — the caller must not guess a
    path for a cross run.
    """

    current = Path(current_project).expanduser().resolve()
    current_alias = current.name
    base = current.parent
    resolved: dict[str, Path] = {}
    for alias in projects:
        if alias == current_alias:
            resolved[alias] = current
            continue
        sibling = base / alias
        if sibling.is_dir():
            resolved[alias] = sibling.resolve()
            continue
        if interactive:
            entered = (prompt_fn(alias) or "").strip()
            if entered:
                candidate = Path(entered).expanduser().resolve()
                if candidate.is_dir():
                    resolved[alias] = candidate
                    continue
        return None
    return resolved


def build_cross_argv(
    pairs: dict[str, Path],
    task: str,
    *,
    profile: str | None = None,
    model: str | None = None,
    mock: bool = False,
) -> list[str]:
    """Build the ``orcho-cross`` argv from resolved ``alias:path`` pairs.

    Carries the task through with ``--task`` and forwards the mono run's
    ``--model`` / ``--mock`` when set, so the operator does not retype anything.
    """

    argv: list[str] = ["--projects"]
    argv += [f"{alias}:{path}" for alias, path in pairs.items()]
    argv += ["--task", task]
    if profile:
        argv += ["--profile", profile]
    if model:
        argv += ["--model", model]
    if mock:
        argv.append("--mock")
    return argv


def _default_launch(argv: list[str]) -> int:
    """Run the cross pipeline in a fresh interpreter and return its exit code.

    A new process — not an in-process ``main()`` call — is what keeps this mono
    invocation from ever becoming a cross run (F2). ``sys.executable -c`` avoids
    depending on the ``orcho-cross`` console script being on ``PATH``, so it
    works under editable / source checkouts too.
    """

    proc = subprocess.run(  # noqa: S603 - fixed interpreter + our own argv
        [
            sys.executable,
            "-c",
            "import sys; from pipeline.cross_project.cli import main; "
            "sys.exit(main() or 0)",
            *argv,
        ],
        check=False,
    )
    return proc.returncode


def _prompt_for_path(alias: str, *, color: bool) -> str:
    """Ask once for a repo path when an alias has no sibling directory."""

    try:
        return input(bold(f"Path for '{alias}' repo: ", color=color))
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _task_preview(task: str) -> str:
    """A single-line, length-bounded echo of the task for the banner."""

    flattened = " ".join(task.split())
    if len(flattened) > _TASK_PREVIEW:
        return flattened[:_TASK_PREVIEW].rstrip() + "…"
    return flattened


def _format_command(
    pairs: dict[str, Path],
    task: str,
    *,
    profile: str | None = None,
    model: str | None = None,
    mock: bool = False,
    work_mode: str | None = None,
) -> str:
    """Human-readable echo of the cross command that is about to launch."""

    argv = ["orcho", "cross", "--projects"]
    argv += [f"{alias}:{path}" for alias, path in pairs.items()]
    argv += ["--task", _task_preview(task)]
    if profile:
        argv += ["--profile", profile]
    if model:
        argv += ["--model", model]
    if mock:
        argv.append("--mock")
    if work_mode:
        argv = [f"{_WORK_MODE_ENV}={work_mode}", *argv]
    return shlex.join(argv)


def _format_manual_hint(projects: Sequence[str]) -> str:
    """Fallback ``orcho cross`` template for the unresolved-path case."""

    args = " ".join(f"{alias}:<path>" for alias in projects)
    return f"orcho cross --projects {args} --task '...'"


@contextmanager
def _scoped_env(key: str, value: str | None):
    """Temporarily set one env var while the fresh child process starts."""

    previous = os.environ.get(key)
    if value:
        os.environ[key] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def launch_cross_from_directive(
    *,
    projects: Sequence[str],
    task: str,
    current_project: str,
    profile: str | None = None,
    work_mode: str | None = None,
    model: str | None = None,
    mock: bool = False,
    interactive: bool,
    color: bool | None = None,
    prompt_fn: Callable[[str], str] | None = None,
    launch_fn: Callable[[list[str]], int] | None = None,
) -> int:
    """Resolve the projected projects and launch a fresh cross run.

    Returns the cross process's exit code, or ``2`` when the repo paths cannot
    be resolved (a manual-launch hint is printed to stderr in that case).
    ``prompt_fn`` / ``launch_fn`` are injectable for tests.
    """

    if color is None:
        resolved = get_color_enabled()
        color = resolved if resolved is not None else is_color_active()
    if prompt_fn is None:
        prompt_fn = lambda alias: _prompt_for_path(alias, color=color)  # noqa: E731
    if launch_fn is None:
        launch_fn = _default_launch

    pairs = resolve_project_paths(
        projects, current_project, interactive=interactive, prompt_fn=prompt_fn,
    )
    if not pairs or len(pairs) < 2:
        print(
            paint(
                "Could not resolve repo paths for every project in the cross "
                "run. Start it manually with the resolved paths:",
                C.YELLOW,
            ),
            file=sys.stderr,
        )
        print(f"  {_format_manual_hint(projects)}", file=sys.stderr)
        return 2

    print(bold("Starting cross run:", color=color))
    print(
        "  "
        + _format_command(
            pairs,
            task,
            profile=profile,
            model=model,
            mock=mock,
            work_mode=work_mode,
        )
    )
    argv = build_cross_argv(
        pairs, task, profile=profile, model=model, mock=mock,
    )
    with _scoped_env(_WORK_MODE_ENV, work_mode):
        return launch_fn(argv)


__all__ = [
    "build_cross_argv",
    "launch_cross_from_directive",
    "resolve_project_paths",
]
