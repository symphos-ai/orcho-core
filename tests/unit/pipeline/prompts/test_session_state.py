"""Unit tests for the M5 prompt-session primitives.

Covers the pure key-selection policy, the canonical sent-part key
helper (id + version), the immutable mutation helpers, and the
boundary regression that asserts :class:`PromptSessionSplit` is not
the same enum as :class:`agents.protocols.SessionMode` — the two
govern different layers and must never be conflated.
"""

from __future__ import annotations

import pytest

from agents.protocols import SessionMode
from pipeline.prompts.session import (
    PhysicalSessionKey,
    PromptSessionSplit,
    PromptSessionState,
    make_session_key,
    part_session_key,
    record_sent_parts,
    with_active_contracts,
    with_active_phase,
    with_active_role,
    with_provider_session_id,
)
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptPart,
    PromptStability,
)

# ---------------------------------------------------------------------------
# Test fixtures: tiny part builders that satisfy M1 validation but
# stay independent of the prompt loader / composer pipeline.
# ---------------------------------------------------------------------------


def _static_part(name: str, *, version: int | None = None) -> PromptPart:
    return PromptPart(
        kind="role",
        name=name,
        source="core",
        body="body",
        version=version,
    )


def _turn_part(name: str) -> PromptPart:
    return PromptPart(
        kind="task",
        name=name,
        source="core",
        body="body",
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="per-turn substitution",
    )


# ---------------------------------------------------------------------------
# make_session_key — pure policy, exhaustive over PromptSessionSplit.
# ---------------------------------------------------------------------------


class TestMakeSessionKey:
    def _key(self, **kwargs):
        defaults = dict(
            run_id="run-1",
            runtime="claude",
            model_key="claude-opus-4-7",
            split=PromptSessionSplit.PER_PHASE,
            phase="validate_plan",
        )
        defaults.update(kwargs)
        return make_session_key(**defaults)

    def test_per_phase_same_run_runtime_model_phase_yields_same_key(
        self,
    ) -> None:
        a = self._key()
        b = self._key()
        assert a == b
        assert isinstance(a, PhysicalSessionKey)

    def test_different_runtime_yields_different_key_in_every_reusable_mode(
        self,
    ) -> None:
        for split, kwargs in (
            (PromptSessionSplit.PER_PHASE, {"phase": "validate_plan"}),
            (PromptSessionSplit.PER_ROLE, {"role": "code_reviewer"}),
            (PromptSessionSplit.COMMON, {}),
        ):
            a = self._key(split=split, runtime="claude", **kwargs)
            b = self._key(split=split, runtime="codex", **kwargs)
            assert a != b
            # Spot-check the runtime field actually flipped.
            assert a is not None and b is not None
            assert a.runtime != b.runtime

    def test_different_model_yields_different_key_by_default(self) -> None:
        a = self._key(model_key="claude-opus-4-7")
        b = self._key(model_key="claude-sonnet-4-7")
        assert a != b

    def test_common_scope_keyed_by_runtime_and_model(self) -> None:
        a = self._key(split=PromptSessionSplit.COMMON)
        b = self._key(split=PromptSessionSplit.COMMON)
        assert a == b
        # Different runtime under COMMON still splits.
        c = self._key(split=PromptSessionSplit.COMMON, runtime="codex")
        assert a != c
        # Different model under COMMON still splits.
        d = self._key(split=PromptSessionSplit.COMMON, model_key="other")
        assert a != d
        # Scope value itself is just "common" — no role/phase suffix.
        assert a is not None and a.scope == "common"

    def test_stateless_yields_no_key(self) -> None:
        # Stateless = no reusable physical prompt session key. Returning
        # ``None`` (not a sentinel) keeps callers honest about handling
        # the "no session" path explicitly.
        result = make_session_key(
            run_id="run-1",
            runtime="claude",
            model_key="claude-opus-4-7",
            split=PromptSessionSplit.STATELESS,
        )
        assert result is None

    def test_per_phase_without_phase_raises(self) -> None:
        with pytest.raises(ValueError, match="PER_PHASE"):
            make_session_key(
                run_id="run-1",
                runtime="claude",
                model_key="claude-opus-4-7",
                split=PromptSessionSplit.PER_PHASE,
                # phase intentionally omitted
            )

    def test_per_role_without_role_raises(self) -> None:
        with pytest.raises(ValueError, match="PER_ROLE"):
            make_session_key(
                run_id="run-1",
                runtime="claude",
                model_key="claude-opus-4-7",
                split=PromptSessionSplit.PER_ROLE,
                # role intentionally omitted
            )

    def test_per_phase_scope_includes_phase_name(self) -> None:
        key = self._key(phase="validate_plan")
        assert key is not None
        assert key.scope == "per_phase:validate_plan"

    def test_per_role_scope_includes_role_name(self) -> None:
        key = self._key(
            split=PromptSessionSplit.PER_ROLE,
            role="code_reviewer",
            phase=None,
        )
        assert key is not None
        assert key.scope == "per_role:code_reviewer"

    def test_same_scope_with_different_run_id_yields_different_keys(
        self,
    ) -> None:
        a = self._key(run_id="run-1")
        b = self._key(run_id="run-2")
        assert a != b


# ---------------------------------------------------------------------------
# part_session_key — composite id + version identity.
# ---------------------------------------------------------------------------


class TestPartSessionKey:
    def test_part_session_key_includes_version(self) -> None:
        # ADR 0026: a version bump on the same id must be treated as
        # unseen by M6. The composite key must therefore differ
        # between v1 and v2 for the same part name.
        v1 = _static_part("plan_json", version=1)
        v2 = _static_part("plan_json", version=2)
        assert part_session_key(v1) != part_session_key(v2)
        assert part_session_key(v1).endswith("@1")
        assert part_session_key(v2).endswith("@2")

    def test_missing_version_treated_as_zero(self) -> None:
        unversioned = _static_part("plan_json")
        assert part_session_key(unversioned).endswith("@0")

    def test_same_id_same_version_yields_same_key(self) -> None:
        a = _static_part("role_a", version=3)
        b = _static_part("role_a", version=3)
        assert part_session_key(a) == part_session_key(b)


# ---------------------------------------------------------------------------
# PromptSessionState mutation helpers — pure / immutable.
# ---------------------------------------------------------------------------


class TestStateMutationHelpers:
    @pytest.fixture
    def fresh_state(self) -> PromptSessionState:
        key = make_session_key(
            run_id="run-1",
            runtime="claude",
            model_key="claude-opus-4-7",
            split=PromptSessionSplit.PER_PHASE,
            phase="validate_plan",
        )
        assert key is not None
        return PromptSessionState(key=key)

    def test_state_record_then_query_sent_part_keys(
        self, fresh_state: PromptSessionState,
    ) -> None:
        parts = (
            _static_part("plan_json", version=1),
            _static_part("plan_artifact_boundary"),
        )
        updated = record_sent_parts(fresh_state, parts)
        assert updated.sent_part_keys == {
            "role:plan_json@1",
            "role:plan_artifact_boundary@0",
        }
        # Original state untouched (frozen dataclass + replace pattern).
        assert fresh_state.sent_part_keys == frozenset()

    def test_record_sent_parts_unions_with_existing(
        self, fresh_state: PromptSessionState,
    ) -> None:
        first = record_sent_parts(fresh_state, (_static_part("a"),))
        second = record_sent_parts(first, (_static_part("b"),))
        assert second.sent_part_keys == {"role:a@0", "role:b@0"}
        # Key (PhysicalSessionKey) is preserved across mutations.
        assert second.key == fresh_state.key

    def test_with_provider_session_id_updates_field_only(
        self, fresh_state: PromptSessionState,
    ) -> None:
        updated = with_provider_session_id(fresh_state, "sess-abc")
        assert updated.session_id == "sess-abc"
        assert fresh_state.session_id is None

    def test_with_active_role_updates_field_only(
        self, fresh_state: PromptSessionState,
    ) -> None:
        updated = with_active_role(fresh_state, "role:code_reviewer")
        assert updated.active_role_id == "role:code_reviewer"
        assert fresh_state.active_role_id is None

    def test_with_active_phase_updates_field_only(
        self, fresh_state: PromptSessionState,
    ) -> None:
        updated = with_active_phase(fresh_state, "validate_plan")
        assert updated.active_phase_id == "validate_plan"
        assert fresh_state.active_phase_id is None

    def test_with_active_contracts_accepts_iterable(
        self, fresh_state: PromptSessionState,
    ) -> None:
        # frozenset and any iterable both work; the helper normalizes.
        a = with_active_contracts(fresh_state, ["c1", "c2"])
        b = with_active_contracts(fresh_state, frozenset({"c1", "c2"}))
        assert a.active_contract_ids == frozenset({"c1", "c2"})
        assert a.active_contract_ids == b.active_contract_ids

    def test_helpers_are_chainable(
        self, fresh_state: PromptSessionState,
    ) -> None:
        # M7 will compose these around a successful invocation: record
        # what was sent, update the provider session id, advance the
        # active anchors. Verify the sequence composes without
        # surprises.
        out = record_sent_parts(fresh_state, (_static_part("plan_json"),))
        out = with_provider_session_id(out, "sess-xyz")
        out = with_active_role(out, "role:code_reviewer")
        out = with_active_phase(out, "validate_plan")
        out = with_active_contracts(out, ["contract:plan_json:v1"])
        assert "role:plan_json@0" in out.sent_part_keys
        assert out.session_id == "sess-xyz"
        assert out.active_role_id == "role:code_reviewer"
        assert out.active_phase_id == "validate_plan"
        assert out.active_contract_ids == frozenset({"contract:plan_json:v1"})
        # Original state still pristine.
        assert fresh_state.sent_part_keys == frozenset()
        assert fresh_state.session_id is None


# ---------------------------------------------------------------------------
# Boundary regression: PromptSessionSplit must stay distinct from the
# runtime SessionMode enum. They govern different layers and must
# never be aliased or conflated.
# ---------------------------------------------------------------------------


class TestEnumBoundary:
    def test_prompt_session_split_does_not_reuse_runtime_session_mode_enum(
        self,
    ) -> None:
        # Distinct classes — not a subclass relationship, not the
        # same identity.
        assert PromptSessionSplit is not SessionMode
        assert not issubclass(PromptSessionSplit, SessionMode)
        assert not issubclass(SessionMode, PromptSessionSplit)

        # Distinct member sets. Runtime SessionMode owns AUTO / CHAIN
        # / HYBRID; prompt-session split owns PER_PHASE / PER_ROLE /
        # COMMON. Both happen to ship a "stateless" member, but its
        # semantics differ (no bridge vs no reusable prompt key) and
        # that's exactly the conflation this test guards against —
        # callers must address each enum by its full type, not by
        # passing one where the other is expected.
        prompt_members = {m.name for m in PromptSessionSplit}
        runtime_members = {m.name for m in SessionMode}
        assert prompt_members == {"STATELESS", "PER_PHASE", "PER_ROLE", "COMMON"}
        assert runtime_members == {"AUTO", "STATELESS", "CHAIN", "HYBRID"}

        # Prompt-session module must not import or alias the runtime
        # enum. Docstrings are allowed to mention the name for
        # documentation purposes (this module's own docstring does,
        # to make the boundary explicit), so the test only flags
        # actual ``import`` references — those would mean the module
        # is functionally coupled to the runtime enum, not just
        # documenting the boundary.
        import inspect

        from pipeline.prompts import session as session_module

        source = inspect.getsource(session_module)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "import SessionMode" not in stripped, (
                "pipeline/prompts/session.py must not import the runtime "
                "SessionMode enum — prompt-session split is a separate "
                "concept governed by PromptSessionSplit."
            )
            assert "from agents.protocols" not in stripped, (
                "pipeline/prompts/session.py must not depend on "
                "agents.protocols — runtime Protocol redesign is "
                "deferred and prompt-session state lives Orcho-side only."
            )

    def test_prompt_session_split_member_string_values(self) -> None:
        # StrEnum: members compare equal to their string values, so
        # tracing/serialization stays human-readable.
        assert PromptSessionSplit.STATELESS == "stateless"
        assert PromptSessionSplit.PER_PHASE == "per_phase"
        assert PromptSessionSplit.PER_ROLE == "per_role"
        assert PromptSessionSplit.COMMON == "common"
