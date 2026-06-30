"""Provider-neutral runtime identity diagnostics.

Covers the value object (:class:`RuntimeIdentity`) and the best-effort
dispatcher (:func:`probe_runtime_identity`). The dispatcher must NEVER raise:
a runtime without the optional ``probe_identity`` capability, or one whose
probe blows up / returns junk, resolves to a clean ``unavailable`` identity so
run setup can never be broken by an identity probe.

These tests also lock the safety contract: an ``unavailable`` identity carries
no account/email, and ``hint()`` renders nothing for it.
"""

from __future__ import annotations

from agents.runtimes.identity import RuntimeIdentity, probe_runtime_identity


class TestRuntimeIdentityShape:
    def test_available_carries_sanitized_fields(self) -> None:
        ident = RuntimeIdentity(
            runtime="claude",
            source="runtime_status",
            available=True,
            provider="anthropic",
            account_label="Smart-gamma",
            email="sales@example.com",
        )
        assert ident.available is True
        assert ident.account_label == "Smart-gamma"
        assert ident.email == "sales@example.com"
        assert ident.hint() == "account=Smart-gamma / sales@example.com"

    def test_unavailable_clears_sensitive_fields(self) -> None:
        ident = RuntimeIdentity.unavailable("claude", "no_status_surface")
        assert ident.available is False
        assert ident.source == "no_status_surface"
        assert ident.provider is None
        assert ident.account_label is None
        assert ident.email is None
        assert ident.hint() == ""

    def test_hint_with_only_label(self) -> None:
        ident = RuntimeIdentity(
            runtime="claude", source="runtime_status", available=True,
            account_label="Smart-gamma", email=None,
        )
        assert ident.hint() == "account=Smart-gamma"

    def test_hint_with_only_email(self) -> None:
        ident = RuntimeIdentity(
            runtime="claude", source="runtime_status", available=True,
            account_label=None, email="sales@example.com",
        )
        assert ident.hint() == "account=sales@example.com"

    def test_hint_suppresses_duplicate_email_label(self) -> None:
        ident = RuntimeIdentity(
            runtime="claude", source="runtime_status", available=True,
            account_label="sales@example.com", email="sales@example.com",
        )
        assert ident.hint() == "account=sales@example.com"

    def test_hint_suppresses_provider_default_org_label(self) -> None:
        ident = RuntimeIdentity(
            runtime="claude", source="runtime_status", available=True,
            account_label="jekccs@gmail.com's Organization",
            email="jekccs@gmail.com",
        )
        assert ident.hint() == "account=jekccs@gmail.com"

    def test_available_but_empty_renders_no_hint(self) -> None:
        ident = RuntimeIdentity(
            runtime="claude", source="runtime_status", available=True,
            account_label="  ", email="",
        )
        assert ident.hint() == ""


class TestProbeRuntimeIdentityDispatch:
    def test_agent_without_probe_method_is_unavailable(self) -> None:
        class _Bare:
            runtime = "claude"

        ident = probe_runtime_identity(_Bare())
        assert ident.available is False
        assert ident.runtime == "claude"
        assert ident.source == "unsupported"

    def test_agent_with_raising_probe_does_not_propagate(self) -> None:
        class _Boom:
            runtime = "codex"

            def probe_identity(self) -> RuntimeIdentity:
                raise RuntimeError("status command exploded")

        ident = probe_runtime_identity(_Boom())
        assert ident.available is False
        assert ident.runtime == "codex"
        assert ident.source == "probe_error"

    def test_agent_returning_identity_is_passed_through(self) -> None:
        expected = RuntimeIdentity(
            runtime="claude", source="runtime_status", available=True,
            account_label="Org", email="a@b.c",
        )

        class _Good:
            runtime = "claude"

            def probe_identity(self) -> RuntimeIdentity:
                return expected

        assert probe_runtime_identity(_Good()) is expected

    def test_agent_returning_junk_is_unavailable(self) -> None:
        class _Junk:
            runtime = "claude"

            def probe_identity(self):
                return {"email": "a@b.c"}  # not a RuntimeIdentity

        ident = probe_runtime_identity(_Junk())
        assert ident.available is False
        assert ident.source == "unavailable"

    def test_missing_runtime_attr_falls_back_to_unknown(self) -> None:
        ident = probe_runtime_identity(object())
        assert ident.runtime == "unknown"
        assert ident.available is False


class TestRuntimeIdentitySanitization:
    """Structural guarantee: the value object has no field that could carry a
    secret. No token / credential / cookie / auth-path field exists, so a
    producer cannot accidentally leak one through this shape."""

    def test_fields_are_exactly_the_sanitized_set(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(RuntimeIdentity)}
        assert field_names == {
            "runtime", "source", "available",
            "provider", "account_label", "email",
        }
        forbidden = {"token", "access_token", "refresh_token", "cookie",
                     "credentials", "auth_path", "auth_file", "raw"}
        assert field_names & forbidden == set()
