# SPDX-License-Identifier: Apache-2.0
"""Run-scoped admission and durable settlement for long agent commands.

Provider tool handles are observations, not process ownership.  This module
provides the small local boundary an authoring agent can use when a command may
outlive such a handle: an atomic lease rejects an equivalent concurrent start,
and only an exact child exit writes the terminal receipt that releases it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class ManagedCommandState(StrEnum):
    """Durable lifecycle states exposed by the managed-command boundary."""

    RUNNING = "running"
    EXITED = "exited"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ManagedCommandIdentity:
    """Stable admission identity for one run/phase/cwd/argv tuple."""

    run_id: str
    phase: str
    cwd: str
    argv: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        run_dir: str | Path,
        phase: str,
        cwd: str | Path,
        argv: Sequence[str],
    ) -> ManagedCommandIdentity:
        resolved_run = Path(run_dir).expanduser().resolve()
        resolved_cwd = Path(cwd).expanduser().resolve()
        normalized_argv = tuple(str(part) for part in argv)
        if not normalized_argv:
            raise ValueError("managed command argv must not be empty")
        if not phase.strip():
            raise ValueError("managed command phase must not be empty")
        return cls(
            run_id=resolved_run.name,
            phase=phase.strip().lower(),
            cwd=str(resolved_cwd),
            argv=normalized_argv,
        )

    @property
    def key(self) -> str:
        """Filesystem-safe digest of the complete normalized identity."""
        payload = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ManagedCommandObservation:
    """Current durable observation for an identity."""

    state: ManagedCommandState
    attempt_id: str | None = None
    exit_code: int | None = None


class DuplicateManagedCommandError(RuntimeError):
    """Raised when an active or unobservable equivalent lease exists."""

    def __init__(
        self,
        identity: ManagedCommandIdentity,
        observation: ManagedCommandObservation,
    ) -> None:
        self.identity = identity
        self.observation = observation
        super().__init__(
            "equivalent managed command is already active or has unknown "
            f"terminal state (identity={identity.key[:12]}, "
            f"state={observation.state})"
        )


@dataclass(frozen=True, slots=True)
class ManagedCommandLease:
    """One admitted command attempt."""

    identity: ManagedCommandIdentity
    attempt_id: str
    lease_path: Path
    receipt_path: Path


PopenFactory = Callable[..., Any]


class ManagedCommandStore:
    """Atomic local lease/receipt store rooted in one run directory."""

    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.root = self.run_dir / "managed_commands"
        self.leases = self.root / "leases"
        self.receipts = self.root / "receipts"

    def admit(self, identity: ManagedCommandIdentity) -> ManagedCommandLease:
        """Atomically admit one identity, rejecting an unsettled duplicate."""
        self.leases.mkdir(parents=True, exist_ok=True)
        self.receipts.mkdir(parents=True, exist_ok=True)
        lease_path = self.leases / f"{identity.key}.json"
        attempt_id = uuid.uuid4().hex
        receipt_path = self.receipts / f"{identity.key}.{attempt_id}.json"
        payload = {
            "schema_version": 1,
            "state": ManagedCommandState.RUNNING,
            "attempt_id": attempt_id,
            "identity": asdict(identity),
            "started_at": _now(),
            "owner_pid": os.getpid(),
        }
        try:
            fd = os.open(
                lease_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            observation = self.observe(identity)
            raise DuplicateManagedCommandError(identity, observation) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            lease_path.unlink(missing_ok=True)
            raise
        return ManagedCommandLease(
            identity=identity,
            attempt_id=attempt_id,
            lease_path=lease_path,
            receipt_path=receipt_path,
        )

    def observe(
        self, identity: ManagedCommandIdentity,
    ) -> ManagedCommandObservation:
        """Read durable truth without inferring liveness from a foreign PID."""
        lease_path = self.leases / f"{identity.key}.json"
        if lease_path.exists():
            attempt_id = _attempt_id_from(lease_path)
            # Another process cannot prove that the recorded owner/child is
            # still live.  Unknown is deliberately fail-closed for admission.
            return ManagedCommandObservation(
                ManagedCommandState.UNKNOWN, attempt_id=attempt_id,
            )
        receipts = list(self.receipts.glob(f"{identity.key}.*.json"))
        if not receipts:
            return ManagedCommandObservation(ManagedCommandState.UNKNOWN)
        latest = max(receipts, key=lambda path: path.stat().st_mtime_ns)
        payload = _read_json(latest)
        exit_code = payload.get("exit_code")
        return ManagedCommandObservation(
            ManagedCommandState.EXITED,
            attempt_id=str(payload.get("attempt_id") or "") or None,
            exit_code=exit_code if isinstance(exit_code, int) else None,
        )

    def settle(self, lease: ManagedCommandLease, *, exit_code: int) -> Path:
        """Record one exact terminal result and release its matching lease."""
        current = _read_json(lease.lease_path)
        if current.get("attempt_id") != lease.attempt_id:
            raise RuntimeError("managed command lease no longer matches attempt")
        receipt = {
            "schema_version": 1,
            "state": ManagedCommandState.EXITED,
            "attempt_id": lease.attempt_id,
            "identity": asdict(lease.identity),
            "exit_code": int(exit_code),
            "finished_at": _now(),
        }
        _write_once(lease.receipt_path, receipt)
        # Receipt first, release second: a crash can cause a conservative false
        # rejection, never an overlapping duplicate launch.
        current = _read_json(lease.lease_path)
        if current.get("attempt_id") == lease.attempt_id:
            lease.lease_path.unlink(missing_ok=True)
        return lease.receipt_path


def run_managed_command(
    *,
    run_dir: str | Path,
    phase: str,
    cwd: str | Path,
    argv: Sequence[str],
    popen_factory: PopenFactory = subprocess.Popen,
) -> int:
    """Admit, run, and settle one exact child command synchronously.

    The child inherits stdio so a provider sees ordinary command output.  The
    wrapper deliberately remains synchronous: if a provider loses its own tool
    handle, the lease still exists and rejects an equivalent second wrapper.
    """
    identity = ManagedCommandIdentity.build(
        run_dir=run_dir, phase=phase, cwd=cwd, argv=argv,
    )
    store = ManagedCommandStore(run_dir)
    lease = store.admit(identity)
    proc: Any | None = None
    try:
        proc = popen_factory(list(identity.argv), cwd=identity.cwd)
        exit_code = int(proc.wait())
    except BaseException:
        if proc is None:
            store.settle(lease, exit_code=126)
        else:
            cancelled_code = _cancel_exact_child(proc)
            if cancelled_code is not None:
                store.settle(lease, exit_code=cancelled_code)
        raise
    store.settle(lease, exit_code=exit_code)
    return exit_code


def _cancel_exact_child(proc: Any) -> int | None:
    """Best-effort cancellation scoped to the exact child object."""
    try:
        code = proc.poll()
        if code is None:
            proc.terminate()
            code = proc.wait(timeout=5)
        return int(code)
    except Exception:
        # No terminal observation means the lease intentionally remains.  A
        # later equivalent launch therefore fails closed instead of guessing.
        return None


def _attempt_id_from(path: Path) -> str | None:
    try:
        value = _read_json(path).get("attempt_id")
    except (OSError, ValueError, TypeError):
        return None
    return str(value) if value else None


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}")
    return payload


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_json(path)
        if existing != payload:
            raise RuntimeError(
                "managed command receipt already differs",
            ) from None
        return
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _now() -> str:
    return datetime.now(UTC).isoformat()
