"""Built-in delivery-provider plugins (ADR 0121).

This sub-package is the ONE place in the engine that executes a git-provider
binary (``gh``) and ``git push``. Core (``pipeline.engine.delivery_publish`` and
every other ``pipeline/engine`` module) stays provider-neutral: it resolves a
provider registered under the ``orcho.delivery_providers`` entry-point group and
calls its ``publish`` method, but never shells a provider itself.

The built-in :class:`~pipeline.engine.delivery_providers.github.GitHubDeliveryProvider`
is registered through that same entry-point group, exactly like any third-party
provider an embedder ships.
"""
from __future__ import annotations

from pipeline.engine.delivery_providers.github import GitHubDeliveryProvider

__all__ = ["GitHubDeliveryProvider"]
