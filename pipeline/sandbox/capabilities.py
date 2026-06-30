"""
pipeline/sandbox/capabilities.py — host probe for L1 backend selection.

Run at run init by the orchestrator. Records the platform and the
presence of the only external dependency L1 actually uses
(:mod:`win32job` for the Windows Job Object backend). Other tools
that earlier drafts probed for (bwrap / sandbox-exec / podman /
docker / landlock) were placeholders for L2/L3/L4 backends that
this ADR explicitly does not build; probing for them would imply
that we plan to use them.

Detection is best-effort and never raises. An unprobeable
condition is reported as ``False`` — never crash a run on a
capability probe.
"""
from __future__ import annotations

import platform
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Frozen snapshot of the host's L1-relevant capabilities.

    * ``platform`` — :func:`platform.system` lowercased
      (``linux`` / ``darwin`` / ``windows``). Other values
      (``freebsd``, ``sunos``) pass through verbatim.
    * ``pywin32`` — ``win32job`` import succeeds. Required for the
      Windows Job Object backend; ``False`` on non-Windows by design.
    """
    platform: str
    pywin32: bool

    def to_manifest(self) -> dict[str, object]:
        """Plain-dict view for ``meta.json``.

        Lives next to the dataclass so the wire shape and the
        Python representation can not drift independently.
        """
        return asdict(self)


def _detect_pywin32() -> bool:
    """``True`` when :mod:`win32job` imports.

    Cheap: an import on Windows where pywin32 is installed; an
    ImportError elsewhere. The module is imported lazily here so
    non-Windows runs never carry the failed-import in their stack.
    """
    if platform.system().lower() != "windows":
        return False
    try:
        import win32job  # noqa: F401 — import for probe only
        return True
    except ImportError:
        return False


def detect_capabilities() -> Capabilities:
    """Probe the host once and return an immutable snapshot.

    Cheap enough to call per-run (one optional import on Windows,
    nothing on Unix). The orchestrator caches the result on the
    run manifest so consumers do not re-probe.
    """
    return Capabilities(
        platform=platform.system().lower(),
        pywin32=_detect_pywin32(),
    )


__all__ = [
    "Capabilities",
    "detect_capabilities",
]
