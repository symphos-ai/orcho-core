"""Packaging smoke test: a fresh wheel install must expose every
runtime asset (``core/_config/`` JSON + ``core/_prompts/`` markdown)
and the loaders that resolve them.

This test guards against the pre-M11.5 packaging regression where
``PACKAGE_ROOT`` pointed at the source-tree root and wheel/sdist
installs silently lost ``_config/`` and ``_prompts/`` because they
sat outside any Python package.

The test runs end-to-end:

1. Build a wheel from the current source tree via ``python -m build
   --no-isolation``.
2. Create a temporary venv with no other orcho-core install.
3. ``pip install --no-deps`` the wheel into the venv.
4. Spawn a subprocess that imports ``core``, ``pipeline``, ``sdk``,
   ``agents``, ``cli`` from the wheel, loads the shipped v2 profile
   bundle, renders a core prompt, and loads :class:`AppConfig`.
5. Assert the subprocess's ``sys.path`` resolved ``core`` from the
   wheel's site-packages, not the source tree.

The test takes a few seconds because it actually builds and
installs a wheel. It runs by default — ``pytest tests/integration``
is part of the manual integrity pipeline — but it can be deselected
with ``-m "not slow_packaging"`` if a quick partial run is needed.

The build intentionally uses ``--no-isolation``. This smoke validates Orcho's
wheel contents, not PyPI connectivity, and an isolated PEP 517 build would
otherwise try to install ``setuptools>=68`` from the network on every run.
The install step likewise uses ``--no-deps`` so this package-content smoke
does not depend on network access for runtime dependencies.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_module_available() -> bool:
    try:
        return importlib.util.find_spec("build.__main__") is not None
    except ModuleNotFoundError:
        return False


@pytest.mark.packaging
@pytest.mark.slow_packaging
@pytest.mark.skipif(
    not _build_module_available(),
    reason="``build`` package not installed; pip install build to run.",
)
def test_wheel_install_round_trip(tmp_path: Path) -> None:
    # ── 1. Build a fresh wheel into ``tmp_path/dist``. ────────────────
    shutil.rmtree(_REPO_ROOT / "build", ignore_errors=True)
    dist_dir = tmp_path / "dist"
    build = subprocess.run(
        [
            sys.executable,
            "-m", "build",
            "--wheel",
            "--no-isolation",
            "--outdir", str(dist_dir),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, (
        f"wheel build failed (stderr):\n{build.stderr}"
    )

    wheels = sorted(dist_dir.glob("orcho_core-*.whl"))
    assert wheels, f"no wheel produced in {dist_dir}"
    wheel_path = wheels[0]
    with zipfile.ZipFile(wheel_path) as wheel:
        mock_artifacts = [
            name for name in wheel.namelist()
            if name.endswith("/Implementation.py")
        ]
    assert not mock_artifacts, (
        "mock implementation artifacts leaked into wheel:\n"
        + "\n".join(mock_artifacts)
    )

    # ── 2. Create a venv, install the wheel. ──────────────────────────
    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True, capture_output=True,
    )
    bin_name = "Scripts" if os.name == "nt" else "bin"
    venv_python = venv_dir / bin_name / "python"
    venv_pip = venv_dir / bin_name / "pip"
    install = subprocess.run(
        [str(venv_pip), "install", "--quiet", "--no-deps", str(wheel_path)],
        capture_output=True, text=True,
    )
    assert install.returncode == 0, (
        f"wheel install failed (stderr):\n{install.stderr}"
    )

    # ── 3. Run the smoke script in a neutral CWD so the source-tree
    #    ``core/`` does not shadow the wheel-installed module. ────────
    smoke = textwrap.dedent(
        """
        import sys

        import core, pipeline, sdk, agents, cli  # noqa: F401
        from core.infra.paths import CONFIG_DIR, PROMPTS_DIR

        # The wheel-installed core must be loaded, not the source tree.
        assert "site-packages" in core.__file__, (
            f"unexpected core source: {core.__file__}"
        )
        assert CONFIG_DIR.is_dir(), f"CONFIG_DIR missing: {CONFIG_DIR}"
        assert PROMPTS_DIR.is_dir(), f"PROMPTS_DIR missing: {PROMPTS_DIR}"

        from pipeline.profiles.loader import load_profiles_v2
        profiles = load_profiles_v2(CONFIG_DIR / "pipeline_profiles_v2.json")
        assert {"task", "small_task"}.issubset(profiles), sorted(profiles)

        from core.io.prompt_loader import list_core_prompts, render_prompt
        prompts = list_core_prompts()
        assert len(prompts) >= 18, f"too few core prompts: {len(prompts)}"
        body = render_prompt("roles/code_reviewer", task="demo")
        assert body, "core prompt rendered empty"

        from core.infra.config import AppConfig
        cfg = AppConfig.load()
        assert cfg.phases, "AppConfig phases empty"

        print("OK")
        """,
    )
    result = subprocess.run(
        [str(venv_python), "-c", smoke],
        cwd=str(tmp_path),  # neutral CWD — no source-tree shadowing
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "wheel smoke subprocess failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout

    # ── Cleanup — ``tmp_path`` is auto-removed by pytest. ────────────
    _ = shutil  # silence unused import lint
