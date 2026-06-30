"""Frozen-dataclass invariants for the sandbox policy shapes."""
from __future__ import annotations

import pytest

from pipeline.sandbox.policy import (
    SandboxLimits,
    SandboxMasking,
    SandboxMode,
    SandboxPolicy,
)


class TestSandboxLimits:
    def test_default_zero_means_no_limit(self) -> None:
        lim = SandboxLimits()
        assert lim.cpu_seconds == 0
        assert lim.memory_mb == 0
        assert lim.open_files == 0
        assert lim.file_size_mb == 0

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="cpu_seconds"):
            SandboxLimits(cpu_seconds=-1)

    def test_bool_rejected_for_int_field(self) -> None:
        with pytest.raises(TypeError, match="cpu_seconds"):
            SandboxLimits(cpu_seconds=True)  # type: ignore[arg-type]


class TestSandboxMasking:
    def test_default_builtin_on_no_custom(self) -> None:
        m = SandboxMasking()
        assert m.builtin_patterns is True
        assert m.custom_patterns == ()

    def test_custom_patterns_must_be_strings(self) -> None:
        with pytest.raises(ValueError, match="custom_patterns"):
            SandboxMasking(custom_patterns=("",))


class TestSandboxPolicy:
    def test_default_env_mode(self) -> None:
        p = SandboxPolicy()
        assert p.mode is SandboxMode.ENV
        assert p.isolation_active is True

    def test_off_means_no_isolation(self) -> None:
        p = SandboxPolicy(mode=SandboxMode.OFF)
        assert p.isolation_active is False

    def test_only_off_and_env_are_valid_modes(self) -> None:
        # The enum deliberately excludes ``native`` / ``container``
        # — orcho does not build L2/L4 backends, so the schema
        # does not pretend to accept them.
        assert {m.value for m in SandboxMode} == {"off", "env"}

    def test_allowlist_entries_must_be_non_empty_strings(self) -> None:
        with pytest.raises(ValueError, match="env_allowlist"):
            SandboxPolicy(env_allowlist=("",))

    def test_denylist_entries_must_be_non_empty_strings(self) -> None:
        with pytest.raises(ValueError, match="env_denylist"):
            SandboxPolicy(env_denylist=("",))

    def test_mode_must_be_enum(self) -> None:
        with pytest.raises(TypeError, match="mode"):
            SandboxPolicy(mode="env")  # type: ignore[arg-type]

    def test_is_frozen(self) -> None:
        p = SandboxPolicy()
        with pytest.raises((AttributeError, TypeError)):
            p.mode = SandboxMode.OFF  # type: ignore[misc]
