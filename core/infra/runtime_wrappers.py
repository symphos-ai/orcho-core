from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


class RuntimeWrapperError(ValueError):
    """Raised when a runtime-wrapper command cannot be completed."""


@dataclass(frozen=True)
class RuntimeWrapperInstallResult:
    runtime: str
    path: Path
    already_current: bool
    on_path: bool
    env_var: str


_WRAPPER_FILES = {
    "claude-glm": "claude-glm.sh",
}

_RUNTIME_BIN_ENV = {
    "claude-glm": "CLAUDE_GLM_BIN",
}


def runtime_wrapper_names() -> tuple[str, ...]:
    return tuple(sorted(_WRAPPER_FILES))


def runtime_wrapper_env_var(runtime: str) -> str:
    _validate_runtime(runtime)
    return _RUNTIME_BIN_ENV[runtime]


def runtime_wrapper_default_path(runtime: str) -> Path:
    _validate_runtime(runtime)
    return Path.home() / ".local" / "bin" / runtime


def runtime_wrapper_script(runtime: str) -> str:
    filename = _WRAPPER_FILES[_validate_runtime(runtime)]
    return (
        resources.files("core._runtime_wrappers")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def install_runtime_wrapper(
    runtime: str,
    *,
    destination: Path | None = None,
    force: bool = False,
) -> RuntimeWrapperInstallResult:
    runtime = _validate_runtime(runtime)
    path = (destination or runtime_wrapper_default_path(runtime)).expanduser()
    if path.exists() and path.is_dir():
        raise RuntimeWrapperError(f"{path} is a directory")

    script = runtime_wrapper_script(runtime)
    already_current = False
    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeWrapperError(
                f"{path} already exists and is not a text wrapper; pass --force to replace it"
            ) from exc
        if current == script:
            already_current = True
        elif not force:
            raise RuntimeWrapperError(f"{path} already exists; pass --force to replace it")

    if not already_current:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script, encoding="utf-8")
    path.chmod(
        stat.S_IRUSR
        | stat.S_IWUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )
    return RuntimeWrapperInstallResult(
        runtime=runtime,
        path=path,
        already_current=already_current,
        on_path=_path_parent_on_path(path),
        env_var=runtime_wrapper_env_var(runtime),
    )


def _validate_runtime(runtime: str) -> str:
    if runtime not in _WRAPPER_FILES:
        names = ", ".join(runtime_wrapper_names())
        raise RuntimeWrapperError(f"unknown runtime wrapper {runtime!r}; available: {names}")
    return runtime


def _path_parent_on_path(path: Path) -> bool:
    parent = _resolve_for_compare(path.parent)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and _resolve_for_compare(Path(entry).expanduser()) == parent:
            return True
    return False


def _resolve_for_compare(path: Path) -> Path:
    return path.resolve(strict=False)
