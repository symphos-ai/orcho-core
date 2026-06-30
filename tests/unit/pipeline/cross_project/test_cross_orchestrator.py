"""Cross-project orchestrator: ADR 0022 mapping regression.

The cross-project CLI keeps the historical ``--model-build`` /
``--model-fix`` / ``--model-review`` flag names as its public surface,
but the underlying ``build_phase_config_from_overrides`` was renamed
to ADR 0022 vocabulary (``implement`` / ``repair_changes`` /
``review_changes``). The orchestrator now bridges those names
internally; this file pins the bridge so a future rename on either
side surfaces as a test failure rather than a runtime ``TypeError``.

It also pins the two attribute reads inside ``run_cross_pipeline``
that previously referenced the stale ``build_agent`` / ``review_agent``
fields on ``PhaseAgentConfig`` — those would have raised
``AttributeError`` whenever a populated ``phase_config`` reached the
function.
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from types import SimpleNamespace
from typing import Any

import pytest


# ── ADR 0046 Phase D test shim ────────────────────────────────────────
#
# Cross-project now dispatches per-alias via
# ``run_project_pipeline(ProjectRunRequest(...))`` instead of the legacy
# ``run_pipeline(**kwargs)`` wrapper. Tests below historically
# monkeypatched ``_dispatch.run_pipeline`` and asserted on the captured
# ``**kwargs``. The two helpers here keep those assertions intact:
#
#   * ``_make_fake_run_project_pipeline(handler)`` returns a callable
#     usable as the monkeypatch target. ``handler`` receives the same
#     kwargs dict the legacy fake saw + an ``output_dir`` Path (so
#     fakes that mkdir / write meta.json keep working).
#
#   * ``_run_pipeline_result(session)`` wraps a session dict into a
#     ``ProjectRunResult`` so the cross dispatch's
#     ``_project_result.session`` access path resolves.
def _request_as_kwargs(request: Any) -> dict[str, Any]:
    """Shallow ``{field.name: getattr(request, field.name)}`` dict so
    existing tests can keep doing ``kwargs["profile_name"]`` etc.
    Shallow (not ``asdict``) because ``Profile`` / ``PhaseAgentConfig``
    / ``Path`` instances must round-trip by identity."""
    return {f.name: getattr(request, f.name) for f in fields(request)}


def _run_pipeline_result(session: dict) -> Any:
    """Build a ``ProjectRunResult`` from a session dict. ``output_dir``
    and ``run_id`` are best-effort — tests that don't inspect them get
    sensible defaults."""
    from pipeline.project.types import ProjectRunResult
    return ProjectRunResult(
        session=session,
        output_dir=session.get("_test_output_dir"),
        run_id=session.get("_test_run_id", "test-run"),
    )


def _make_fake_run_project_pipeline(handler):
    """Wrap a legacy ``**kwargs → session-dict`` handler into the new
    ``(request) → ProjectRunResult`` shape the cross dispatch now calls.
    The handler may return a session dict OR a ``ProjectRunResult``;
    dicts are wrapped automatically."""
    def _fake(request):
        kwargs = _request_as_kwargs(request)
        result = handler(**kwargs)
        if isinstance(result, dict):
            return _run_pipeline_result(result)
        return result
    return _fake


def test_build_phase_config_from_overrides_accepts_adr_0022_kwargs() -> None:
    """The helper's kwarg signature must keep ``implement`` /
    ``repair_changes`` / ``review_changes`` (plus the matching
    ``provider_*`` variants). Cross-orchestrator's CLI bridge passes
    these names directly — if any kwarg gets renamed, cross-CLI breaks
    silently with a TypeError on the next invocation.
    """
    from pipeline.project.phase_config import build_phase_config_from_overrides

    sig = inspect.signature(build_phase_config_from_overrides)
    expected_kwargs = {
        "plan",
        "implement",
        "repair_changes",
        "review_changes",
        "runtime_plan",
        "runtime_implement",
        "runtime_repair_changes",
        "runtime_review_changes",
    }
    missing = expected_kwargs - set(sig.parameters)
    assert not missing, (
        f"build_phase_config_from_overrides is missing ADR 0022 kwargs: "
        f"{sorted(missing)}"
    )


def test_phase_agent_config_carries_adr_0022_slot_names() -> None:
    """``run_cross_pipeline`` reads ``phase_config.implement_agent.model``
    and ``phase_config.review_changes_agent.model``. The pre-ADR-0022
    names (``build_agent``, ``review_agent``) must stay gone so a
    regression cannot reintroduce silent AttributeError on populated
    phase_config inputs.
    """
    from agents.registry import PhaseAgentConfig

    fields = set(PhaseAgentConfig.__dataclass_fields__)
    assert "implement_agent" in fields
    assert "review_changes_agent" in fields
    # The stale ADR-2021 names must not come back.
    assert "build_agent" not in fields
    assert "review_agent" not in fields


class _Runtime:
    def __init__(self, output: str) -> None:
        self.output = output
        self.model = "fake-model"
        self.calls: list[tuple[str, str]] = []

    def invoke(self, prompt: str, cwd: str, **_kwargs) -> str:
        self.calls.append((prompt, cwd))
        return self.output


class _ScriptedRuntime:
    """Runtime that returns a different scripted output per invoke call."""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.model = "fake-model"
        self.calls: list[tuple[str, str, dict]] = []

    def invoke(self, prompt: str, cwd: str, **kwargs) -> str:
        self.calls.append((prompt, cwd, kwargs))
        if self.outputs:
            return self.outputs.pop(0)
        return ""  # fall back when script runs short


class _Provider:
    def __init__(self, plan_output: str) -> None:
        self.plan = _Runtime(plan_output)
        self.review = _Runtime("")

    def resolve(self, runtime: str, _model: str, *, effort: str | None = None):  # noqa: ARG002
        return self.review if runtime == "codex" else self.plan

    def claude(self, _model: str) -> _Runtime:
        return self.plan

    def codex(self, _model: str) -> _Runtime:
        return self.review


class _ScriptedProvider:
    """Provider whose runtimes return scripted, multi-call output streams."""

    def __init__(
        self,
        plan_outputs: list[str],
        review_outputs: list[str],
    ) -> None:
        self.plan = _ScriptedRuntime(plan_outputs)
        self.review = _ScriptedRuntime(review_outputs)

    def resolve(self, runtime: str, _model: str, *, effort: str | None = None):  # noqa: ARG002
        return self.review if runtime == "codex" else self.plan

    def claude(self, _model: str) -> _ScriptedRuntime:
        return self.plan

    def codex(self, _model: str) -> _ScriptedRuntime:
        return self.review


def _cp_json(
    *aliases: str,
    summary: str = "scripted cross plan",
    interface_contract: str | None = None,
    depends_on: dict[str, list[str]] | None = None,
    spec_by_alias: dict[str, str] | None = None,
    goal_by_alias: dict[str, str] | None = None,
    order: list[str] | None = None,
) -> str:
    """One valid cross-plan JSON object for ``aliases`` (ADR 0054 fixture).

    Produces EXACTLY one subtask per supplied alias with happy-path
    defaults; overrides cover what flow tests actually vary
    (``summary`` / ``depends_on`` / ``spec_by_alias`` / ``goal_by_alias`` /
    ``interface_contract`` / ``order``).

    Do NOT build alias-coverage violations (missing/extra/duplicate alias)
    with this helper — it always covers exactly the supplied aliases, so it
    would mask an alias-coverage regression. Those fixtures construct the
    JSON inline (see ``test_cross_plan_parser``) so the intent is explicit.
    """
    import json as _json

    al = list(aliases)
    deps = depends_on or {}
    specs = spec_by_alias or {}
    goals = goal_by_alias or {}
    ic = (
        interface_contract if interface_contract is not None
        else ("shared cross contract" if len(al) > 1 else "")
    )
    return _json.dumps({
        "short_summary": summary,
        "interface_contract": ic,
        "implementation_order": order or [f"change {a}" for a in al],
        "subtasks": [
            {
                "alias": a,
                "goal": goals.get(a, f"{a} goal"),
                "spec": specs.get(a, f"{a} spec"),
                "depends_on": deps.get(a, []),
            }
            for a in al
        ],
    })


def test_cross_planning_usage_summary_matches_done_style(
    capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infra import config
    from pipeline.cross_project.usage import _print_cross_planning_usage

    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    _print_cross_planning_usage({
        "cross_plan": {
            "tokens_in": 100,
            "tokens_out": 25,
            "total_tokens": 125,
            "duration_s": 2.0,
            "calls": 1,
            "cost_usd_equivalent": 0.0123,
        },
        "cross_validate_plan": {
            "tokens_in": 40,
            "tokens_out": 10,
            "total_tokens": 50,
            "duration_s": 1.5,
            "calls": 1,
            "cost_usd_equivalent": 0.004,
        },
    })

    out = capsys.readouterr().out
    assert "✓ Usage:   Tokens: 175 (in=140 out=35)" in out
    assert "Time: 3.5s" in out
    assert "API-equiv: $0.02" in out
    config._reset_config()


def test_cross_checks_usage_summary_filters_terminal_check_phases(
    capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infra import config
    from pipeline.cross_project.usage import _print_cross_checks_usage

    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    _print_cross_checks_usage({
        "cross_plan": {
            "tokens_in": 9_000,
            "tokens_out": 1_000,
            "total_tokens": 10_000,
            "duration_s": 10.0,
        },
        "contract_check": {
            "tokens_in": 300,
            "tokens_out": 70,
            "total_tokens": 370,
            "duration_s": 4.0,
            "calls": 1,
            "cost_usd_equivalent": 0.01,
        },
        "cross_final_acceptance": {
            "tokens_in": 200,
            "tokens_out": 30,
            "total_tokens": 230,
            "duration_s": 2.5,
            "calls": 1,
            "cost_usd_equivalent": 0.02,
        },
    })

    out = capsys.readouterr().out
    assert "✓ Cross checks usage: Tokens: 600 (in=500 out=100)" in out
    assert "Time: 6.5s" in out
    assert "API-equiv: $0.03" in out
    assert "10,000" not in out
    config._reset_config()


def test_cross_plan_prompt_receives_validated_hypothesis_context(
    tmp_path,
    monkeypatch,
) -> None:
    """Approved cross hypotheses must influence CROSS_PLAN explicitly."""
    from pipeline.cross_project import orchestrator as cross

    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    run_dir = tmp_path / "run"

    monkeypatch.setattr(
        cross.config.AppConfig,
        "load",
        classmethod(
            lambda _cls: SimpleNamespace(
                hypothesis={"enabled": True, "max_attempts": 1},
                task_language="English",
            )
        ),
    )
    monkeypatch.setattr(
        cross,
        "load_plugin",
        lambda _path: SimpleNamespace(
            name="Project",
            language="Python",
            architecture="",
            file_hints=[],
        ),
    )
    # ADR 0047 Phase F — the run body lives in
    # ``pipeline.cross_project.session_run``; patch ``run_hypothesis_loop``
    # on the coordinator's module namespace.
    from pipeline.cross_project import session_run as cross_session_run
    monkeypatch.setattr(
        cross_session_run,
        "run_hypothesis_loop",
        lambda *_args, **_kwargs: (
            "The api emits email_address while web expects email.",
            [{"attempt": 1, "approved": True}],
        ),
    )

    provider = _Provider(
        _cp_json("api", "web")
    )

    cross.run_cross_pipeline(
        task="Align email field",
        projects={"api": api, "web": web},
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        hypothesis_enabled=True,
    )

    prompt, _cwd = provider.plan.calls[0]
    assert "VALIDATED HYPOTHESIS (QA-approved planning context):" in prompt
    assert "planning input, not execution approval" in prompt
    assert "verify/falsify its riskiest assumption early" in prompt
    assert "explain why it diverges" in prompt
    assert "email_address while web expects email" in prompt


def test_cross_plan_prompt_uses_configured_plan_language(
    tmp_path,
    monkeypatch,
) -> None:
    """Cross-plan markdown must keep the workspace plan-language contract."""
    from pipeline.cross_project import orchestrator as cross

    monkeypatch.setattr(
        cross.config.AppConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(plan_language="Russian")),
    )

    turn = cross.cross_plan_prompt(
        "Add edit flow",
        {"api": tmp_path / "api", "web": tmp_path / "web"},
        tmp_path / "run",
    )
    prompt = turn.text if hasattr(turn, "text") else turn

    assert 'name="authoring_language"' in prompt
    assert "Write natural-language output in Russian." in prompt


def test_cross_replan_prompt_uses_configured_plan_language(
    tmp_path,
    monkeypatch,
) -> None:
    """Cross-replan markdown must not fall back to English after QA rejection."""
    from pipeline.cross_project import orchestrator as cross

    monkeypatch.setattr(
        cross.config.AppConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(plan_language="Russian")),
    )

    turn = cross.cross_replan_prompt(
        "Add edit flow",
        "Fix API route coverage.",
        {"api": tmp_path / "api", "web": tmp_path / "web"},
        tmp_path / "run",
    )
    prompt = turn.text if hasattr(turn, "text") else turn

    assert 'name="authoring_language"' in prompt
    assert "Write natural-language output in Russian." in prompt


def test_cross_plan_prompt_receives_rejected_feedback_when_all_rejected(
    tmp_path, monkeypatch,
) -> None:
    """When run_hypothesis_loop returns (None, attempts), CROSS_PLAN must
    receive the reviewer's findings as NEGATIVE context — not as approved
    direction. The wording must explicitly disclaim direction.
    """
    from pipeline.cross_project import orchestrator as cross

    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    run_dir = tmp_path / "run"

    monkeypatch.setattr(
        cross.config.AppConfig,
        "load",
        classmethod(
            lambda _cls: SimpleNamespace(
                hypothesis={"enabled": True, "max_attempts": 1},
                task_language="English",
            )
        ),
    )
    monkeypatch.setattr(
        cross,
        "load_plugin",
        lambda _path: SimpleNamespace(
            name="Project", language="Python", architecture="", file_hints=[],
        ),
    )
    rejected_attempts = [{
        "attempt": 1,
        "hypothesis": "HYP_REJECTED_TEXT",
        "approved": False,
        "review": {
            "verdict": "REJECTED",
            "short_summary": "SUMMARY_REJECTED",
            "findings": [{
                "id": "F1", "severity": "P1",
                "title": "TITLE_FINDING",
                "body": "BODY_FINDING",
                "required_fix": "FIX_FINDING",
            }],
            "risks": ["RISK_LISTED"],
            "checks": ["CHECK_PERFORMED"],
        },
    }]
    # ADR 0047 Phase F — patch on session_run.py (body's new home).
    from pipeline.cross_project import session_run as cross_session_run
    monkeypatch.setattr(
        cross_session_run,
        "run_hypothesis_loop",
        lambda *_args, **_kwargs: (None, rejected_attempts),
    )

    provider = _Provider(
        _cp_json("api", "web")
    )

    cross.run_cross_pipeline(
        task="Align email field",
        projects={"api": api, "web": web},
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
        hypothesis_enabled=True,
    )

    prompt, _cwd = provider.plan.calls[0]
    # Rejected-feedback block landed.
    assert "REJECTED HYPOTHESIS FEEDBACK" in prompt
    assert "not validated direction" in prompt
    assert "HYP_REJECTED_TEXT" in prompt
    assert "SUMMARY_REJECTED" in prompt
    assert "TITLE_FINDING" in prompt
    assert "FIX_FINDING" in prompt
    assert "RISK_LISTED" in prompt
    assert "CHECK_PERFORMED" in prompt
    # And NOT the approved wording — the bug we're guarding against.
    assert "VALIDATED HYPOTHESIS" not in prompt


def _strict_cross_gates() -> dict:
    """Strict opt-in for both terminal cross gates.

    Custom in-test profiles need an explicit ``cross_gates`` block to
    run contract_check + cross_final_acceptance — the catalogue rule
    is missing≡off, so a Profile() built with default ``cross_gates``
    (an empty mapping) would resolve both gates to disabled and
    silently change what the test exercises.
    """
    from pipeline.runtime import (
        ContractCheckMode,
        CrossGatePolicy,
        CrossGateRunPolicy,
        CrossGateSkipPolicy,
    )
    return {
        "contract_check": CrossGatePolicy(
            enabled=True,
            run=CrossGateRunPolicy.ALWAYS,
            on_skip=CrossGateSkipPolicy.BLOCK,
            mode=ContractCheckMode.ARTIFACT_BUNDLE.value,
        ),
        "cross_final_acceptance": CrossGatePolicy(
            enabled=True,
            run=CrossGateRunPolicy.ALWAYS,
            on_skip=CrossGateSkipPolicy.BLOCK,
        ),
    }


def _approved_review_json() -> str:
    return (
        '{"verdict": "APPROVED", "short_summary": "Plan is coherent.", '
        '"findings": [], "risks": [], "checks": []}'
    )


def _rejected_review_json(reason: str = "Missing persistence change.") -> str:
    return (
        '{"verdict": "REJECTED", "short_summary": "' + reason + '", '
        '"findings": [{"id": "F1", "severity": "P1", "title": "Persistence gap", '
        '"body": "Schema not addressed.", "required_fix": "Add migration."}], '
        '"risks": [], "checks": []}'
    )


def _cross_test_appconfig_mock(monkeypatch, cross) -> None:
    """Shared AppConfig + plugin mock for cross-orchestrator tests."""
    monkeypatch.setattr(
        cross.config.AppConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(
            hypothesis={"enabled": False},
            task_language="English",
            pipeline={},
            artifacts={},
        )),
    )
    monkeypatch.setattr(
        cross,
        "load_plugin",
        lambda _path: SimpleNamespace(
            name="Project", language="Python", architecture="", file_hints=[],
        ),
    )
    # ADR 0047 Phase F — `_plan_hypothesis_step` is resolved by the run
    # body in ``pipeline.cross_project.session_run``'s namespace. Patch
    # there so the spy actually intercepts the body's lookup. The
    # orchestrator back-compat re-export is still importable but patching
    # it would be a no-op for the body.
    from pipeline.cross_project import session_run as cross_session_run
    original = cross_session_run._plan_hypothesis_step

    def _skip_implicit_hypothesis(profile, *, override_enabled):
        if override_enabled is None:
            return None
        return original(profile, override_enabled=override_enabled)

    monkeypatch.setattr(
        cross_session_run, "_plan_hypothesis_step", _skip_implicit_hypothesis,
    )


def test_cross_validate_plan_approves_on_round_1_no_replan(
    tmp_path, monkeypatch,
) -> None:
    """Single round, approved: the architect is called once, no replan."""
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    web = tmp_path / "web"
    web.mkdir()

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[_approved_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="Align email field",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="plan",
    )
    assert len(provider.plan.calls) == 1
    assert len(provider.review.calls) == 1
    cross_plan = session["phases"]["cross_plan"]
    assert cross_plan["approved"] is True
    assert len(cross_plan["rounds"]) == 1
    assert cross_plan["rounds"][0]["approved"] is True


def test_cross_validate_plan_retries_on_reject_then_approves(
    tmp_path, monkeypatch,
) -> None:
    """Reject → replan → approve: architect called twice, review called twice.

    ADR 0113: cross_replan is FRESH (the ``plan`` role is non-edit-shaped), so
    the second architect call does NOT resume the bridge — instead the prior
    reviewer critique rides ``cross_replan_prompt`` as a compact handoff.
    """
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    web = tmp_path / "web"
    web.mkdir()

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json(), _approved_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="Align email field",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="plan",
    )
    assert len(provider.plan.calls) == 2
    assert len(provider.review.calls) == 2
    # ADR 0113: round-2 cross_replan is FRESH (no bridge resume) ...
    prompt_round2, _, kwargs_round2 = provider.plan.calls[1]
    assert kwargs_round2.get("continue_session") is False
    # ... and the compact handoff carries the prior reviewer critique.
    assert "Missing persistence change." in prompt_round2
    # The cross reviewer (validate_cross_plan) is FRESH on every round too.
    _, _, review_kwargs2 = provider.review.calls[1]
    assert review_kwargs2.get("continue_session") is False
    cross_plan = session["phases"]["cross_plan"]
    assert cross_plan["approved"] is True
    assert len(cross_plan["rounds"]) == 2
    assert cross_plan["rounds"][0]["approved"] is False
    assert cross_plan["rounds"][1]["approved"] is True


def test_invalid_plan_round1_routes_through_validate_then_replans(
    tmp_path, monkeypatch,
) -> None:
    """ADR 0054: a schema-invalid plan in round 1 must NOT raise out of the
    planning loop. It is caught in ``_produce`` and surfaced through
    ``_validate`` as a synthetic reject (WITHOUT calling the reviewer for
    that round), the architect replans, and round 2's valid JSON is
    approved. The round trace carries the pinned ADR 0054 shape, and the
    canonical ``cross_plan.json`` holds the normalized round-2 object.
    """
    import json

    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    web = tmp_path / "web"
    web.mkdir()
    run_dir = tmp_path / "run"

    provider = _ScriptedProvider(
        plan_outputs=[
            "this is not a valid cross plan json object",  # round 1: unparseable
            _cp_json("api", "web"),                        # round 2: valid
        ],
        # Only round 2 reaches the reviewer; round 1 is synthetic-rejected.
        review_outputs=[_approved_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="Align email field",
        projects={"api": api, "web": web},
        output_dir=run_dir,
        provider=provider,
        cross_mode="plan",
    )

    # The architect was called twice; the reviewer only once (round 1 never
    # reached the reviewer — the parse error short-circuited via _validate).
    assert len(provider.plan.calls) == 2
    assert len(provider.review.calls) == 1

    cp = session["phases"]["cross_plan"]
    assert cp["approved"] is True
    assert len(cp["rounds"]) == 2
    r1, r2 = cp["rounds"]

    # Round 1 — invalid: pinned shape.
    assert r1["approved"] is False
    assert r1["normalized_plan"] is None
    assert r1["rendered_markdown"] == ""
    assert r1["parse_error"]
    assert r1["raw_output"] == "this is not a valid cross plan json object"

    # Round 2 — valid: pinned shape.
    assert r2["approved"] is True
    assert r2["normalized_plan"] is not None
    assert r2["parse_error"] is None
    assert r2["rendered_markdown"]

    # cross_plan.json is the normalized validated object (round 2), not raw.
    plan_json = json.loads((run_dir / "cross_plan.json").read_text("utf-8"))
    assert plan_json == r2["normalized_plan"]
    assert {st["alias"] for st in plan_json["subtasks"]} == {"api", "web"}


def test_cross_validate_plan_all_rejected_emits_phase_handoff(
    tmp_path, monkeypatch,
) -> None:
    """All cross_plan rounds rejected → ADR 0038 handoff pause.

    The active profile (``advanced``, the default) declares
    ``human_feedback_on_reject`` on the ``cross_validate_plan``
    step. When the loop's auto budget is exhausted with the final
    round still rejected, the cross orchestrator must pause with
    ``meta.status="awaiting_phase_handoff"`` and a populated
    ``meta.phase_handoff`` payload so the operator can resolve via
    ``orcho_phase_handoff_decide`` + ``orcho_run_resume``. The
    legacy "proceed with last plan + warn" fallthrough now fires
    only on profiles that declare ``human_bypass`` explicitly.
    """
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    web = tmp_path / "web"
    web.mkdir()

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web"),
            _cp_json("api", "web"),
        ],
        review_outputs=[_rejected_review_json(), _rejected_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="Align email field",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="plan",
    )
    cp = session["phases"]["cross_plan"]
    assert cp["approved"] is False
    assert len(cp["rounds"]) == 2
    # ADR 0038: pause for operator decision instead of falling through.
    assert session["status"] == "awaiting_phase_handoff"
    handoff = session.get("phase_handoff")
    assert handoff is not None
    assert handoff["phase"] == "cross_plan"
    assert handoff["type"] == "human_feedback_on_reject"
    assert handoff["trigger"] == "rejected"
    assert handoff["approved"] is False
    assert handoff["round"] == 2
    assert handoff["loop_max_rounds"] == 2
    assert handoff["id"] == "cross_plan:cross_plan_round:2"
    assert set(handoff["available_actions"]) == {
        "continue", "retry_feedback", "halt",
    }


def test_cross_validate_plan_dry_run_round_trips_through_parse_review(
    tmp_path, monkeypatch,
) -> None:
    """Dry-run cross_validate_plan must produce its result by routing an
    APPROVED JSON envelope through ``parse_review`` — same parser as the
    real reviewer — instead of synthesising approval from booleans alone.

    Without this, a future regression that handed dry-run a bare boolean
    or a `[DRY RUN]` placeholder would bypass the JSON-only contract that
    every other gate enforces.
    """
    from pipeline.cross_project import orchestrator as cross
    from pipeline.review_parser import parse_review
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    web = tmp_path / "web"
    web.mkdir()

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[],   # never invoked in dry_run
    )
    session = cross.run_cross_pipeline(
        task="Align email field",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="plan",
        dry_run=True,
    )
    cp = session["phases"]["cross_plan"]
    assert cp["approved"] is True
    review = cp["rounds"][0]["review"]
    # Result shape comes from the parsed schema, not hand-built booleans.
    assert review["verdict"] == "APPROVED"
    assert review["findings"] == []
    assert review["risks"] == []
    assert review["checks"] == []
    # The raw_response is the same JSON the parser consumed — re-parsing
    # must produce an identical verdict. A future regression that handed
    # back `[DRY RUN]` prose would fail this assertion.
    assert "raw_response" in review
    re_parsed = parse_review(review["raw_response"])
    assert re_parsed.approved is True
    assert re_parsed.verdict == "APPROVED"
    # Reviewer runtime was never invoked.
    assert provider.review.calls == []


def test_contract_check_dry_run_round_trips_through_parse_review(
    tmp_path, monkeypatch, capsys,
) -> None:
    """Dry-run contract_check (artifact_bundle default) synthesises a
    single APPROVED JSON envelope, routes it through ``parse_review``,
    and mirrors it into per-alias entries with
    ``source="artifact_bundle"``. Asserts no bypass.
    """
    import dataclasses

    from pipeline.cross_project import orchestrator as cross
    from pipeline.profiles.loader import load_profiles_v2_with_plugins
    from pipeline.review_parser import parse_review
    from pipeline.runtime.profile import CrossGatePolicy
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    web = tmp_path / "web"
    web.mkdir()

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[],
    )

    # Decouple the test from the current state of the shipped
    # ``_config/pipeline_profiles_v2.json``: this test asserts
    # contract_check behaviour, so the gate must be enabled
    # regardless of any local config edit. Wrap the loader so the
    # contract_check + cross_final_acceptance gates are force-enabled
    # for every loaded profile.
    real_loader = load_profiles_v2_with_plugins

    def _force_gates_loader(*args, **kwargs):
        loaded = real_loader(*args, **kwargs)
        out = {}
        for name, profile in loaded.items():
            gates = dict(profile.cross_gates)
            for gate_name in ("contract_check", "cross_final_acceptance"):
                policy = gates.get(gate_name)
                if policy is None:
                    gates[gate_name] = CrossGatePolicy(enabled=True)
                elif not policy.enabled:
                    gates[gate_name] = dataclasses.replace(
                        policy, enabled=True,
                    )
            out[name] = dataclasses.replace(profile, cross_gates=gates)
        return out

    monkeypatch.setattr(
        "pipeline.profiles.loader.load_profiles_v2_with_plugins",
        _force_gates_loader,
    )

    session = cross.run_cross_pipeline(
        task="Align email field",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        dry_run=True,
    )
    stdout = capsys.readouterr().out
    contract_stdout = stdout.split("[CONTRACT_CHECK]", 1)[1].split(
        "[CROSS_FINAL_ACCEPTANCE]", 1,
    )[0]
    assert "Contract Check — system" in contract_stdout
    assert "verdict" in contract_stdout
    assert "APPROVED" in contract_stdout
    assert "**Verdict:**" not in contract_stdout

    cc = session["phases"].get("contract_check", {})
    assert set(cc.keys()) == {"api", "web"}
    for _alias, result in cc.items():
        assert result["approved"] is True
        assert result["verdict"] == "APPROVED"
        assert result["findings"] == []
        assert result["risks"] == []
        assert result["checks"] == []
        # raw_response is the JSON the parser consumed — not a placeholder.
        re_parsed = parse_review(result["raw_response"])
        assert re_parsed.approved is True
        assert re_parsed.verdict == "APPROVED"
        # Audit trail records that this entry came from the system-level
        # artifact bundle (one reviewer call mirrored per alias).
        assert result["source"] == "artifact_bundle"
    # Artifact-bundle verdict is system-level; the bundle's
    # short_summary is shared across aliases rather than naming each one.
    assert {r["short_summary"] for r in cc.values()} == {
        next(iter(cc.values()))["short_summary"]
    }


def test_contract_check_runtime_prompt_rehomes_json_contract(
    tmp_path, monkeypatch,
) -> None:
    """The cross contract-check focus already carries ``review_json``.

    The runtime wrapper must not leave that contract embedded inside the
    user-level focus section; Codex has to see one final JSON contract
    in the actual system-tail.
    """
    from pipeline.cross_project import orchestrator as cross
    from pipeline.prompts.builders import runtime_review_uncommitted_prompt

    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    focus = cross.contract_review_focus(
        "Align email field",
        {"api": api, "web": web},
    )
    turn = runtime_review_uncommitted_prompt(focus, project_dir=str(web))
    prompt = turn.text if hasattr(turn, "text") else turn

    # Dedup invariant: exactly one of each contract block in the wire.
    assert prompt.count('name="review_json"') == 1
    assert prompt.count('name="review_target"') == 1
    # ADR 0028 / M10.5 Step 5: protected contracts now lead the wire
    # (TIER GLOBAL). ``partition`` at the first ``<orcho:system-block``
    # marker leaves an empty user_part — which proves no user content
    # sits before the contracts. Assert presence in the remainder.
    _user_part, marker, system_part = prompt.partition("<orcho:system-block")
    assert marker
    assert 'name="review_json"' in system_part
    assert 'name="review_target"' in system_part


def test_run_cross_pipeline_uses_advanced_profile_default(
    tmp_path, monkeypatch,
) -> None:
    """Default profile is the workflow knob; no separate sub-profile flag.

    Asserts the session records ``profile=advanced`` and
    ``projected_profile=advanced#project`` — the new wire fields that
    replace the deleted ``per_project_profile=task`` hardcoding.
    """
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[_approved_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="plan",
    )
    assert session["profile"] == "feature"
    assert session["plan_source"] == "cross"
    assert session["projected_profile"] == "feature#project"


def test_run_cross_pipeline_rejects_task_profile_for_cross(
    tmp_path, monkeypatch,
) -> None:
    """The scoped ``task`` profile has no global planning step; the
    projection coherence rule must reject it before any agent is invoked.
    """
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    with pytest.raises(ValueError, match="cannot run in cross mode"):
        cross.run_cross_pipeline(
            task="x",
            projects={"api": api, "web": web},
            output_dir=tmp_path / "run",
            provider=provider,
            cross_mode="plan",
            profile_name="task",
        )
    assert provider.plan.calls == []
    assert provider.review.calls == []


def test_contract_check_parse_error_pauses_cross_run_via_cfa_gate(
    tmp_path, monkeypatch,
) -> None:
    """Reviewer JSON that fails to parse must not silently green-light
    the run. Pre-Phase-A this branch terminated as status=failed with
    a ``parse error``-naming failure_reason; Phase A turns the
    terminal into a pause via the CFA gate's ``source="precondition"``
    branch — CFA detects the missing/unparseable contract verdict per
    alias as a precondition blocker, which under Phase A pauses the
    run on a ``cfa:``-prefixed handoff with narrowed actions
    ``[continue, halt]`` (retry_feedback omitted — the reviewer never
    ran on the CFA agent path here).
    """
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    # Skip per-project pipelines: project_steps will project; but to keep
    # the test focused, force a profile with only global planning + skip
    # everything else by injecting a custom profile via loader monkeypatch.
    from pipeline.runtime import (
        CrossScope,
        CrossStepPolicy,
        LoopStep,
        PhaseStep,
        Profile,
        ProfileKind,
    )
    plan_only = Profile(
        name="plan_only",
        kind=ProfileKind.CUSTOM,
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(
                        phase="plan",
                        cross=CrossStepPolicy(scope=CrossScope.GLOBAL, handler="cross_plan"),
                    ),
                    PhaseStep(
                        phase="validate_plan",
                        cross=CrossStepPolicy(scope=CrossScope.GLOBAL, handler="cross_validate_plan"),
                    ),
                ),
                until="validate_plan.approved",
                max_rounds=1,
            ),
        ),
        cross_gates=_strict_cross_gates(),
    )
    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader,
        "load_profiles_v2_with_plugins",
        lambda _path: {"plan_only": plan_only},
    )

    # Plan approved on round 1; contract_check returns un-parseable garbage.
    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[
            _approved_review_json(),
            "this is not json at all",
            "still not json",
        ],
    )
    session = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="plan_only",
    )
    # Phase A — contract_check parse error → CFA precondition blocker
    # → pause point with narrowed action set (no reviewer ran on the
    # CFA agent path, so retry_feedback is omitted).
    assert session["status"] == "awaiting_phase_handoff"
    handoff = session.get("phase_handoff") or {}
    assert handoff.get("id", "").startswith("cfa:")
    assert handoff.get("phase") == "cross_final_acceptance"
    assert handoff.get("available_actions") == ["continue", "halt"]
    artifacts = handoff.get("artifacts") or {}
    assert artifacts.get("source") == "precondition"


def test_dispatcher_routes_by_handler_not_phase_name(
    tmp_path, monkeypatch,
) -> None:
    """Positive proof: a step whose ``phase`` is unrelated to ``plan``
    but whose ``cross.handler='cross_plan'`` still dispatches through
    the cross_plan body. Confirms handler is the contract, not the
    phase name.
    """
    from pipeline.cross_project import orchestrator as cross
    from pipeline.runtime import (
        CrossScope,
        CrossStepPolicy,
        PhaseStep,
        Profile,
        ProfileKind,
    )
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    # ``phase='my_planner'`` is intentionally NOT a recognised semantic
    # phase name. The cross runner finds this step solely because its
    # ``cross.handler == 'cross_plan'`` matches the registry key.
    odd_profile = Profile(
        name="custom",
        kind=ProfileKind.CUSTOM,
        steps=(
            PhaseStep(
                phase="my_planner",
                cross=CrossStepPolicy(
                    scope=CrossScope.GLOBAL, handler="cross_plan",
                ),
            ),
        ),
    )
    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader, "load_profiles_v2_with_plugins",
        lambda _path: {"custom": odd_profile},
    )

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[],
    )
    cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="plan",
        profile_name="custom",
    )
    # The cross_plan body executed (architect was invoked) — proving the
    # dispatcher consulted ``cross.handler``, not ``phase``.
    assert len(provider.plan.calls) == 1


def test_cross_dispatcher_uses_handler_not_phase_name(
    tmp_path, monkeypatch,
) -> None:
    """The cross dispatcher must route by ``step.cross.handler``. Inject a
    profile whose plan step still has ``phase='plan'`` but declares a
    different handler — the registry should reject it at projection time
    so the dispatch contract holds: ``handler`` is canonical, not phase
    name.
    """
    from pipeline.cross_project import orchestrator as cross
    from pipeline.cross_project.profile_projection import CrossProjectionError
    from pipeline.profiles.loader import parse_profile
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()

    bogus = parse_profile("bogus", {
        "kind": "custom",
        "steps": [
            {"phase": "plan", "cross": {
                "scope": "global", "handler": "my_alt_plan",
            }},
            {"phase": "implement", "cross": {"scope": "project"}},
        ],
    })
    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader, "load_profiles_v2_with_plugins",
        lambda _path: {"bogus": bogus},
    )

    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    with pytest.raises(
        (ValueError, CrossProjectionError),
        match="unknown cross.handler",
    ):
        cross.run_cross_pipeline(
            task="x",
            projects={"api": api},
            output_dir=tmp_path / "run",
            provider=provider,
            cross_mode="plan",
            profile_name="bogus",
        )
    # Architect never invoked: rejection at projection time precedes dispatch.
    assert provider.plan.calls == []


def test_cross_review_child_banner_omits_handoff_wording(
    tmp_path, monkeypatch, capsys,
) -> None:
    """When the projected profile has no global plan, the child banner
    must NOT promise a handoff — there is none. Asserts the wording
    reflects the actual state.
    """
    from pipeline.cross_project import orchestrator as cross, project_dispatch as _dispatch
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_fake_run_project_pipeline(lambda **kw: {"status": "done"}),
    )

    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[
        _approved_review_json(),
    ])
    cross.run_cross_pipeline(
        task="x",
        projects={"api": api},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="delivery_audit",
    )
    captured = capsys.readouterr().out
    assert "▶ SUB-PIPELINE [api]" in captured
    assert "satisfied by approved cross handoff" not in captured, (
        "review-only projection has no handoff — wording must not promise one"
    )
    assert "review-only projection" in captured


def test_cross_review_skips_handoff_artifact_for_child(
    tmp_path, monkeypatch,
) -> None:
    """``cross --profile review`` must not write
    ``implementation_handoff.{md,json}`` since the projection has no
    global plan.
    """
    from pipeline.cross_project import orchestrator as cross, project_dispatch as _dispatch
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    captured: list[dict] = []
    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_fake_run_project_pipeline(
            lambda **kw: captured.append(kw) or {"status": "done"},
        ),
    )

    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    cross.run_cross_pipeline(
        task="x",
        projects={"api": api},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="delivery_audit",
    )
    assert captured, "expected run_pipeline invocation for [api]"
    assert captured[0]["handoff_path"] is None, (
        "review projection has no handoff — handoff_path must be None"
    )
    assert captured[0]["plan_source"] == "cross"
    alias_dir = tmp_path / "run" / "api"
    assert not (alias_dir / "implementation_handoff.md").exists()
    assert not (alias_dir / "implementation_handoff.json").exists()


def test_cross_review_skips_global_planning_entirely(
    tmp_path, monkeypatch,
) -> None:
    """``cross --profile review`` must skip cross-level planning: review
    has empty global_steps. Cross_plan.md is not produced; reviewer is
    not invoked at the cross level (only per-project review_changes /
    final_acceptance run inside children).
    """
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    provider = _ScriptedProvider(plan_outputs=[], review_outputs=[])
    session = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="plan",
        profile_name="delivery_audit",
    )
    # Neither architect nor reviewer invoked at the cross level.
    assert provider.plan.calls == []
    assert provider.review.calls == []
    # ``cross_plan`` phase section not populated — projection had no plan.
    assert session["phases"].get("cross_plan") is None


def test_cross_child_metadata_keeps_requested_profile_name(
    tmp_path, monkeypatch,
) -> None:
    """The child's run_pipeline call must receive the REQUESTED profile
    name (``advanced``) — not the synthetic projected name
    (``advanced#project``). The ``profile_obj`` carries the projected
    shape; ``profile_name`` stays canonical. ``plan_source="cross"``
    and ``handoff_path`` are passed explicitly.
    """
    from pipeline.cross_project import orchestrator as cross, project_dispatch as _dispatch
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()

    # Intercept run_pipeline to capture kwargs without actually running it.
    captured: list[dict] = []

    def _fake_run_pipeline(**kwargs):
        captured.append(kwargs)
        return {"status": "done"}

    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_fake_run_project_pipeline(_fake_run_pipeline),
    )

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api")
        ],
        review_outputs=[_approved_review_json(), _approved_review_json()],
    )
    cross.run_cross_pipeline(
        task="x",
        projects={"api": api},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="feature",
    )
    assert captured, "expected run_pipeline to be invoked for [api]"
    kwargs = captured[0]
    assert kwargs["profile_name"] == "feature", (
        f"child must receive requested profile name; got {kwargs['profile_name']!r}"
    )
    assert kwargs["plan_source"] == "cross"
    assert kwargs["handoff_path"].endswith("/api/implementation_handoff.json")
    # The synthetic projected profile rides on ``profile_obj``; its name
    # carries the ``#project`` suffix but is NOT surfaced as profile_name.
    assert kwargs["profile_obj"].name == "feature#project"


def test_cross_child_phase_handoff_pauses_parent_run(
    tmp_path, monkeypatch,
) -> None:
    from pipeline.cross_project import orchestrator as cross, project_dispatch as _dispatch
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()

    def _fake_run_pipeline(**_kwargs):
        return {
            "status": "awaiting_phase_handoff",
            "phase_handoff": {
                "id": "review_changes:repair_round:1",
                "phase": "review_changes",
                "type": "human_feedback_on_reject",
                "trigger": "rejected",
                "verdict": "REJECTED",
                "approved": False,
                "round_extras_key": "repair_round",
                "round": 1,
                "loop_max_rounds": 1,
                "available_actions": ["continue", "retry_feedback", "halt"],
                "artifacts": {"short_summary": "Needs fix"},
                "last_output": "fix critique",
            },
            "phases": {"rounds": [{"round": 1, "repair_output": "done"}]},
        }

    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_fake_run_project_pipeline(_fake_run_pipeline),
    )
    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api")
        ],
        review_outputs=[_approved_review_json()],
    )
    session = cross.run_cross_pipeline(
        task="x",
        projects={"api": api},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="small_task",
    )

    assert session["status"] == "awaiting_phase_handoff"
    handoff = session["phase_handoff"]
    assert handoff["id"] == "project:api:review_changes:repair_round:1"
    assert handoff["phase"] == "review_changes"
    assert handoff["artifacts"]["project_alias"] == "api"
    assert handoff["artifacts"]["child_handoff_id"] == (
        "review_changes:repair_round:1"
    )


def test_cross_project_handoff_resume_writes_child_decision(
    tmp_path, monkeypatch,
) -> None:
    import json

    from pipeline.cross_project import orchestrator as cross, project_dispatch as _dispatch
    from sdk.phase_handoff import phase_handoff_decide, safe_handoff_id
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    api.mkdir()
    run_dir = tmp_path / "run"
    child_handoff = {
        "id": "review_changes:repair_round:1",
        "phase": "review_changes",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "verdict": "REJECTED",
        "approved": False,
        "round_extras_key": "repair_round",
        "round": 1,
        "loop_max_rounds": 1,
        "available_actions": ["continue", "retry_feedback", "halt"],
        "artifacts": {"short_summary": "Needs fix"},
        "last_output": "fix critique",
    }
    calls: list[dict] = []

    def _fake_run_pipeline(**kwargs):
        calls.append(kwargs)
        out = kwargs["output_dir"]
        if len(calls) == 1:
            out.mkdir(parents=True, exist_ok=True)
            (out / "meta.json").write_text(
                json.dumps({
                    "status": "awaiting_phase_handoff",
                    "phase_handoff": child_handoff,
                    "phases": {"rounds": [{"round": 1}]},
                }),
                encoding="utf-8",
            )
            return {
                "status": "awaiting_phase_handoff",
                "phase_handoff": child_handoff,
                "phases": {"rounds": [{"round": 1}]},
            }
        return {"status": "done", "phases": {"rounds": [{"round": 1}]}}

    monkeypatch.setattr(
        _dispatch, "run_project_pipeline",
        _make_fake_run_project_pipeline(_fake_run_pipeline),
    )
    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api")
        ],
        review_outputs=[_approved_review_json()],
    )
    paused = cross.run_cross_pipeline(
        task="x",
        projects={"api": api},
        output_dir=run_dir,
        provider=provider,
        cross_mode="full",
        profile_name="small_task",
    )
    parent_handoff_id = paused["phase_handoff"]["id"]
    phase_handoff_decide(
        run_dir.name,
        parent_handoff_id,
        "continue",
        runs_dir=run_dir.parent,
        cwd=None,
    )

    resumed = cross.run_cross_pipeline(
        task="x",
        projects={"api": api},
        output_dir=run_dir,
        provider=provider,
        cross_mode="full",
        profile_name="small_task",
        resume_from=run_dir.name,
    )

    assert resumed["phases"]["projects"]["api"]["status"] == "done"
    assert calls[-1]["resume_from"] == "api"
    decision_file = (
        run_dir / "api" / "phase_handoff_decisions"
        / f"{safe_handoff_id(child_handoff['id'])}.json"
    )
    assert decision_file.is_file()


def test_contract_check_rejected_pauses_cross_run_via_cfa_gate(
    tmp_path, monkeypatch,
) -> None:
    """Reviewer JSON parsed cleanly but verdict=REJECTED must surface as
    a CFA precondition blocker → CFA REJECTED → Phase A pause point.
    Pre-Phase-A this branch terminated as ``status=failed``; Phase A
    turns the terminal into a pause with narrowed actions (no reviewer
    ran on the CFA agent path here, so retry_feedback is omitted)."""
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    from pipeline.runtime import (
        CrossScope,
        CrossStepPolicy,
        LoopStep,
        PhaseStep,
        Profile,
        ProfileKind,
    )
    plan_only = Profile(
        name="plan_only",
        kind=ProfileKind.CUSTOM,
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(
                        phase="plan",
                        cross=CrossStepPolicy(scope=CrossScope.GLOBAL, handler="cross_plan"),
                    ),
                    PhaseStep(
                        phase="validate_plan",
                        cross=CrossStepPolicy(scope=CrossScope.GLOBAL, handler="cross_validate_plan"),
                    ),
                ),
                until="validate_plan.approved",
                max_rounds=1,
            ),
        ),
        cross_gates=_strict_cross_gates(),
    )
    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader,
        "load_profiles_v2_with_plugins",
        lambda _path: {"plan_only": plan_only},
    )

    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[
            _approved_review_json(),
            _rejected_review_json("Schema drift between api and web."),
            _approved_review_json(),
        ],
    )
    session = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="plan_only",
    )
    # Phase A — contract_check REJECTED → CFA precondition blocker →
    # pause with narrowed action set.
    assert session["status"] == "awaiting_phase_handoff"
    handoff = session.get("phase_handoff") or {}
    assert handoff.get("id", "").startswith("cfa:")
    assert handoff.get("available_actions") == ["continue", "halt"]
    artifacts = handoff.get("artifacts") or {}
    assert artifacts.get("source") == "precondition"


# ─────────────────────────────────────────────────────────────────────────────
# ADR 0025 Phase 3 — cross_final_acceptance ordering invariants
# ─────────────────────────────────────────────────────────────────────────────


def _stub_cfa_result(
    *,
    verdict: str,
    ship_ready: bool,
    source: str,
    short_summary: str,
    rendered: str = "",
):
    """Build a CrossFinalAcceptanceResult for monkeypatching the gate.

    Used by ordering-invariant tests to bypass the real prompt/parse path
    while still exercising the orchestrator's status / event-emission
    handling around the gate.
    """
    from pipeline.cross_project.final_acceptance import CrossFinalAcceptanceResult
    from pipeline.release_parser import (
        ContractStatus,
        ParsedRelease,
        ReleaseBlocker,
    )
    blockers: tuple[ReleaseBlocker, ...] = ()
    if verdict == "REJECTED":
        blockers = (
            ReleaseBlocker(
                id="CFA_STUB_REJECT",
                severity="P1",
                title="Stub rejection",
                body=short_summary,
                required_fix="Make the gate happy.",
                why_blocks_release="Test wires a forced reject through the gate.",
            ),
        )
    parsed = ParsedRelease(
        verdict=verdict,
        ship_ready=ship_ready,
        short_summary=short_summary,
        release_blockers=blockers,
        verification_gaps=(),
        contract_status=ContractStatus(
            task_contract="OK" if verdict == "APPROVED" else "MISMATCH",
            interfaces="OK",
            persistence="OK",
            tests="OK",
        ),
        source="json",
    )
    return CrossFinalAcceptanceResult(
        parsed=parsed,
        source=source,
        raw_output="{}",
        rendered=rendered or short_summary,
        duration_s=0.0,
    )


def _plan_only_profile():
    from pipeline.runtime import (
        CrossScope,
        CrossStepPolicy,
        LoopStep,
        PhaseStep,
        Profile,
        ProfileKind,
    )
    return Profile(
        name="plan_only",
        kind=ProfileKind.CUSTOM,
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(
                        phase="plan",
                        cross=CrossStepPolicy(
                            scope=CrossScope.GLOBAL, handler="cross_plan",
                        ),
                    ),
                    PhaseStep(
                        phase="validate_plan",
                        cross=CrossStepPolicy(
                            scope=CrossScope.GLOBAL,
                            handler="cross_validate_plan",
                        ),
                    ),
                ),
                until="validate_plan.approved",
                max_rounds=1,
            ),
        ),
        cross_gates=_strict_cross_gates(),
    )


def test_cross_final_acceptance_rejection_pauses_run_even_when_contract_check_passes(
    tmp_path, monkeypatch,
) -> None:
    """Ordering invariant: contract_check approving must NOT short-circuit
    the system release gate. If ``cross_final_acceptance`` returns
    REJECTED (via stub) the cross run PAUSES on a ``cfa:``-prefixed
    phase handoff so the operator can ``continue`` (override) / halt /
    retry_feedback. Pre-Phase-A this branch terminated as
    ``status="failed"``; Phase A turns the terminal into a pause point.

    This is the central ADR 0025 Phase 3 contract: contract_check no
    longer writes terminal status — the gate is the single decision
    point. Regressing to "approve = done" must surface here, not in an
    acceptance test.
    """
    from pipeline.cross_project import final_acceptance as cfa_mod, orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader,
        "load_profiles_v2_with_plugins",
        lambda _path: {"plan_only": _plan_only_profile()},
    )
    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[
            _approved_review_json(),  # validate_plan
            _approved_review_json(),  # contract_check api
            _approved_review_json(),  # contract_check web
        ],
    )

    monkeypatch.setattr(
        cfa_mod,
        "run_cross_final_acceptance",
        lambda _ctx, **_kw: _stub_cfa_result(
            verdict="REJECTED",
            ship_ready=False,
            source="agent",
            short_summary="Integration risk not addressed by either project.",
        ),
    )

    session = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="plan_only",
    )

    # Phase A — CFA REJECTED is a pause point, not a terminal failure.
    # The session enters awaiting_phase_handoff with a ``cfa:``-prefixed
    # handoff id; the operator's decision is what then routes the run
    # to done (continue override) or halted (halt).
    assert session["status"] == "awaiting_phase_handoff", (
        f"Phase A invariant: CFA REJECTED must pause (not status=failed); "
        f"got status={session['status']!r}"
    )
    handoff = session.get("phase_handoff") or {}
    assert isinstance(handoff, dict)
    assert handoff.get("id", "").startswith("cfa:"), (
        f"pause payload id must use the cfa: prefix; got {handoff.get('id')!r}"
    )
    assert handoff.get("phase") == "cross_final_acceptance"
    assert handoff.get("verdict") == "REJECTED"
    # retry_feedback is narrowed out of the cfa: payload until A2c
    # builds the feedback-aware reviewer — advertising it would
    # dead-end the operator in NotImplementedError. continue / halt only.
    assert handoff.get("available_actions") == ["continue", "halt"]
    assert "retry_feedback" not in handoff.get("available_actions", [])


def test_cfa_continue_override_resume_preserves_findings_and_finalizes_done(
    tmp_path, monkeypatch,
) -> None:
    """Codex P1 regression — the full CFA continue-override resume cycle.

    1. Fresh run → CFA REJECTED (agent, with a release blocker) → pause.
    2. Operator records ``continue`` (override).
    3. Resume → run finalizes ``done`` (override flows through the
       APPROVED branch of _decide_status via the synthesised
       ``operator_override`` source).
    4. The persisted ``cross_final_acceptance`` phase entry STILL
       carries the original ``release_blockers`` (audit preserved —
       NOT overwritten by the empty-blocker synthetic result) and the
       honest ``verdict="REJECTED"`` plus an ``override`` marker.
    """
    from pipeline.cross_project import final_acceptance as cfa_mod, orchestrator as cross
    from sdk.phase_handoff import phase_handoff_decide
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    run_dir = tmp_path / "run"

    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader,
        "load_profiles_v2_with_plugins",
        lambda _path: {"plan_only": _plan_only_profile()},
    )
    monkeypatch.setattr(
        cfa_mod,
        "run_cross_final_acceptance",
        lambda _ctx, **_kw: _stub_cfa_result(
            verdict="REJECTED",
            ship_ready=False,
            source="agent",
            short_summary="Integration risk not addressed.",
        ),
    )

    def _provider():
        return _ScriptedProvider(
            plan_outputs=[
                _cp_json("api", "web")
            ],
            review_outputs=[
                _approved_review_json(),
                _approved_review_json(),
                _approved_review_json(),
            ],
        )

    paused = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=run_dir,
        provider=_provider(),
        cross_mode="full",
        profile_name="plan_only",
    )
    assert paused["status"] == "awaiting_phase_handoff"
    handoff_id = paused["phase_handoff"]["id"]
    assert handoff_id.startswith("cfa:")
    # Pause persisted the original REJECTED entry with the blocker.
    paused_entry = paused["phases"]["cross_final_acceptance"]
    assert paused_entry["verdict"] == "REJECTED"
    assert any(
        b["id"] == "CFA_STUB_REJECT"
        for b in paused_entry.get("release_blockers", [])
    )

    # Operator overrides.
    phase_handoff_decide(
        run_dir.name,
        handoff_id,
        "continue",
        note="ship past the integration risk for this release",
        runs_dir=run_dir.parent,
        cwd=None,
    )

    resumed = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=run_dir,
        provider=_provider(),
        cross_mode="full",
        profile_name="plan_only",
        resume_from=run_dir.name,
    )

    # Override flows through to a terminal done.
    assert resumed["status"] == "done", (
        f"continue-override must finalize done; got {resumed['status']!r}"
    )
    entry = resumed["phases"]["cross_final_acceptance"]
    # P1 #1 — original findings preserved (NOT wiped by synthetic).
    assert any(
        b["id"] == "CFA_STUB_REJECT"
        for b in entry.get("release_blockers", [])
    ), "original release_blockers must survive the override (audit)"
    # Honest reviewer verdict preserved; override recorded separately.
    assert entry["verdict"] == "REJECTED"
    assert entry["override"]["action"] == "continue"
    assert entry["override"]["preserved_verdict"] == "REJECTED"


def test_cfa_continue_resume_does_not_rerun_contract_check(
    tmp_path, monkeypatch, capsys,
) -> None:
    """Bugfix — the CFA continue-resume must NOT re-run contract_check
    (the live "ran the pipeline in a circle" symptom). On resume the
    cached contract_check result is reused: a grey
    'CONTRACT_CHECK — skipped (resume, cached)' line appears and NO
    second fresh 'Codex reviews cross-project consistency' banner is
    emitted. The cached entry + reconstructed failure flag survive, and
    the override still finalizes done coherently."""
    from pipeline.cross_project import final_acceptance as cfa_mod, orchestrator as cross
    from sdk.phase_handoff import phase_handoff_decide
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    run_dir = tmp_path / "run"

    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader,
        "load_profiles_v2_with_plugins",
        lambda _path: {"plan_only": _plan_only_profile()},
    )
    monkeypatch.setattr(
        cfa_mod,
        "run_cross_final_acceptance",
        lambda _ctx, **_kw: _stub_cfa_result(
            verdict="REJECTED", ship_ready=False, source="agent",
            short_summary="Integration risk.",
        ),
    )

    def _provider():
        return _ScriptedProvider(
            plan_outputs=[
                _cp_json("api", "web")
            ],
            review_outputs=[
                _approved_review_json(), _approved_review_json(),
                _approved_review_json(),
            ],
        )

    paused = cross.run_cross_pipeline(
        task="x", projects={"api": api, "web": web}, output_dir=run_dir,
        provider=_provider(), cross_mode="full", profile_name="plan_only",
    )
    handoff_id = paused["phase_handoff"]["id"]
    capsys.readouterr()  # clear the fresh-run output

    phase_handoff_decide(
        run_dir.name, handoff_id, "continue",
        runs_dir=run_dir.parent, cwd=None,
    )
    resumed = cross.run_cross_pipeline(
        task="x", projects={"api": api, "web": web}, output_dir=run_dir,
        provider=_provider(), cross_mode="full", profile_name="plan_only",
        resume_from=run_dir.name,
    )
    resume_out = capsys.readouterr().out

    assert resumed["status"] == "done"
    # Grey skipped line, NOT a fresh contract_check phase banner.
    assert "CONTRACT_CHECK — skipped (resume, cached)" in resume_out
    assert "Codex reviews cross-project consistency" not in resume_out, (
        "contract_check must NOT re-run a fresh reviewer pass on resume"
    )
    # Cached contract_check entry survives on the resumed session.
    assert "contract_check" in resumed["phases"]


def test_cross_final_acceptance_records_phase_entry_after_contract_check(
    tmp_path, monkeypatch,
) -> None:
    """Phase-log ordering: ``contract_check`` is recorded before
    ``cross_final_acceptance``. The cross runner relies on this order
    when building evidence release_summary and when MCP / Web consumers
    project ``session.phases`` keys onto a timeline.
    """
    from pipeline.cross_project import final_acceptance as cfa_mod, orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader,
        "load_profiles_v2_with_plugins",
        lambda _path: {"plan_only": _plan_only_profile()},
    )
    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[
            _approved_review_json(),
            _approved_review_json(),
            _approved_review_json(),
        ],
    )
    monkeypatch.setattr(
        cfa_mod,
        "run_cross_final_acceptance",
        lambda _ctx, **_kw: _stub_cfa_result(
            verdict="APPROVED",
            ship_ready=True,
            source="agent",
            short_summary="Ship-ready.",
        ),
    )

    session = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="plan_only",
    )

    phase_keys = list(session["phases"].keys())
    assert "contract_check" in phase_keys
    assert "cross_final_acceptance" in phase_keys
    assert phase_keys.index("contract_check") < phase_keys.index(
        "cross_final_acceptance"
    ), (
        "cross_final_acceptance must record AFTER contract_check — the "
        "gate consumes contract_check results via preconditions"
    )
    assert session["status"] == "done"


def test_cross_final_acceptance_parse_error_pauses_with_narrowed_actions(
    tmp_path, monkeypatch,
) -> None:
    """Gate parse-error path (source=parse_error) PAUSES on a
    ``cfa:``-prefixed handoff with the action set narrowed to
    ``[continue, halt]`` — ``retry_feedback`` is omitted because the
    reviewer's output could not be parsed and retrying with the same
    prompt is unlikely to yield a different shape (Phase A invariant
    7). Pre-Phase-A this branch terminated as ``status="failed"`` with
    a ``parse error``-naming failure_reason.
    """
    from pipeline.cross_project import final_acceptance as cfa_mod, orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    import pipeline.profiles.loader as _loader
    monkeypatch.setattr(
        _loader,
        "load_profiles_v2_with_plugins",
        lambda _path: {"plan_only": _plan_only_profile()},
    )
    provider = _ScriptedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[
            _approved_review_json(),
            _approved_review_json(),
            _approved_review_json(),
        ],
    )

    monkeypatch.setattr(
        cfa_mod,
        "run_cross_final_acceptance",
        lambda _ctx, **_kw: _stub_cfa_result(
            verdict="REJECTED",
            ship_ready=False,
            source="parse_error",
            short_summary="Reviewer returned non-JSON output.",
        ),
    )

    session = cross.run_cross_pipeline(
        task="x",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="plan_only",
    )

    # Phase A — CFA REJECTED (parse_error) is a pause point with
    # narrowed actions per invariant 7.
    assert session["status"] == "awaiting_phase_handoff"
    handoff = session.get("phase_handoff") or {}
    assert handoff.get("id", "").startswith("cfa:")
    assert handoff.get("phase") == "cross_final_acceptance"
    # source="parse_error" → retry_feedback omitted (no reviewer to
    # retry against the same prompt; only override/halt remain).
    assert handoff.get("available_actions") == ["continue", "halt"]
    # The parse_error text is carried in artifacts for operator
    # forensics — even when the action set is narrowed, the operator
    # can see WHY the reviewer's output could not be parsed.
    artifacts = handoff.get("artifacts") or {}
    assert artifacts.get("source") == "parse_error"


# ── _render_cross_plan_preview: structured-vs-fallback dispatch ───────


def _canonical_cross_plan_json(aliases: list[str]) -> str:
    """Canonical valid cross-plan JSON (ADR 0054) for the preview tests."""
    return _cp_json(*aliases)


def test_render_cross_plan_preview_renders_structured_block_for_canonical_input(
    monkeypatch,
) -> None:
    """Canonical input must route through ``render_cross_plan_block`` and
    never through the raw ``preview`` fallback.

    The names are bound in the ``rendering`` module's namespace
    (``preview`` is defined there; ``render_cross_plan_block`` is
    imported there) — ADR 0047 Phase B extracted these from
    ``orchestrator.py``. Patches land on ``rendering`` to take effect
    inside ``_render_cross_plan_preview``.
    """
    from pipeline.cross_project import rendering as cross_rendering

    render_calls: list[dict] = []

    def fake_render(mapping, **kwargs):
        render_calls.append({"arg": mapping, "kwargs": kwargs})
        return "<structured render>"

    def boom_preview(*_args, **_kwargs):
        raise AssertionError(
            "preview() must not run on canonical 3-section input"
        )

    monkeypatch.setattr(cross_rendering, "render_cross_plan_block", fake_render)
    monkeypatch.setattr(cross_rendering, "preview", boom_preview)

    cross_rendering._render_cross_plan_preview(
        _canonical_cross_plan_json(["api", "client"]),
        ["api", "client"],
    )

    assert len(render_calls) == 1


def test_render_cross_plan_preview_falls_back_to_preview_when_section_missing(
    monkeypatch,
) -> None:
    """Drop one canonical heading — the helper must invoke ``preview``
    with the magenta-coloured ``Cross-project plan`` label and skip the
    structured renderer entirely. This is the regression net for "parser
    failed silently and nobody noticed."
    """
    from pipeline.cross_project import rendering as cross_rendering

    preview_calls: list[tuple] = []

    def boom_render(*_args, **_kwargs):
        raise AssertionError(
            "render_cross_plan_block() must not run when a section is missing"
        )

    def spy_preview(label, text, color, *args, **kwargs):
        preview_calls.append((label, text, color))

    monkeypatch.setattr(cross_rendering, "render_cross_plan_block", boom_render)
    monkeypatch.setattr(cross_rendering, "preview", spy_preview)

    raw = "## Interface Contract\nx\n\n## Per-Project Subtasks\ny\n"
    cross_rendering._render_cross_plan_preview(raw, ["api"])

    assert len(preview_calls) == 1
    label, text, color = preview_calls[0]
    assert label == "Cross-project plan"
    assert text == raw
    assert color == cross_rendering.C.MAGENTA


def test_render_cross_plan_preview_passes_mapping_to_renderer(
    monkeypatch,
) -> None:
    """The dataclass-to-mapping adapter must run at the call-site so the
    renderer only ever sees a ``Mapping`` — never the frozen dataclass.
    Pins the bridge by shape (the four expected keys), not by which
    adapter implementation produced it.
    """
    from pipeline.cross_project import rendering as cross_rendering

    captured: list = []

    def fake_render(mapping, **_kwargs):
        captured.append(mapping)
        return ""

    monkeypatch.setattr(cross_rendering, "render_cross_plan_block", fake_render)
    monkeypatch.setattr(cross_rendering, "preview", lambda *a, **kw: None)

    cross_rendering._render_cross_plan_preview(
        _canonical_cross_plan_json(["api"]),
        ["api"],
    )

    assert len(captured) == 1
    arg = captured[0]
    assert isinstance(arg, dict)
    assert set(arg.keys()) == {
        "interface_contract",
        "implementation_order",
        "subtasks",
        "aliases_missing",
    }


def test_orchestrator_does_not_expose_extract_subtasks() -> None:
    """Regression pin: ``extract_subtasks`` must not exist as an
    attribute on the orchestrator module. A future change that adds
    ``from pipeline.cross_project.plan_parser import extract_subtasks``
    at module top would silently resurrect the deprecated import path
    ``pipeline.cross_project.orchestrator.extract_subtasks`` — this
    test fails fast if that happens.
    """
    from pipeline.cross_project import orchestrator as cross

    assert not hasattr(cross, "extract_subtasks"), (
        "extract_subtasks lives in pipeline.cross_project.plan_parser; "
        "do not rebind it on the orchestrator module."
    )


def test_cross_builders_are_reexported_at_package_level() -> None:
    """Regression pin for ADR 0060 acceptance: the cross-level prompt
    builders must remain importable from ``pipeline.cross_project``
    directly. The short-import surface is part of the public contract
    that PromptTurn refactor must preserve.
    """
    import pipeline.cross_project as cross_pkg
    from pipeline.prompts.turn import PromptTurn

    for name in (
        "cross_plan_prompt",
        "cross_replan_prompt",
        "cross_plan_review_focus",
        "contract_review_focus",
    ):
        assert hasattr(cross_pkg, name), (
            f"pipeline.cross_project.{name} must remain re-exported"
        )
        assert name in cross_pkg.__all__, (
            f"pipeline.cross_project.__all__ must list {name}"
        )

    sig = inspect.signature(cross_pkg.cross_plan_prompt)
    assert sig.return_annotation in (PromptTurn, "PromptTurn"), (
        "cross_plan_prompt must advertise a PromptTurn return type"
    )
    sig = inspect.signature(cross_pkg.cross_replan_prompt)
    assert sig.return_annotation in (PromptTurn, "PromptTurn"), (
        "cross_replan_prompt must advertise a PromptTurn return type"
    )
