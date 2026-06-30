"""Unit tests for the M2 prefix/turn-payload render envelope.

The envelope partitions a rendered prompt's parts into a stable
cacheable prefix and a per-turn payload, exposes stable hashes over
each partition, and is published through the prompt-trace sidecar so
M6/M7 can drive delta rendering on resumed runtime sessions.

Tests cover:

* the metadata-driven prefix-eligibility rule (RUN/TURN/none excluded;
  PROFILE in prefix only with global/workspace cache scope);
* the conservative contiguous-prefix invariant (a prefix-eligible part
  rendered after a payload part lands in payload);
* hash stability under task / artifact body changes;
* the composer's turn-variable classification rule for editable parts;
* the byte-identical guarantee for the public string-returning
  prompt APIs.
"""

from __future__ import annotations

import pytest

from pipeline import prompts
from pipeline.plugins import PluginConfig
from pipeline.prompts.envelope import (
    PromptRenderEnvelope,
    is_prefix_eligible,
    make_render_envelope,
    stable_prefix_parts,
    turn_payload_parts,
)
from pipeline.prompts.types import (
    PromptCacheScope,
    PromptPart,
    PromptStability,
)

# ---------------------------------------------------------------------------
# Part fixtures: small builders that produce metadata-tagged parts the
# tests can compose into envelopes without going through the real
# prompt loader.
# ---------------------------------------------------------------------------


def _static_global(name: str, body: str = "STATIC", *, kind: str = "role") -> PromptPart:
    return PromptPart(
        kind=kind,
        name=name,
        source="core",
        body=body,
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.GLOBAL,
    )


def _profile_global(name: str, body: str = "PROFILE") -> PromptPart:
    return PromptPart(
        kind="role",
        name=name,
        source="core",
        body=body,
        stability=PromptStability.PROFILE,
        cache_scope=PromptCacheScope.GLOBAL,
        volatile_reason="depends on profile",
    )


def _profile_session(name: str, body: str = "PROFILE_SESSION") -> PromptPart:
    return PromptPart(
        kind="role",
        name=name,
        source="core",
        body=body,
        stability=PromptStability.PROFILE,
        cache_scope=PromptCacheScope.SESSION,
        volatile_reason="profile + session",
    )


def _run_part(name: str, body: str = "RUN") -> PromptPart:
    return PromptPart(
        kind="context",
        name=name,
        source="code-owned",
        body=body,
        stability=PromptStability.RUN,
        cache_scope=PromptCacheScope.SESSION,
        volatile_reason="depends on per-run config",
    )


def _turn_part(name: str, body: str = "TURN") -> PromptPart:
    return PromptPart(
        kind="task",
        name=name,
        source="core",
        body=body,
        stability=PromptStability.TURN,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="depends on per-turn substitution",
    )


def _none_scope_part(name: str, body: str = "NONE_SCOPE") -> PromptPart:
    return PromptPart(
        kind="role",
        name=name,
        source="core",
        body=body,
        stability=PromptStability.STATIC,
        cache_scope=PromptCacheScope.NONE,
        volatile_reason="always resend even when static",
    )


# ---------------------------------------------------------------------------
# Prefix-eligibility metadata rule
# ---------------------------------------------------------------------------


class TestPrefixEligibility:
    """Metadata declares where a part may live; envelope trusts it."""

    def test_static_global_is_prefix_eligible(self) -> None:
        assert is_prefix_eligible(_static_global("role_a")) is True

    def test_run_part_excluded_from_prefix(self) -> None:
        assert is_prefix_eligible(_run_part("ctx")) is False

    def test_turn_part_excluded_from_prefix(self) -> None:
        assert is_prefix_eligible(_turn_part("task_body")) is False

    def test_cache_scope_none_excluded_from_prefix(self) -> None:
        assert is_prefix_eligible(_none_scope_part("always_resend")) is False

    def test_profile_global_part_allowed_in_stable_prefix_with_reason(
        self,
    ) -> None:
        # Mirrors the M2 brief test name verbatim.
        part = _profile_global("language_anchor")
        assert is_prefix_eligible(part) is True
        # The M1 validation rule already guarantees ``volatile_reason``
        # exists for any non-static part — re-asserting here makes the
        # cross-milestone contract explicit.
        assert part.volatile_reason

    def test_profile_session_excluded_from_prefix(self) -> None:
        # PROFILE in session scope cannot live in the global prefix —
        # session-scoped parts may be reusable, but they are not
        # prefix-cacheable across runs in M2's classifier.
        assert is_prefix_eligible(_profile_session("p")) is False


# ---------------------------------------------------------------------------
# Brief test names for excluded part categories
# ---------------------------------------------------------------------------


class TestExcludedFromStablePrefix:
    """Brief-named negative tests grouped together for grep-ability."""

    def test_run_and_turn_parts_excluded_from_stable_prefix(self) -> None:
        run = _run_part("ctx")
        turn = _turn_part("task")
        env = make_render_envelope(
            text="RUN\n\nTURN", parts=(run, turn),
        )
        assert env.stable_prefix_parts == ()
        assert env.turn_payload_parts == (run, turn)

    def test_cache_scope_none_excluded_from_stable_prefix(self) -> None:
        none_p = _none_scope_part("always_resend")
        env = make_render_envelope(text="NONE_SCOPE", parts=(none_p,))
        assert env.stable_prefix_parts == ()
        assert env.turn_payload_parts == (none_p,)


# ---------------------------------------------------------------------------
# Contiguous-prefix invariant — the wire-layout rule M6/M7 relies on
# ---------------------------------------------------------------------------


class TestContiguousPrefixInvariant:
    """A prefix-eligible part rendered after a payload part is moved
    into the payload partition. The wire prompt is unchanged; only
    the metadata partition shifts."""

    def test_pure_prefix_run_is_all_prefix(self) -> None:
        a = _static_global("role")
        b = _profile_global("contract")
        env = make_render_envelope(text="A\n\nB", parts=(a, b))
        assert env.stable_prefix_parts == (a, b)
        assert env.turn_payload_parts == ()

    def test_first_payload_part_terminates_prefix(self) -> None:
        role = _static_global("role")
        task = _turn_part("task_body")
        env = make_render_envelope(text="role\n\ntask", parts=(role, task))
        assert env.stable_prefix_parts == (role,)
        assert env.turn_payload_parts == (task,)

    def test_static_after_payload_demoted_to_payload(self) -> None:
        # Conservative M2 rule: format(static) rendered after task(turn)
        # cannot stay in prefix; it would interleave with the volatile
        # body and break the omit-on-resume invariant.
        role = _static_global("role")
        task = _turn_part("task_body")
        format_part = _static_global("format", kind="format")
        env = make_render_envelope(
            text="role\n\ntask\n\nfmt",
            parts=(role, task, format_part),
        )
        assert env.stable_prefix_parts == (role,)
        assert env.turn_payload_parts == (task, format_part)

    def test_envelope_rejects_non_contiguous_prefix(self) -> None:
        role = _static_global("role")
        task = _turn_part("task_body")
        format_part = _static_global("format", kind="format")
        # Manually constructing an envelope with a non-contiguous prefix
        # must fail — the constructor invariant guards M6/M7's wire
        # layout assumption.
        with pytest.raises(ValueError, match="contiguous leading slice"):
            PromptRenderEnvelope(
                text="role\n\ntask\n\nfmt",
                parts=(role, task, format_part),
                stable_prefix_parts=(role, format_part),
                turn_payload_parts=(task,),
                prefix_hash="x",
                payload_hash="y",
            )


# ---------------------------------------------------------------------------
# Partition exhaustion and disjointness
# ---------------------------------------------------------------------------


class TestPartitionExhaustion:
    def test_prefix_and_payload_must_exhaust_parts(self) -> None:
        role = _static_global("role")
        with pytest.raises(ValueError, match="exhaust parts"):
            PromptRenderEnvelope(
                text="role",
                parts=(role,),
                stable_prefix_parts=(),
                turn_payload_parts=(),
                prefix_hash="x",
                payload_hash="y",
            )

    def test_prefix_and_payload_must_be_disjoint(self) -> None:
        role = _static_global("role")
        with pytest.raises(ValueError, match="overlap"):
            PromptRenderEnvelope(
                text="role",
                parts=(role,),
                stable_prefix_parts=(role,),
                turn_payload_parts=(role,),
                prefix_hash="x",
                payload_hash="y",
            )


# ---------------------------------------------------------------------------
# Hash stability under content changes
# ---------------------------------------------------------------------------


class TestHashStability:
    def test_task_text_change_only_payload_hash_changes(self) -> None:
        role = _static_global("role", body="ROLE BODY")
        task_v1 = _turn_part("task", body="task v1")
        task_v2 = _turn_part("task", body="task v2")
        env_v1 = make_render_envelope(text="x", parts=(role, task_v1))
        env_v2 = make_render_envelope(text="y", parts=(role, task_v2))
        assert env_v1.prefix_hash == env_v2.prefix_hash
        assert env_v1.payload_hash != env_v2.payload_hash

    def test_artifact_content_change_only_payload_hash_changes(self) -> None:
        role = _static_global("role", body="ROLE BODY")
        artifact_v1 = _turn_part("artifact", body="--- file:foo.py ---\nbody1")
        artifact_v2 = _turn_part("artifact", body="--- file:foo.py ---\nbody2")
        env_v1 = make_render_envelope(text="x", parts=(role, artifact_v1))
        env_v2 = make_render_envelope(text="y", parts=(role, artifact_v2))
        assert env_v1.prefix_hash == env_v2.prefix_hash
        assert env_v1.payload_hash != env_v2.payload_hash

    def test_prefix_part_body_change_changes_prefix_hash(self) -> None:
        role_v1 = _static_global("role", body="ROLE V1")
        role_v2 = _static_global("role", body="ROLE V2")
        task = _turn_part("task", body="same task")
        env_v1 = make_render_envelope(text="x", parts=(role_v1, task))
        env_v2 = make_render_envelope(text="y", parts=(role_v2, task))
        assert env_v1.prefix_hash != env_v2.prefix_hash

    def test_empty_partition_hashes_are_stable(self) -> None:
        env = make_render_envelope(text="", parts=())
        # Both sides use the same SHA-256 over the empty input; this
        # makes "no parts" trivially equal to "no parts" without a
        # special-case sentinel.
        assert env.prefix_hash == env.payload_hash
        assert env.prefix_hash != ""


# ---------------------------------------------------------------------------
# Composer classification — turn-variable scan on raw template
# ---------------------------------------------------------------------------


class TestComposerTurnVariableClassification:
    """The composer reads each editable part's raw template, scans for
    known turn substitution variables, and declares TURN metadata when
    any are present. This is what makes ``test_rendered_editable_part_
    with_task_variable_classified_as_payload`` true end-to-end."""

    def test_user_task_lands_in_payload_via_turn_input_part(self) -> None:
        # ADR 0028 / M10.5 Step 2: ``tasks/hypothesis.md`` no longer
        # references ``$task`` — the task file is pure static method
        # prose and classifies STATIC/GLOBAL (prefix-eligible). The
        # user task text now arrives as a typed ``turn_input`` part
        # (TURN/NONE) emitted by the builder. That part lives in
        # ``turn_payload_parts`` so the actual task content is still
        # excluded from the cacheable prefix.
        env = prompts.hypothesis_prompt("Fix calc.add", "/project", codemap="x.py").envelope()

        assert env is not None
        payload_names = [p.name for p in env.turn_payload_parts]
        # The user task rides in the turn_input part, not in the
        # task-method part.
        assert "hypothesis_task" in payload_names
        # And the user task string is in the payload-part bodies.
        payload_bodies = "\n".join(p.body for p in env.turn_payload_parts)
        assert "Fix calc.add" in payload_bodies

    def test_default_role_is_static_global_after_m10p5_cleanup(
        self,
    ) -> None:
        # ADR 0028 / M10.5: default role files no longer reference
        # ``$project_dir`` or ``$context``; the project anchor and
        # plugin context block live in a typed ``context:project``
        # part. The default role is therefore STATIC/GLOBAL and
        # enters the cacheable prefix.
        env = prompts.hypothesis_prompt("Fix calc.add", "/project", codemap="x.py").envelope()
        assert env is not None
        role = next(p for p in env.parts if p.kind == "role")
        assert role.stability is PromptStability.STATIC
        assert role.cache_scope is PromptCacheScope.GLOBAL
        assert role.volatile_reason is None

    def test_project_context_part_is_project_scope_static(self) -> None:
        # ADR 0028 / M10.5: the project anchor + plugin context block
        # are emitted as a typed ``context:project`` part with
        # ``stability=STATIC`` and ``cache_scope=PROJECT``. Body/hash
        # invalidation tracks the project_dir + plugin facts; the
        # part is prefix-eligible (RUN is reserved and excluded from
        # ``stable_prefix_parts`` by ``envelope.is_prefix_eligible``).
        env = prompts.hypothesis_prompt("Fix calc.add", "/project", codemap="x.py").envelope()
        assert env is not None
        ctx = next(
            (
                p for p in env.parts
                if p.kind == "context" and p.name == "project"
            ),
            None,
        )
        assert ctx is not None
        assert ctx.stability is PromptStability.STATIC
        assert ctx.cache_scope is PromptCacheScope.PROJECT
        assert (
            "You are working directly in the project directory at: /project"
            in ctx.body
        )
        assert ctx in env.stable_prefix_parts

    def test_task_method_part_is_static_after_m10p5_cleanup(
        self,
    ) -> None:
        # ADR 0028 / M10.5 Step 2: ``tasks/hypothesis.md`` no longer
        # references ``$task`` / ``$codemap_section``; the task-method
        # part is pure static prose and classifies STATIC/GLOBAL. The
        # task-volatile content lives in the typed ``turn_input``
        # part covered above.
        env = prompts.hypothesis_prompt("Fix calc.add", "/project", codemap="x.py").envelope()
        assert env is not None
        task = next(p for p in env.parts if p.kind == "task")
        assert task.stability is PromptStability.STATIC
        assert task.cache_scope is PromptCacheScope.GLOBAL
        assert task.volatile_reason is None


# ---------------------------------------------------------------------------
# Sidecar wiring — prompt_trace surfaces the envelope to consumers
# ---------------------------------------------------------------------------


class TestSidecarPublication:
    def test_builder_publishes_envelope_via_turn(self) -> None:
        turn = prompts.hypothesis_prompt("Fix calc.add", "/project", codemap="x.py")
        env = turn.envelope()
        assert env is not None
        assert env.text  # wire-identical string
        assert env.parts  # at least one part
        assert env.prefix_hash and env.payload_hash

    def test_envelope_is_deterministic_across_calls(self) -> None:
        turn = prompts.hypothesis_prompt("Fix calc.add", "/project", codemap="x.py")
        # envelope() can be called multiple times and returns equivalent envelopes.
        first = turn.envelope()
        second = turn.envelope()
        assert first is not None
        assert second is not None
        assert first.text == second.text
        assert first.prefix_hash == second.prefix_hash


# ---------------------------------------------------------------------------
# Byte-identical wire prompt — the brief's strongest contract
# ---------------------------------------------------------------------------


class TestStringReturningApiByteIdentity:
    """Public prompt builders return a :class:`PromptTurn` whose ``.text``
    is the wire-identical string. The render envelope is derived from the turn."""

    @pytest.fixture
    def plugin(self) -> PluginConfig:
        return PluginConfig()

    def test_prompt_apis_remain_byte_identical(
        self, plugin: PluginConfig,
    ) -> None:
        # Each builder is called twice; both invocations must return
        # equal PromptTurn objects (composer determinism).
        for build in (
            lambda: prompts.hypothesis_prompt(
                "Fix calc.add", "/project", codemap="x.py",
            ),
            lambda: prompts.plan_prompt("Fix calc.add", "/proj", plugin),
            lambda: prompts.build_prompt("Fix calc.add", "/proj", plugin),
            lambda: prompts.review_focus("Fix calc.add", plugin, "/proj"),
            lambda: prompts.fix_prompt(
                "Fix calc.add", "Reviewer note", "/proj", plugin,
            ),
        ):
            a = build()
            b = build()
            assert a == b
            assert a.text.strip()  # non-empty rendered prompt

    def test_envelope_text_equals_turn_text(
        self, plugin: PluginConfig,
    ) -> None:
        turn = prompts.plan_prompt("Fix calc.add", "/proj", plugin)
        env = turn.envelope()
        assert env is not None
        assert env.text == turn.text


# ---------------------------------------------------------------------------
# Brief test name — partition order
# ---------------------------------------------------------------------------


class TestPartitionOrder:
    def test_parts_order_static_profile_before_run_turn(self) -> None:
        # In the envelope's partition tuples, prefix parts (static +
        # profile-eligible) appear before payload parts (run + turn +
        # cache-none). The wire prompt order itself is composer-driven
        # and unchanged by M2; the assertion here is on the partition
        # surface that M6's selector and M12's trace consume.
        role = _static_global("role")
        contract = _profile_global("contract")
        artifact = _turn_part("artifact")
        env = make_render_envelope(
            text="role\n\ncontract\n\nartifact",
            parts=(role, contract, artifact),
        )
        assert env.stable_prefix_parts == (role, contract)
        assert env.turn_payload_parts == (artifact,)


# ---------------------------------------------------------------------------
# Helpers — exposed for downstream milestones
# ---------------------------------------------------------------------------


class TestPartitionHelpers:
    def test_stable_prefix_parts_helper_matches_envelope(self) -> None:
        role = _static_global("role")
        artifact = _turn_part("artifact")
        parts = (role, artifact)
        assert stable_prefix_parts(parts) == (role,)
        assert turn_payload_parts(parts) == (artifact,)

    def test_helpers_apply_contiguous_rule(self) -> None:
        role = _static_global("role")
        artifact = _turn_part("artifact")
        format_part = _static_global("format", kind="format")
        parts = (role, artifact, format_part)
        assert stable_prefix_parts(parts) == (role,)
        assert turn_payload_parts(parts) == (artifact, format_part)
