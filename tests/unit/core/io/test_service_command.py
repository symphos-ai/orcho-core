from __future__ import annotations

from core.io.service_command import bounded_command_identity


def test_bounded_command_identity_clips_long_argv() -> None:
    identity = bounded_command_identity(["python", "-c", "x" * 1000])

    assert len(identity) == 512
    assert identity.endswith("…")


def test_bounded_command_identity_does_not_accept_process_inputs() -> None:
    # The public helper has no env/stdin parameters: a service breadcrumb can
    # only be formed from argv identity at this boundary.
    identity = bounded_command_identity(["python", "-c", "print('ok')"])

    assert identity.startswith("python")


def test_command_identity_accepts_a_string_command() -> None:
    assert bounded_command_identity("git status --porcelain") == "git status --porcelain"


def test_command_identity_falls_back_for_unjoinable_argv() -> None:
    """The identity is diagnostic text, so a caller passing something argv-shaped
    but not iterable-of-str must still produce a readable label, not raise."""
    assert bounded_command_identity(42) == "42"
