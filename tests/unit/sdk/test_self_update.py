"""Install-provenance detection and upgrade planning (`sdk/self_update.py`).

Every branch is driven against a synthetic prefix, so the tests describe the
contract rather than the machine they run on: the same assertions hold on a
pipx laptop, a uv-tool container, and CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk.self_update import (
    InstallProvenance,
    _direct_url_sources,
    _environment_distributions,
    detect_provenance,
    plan_upgrade,
)


def _pipx_venv(root: Path, *, package: str = "orcho", suffix: str = "") -> Path:
    prefix = root / "venvs" / package
    prefix.mkdir(parents=True)
    (prefix / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": package, "suffix": suffix}}),
        encoding="utf-8",
    )
    return prefix


def _uv_tool_venv(root: Path, *, package: str = "orcho") -> Path:
    prefix = root / "tools" / package
    prefix.mkdir(parents=True)
    (prefix / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    return prefix


def _always_found(name: str) -> str:
    return f"/usr/bin/{name}"


def _never_found(name: str) -> None:
    return None


class TestDetectProvenance:
    """Which installer owns the environment the CLI is running from."""

    def test_no_installed_distribution_is_a_source_checkout(self, tmp_path: Path) -> None:
        provenance = detect_provenance(prefix=tmp_path, base_prefix=tmp_path, packages=())

        assert provenance.manager == "source"
        assert provenance.package == ""

    def test_pipx_venv_detected_from_its_metadata_marker(self, tmp_path: Path) -> None:
        prefix = _pipx_venv(tmp_path)

        provenance = detect_provenance(
            prefix=prefix, base_prefix=tmp_path, packages=("orcho",),
        )

        assert provenance.manager == "pipx"
        assert provenance.package == "orcho"

    def test_pipx_install_suffix_is_part_of_the_package_name(self, tmp_path: Path) -> None:
        """pipx addresses a suffixed install by its suffixed name."""
        prefix = _pipx_venv(tmp_path, package="orcho", suffix="_dev")

        provenance = detect_provenance(
            prefix=prefix, base_prefix=tmp_path, packages=("orcho",),
        )

        assert provenance.package == "orcho_dev"

    def test_unreadable_pipx_metadata_falls_back_to_the_venv_name(
        self, tmp_path: Path,
    ) -> None:
        prefix = tmp_path / "venvs" / "orcho"
        prefix.mkdir(parents=True)
        (prefix / "pipx_metadata.json").write_text("{not json", encoding="utf-8")

        provenance = detect_provenance(
            prefix=prefix, base_prefix=tmp_path, packages=("orcho",),
        )

        assert provenance.manager == "pipx"
        assert provenance.package == "orcho"

    def test_uv_tool_venv_detected_from_its_receipt(self, tmp_path: Path) -> None:
        prefix = _uv_tool_venv(tmp_path)

        provenance = detect_provenance(
            prefix=prefix, base_prefix=tmp_path, packages=("orcho",),
        )

        assert provenance.manager == "uv-tool"
        assert provenance.package == "orcho"

    def test_plain_virtualenv_is_distinguished_from_the_base_interpreter(
        self, tmp_path: Path,
    ) -> None:
        prefix = tmp_path / "venv"
        prefix.mkdir()

        provenance = detect_provenance(
            prefix=prefix, base_prefix=tmp_path / "base", packages=("orcho",),
        )

        assert provenance.manager == "venv-pip"

    def test_base_interpreter_reports_plain_pip(self, tmp_path: Path) -> None:
        provenance = detect_provenance(
            prefix=tmp_path, base_prefix=tmp_path, packages=("orcho",),
        )

        assert provenance.manager == "pip"

    def test_convenience_distribution_is_preferred_over_the_engine(
        self, tmp_path: Path,
    ) -> None:
        provenance = detect_provenance(
            prefix=tmp_path, base_prefix=tmp_path, packages=("orcho", "orcho-core"),
        )

        assert provenance.package == "orcho"

    def test_engine_is_the_target_when_the_convenience_package_is_absent(
        self, tmp_path: Path,
    ) -> None:
        provenance = detect_provenance(
            prefix=tmp_path, base_prefix=tmp_path, packages=("orcho-core",),
        )

        assert provenance.package == "orcho-core"


class TestPlanUpgrade:
    """The command each manager needs, and whether Orcho may run it."""

    def test_pipx_install_upgrades_through_pipx(self, tmp_path: Path) -> None:
        provenance = InstallProvenance(manager="pipx", package="orcho", prefix=tmp_path)

        plan = plan_upgrade(provenance=provenance, which=_always_found)

        assert plan.command == ("pipx", "upgrade", "orcho")
        assert plan.auto_runnable

    def test_uv_tool_install_upgrades_through_uv(self, tmp_path: Path) -> None:
        provenance = InstallProvenance(manager="uv-tool", package="orcho", prefix=tmp_path)

        plan = plan_upgrade(provenance=provenance, which=_always_found)

        assert plan.command == ("uv", "tool", "upgrade", "orcho")
        assert plan.auto_runnable

    @pytest.mark.parametrize("manager", ["venv-pip", "pip"])
    def test_pip_installs_upgrade_with_their_own_interpreter(
        self, tmp_path: Path, manager: str,
    ) -> None:
        """A venv must be upgraded by its own python, never by whatever is on PATH."""
        provenance = InstallProvenance(manager=manager, package="orcho", prefix=tmp_path)

        plan = plan_upgrade(
            provenance=provenance, python="/env/bin/python", which=_always_found,
        )

        assert plan.command == (
            "/env/bin/python", "-m", "pip", "install", "--upgrade", "orcho",
        )
        assert plan.auto_runnable

    def test_source_checkout_has_no_command_and_points_at_the_checkout(
        self, tmp_path: Path,
    ) -> None:
        provenance = InstallProvenance(manager="source", package="", prefix=tmp_path)

        plan = plan_upgrade(provenance=provenance, which=_always_found)

        assert plan.command == ()
        assert not plan.auto_runnable
        assert "source checkout" in plan.blocked_reason

    def test_editable_install_is_never_upgraded_by_a_package_manager(
        self, tmp_path: Path,
    ) -> None:
        """The checkout is the upgrade unit; pip would fight it."""
        provenance = InstallProvenance(
            manager="editable",
            package="orcho-core",
            prefix=tmp_path,
            editable_source="file:///work/orcho-core",
        )

        plan = plan_upgrade(provenance=provenance, which=_always_found)

        assert plan.command == ()
        assert not plan.auto_runnable
        assert "/work/orcho-core" in plan.hint

    def test_missing_manager_binary_prints_the_command_instead_of_running_it(
        self, tmp_path: Path,
    ) -> None:
        provenance = InstallProvenance(manager="pipx", package="orcho", prefix=tmp_path)

        plan = plan_upgrade(provenance=provenance, which=_never_found)

        assert plan.command == ("pipx", "upgrade", "orcho")
        assert not plan.auto_runnable
        assert "pipx" in plan.blocked_reason

    def test_locally_built_install_is_not_overwritten_without_intent(
        self, tmp_path: Path,
    ) -> None:
        """An index upgrade would silently discard locally built code."""
        provenance = InstallProvenance(
            manager="pipx",
            package="orcho",
            prefix=tmp_path,
            local_source="file:///work/orcho-dist",
        )

        plan = plan_upgrade(provenance=provenance, which=_always_found)

        assert plan.command == ("pipx", "upgrade", "orcho")
        assert not plan.auto_runnable
        assert "/work/orcho-dist" in plan.blocked_reason

    def test_unknown_manager_yields_a_reason_rather_than_a_guess(
        self, tmp_path: Path,
    ) -> None:
        provenance = InstallProvenance(manager="conda", package="orcho", prefix=tmp_path)

        plan = plan_upgrade(provenance=provenance, which=_always_found)

        assert plan.command == ()
        assert not plan.auto_runnable
        assert "conda" in plan.blocked_reason


class _FakeDistribution:
    """Minimal stand-in for ``importlib.metadata.Distribution``.

    Only the three members discovery touches are modelled: the name, where the
    metadata lives, and ``direct_url.json``.
    """

    def __init__(self, name: str, location: Path, direct_url: str | None = None) -> None:
        self.metadata = {"Name": name}
        self._location = location
        self._direct_url = direct_url

    def locate_file(self, _path: str) -> Path:
        return self._location

    def read_text(self, filename: str) -> str | None:
        return self._direct_url if filename == "direct_url.json" else None


class TestEnvironmentDistributions:
    """Which copy of a distribution counts as "the install"."""

    def test_metadata_outside_every_site_directory_is_not_an_install(
        self, tmp_path: Path,
    ) -> None:
        """A build leaves `*.egg-info` in the checkout; that is not an install.

        Without this rule the CLI reads that metadata and names a package
        manager that does not own the code being executed.
        """
        checkout = tmp_path / "checkout"
        checkout.mkdir()

        found = _environment_distributions(
            tmp_path / "prefix",
            discovered=[_FakeDistribution("orcho-core", checkout)],
        )

        assert found == {}

    def test_installed_copy_wins_over_a_shadowing_checkout_copy(
        self, tmp_path: Path,
    ) -> None:
        """An editable install puts the checkout on `sys.path`, egg-info and all.

        A by-name lookup returns whichever copy `sys.path` reaches first, so
        discovery must scan and keep the one that is really installed.
        """
        prefix = tmp_path / "prefix"
        site_packages = prefix / "lib" / "site-packages"
        site_packages.mkdir(parents=True)
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        installed = _FakeDistribution(
            "orcho-core", site_packages, '{"url": "file:///checkout", "dir_info": {"editable": true}}',
        )

        found = _environment_distributions(
            prefix,
            discovered=[_FakeDistribution("orcho-core", checkout), installed],
        )

        assert found == {"orcho-core": installed}

    def test_distribution_names_are_matched_after_normalisation(
        self, tmp_path: Path,
    ) -> None:
        """Metadata may spell the name `orcho_core`; it is the same distribution."""
        prefix = tmp_path / "prefix"
        prefix.mkdir()

        found = _environment_distributions(
            prefix, discovered=[_FakeDistribution("Orcho_Core", prefix)],
        )

        assert list(found) == ["orcho-core"]

    def test_convenience_distribution_is_ordered_before_the_engine(
        self, tmp_path: Path,
    ) -> None:
        prefix = tmp_path / "prefix"
        prefix.mkdir()

        found = _environment_distributions(
            prefix,
            discovered=[
                _FakeDistribution("orcho-core", prefix),
                _FakeDistribution("orcho", prefix),
            ],
        )

        assert list(found) == ["orcho", "orcho-core"]


class TestDirectUrlSources:
    """Reading PEP 610 install provenance off a distribution."""

    def test_editable_marker_is_reported_as_an_editable_source(
        self, tmp_path: Path,
    ) -> None:
        dist = _FakeDistribution(
            "orcho-core", tmp_path,
            '{"url": "file:///work/orcho-core", "dir_info": {"editable": true}}',
        )

        assert _direct_url_sources([dist]) == ("file:///work/orcho-core", "")

    def test_non_editable_path_install_is_reported_as_a_local_build(
        self, tmp_path: Path,
    ) -> None:
        dist = _FakeDistribution(
            "orcho", tmp_path, '{"url": "file:///work/orcho-dist", "dir_info": {}}',
        )

        assert _direct_url_sources([dist]) == ("", "file:///work/orcho-dist")

    def test_index_install_has_no_direct_url_evidence(self, tmp_path: Path) -> None:
        assert _direct_url_sources([_FakeDistribution("orcho", tmp_path)]) == ("", "")

    def test_malformed_metadata_is_treated_as_no_evidence(self, tmp_path: Path) -> None:
        dist = _FakeDistribution("orcho", tmp_path, "{not json")

        assert _direct_url_sources([dist]) == ("", "")

    def test_an_editable_install_dominates_a_local_build(self, tmp_path: Path) -> None:
        """The checkout is authoritative for the code actually executing."""
        sources = _direct_url_sources([
            _FakeDistribution(
                "orcho", tmp_path, '{"url": "file:///dist", "dir_info": {}}',
            ),
            _FakeDistribution(
                "orcho-core", tmp_path,
                '{"url": "file:///core", "dir_info": {"editable": true}}',
            ),
        ])

        assert sources == ("file:///core", "file:///dist")


class TestLiveDetection:
    """The zero-argument path used by the CLI must work on this interpreter."""

    def test_detection_reports_a_known_manager_without_arguments(self) -> None:
        provenance = detect_provenance()

        assert provenance.manager in {
            "source", "editable", "pipx", "uv-tool", "venv-pip", "pip",
        }

    def test_planning_never_raises_on_the_live_interpreter(self) -> None:
        """`orcho update` must produce a plan, not a traceback, anywhere."""
        plan = plan_upgrade()

        assert plan.auto_runnable or plan.blocked_reason
