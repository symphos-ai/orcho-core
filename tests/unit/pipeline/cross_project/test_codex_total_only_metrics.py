"""Regression: Codex-style runtimes that surface only ``last_tokens_total``
(no in/out split, no cost) must still contribute a non-zero token split
to ``cross_phase_usage`` for every Codex-driven cross-level phase.

Two coverage tiers:

* Plan-mode test exercises ``cross_plan`` (Claude exact path) and
  ``cross_validate_plan`` (Codex total-only path).
* Full-mode test (``plan_only`` profile + precondition stub) exercises
  ``contract_check`` and ``cross_final_acceptance`` — the two phases
  that were visibly broken in the original screenshot.

Pre-fix symptom: these phases displayed ``In=0 Out=0 Total=N`` in the
cross-run rollup, losing the split and the API-equivalent cost estimate.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tests.unit.pipeline.cross_project.test_cross_orchestrator import _cp_json

# ──────────────────────────────────────────────────────────────────────────
# Total-only fake runtimes
# ──────────────────────────────────────────────────────────────────────────


class _TotalOnlyRuntime:
    """Fake runtime that mimics Codex: sets ``last_tokens_total`` after
    each invoke; never sets ``last_tokens_in`` / ``last_tokens_out`` /
    ``last_cost_usd``. The normalised capture path must synthesise a
    non-zero in/out split from this.
    """

    model = "fake-codex"

    def __init__(self, outputs: list[str], total_per_call: int = 1000) -> None:
        self._outputs = list(outputs)
        self._total = total_per_call
        self.calls: list[tuple[str, str, dict]] = []
        self.last_tokens_total = 0
        self.last_tokens_in = 0
        self.last_tokens_out = 0
        self.last_cost_usd = None

    def invoke(self, prompt: str, cwd: str, **kwargs) -> str:
        self.calls.append((prompt, cwd, kwargs))
        self.last_tokens_total = self._total
        # explicitly NOT setting in/out — mirroring Codex CLI behaviour
        if self._outputs:
            return self._outputs.pop(0)
        return ""


class _RichClaudeRuntime:
    """Fake runtime that mimics Claude: sets exact in/out + cost."""

    model = "fake-claude"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.calls: list[tuple[str, str, dict]] = []
        self.last_tokens_in = 0
        self.last_tokens_out = 0
        self.last_tokens_total = 0
        self.last_cost_usd = None

    def invoke(self, prompt: str, cwd: str, **kwargs) -> str:
        self.calls.append((prompt, cwd, kwargs))
        self.last_tokens_in = 800
        self.last_tokens_out = 200
        self.last_tokens_total = 1000
        self.last_cost_usd = 0.05
        if self._outputs:
            return self._outputs.pop(0)
        return ""


class _MixedProvider:
    """Provider wiring: Claude for plan, total-only Codex for reviewer."""

    def __init__(self, plan_outputs: list[str], review_outputs: list[str]) -> None:
        self.plan = _RichClaudeRuntime(plan_outputs)
        self.review = _TotalOnlyRuntime(review_outputs, total_per_call=1500)

    def resolve(self, runtime: str, _model: str, *, effort: str | None = None):  # noqa: ARG002
        return self.review if runtime == "codex" else self.plan

    def claude(self, _model: str):
        return self.plan

    def codex(self, _model: str):
        return self.review


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _approved_review_json() -> str:
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": "Plan coherent.",
        "findings":      [],
        "risks":         [],
        "checks":        [],
    })


def _approved_release_json() -> str:
    return json.dumps({
        "verdict":            "APPROVED",
        "ship_ready":         True,
        "short_summary":      "Coordinated change ships.",
        "release_blockers":   [],
        "verification_gaps":  [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "compatible",
            "persistence":   "safe",
            "tests":         "sufficient",
        },
    })


def _cross_test_appconfig_mock(monkeypatch, cross_mod) -> None:
    monkeypatch.setattr(
        cross_mod.config.AppConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(
            hypothesis={"enabled": False},
            task_language="English",
            pipeline={},
            artifacts={},
        )),
    )
    monkeypatch.setattr(
        cross_mod,
        "load_plugin",
        lambda _path: SimpleNamespace(
            name="P", language="Python", architecture="", file_hints=[],
        ),
    )


# ──────────────────────────────────────────────────────────────────────────
# Regression test
# ──────────────────────────────────────────────────────────────────────────


def _run_with_usage_capture(tmp_path, monkeypatch, provider):
    """Drive ``run_cross_pipeline`` and capture every per-invoke usage
    object that the orchestrator hands to ``_accumulate_phase_usage``.

    Plan-mode returns before the final rollup block, so ``session["metrics"]``
    isn't populated. Tap the accumulator instead — that's the single source
    of truth for both the live snapshots and the final rollup.
    """
    from pipeline.cross_project import orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    captured: list[tuple[str, dict]] = []
    # ADR 0047 Phase D — three call sites use ``accumulate_phase_usage``:
    # ``app.py`` (cross_hypothesis / contract_check / cross_final_acceptance)
    # via a top-level import alias, and ``planning_loop.py``'s
    # ``_invoke_validate_round`` / ``_invoke_plan_round`` /
    # ``_retry_with_feedback`` via FRESH lazy imports at each call. The
    # canonical source for both is ``pipeline.cross_project.usage``;
    # patching at that root intercepts every lazy lookup uniformly.
    from pipeline.cross_project import (
        session_run as cross_session_run,
        usage as cross_usage,
    )
    real_accumulate = cross_usage.accumulate_phase_usage

    def _spy(target, phase, usage):
        captured.append((phase, dict(usage)))
        return real_accumulate(target, phase, usage)

    monkeypatch.setattr(cross_usage, "accumulate_phase_usage", _spy)
    # ``app.py`` already bound a reference into its namespace at module
    # load; also patch that local binding so the body's
    # ``_accumulate_phase_usage`` resolves to the spy too.
    monkeypatch.setattr(cross_session_run, "_accumulate_phase_usage", _spy)

    cross.run_cross_pipeline(
        task="Align field",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="plan",
    )
    return captured


def test_codex_total_only_produces_nonzero_in_out_for_validate_plan(
    tmp_path, monkeypatch,
) -> None:
    """When the reviewer runtime exposes only ``last_tokens_total``, the
    ``cross_validate_plan`` usage dict must have a non-zero in/out split
    that sums to ``total_tokens`` — not the pre-fix ``In=0 Out=0 Total=N``
    shape.
    """
    provider = _MixedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[_approved_review_json()],
    )
    captured = _run_with_usage_capture(tmp_path, monkeypatch, provider)

    cv_entries = [u for (phase, u) in captured if phase == "cross_validate_plan"]
    assert cv_entries, (
        f"expected at least one cross_validate_plan usage capture; "
        f"got phases={[p for p, _ in captured]}"
    )
    entry = cv_entries[0]
    assert entry["total_tokens"] > 0, entry
    # **Regression assertion**: pre-fix bug was zero in/out with non-zero total.
    assert not (entry["tokens_in"] == 0 and entry["tokens_out"] == 0), entry
    assert entry["tokens_in"] + entry["tokens_out"] == entry["total_tokens"]
    assert entry["token_split_estimated"] is True


def _plan_only_profile():
    """Plan + validate_plan at the cross level, no per-project steps.

    Drives ``cross_mode="full"`` through ``contract_check`` and
    ``cross_final_acceptance`` without spinning up sub-pipelines.
    Carries an explicit ``cross_gates`` opt-in because the catalogue
    rule is missing≡off — a Profile() built with the default empty
    cross_gates would disable both terminal gates and skip the very
    paths these metrics tests need to exercise.
    """
    from pipeline.runtime import (
        ContractCheckMode,
        CrossGatePolicy,
        CrossGateRunPolicy,
        CrossGateSkipPolicy,
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
        cross_gates={
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
        },
    )


def _run_full_with_usage_capture(tmp_path, monkeypatch, provider):
    """Full-mode cross run with the ``plan_only`` profile and stubbed
    preconditions, so ``contract_check`` and ``cross_final_acceptance``
    both reach their agent paths and call the fake Codex.
    """
    import pipeline.profiles.loader as _loader
    from pipeline.cross_project import final_acceptance as cfa_mod, orchestrator as cross
    _cross_test_appconfig_mock(monkeypatch, cross)

    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()

    monkeypatch.setattr(
        _loader,
        "load_profiles_v2_with_plugins",
        lambda _path: {"plan_only": _plan_only_profile()},
    )
    # Bypass precondition collection so the gate reaches the agent path.
    # Real preconditions need per-project ``final_acceptance`` entries
    # which ``plan_only`` doesn't produce — orthogonal to what we're
    # testing here (token accounting on the agent invoke).
    monkeypatch.setattr(
        cfa_mod,
        "_collect_preconditions",
        lambda _ctx: cfa_mod.CrossFinalPreconditions(),
    )

    captured: list[tuple[str, dict]] = []
    # ADR 0047 Phase D — patch at usage.py's root + at app.py's
    # imported reference. See the cross_validate_plan helper above
    # for the full rationale (planning_loop's lazy imports go to
    # usage; app body's top-level alias binds at module load).
    from pipeline.cross_project import (
        contract_check as cross_contract,
        session_run as cross_session_run,
        usage as cross_usage,
    )
    real_accumulate = cross_usage.accumulate_phase_usage

    def _spy(target, phase, usage):
        captured.append((phase, dict(usage)))
        return real_accumulate(target, phase, usage)

    monkeypatch.setattr(cross_usage, "accumulate_phase_usage", _spy)
    monkeypatch.setattr(cross_session_run, "_accumulate_phase_usage", _spy)
    monkeypatch.setattr(cross_contract, "_accumulate_phase_usage", _spy)

    cross.run_cross_pipeline(
        task="Align field",
        projects={"api": api, "web": web},
        output_dir=tmp_path / "run",
        provider=provider,
        cross_mode="full",
        profile_name="plan_only",
    )
    return captured


def test_contract_check_total_only_produces_nonzero_split(
    tmp_path, monkeypatch,
) -> None:
    """``contract_check`` runs once per alias against a Codex agent. With
    a total-only fake the per-invoke captures must each have non-zero
    splits; the rolled-up phase entry sums them and stays consistent."""
    # Outputs needed across the full flow:
    #   2 review outputs: cross_validate_plan (round 1) — provider's
    #     review runtime is shared; the codex agent answers reviewer +
    #     contract_check + cross_final_acceptance.
    # contract_check loops over 2 aliases (api, web).
    # cross_final_acceptance asks Codex once more.
    review_outputs = [
        _approved_review_json(),  # cross_validate_plan
        _approved_review_json(),  # contract_check (artifact_bundle: one call)
        # cross_final_acceptance — release-shaped payload
        _approved_release_json(),
    ]
    provider = _MixedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=review_outputs,
    )
    captured = _run_full_with_usage_capture(tmp_path, monkeypatch, provider)

    cc_entries = [u for (phase, u) in captured if phase == "contract_check"]
    # Artifact-bundle is the documented default: exactly one Codex call
    # per cross-run for contract_check, regardless of alias count.
    assert len(cc_entries) == 1, (
        f"contract_check (artifact_bundle) must capture once per "
        f"cross-run; got {len(cc_entries)} "
        f"(phases={[p for p, _ in captured]})"
    )
    for entry in cc_entries:
        assert entry["total_tokens"] > 0, entry
        assert not (entry["tokens_in"] == 0 and entry["tokens_out"] == 0), entry
        assert entry["tokens_in"] + entry["tokens_out"] == entry["total_tokens"]
        assert entry["token_split_estimated"] is True


def test_cross_final_acceptance_total_only_produces_nonzero_split(
    tmp_path, monkeypatch,
) -> None:
    """``cross_final_acceptance`` invokes Codex once on the agent path.
    The accumulated usage entry must have a non-zero in/out split."""
    review_outputs = [
        _approved_review_json(),
        _approved_review_json(),
        _approved_review_json(),
        _approved_release_json(),
    ]
    provider = _MixedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=review_outputs,
    )
    captured = _run_full_with_usage_capture(tmp_path, monkeypatch, provider)

    cfa_entries = [
        u for (phase, u) in captured if phase == "cross_final_acceptance"
    ]
    assert len(cfa_entries) == 1, (
        f"cross_final_acceptance agent path must capture exactly once; "
        f"got {len(cfa_entries)} (phases={[p for p, _ in captured]})"
    )
    entry = cfa_entries[0]
    assert entry["total_tokens"] > 0, entry
    assert not (entry["tokens_in"] == 0 and entry["tokens_out"] == 0), entry
    assert entry["tokens_in"] + entry["tokens_out"] == entry["total_tokens"]
    assert entry["token_split_estimated"] is True


def test_cross_final_acceptance_total_only_captures_zero_duration_invoke(
    tmp_path, monkeypatch,
) -> None:
    """CFA token capture must not depend on positive wall-clock duration.

    Fast fakes can complete inside one ``time.time()`` tick after prompt
    caches are warm, but the model usage is still real and must be
    accumulated.
    """
    from pipeline.cross_project import final_acceptance as cfa_mod

    monkeypatch.setattr(cfa_mod.time, "time", lambda: 1.0)
    provider = _MixedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[
            _approved_review_json(),
            _approved_review_json(),
            _approved_review_json(),
            _approved_release_json(),
        ],
    )
    captured = _run_full_with_usage_capture(tmp_path, monkeypatch, provider)

    cfa_entries = [
        u for (phase, u) in captured if phase == "cross_final_acceptance"
    ]
    assert len(cfa_entries) == 1, (
        f"cross_final_acceptance must capture zero-duration invokes; "
        f"got phases={[p for p, _ in captured]}"
    )
    entry = cfa_entries[0]
    assert entry["duration_s"] == 0.0
    assert entry["total_tokens"] > 0, entry
    assert entry["tokens_in"] + entry["tokens_out"] == entry["total_tokens"]


def test_rich_claude_runtime_uses_exact_split(tmp_path, monkeypatch) -> None:
    """Precedence sanity: when the runtime supplies an exact in/out split,
    normalisation does NOT overwrite it with an estimate."""
    from core.infra import config
    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    provider = _MixedProvider(
        plan_outputs=[
            _cp_json("api", "web")
        ],
        review_outputs=[_approved_review_json()],
    )
    captured = _run_with_usage_capture(tmp_path, monkeypatch, provider)

    cp_entries = [u for (phase, u) in captured if phase == "cross_plan"]
    assert cp_entries
    entry = cp_entries[0]
    # Claude fake hard-codes 800 in / 200 out → must round-trip verbatim.
    assert entry["tokens_in"] == 800, entry
    assert entry["tokens_out"] == 200, entry
    assert entry["total_tokens"] == 1000, entry
    assert entry["token_split_source"] == "exact"
    assert entry["token_split_estimated"] is False
    assert entry["cost_usd_equivalent"] == 0.05
    assert entry["cost_estimated"] is False
