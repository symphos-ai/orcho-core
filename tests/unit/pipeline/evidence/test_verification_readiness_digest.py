# SPDX-License-Identifier: Apache-2.0
"""Lock the evidence ``verification_readiness`` digest as observed-facts-only.

Review F2 (ADR 0089): the evidence v1 bundle's ``verification_readiness`` digest
(:func:`pipeline.evidence.collector._build_verification_readiness`) is the only
evidence surface that touches command/env receipts. It must remain *observed
facts only* — a per-receipt ``passed`` rollup and a per-env ``all_passed`` rollup
— and must NOT recompute the required-missing/failed/stale readiness verdict,
which lives solely in
:func:`pipeline.verification_readiness.classify_required_receipts`.

These tests pin both invariants so a future change cannot silently grow a second
verdict here or drift the schema-validated evidence v1 wire shape:

* the digest's wire shape (top-level keys, per-command keys, per-env keys) is
  exactly the documented set;
* no verdict key (``required`` / ``missing`` / ``failed`` / ``stale``) appears
  anywhere in the digest, even when receipts on disk include a failing command
  and the contract would mark other required commands missing;
* the digest reads ONLY the current run dir (a failing receipt is reported as an
  observed fact via ``exit_code`` / ``passed``, never as a ``failed`` verdict).
"""

from __future__ import annotations

from pathlib import Path

from pipeline.evidence.collector import _build_verification_readiness
from pipeline.evidence.verification_receipt import (
    write_command_receipt,
    write_env_assertion_receipt,
)

# The exact, documented wire shape of the digest. If any of these change, the
# evidence v1 schema and its consumers (and this lock) must change together.
_TOP_LEVEL_KEYS = {"commands", "envs"}
_COMMAND_KEYS = {
    "command", "env", "exit_code", "parity", "passed", "has_baseline",
}
_ENV_KEYS = {"env", "all_passed"}

# Tokens that would signal a readiness *verdict* leaking into the observed-facts
# digest. ``passed`` / ``all_passed`` are observed per-receipt rollups, not these.
_VERDICT_TOKENS = {"required", "missing", "failed", "stale"}


def _command_receipt(
    run_dir: Path, command: str, *, exit_code: int, env: str = "ci",
) -> None:
    write_command_receipt(output_dir=run_dir, result={
        "command": command,
        "env": env,
        "cwd": "/cwd",
        "placeholders": {"checkout": "/co", "project": "/co"},
        "argv": [command],
        "assertions": [],
        "exit_code": exit_code,
        "duration_s": 0.1,
        "parity": "absolute",
        "detail": "",
        "git": {
            "checkout_head": "abc123",
            "baseline_head": None,
            "changed_files_fingerprint": "deadbeefdeadbeef",
        },
        "dependencies": [],
    })


def _env_receipt(run_dir: Path, env: str, *, all_passed: bool) -> None:
    write_env_assertion_receipt(output_dir=run_dir, result={
        "subject": {"checkout": "/co", "project": "/co", "env": env},
        "cwd": "/co", "interpreter": "3.12", "env_overrides": {},
        "assertions": [], "all_passed": all_passed,
    })


def _digest_with_receipts(tmp_path: Path) -> dict:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # A passing and a FAILING command receipt + one env receipt. A verdict
    # surface would render the failure as "failed"; this digest must not.
    _command_receipt(run_dir, "test", exit_code=0)
    _command_receipt(run_dir, "lint", exit_code=1)
    _env_receipt(run_dir, "ci", all_passed=True)
    return _build_verification_readiness(run_dir)


def _iter_keys(obj) -> list[str]:
    """Every mapping key reachable in a nested dict/list structure."""
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(str(k))
            keys.extend(_iter_keys(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            keys.extend(_iter_keys(v))
    return keys


class TestDigestShapeIsLocked:
    def test_empty_run_has_exact_top_level_shape(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        digest = _build_verification_readiness(run_dir)
        assert set(digest.keys()) == _TOP_LEVEL_KEYS
        assert digest["commands"] == []
        assert digest["envs"] == []

    def test_command_and_env_entries_have_exact_keys(
        self, tmp_path: Path,
    ) -> None:
        digest = _digest_with_receipts(tmp_path)
        assert set(digest.keys()) == _TOP_LEVEL_KEYS
        assert digest["commands"], "expected command receipts to surface"
        for entry in digest["commands"]:
            assert set(entry.keys()) == _COMMAND_KEYS
        for entry in digest["envs"]:
            assert set(entry.keys()) == _ENV_KEYS


class TestDigestCarriesNoVerdict:
    def test_no_verdict_keys_anywhere_in_digest(self, tmp_path: Path) -> None:
        digest = _digest_with_receipts(tmp_path)
        present_keys = {k.lower() for k in _iter_keys(digest)}
        leaked = present_keys & _VERDICT_TOKENS
        assert not leaked, f"verdict tokens leaked into digest keys: {leaked}"

    def test_failing_receipt_is_observed_fact_not_failed_verdict(
        self, tmp_path: Path,
    ) -> None:
        digest = _digest_with_receipts(tmp_path)
        by_command = {e["command"]: e for e in digest["commands"]}
        # The failing command is reported via its observed exit_code / passed
        # rollup — never promoted to a "failed" readiness verdict.
        assert by_command["lint"]["exit_code"] == 1
        assert by_command["lint"]["passed"] is False
        assert by_command["test"]["exit_code"] == 0
        assert by_command["test"]["passed"] is True

    def test_digest_does_not_invent_missing_for_absent_required_command(
        self, tmp_path: Path,
    ) -> None:
        # Only "test" has a receipt; a verdict surface might list other required
        # commands as "missing". The observed-facts digest reflects ONLY what was
        # observed on disk — exactly one command entry, no missing verdict.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        _command_receipt(run_dir, "test", exit_code=0)
        digest = _build_verification_readiness(run_dir)
        assert [e["command"] for e in digest["commands"]] == ["test"]
        assert _VERDICT_TOKENS.isdisjoint(
            {k.lower() for k in _iter_keys(digest)},
        )
