"""ContextVar contract for active sandbox policy."""
from __future__ import annotations

from pipeline.sandbox.context import (
    get_active_sandbox_policy,
    reset_active_sandbox_policy,
    set_active_sandbox_policy,
)
from pipeline.sandbox.policy import SandboxMode, SandboxPolicy


def test_default_is_none() -> None:
    assert get_active_sandbox_policy() is None


def test_set_and_reset_round_trip() -> None:
    policy = SandboxPolicy(mode=SandboxMode.ENV)
    token = set_active_sandbox_policy(policy)
    try:
        assert get_active_sandbox_policy() is policy
    finally:
        reset_active_sandbox_policy(token)
    assert get_active_sandbox_policy() is None


def test_nested_set_restores_previous() -> None:
    outer = SandboxPolicy(mode=SandboxMode.OFF)
    inner = SandboxPolicy(mode=SandboxMode.ENV)
    t1 = set_active_sandbox_policy(outer)
    try:
        t2 = set_active_sandbox_policy(inner)
        try:
            assert get_active_sandbox_policy() is inner
        finally:
            reset_active_sandbox_policy(t2)
        assert get_active_sandbox_policy() is outer
    finally:
        reset_active_sandbox_policy(t1)
    assert get_active_sandbox_policy() is None
