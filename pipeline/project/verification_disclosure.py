"""Single owner of the "no verification contract declared" fact.

One fact, one writer, many readers.

* **The fact** is binary: did this run start with a declared verification
  contract? It is decided exactly once per run, in
  ``pipeline.project.session_run`` — right after the contract has been
  resolved and default-mode-projected — and never re-derived afterwards.
* **The writer** is :func:`stamp_contract_presence`, reached through the real
  session-init chain ``session_run`` → ``run_setup.init_run_session`` →
  ``bootstrap.init_session_with_atexit``. The block lands in the session dict
  before the first ``save_session``, so it is in ``meta.json`` from the very
  first durable write and survives an abnormal exit (the atexit hook captured
  the same dict).
* **The readers** — run header, DONE/HALTED tail, final_acceptance readiness,
  ``orcho status``, the delivery-decision state — project the persisted block
  (``meta.json`` / ``state.extras``). They never load the project plugin, and
  they never reconstruct the fact from the scheduled-gate ledger: a run
  without a contract has no ledger at all, which is precisely the state this
  module makes legible.

The user-facing wording for every one of those surfaces lives here and nowhere
else. It is vendor-neutral: no plugin file name, no tier-specific vocabulary,
no box-drawing rules.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

__all__ = [
    "HEADER_VALUE",
    "META_KEY",
    "VerificationContractPresence",
    "delivery_gate_line",
    "readiness_block",
    "stamp_contract_presence",
    "status_line",
    "tail_line",
]

#: ``meta.json`` / ``state.extras`` key carrying the persisted fact.
META_KEY = "verification_contract_presence"

#: Run-header value rendered in place of the scheduled-gate table.
HEADER_VALUE = (
    "contract: none — engine runs no gates; verification comes from the agents only"
)

#: Where an operator goes to declare one. Named once, reused by every surface.
_DOC_POINTER = "docs/architecture/verification_contract.md"


@dataclasses.dataclass(frozen=True, slots=True)
class VerificationContractPresence:
    """Whether this run started with a declared verification contract."""

    declared: bool

    @classmethod
    def from_contract(cls, contract: Any) -> VerificationContractPresence:
        """Decide the fact from the resolved contract projection.

        ``None`` is the engine's own "no contract declared" signal — see
        ``pipeline.project.run_setup.project_verification_contract``.
        """
        return cls(declared=contract is not None)

    def to_meta(self) -> dict[str, bool]:
        """The persisted block shape: ``{"declared": <bool>}``."""
        return {"declared": bool(self.declared)}

    @classmethod
    def from_mapping(cls, source: Any) -> VerificationContractPresence | None:
        """Project the fact out of a meta/session/extras mapping.

        Returns ``None`` — "this run never recorded the fact" — when the
        block is absent or malformed. Runs written before the block existed
        land here, and readers must keep behaving exactly as they did then.
        """
        if not isinstance(source, Mapping):
            return None
        block = source.get(META_KEY)
        if not isinstance(block, Mapping):
            return None
        declared = block.get("declared")
        if not isinstance(declared, bool):
            return None
        return cls(declared=declared)


def stamp_contract_presence(
    session: dict, presence: VerificationContractPresence | None,
) -> None:
    """Write the fact into ``session`` in place. No-op without a presence.

    Called while the session dict is being built, before it is first
    persisted. Idempotent: a resume re-stamps the same fact from the freshly
    resolved contract.
    """
    if presence is None:
        return
    session[META_KEY] = presence.to_meta()


# ── wording (the only copy of it) ────────────────────────────────────────


def tail_line() -> str:
    """The single DONE/HALTED-tail line replacing today's silent omission."""
    return (
        "Verification gates: none — no verification contract declared; engine ran "
        "no gates (verification came from the agents only). Declare one: "
        f"{_DOC_POINTER}"
    )


def readiness_block() -> str:
    """The final_acceptance readiness block for a run without a contract."""
    return "\n".join((
        "Verification readiness — final_acceptance:",
        "  No verification contract declared; 0 receipts — the engine ran no "
        "gates; verification came from the agents only.",
        f"  Declare a contract: {_DOC_POINTER}",
    ))


def status_line() -> str:
    """The ``orcho status`` Gates-section value."""
    return "none — no verification contract declared; engine ran no gates"


def delivery_gate_line() -> str:
    """The ``orcho delivery gate`` value."""
    return "no contract declared — engine ran no gates"
