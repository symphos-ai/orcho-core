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

ProviderModeSource = Literal["fresh", "explicit", "inherited"]


class ProviderModeError(ValueError):
    """Raised when persisted provider-mode metadata is not trustworthy."""


@dataclass(frozen=True)
class ProviderModeResolution:
    """The effective provider mode and the decision path that selected it."""

    mock: bool
    source: ProviderModeSource

    @property
    def label(self) -> str:
        return "mock" if self.mock else "real"


def resolve_provider_mode(
    *,
    explicit_mock: bool,
    resumed_meta: Mapping[str, object] | None,
) -> ProviderModeResolution:
    """Resolve fresh, explicit, and inherited cross provider modes.

    ``--mock`` is the sole explicit override available on the current CLI and
    may override a valid persisted value.  A resumed run with a boolean
    top-level ``mock`` otherwise inherits that value.  Resume metadata without
    the required field, or with a non-boolean value, is rejected before any
    override instead of choosing a provider mode implicitly.
    """
    if resumed_meta is None:
        return ProviderModeResolution(
            mock=explicit_mock,
            source="explicit" if explicit_mock else "fresh",
        )
    if "mock" not in resumed_meta:
        raise ProviderModeError(
            "resumed cross run has no persisted provider mode; required "
            "boolean meta.mock is missing, so this run cannot be resumed"
        )
    persisted = resumed_meta["mock"]
    if not isinstance(persisted, bool):
        raise ProviderModeError(
            "resumed cross run has invalid persisted mock mode: "
            f"expected boolean, got {type(persisted).__name__}"
        )
    if explicit_mock:
        return ProviderModeResolution(mock=True, source="explicit")
    return ProviderModeResolution(mock=persisted, source="inherited")
