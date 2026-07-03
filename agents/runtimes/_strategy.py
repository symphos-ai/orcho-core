"""
agents/providers/_strategy.py — AgentProvider Strategy pattern.

Defines HOW a runtime is constructed for a given (runtime, model, effort)
triple. Orchestrators call ``provider.resolve(runtime, model, effort=...)``
and never branch on ``if mock`` — the provider IS the configuration. The
provider never branches on runtime name either: every backend (Claude,
Codex, Gemini, third-party) flows through the same resolver.

Implementations:
  RealAgentProvider       → delegates to ``AgentRegistry.default().resolve``
  MockAgentProvider       → inline stubs, instant response, zero API calls
  FailingMockProvider     → retry-test stubs that fail N times then succeed

Factory:
  make_provider(mock=False) → appropriate provider

Legacy named helpers (``.claude``, ``.codex``, ``.gemini``) are kept on
the implementations as one-line shims that forward to ``.resolve(...)``.
They exist only so existing tests using ``provider.codex("mock")`` keep
working — production orchestration code MUST go through ``.resolve``.
"""

from __future__ import annotations

import json
import os
import random
import re
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.observability.metrics import estimate_tokens

if TYPE_CHECKING:
    from agents.entities import TestResult

# ── Protocol (the Strategy interface) ────────────────────────────────────────

@runtime_checkable
class AgentProvider(Protocol):
    """Resolves any registered runtime to a concrete ``IAgentRuntime``.

    Orchestrators depend only on this interface — never on concrete
    classes. ``resolve`` is the single construction surface; every
    runtime (Claude, Codex, Gemini, third-party) flows through it.

    ``effort`` (low|medium|high|xhigh|max) is the per-call reasoning
    budget; ``None`` means "let the underlying CLI keep its own default".

    Legacy named helpers (``.claude``, ``.codex``, ``.gemini``) remain on
    the implementations as thin shims for back-compat with existing
    tests; production code MUST use ``.resolve``.
    """

    def resolve(
        self,
        runtime: str,
        model: str,
        *,
        effort: str | None = None,
    ): ...

    def claude(self, model: str, *, effort: str | None = None): ...
    def codex(self, model: str, *, effort: str | None = None): ...
    def gemini(self, model: str, *, effort: str | None = None): ...
    def run_tests(self, cwd: str, plugin) -> TestResult | None:
        """Return a TestResult to short-circuit the real runner, or None to run it."""
        ...


# ── Real provider ─────────────────────────────────────────────────────────────

class RealAgentProvider:
    """Constructs real runtime agents via the ``AgentRegistry`` entry-point
    table. Every runtime — Claude, Codex, Gemini, or a third-party id
    registered under ``orcho.agent_runtimes`` — flows through the same
    ``resolve`` path.
    """

    def __init__(self) -> None:
        # Cache the registry once per provider instance so a single run
        # doesn't re-discover entry points on every phase.
        self._registry = None

    def _get_registry(self):
        if self._registry is None:
            from agents.registry import AgentRegistry
            self._registry = AgentRegistry.default()
        return self._registry

    def resolve(
        self,
        runtime: str,
        model: str,
        *,
        effort: str | None = None,
    ):
        """Single construction surface for production code.

        Dispatches through the legacy named methods for the built-in
        runtime ids (``claude`` / ``codex`` / ``gemini``) so tests that
        monkey-patch ``provider.claude = ...`` to inject a stub still
        intercept the construction. Anything else routes straight to
        the registry — third-party runtime ids registered under
        ``orcho.agent_runtimes`` do not need a named shim.
        """
        if runtime == "claude":
            return self.claude(model, effort=effort)
        if runtime == "codex":
            return self.codex(model, effort=effort)
        if runtime == "gemini":
            return self.gemini(model, effort=effort)
        return self._get_registry().resolve(model, runtime, effort=effort)

    # Legacy named methods — kept as the construction site for the
    # built-in runtimes so existing tests can intercept by replacing
    # ``provider.claude`` / ``provider.codex`` / ``provider.gemini``.
    # Production code calls ``.resolve``, which dispatches here for the
    # built-in names.
    def claude(self, model: str, *, effort: str | None = None):
        return self._get_registry().resolve(model, "claude", effort=effort)

    def codex(self, model: str, *, effort: str | None = None):
        return self._get_registry().resolve(model, "codex", effort=effort)

    def gemini(self, model: str, *, effort: str | None = None):
        return self._get_registry().resolve(model, "gemini", effort=effort)

    def run_tests(self, cwd: str, plugin) -> None:
        """Use the real test runner."""
        return None


# ── Mock provider ─────────────────────────────────────────────────────────────
# The mock IS the provider — it configures which stub handles each role.
# No separate MockClaudeAgent / MockCodexAgent files.

class MockAgentProvider:
    """Routes roles to inline stubs — instant, zero API/subprocess calls.

    Args:
        latency: per-call simulated delay in seconds (default 0 — instant).
        test_pass_rate: fraction of test runs that return pass (0.0–1.0).
                        Default 0.6 — realistic mix of pass/fail.
        validate_plan_reject_rounds: number of validate_plan review_file
                        calls that return REJECTED JSON before flipping to
                        APPROVED JSON. Default 0 — always approve.
                        Used to manually drive the
                        plan-gate manual-approval UI without a real LLM.
                        Detection is by filename: ``plan_*.md`` is
                        treated as plan-validation; ``hypothesis_*.md``
                        and other files are always approved so the
                        upstream hypothesis loop isn't starved.
    """

    def __init__(self, latency: float = 0.0, test_pass_rate: float = 0.6,
                 validate_plan_reject_rounds: int = 0) -> None:
        self._latency = latency
        self._test_pass_rate = test_pass_rate
        self._validate_plan_reject_rounds = max(0, int(validate_plan_reject_rounds))
        # Single shared codex instance per provider so the reject counter
        # spans every validate_plan round in the same pipeline run.
        self._codex_singleton: _MockCodex | None = None
        self._test_call_n = 0  # deterministic alternation

    def resolve(
        self,
        runtime: str,
        model: str,
        *,
        effort: str | None = None,
    ):
        """Single construction surface for production code.

        Dispatches through the legacy named methods (``claude`` /
        ``codex`` / ``gemini``) for the built-in runtime ids so tests
        that swap a single stub in via
        ``provider.codex = lambda m, **kw: ...`` continue to intercept
        the construction. A third-party runtime id (anything not
        ``claude`` / ``codex`` / ``gemini``) lands on a fresh
        ``_MockClaude`` with its ``runtime`` attribute stamped to the
        requested id so pipeline chips, metrics, evidence, and
        prompt-session keys stay aligned.

        The codex singleton (validate-plan reject counter) lives on
        ``self.codex(...)``; both ``resolve("codex", ...)`` and
        ``provider.codex(...)`` reach the same instance.
        """
        if runtime == "claude":
            return self.claude(model, effort=effort)
        if runtime == "codex":
            return self.codex(model, effort=effort)
        if runtime == "gemini":
            return self.gemini(model, effort=effort)
        agent = _MockClaude(latency=self._latency)
        agent.runtime = runtime
        return agent

    # Legacy named methods — kept as the construction site for the
    # built-in mock runtimes so existing tests can intercept by
    # assigning to ``provider.claude`` / ``provider.codex`` / etc.
    # Production code calls ``.resolve``, which dispatches here for the
    # built-in names.
    def claude(self, model: str, *, effort: str | None = None) -> _MockClaude:  # noqa: ARG002 — mock is pure-Python
        agent = _MockClaude(latency=self._latency)
        agent.runtime = "claude"
        return agent

    def codex(self, model: str, *, effort: str | None = None) -> _MockCodex:  # noqa: ARG002 — mock is pure-Python
        if self._codex_singleton is None:
            self._codex_singleton = _MockCodex(
                latency=self._latency,
                validate_plan_reject_rounds=self._validate_plan_reject_rounds,
            )
        return self._codex_singleton

    def gemini(self, model: str, *, effort: str | None = None) -> _MockClaude:  # noqa: ARG002 — mock is pure-Python
        agent = _MockClaude(latency=self._latency)
        agent.runtime = "gemini"
        return agent

    def run_tests(self, cwd: str, plugin) -> TestResult:
        """Return a mock TestResult — no subprocess, no Unity/Behat.

        Alternates pass/fail based on test_pass_rate so progress.log shows
        both outcomes for integration-test realism.
        """
        from agents.entities import TestResult  # agents.entities, not pipeline.tests
        self._test_call_n += 1
        passed = (self._test_call_n % round(1 / max(self._test_pass_rate, 0.01))) == 1
        suite_name = _guess_suite(cwd)
        duration = round(0.05 + self._test_call_n * 0.03, 2)
        if passed:
            return TestResult(
                skipped=False, passed=True, duration=duration,
                output=f"[{suite_name}] ✓ All tests passed",
            )
        else:
            return TestResult(
                skipped=False, passed=False, duration=duration,
                output=(
                    f"[{suite_name}] ✗ 1 test failed\n"
                    f"  AssertionError: expected condition not met (mock)"
                ),
            )


def _guess_suite(cwd: str) -> str:
    """Guess the test suite name from the project path."""
    cwd_lower = cwd.lower()
    if "unity" in cwd_lower or "mag_unity" in cwd_lower:
        return "EditMode"
    if "api" in cwd_lower:
        return "Behat"
    if "stats" in cwd_lower:
        return "pytest"
    return "Tests"


# ── Inline stubs (private) ────────────────────────────────────────────────────

class _MockClaude:
    """Behavior-preserving mock for ``ClaudeAgent``.

    Exposes the same :class:`IAgentRuntime` surface (``invoke`` +
    ``reset_session``) plus the legacy ``run`` / ``plan`` / ``hypothesize``
    direct entry points that Phase-7-pre tests still rely on. Once
    Phase 7.8 test migration lands the legacy methods can be deleted.
    """

    runtime: str = "claude"

    def __init__(self, latency: float = 0.0) -> None:
        self._latency = latency
        self.model = "mock"
        self.session_id: str | None = None  # bridge handle parity with ClaudeAgent
        self._followup_resume_pending: bool = False
        self._last_continue_session: bool = False
        self._last_resumed_session_id: str | None = None
        self._last_followup_parent_session_id: str | None = None
        self._session_counter = 0
        self.last_prompt: str = ""
        self.last_duration_s: float = 0.0

    # ── IAgentRuntime ─────────────────────────────────────────────────────
    def _record_runtime_resume(self, *, continue_session: bool) -> None:
        if self._followup_resume_pending and self.session_id:
            continue_session = True
            self._last_followup_parent_session_id = self.session_id
            self._followup_resume_pending = False
        else:
            self._last_followup_parent_session_id = None
        self._last_continue_session = bool(continue_session and self.session_id)
        self._last_resumed_session_id = (
            self.session_id if self._last_continue_session else None
        )
        if not self.session_id:
            self._session_counter += 1
            self.session_id = f"mock-claude-{self._session_counter}"

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),  # noqa: ARG002 — mock ignores
    ) -> str:
        """Dispatch the ``IAgentRuntime`` entry point to a behavior-preserving
        synthetic content path. Prompt classification keeps materialization,
        plan-generation, and hypothesis surfaces working without callers
        needing a separate API.

        Routing:

        * ``mutates_artifacts=True`` → write path: produce build content and
          materialize claimed files on disk so the fake worktree is dirty.
        * ``mutates_artifacts=False`` + hypothesis-shaped prompt → terse
          hypothesis-style response.
        * ``mutates_artifacts=False`` (default) → return a deterministic plan
          markdown (``_mock_plan_content``). This is what almost every
          read-only invocation expects: PLAN / REPLAN read the markdown and
          parse it; the test harness's ``parse_plan`` then emits the
          ``plan.parsed`` event for the lifecycle vocabulary.
        """
        if mutates_artifacts:
            return self.run(prompt, cwd, continue_session=continue_session)
        self._record_runtime_resume(continue_session=continue_session)
        # Correction triage (ADR 0085) is a read-only reviewer pass keyed on
        # the ``[correction_triage]`` task marker. Detect it first so the
        # synthetic triage record never falls through to the plan emitter.
        if _prompt_requests_correction_triage(prompt):
            content = _correction_triage_json(prompt)
            self._record_call(
                f"correction_triage for: {prompt[:80]}",
                content,
                duration_s=_mock_duration("plan", prompt, content),
            )
            return content
        # Phase-handoff advisor (read-only) is keyed on the ``[handoff_advice]``
        # task marker. Detect it before the plan/release/review branches so the
        # synthetic advisor JSON never falls through to a plan or review parser.
        if _prompt_requests_handoff_advice(prompt):
            content = _handoff_advice_json()
            self._record_call(
                f"handoff_advice for: {prompt[:80]}",
                content,
                duration_s=_mock_duration("plan", prompt, content),
            )
            return content
        # Hypothesis prompts render ``tasks/hypothesis.md`` whose unique
        # opening line is the only stable substring we can pin: every
        # other architect surface (plan, replan, decompose, readonly_plan)
        # has a different task body. ADR-0009 system-blocks all share
        # ``authoring_language`` etc. tokens, so don't try to discriminate
        # by them.
        if "Produce a SHORT hypothesis" in prompt:
            task = _extract_prompt_field(prompt, "task:") or prompt[:120]
            return self.hypothesize(task, cwd)
        # Reviewer gates are JSON-only. When Claude is pinned as the reviewer
        # runtime under --mock (``--runtime-review claude``), the reviewer
        # surfaces (validate_plan / review_changes / validate_cross_plan /
        # contract_check / *_final_acceptance) must emit the review/release
        # JSON contract — NOT a plan object. This MUST precede the cross-plan
        # detection below: the cross_validate_plan focus prompt embeds the
        # rendered "# Cross-Project Plan" artifact, which would otherwise trip
        # ``_is_cross_plan_prompt`` and emit a plan to a review parser.
        if _prompt_requests_release_contract(prompt):
            if _mock_release_reject_enabled():
                content = _rejected_release_json(
                    "Mock release gate: change rejected (ORCHO_MOCK_RELEASE_REJECT)."
                )
            else:
                content = _approved_release_json(
                    "Mock release gate: change is ship-ready in mock mode."
                )
            self._record_call(
                f"release for: {prompt[:80]}",
                content,
                duration_s=_mock_duration("plan", prompt, content),
            )
            return content
        if _prompt_requests_review_contract(prompt):
            content = _approved_review_json(
                "Mock review: no blocking issues."
            )
            self._record_call(
                f"review for: {prompt[:80]}",
                content,
                duration_s=_mock_duration("plan", prompt, content),
            )
            return content
        # Cross-plan / cross-replan (ADR 0054): the cross architect surface
        # is read-only (``mutates_artifacts=False``) and must emit a typed
        # ``cross_plan_json`` object, NOT the mono plan markdown. Route it
        # to the cross emitter before the mono plan/replan fallthrough.
        if _is_cross_plan_prompt(prompt):
            content = _cross_plan_json(prompt)
            self._record_call(
                f"cross_plan for: {prompt[:80]}",
                content,
                duration_s=_mock_duration("plan", prompt, content),
            )
            return content
        # Replan path produces the same shape as plan but with the
        # ``revised`` marker on so downstream renderers can distinguish.
        task = (
            _extract_prompt_field(prompt, "task to plan:")
            or _extract_prompt_field(prompt, "task:")
            or prompt[:120]
        )
        revised = _is_replan_prompt(prompt)
        return self._plan_markdown(task, cwd, revised=revised)

    def _plan_markdown(self, task: str, cwd: str, *, revised: bool) -> str:
        """Generate the deterministic plan markdown the mock has always
        produced via ``plan()``. Returned as raw text so callers can run
        the real ``parse_plan`` against it."""
        plugin_name, file_hints, language = _load_plugin_hints(cwd)
        project = Path(cwd).name if cwd else "project"
        summary = _mock_plan_content(
            project, task, cwd, plugin_name, file_hints, language,
            revised=revised,
        )
        self._record_call(
            f"plan for: {task}",
            summary,
            duration_s=_mock_duration("plan", task, summary),
        )
        return summary

    # ── Legacy direct entry points (kept for tests during Phase 7 migration)
    def run(self, prompt: str, cwd: str = "", *, continue_session: bool = False) -> str:
        self._record_runtime_resume(continue_session=continue_session)
        if self._latency:
            time.sleep(self._latency)
        content = _claude_content(prompt, cwd)
        materialized = _materialize_mock_build_files(prompt, cwd, content)
        self._record_call(prompt, content, duration_s=_mock_duration("claude", prompt, content))
        _write_to_agent_log("CLAUDE (mock)", content, duration_s=self.last_duration_s)
        if materialized:
            _write_to_agent_log(
                "MOCK file materialization",
                "\n".join(f"- {p}" for p in materialized),
                duration_s=0.01,
            )
        return content

    def reset_session(self) -> None:
        self.session_id = None
        self._followup_resume_pending = False
        self._last_continue_session = False
        self._last_resumed_session_id = None
        self._last_followup_parent_session_id = None

    def probe_identity(self):
        """Deterministic fake identity so --mock runs and header tests can
        exercise the 'available' rendering branch without a real subprocess."""
        from agents.runtimes.identity import RuntimeIdentity
        runtime = getattr(self, "runtime", "claude")
        return RuntimeIdentity(
            runtime=runtime,
            source="mock",
            available=True,
            provider="mock",
            account_label="Mock-Org",
            email="mock@example.com",
        )

    # ── Legacy plan() shim (Phase 7.8 test migration deletes this) ────────
    def plan(self, task: str, cwd: str = "", codemap: str = ""):
        """Return a mock ParsedPlan with realistic file hints."""
        plugin_name, file_hints, language = _load_plugin_hints(cwd)
        project = Path(cwd).name if cwd else "project"
        summary = _mock_plan_content(
            project, task, cwd, plugin_name, file_hints, language,
            revised=False,
        )
        self._record_call(
            f"plan for: {task}\n\n{codemap}",
            summary,
            duration_s=_mock_duration("plan", task, summary),
        )
        from pipeline.plan_parser import parse_plan
        return parse_plan(summary)

    # ── Legacy hypothesize() shim (Phase 7.8 test migration deletes this) ─
    def hypothesize(self, task: str, cwd: str = "", codemap: str = "") -> str:
        """Fast mock hypothesis — always plausible, triggers 'approved' in QA."""
        _, file_hints, language = _load_plugin_hints(cwd)
        project = Path(cwd).name if cwd else "project"
        files = _mock_plan_files(project, cwd, file_hints, language)
        files_hint = "\n".join(f"  - {f}" for f in files[:3]) or "  - (project files)"
        language_note = f" ({language})" if language else ""
        content = (
            f"## Hypothesis\n\n"
            f"The task '{task[:120]}' can be addressed by:\n\n"
            f"1. Identify the relevant entry points in the project\n"
            f"2. Apply targeted changes to:\n{files_hint}\n"
            f"3. Verify interface contracts are preserved\n"
            f"4. Run test suite to confirm no regressions\n\n"
            f"This approach minimises scope and follows existing conventions{language_note}."
        )
        self._record_call(
            f"hypothesize: {task}\n\n{codemap}",
            content,
            duration_s=_mock_duration("hypothesis", task, content),
        )
        return content

    def _record_call(self, prompt: str, content: str, *, duration_s: float) -> None:
        self.last_prompt = prompt
        self.last_duration_s = duration_s
        # Keep estimated token fields available for direct tests/debugging
        # without making MetricsCollector treat them as measured API usage.
        self.last_estimated_tokens_in = estimate_tokens(prompt)
        self.last_estimated_tokens_out = estimate_tokens(content)
        self.last_estimated_tokens_total = (
            self.last_estimated_tokens_in + self.last_estimated_tokens_out
        )


class _MockCodex:
    """Satisfies CodexAgent.review_uncommitted() / review_file() interface."""

    runtime: str = "codex"

    def __init__(self, latency: float = 0.0,
                 validate_plan_reject_rounds: int = 0) -> None:
        self._latency = latency
        self.model = "mock"
        self.session_id: str | None = None  # Codex has no resumable session
        self._followup_resume_pending: bool = False
        self._last_continue_session: bool = False
        self._last_resumed_session_id: str | None = None
        self._last_followup_parent_session_id: str | None = None
        self._session_counter = 0
        self._validate_plan_reject_budget = max(0, int(validate_plan_reject_rounds))
        self._validate_plan_calls = 0   # only counts review_file on plan_*.md
        self.last_prompt: str = ""
        self.last_duration_s: float = 0.0

    # ── IAgentRuntime ─────────────────────────────────────────────────────
    def _record_runtime_resume(self, *, continue_session: bool) -> None:
        if self._followup_resume_pending and self.session_id:
            continue_session = True
            self._last_followup_parent_session_id = self.session_id
            self._followup_resume_pending = False
        else:
            self._last_followup_parent_session_id = None
        self._last_continue_session = bool(continue_session and self.session_id)
        self._last_resumed_session_id = (
            self.session_id if self._last_continue_session else None
        )
        if not self.session_id:
            self._session_counter += 1
            self.session_id = f"mock-codex-{self._session_counter}"

    def invoke(
        self,
        prompt: str,
        cwd: str,
        *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,  # noqa: ARG002 — Codex bridge always None
        attachments: tuple = (),  # noqa: ARG002 — mock ignores
    ) -> str:
        """Dispatch ``IAgentRuntime.invoke`` to a behavior-preserving legacy method.

        Routing:

        * ``mutates_artifacts=True`` → raises ``NotImplementedError`` because
          the Codex mock has no ``exec`` surface (the real CodexAgent supports
          it; the mock is reviewer-only). A profile that pins codex to a write
          phase under ``--mock`` is misconfigured.
        * Otherwise → review path. The flow is detected via the
          artifact body header in the prompt (``# Implementation Plan``
          for validate_plan, ``# Hypothesis`` for hypothesis QA),
          dispatched to ``review_file`` with a synthetic filename so
          the reject/approve counter — keyed on the legacy
          ``plan_*.md`` / ``hypothesis_*.md`` filename prefix — still
          works. Other prompts are treated as uncommitted-change review.
        """
        self._record_runtime_resume(continue_session=continue_session)
        if mutates_artifacts:
            raise NotImplementedError(
                "Mock CodexAgent does not implement write path. Pin claude for "
                "build/fix phases under --mock, or use the real provider."
            )
        # ADR 0085: correction_triage maps to the review slot, so the codex
        # mock is the one that serves it under --mock. Detect the marker
        # before the release / review branches and emit a triage record.
        if _prompt_requests_correction_triage(prompt):
            content = _correction_triage_json(prompt)
            self._record_call(prompt, content, duration_s=_mock_duration("codex", prompt, content))
            _write_to_agent_log("CODEX correction_triage (mock)", content, duration_s=self.last_duration_s)
            return content
        # Phase-handoff advisor (read-only) maps to the reviewer slot, so the
        # codex mock serves it under --mock too. Detect the marker before the
        # release / review branches and emit an advisor record.
        if _prompt_requests_handoff_advice(prompt):
            content = _handoff_advice_json()
            self._record_call(prompt, content, duration_s=_mock_duration("codex", prompt, content))
            _write_to_agent_log("CODEX handoff_advice (mock)", content, duration_s=self.last_duration_s)
            return content
        # ADR 0025: project ``final_acceptance`` invokes the reviewer
        # under the release contract; detect that and emit a release-
        # shaped APPROVED payload so ``parse_release`` accepts the
        # mock output. Every other reviewer surface (validate_plan,
        # review_changes, hypothesis QA, validate_cross_plan,
        # contract_check) stays on review_json and falls through to the
        # legacy review path.
        if _prompt_requests_release_contract(prompt):
            if _mock_release_reject_enabled():
                content = _rejected_release_json(
                    "Mock release gate: change rejected (ORCHO_MOCK_RELEASE_REJECT)."
                )
            else:
                content = _approved_release_json(
                    "Mock release gate: change is ship-ready in mock mode."
                )
            self._record_call(prompt, content, duration_s=_mock_duration("codex", prompt, content))
            _write_to_agent_log("CODEX release (mock)", content, duration_s=self.last_duration_s)
            return content
        # Flow detection. Must be delta-safe: round 2+ of a phase
        # under PER_PHASE session split sends only TURN-payload parts
        # — the static task-body anchors live in cached prefix parts
        # and DO NOT appear on the delta wire. Use markers that ride
        # in a TURN/NONE part for each surface:
        #
        # * validate_plan: PR3 emits a ``plan_tasks:validate_plan``
        #   (TURN/NONE) part whose body always starts with the
        #   ``## Tasks`` header rendered by
        #   :func:`pipeline.plan_markdown.render_validate_plan_tasks`.
        #   That heading is the delta-safe anchor.
        # * validate_hypothesis: the ``artifact:validate_hypothesis``
        #   (TURN/NONE) part body comes from the architect's
        #   hypothesis markdown which starts with ``# Hypothesis``.
        #
        # History of earlier markers that were withdrawn as
        # composition tightened:
        #
        # * The ``File: /tmp/plan_<ts>.md`` prefix was removed in PR1
        #   (artifact_path is metadata only — never wire body).
        # * The ``# Implementation Plan`` body header was removed in
        #   PR3 (validate_plan ships typed plan views, not the full
        #   plan markdown).
        # * The static task-text anchor "Review the implementation
        #   plan against the task" was tried in PR3 first but was
        #   prefix-cached so delta rounds missed it.
        #
        # Synthetic filenames keep ``review_file``'s filename-prefix
        # counter logic intact.
        if "task:review_uncommitted" in prompt or "contract:change_handoff" in prompt:
            return self.review_uncommitted(cwd, focus="")
        if "## Tasks" in prompt:
            return self.review_file(
                "plan_synthetic.md", focus="", cwd=cwd,
            )
        if "## Cross plan review" in prompt:
            # Cross-level validate_plan rides on the same reject
            # counter as the project path: synthetic ``plan_*.md``
            # filename trips ``review_file``'s prefix check at
            # ``is_validate_plan = name.startswith("plan_")``.
            return self.review_file(
                "plan_cross_synthetic.md", focus="", cwd=cwd,
            )
        if "# Hypothesis" in prompt:
            return self.review_file(
                "hypothesis_synthetic.md", focus="", cwd=cwd,
            )
        return self.review_uncommitted(cwd, focus="")

    # ── Legacy direct entry points (kept for tests during Phase 7 migration)
    def review_uncommitted(self, cwd: str, focus: str = "") -> str:
        if self._latency:
            time.sleep(self._latency)
        content = _codex_content()
        prompt = _mock_review_prompt(cwd, focus)
        self._record_call(prompt, content, duration_s=_mock_duration("codex", prompt, content))
        _write_to_agent_log("CODEX review (mock)", content, duration_s=self.last_duration_s)
        return content

    def review_file(self, file_path: str, focus: str = "", cwd: str | None = None) -> str:
        if self._latency:
            time.sleep(self._latency)
        # Detect validate_plan by filename prefix so the hypothesis-validation
        # loop (which uses tempfile prefix=hypothesis_) is unaffected.
        from pathlib import Path as _P
        is_validate_plan = _P(file_path).name.startswith("plan_")
        if is_validate_plan and self._validate_plan_calls < self._validate_plan_reject_budget:
            self._validate_plan_calls += 1
            n = self._validate_plan_calls
            content = _rejected_review_json(
                f"P2: Mock validate_plan round {n} flagged missing coverage and rollback plan.",
                findings=[
                    {
                        "id": "F1",
                        "severity": "P2",
                        "title": "Missing test coverage for edge case A",
                        "body": "The plan does not specify how edge case A will be covered.",
                        "required_fix": "Add a concrete test case for edge case A with acceptance criteria.",
                    },
                    {
                        "id": "F2",
                        "severity": "P3",
                        "title": "Module boundary unclear in section 3",
                        "body": "Section 3 does not state which module owns the new behavior.",
                        "required_fix": "Name the owning module and list the files it touches.",
                    },
                    {
                        "id": "F3",
                        "severity": "P2",
                        "title": "Verification step lacks rollback plan",
                        "body": "Verification mentions running tests but does not describe rollback if they fail.",
                        "required_fix": "Document the rollback path and how to revert the change safely.",
                    },
                ],
            )
        elif is_validate_plan:
            self._validate_plan_calls += 1
            content = _approved_review_json(
                "Plan approved by JSON contract; no residual risk identified.",
                checks=["Reviewed plan structure", "Reviewed acceptance criteria"],
            )
        else:
            content = _approved_review_json(
                "Plan is clear; no blocking issues found.",
                checks=["Reviewed hypothesis"],
            )
        prompt = _mock_file_review_prompt(file_path, focus, cwd)
        self._record_call(prompt, content, duration_s=_mock_duration("codex_file", prompt, content))
        _write_to_agent_log("CODEX review_file (mock)", content, duration_s=self.last_duration_s)
        return content

    def _record_call(self, prompt: str, content: str, *, duration_s: float) -> None:
        self.last_prompt = prompt
        self.last_duration_s = duration_s
        self.last_estimated_tokens_in = estimate_tokens(prompt)
        self.last_estimated_tokens_out = estimate_tokens(content)
        self.last_estimated_tokens_total = (
            self.last_estimated_tokens_in + self.last_estimated_tokens_out
        )

    def reset_session(self) -> None:
        self.session_id = None
        self._followup_resume_pending = False
        self._last_continue_session = False
        self._last_resumed_session_id = None
        self._last_followup_parent_session_id = None

    def probe_identity(self):
        """Deterministic fake identity so --mock runs and header tests can
        exercise the 'available' rendering branch without a real subprocess."""
        from agents.runtimes.identity import RuntimeIdentity
        return RuntimeIdentity(
            runtime=getattr(self, "runtime", "codex"),
            source="mock",
            available=True,
            provider="mock",
            account_label="Mock-Org",
            email="mock@example.com",
        )


# ── Stream log helper ────────────────────────────────────────────────────────
# Mirrors the separator format of agents/stream.py so output.log looks
# identical whether produced by real agents or mock stubs.

def _write_to_agent_log(label: str, content: str, *, duration_s: float = 0.0) -> None:
    """Append *content* to the global agent log file (if set)."""
    try:
        from agents.stream import write_agent_log_section
        write_agent_log_section(label, content, duration_s=duration_s)
    except Exception:
        pass  # logging must never break the pipeline


def _mock_duration(kind: str, prompt: str, content: str) -> float:
    """Deterministic non-zero simulated duration for mock telemetry.

    Mock mode must stay fast and hermetic, but a literal 0.0s hides bugs
    in metrics plumbing and makes smoke summaries look physically
    impossible. Use a tiny formula based on text size so larger prompts
    read as slightly more expensive without sleeping.
    """
    base = {
        "claude": 0.16,
        "plan": 0.10,
        "hypothesis": 0.08,
        "codex": 0.09,
        "codex_file": 0.07,
    }.get(kind, 0.05)
    size = estimate_tokens(prompt) + estimate_tokens(content)
    return round(base + min(size / 10_000.0, 0.25), 3)


def _mock_review_prompt(cwd: str, focus: str) -> str:
    return f"Review uncommitted changes in: {cwd}\n\nFocus:\n{focus}"


def _mock_file_review_prompt(file_path: str, focus: str, cwd: str | None) -> str:
    body = ""
    try:
        p = Path(file_path)
        if p.exists() and p.is_file() and p.stat().st_size <= 128_000:
            body = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        body = ""
    return (
        f"Review file: {file_path}\n"
        f"Project: {cwd or ''}\n\n"
        f"Focus:\n{focus}\n\n"
        f"File content:\n{body}"
    )


# ── Content generators ────────────────────────────────────────────────────────

def _load_plugin_hints(cwd: str) -> tuple[str, list[str], str]:
    """Try to load file_hints and language from project plugin. Never raises."""
    try:
        import importlib.util
        from pathlib import Path
        plugin_path = Path(cwd) / ".orcho" / "multiagent" / "plugin.py"
        if not plugin_path.exists():
            return "", [], ""
        spec = importlib.util.spec_from_file_location("_mp", plugin_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        p = getattr(mod, "PLUGIN", {})
        return p.get("name", ""), p.get("file_hints", []), p.get("language", "")
    except Exception:
        return "", [], ""


def _is_build_prompt(prompt: str) -> bool:
    p = prompt.lower()
    # ADR 0028 / M10.5 Step 2: the implement task method opens with
    # "Implement the task end-to-end." (was: "Implement this task
    # end-to-end."). "TASK:" header lives in the typed turn_input
    # part body emitted by the builder.
    return (
        (
            "implement the task end-to-end" in p
            and "task:" in p
            and "task to plan:" not in p
        )
        or (
            # subtask_dag executable block. P2 renamed the header from
            # "## Subtask `<id>`" to "## Current Executable Subtask `<id>`";
            # match the current header so a subtask whose id contains "fix"
            # (e.g. "apply-fix") is not mis-detected as a repair prompt.
            "## current executable subtask `" in p
            and "**goal:**" in p
        )
    )


def _is_plan_prompt(prompt: str) -> bool:
    p = prompt.lower()
    # Anchored on stable PLAN signals: the user-editable planner task
    # opening directive and the code-owned plan artifact boundary
    # contract. ADR 0028 / M10.5 Step 2: the "TASK TO PLAN:" header
    # rides in a typed turn_input part emitted by the builder, not in
    # the task .md prose. The static method prose now reads
    # "implementation plan for the task before any code lands".
    return (
        "task to plan:" in p
        and "implementation plan for the task before any code lands" in p
        and 'name="plan_artifact_boundary"' in p
    )


def _is_replan_prompt(prompt: str) -> bool:
    p = prompt.lower()
    return (
        "you are revising the plan for another attempt" in p
        and "apply human feedback as authoritative operator guidance" in p
    )


def _extract_prompt_field(prompt: str, label: str) -> str:
    pat = re.compile(rf"^\s*{re.escape(label)}\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
    m = pat.search(prompt)
    return m.group(1).strip() if m else ""


def _extract_modified_paths(content: str) -> list[str]:
    """Extract backticked paths from the mock ``### Modified files`` block."""
    paths: list[str] = []
    in_block = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "### Modified files":
            in_block = True
            continue
        if in_block and stripped.startswith("### "):
            break
        if not in_block or not stripped.startswith("- "):
            continue
        m = re.search(r"`([^`]+)`", stripped)
        if m:
            paths.append(m.group(1).strip())
    return paths


def _materialize_mock_build_files(prompt: str, cwd: str, content: str) -> list[str]:
    """Create deterministic mock build artefacts for claimed file changes.

    The mock build output already says it modified files. Materialising
    missing claimed paths makes smoke runs exercise the same dirty-tree
    review path as real builds. Existing text files receive a tiny
    deterministic mock note so git-backed demos produce a tracked diff;
    when no claimed path can be written, a harmless marker under
    ``.orcho/mock_changes`` makes the git tree dirty without clobbering
    project code.
    """
    if not cwd or not _is_build_prompt(prompt):
        return []

    try:
        base = Path(cwd).resolve()
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []

    written: list[str] = []
    claimed_paths = _extract_modified_paths(content)
    for rel in claimed_paths:
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            continue
        target = (base / rel_path).resolve()
        if target == base or base not in target.parents:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file():
                    continue
                if "## Plan Contract" not in prompt:
                    continue
                try:
                    original = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                note = _mock_existing_file_note(rel, prompt)
                if note in original:
                    continue
                separator = "" if not original or original.endswith("\n") else "\n"
                target.write_text(f"{original}{separator}{note}", encoding="utf-8")
            else:
                target.write_text(_mock_file_body(rel, prompt), encoding="utf-8")
            written.append(str(target))
        except Exception:
            continue

    if not written and claimed_paths:
        marker = base / ".orcho" / "mock_changes" / "last_build.md"
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(_mock_file_body("mock_changes/last_build.md", prompt), encoding="utf-8")
            written.append(str(marker))
        except Exception:
            pass

    return written


def _mock_existing_file_note(rel_path: str, prompt: str) -> str:
    task_line = ""
    for line in prompt.splitlines():
        if line.startswith("TASK:"):
            task_line = line.removeprefix("TASK:").strip()
            break
    task_note = f" for: {task_line}" if task_line else ""
    return textwrap.dedent(f"""\
        # orcho mock implementation touched {rel_path}{task_note}
    """)


def _mock_file_body(rel_path: str, prompt: str) -> str:
    task_line = ""
    for line in prompt.splitlines():
        if line.startswith("TASK:"):
            task_line = line.removeprefix("TASK:").strip()
            break
    plan_contract = "present" if "## Plan Contract" in prompt else "absent"
    return textwrap.dedent(f"""\
        # Mock implementation artifact

        Path: {rel_path}
        Task: {task_line or "(mock task)"}
        Plan Contract: {plan_contract}

        This file was created by orcho --mock so the pipeline can exercise
        dirty-tree review, progress, and metrics contracts without invoking
        external agent CLIs.
    """)


def _cross_plan_aliases(prompt: str) -> list[str]:
    """Extract the supplied aliases from a cross-plan/replan prompt.

    Prefer the explicit ``ALIASES: a, b`` line the cross runner injects —
    the ADR 0054 schema doc embeds ``[alias]`` / ``[api]/relative/path``
    examples, so a blanket ``[\\w+]`` scrape would invent phantom aliases
    and trip the alias-coverage validator. Fall back to the
    ``PROJECTS``/``PROJECTS INVOLVED`` ``[alias] path`` block scrape only
    when no ALIASES line is present (minimal-mode intents).
    """
    alias_line = re.search(r"^ALIASES:\s*(.+)$", prompt, re.MULTILINE)
    if alias_line:
        return [a.strip() for a in alias_line.group(1).split(",") if a.strip()]
    return [
        a for a in dict.fromkeys(re.findall(r"\[(\w+)\]", prompt))
        if a != "alias"
    ]


def _is_cross_plan_prompt(prompt: str) -> bool:
    """True iff this is the cross architect plan/replan surface (ADR 0054).

    The cross planning loop is the only surface the architect mock sees
    that must emit a ``cross_plan_json`` object instead of a mono plan.
    Detect it from stable content that survives both the round-1 full
    render and the round-2 delta render: the ``ALIASES:`` turn block the
    cross runner always injects (FULL mode, both rounds), and the
    cross-specific task prose used in minimal mode. Mono plan / replan
    prompts carry none of these — note the mono plan prompt mentions
    "cross-module or cross-project contracts", so a bare ``cross-project``
    substring is NOT a safe signal.

    Reviewer surfaces (the ``review_json`` / ``release_json`` JSON gates) are
    excluded defensively: the cross_validate_plan focus prompt embeds the
    rendered "# Cross-Project Plan" artifact and its task prose says "Validate
    the cross-project plan", so it would otherwise match — but it must emit a
    review verdict, never a plan object.
    """
    if _prompt_requests_review_contract(prompt) or _prompt_requests_release_contract(prompt):
        return False
    if re.search(r"^ALIASES:\s*\S", prompt, re.MULTILINE):
        return True
    p = prompt.lower()
    return (
        "spans multiple codebases" in p
        or "cross-project implementation plan" in p
        or "cross-project plan" in p
    )


def _cross_plan_json(prompt: str) -> str:
    """Emit ONE valid cross-plan JSON object — the ``cross_plan_json``
    contract the runner validates against
    ``core.contracts.cross_plan_schema`` (ADR 0054)."""
    plan_aliases = _cross_plan_aliases(prompt) or ["project"]
    root = plan_aliases[0]
    subtasks = []
    for i, alias in enumerate(plan_aliases):
        subtasks.append({
            "alias": alias,
            "goal": f"Implement the {alias} component",
            "spec": (
                f"Apply the required changes in {alias} following project "
                "conventions and keep the shared contract in sync."
            ),
            "depends_on": ([root] if i > 0 else []),
            "files": [f"[{alias}]/src/main"],
            "produces": "fields consumed by sibling projects",
            "consumes": "fields produced by sibling projects",
        })
    plan = {
        "short_summary": (
            f"Coordinated change across {len(plan_aliases)} project(s)."
        ),
        "interface_contract": (
            "Shared API/event field names and payload shapes stay "
            "consistent across projects."
            if len(plan_aliases) > 1 else ""
        ),
        "implementation_order": [
            f"Change {alias}" for alias in plan_aliases
        ],
        "subtasks": subtasks,
    }
    return json.dumps(plan)


def _claude_content(prompt: str, cwd: str = "") -> str:
    p = prompt.lower()
    project = Path(cwd).name if cwd else "project"
    plugin_name, file_hints, language = _load_plugin_hints(cwd)

    # Cross-plan (ADR 0054): plan/replan across multiple codebases emits a
    # typed cross-plan JSON object. NOTE: plain "[unity]" in a build prompt
    # must NOT trigger this path — detection requires cross task prose, an
    # ALIASES block, or the code-owned schema markers.
    if _is_cross_plan_prompt(prompt):
        return _cross_plan_json(prompt)

    if _is_plan_prompt(prompt):
        task = _extract_prompt_field(prompt, "TASK TO PLAN:") or "mock task"
        return _mock_plan_content(
            project, task, cwd, plugin_name, file_hints, language,
            revised=False,
        )

    if _is_replan_prompt(prompt):
        task = _extract_prompt_field(prompt, "TASK:") or "mock task"
        return _mock_plan_content(
            project, task, cwd, plugin_name, file_hints, language,
            revised=True,
        )

    # implement: realistic file list from plugin hints. Detect this before
    # generic "fix/critique" keywords because real implement tasks are often
    # phrased as "Fix bug ..." and must still dirty the mock worktree.
    if _is_build_prompt(prompt):
        content = _mock_build_content(
            project, cwd, plugin_name, file_hints, language
        )
        # P7: a subtask_dag build prompt carries the done-criteria attestation
        # contract. Close it honestly (all met) so the mock pipeline is
        # delivery-clean; whole_plan builds carry no contract and append
        # nothing. Detected from the executable-subtask block, not a flag.
        return content + _mock_subtask_attestation(prompt)

    # Fix: acknowledge critique with concrete steps.
    if "fix" in p or "address" in p or "critique" in p:
        return _mock_fix_content(plugin_name or project)

    return _mock_build_content(project, cwd, plugin_name, file_hints, language)


def _mock_plan_content(
    project: str,
    task: str,
    cwd: str,
    plugin_name: str,
    file_hints: list[str],
    language: str,
    *,
    revised: bool,
) -> str:
    files = _mock_plan_files(project, cwd, file_hints, language)
    primary = files[0]
    tasks = [
        {
            "id": "inspect-target",
            "goal": f"Inspect the current implementation for: {task}",
            "spec": (
                f"Read {primary} and adjacent tests/docs to confirm the bug "
                "surface before making changes."
            ),
            "files": files,
            "skill": None,
            "model": None,
            "depends_on": [],
            "done_criteria": [
                "Relevant code path identified",
                "Expected behaviour stated before edit",
            ],
        },
        {
            "id": "apply-fix",
            "goal": f"Implement the requested change for {project}",
            "spec": (
                "Apply the smallest code change that satisfies the task while "
                "preserving existing public interfaces."
            ),
            "files": files,
            "skill": None,
            "model": None,
            "depends_on": ["inspect-target"],
            "done_criteria": [
                "Bug is fixed in the target implementation",
                "No unrelated files are changed",
            ],
        },
        {
            "id": "verify",
            "goal": "Verify behaviour and regression safety",
            "spec": (
                "Run the narrowest relevant tests first, then the configured "
                "project test command when available."
            ),
            "files": files,
            "skill": None,
            "model": None,
            "depends_on": ["apply-fix"],
            "done_criteria": [
                "Targeted verification passes",
                "Failure output is captured for the fix loop if tests fail",
            ],
        },
    ]
    plan_json = {
        "short_summary": f"Mock plan for {task}",
        "planning_context": (
            f"Mock planner selected a focused inspect/apply/verify path for: {task}"
        ),
        # REA-1 typed plan contract — mock emits every field so the
        # golden scenario exercises the full propagation chain.
        "goal": f"Deliver a focused, verified change for: {task}",
        "acceptance_criteria": [
            "Implementation changes are limited to the planned files",
            "Tests or equivalent verification are run before final QA",
            "No regressions in adjacent modules",
        ],
        "owned_files": list(files),
        "commands_to_run": [
            "pytest -q",
        ],
        "risks": [
            "Existing public interfaces must remain stable",
            "Do not mask test failures with broad rewrites",
        ],
        "review_focus": [
            "Targeted change in planned files only",
            "Verification command was actually run",
        ],
        "mcp_context": [],
        "tasks": tasks,
    }
    if revised:
        plan_json["short_summary"] = f"Revised mock plan for {task}"
        plan_json["planning_context"] = (
            f"Mock planner revised the inspect/apply/verify path for: {task}"
        )
    return json.dumps(plan_json, indent=2)


def _mock_plan_files(
    project: str,
    cwd: str,
    file_hints: list[str],
    language: str,
) -> list[str]:
    if file_hints:
        files: list[str] = []
        for hint in file_hints[:3]:
            path = Path(hint)
            if path.suffix:
                files.append(hint)
            else:
                files.append(f"{hint.rstrip('/')}/Implementation.{_ext(language)}")
        return files
    discovered = _discover_project_files(cwd)
    if discovered:
        return discovered[:3]
    return [f"src/{project}/implementation.{_ext(language)}"]


def _discover_project_files(cwd: str) -> list[str]:
    if not cwd:
        return []
    root = Path(cwd)
    if not root.is_dir():
        return []
    skip_dirs = {
        ".git", ".orcho", ".venv", "venv", "__pycache__", "node_modules",
        "dist", "build", "worktree",
    }
    suffixes = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".cs", ".php", ".go", ".rs",
        ".java", ".kt", ".swift", ".rb",
    }
    files: list[str] = []
    try:
        for path in root.rglob("*"):
            if len(files) >= 20:
                break
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in skip_dirs for part in rel.parts):
                continue
            if path.suffix.lower() not in suffixes:
                continue
            files.append(str(rel))
    except Exception:
        return []
    return sorted(files, key=lambda p: (p.count("/"), p))


def _mock_build_content(
    project: str,
    cwd: str,
    plugin_name: str,
    file_hints: list[str],
    language: str,
) -> str:
    if file_hints:
        # Pick 2-3 representative files from hints
        files = []
        for hint in file_hints[:3]:
            path = Path(hint)
            if path.suffix:
                files.append(hint)
            else:
                files.append(f"{hint.rstrip('/')}/Implementation.{_ext(language)}")
    else:
        files = [f"src/{project}/implementation.{_ext(language)}"]
    files_block = "\n".join(f"- `{path}`" for path in files)

    lang_note = f" ({language})" if language else ""
    name_note = f" [{plugin_name}]" if plugin_name else ""

    return (
        f"## Build output — {project}{name_note}{lang_note}\n\n"
        "Implemented the required changes:\n\n"
        "### Modified files\n"
        f"{files_block}\n\n"
        "### Summary\n"
        "- Applied task requirements following project conventions\n"
        "- No breaking changes to public interfaces\n"
        "- Event subscriptions follow OnEnable/OnDisable pattern (where applicable)\n"
        "- All existing tests pass\n\n"
        "Ready for Codex review.\n"
    )


_SUBTASK_HEADER_RE = re.compile(
    r"##\s+Current Executable Subtask\s+`([^`]+)`", re.IGNORECASE
)


def _extract_subtask_done_criteria(prompt: str) -> list[str]:
    """Pull the executable subtask's done-criteria bullets from the prompt.

    Mirrors the ``_subtask_block`` layout in
    :func:`pipeline.prompts.subtask._subtask_block`: a
    ``**Done criteria ...:**`` header followed by ``- `` bullets, terminated
    by a blank line or the next ``**bold**`` section. Returns ``[]`` when the
    subtask declares no criteria.
    """
    lines = prompt.splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if not collecting:
            if stripped.startswith("**Done criteria") and stripped.endswith(":**"):
                collecting = True
            continue
        if stripped.startswith("- "):
            out.append(stripped[2:].strip())
            continue
        # First non-bullet after the block ends collection.
        break
    return out


def _mock_implement_incomplete_enabled() -> bool:
    """Whether the deterministic incomplete-delivery mock trigger is armed.

    Gated on ``ORCHO_MOCK_IMPLEMENT_INCOMPLETE``; only truthy values
    (``1``/``true``/``yes``/``on``) arm it. Unset or falsy leaves the mock's
    default all-met behaviour untouched. Used to exercise ADR-0073's incomplete
    implement delivery + phase-handoff pause path in --mock E2E smokes.
    """
    raw = os.environ.get("ORCHO_MOCK_IMPLEMENT_INCOMPLETE")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _mock_subtask_attestation(prompt: str) -> str:
    """Append a valid ``subtask_attestation`` for a subtask_dag build.

    P7: criteria-bearing subtask prompts carry the attestation contract, so the
    mock must close it or the runner marks every subtask incomplete. Returns an
    empty string for whole_plan builds (no executable-subtask header) and for
    subtasks that declare no done-criteria (no contract was sent).

    By default every criterion is reported ``met: true``. When the env-gated
    incomplete trigger is armed (:func:`_mock_implement_incomplete_enabled`) the
    last criterion is reported ``met: false`` so subtask_dag marks the subtask
    INCOMPLETE, deterministically driving the ADR-0073 incomplete-delivery path.
    """
    header = _SUBTASK_HEADER_RE.search(prompt)
    if header is None:
        return ""
    criteria = _extract_subtask_done_criteria(prompt)
    if not criteria:
        return ""
    subtask_id = header.group(1)
    incomplete = _mock_implement_incomplete_enabled()
    unmet_index = len(criteria) if incomplete else None
    attestation = {
        "type": "subtask_attestation",
        "subtask_id": subtask_id,
        "criteria": [
            {
                "index": i,
                "criterion": text,
                "met": i != unmet_index,
                "evidence": (
                    "mock: criterion left unmet to exercise ADR-0073 "
                    "incomplete delivery"
                    if i == unmet_index
                    else "mock implementation satisfied this criterion"
                ),
            }
            for i, text in enumerate(criteria, start=1)
        ],
        "summary": (
            f"mock subtask {subtask_id}: {len(criteria) - 1} of "
            f"{len(criteria)} criteria met, 1 left unmet (incomplete)"
            if incomplete
            else f"mock subtask {subtask_id}: all {len(criteria)} criteria met"
        ),
    }
    return "\n\n" + json.dumps(attestation, ensure_ascii=False)


def _mock_fix_content(scope: str) -> str:
    hint = f" in {scope}" if scope else ""
    return textwrap.dedent(f"""\
        Applied review feedback{hint}:

        Changes made:
        - Addressed all critique points from Codex review
        - Refactored as suggested (extracted repeated logic into helper)
        - Added inline comment explaining the edge case
        - Verified no regressions in adjacent modules

        Tests: all passing.
    """)


def _ext(language: str) -> str:
    """Guess file extension from language hint."""
    lang = language.lower()
    if "c#" in lang or "unity" in lang:
        return "cs"
    if "php" in lang:
        return "php"
    if "python" in lang:
        return "py"
    if "typescript" in lang:
        return "ts"
    return "txt"


def _codex_content() -> str:
    summary = random.choice([
        "Reviewed working tree; implementation is clean and tests cover the main paths.",
        "Reviewed change set; no blocking issues found. Minor stylistic notes only.",
        "Reviewed diff; interface contracts preserved and ready to merge.",
        "Reviewed working tree; tests cover main paths, no blockers.",
    ])
    return _approved_review_json(summary)


def _approved_review_json(short_summary: str, *, checks: list[str] | None = None) -> str:
    payload = {
        "verdict": "APPROVED",
        "short_summary": short_summary,
        "findings": [],
        "risks": [],
        "checks": checks or [],
    }
    return json.dumps(payload)


def _approved_release_json(short_summary: str) -> str:
    """Synthesise an approved release-gate JSON payload (ADR 0025).

    Mocks return this when the reviewer prompt carries the
    ``release_json`` system-tail block — i.e. the project
    ``final_acceptance`` phase invoked the reviewer under the release
    contract. Without this, a release prompt to a review-emitting
    stub would land at ``parse_release`` and raise ``ReleaseSchemaError``.
    """
    payload = {
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      short_summary,
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "sufficient",
        },
    }
    return json.dumps(payload)


def _mock_release_reject_enabled() -> bool:
    """Whether the deterministic release-reject mock trigger is armed (F2).

    Gated on ``ORCHO_MOCK_RELEASE_REJECT`` by the same convention as
    :func:`_mock_implement_incomplete_enabled`; only truthy values
    (``1``/``true``/``yes``/``on``) arm it. Unset or falsy leaves the mock's
    default APPROVED release behaviour untouched. Used to exercise the
    rejected-acceptance halt path (correction shortcut routes, final_acceptance)
    in --mock acceptance smokes.
    """
    raw = os.environ.get("ORCHO_MOCK_RELEASE_REJECT")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _rejected_release_json(short_summary: str) -> str:
    """Synthesise a REJECTED release-gate JSON payload (F2).

    Mirrors :func:`_approved_release_json` for the rejection path: the
    schema requires ``ship_ready=False`` plus at least one ``release_blockers``
    entry, so the payload carries a concrete blocker. Emitted only when the
    env-gated trigger is armed; ``parse_release`` accepts it and the closing
    acceptance gate routes the run into the rejected-acceptance outcome.
    """
    payload = {
        "verdict":            "REJECTED",
        "ship_ready":         False,
        "short_summary":      short_summary,
        "release_blockers":   [
            {
                "id": "MR1",
                "severity": "P0",
                "title": "Mock release reject directive armed",
                "body": (
                    "ORCHO_MOCK_RELEASE_REJECT is set, so the mock release "
                    "gate reports the change as not ship-ready."
                ),
                "required_fix": "Unset ORCHO_MOCK_RELEASE_REJECT to clear the mock rejection.",
                "why_blocks_release": "A deliberately armed mock blocker keeps the change from shipping.",
            }
        ],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "incomplete",
            "interfaces":    "not_applicable",
            "persistence":   "not_applicable",
            "tests":         "missing",
        },
    }
    return json.dumps(payload)


def _prompt_requests_correction_triage(prompt: str) -> bool:
    """Detect the correction_triage prompt by its task marker (ADR 0085).

    The triage handler prefixes the task with ``[correction_triage]``
    (mirroring ``[final_acceptance]``); the marker is the stable anchor
    the mock keys its triage branch on. The triage response contract is
    plain text (not a ``release_json`` / ``review_json`` system-block), so
    it never trips the JSON-contract detectors above.
    """
    return "[correction_triage]" in prompt


#: Supported triage kinds the directive-driven mock can emit (ADR 0086).
_MOCK_TRIAGE_KINDS = ("code_fix", "contract_ack", "gate_rerun", "blocked")

#: Directive the mock scans for in the triage prompt (the recorded
#: ``correction_context.md`` body, embedded verbatim by
#: ``_build_triage_prompt``) to pin a specific triage classification.
_MOCK_TRIAGE_DIRECTIVE_RE = re.compile(
    r"orcho-mock-triage-kind:\s*([A-Za-z_]+)"
)


def _mock_triage_kind_directive(prompt: str) -> str | None:
    """Parse an ``orcho-mock-triage-kind: <kind>`` directive, or ``None``.

    The directive lets --mock smokes deterministically pin a triage kind by
    writing it into ``correction_context.md`` (which the triage prompt embeds).
    Returns the lowercased kind only when it names a supported kind; an absent
    or unsupported directive returns ``None`` so the caller falls back to the
    default ``code_fix`` (keeping the directive-free mock behaviour intact).
    """
    match = _MOCK_TRIAGE_DIRECTIVE_RE.search(prompt)
    if match is None:
        return None
    kind = match.group(1).strip().lower()
    return kind if kind in _MOCK_TRIAGE_KINDS else None


def _correction_triage_json(prompt: str = "") -> str:
    """Synthesise a valid correction-triage JSON record (ADR 0085 / 0086).

    Returned by the mock when the reviewer prompt carries the
    ``[correction_triage]`` marker so the handler's tolerant parser yields a
    well-formed triage record. Without an ``orcho-mock-triage-kind`` directive
    the record defaults to ``code_fix`` (Stage 0 behaviour); with the directive
    it emits the named kind so route smokes can drive every branch — ``blocked``
    carries a non-empty ``blockers`` so the halt path has a concrete cause.
    """
    kind = _mock_triage_kind_directive(prompt) or "code_fix"
    payloads: dict[str, dict[str, object]] = {
        "code_fix": {
            "kind": "code_fix",
            "summary": "Mock correction triage: narrow code fix in the retained worktree.",
            "allowed_scope": ["the files named in the recorded release blockers"],
            "required_checks": ["re-run the project test command and confirm it passes"],
            "blockers": [],
        },
        "contract_ack": {
            "kind": "contract_ack",
            "summary": "Mock correction triage: blockers are a contract/doc acknowledgement, no code change.",
            "allowed_scope": [],
            "required_checks": ["re-run the contract check and confirm the acknowledgement"],
            "blockers": [],
        },
        "gate_rerun": {
            "kind": "gate_rerun",
            "summary": "Mock correction triage: blockers are stale; re-running the gate should clear them.",
            "allowed_scope": [],
            "required_checks": ["re-run the release gate against the retained worktree"],
            "blockers": [],
        },
        "blocked": {
            "kind": "blocked",
            "summary": "Mock correction triage: no safe remediation path for the recorded blockers.",
            "allowed_scope": [],
            "required_checks": [],
            "blockers": [
                "Mock: remediation needs inputs absent from the retained worktree."
            ],
        },
    }
    return json.dumps(payloads[kind])


def _prompt_requests_handoff_advice(prompt: str) -> bool:
    """Detect the phase-handoff advisor prompt by its task marker.

    The advisor handler prefixes the prompt with ``[handoff_advice]``
    (mirroring ``[correction_triage]``); the marker is the stable anchor the
    mock keys its advisor branch on. The advisor response contract is a plain
    JSON object (not a ``release_json`` / ``review_json`` system-block), so it
    never trips the JSON-contract detectors below.
    """
    return "[handoff_advice]" in prompt


def _handoff_advice_json() -> str:
    """Synthesise a valid phase-handoff advisor JSON record.

    Returned by the mock when the reviewer prompt carries the
    ``[handoff_advice]`` marker so the handler's tolerant parser yields a
    well-formed recommendation. Defaults to a confident ``retry_feedback`` with
    a non-empty feedback string so the advisory retry path has concrete input.
    """
    return json.dumps({
        "recommended_action": "retry_feedback",
        "confidence": "high",
        "rationale": (
            "Mock handoff advice: the recorded findings name a concrete, "
            "bounded fix the next round can make."
        ),
        "retry_feedback": (
            "Address the recorded findings directly: close the named gaps and "
            "re-run the verification the reviewer flagged."
        ),
        "risks": ["The retry must stay scoped to the recorded findings."],
        "expected_files": ["the files named in the recorded findings"],
        "operator_note": "Mock advisor recommendation.",
    })


def _prompt_requests_release_contract(prompt: str) -> bool:
    """Detect the release_json system-tail block in a reviewer prompt.

    Matches the renderer (``contracts.py:58``) output:
    ``<orcho:system-block kind="contract" name="release_json" version="...">``.
    Does NOT substring-search for the bare token ``release_json`` —
    a review prompt that incidentally mentions the word would
    false-positive and the stub would emit release JSON to a review
    parser, halting the test with a misleading error.
    """
    return (
        'kind="contract"' in prompt
        and 'name="release_json"' in prompt
        and "<orcho:system-block " in prompt
    )


def _prompt_requests_review_contract(prompt: str) -> bool:
    """Detect the review_json system-tail block in a reviewer prompt.

    Mirrors :func:`_prompt_requests_release_contract`. Matches the
    renderer output for the JSON-only reviewer gates (validate_plan,
    review_changes, validate_cross_plan, contract_check, hypothesis QA):
    ``<orcho:system-block kind="contract" name="review_json" ...>``.
    Anchored on the block attributes (not a bare token) so a prompt that
    merely embeds a rendered plan artifact mentioning "review" does not
    false-positive.
    """
    return (
        'kind="contract"' in prompt
        and 'name="review_json"' in prompt
        and "<orcho:system-block " in prompt
    )


def _rejected_review_json(
    short_summary: str,
    *,
    findings: list[dict[str, object]],
) -> str:
    payload = {
        "verdict": "REJECTED",
        "short_summary": short_summary,
        "findings": findings,
        "risks": [],
        "checks": [],
    }
    return json.dumps(payload)


# ── Factory ───────────────────────────────────────────────────────────────────

def make_provider(mock: bool = False, latency: float = 0.0,
                  mock_validate_plan_reject_rounds: int = 0) -> AgentProvider:
    """Return the provider for the current run mode.

    Args:
        mock:    True → MockAgentProvider (full flow, zero API calls)
        latency: optional stub delay per call (seconds)
        mock_validate_plan_reject_rounds: when mock=True, how many initial
                 plan validation reviews return REJECTED before flipping to
                 APPROVED. Lets you exercise the Stage 5 gate UI from
                 the dashboard without a real LLM. 0 = legacy behaviour
                 (always approve). Has no effect when mock=False.
    """
    if mock:
        return MockAgentProvider(
            latency=latency,
            validate_plan_reject_rounds=mock_validate_plan_reject_rounds,
        )
    return RealAgentProvider()


def make_mock_phase_config(
    *,
    latency: float = 0.0,
    validate_plan_reject_rounds: int = 0,
):
    """Return a fully-mocked ``PhaseAgentConfig`` — every slot is an inline stub.

    The legacy wiring used to leave ``PhaseAgentConfig`` populated with real
    CLI agents (CodexAgent, ClaudeAgent) even when ``args.mock=True`` —
    only the side-channel ``provider`` argument switched to mock. Phases
    that read directly off ``PhaseAgentConfig`` (validate_plan, review, final_acceptance)
    therefore still spawned real ``codex`` / ``claude`` subprocesses,
    breaking the «zero real CLI calls» promise of mock mode.

    This factory replaces every slot with the same private stubs the
    ``MockAgentProvider`` uses, so ``mock=True`` is **truly hermetic**
    end-to-end:
      - plan_agent / implement_agent / repair_changes_agent /
        repair_escalation_agent → separate ``_MockClaude`` instances
      - validate_plan_agent / review_changes_agent / final_acceptance_agent
        → separate ``_MockCodex`` instances

    Separate slots matter for follow-up seeding: each phase owns an
    independent parent session id and pending-resume flag. The validate
    reject counter lives only on the validate_plan mock, which is the
    only slot that consumes it.
    """
    from agents.registry import PhaseAgentConfig
    return PhaseAgentConfig(
        plan_agent=_MockClaude(latency=latency),
        validate_plan_agent=_MockCodex(
            latency=latency,
            validate_plan_reject_rounds=validate_plan_reject_rounds,
        ),
        implement_agent=_MockClaude(latency=latency),
        review_changes_agent=_MockCodex(latency=latency),
        repair_changes_agent=_MockClaude(latency=latency),
        repair_escalation_agent=_MockClaude(latency=latency),
        final_acceptance_agent=_MockCodex(latency=latency),
    )


# ── FailingMockProvider (for M1.3 retry tests) ────────────────────────────────

class FailingMockProvider:
    """Provider that fails N times with a specific error, then succeeds.

    Used exclusively in M1.3 retry tests. Never touches real API.

    Args:
        fail_times: how many times to raise before returning success.
        error_type: one of "rate_limit", "timeout", "context_overflow", "generic".
        latency: simulated delay per successful call (seconds).
    """

    def __init__(
        self,
        fail_times: int = 1,
        error_type: str = "rate_limit",
        latency: float = 0.0,
    ) -> None:
        self._fail_times = fail_times
        self._error_type = error_type
        self._latency = latency

    def resolve(
        self,
        runtime: str,
        model: str,
        *,
        effort: str | None = None,
    ):
        """Dispatch through named shims for built-in runtimes so test
        monkey-patches keep working; fall back to a generic
        ``_FailingClaude`` with stamped ``runtime`` attribute for
        third-party ids."""
        if runtime == "claude":
            return self.claude(model, effort=effort)
        if runtime == "codex":
            return self.codex(model, effort=effort)
        if runtime == "gemini":
            return self.gemini(model, effort=effort)
        return _FailingClaude(
            fail_times=self._fail_times,
            error_type=self._error_type,
            latency=self._latency,
            runtime=runtime,
        )

    # Legacy named methods — construction site for built-in failing
    # stubs so tests can swap them via ``provider.claude = ...``.
    def claude(self, model: str, *, effort: str | None = None) -> _FailingClaude:  # noqa: ARG002
        return _FailingClaude(
            fail_times=self._fail_times,
            error_type=self._error_type,
            latency=self._latency,
            runtime="claude",
        )

    def codex(self, model: str, *, effort: str | None = None) -> _FailingCodex:  # noqa: ARG002
        return _FailingCodex(
            fail_times=self._fail_times,
            error_type=self._error_type,
            latency=self._latency,
        )

    def gemini(self, model: str, *, effort: str | None = None) -> _FailingClaude:  # noqa: ARG002
        return _FailingClaude(
            fail_times=self._fail_times,
            error_type=self._error_type,
            latency=self._latency,
            runtime="gemini",
        )

    def run_tests(self, cwd: str, plugin) -> None:
        return None


def _make_typed_error(error_type: str) -> Exception:
    """Build a typed AgentCallError for the given error_type string."""
    from core.io.retry import (
        AgentCallError,
        ApiTimeoutError,
        ContextOverflowError,
        RateLimitError,
    )
    match error_type:
        case "rate_limit":
            return RateLimitError("rate_limit_exceeded: too many requests", exit_code=429)
        case "timeout":
            return ApiTimeoutError("timed out waiting for agent response", exit_code=-1)
        case "context_overflow":
            return ContextOverflowError("context_length_exceeded: prompt too long", exit_code=-1)
        case _:
            return AgentCallError(f"agent call failed (mock error: {error_type})", exit_code=1)


class _FailingClaude:
    """Mock Claude that fails fail_times before returning success."""

    runtime: str = "claude"

    def __init__(
        self,
        fail_times: int,
        error_type: str,
        latency: float,
        *,
        runtime: str = "claude",
    ) -> None:
        self._remaining = fail_times
        self._error_type = error_type
        self._latency = latency
        self.runtime = runtime
        self.model = "mock-failing"
        self.session_id: str | None = None

    def run(self, prompt: str, cwd: str = "", *, continue_session: bool = False) -> str:
        if self._remaining > 0:
            self._remaining -= 1
            raise _make_typed_error(self._error_type)
        if self._latency:
            time.sleep(self._latency)
        return _claude_content(prompt, cwd)

    def reset_session(self) -> None:
        self.session_id = None

    def plan(self, task: str, cwd: str = "", codemap: str = ""):
        if self._remaining > 0:
            self._remaining -= 1
            raise _make_typed_error(self._error_type)
        from pipeline.plan_parser import ParsedPlan
        return ParsedPlan(
            short_summary=f"Mock plan: {task}",
            planning_context=f"Mock plan: {task}",
            subtasks=(),
            source="raw",
        )

    def hypothesize(self, task: str, cwd: str = "", codemap: str = "") -> str:
        return f"Mock hypothesis for: {task}"


class _FailingCodex:
    """Mock Codex that fails fail_times before returning approved JSON."""

    runtime: str = "codex"

    def __init__(self, fail_times: int, error_type: str, latency: float) -> None:
        self._remaining = fail_times
        self._error_type = error_type
        self._latency = latency
        self.model = "mock-failing"

    def review_uncommitted(self, cwd: str, focus: str = "") -> str:
        if self._remaining > 0:
            self._remaining -= 1
            raise _make_typed_error(self._error_type)
        if self._latency:
            time.sleep(self._latency)
        return _approved_review_json("Reviewed working tree after retry; approved.")

    def review_file(self, file_path: str, focus: str = "", cwd: str | None = None) -> str:
        if self._remaining > 0:
            self._remaining -= 1
            raise _make_typed_error(self._error_type)
        return _approved_review_json("Plan is clear (after retry); no blocking issues.")
