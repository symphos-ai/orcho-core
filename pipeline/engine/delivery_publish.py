"""Provider-neutral delivery publishing orchestrator (ADR 0121).

ADR 0119 stops at a *published local branch*: an isolated ``worktree_branch``
run creates ``orcho/deliver/<run_id>-<slug>`` in local refs over the run
worktree and records a provider-neutral :class:`DeliveryPrIntent`, but core
never pushes or opens a pull request. ADR 0121 adds the missing step behind an
explicit gate and a plugin boundary:

* :class:`PublishResult` is the typed outcome of a publish attempt — ``pushed``
  (did the branch reach the remote), ``pr_url`` (the opened pull request, if
  any), and ``warnings`` (non-fatal diagnostics). It is intentionally NOT a
  wire/SDK type: ``pr_url`` travels as a string inside the delivery decision's
  ``delivery_notices``, so no new persisted field is introduced.
* :class:`DeliveryPublisher` is the extension protocol a git-provider plugin
  implements. Any embedder can register one under the ``orcho.delivery_providers``
  entry-point group; the built-in provider is registered the same way.
* :func:`publish_delivery` is the single orchestration entry point. It reads the
  ``commit.publish`` gate, resolves a registered provider by name or by being
  the sole registration, invokes ``publish()``, and maps *every* failure into
  ``PublishResult.warnings`` — it never raises. When the gate is ``off`` it
  returns immediately without resolving or invoking any provider, so a disabled
  gate produces no provider work at all.

This module owns provider resolution and the publish call; it never names or
executes a provider binary (all binary detection, authentication, push, and
pull-request creation live only inside a registered provider package). The
commit site keeps a thin call into
:func:`publish_delivery` and folds the result into the delivery decision's
notices/warnings.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pipeline.engine.delivery_branch import DeliveryPrIntent
from pipeline.entry_points import discover_entry_points

__all__ = [
    "DELIVERY_PROVIDER_GROUP",
    "DeliveryPublisher",
    "PublishResult",
    "collect_delivery_setup_hints",
    "publish_delivery",
]

# Entry-point group git-provider plugins register under. Read through the shared
# ``pipeline.entry_points`` discovery helper so provider plugins follow the same
# author contract as every other Orcho extension point.
DELIVERY_PROVIDER_GROUP = "orcho.delivery_providers"

_PUBLISH_GATE_OFF = "off"
_DEFAULT_PUBLISH_GATE = "auto"


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of a delivery-branch publish attempt (ADR 0121).

    ``pushed`` records whether the branch reached the remote; ``pr_url`` the
    opened pull request when the provider created one; ``warnings`` any
    non-fatal diagnostics (missing binary, auth failure, offline, provider
    error). This is a plain in-process value, not a wire/SDK type — the commit
    site folds ``pr_url`` into ``delivery_notices`` and ``warnings`` into
    ``delivery_warnings`` rather than adding a new persisted field.
    """

    pushed: bool
    pr_url: str | None = None
    warnings: tuple[str, ...] = ()


class DeliveryPublisher(Protocol):
    """Extension protocol for a git-provider delivery publisher (ADR 0121).

    A plugin registered under :data:`DELIVERY_PROVIDER_GROUP` implements
    ``publish``: push ``branch`` (which already exists in ``cwd`` — the run
    worktree — over an already-created signed commit) to ``remote`` and open a
    pull request described by ``pr_intent``. The provider owns all binary
    detection, authentication, retries, and provider-specific errors, and must
    map every failure into :class:`PublishResult` warnings rather than raising.
    It must never create new or unsigned commits.
    """

    def publish(
        self,
        pr_intent: DeliveryPrIntent,
        *,
        branch: str,
        cwd: Path,
        remote: str,
    ) -> PublishResult:
        ...

    # --- optional -------------------------------------------------------
    # ``setup_hint`` is an OPTIONAL provider capability: providers that do
    # not implement it are fully legal ``DeliveryPublisher`` plugins. The
    # collector below duck-types the method with ``getattr`` + ``callable``
    # rather than requiring it, so this signature documents the contract
    # without forcing every provider to satisfy it.
    def setup_hint(self, project_dir: Path) -> str | None:
        """Return a human-readable setup hint, or ``None``.

        A provider returns a non-empty hint string when it *could* help
        deliver from ``project_dir`` but is not yet ready — for example a
        remote of its kind exists, yet the provider CLI is not installed or
        not authenticated. It returns ``None`` when it has nothing to
        suggest (wrong remote kind, already ready, not applicable). The
        hint is descriptive text only; a provider must never install,
        authenticate, or mutate anything to produce it, and must not raise.
        """
        ...


def publish_delivery(
    pr_intent: DeliveryPrIntent | None,
    *,
    branch: str | None,
    cwd: Path,
    remote: str = "origin",
    commit_config: Mapping[str, Any] | None = None,
) -> PublishResult:
    """Publish a resolved delivery branch through a registered provider.

    Reads the ``commit.publish`` gate from ``commit_config``:

    * ``off`` — reproduce the ADR 0119 behavior (local branch only). Returns
      ``PublishResult(pushed=False)`` WITHOUT resolving or invoking any provider
      and without any shell call.
    * ``auto`` (default) — resolve a provider from
      :data:`DELIVERY_PROVIDER_GROUP` (by ``commit.publish_provider`` name, or
      the sole registration) and invoke ``publish()``. No provider registered
      degrades to ``PublishResult(pushed=False)`` (the commit site adds a
      "branch ready" notice). Any provider exception — or an invalid return —
      is captured as a warning; this function never raises.

    ``cwd`` is the run worktree where the delivery branch physically exists;
    ``branch`` is the authoritative published branch name.
    """
    cfg = dict(commit_config or {})
    gate = str(cfg.get("publish", _DEFAULT_PUBLISH_GATE) or "").strip().lower()
    if gate == _PUBLISH_GATE_OFF:
        # Disabled gate: never resolve a provider and never shell out. The
        # already-published local branch is the deliverable.
        return PublishResult(pushed=False)
    if pr_intent is None or not branch:
        # Nothing publishable (defensive: only reached on a publish plan).
        return PublishResult(pushed=False)

    provider, warning = _resolve_provider(cfg)
    if provider is None:
        return PublishResult(pushed=False, warnings=(warning,) if warning else ())

    try:
        result = provider.publish(
            pr_intent, branch=branch, cwd=cwd, remote=remote
        )
    except Exception as exc:  # noqa: BLE001 — a provider must never crash the run
        return PublishResult(
            pushed=False,
            warnings=(f"delivery publish provider raised: {exc}",),
        )
    if not isinstance(result, PublishResult):
        return PublishResult(
            pushed=False,
            warnings=("delivery publish provider returned an invalid result",),
        )
    return result


def collect_delivery_setup_hints(
    project_dir: Path,
    *,
    commit_config: Mapping[str, Any] | None = None,
) -> list[str]:
    """Gather provider setup hints for ``project_dir`` (provider-neutral).

    Walks every provider registered under :data:`DELIVERY_PROVIDER_GROUP`
    and, for each one that exposes a callable ``setup_hint``, asks it whether
    it has a setup recommendation for ``project_dir`` (see
    :meth:`DeliveryPublisher.setup_hint`). Non-empty hints are collected in
    discovery order, de-duplicated so the same string never repeats.

    This is strictly best-effort:

    * A provider without ``setup_hint`` is skipped silently — the method is
      optional.
    * Any exception a provider raises is swallowed and that provider is
      skipped; one broken provider never suppresses the others' hints.
    * Discovery failure returns an empty list rather than raising.

    ``commit_config`` is accepted for symmetry with :func:`publish_delivery`
    (a future provider selection/gate could consult it) but is not required:
    setup guidance is useful independent of the publish gate, so hints are
    collected regardless of ``commit.publish``. This function is
    provider-agnostic — it names no specific provider or CLI.
    """
    _ = commit_config  # reserved for future symmetry; intentionally unused
    try:
        providers = discover_entry_points(DELIVERY_PROVIDER_GROUP)
    except Exception:  # noqa: BLE001 — discovery must never crash the caller
        return []

    hints: list[str] = []
    seen: set[str] = set()
    for provider in providers.values():
        hint_fn = getattr(provider, "setup_hint", None)
        if not callable(hint_fn):
            continue
        try:
            hint = hint_fn(project_dir)
        except Exception:  # noqa: BLE001 — a provider hint must never crash
            continue
        if not hint:
            continue
        text = str(hint)
        if text in seen:
            continue
        seen.add(text)
        hints.append(text)
    return hints


def _resolve_provider(
    cfg: Mapping[str, Any],
) -> tuple[DeliveryPublisher | None, str | None]:
    """Resolve the delivery provider from the entry-point registry.

    Returns ``(provider, None)`` on a clean resolution, ``(None, None)`` when no
    provider is registered (a silent degrade — the commit site emits a notice),
    or ``(None, warning)`` when a named provider is missing or the choice is
    ambiguous. Discovery failure is itself captured as a warning, never raised.
    """
    try:
        providers = discover_entry_points(DELIVERY_PROVIDER_GROUP)
    except Exception as exc:  # noqa: BLE001 — discovery must never crash the run
        return None, f"delivery publish provider discovery failed: {exc}"
    if not providers:
        return None, None
    name = str(cfg.get("publish_provider") or "").strip()
    if name:
        provider = providers.get(name)
        if provider is None:
            return None, (
                f"configured delivery provider {name!r} is not registered"
            )
        return provider, None
    if len(providers) == 1:
        return next(iter(providers.values())), None
    names = ", ".join(sorted(providers))
    return None, (
        f"multiple delivery providers registered ({names}); "
        "set commit.publish_provider to select one"
    )
