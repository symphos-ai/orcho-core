"""Host capability detector — best-effort, never raises."""
from __future__ import annotations

import platform

from pipeline.sandbox.capabilities import Capabilities, detect_capabilities


def test_detect_returns_capabilities_for_this_host() -> None:
    caps = detect_capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.platform == platform.system().lower()
    assert isinstance(caps.pywin32, bool)


def test_manifest_view_is_plain_dict() -> None:
    caps = detect_capabilities()
    m = caps.to_manifest()
    assert isinstance(m, dict)
    assert m["platform"] == caps.platform
    assert "pywin32" in m


def test_pywin32_false_on_non_windows() -> None:
    caps = detect_capabilities()
    if platform.system().lower() != "windows":
        assert caps.pywin32 is False


def test_capabilities_surface_is_narrow() -> None:
    """Detector reports only what L1 uses. Probing bwrap /
    sandbox-exec / podman / docker would imply we plan to use
    them — and we don't."""
    caps = detect_capabilities()
    manifest = caps.to_manifest()
    assert set(manifest.keys()) == {"platform", "pywin32"}