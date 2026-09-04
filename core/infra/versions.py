"""core/infra/versions.py — Installed Orcho distribution versions.

A run's ``meta.json`` records which Orcho packages produced it so that a
behaviour observed in an artifact can be matched to the engine version
that wrote it, without consulting the environment the run was launched
from (which may no longer exist by the time someone reads the run).

The probe is metadata-only: it reads installed distribution versions via
``importlib.metadata`` and never imports the packages, so recording a
companion package's version does not create a code dependency on it.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distributions, version

#: Distribution-name prefix that identifies Orcho packages. Any installed
#: distribution whose name starts with it is recorded, so companion
#: packages and third-party ``orcho-*`` plugins appear alongside the engine
#: without this module enumerating them.
ORCHO_DISTRIBUTION_PREFIX = "orcho"

#: The engine's own distribution name. Recorded even when the probe cannot
#: find it (source checkout without an install), so the key is always
#: present and its absence never has to be interpreted.
ENGINE_DISTRIBUTION = "orcho-core"

UNKNOWN_VERSION = "0+unknown"


def _normalize_name(name: str) -> str:
    return name.lower().replace("_", "-")


def installed_orcho_versions() -> dict[str, str]:
    """Return ``{distribution_name: version}`` for every installed Orcho package.

    Always contains :data:`ENGINE_DISTRIBUTION`; its value is
    :data:`UNKNOWN_VERSION` when no ``orcho-core`` distribution is installed
    in the running interpreter. Keys are sorted so the mapping serializes
    deterministically.
    """
    found: dict[str, str] = {}
    for dist in distributions():
        raw_name = dist.metadata["Name"] if dist.metadata else None
        if not raw_name:
            continue
        name = _normalize_name(raw_name)
        if not name.startswith(ORCHO_DISTRIBUTION_PREFIX):
            continue
        found.setdefault(name, dist.version or UNKNOWN_VERSION)
    if ENGINE_DISTRIBUTION not in found:
        try:
            found[ENGINE_DISTRIBUTION] = version(ENGINE_DISTRIBUTION)
        except PackageNotFoundError:
            found[ENGINE_DISTRIBUTION] = UNKNOWN_VERSION
    return dict(sorted(found.items()))
