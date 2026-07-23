"""Cheap typed verification-subject fixtures for policy-level tests.

Real worktree snapshot coverage lives in the focused integration slice.  Unit
tests consume these values so receipt policy can be exercised without spawning
Git once per classification.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from pipeline.verification_subject import (
    VerificationSubjectAvailable,
    VerificationSubjectCapture,
    VerificationSubjectIdentity,
    VerificationSubjectUnavailable,
)

DEFAULT_VERIFICATION_SUBJECT = VerificationSubjectIdentity(
    version=1,
    object_format="sha1",
    tree_oid="1" * 40,
    observed_head_oid="2" * 40,
    baseline_oid=None,
)


@dataclass
class FakeVerificationSubjectCapture:
    """Mutable path-aware capture fake for subject freshness transitions."""

    default: VerificationSubjectCapture = VerificationSubjectAvailable(
        DEFAULT_VERIFICATION_SUBJECT,
    )

    def __post_init__(self) -> None:
        self._by_path: dict[str, VerificationSubjectCapture] = {}

    def __call__(
        self,
        checkout: Path,
        *,
        baseline_ref: str | None = None,
    ) -> VerificationSubjectCapture:
        capture = self._by_path.get(str(Path(checkout)), self.default)
        if (
            baseline_ref is not None
            and isinstance(capture, VerificationSubjectAvailable)
            and capture.identity.baseline_oid is None
        ):
            return VerificationSubjectAvailable(
                replace(
                    capture.identity,
                    baseline_oid=capture.identity.observed_head_oid,
                ),
            )
        return capture

    def set_identity(
        self,
        checkout: Path,
        *,
        tree_oid: str | None = None,
        observed_head_oid: str | None = None,
        baseline_oid: str | None = None,
    ) -> VerificationSubjectIdentity:
        current = self(checkout)
        identity = (
            current.identity
            if isinstance(current, VerificationSubjectAvailable)
            else DEFAULT_VERIFICATION_SUBJECT
        )
        updated = replace(
            identity,
            tree_oid=tree_oid or identity.tree_oid,
            observed_head_oid=observed_head_oid or identity.observed_head_oid,
            baseline_oid=baseline_oid,
        )
        self._by_path[str(Path(checkout))] = VerificationSubjectAvailable(updated)
        return updated

    def set_unavailable(self, checkout: Path, reason: str) -> None:
        self._by_path[str(Path(checkout))] = VerificationSubjectUnavailable(reason)


_CAPTURE_TARGETS = (
    "pipeline.verification_subject.capture_verification_subject",
    "pipeline.verification_command.capture_verification_subject",
    "pipeline.verification_dependencies.capture_verification_subject",
)


@pytest.fixture
def fake_verification_subject_capture(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> FakeVerificationSubjectCapture:
    """Install one typed subject source across unit-level receipt consumers."""
    fake = FakeVerificationSubjectCapture()
    for target in _CAPTURE_TARGETS:
        monkeypatch.setattr(target, fake)
    if hasattr(request.module, "capture_verification_subject"):
        monkeypatch.setattr(request.module, "capture_verification_subject", fake)
    return fake
