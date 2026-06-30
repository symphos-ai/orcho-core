# SPDX-License-Identifier: Apache-2.0
"""ADR 0115 slice 2 — single release-verdict source + blocked-predicate parity.

Pins the canonical classifier (``pipeline/run_state/release_verdict.py``) and a
grep-invariant: the non-``APPROVED`` blocked-predicate literal lives in exactly
one place (plus the deliberately-deferred cross site), so a new open-coded
release-blocked derivation cannot silently reappear.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.run_state.release_verdict import (
    APPROVED,
    REJECTED,
    is_approved,
    is_rejected,
    is_release_blocked,
    normalize_verdict,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("APPROVED", "APPROVED"),
        (" approved ", "APPROVED"),
        ("rejected", "REJECTED"),
        ("", ""),
        (None, ""),
        ("weird", "WEIRD"),
    ],
)
def test_normalize_verdict(raw, expected) -> None:
    assert normalize_verdict(raw) == expected


def test_constants() -> None:
    assert APPROVED == "APPROVED"
    assert REJECTED == "REJECTED"


def test_is_approved_is_rejected() -> None:
    assert is_approved("APPROVED")
    assert is_approved(" approved ")  # normalized
    assert not is_approved("REJECTED")
    assert not is_approved("")
    assert not is_approved(None)
    assert is_rejected("REJECTED")
    assert is_rejected(" rejected ")
    assert not is_rejected("APPROVED")
    assert not is_rejected("")
    assert not is_rejected(None)
    # A non-APPROVED verdict that is not literally REJECTED is neither approved
    # nor rejected (blocked, but not rejected — the distinction slice 2 keeps).
    assert not is_approved("weird")
    assert not is_rejected("weird")


@pytest.mark.parametrize(
    "verdict,empty_blocks,expected",
    [
        # APPROVED never blocks, under either empty policy.
        ("APPROVED", True, False),
        ("APPROVED", False, False),
        (" approved ", False, False),  # normalized
        # A present non-APPROVED verdict always blocks.
        ("REJECTED", True, True),
        ("REJECTED", False, True),
        ("weird", True, True),
        ("weird", False, True),
        # The one legitimate per-consumer difference: empty/missing verdict.
        # commit_delivery (empty_blocks=True): a gating profile with no APPROVED
        # is blocked. SDK / run.py (empty_blocks=False): nothing to refuse on.
        ("", True, True),
        ("", False, False),
        (None, True, True),
        (None, False, False),
    ],
)
def test_is_release_blocked_parity(verdict, empty_blocks, expected) -> None:
    assert is_release_blocked(verdict, empty_blocks=empty_blocks) is expected


def test_blocked_predicate_is_single_source() -> None:
    """The ``!= "APPROVED"`` blocked literal lives only in the single source.

    Grep-invariant regression: any consumer re-introducing an open-coded
    non-approved release guard (instead of calling ``is_release_blocked`` /
    ``is_approved``) makes this fail. The cross-delivery per-child guard was
    folded onto the single source in ADR 0115 slice 4, so it is no longer
    whitelisted — ``release_verdict.py`` is the only allowed site.
    """
    repo_root = Path(__file__).resolve().parents[4]
    allowed = {
        "pipeline/run_state/release_verdict.py",  # the single source (+ its docstring)
    }
    literal = re.compile(r'!=\s*"APPROVED"')
    offenders: list[str] = []
    for base in ("pipeline", "sdk"):
        for path in (repo_root / base).rglob("*.py"):
            rel = path.relative_to(repo_root).as_posix()
            if "/tests/" in rel or path.name.startswith("test_"):
                continue
            if rel in allowed:
                continue
            if literal.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
    assert not offenders, (
        "open-coded non-approved release guard outside the single source: "
        f"{offenders} — route them through release_verdict.is_release_blocked()"
    )


def test_finalization_verdict_detectors_routed() -> None:
    """Slice-3a: finalization's reducer-side verdict detectors read the source.

    The release-verdict re-judges in ``finalization.py`` (the no-diff outcome
    detectors, the rejected-signal detector, the supersede-residue guard, and the
    DONE-summary mappers) were collapsed onto ``release_verdict`` — none may
    re-open-code a ``verdict.upper() == "APPROVED"/"REJECTED"`` detector.
    """
    repo_root = Path(__file__).resolve().parents[4]
    src = (repo_root / "pipeline/project/finalization.py").read_text(
        encoding="utf-8",
    )
    detector = re.compile(r'verdict\.upper\(\)\s*==\s*"(APPROVED|REJECTED)"')
    assert not detector.search(src), (
        "open-coded verdict detector in finalization.py — route through "
        "release_verdict.is_approved / is_rejected"
    )
