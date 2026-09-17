"""Install-provenance detection and upgrade planning for the Orcho CLI.

Orcho ships as an ordinary Python distribution, so "how do I upgrade?" has no
single answer: the correct command depends on the installer that owns the
environment the CLI is running from. This module resolves that ownership from
observable on-disk evidence and returns a typed :class:`UpgradePlan`.

It is a read-only planner. Nothing here installs, spawns a subprocess, or
mutates the environment; ``cli/_update_cli.py`` owns that side of the journey.

Recognised managers, in resolution order:

``source``
    Running from a checkout with no installed distribution metadata. There is
    no package to upgrade; the source tree is the upgrade unit.
``editable``
    An editable (``pip install -e``) install. The checkout is authoritative,
    so a package-manager upgrade would be the wrong action.
``pipx``
    A pipx-managed venv, identified by ``pipx_metadata.json`` at the venv root.
``uv-tool``
    A ``uv tool`` venv, identified by ``uv-receipt.toml`` at the venv root.
``venv-pip``
    Any other virtual environment; upgrade with that venv's own interpreter.
``pip``
    A base/system/user interpreter.

Detection is structural rather than machine-specific, so custom install roots,
relocated tool directories, and future embedders keep working.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import site
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Final

#: Distribution preferred as the upgrade target. ``orcho`` is the convenience
#: distribution that pulls in the engine; when it is absent the engine
#: distribution is the thing the operator actually installed.
_PREFERRED_PACKAGE: Final[str] = "orcho"
_ENGINE_PACKAGE: Final[str] = "orcho-core"

#: Marker files that identify a tool-manager-owned venv from its root.
_PIPX_MARKER: Final[str] = "pipx_metadata.json"
_UV_TOOL_MARKER: Final[str] = "uv-receipt.toml"

#: Every value :func:`detect_provenance` can report as ``manager``. Renderers
#: use it to prove they have a label for each case rather than discovering a
#: gap in front of an operator.
KNOWN_MANAGERS: Final[frozenset[str]] = frozenset({
    "source", "editable", "pipx", "uv-tool", "venv-pip", "pip",
})


@dataclass(frozen=True, slots=True)
class InstallProvenance:
    """Where the running Orcho CLI was installed from, and by what."""

    manager: str
    package: str
    prefix: Path
    #: Checkout backing an editable install, when the install is editable.
    editable_source: str = ""
    #: Local directory/URL a non-editable install was built from, when the
    #: install did not come from a package index. Upgrading such an install
    #: replaces locally built code with the published release.
    local_source: str = ""

    @property
    def is_local_build(self) -> bool:
        """True when installed code came from a path rather than an index."""
        return bool(self.editable_source or self.local_source)


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    """The upgrade command for one resolved install, and whether to run it."""

    provenance: InstallProvenance
    #: argv of the upgrade command. Empty when no command applies.
    command: tuple[str, ...] = ()
    #: True when Orcho may execute ``command`` without further confirmation.
    auto_runnable: bool = False
    #: Why the plan is print-only. Empty when :attr:`auto_runnable` is True.
    blocked_reason: str = ""
    #: Operator-facing guidance for a print-only plan.
    hint: str = ""


def _normalize(name: str) -> str:
    """Normalize a distribution name for comparison (PEP 503)."""
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _site_roots(prefix: Path) -> frozenset[Path]:
    """Directories whose distribution metadata belongs to this environment.

    ``importlib.metadata`` searches all of ``sys.path``, which routinely
    contains directories that are not installed locations: the current
    directory, and — for an editable install — the checkout itself. Both can
    hold a stale ``*.egg-info`` left by a build. Answering "how do I upgrade?"
    from that metadata would name a package manager that does not own the code
    being executed, so candidates are restricted to real site directories.
    ``site`` is used rather than ``prefix`` alone so ``pip install --user``,
    whose site directory sits outside the interpreter prefix, still counts.
    """
    roots = {prefix.resolve()}
    for candidate in (*site.getsitepackages(), site.getusersitepackages()):
        with contextlib.suppress(OSError, ValueError):
            roots.add(Path(candidate).resolve())
    return frozenset(roots)


def _in_roots(path: Path, roots: frozenset[Path]) -> bool:
    """True when ``path`` is one of ``roots`` or lives inside one."""
    return any(path == root or root in path.parents for root in roots)


def _environment_distributions(
    prefix: Path,
    *,
    discovered: Iterable[metadata.Distribution] | None = None,
) -> dict[str, metadata.Distribution]:
    """Orcho distributions actually installed into ``prefix``'s environment.

    All distributions are enumerated rather than resolved by name: a
    name lookup returns whichever copy ``sys.path`` reaches first, which is the
    checkout's ``*.egg-info`` whenever a checkout is importable — precisely the
    case an editable install creates. Scanning lets the installed copy win over
    a shadowing one instead of losing to it.

    Keys are the canonical Orcho distribution names, ordered with the
    convenience distribution first so the upgrade target is the package the
    operator most likely installed.
    """
    roots = _site_roots(prefix)
    wanted = {_normalize(name): name for name in (_PREFERRED_PACKAGE, _ENGINE_PACKAGE)}
    found: dict[str, metadata.Distribution] = {}

    population = metadata.distributions() if discovered is None else discovered
    for dist in population:
        try:
            raw_name = dist.metadata["Name"] or ""
        except (KeyError, TypeError):
            continue
        canonical = wanted.get(_normalize(raw_name))
        if canonical is None or canonical in found:
            continue
        try:
            located = Path(str(dist.locate_file(""))).resolve()
        except OSError:
            continue
        if _in_roots(located, roots):
            found[canonical] = dist

    return {
        name: found[name]
        for name in (_PREFERRED_PACKAGE, _ENGINE_PACKAGE)
        if name in found
    }


def _read_direct_url(dist: metadata.Distribution) -> dict:
    """Return a distribution's ``direct_url.json``, or ``{}``.

    Written by pip (PEP 610) for anything not installed from an index. Missing,
    unreadable, and malformed metadata all mean "no direct URL evidence", which
    is the ordinary index-install case.
    """
    try:
        raw = dist.read_text("direct_url.json")
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _direct_url_sources(
    distributions: Iterable[metadata.Distribution],
) -> tuple[str, str]:
    """Return ``(editable_source, local_source)`` across ``distributions``.

    An editable install anywhere in the Orcho set dominates: the checkout is
    authoritative for the code being executed. Otherwise a plain local build
    (``pip install ./path``) is reported so the caller can warn that upgrading
    discards it.
    """
    editable = ""
    local = ""
    for dist in distributions:
        direct_url = _read_direct_url(dist)
        url = str(direct_url.get("url", ""))
        if not url:
            continue
        dir_info = direct_url.get("dir_info")
        is_editable = isinstance(dir_info, dict) and bool(dir_info.get("editable"))
        if is_editable and not editable:
            editable = url
        elif not is_editable and not local:
            local = url
    return editable, local


def _pipx_package(prefix: Path) -> str:
    """Return the pipx main-package name for ``prefix``, or ``""``.

    The venv directory name is a good guess but not authoritative — pipx
    supports install suffixes — so the recorded metadata wins when readable.
    """
    marker = prefix / _PIPX_MARKER
    try:
        parsed = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return prefix.name
    main = parsed.get("main_package")
    if not isinstance(main, dict):
        return prefix.name
    package = str(main.get("package") or "")
    suffix = str(main.get("suffix") or "")
    if not package:
        return prefix.name
    return f"{package}{suffix}"


def detect_provenance(
    *,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
    packages: tuple[str, ...] | None = None,
    sources: tuple[str, str] | None = None,
) -> InstallProvenance:
    """Resolve how the running CLI was installed.

    Arguments default to the live interpreter's state. They are injectable so
    tests can drive every branch against a synthetic prefix: passing
    ``packages`` replaces distribution discovery entirely, and ``sources``
    supplies the ``(editable, local)`` pair discovery would have read from
    ``direct_url.json``.
    """
    resolved_prefix = Path(prefix if prefix is not None else sys.prefix)
    resolved_base = Path(base_prefix if base_prefix is not None else sys.base_prefix)
    if packages is None:
        installed = _environment_distributions(resolved_prefix)
        resolved_packages = tuple(installed)
        editable_source, local_source = _direct_url_sources(installed.values())
    else:
        resolved_packages = packages
        editable_source, local_source = sources or ("", "")

    if not resolved_packages:
        return InstallProvenance(
            manager="source", package="", prefix=resolved_prefix,
        )

    target = resolved_packages[0]

    if editable_source:
        return InstallProvenance(
            manager="editable",
            package=target,
            prefix=resolved_prefix,
            editable_source=editable_source,
        )

    if (resolved_prefix / _PIPX_MARKER).is_file():
        return InstallProvenance(
            manager="pipx",
            package=_pipx_package(resolved_prefix),
            prefix=resolved_prefix,
            local_source=local_source,
        )

    if (resolved_prefix / _UV_TOOL_MARKER).is_file():
        return InstallProvenance(
            manager="uv-tool",
            package=resolved_prefix.name,
            prefix=resolved_prefix,
            local_source=local_source,
        )

    manager = "venv-pip" if resolved_prefix != resolved_base else "pip"
    return InstallProvenance(
        manager=manager,
        package=target,
        prefix=resolved_prefix,
        local_source=local_source,
    )


def _upgrade_command(provenance: InstallProvenance, *, python: str) -> tuple[str, ...]:
    """Return the manager-appropriate upgrade argv for ``provenance``."""
    if provenance.manager == "pipx":
        return ("pipx", "upgrade", provenance.package)
    if provenance.manager == "uv-tool":
        return ("uv", "tool", "upgrade", provenance.package)
    if provenance.manager in {"venv-pip", "pip"}:
        return (python, "-m", "pip", "install", "--upgrade", provenance.package)
    return ()


def plan_upgrade(
    *,
    provenance: InstallProvenance | None = None,
    python: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> UpgradePlan:
    """Build the upgrade plan for the current (or given) install.

    A plan is ``auto_runnable`` only when a concrete command exists, the
    manager binary is resolvable, and running it would not silently discard
    locally built code. Every other case is print-only with a reason, which is
    the honest answer for a checkout, an editable install, or a locally built
    install that an index upgrade would overwrite.
    """
    resolved = detect_provenance() if provenance is None else provenance
    resolved_python = python or sys.executable
    lookup = shutil.which if which is None else which

    if resolved.manager == "source":
        return UpgradePlan(
            provenance=resolved,
            blocked_reason="running from a source checkout with no installed Orcho distribution",
            hint="Update the checkout itself (for example `git pull`), then reinstall it.",
        )

    if resolved.manager == "editable":
        return UpgradePlan(
            provenance=resolved,
            blocked_reason="this is an editable install; the checkout is the upgrade unit",
            hint=(
                "Update the checkout backing this install "
                f"({resolved.editable_source}), for example with `git pull`."
            ),
        )

    command = _upgrade_command(resolved, python=resolved_python)
    if not command:
        return UpgradePlan(
            provenance=resolved,
            blocked_reason=f"no upgrade command is known for install manager {resolved.manager!r}",
            hint="Upgrade the `orcho` distribution with the package manager that installed it.",
        )

    if lookup(command[0]) is None:
        return UpgradePlan(
            provenance=resolved,
            command=command,
            blocked_reason=f"`{command[0]}` was not found on PATH",
            hint=f"Install `{command[0]}`, or run the command above from an environment that has it.",
        )

    if resolved.local_source:
        return UpgradePlan(
            provenance=resolved,
            command=command,
            blocked_reason=(
                "this install was built from a local path "
                f"({resolved.local_source}), not from a package index"
            ),
            hint=(
                "Upgrading replaces that locally built code with the published release. "
                "Run the command above to do that deliberately."
            ),
        )

    return UpgradePlan(provenance=resolved, command=command, auto_runnable=True)


__all__ = [
    "KNOWN_MANAGERS",
    "InstallProvenance",
    "UpgradePlan",
    "detect_provenance",
    "plan_upgrade",
]
