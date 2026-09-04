"""core/infra/versions — installed Orcho distribution probe."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from core.infra import versions


def _dist(name: str, version: str | None) -> SimpleNamespace:
    return SimpleNamespace(metadata={"Name": name}, version=version)


def test_records_every_orcho_distribution_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        versions,
        "distributions",
        lambda: [
            _dist("requests", "2.0"),
            _dist("orcho_mcp", "0.8.2"),
            _dist("orcho-core", "0.9.0"),
            _dist("orcho", "0.9.0"),
            _dist("Orcho-Extra-Plugin", "1.0"),
        ],
    )

    assert versions.installed_orcho_versions() == {
        "orcho": "0.9.0",
        "orcho-core": "0.9.0",
        "orcho-extra-plugin": "1.0",
        "orcho-mcp": "0.8.2",
    }


def test_engine_key_always_present_when_uninstalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(versions, "distributions", lambda: [_dist("requests", "2.0")])

    def _missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(versions, "version", _missing)

    assert versions.installed_orcho_versions() == {
        "orcho-core": versions.UNKNOWN_VERSION,
    }


def test_first_seen_distribution_wins_and_missing_names_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        versions,
        "distributions",
        lambda: [
            SimpleNamespace(metadata=None, version="9"),
            _dist("orcho-core", "0.9.0"),
            _dist("orcho-core", "0.3.1"),
        ],
    )

    assert versions.installed_orcho_versions() == {"orcho-core": "0.9.0"}


def test_live_probe_reports_the_engine() -> None:
    found = versions.installed_orcho_versions()
    assert "orcho-core" in found
    assert all(isinstance(v, str) and v for v in found.values())
