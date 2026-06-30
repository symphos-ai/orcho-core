# SPDX-License-Identifier: Apache-2.0
"""ADR 0113 T1 — the session-disposition policy projection.

Pins the single continuity decision keyed on the declarative
:class:`SessionContinuity` policy (resolved from the profile above the stack):

- ``fresh_only`` → always FRESH,
- ``loop_continue`` → CONTINUE iff ``loop_followon``,
- ``same_zone_continue`` → CONTINUE iff ``same_write_zone``.

Also pins the projection's totality over the closed policy vocabulary, its
loud failure on an unknown member, and its purity (determinism + fail-fast
typing).
"""
from __future__ import annotations

from enum import StrEnum

import pytest

from pipeline.runtime.roles import SessionContinuity
from pipeline.runtime.run_shape import OperatingMode
from pipeline.runtime.session_disposition import (
    SessionDisposition,
    decide,
)

_ALL_MODES = tuple(OperatingMode)
_ALL_POLICIES = tuple(SessionContinuity)


# ── policy source ─────────────────────────────────────────────────────


class TestPolicySource:
    def test_is_a_strenum(self) -> None:
        assert issubclass(SessionContinuity, StrEnum)

    def test_carries_the_three_members(self) -> None:
        assert {p.value for p in SessionContinuity} == {
            "fresh_only",
            "loop_continue",
            "same_zone_continue",
        }


# ── decision table (keyed on SessionContinuity) ───────────────────────


class TestDecisionTable:
    @pytest.mark.parametrize("same_write_zone", [True, False])
    @pytest.mark.parametrize("loop_followon", [True, False])
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_fresh_only_is_always_fresh(
        self, same_write_zone: bool, loop_followon: bool, mode: OperatingMode
    ) -> None:
        disp = decide(
            policy=SessionContinuity.FRESH_ONLY,
            same_write_zone=same_write_zone,
            loop_followon=loop_followon,
            operating_mode=mode,
        )
        assert disp.continue_session is False
        assert SessionContinuity.FRESH_ONLY.value in disp.reason

    @pytest.mark.parametrize("loop_followon", [True, False])
    @pytest.mark.parametrize("same_write_zone", [True, False])
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_loop_continue_follows_loop_signal(
        self, loop_followon: bool, same_write_zone: bool, mode: OperatingMode
    ) -> None:
        # loop_continue keys only on loop_followon, never on the write zone.
        disp = decide(
            policy=SessionContinuity.LOOP_CONTINUE,
            same_write_zone=same_write_zone,
            loop_followon=loop_followon,
            operating_mode=mode,
        )
        assert disp.continue_session is loop_followon
        assert SessionContinuity.LOOP_CONTINUE.value in disp.reason

    @pytest.mark.parametrize("same_write_zone", [True, False])
    @pytest.mark.parametrize("loop_followon", [True, False])
    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_same_zone_continue_follows_write_zone(
        self, same_write_zone: bool, loop_followon: bool, mode: OperatingMode
    ) -> None:
        # same_zone_continue keys only on same_write_zone, never on the loop.
        disp = decide(
            policy=SessionContinuity.SAME_ZONE_CONTINUE,
            same_write_zone=same_write_zone,
            loop_followon=loop_followon,
            operating_mode=mode,
        )
        assert disp.continue_session is same_write_zone
        assert SessionContinuity.SAME_ZONE_CONTINUE.value in disp.reason

    def test_named_examples(self) -> None:
        # plan/validate (loop_continue) round 2+ → CONTINUE (regression fix);
        # review (fresh_only) → FRESH; implement (same_zone_continue)
        # same-zone follow-on → CONTINUE.
        m = OperatingMode.PRO
        assert decide(
            policy=SessionContinuity.LOOP_CONTINUE,
            same_write_zone=False,
            loop_followon=True,
            operating_mode=m,
        ).continue_session is True
        assert decide(
            policy=SessionContinuity.FRESH_ONLY,
            same_write_zone=True,
            loop_followon=True,
            operating_mode=m,
        ).continue_session is False
        assert decide(
            policy=SessionContinuity.SAME_ZONE_CONTINUE,
            same_write_zone=True,
            loop_followon=False,
            operating_mode=m,
        ).continue_session is True


# ── totality / purity ─────────────────────────────────────────────────


class TestTotalityAndPurity:
    @pytest.mark.parametrize("policy", _ALL_POLICIES)
    @pytest.mark.parametrize("same_write_zone", [True, False])
    @pytest.mark.parametrize("loop_followon", [True, False])
    def test_total_over_closed_vocabulary(
        self,
        policy: SessionContinuity,
        same_write_zone: bool,
        loop_followon: bool,
    ) -> None:
        # Every member is handled — no fall-through / no None for any policy.
        disp = decide(
            policy=policy,
            same_write_zone=same_write_zone,
            loop_followon=loop_followon,
            operating_mode=OperatingMode.FAST,
        )
        assert isinstance(disp, SessionDisposition)
        assert isinstance(disp.continue_session, bool)
        assert disp.reason

    def test_loud_fail_on_unknown_policy_member(self) -> None:
        # A value that passes the isinstance check but is not one of the three
        # handled members must hit the exhaustiveness guard rather than
        # silently defaulting to fresh. StrEnum cannot be extended with new
        # members once defined, so spoof an instance with an unhandled value.
        sentinel = str.__new__(SessionContinuity, "unknown")
        object.__setattr__(sentinel, "_name_", "UNKNOWN")
        object.__setattr__(sentinel, "_value_", "unknown")
        with pytest.raises(AssertionError):
            decide(
                policy=sentinel,  # type: ignore[arg-type]
                same_write_zone=True,
                loop_followon=True,
                operating_mode=OperatingMode.FAST,
            )

    def test_deterministic(self) -> None:
        kw = dict(
            policy=SessionContinuity.SAME_ZONE_CONTINUE,
            same_write_zone=True,
            loop_followon=False,
            operating_mode=OperatingMode.FAST,
        )
        assert decide(**kw) == decide(**kw)

    def test_rejects_non_policy_input(self) -> None:
        with pytest.raises(TypeError):
            decide(
                policy="same_zone_continue",  # type: ignore[arg-type]
                same_write_zone=True,
                loop_followon=False,
                operating_mode=OperatingMode.FAST,
            )

    def test_rejects_non_operating_mode_input(self) -> None:
        with pytest.raises(TypeError):
            decide(
                policy=SessionContinuity.SAME_ZONE_CONTINUE,
                same_write_zone=True,
                loop_followon=False,
                operating_mode="fast",  # type: ignore[arg-type]
            )
