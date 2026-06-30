"""
pipeline.profiles — profile loading + validation.

Phase 3 of the runtime/subdomain refactor moved
``pipeline/profiles/loader.py`` into this package. The loader exposes
the same public symbols (``parse_profile``, ``parse_profiles``,
``load_profiles_v2``, ``load_profiles_v2_with_plugins``,
``ProfileLoadError``) at a tidier import path.

``from pipeline.profiles.loader import ...`` is the canonical path.
``from pipeline.profiles import ...`` re-exports the public surface
for short call sites.
"""

from __future__ import annotations

from pipeline.profiles.loader import (
    ProfileLoadError,
    load_profiles_v2,
    load_profiles_v2_with_plugins,
    parse_profile,
    parse_profiles,
)

__all__ = [
    "ProfileLoadError",
    "load_profiles_v2",
    "load_profiles_v2_with_plugins",
    "parse_profile",
    "parse_profiles",
]
