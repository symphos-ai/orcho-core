"""Drift guard for the public SDK contract.

`docs/sdk_schema.json` is the committed snapshot of `sdk.__all__` and
every public dataclass / callable / exception. Any change to the SDK
surface (added export, renamed parameter, changed default, removed
field) flips this test red.

When the change is intentional, regenerate the snapshot:

    python tools/dump_sdk_schema.py

Then commit `docs/sdk_schema.json` together with the SDK change.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _REPO_ROOT / "docs" / "sdk_schema.json"
_DUMPER_PATH = _REPO_ROOT / "tools" / "dump_sdk_schema.py"


def test_snapshot_exists() -> None:
    assert _SCHEMA_PATH.exists(), (
        f"{_SCHEMA_PATH.relative_to(_REPO_ROOT)} missing. "
        "Generate with: python tools/dump_sdk_schema.py"
    )


def test_dumper_check_mode_passes() -> None:
    """Run the dumper in --check mode; any drift fails the test.

    Routed through subprocess so the dumper exercises its own CLI
    contract (the same contract a CI hook or `pre-commit` would use).
    """
    result = subprocess.run(
        [sys.executable, str(_DUMPER_PATH), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"sdk schema drift detected.\n"
        f"stderr:\n{result.stderr}\n"
        "Regenerate the snapshot with:\n"
        "  python tools/dump_sdk_schema.py"
    )


def test_snapshot_covers_full_all() -> None:
    """The snapshot's export list must equal `sdk.__all__` exactly."""
    import sdk

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    snapshot_names = sorted(e["name"] for e in schema["exports"])
    expected = sorted(sdk.__all__)
    assert snapshot_names == expected, (
        "sdk.__all__ and the snapshot have diverged.\n"
        f"in __all__ but not in snapshot: {set(expected) - set(snapshot_names)}\n"
        f"in snapshot but not in __all__: {set(snapshot_names) - set(expected)}\n"
        "Regenerate with: python tools/dump_sdk_schema.py"
    )


def test_every_exception_has_exit_code() -> None:
    """Every exception export carries a numeric `exit_code` for CLI mapping."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    for entry in schema["exports"]:
        if entry["kind"] != "exception":
            continue
        assert entry.get("exit_code") is not None, (
            f"{entry['name']}.exit_code is None — every OrchoError subclass "
            "must declare an exit_code so CLI handlers can map it uniformly."
        )


def test_every_dataclass_is_frozen_slotted() -> None:
    """Public dataclasses must be `frozen=True, slots=True` per ADR 0021."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    for entry in schema["exports"]:
        if entry["kind"] != "dataclass":
            continue
        assert entry["frozen"] is True, f"{entry['name']} is not frozen"
        assert entry["slots"] is True, f"{entry['name']} has no slots"


def test_runs_dir_resolution_kwargs_uniform() -> None:
    """Every read/report call accepts the (workspace, runs_dir, cwd) triple."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    READERS = {
        "load_status",
        "list_history",
        "get_run_metrics",
        "list_metrics",
        "list_events",
        "aggregate_cost",
        "collect_evidence",
        "find_run",
        "find_runs_dir",
    }
    for entry in schema["exports"]:
        if entry["name"] not in READERS:
            continue
        param_names = {p["name"] for p in entry["params"]}
        for required in ("workspace", "runs_dir", "cwd"):
            assert required in param_names, (
                f"{entry['name']} is missing the {required!r} kwarg "
                "from the standard read/report context triple."
            )


@pytest.mark.parametrize(
    "name,expected_exit_code",
    [
        ("OrchoError", 1),
        ("NoWorkspace", 1),
        ("RunNotFound", 1),
        ("PricingFetchError", 2),
        ("PromptNotFound", 1),
        ("EvidenceInvalid", 1),
    ],
)
def test_exit_code_pinning(name: str, expected_exit_code: int) -> None:
    """Pin error exit codes; the CLI's `_run_cli` adapter relies on them."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    entry = next(e for e in schema["exports"] if e["name"] == name)
    assert entry["exit_code"] == expected_exit_code


# ── In-process coverage of the dumper ─────────────────────────────────────────
#
# ``test_dumper_check_mode_passes`` above runs the dumper as a *subprocess* to
# pin the real CLI contract, but a child process is invisible to the parent's
# coverage session (the dumper's own line coverage reads as 0%). These tests
# drive the same ``build_schema`` / ``main`` code paths in-process so the
# contract-critical reflection logic is actually measured.

import importlib.util  # noqa: E402


def _load_dumper():
    spec = importlib.util.spec_from_file_location("dump_sdk_schema_mod", _DUMPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_build_schema_shape_in_process() -> None:
    """``build_schema`` reflects the full public SDK surface in-process."""
    import sdk

    dumper = _load_dumper()
    schema = dumper.build_schema()
    assert schema["exports"], "schema must enumerate exports"
    assert all("name" in e for e in schema["exports"])
    assert {e["name"] for e in schema["exports"]} == set(sdk.__all__)


def test_check_mode_matches_committed_in_process() -> None:
    """``--check`` against the committed snapshot passes in-process (exit 0)."""
    dumper = _load_dumper()
    assert dumper.main(["--check"]) == 0


def test_check_mode_missing_snapshot_returns_1(tmp_path: Path) -> None:
    dumper = _load_dumper()
    assert dumper.main(["--check", "--out", str(tmp_path / "absent.json")]) == 1


def test_check_mode_drift_returns_1(tmp_path: Path) -> None:
    dumper = _load_dumper()
    stale = tmp_path / "stale.json"
    stale.write_text("{}", encoding="utf-8")
    assert dumper.main(["--check", "--out", str(stale)]) == 1
