from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from agents.managed_command import (
    DuplicateManagedCommandError,
    ManagedCommandIdentity,
    ManagedCommandState,
    ManagedCommandStore,
    run_managed_command,
)


def _identity(tmp_path: Path, argv: tuple[str, ...] = ("pytest", "-q")):
    run_dir = tmp_path / "runs" / "run-1"
    cwd = tmp_path / "checkout"
    run_dir.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(exist_ok=True)
    return ManagedCommandIdentity.build(
        run_dir=run_dir, phase="implement", cwd=cwd, argv=argv,
    )


def test_unsettled_identity_rejects_duplicate_after_handle_loss(
    tmp_path: Path,
) -> None:
    identity = _identity(tmp_path)
    store = ManagedCommandStore(tmp_path / "runs" / "run-1")
    lease = store.admit(identity)

    # A provider may report partial output and lose its cell, but neither event
    # is terminal process truth. A fresh observer therefore sees UNKNOWN.
    observed = store.observe(identity)
    assert observed.state is ManagedCommandState.UNKNOWN
    assert observed.attempt_id == lease.attempt_id

    with pytest.raises(DuplicateManagedCommandError) as raised:
        store.admit(identity)
    assert raised.value.observation.state is ManagedCommandState.UNKNOWN


def test_exact_terminal_receipt_admits_later_repeat(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    store = ManagedCommandStore(tmp_path / "runs" / "run-1")
    first = store.admit(identity)
    receipt = store.settle(first, exit_code=0)

    assert receipt.exists()
    observed = store.observe(identity)
    assert observed.state is ManagedCommandState.EXITED
    assert observed.exit_code == 0

    second = store.admit(identity)
    assert second.attempt_id != first.attempt_id


def test_different_and_targeted_commands_have_distinct_admission(
    tmp_path: Path,
) -> None:
    broad = _identity(tmp_path, ("pytest", "-q"))
    targeted = _identity(tmp_path, ("pytest", "-q", "tests/unit/test_one.py"))
    store = ManagedCommandStore(tmp_path / "runs" / "run-1")

    store.admit(broad)
    targeted_lease = store.admit(targeted)

    assert targeted_lease.identity.key != broad.key


def test_run_settles_exact_child_once(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    cwd = tmp_path / "checkout"
    run_dir.mkdir()
    cwd.mkdir()

    class FakeProcess:
        def wait(self, timeout=None):
            assert timeout is None
            return 7

    calls = []

    def fake_popen(argv, *, cwd):
        calls.append((argv, cwd))
        return FakeProcess()

    exit_code = run_managed_command(
        run_dir=run_dir,
        phase="repair_changes",
        cwd=cwd,
        argv=("python", "-m", "pytest"),
        popen_factory=fake_popen,
    )

    assert exit_code == 7
    assert calls == [(["python", "-m", "pytest"], str(cwd.resolve()))]
    assert not list((run_dir / "managed_commands" / "leases").iterdir())
    assert len(list((run_dir / "managed_commands" / "receipts").iterdir())) == 1


def test_spawn_failure_is_terminal_and_does_not_poison_identity(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run-1"
    cwd = tmp_path / "checkout"
    run_dir.mkdir()
    cwd.mkdir()

    def fail_spawn(argv, *, cwd):
        raise OSError("missing executable")

    with pytest.raises(OSError, match="missing executable"):
        run_managed_command(
            run_dir=run_dir,
            phase="implement",
            cwd=cwd,
            argv=("missing",),
            popen_factory=fail_spawn,
        )

    identity = ManagedCommandIdentity.build(
        run_dir=run_dir, phase="implement", cwd=cwd, argv=("missing",),
    )
    assert ManagedCommandStore(run_dir).admit(identity)


def test_interrupt_cancels_only_exact_child_and_records_exit(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    cwd = tmp_path / "checkout"
    run_dir.mkdir()
    cwd.mkdir()

    class InterruptingProcess:
        terminated = False

        def wait(self, timeout=None):
            if timeout is None:
                raise KeyboardInterrupt
            assert timeout == 5
            return -15

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    child = InterruptingProcess()

    with pytest.raises(KeyboardInterrupt):
        run_managed_command(
            run_dir=run_dir,
            phase="implement",
            cwd=cwd,
            argv=("python", "worker.py"),
            popen_factory=lambda *args, **kwargs: child,
        )

    assert child.terminated is True
    identity = ManagedCommandIdentity.build(
        run_dir=run_dir,
        phase="implement",
        cwd=cwd,
        argv=("python", "worker.py"),
    )
    observed = ManagedCommandStore(run_dir).observe(identity)
    assert observed.state is ManagedCommandState.EXITED
    assert observed.exit_code == -15


def test_evidence_projects_terminal_receipt_without_raw_arguments(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    cwd = tmp_path / "checkout"
    run_dir.mkdir(parents=True)
    cwd.mkdir()
    secret = "sentinel-secret-must-not-project"
    identity = ManagedCommandIdentity.build(
        run_dir=run_dir,
        phase="implement",
        cwd=cwd,
        argv=("/usr/bin/python3", "-c", secret),
    )
    store = ManagedCommandStore(run_dir)
    store.settle(store.admit(identity), exit_code=0)

    records = store.evidence()

    assert len(records) == 1
    record = records[0]
    assert record.identity_digest == identity.key
    assert record.state is ManagedCommandState.EXITED
    assert record.exit_code == 0
    assert record.executable == "python3"
    assert record.phase == "implement"
    assert record.artifact_path.startswith("managed_commands/receipts/")
    assert secret not in json.dumps(asdict(record))


def test_evidence_projects_unsettled_lease_as_unknown(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    store = ManagedCommandStore(tmp_path / "runs" / "run-1")
    store.admit(identity)

    records = store.evidence()

    assert len(records) == 1
    assert records[0].state is ManagedCommandState.UNKNOWN
    assert records[0].exit_code is None
    assert records[0].artifact_path.startswith("managed_commands/leases/")


def test_evidence_keeps_corrupt_record_without_erasing_valid_receipt(
    tmp_path: Path,
) -> None:
    identity = _identity(tmp_path)
    store = ManagedCommandStore(tmp_path / "runs" / "run-1")
    store.settle(store.admit(identity), exit_code=3)
    corrupt = store.receipts / f"{'f' * 64}.broken.json"
    corrupt.write_text("not json", encoding="utf-8")

    records = store.evidence()

    assert len(records) == 2
    assert any(
        record.state is ManagedCommandState.EXITED and record.exit_code == 3
        for record in records
    )
    degraded = next(record for record in records if record.degraded_reason)
    assert degraded.state is ManagedCommandState.UNKNOWN
    assert degraded.identity_digest == "f" * 64
