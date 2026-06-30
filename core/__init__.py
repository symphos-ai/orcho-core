"""core — Multi-agent core package."""
from pathlib import Path

# Root of the ``core`` package directory itself.
#
# Asset directories (``_prompts/``, ``_config/``) live INSIDE this
# package so they ship in wheels / sdists via standard
# ``[tool.setuptools.package-data]``. Before the M11.5 packaging
# fix this attribute pointed one level higher (the source-tree
# root, which has no equivalent inside an installed wheel) — that
# only worked under editable installs.
#
# Prefer :mod:`importlib.resources` for asset lookup (see
# :mod:`core.infra.paths`) so resolution stays portable across
# editable and installed distributions.
PACKAGE_ROOT: Path = Path(__file__).parent
