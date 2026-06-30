"""Global+profile sandbox-config merge + mode rejection."""
from __future__ import annotations

import pytest

from pipeline.sandbox.policy import (
    SandboxMode,
    SandboxPolicy,
)
from pipeline.sandbox.resolver import (
    SandboxConfigError,
    materialize_masker,
    resolve_sandbox_policy,
)


def test_empty_config_yields_default_env_policy() -> None:
    p = resolve_sandbox_policy(global_config={}, profile_override=None)
    assert isinstance(p, SandboxPolicy)
    assert p.mode is SandboxMode.ENV


def test_global_off_disables_isolation() -> None:
    p = resolve_sandbox_policy(global_config={"mode": "off"}, profile_override=None)
    assert p.mode is SandboxMode.OFF
    assert p.isolation_active is False


def test_profile_overrides_global_mode() -> None:
    p = resolve_sandbox_policy(
        global_config={"mode": "env"},
        profile_override={"mode": "off"},
    )
    assert p.mode is SandboxMode.OFF


def test_allowlist_merges_global_and_profile() -> None:
    p = resolve_sandbox_policy(
        global_config={"env_allowlist": ["GLOBAL_TOK"]},
        profile_override={"env_allowlist": ["PROFILE_TOK"]},
    )
    assert "GLOBAL_TOK" in p.env_allowlist
    assert "PROFILE_TOK" in p.env_allowlist


def test_native_mode_is_not_a_valid_enum_value() -> None:
    # ``native`` and ``container`` were earlier draft enum values for
    # never-built L2 / L4 backends. They are not in the schema today
    # — the resolver must reject them as unknown, not "deferred".
    with pytest.raises(SandboxConfigError, match="is not one of"):
        resolve_sandbox_policy(
            global_config={"mode": "native"}, profile_override=None,
        )


def test_container_mode_is_not_a_valid_enum_value() -> None:
    with pytest.raises(SandboxConfigError, match="is not one of"):
        resolve_sandbox_policy(
            global_config={"mode": "container"}, profile_override=None,
        )


def test_unknown_mode_value_rejected() -> None:
    with pytest.raises(SandboxConfigError, match="is not one of"):
        resolve_sandbox_policy(
            global_config={"mode": "fortified"}, profile_override=None,
        )


def test_unknown_top_key_rejected() -> None:
    with pytest.raises(SandboxConfigError, match="unknown keys"):
        resolve_sandbox_policy(
            global_config={"strict_mode": True}, profile_override=None,
        )


def test_network_key_rejected_as_unknown() -> None:
    # ``network`` is not in the schema — orcho does not gate egress.
    with pytest.raises(SandboxConfigError, match="unknown keys"):
        resolve_sandbox_policy(
            global_config={"network": "open"}, profile_override=None,
        )


def test_proxy_key_rejected_as_unknown() -> None:
    with pytest.raises(SandboxConfigError, match="unknown keys"):
        resolve_sandbox_policy(
            global_config={"proxy": {"allowed_hosts": []}},
            profile_override=None,
        )


def test_negative_limit_rejected() -> None:
    with pytest.raises(SandboxConfigError, match="cpu_seconds"):
        resolve_sandbox_policy(
            global_config={"limits": {"cpu_seconds": -1}},
            profile_override=None,
        )


def test_bad_masking_regex_surfaces_at_resolve() -> None:
    with pytest.raises(SandboxConfigError, match="masking"):
        resolve_sandbox_policy(
            global_config={"masking": {"custom_patterns": [r"sk-[unclosed"]}},
            profile_override=None,
        )


def test_materialize_masker_active_when_builtin_on() -> None:
    p = resolve_sandbox_policy(global_config={"mode": "env"}, profile_override=None)
    m = materialize_masker(p)
    assert m.active is True


def test_materialize_masker_inactive_when_builtin_off_and_no_custom() -> None:
    p = resolve_sandbox_policy(
        global_config={"masking": {"builtin_patterns": False, "custom_patterns": []}},
        profile_override=None,
    )
    m = materialize_masker(p)
    assert m.active is False
