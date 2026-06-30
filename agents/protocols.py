"""
agents/protocols.py — Public agent contract.

:class:`IAgentRuntime` is the single Protocol every runtime implements. One
``invoke()`` entry point: it receives a fully-composed prompt (the composer
builds it) plus per-call flags and returns raw text. Callers parse plans /
critiques / etc. from the returned string.

``session_id`` is the bridge handle. A runtime instance carries it across
phase boundaries so subsequent invocations can ``--resume`` the same
conversation. Runtimes without resumable sessions leave it ``None``. The
orchestrator decides per-call whether to resume via ``continue_session=True``;
the runtime is mechanical and never raises on cross-mutation resume — that
policy lives in the orchestrator.

``mutates_artifacts`` declares whether the call may modify project artifacts
on disk (code, configs, generated files in ``cwd``). Maps to the CLI write
flags. Default ``False`` is safe; ``build`` / ``fix`` phases opt in. The flag
is per-call, not per-session — one bridge can carry both mutating and
non-mutating invocations.

``attachments`` is multimodal-only (IMAGE / BINARY). TEXT attachments are
rendered into the prompt outside the runtime by
``builtin._plan_prompt_prefix`` via ``render_text_block``; passing TEXT into
``invoke(attachments=...)`` is a caller bug and the runtime raises
``ValueError`` to prevent double injection.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pipeline.runtime.steps import Attachment


class SessionMode(str, Enum):  # noqa: UP042  # StrEnum changes __str__; appears in SDK schema snapshot, keep stable
    """How the orchestrator should chain calls between phases."""

    AUTO      = "auto"
    STATELESS = "stateless"
    CHAIN     = "chain"
    HYBRID    = "hybrid"


@runtime_checkable
class IAgentRuntime(Protocol):
    """Unified agent runtime contract.

    A concrete implementation backs one CLI / SDK (Claude Code, Codex CLI,
    Gemini, …). It receives a fully-composed prompt and returns the agent's
    raw textual response. Plan parsing, review verdict extraction, and any
    other downstream interpretation happen in the caller.

    Fields:
        model: identifier of the underlying model.
        session_id: bridge handle. ``None`` until the first successful call,
            or for runtimes without resumable sessions. Same instance, same
            session through the pipeline; reset with :meth:`reset_session`.
        _followup_resume_pending: optional follow-up seed flag. When true and
            ``session_id`` is set, the next ``invoke`` must pass that id to the
            runtime resume flag even if the caller did not request continuation.
        _last_resumed_session_id: the exact id passed to the runtime resume
            flag on the most recent call, or ``None`` when the call was fresh.
        _last_followup_parent_session_id: the parent-run session id consumed by
            a follow-up seed on the most recent call, or ``None`` otherwise.
    """

    model: str
    session_id: str | None
    _followup_resume_pending: bool
    _last_resumed_session_id: str | None
    _last_followup_parent_session_id: str | None

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple[Attachment, ...] = (),
    ) -> str:
        """Run the agent on ``prompt`` inside working directory ``cwd``.

        Args:
            prompt: fully-composed prompt text (composer built it).
            cwd: project working directory.
            mutates_artifacts: when ``True`` the call may modify files on
                disk in ``cwd``. Maps to the underlying CLI's write flags.
                Default ``False`` is read-only.
            continue_session: when ``True`` and ``self.session_id`` is set,
                resume that session (``--resume``) so this call inherits the
                bridge's accumulated context. Otherwise start fresh and
                capture a new ``session_id``.
            attachments: multimodal attachments (IMAGE / BINARY) the runtime
                must hand to the CLI. **TEXT attachments are forbidden here**
                — they are rendered into ``prompt`` outside the runtime; if
                a TEXT entry leaks in, the runtime raises ``ValueError``.

        Returns:
            Raw assistant text. Callers parse plans/critiques/etc. from it.
        """
        ...

    def reset_session(self) -> None:
        """Clear ``session_id`` so the next ``invoke()`` starts fresh.

        Use to deliberately burn the bridge (e.g. when a human operator
        requested a clean restart from a paused run).
        """
        ...

    # ── Optional capability: account identity diagnostics ────────────────
    #
    # A runtime MAY implement ``probe_identity(self) -> RuntimeIdentity`` to
    # report which provider account / organization it is executing under. It
    # is intentionally NOT declared as a required method on this Protocol:
    # the capability is structural, so a third-party runtime that omits it
    # simply yields an ``unavailable`` identity via
    # :func:`agents.runtimes.identity.probe_runtime_identity`.
    #
    # Contract for implementers:
    #   * Diagnostic only — never an authorization or delivery decision.
    #   * Best-effort and non-interactive: read a status surface the provider
    #     already shows users, with a short timeout. Any failure (missing
    #     binary, timeout, non-zero exit, unparsable output, no account field)
    #     returns ``RuntimeIdentity.unavailable(...)``; it must never raise.
    #   * Sanitized: populate only ``account_label`` / ``email`` (values the
    #     provider already surfaces). Never read or return tokens, cookies,
    #     auth-file paths, or raw auth JSON.
    #   * Lazy: must not be called from the constructor, profile listing, or
    #     dry-run rendering. Resolving the CLI binary here (first real use) is
    #     fine; the orchestrator only calls it during real run setup.
