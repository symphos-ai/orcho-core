"""Resolve the durable provider mode for cross-project CLI runs.

The ``mock`` flag is a safety boundary: a checkpoint resume must not turn a
mock run into a real-provider run merely because its operator omitted the
flag.  This module is deliberately CLI-neutral so resolution can happen before
provider and phase-agent construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

ProviderModeSource = Literal["fresh", "explicit", "inherited", "legacy"]


class ProviderModeError(ValueError):
    """Raised when persisted provider-mode metadata is not trustworthy."""


@dataclass(frozen=True)
class ProviderModeResolution:
    """The effective provider mode and the decision path that selected it."""

    mock: bool
    source: ProviderModeSource
    legacy_fallback_warning: bool = False

    @property
    def label(self) -> str:
        return "mock" if self.mock else "real"


def resolve_provider_mode(
    *,
    explicit_mock: bool,
    resumed_meta: Mapping[str, object] | None,
) -> ProviderModeResolution:
    """Resolve fresh, explicit, inherited, and legacy cross provider modes.

    ``--mock`` is the sole explicit override available on the current CLI and
    wins over durable metadata.  A resumed run with a boolean top-level
    ``mock`` inherits that value.  Metadata created before this field existed
    retains the historical argv-driven behaviour, while a present non-boolean
    value is rejected instead of being silently coerced.
    """
    legacy_fallback_warning = resumed_meta is not None and "mock" not in resumed_meta
    if resumed_meta is not None and "mock" in resumed_meta:
        persisted = resumed_meta["mock"]
        if not isinstance(persisted, bool):
            raise ProviderModeError(
                "resumed cross run has invalid persisted mock mode: "
                f"expected boolean, got {type(persisted).__name__}"
            )
    else:
        persisted = None

    if explicit_mock:
        return ProviderModeResolution(
            mock=True,
            source="explicit",
            legacy_fallback_warning=legacy_fallback_warning,
        )
    if resumed_meta is None:
        return ProviderModeResolution(mock=False, source="fresh")
    if persisted is None:
        return ProviderModeResolution(
            mock=False,
            source="legacy",
            legacy_fallback_warning=True,
        )
    return ProviderModeResolution(mock=persisted, source="inherited")
