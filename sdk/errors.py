"""Typed exception hierarchy for the public SDK boundary.

Every SDK call raises an `OrchoError` subclass instead of returning
sentinel `None` or printing. Each carries an `exit_code` so the CLI's
shared `_run_cli` adapter can map errors onto process exit codes once,
without each handler re-deciding.
"""
from __future__ import annotations


class OrchoError(Exception):
    """Base class for all SDK-raised errors.

    Subclasses set `exit_code` so CLI callers can map errors to process
    exit codes uniformly. Embedders that don't care about exit codes
    can ignore the attribute.
    """

    exit_code: int = 1

    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class NoWorkspace(OrchoError):
    """No runs directory could be resolved from the given context."""

    exit_code = 1


class RunNotFound(OrchoError):
    """Requested run id does not exist under the resolved runs directory."""

    exit_code = 1


class LaunchError(OrchoError):
    """Failed to spawn (or resume) a detached pipeline subprocess.

    Raised by the ``sdk.run_control.launch`` surface when the underlying
    ``subprocess.Popen`` fails (``OSError`` / ``FileNotFoundError``) or a
    launch input (``project_dir`` / ``task_file``) is invalid. A missing
    run on resume/cancel signals :class:`RunNotFound` instead.
    """

    exit_code = 1


class PricingFetchError(OrchoError):
    """Pricing-table refresh from upstream sources failed."""

    exit_code = 2


class PromptNotFound(OrchoError):
    """Requested prompt name has no resolution in any registered location."""

    exit_code = 1


class EvidenceInvalid(OrchoError):
    """Evidence bundle failed validation against the schema contract."""

    exit_code = 1


class WorkspaceInitError(OrchoError):
    """``sdk.init_workspace`` refused a target or hit a conflict.

    Raised for cases the caller can fix: target is ``/`` or the user
    home directory exactly, target looks like an individual project
    repo (``--force`` needed), MCP config file is malformed, or an
    MCP server entry with the same name already exists with different
    content (``--force`` needed to replace).
    """

    exit_code = 2


class ProfileCustomizeError(OrchoError):
    """``sdk.customize_profile`` rejected a local profile overlay request."""

    exit_code = 2


class ProjectNotFound(OrchoError):
    """``--project`` path does not exist on disk."""

    exit_code = 2


class InvalidPhaseHandoffState(OrchoError):
    """Generic phase-handoff decision rejected by state or contract.

    Raised by ``sdk.phase_handoff_decide`` when one of these holds:

    * The run is not in ``awaiting_phase_handoff`` and no matching prior
      decision artifact exists (no pause is in effect).
    * The submitted ``handoff_id`` does not match the active
      ``meta.phase_handoff.id`` — stale UI or wrong-run dispatch.
    * The chosen action is not in the active handoff's runtime-produced
      ``available_actions``.
    * A prior decision for the same ``handoff_id`` recorded a different
      ``action`` / ``feedback`` / ``note`` (exact-payload idempotency
      violation).

    The artifact path is the audit record of the *exact* human
    instruction used on resume, so transports retrying with inconsistent
    payloads is a contract error, not a silent overwrite.
    """

    exit_code = 1
