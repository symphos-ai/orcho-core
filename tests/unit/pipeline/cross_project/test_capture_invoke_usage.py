"""Unit tests for ``_capture_invoke_usage`` and its accumulator / formatter.

These helpers normalise per-invoke usage at the cross-run capture boundary
so that runtimes which only surface ``last_tokens_total`` (Codex) still
contribute a non-zero in/out split to the rollup. The same normalised dict
feeds both the compact phase-level snapshot and the final rollup — there
is one accounting path, never two.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# ADR 0047 Phase D — usage helpers moved with the run body to
# ``pipeline.cross_project.session_run`` (or stayed in ``pipeline.cross_project.usage``
# where they were always defined). Tests now reach the canonical home.
from pipeline.cross_project.usage import (
    _capture_invoke_usage,
    accumulate_phase_usage as _accumulate_phase_usage,
    format_usage_snapshot as _format_usage_snapshot,
)

# ──────────────────────────────────────────────────────────────────────────
# fakes
# ──────────────────────────────────────────────────────────────────────────


def _agent(**kw):
    """SimpleNamespace agent with safe defaults for unspecified ``last_*`` attrs."""
    defaults = {
        "last_tokens_in": 0,
        "last_tokens_out": 0,
        "last_tokens_total": 0,
        "last_estimated_tokens_in": 0,
        "last_estimated_tokens_out": 0,
        "last_cost_usd": None,
        "model": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.fixture
def accounting_on(monkeypatch: pytest.MonkeyPatch):
    from core.infra import config
    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    yield
    config._reset_config()


# ──────────────────────────────────────────────────────────────────────────
# split precedence
# ──────────────────────────────────────────────────────────────────────────


class TestExactSplit:
    def test_runtime_provides_in_and_out(self):
        u = _capture_invoke_usage(
            _agent(last_tokens_in=100, last_tokens_out=20),
            duration_s=1.5,
        )
        assert u["token_split_source"] == "exact"
        assert u["token_split_estimated"] is False
        assert u["tokens_in"] == 100
        assert u["tokens_out"] == 20
        assert u["total_tokens"] == 120
        assert u["calls"] == 1
        assert u["duration_s"] == 1.5

    def test_exact_split_with_total_already_set(self):
        u = _capture_invoke_usage(
            _agent(last_tokens_in=100, last_tokens_out=20, last_tokens_total=120),
        )
        assert u["token_split_source"] == "exact"
        assert u["total_tokens"] == 120
        assert u["tokens_in"] + u["tokens_out"] == 120

    def test_exact_split_overrides_conflicting_runtime_total(self):
        """If a runtime reports a ``last_tokens_total`` that disagrees with
        ``in + out``, the split wins. The invariant downstream consumers
        (snapshot, rollup) rely on is ``tokens_in + tokens_out == total_tokens``;
        a conflicting runtime total would break it."""
        u = _capture_invoke_usage(
            _agent(
                last_tokens_in=100, last_tokens_out=20,
                last_tokens_total=999,  # disagrees with 100+20
            ),
        )
        assert u["token_split_source"] == "exact"
        assert u["tokens_in"] == 100
        assert u["tokens_out"] == 20
        assert u["total_tokens"] == 120
        assert u["tokens_in"] + u["tokens_out"] == u["total_tokens"]

    def test_runtime_estimated_split_overrides_conflicting_total(self):
        u = _capture_invoke_usage(
            _agent(
                last_estimated_tokens_in=80,
                last_estimated_tokens_out=20,
                last_tokens_total=999,
            ),
        )
        assert u["token_split_source"] == "runtime_estimate"
        assert u["tokens_in"] == 80
        assert u["tokens_out"] == 20
        assert u["total_tokens"] == 100


class TestRuntimeEstimatedSplit:
    def test_runtime_estimated_in_out_preferred_over_text(self):
        u = _capture_invoke_usage(
            _agent(
                last_estimated_tokens_in=80,
                last_estimated_tokens_out=20,
                last_tokens_total=100,
            ),
            prompt="ignored should not be used",
            output="ignored",
        )
        assert u["token_split_source"] == "runtime_estimate"
        assert u["token_split_estimated"] is True
        assert u["tokens_in"] == 80
        assert u["tokens_out"] == 20


class TestTextEstimateScaled:
    def test_codex_total_only_with_prompt_and_output(self):
        # Prompt ~ 4x larger than output → tokens_in should dominate.
        prompt = "a" * 4000
        output = "b" * 1000
        u = _capture_invoke_usage(
            _agent(last_tokens_total=1000),
            prompt=prompt,
            output=output,
        )
        assert u["token_split_source"] == "text_estimate_scaled"
        assert u["token_split_estimated"] is True
        assert u["total_tokens"] == 1000
        assert u["tokens_in"] + u["tokens_out"] == 1000
        assert u["tokens_in"] > u["tokens_out"]
        # ratio sanity: prompt remains the dominant estimated side.
        # The exact ratio depends on the local tokenizer/fallback.
        assert 600 <= u["tokens_in"] <= 850

    def test_both_empty_strings_fall_through_to_aggregate(self):
        # Empty strings give zero text estimate → fall through to 50/50.
        u = _capture_invoke_usage(
            _agent(last_tokens_total=200),
            prompt="",
            output="",
        )
        assert u["token_split_source"] == "aggregate_total_only"
        assert u["tokens_in"] + u["tokens_out"] == 200

    def test_one_side_empty_still_scales(self):
        # Even if output is "", prompt has signal → scale gives 100% to in.
        u = _capture_invoke_usage(
            _agent(last_tokens_total=400),
            prompt="x" * 1000,
            output="",
        )
        assert u["token_split_source"] == "text_estimate_scaled"
        assert u["tokens_in"] + u["tokens_out"] == 400
        assert u["tokens_in"] == 400
        assert u["tokens_out"] == 0


class TestAggregateTotalOnly:
    def test_total_only_no_prompt_or_output(self):
        u = _capture_invoke_usage(_agent(last_tokens_total=1000))
        assert u["token_split_source"] == "aggregate_total_only"
        assert u["token_split_estimated"] is True
        assert u["total_tokens"] == 1000
        assert u["tokens_in"] + u["tokens_out"] == 1000
        # 50/50 ±1
        assert abs(u["tokens_in"] - u["tokens_out"]) <= 1

    def test_total_only_prompt_missing(self):
        u = _capture_invoke_usage(
            _agent(last_tokens_total=500),
            output="something",
        )
        assert u["token_split_source"] == "aggregate_total_only"
        assert u["tokens_in"] + u["tokens_out"] == 500

    def test_odd_total_split_sums_exactly(self):
        u = _capture_invoke_usage(_agent(last_tokens_total=1001))
        assert u["tokens_in"] + u["tokens_out"] == 1001


# ──────────────────────────────────────────────────────────────────────────
# cost
# ──────────────────────────────────────────────────────────────────────────


class TestCost:
    @pytest.fixture(autouse=True)
    def _enable_accounting(self, accounting_on) -> None:
        pass

    def test_real_cost_preserved(self):
        u = _capture_invoke_usage(
            _agent(last_tokens_in=100, last_tokens_out=50, last_cost_usd=0.123),
        )
        assert u["cost_usd_equivalent"] == 0.123
        assert u["cost_estimated"] is False

    def test_estimated_cost_when_model_known(self, monkeypatch):

        # Stub pricing so the test doesn't depend on the bundled snapshot.
        def _fake_estimate(model, *, tokens_in, tokens_out, cached_tokens_in=0):
            assert model == "fake-priced-model"
            return tokens_in * 0.000001 + tokens_out * 0.000002

        monkeypatch.setattr(
            "core.observability.pricing.estimate_cost_usd", _fake_estimate,
        )
        u = _capture_invoke_usage(
            _agent(last_tokens_total=1000, model="fake-priced-model"),
        )
        assert "cost_usd_equivalent" in u
        assert u["cost_estimated"] is True
        # 500 * 1e-6 + 500 * 2e-6 = 0.0015
        assert u["cost_usd_equivalent"] == pytest.approx(0.0015, rel=1e-3)

    def test_estimated_cost_uses_cache_read_tokens_for_codex(
        self, monkeypatch,
    ):
        calls = []

        def _fake_estimate(model, *, tokens_in, tokens_out, cached_tokens_in=0):
            calls.append({
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cached_tokens_in": cached_tokens_in,
            })
            return 1.2345

        monkeypatch.setattr(
            "core.observability.pricing.estimate_cost_usd", _fake_estimate,
        )

        u = _capture_invoke_usage(
            _agent(
                last_tokens_in=10_000,
                last_tokens_out=400,
                last_tokens_in_cache_read=8_000,
                model="gpt-5.5",
            ),
        )

        assert u["tokens_in_cache_read"] == 8_000
        assert u["cost_usd_equivalent"] == pytest.approx(1.2345)
        assert calls == [{
            "model": "gpt-5.5",
            "tokens_in": 10_000,
            "tokens_out": 400,
            "cached_tokens_in": 8_000,
        }]

    def test_unknown_model_omits_cost(self, monkeypatch):
        monkeypatch.setattr(
            "core.observability.pricing.estimate_cost_usd",
            lambda *a, **kw: None,
        )
        # Reset the warning dedupe set so this test doesn't depend on
        # whether a previous test already warned for "unknown-model".
        # ADR 0047 Phase D — `_UNPRICED_MODELS_WARNED` is owned by
        # ``pipeline.cross_project.usage`` (its canonical home); the
        # ``_capture_invoke_usage`` wrapper in ``pipeline.cross_project.session_run``
        # imports a reference into its module namespace. Reset the
        # canonical set so subsequent calls into either location see
        # the reset.
        from pipeline.cross_project import usage as orch_mod
        monkeypatch.setattr(orch_mod, "_UNPRICED_MODELS_WARNED", set())
        u = _capture_invoke_usage(
            _agent(last_tokens_total=1000, model="unknown-model"),
        )
        assert "cost_usd_equivalent" not in u
        assert "cost_estimated" not in u

    def test_unpriced_model_warns_once(self, monkeypatch, capsys):
        """When a model has no pricing entry, ``_capture_invoke_usage``
        warns once per process. Subsequent invokes of the same unpriced
        model are silent — multi-call phases shouldn't spam."""
        monkeypatch.setattr(
            "core.observability.pricing.estimate_cost_usd",
            lambda *a, **kw: None,
        )
        # ADR 0047 Phase D — `_UNPRICED_MODELS_WARNED` lives in
        # ``pipeline.cross_project.usage``; ``_capture_invoke_usage``
        # (now in ``pipeline.cross_project.session_run``) imports a reference
        # into app's namespace. Reset via the app reference.
        from pipeline.cross_project import usage as orch_mod
        monkeypatch.setattr(orch_mod, "_UNPRICED_MODELS_WARNED", set())

        _capture_invoke_usage(
            _agent(last_tokens_total=1000, model="gpt-x-unpriced"),
        )
        out_first = capsys.readouterr().out

        _capture_invoke_usage(
            _agent(last_tokens_total=500, model="gpt-x-unpriced"),
        )
        _capture_invoke_usage(
            _agent(last_tokens_total=2000, model="gpt-x-unpriced"),
        )
        out_rest = capsys.readouterr().out

        assert "gpt-x-unpriced" in out_first
        assert "No pricing" in out_first
        assert "orcho pricing refresh" in out_first
        # No further warnings for the same model.
        assert "gpt-x-unpriced" not in out_rest

    def test_unpriced_model_warns_per_distinct_model(self, monkeypatch, capsys):
        """Dedup is per model name — a different unpriced model triggers
        its own warning."""
        monkeypatch.setattr(
            "core.observability.pricing.estimate_cost_usd",
            lambda *a, **kw: None,
        )
        # ADR 0047 Phase D — `_UNPRICED_MODELS_WARNED` lives in
        # ``pipeline.cross_project.usage``; ``_capture_invoke_usage``
        # (now in ``pipeline.cross_project.session_run``) imports a reference
        # into app's namespace. Reset via the app reference.
        from pipeline.cross_project import usage as orch_mod
        monkeypatch.setattr(orch_mod, "_UNPRICED_MODELS_WARNED", set())

        _capture_invoke_usage(
            _agent(last_tokens_total=1000, model="model-a"),
        )
        _capture_invoke_usage(
            _agent(last_tokens_total=1000, model="model-b"),
        )
        out = capsys.readouterr().out
        assert "model-a" in out
        assert "model-b" in out

    def test_priced_model_does_not_warn(self, monkeypatch, capsys):
        """Cost found → no warning emitted, even on first invoke."""
        monkeypatch.setattr(
            "core.observability.pricing.estimate_cost_usd",
            lambda *a, **kw: 0.001,
        )
        # ADR 0047 Phase D — `_UNPRICED_MODELS_WARNED` lives in
        # ``pipeline.cross_project.usage``; ``_capture_invoke_usage``
        # (now in ``pipeline.cross_project.session_run``) imports a reference
        # into app's namespace. Reset via the app reference.
        from pipeline.cross_project import usage as orch_mod
        monkeypatch.setattr(orch_mod, "_UNPRICED_MODELS_WARNED", set())
        _capture_invoke_usage(
            _agent(last_tokens_total=1000, model="known-model"),
        )
        out = capsys.readouterr().out
        assert "No pricing" not in out

    def test_zero_total_no_cost_attempt(self, monkeypatch):
        # Tracker: estimate_cost_usd must NOT be called when total_tokens=0.
        calls = []
        monkeypatch.setattr(
            "core.observability.pricing.estimate_cost_usd",
            lambda *a, **kw: calls.append((a, kw)) or 0.0,
        )
        u = _capture_invoke_usage(_agent(model="some-model"))
        assert calls == []
        assert "cost_usd_equivalent" not in u


# ──────────────────────────────────────────────────────────────────────────
# invariant
# ──────────────────────────────────────────────────────────────────────────


class TestInvariant:
    @pytest.mark.parametrize(
        "agent_kw",
        [
            {"last_tokens_in": 7, "last_tokens_out": 3},
            {"last_tokens_in": 7, "last_tokens_out": 3, "last_tokens_total": 10},
            {"last_estimated_tokens_in": 60, "last_estimated_tokens_out": 40, "last_tokens_total": 100},
            {"last_tokens_total": 1000},
            {"last_tokens_total": 1001},
            {"last_tokens_total": 1},
        ],
    )
    def test_split_sums_to_total_when_total_positive(self, agent_kw):
        u = _capture_invoke_usage(
            _agent(**agent_kw),
            prompt="alpha" * 50,
            output="beta" * 10,
        )
        if u["total_tokens"] > 0:
            assert u["tokens_in"] + u["tokens_out"] == u["total_tokens"]


# ──────────────────────────────────────────────────────────────────────────
# accumulator metadata merge
# ──────────────────────────────────────────────────────────────────────────


class TestAccumulatorMerge:
    def test_split_estimated_is_logical_or(self):
        target: dict = {}
        _accumulate_phase_usage(target, "phase", {
            "tokens_in": 10, "tokens_out": 5, "total_tokens": 15,
            "duration_s": 1.0, "calls": 1,
            "token_split_estimated": False,
            "token_split_source": "exact",
            "model": "claude-x",
        })
        _accumulate_phase_usage(target, "phase", {
            "tokens_in": 20, "tokens_out": 10, "total_tokens": 30,
            "duration_s": 2.0, "calls": 1,
            "token_split_estimated": True,
            "token_split_source": "aggregate_total_only",
            "model": "codex-y",
        })
        e = target["phase"]
        assert e["tokens_in"] == 30
        assert e["tokens_out"] == 15
        assert e["total_tokens"] == 45
        assert e["calls"] == 2
        assert e["token_split_estimated"] is True
        assert e["token_split_source"] == "mixed"
        assert e["model"] == "mixed"

    def test_agree_keeps_value(self):
        target: dict = {}
        for _ in range(3):
            _accumulate_phase_usage(target, "phase", {
                "tokens_in": 10, "tokens_out": 5, "total_tokens": 15,
                "duration_s": 1.0, "calls": 1,
                "token_split_estimated": True,
                "token_split_source": "text_estimate_scaled",
                "model": "codex-y",
            })
        e = target["phase"]
        assert e["token_split_source"] == "text_estimate_scaled"
        assert e["model"] == "codex-y"
        assert e["token_split_estimated"] is True

    def test_cost_estimated_or_across_invokes_with_cost(self):
        target: dict = {}
        _accumulate_phase_usage(target, "phase", {
            "tokens_in": 10, "tokens_out": 5, "total_tokens": 15,
            "duration_s": 1.0, "calls": 1,
            "cost_usd_equivalent": 0.1,
            "cost_estimated": False,
        })
        _accumulate_phase_usage(target, "phase", {
            "tokens_in": 10, "tokens_out": 5, "total_tokens": 15,
            "duration_s": 1.0, "calls": 1,
            "cost_usd_equivalent": 0.05,
            "cost_estimated": True,
        })
        assert target["phase"]["cost_estimated"] is True
        assert target["phase"]["cost_usd_equivalent"] == 0.15

    def test_model_omitted_when_no_invoke_supplies_it(self):
        target: dict = {}
        _accumulate_phase_usage(target, "phase", {
            "tokens_in": 10, "tokens_out": 5, "total_tokens": 15,
            "duration_s": 1.0, "calls": 1,
        })
        assert "model" not in target["phase"]


# ──────────────────────────────────────────────────────────────────────────
# snapshot formatter
# ──────────────────────────────────────────────────────────────────────────


class TestSnapshotFormatter:
    @pytest.fixture(autouse=True)
    def _enable_accounting(self, accounting_on) -> None:
        pass

    def test_exact_split_real_cost(self):
        line = _format_usage_snapshot("cross_plan round=1", {
            "total_tokens": 330_401,
            "tokens_in": 326_846,
            "tokens_out": 3_555,
            "duration_s": 66.5,
            "calls": 1,
            "token_split_estimated": False,
            "cost_usd_equivalent": 0.35,
            "cost_estimated": False,
        })
        assert "usage: cross_plan round=1" in line
        assert "total=330,401" in line
        assert "in=326,846" in line
        assert "out=3,555" in line
        assert "cost_ref=runtime-reported:$0.35" in line
        assert "~" not in line
        assert "\n" not in line

    def test_estimated_split_and_cost(self):
        line = _format_usage_snapshot("cross_validate_plan round=2", {
            "total_tokens": 21_673,
            "tokens_in": 18_900,
            "tokens_out": 2_773,
            "duration_s": 22.0,
            "calls": 1,
            "token_split_estimated": True,
            "cost_usd_equivalent": 0.02,
            "cost_estimated": True,
        })
        assert "in~18,900" in line
        assert "out~2,773" in line
        assert "cost_ref=estimated-api:~$0.02" in line

    def test_independent_markers_exact_split_estimated_cost(self):
        line = _format_usage_snapshot("phase", {
            "total_tokens": 100,
            "tokens_in": 60,
            "tokens_out": 40,
            "duration_s": 1.0,
            "calls": 1,
            "token_split_estimated": False,
            "cost_usd_equivalent": 0.001,
            "cost_estimated": True,
        })
        assert "in=60" in line
        assert "out=40" in line
        assert "cost_ref=estimated-api:~$0.00" in line  # rounds to 2dp

    def test_no_cost_renders_dash(self):
        line = _format_usage_snapshot("phase", {
            "total_tokens": 100,
            "tokens_in": 50,
            "tokens_out": 50,
            "duration_s": 1.0,
            "calls": 1,
            "token_split_estimated": True,
        })
        assert "cost_ref=-" in line
        assert "in~50" in line
        assert "out~50" in line

    def test_single_line_no_table_header(self):
        line = _format_usage_snapshot("phase", {
            "total_tokens": 1, "tokens_in": 0, "tokens_out": 1,
            "duration_s": 0.1, "calls": 1,
            "token_split_estimated": False,
        })
        assert "\n" not in line
        assert "Phase" not in line
        assert "TOTAL" not in line
