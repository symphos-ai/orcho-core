"""
agents/runtimes/identity.py — provider-neutral runtime identity diagnostics.

A tiny value object plus a best-effort dispatcher. Core owns the *shape* and
the *rendering hint*; provider-specific extraction (which CLI surface to read,
how to parse it) lives in the runtime adapters (``agents/runtimes/claude.py``,
``codex.py``). This module never spawns a subprocess and never knows about any
particular provider's status command.

The signal is **diagnostic only**. It exists so an operator can see *which*
provider account / organization a run is actually executing under before
expensive work starts — the dogfood failure mode was a "mystery rate-limit"
that was really a quota-bucket / account mismatch. It is never an authorization
decision, never a delivery gate, and a missing identity never fails a run.

Safety contract honoured by every producer:

* Only fields a provider already shows in a *user-facing* status command may be
  populated (``account_label`` / ``email``). Access tokens, refresh tokens,
  cookies, auth-file paths, and raw auth JSON must never reach this object.
* Probing is best-effort: any failure resolves to an ``unavailable`` identity.
  :func:`probe_runtime_identity` swallows every exception so a probe can never
  abort run setup.
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class RuntimeIdentity:
    """Sanitized, provider-neutral account identity for one runtime instance.

    Required fields: ``runtime`` (backend id, e.g. ``"claude"``), ``source``
    (where the value came from — ``"runtime_status"`` for a real probe,
    ``"unavailable"`` / ``"no_status_surface"`` / ``"probe_error"`` for the
    miss cases, ``"mock"`` for fakes), and ``available``.

    Optional fields are all sanitized, user-facing values only: ``provider``
    (vendor label), ``account_label`` (org / account display name), and
    ``email``. They stay ``None`` when unavailable. No token, credential, or
    file-path field exists on this object by construction.
    """

    runtime: str
    source: str
    available: bool
    provider: str | None = None
    account_label: str | None = None
    email: str | None = None

    @classmethod
    def unavailable(cls, runtime: str, source: str = "unavailable") -> RuntimeIdentity:
        """Clean 'no identity' marker — every sensitive field left ``None``."""
        return cls(
            runtime=runtime,
            source=source,
            available=False,
            provider=None,
            account_label=None,
            email=None,
        )

    def hint(self) -> str:
        """Compact one-line hint for the run header, or ``""`` when there is
        nothing safe to show.

        Renders ``account=<label> / <email>`` when both are known, or whichever
        single field is present. Provider-generated labels that simply restate
        the same email are collapsed to the email alone. Unavailable identities
        render nothing — the product contract says show nothing rather than a
        noisy placeholder.
        """
        if not self.available:
            return ""
        label = (self.account_label or "").strip()
        email = (self.email or "").strip()
        if label and email and _label_restates_email(label, email):
            label = ""
        if label and email:
            return f"account={label} / {email}"
        if label:
            return f"account={label}"
        if email:
            return f"account={email}"
        return ""


def _label_restates_email(label: str, email: str) -> bool:
    label_norm = " ".join(label.casefold().split())
    email_norm = email.casefold().strip()
    return label_norm in {
        email_norm,
        f"{email_norm}'s organization",
    }


def probe_runtime_identity(agent: Any) -> RuntimeIdentity:
    """Best-effort dispatch to a runtime adapter's optional ``probe_identity``.

    ``probe_identity`` is a *structural* capability, not a required method on
    :class:`agents.protocols.IAgentRuntime`: a third-party runtime that does
    not implement it simply yields an ``unavailable`` identity here. The call
    is wrapped so a slow / failing / misbehaving probe can never raise into run
    setup — every error path returns ``unavailable``.

    Returns the adapter's :class:`RuntimeIdentity` when it produces one;
    otherwise an ``unavailable`` marker tagged with the reason.
    """
    runtime = str(getattr(agent, "runtime", "") or "unknown")
    probe = getattr(agent, "probe_identity", None)
    if not callable(probe):
        return RuntimeIdentity.unavailable(runtime, "unsupported")
    try:
        result = probe()
    except Exception:  # noqa: BLE001 — best-effort: a probe must never break run setup
        return RuntimeIdentity.unavailable(runtime, "probe_error")
    if isinstance(result, RuntimeIdentity):
        return result
    return RuntimeIdentity.unavailable(runtime, "unavailable")


__all__ = ["RuntimeIdentity", "probe_runtime_identity"]
