"""
M1.4 Metrics & Token Tracking tests.

Tests cover:
 * estimate_tokens: empty, short, long, utf-8
 * PhaseMetrics: total_tokens, as_dict, retries field omitted when 0
 * MetricsCollector: record_phase, aggregates, add_round, as_dict
 * save() → metrics.json with correct schema
 * summary_line / summary_table
 * load_historical_runs / format_history_table
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.observability.metrics import (
    MetricsCollector,
    PhaseMetrics,
    _clear_tokenizer_cache_for_tests,
    cross_metrics_dict,
    cross_summary_line,
    cross_summary_table,
    estimate_model_tokens,
    estimate_tokens,
    estimate_tokens_with_source,
    format_history_table,
    load_historical_runs,
)


@pytest.fixture
def accounting_on(monkeypatch: pytest.MonkeyPatch):
    from core.infra import config
    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    yield
    config._reset_config()

# ─────────────────────────────────────────────────────────────────────────────
# estimate_tokens
# ─────────────────────────────────────────────────────────────────────────────

class TestEstimateTokens:
    def test_empty_string_returns_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_none_equivalent_empty(self) -> None:
        # passing empty string covers the None guard
        assert estimate_tokens("") == 0

    def test_short_string(self) -> None:
        # "hello" = 5 bytes → 5//4 = 1 (min 1)
        assert estimate_tokens("hello") == 1

    def test_hundred_chars(self) -> None:
        text = "a" * 100
        estimate = estimate_tokens_with_source(text)
        assert estimate.tokens > 0
        assert estimate.source in {
            "byte_heuristic",
            "tiktoken:o200k_base:heuristic",
        }

    def test_utf8_multibyte(self) -> None:
        text = "а" * 10
        estimate = estimate_tokens_with_source(text)
        assert estimate.tokens > 0
        assert estimate.source in {
            "byte_heuristic",
            "tiktoken:o200k_base:heuristic",
        }

    def test_large_text(self) -> None:
        text = "x" * 40_000
        estimate = estimate_tokens_with_source(text)
        assert estimate.tokens > 0
        assert estimate.source in {
            "byte_heuristic",
            "tiktoken:o200k_base:heuristic",
        }

    def test_model_estimator_uses_optional_tiktoken_for_openai_like_model(
        self, monkeypatch,
    ) -> None:
        class FakeEncoding:
            name = "fake_base"

            def encode(self, text: str) -> list[int]:
                return list(range(len(text.split())))

        fake_tiktoken = SimpleNamespace(
            encoding_for_model=lambda model: FakeEncoding(),
            get_encoding=lambda name: FakeEncoding(),
        )
        monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)
        _clear_tokenizer_cache_for_tests()
        try:
            estimate = estimate_tokens_with_source(
                "one two three", model="gpt-5-codex",
            )
            assert estimate.tokens == 3
            assert estimate.source == "tiktoken:fake_base"
            assert estimate.exact is True
            assert estimate_model_tokens("one two", model="gpt-5-codex") == 2
        finally:
            _clear_tokenizer_cache_for_tests()

    def test_openai_like_unknown_model_uses_tiktoken_fallback_not_exact(
        self, monkeypatch,
    ) -> None:
        class FakeEncoding:
            name = "fallback_base"

            def encode(self, text: str) -> list[int]:
                return list(range(len(text.split())))

        fake_tiktoken = SimpleNamespace(
            encoding_for_model=lambda model: (_ for _ in ()).throw(KeyError(model)),
            get_encoding=lambda name: FakeEncoding(),
        )
        monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)
        _clear_tokenizer_cache_for_tests()
        try:
            estimate = estimate_tokens_with_source(
                "one two three", model="gpt-5.5",
            )
            assert estimate.tokens == 3
            assert estimate.source == "tiktoken:o200k_base:fallback"
            assert estimate.exact is False
        finally:
            _clear_tokenizer_cache_for_tests()

    def test_model_estimator_uses_tiktoken_as_heuristic_for_claude(
        self, monkeypatch,
    ) -> None:
        class FakeEncoding:
            name = "fake_base"

            def encode(self, text: str) -> list[int]:
                return list(range(len(text.split())))

        fake_tiktoken = SimpleNamespace(
            encoding_for_model=lambda model: FakeEncoding(),
            get_encoding=lambda name: FakeEncoding(),
        )
        monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)
        _clear_tokenizer_cache_for_tests()
        try:
            estimate = estimate_tokens_with_source(
                "one two three", model="claude-sonnet-4-6",
            )
            assert estimate.tokens == 3
            assert estimate.source == "tiktoken:fake_base:heuristic"
            assert estimate.exact is False
        finally:
            _clear_tokenizer_cache_for_tests()

    def test_model_estimator_falls_back_when_tiktoken_encoding_unavailable(
        self, monkeypatch,
    ) -> None:
        fake_tiktoken = SimpleNamespace(
            encoding_for_model=lambda model: (_ for _ in ()).throw(KeyError(model)),
            get_encoding=lambda name: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)
        _clear_tokenizer_cache_for_tests()
        try:
            estimate = estimate_tokens_with_source("x" * 40, model="gpt-5.5")
            assert estimate.tokens == 10
            assert estimate.source == "byte_heuristic"
            assert estimate.exact is False
        finally:
            _clear_tokenizer_cache_for_tests()


# ─────────────────────────────────────────────────────────────────────────────
# PhaseMetrics
# ─────────────────────────────────────────────────────────────────────────────

class TestPhaseMetrics:
    def test_total_tokens(self) -> None:
        pm = PhaseMetrics(phase="plan", model="m", tokens_in=100, tokens_out=200)
        assert pm.total_tokens == 300

    def test_as_dict_basic(self) -> None:
        pm = PhaseMetrics(phase="implement", model="claude", tokens_in=500, tokens_out=1000, duration_s=12.5)
        d = pm.as_dict()
        assert d["tokens_in"] == 500
        assert d["tokens_out"] == 1000
        assert d["total_tokens"] == 1500
        assert d["duration_s"] == 12.5
        assert d["model"] == "claude"

    def test_retries_omitted_when_zero(self) -> None:
        pm = PhaseMetrics(phase="plan", model="m", retries=0)
        assert "retries" not in pm.as_dict()

    def test_retries_included_when_nonzero(self) -> None:
        pm = PhaseMetrics(phase="implement", model="m", retries=2)
        assert pm.as_dict()["retries"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# MetricsCollector
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricsCollector:
    def test_empty_collector(self) -> None:
        m = MetricsCollector()
        assert m.total_tokens == 0
        assert m.total_duration_s == 0.0
        assert m.total_rounds == 0
        assert m.total_retries == 0
        assert m.phases == []

    def test_record_phase_estimates_tokens(self) -> None:
        m = MetricsCollector()
        pm = m.record_phase("plan", prompt="a" * 400, output="b" * 800, duration_s=10.0)
        assert pm.tokens_in > 0
        assert pm.tokens_out > 0
        assert pm.duration_s == 10.0

    def test_record_phase_explicit_tokens(self) -> None:
        m = MetricsCollector()
        pm = m.record_phase("implement", tokens_in=5000, tokens_out=12000, duration_s=25.0)
        assert pm.tokens_in == 5000
        assert pm.tokens_out == 12000

    def test_record_phase_total_only_keeps_split_unknown(self) -> None:
        m = MetricsCollector()
        pm = m.record_phase(
            "validate_plan", tokens_total=258_247, duration_s=3.0,
        )
        assert pm.tokens_in == 0
        assert pm.tokens_out == 0
        assert pm.tokens_unknown == 258_247
        assert pm.total_tokens == 258_247
        assert pm.as_dict()["tokens_unknown"] == 258_247

    def test_aggregates_sum_correctly(self) -> None:
        m = MetricsCollector()
        m.record_phase("plan",   tokens_in=1000, tokens_out=2000, duration_s=10.0)
        m.record_phase("implement",  tokens_in=5000, tokens_out=8000, duration_s=30.0)
        m.record_phase("review_changes", tokens_in=500,  tokens_out=600,  duration_s=5.0)
        assert m.total_tokens_in  == 6500
        assert m.total_tokens_out == 10600
        assert m.total_tokens     == 17100
        assert m.total_duration_s == 45.0

    def test_add_round(self) -> None:
        m = MetricsCollector()
        m.add_round()
        m.add_round()
        assert m.total_rounds == 2

    def test_retries_accumulated(self) -> None:
        m = MetricsCollector()
        m.record_phase("implement",  tokens_in=0, tokens_out=0, retries=2)
        m.record_phase("review_changes", tokens_in=0, tokens_out=0, retries=1)
        assert m.total_retries == 3

    def test_default_model_applied(self) -> None:
        m = MetricsCollector(default_model="claude-opus-4-7")
        pm = m.record_phase("plan", prompt="x")
        assert pm.model == "claude-opus-4-7"

    def test_explicit_model_overrides_default(self) -> None:
        m = MetricsCollector(default_model="claude-opus-4-7")
        pm = m.record_phase("review_changes", model="gpt-5.5", prompt="x")
        assert pm.model == "gpt-5.5"

    def test_as_dict_schema(self) -> None:
        m = MetricsCollector()
        m.record_phase("plan", tokens_in=1000, tokens_out=2000, duration_s=5.0)
        m.add_round()
        d = m.as_dict()
        assert "total_tokens_in"  in d
        assert "total_tokens_out" in d
        assert "total_tokens"     in d
        assert "total_duration_s" in d
        assert "phases"           in d
        assert "plan"             in d["phases"]
        assert "phase_attempts"   in d
        assert "total_rounds"     in d  # present because rounds > 0

    def test_record_phase_runtime_id_written_to_phase_rollup(self) -> None:
        # A phase that ran under a wrapper runtime (e.g. claude-glm) carries
        # that resolved runtime id into the phase rollup, so cost aggregation
        # can attribute it to the runtime rather than the base model.
        m = MetricsCollector()
        m.record_phase(
            "implement",
            model="claude-sonnet-4-6",
            runtime="claude-glm",
            tokens_in=5000,
            tokens_out=8000,
            duration_s=25.0,
        )
        rollup = m.as_dict()["phases"]["implement"]
        assert rollup["runtime"] == "claude-glm"

    def test_record_phase_without_runtime_omits_runtime_key(self) -> None:
        # Legacy shape: no explicit runtime and no default_runtime → the phase
        # rollup must not carry a 'runtime' key at all (older metrics.json).
        m = MetricsCollector()
        m.record_phase(
            "implement",
            model="claude-sonnet-4-6",
            tokens_in=5000,
            tokens_out=8000,
            duration_s=25.0,
        )
        rollup = m.as_dict()["phases"]["implement"]
        assert "runtime" not in rollup

    def test_runtime_id_survives_load_from_disk_roundtrip(self, tmp_path) -> None:
        # Resume path: a phase that ran under 'claude-glm' saves metrics.json,
        # a fresh collector rehydrates it, and re-serialising must keep the
        # runtime id in both phase_attempts and the phase rollup. Without the
        # rehydrate carry-through, sdk/cost would fall back to model→provider
        # bucketing and collapse claude-glm into claude for resumed runs.
        pre = MetricsCollector()
        pre.record_phase(
            "implement",
            model="claude-sonnet-4-6",
            runtime="claude-glm",
            tokens_in=5000,
            tokens_out=8000,
            duration_s=25.0,
        )
        pre.save(tmp_path)

        resumed = MetricsCollector()
        resumed.load_from_disk(tmp_path / "metrics.json")
        out = resumed.as_dict()
        assert out["phases"]["implement"]["runtime"] == "claude-glm"
        assert out["phase_attempts"][0]["runtime"] == "claude-glm"

    def test_repeated_phase_attempts_are_preserved_and_rolled_up(
        self, accounting_on,
    ) -> None:
        m = MetricsCollector()
        m.record_phase(
            "plan", tokens_in=100, tokens_out=20, duration_s=1.0,
            tool_calls=2, cost_usd=0.01,
        )
        m.record_phase(
            "plan", tokens_in=300, tokens_out=40, duration_s=2.0,
            tool_calls=1, cost_usd=0.03,
        )

        d = m.as_dict()
        assert d["phases"]["plan"]["attempts"] == 2
        assert d["phases"]["plan"]["tokens_in"] == 400
        assert d["phases"]["plan"]["tokens_out"] == 60
        assert d["phases"]["plan"]["tool_calls"] == 3
        assert d["phases"]["plan"]["cost_usd_equivalent"] == 0.04
        assert d["phases"]["plan"]["cost_estimated"] is False
        assert [
            (item["phase"], item["attempt"], item["tokens_in"])
            for item in d["phase_attempts"]
        ] == [
            ("plan", 1, 100),
            ("plan", 2, 300),
        ]

    def _subtask_records(self) -> list[dict]:
        return [
            {
                "subtask_id": "T1",
                "runtime": "claude",
                "model": "m",
                "invocations": 1,
                "duration_s": 1.0,
                "tokens_in": 1000,
                "tokens_out": 100,
                "total_tokens": 1100,
                "tool_calls": 2,
                "tokens_exact": True,
                "cost_usd_equivalent": 0.02,
                "state": "done",
                "declared_files": ["a.py"],
            },
            {
                "subtask_id": "T2",
                "runtime": "claude",
                "model": "m",
                "invocations": 1,
                "duration_s": 2.0,
                "tokens_in": 2000,
                "tokens_out": 200,
                "total_tokens": 2200,
                "tool_calls": 1,
                "tokens_exact": True,
                "cost_usd_equivalent": 0.04,
                "state": "done",
            },
        ]

    def test_record_subtask_usage_is_additive_and_does_not_change_totals(
        self, accounting_on,
    ) -> None:
        m = MetricsCollector()
        m.record_phase(
            "implement", tokens_in=3000, tokens_out=300, duration_s=3.0,
            tool_calls=3, cost_usd=0.06,
        )
        before = m.as_dict()
        m.record_subtask_usage("implement", self._subtask_records())
        after = m.as_dict()

        # Additive: the breakdown appears under ``subtasks`` ...
        assert after["subtasks"]["implement"][0]["subtask_id"] == "T1"
        assert [r["subtask_id"] for r in after["subtasks"]["implement"]] == [
            "T1", "T2",
        ]
        # ... and every existing total / phase rollup is byte-for-byte
        # unchanged (no double counting).
        assert after["total_tokens"] == before["total_tokens"]
        assert after["total_tokens_in"] == before["total_tokens_in"]
        assert after["phases"]["implement"] == before["phases"]["implement"]
        assert "subtasks" not in before

    def test_subtask_usage_sums_reconcile_with_phase_total(
        self, accounting_on,
    ) -> None:
        m = MetricsCollector()
        records = self._subtask_records()
        m.record_subtask_usage("implement", records)
        d = m.as_dict()
        rows = d["subtasks"]["implement"]
        # The records' token/cost sums equal the phase total they explain.
        assert sum(r["total_tokens"] for r in rows) == 3300
        assert round(sum(r["cost_usd_equivalent"] for r in rows), 4) == 0.06

    def test_no_subtask_usage_means_no_subtasks_key(self) -> None:
        m = MetricsCollector()
        m.record_phase("implement", tokens_in=10, tokens_out=2)
        assert "subtasks" not in m.as_dict()

    def test_record_subtask_usage_merges_by_subtask_id(self) -> None:
        # A second call for the same phase MERGES by subtask_id: matching ids
        # accumulate (numeric summed, bool AND-ed, other latest-wins) and
        # untouched ids are preserved — never a wholesale replace.
        m = MetricsCollector()
        m.record_subtask_usage("implement", [
            {"subtask_id": "T1", "total_tokens": 1000, "invocations": 1,
             "tokens_exact": True, "state": "incomplete"},
            {"subtask_id": "T2", "total_tokens": 500, "invocations": 1,
             "tokens_exact": True, "state": "done"},
        ])
        m.record_subtask_usage("implement", [
            {"subtask_id": "T1", "total_tokens": 200, "invocations": 1,
             "tokens_exact": False, "state": "done"},
        ])
        rows = {r["subtask_id"]: r for r in m.as_dict()["subtasks"]["implement"]}
        # T2 (not rerun) preserved untouched.
        assert rows["T2"]["total_tokens"] == 500
        # T1 accumulated: tokens summed, invocations summed, exactness AND-ed,
        # final state from the rerun.
        assert rows["T1"]["total_tokens"] == 1200
        assert rows["T1"]["invocations"] == 2
        assert rows["T1"]["tokens_exact"] is False
        assert rows["T1"]["state"] == "done"
        # Order: existing ids first (T1, T2), no duplicate T1.
        assert [r["subtask_id"] for r in m.as_dict()["subtasks"]["implement"]] == [
            "T1", "T2",
        ]

    def test_partial_retry_resume_preserves_all_subtasks_and_reconciles(
        self, accounting_on, tmp_path: Path,
    ) -> None:
        # F1 regression: handoff pause → resume with implement_retry re-emits
        # ONLY the rerun subtask. The saved breakdown (both subtasks) must
        # survive, and sums must stay reconciled with the cumulative
        # phases.implement rollup (which also accumulates pre-pause + retry).
        pre = MetricsCollector()
        pre.record_phase("implement", tokens_in=3000, tokens_out=300, cost_usd=0.06)
        pre.record_subtask_usage("implement", [
            {"subtask_id": "T1", "runtime": "claude", "model": "m",
             "invocations": 1, "duration_s": 1.0, "tokens_in": 1000,
             "tokens_out": 100, "total_tokens": 1100, "tool_calls": 1,
             "tokens_exact": True, "cost_usd_equivalent": 0.02,
             "state": "incomplete"},
            {"subtask_id": "T2", "runtime": "claude", "model": "m",
             "invocations": 1, "duration_s": 2.0, "tokens_in": 2000,
             "tokens_out": 200, "total_tokens": 2200, "tool_calls": 2,
             "tokens_exact": True, "cost_usd_equivalent": 0.04,
             "state": "done"},
        ])
        pre.save(tmp_path)

        # Resume subprocess: rehydrate, then the retry reruns ONLY T1.
        resumed = MetricsCollector()
        resumed.load_from_disk(tmp_path / "metrics.json")
        resumed.record_phase(
            "implement", tokens_in=500, tokens_out=50, cost_usd=0.01,
        )
        resumed.record_subtask_usage("implement", [
            {"subtask_id": "T1", "runtime": "claude", "model": "m",
             "invocations": 1, "duration_s": 0.5, "tokens_in": 500,
             "tokens_out": 50, "total_tokens": 550, "tool_calls": 1,
             "tokens_exact": True, "cost_usd_equivalent": 0.01,
             "state": "done"},
        ])

        d = resumed.as_dict()
        rows = {r["subtask_id"]: r for r in d["subtasks"]["implement"]}
        # Both subtasks survive; T2 was NOT lost.
        assert set(rows) == {"T1", "T2"}
        # T1 cumulated across pre-pause + retry; final state is the rerun's.
        assert rows["T1"]["total_tokens"] == 1650
        assert rows["T1"]["invocations"] == 2
        assert rows["T1"]["state"] == "done"
        assert rows["T2"]["total_tokens"] == 2200
        # Sums reconcile with the cumulative phase rollup.
        impl = d["phases"]["implement"]
        assert sum(r["total_tokens"] for r in rows.values()) == impl["total_tokens"]
        assert round(
            sum(r["cost_usd_equivalent"] for r in rows.values()), 4
        ) == round(impl["cost_usd_equivalent"], 4)

    def test_subtask_usage_round_trips_through_disk(
        self, accounting_on, tmp_path: Path,
    ) -> None:
        m = MetricsCollector()
        m.record_phase("implement", tokens_in=3000, tokens_out=300)
        m.record_subtask_usage("implement", self._subtask_records())
        m.save(tmp_path)

        # A fresh subprocess-style collector rehydrates the breakdown, so a
        # handoff pause → resume → final save does not lose it.
        resumed = MetricsCollector()
        resumed.load_from_disk(tmp_path / "metrics.json")
        d = resumed.as_dict()
        assert [r["subtask_id"] for r in d["subtasks"]["implement"]] == [
            "T1", "T2",
        ]
        assert d["subtasks"]["implement"][0]["state"] == "done"

    def test_load_from_disk_ignores_malformed_subtasks(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "metrics.json"
        path.write_text(json.dumps({
            "phase_attempts": [],
            "subtasks": {
                "implement": ["not-a-dict", {"subtask_id": "ok"}],
                "bogus": "not-a-list",
            },
        }))
        m = MetricsCollector()
        m.load_from_disk(path)
        d = m.as_dict()
        # Only the well-formed record under a valid phase survives; the
        # non-list ``bogus`` phase and the non-dict record are dropped.
        assert d["subtasks"] == {"implement": [{"subtask_id": "ok"}]}

    def test_subtask_cost_scrubbed_when_accounting_disabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.infra import config
        monkeypatch.delenv("ORCHO_ACCOUNTING", raising=False)
        config._reset_config()
        try:
            m = MetricsCollector()
            m.record_subtask_usage("implement", self._subtask_records())
            rows = m.as_dict()["subtasks"]["implement"]
            assert all("cost_usd_equivalent" not in r for r in rows)
            # Non-cost fields survive the scrub.
            assert rows[0]["total_tokens"] == 1100
        finally:
            config._reset_config()

    def test_accounting_disabled_omits_reported_cost(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.infra import config
        monkeypatch.delenv("ORCHO_ACCOUNTING", raising=False)
        config._reset_config()
        try:
            m = MetricsCollector()
            m.record_phase("plan", tokens_in=100, tokens_out=20, cost_usd=0.10)
            d = m.as_dict()
            assert "total_cost_usd_equivalent" not in d
            assert "cost_usd_equivalent" not in d["phases"]["plan"]
            assert "Cost ref" not in m.summary_line()
        finally:
            config._reset_config()

    def test_exact_tokens_without_reported_cost_get_estimated_cost_ref(
        self,
        accounting_on,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.observability import pricing

        monkeypatch.setattr(
            pricing,
            "estimate_cost_usd",
            lambda model, *, tokens_in, tokens_out, cached_tokens_in=0: (
                tokens_in * 0.001 + tokens_out * 0.002
            ),
        )
        m = MetricsCollector(default_model="gpt-5.5")

        pm = m.record_phase(
            "review_changes",
            tokens_in=100,
            tokens_out=25,
            cost_usd=None,
        )

        assert pm.cost_usd_equivalent == pytest.approx(0.15)
        assert pm.cost_estimated is True
        d = m.as_dict()
        assert d["total_cost_usd_equivalent"] == 0.15
        assert d["cost_estimated"] is True
        assert d["phases"]["review_changes"]["cost_estimated"] is True
        assert d["phase_attempts"][0]["cost_estimated"] is True
        assert "Cost ref: estimated-api ~$0.15" in m.summary_line()

    def test_total_only_tokens_without_reported_cost_use_total_estimate(
        self,
        accounting_on,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.observability import pricing

        monkeypatch.setattr(
            pricing,
            "estimate_cost_from_total",
            lambda model, tokens_total: tokens_total * 0.01,
        )
        m = MetricsCollector(default_model="gpt-5.5")

        pm = m.record_phase("plan", tokens_total=12, cost_usd=None)

        assert pm.tokens_unknown == 12
        assert pm.cost_usd_equivalent == pytest.approx(0.12)
        assert pm.cost_estimated is True

    def test_heuristic_tokens_do_not_get_dollar_estimate(
        self,
        accounting_on,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.observability import pricing

        def fail_estimate(*args, **kwargs):
            raise AssertionError("heuristic tokens must not be priced")

        monkeypatch.setattr(pricing, "estimate_cost_usd", fail_estimate)
        monkeypatch.setattr(pricing, "estimate_cost_from_total", fail_estimate)
        m = MetricsCollector(default_model="gpt-5.5")

        pm = m.record_phase("plan", prompt="hello", output="world")

        assert pm.tokens_exact is False
        assert pm.cost_usd_equivalent is None
        assert pm.cost_estimated is False

    def test_rounds_omitted_when_zero(self) -> None:
        m = MetricsCollector()
        m.record_phase("plan", tokens_in=10, tokens_out=10)
        d = m.as_dict()
        assert "total_rounds"   not in d
        assert "total_retries"  not in d

    def test_retries_omitted_when_zero(self) -> None:
        m = MetricsCollector()
        d = m.as_dict()
        assert "total_retries" not in d


# ─────────────────────────────────────────────────────────────────────────────
# save() → metrics.json
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricsSave:
    def test_save_writes_json(self, tmp_path: Path) -> None:
        m = MetricsCollector()
        m.record_phase("plan",  tokens_in=1000, tokens_out=2000, duration_s=10.0, model="claude-opus-4-7")
        m.record_phase("implement", tokens_in=5000, tokens_out=8000, duration_s=30.0, model="claude-sonnet-4-6")
        f = m.save(tmp_path)

        assert f.name == "metrics.json"
        data = json.loads(f.read_text())
        assert data["total_tokens"] == 16000
        assert data["total_duration_s"] == 40.0
        assert "plan" in data["phases"]
        assert "implement" in data["phases"]

    def test_save_creates_dir(self, tmp_path: Path) -> None:
        m = MetricsCollector()
        m.record_phase("plan", tokens_in=100, tokens_out=200, duration_s=1.0)
        nested = tmp_path / "deep" / "nested"
        f = m.save(nested)
        assert f.exists()

    def test_json_valid(self, tmp_path: Path) -> None:
        m = MetricsCollector()
        m.record_phase("plan", tokens_in=100, tokens_out=200, duration_s=1.0)
        m.record_phase("implement", tokens_in=500, tokens_out=800, duration_s=5.0, retries=1)
        m.add_round()
        f = m.save(tmp_path)
        data = json.loads(f.read_text())
        # Verify all required keys are present
        for key in ("total_tokens_in", "total_tokens_out", "total_tokens", "total_duration_s", "phases"):
            assert key in data, f"Missing key: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# load_from_disk — resume continuity for cross-subprocess metrics aggregation
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricsLoadFromDisk:
    def test_roundtrip_preserves_attempts(self, tmp_path: Path) -> None:
        # The resume subprocess starts with an empty MetricsCollector
        # and rehydrates from the prior subprocess's snapshot. Every
        # attempt that ran before the pause must survive intact.
        src = MetricsCollector()
        src.record_phase(
            "plan", tokens_in=10, tokens_out=20, duration_s=0.5,
            model="m1", tool_calls=2,
        )
        src.record_phase(
            "plan", tokens_in=15, tokens_out=25, duration_s=0.7,
            model="m1", attempt=2, retries=1,
        )
        src.record_phase(
            "validate_plan", tokens_in=5, tokens_out=8, duration_s=0.1,
            model="m2",
        )
        src.add_round(3)
        src.save(tmp_path)

        dst = MetricsCollector()
        loaded = dst.load_from_disk(tmp_path / "metrics.json")
        assert loaded == 3
        # Attempts survive in order.
        ph = dst.phases
        assert [(p.phase, p.attempt) for p in ph] == [
            ("plan", 1), ("plan", 2), ("validate_plan", 1),
        ]
        # Roundtrip preserves the per-attempt scalar fields.
        assert ph[0].tokens_in == 10 and ph[0].tokens_out == 20
        assert ph[0].tool_calls == 2 and ph[0].model == "m1"
        assert ph[1].retries == 1
        assert ph[2].model == "m2"
        # Aggregates and rounds are recomputed correctly.
        assert dst.total_tokens_in == 30
        assert dst.total_tokens_out == 53
        assert dst.total_rounds == 3
        assert dst.total_retries == 1

    def test_load_then_record_extends_not_replaces(
        self, tmp_path: Path,
    ) -> None:
        # The cross-subprocess invariant: after load_from_disk the
        # collector behaves as if all the prior attempts were recorded
        # in-process, and subsequent ``record_phase`` calls extend the
        # list rather than restart attempt numbering for a phase.
        src = MetricsCollector()
        src.record_phase("plan", tokens_in=10, tokens_out=20, duration_s=0.1)
        src.record_phase(
            "plan", tokens_in=11, tokens_out=21, duration_s=0.1, attempt=2,
        )
        src.save(tmp_path)

        dst = MetricsCollector()
        dst.load_from_disk(tmp_path / "metrics.json")
        # Next plan attempt auto-numbered as 3 (continues the prior run).
        new_pm = dst.record_phase(
            "plan", tokens_in=12, tokens_out=22, duration_s=0.1,
        )
        assert new_pm.attempt == 3
        assert len(dst.phases) == 3
        assert dst.total_tokens_in == 33

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        m = MetricsCollector()
        assert m.load_from_disk(tmp_path / "absent.json") == 0
        assert m.phases == []

    def test_malformed_file_returns_zero_without_raising(
        self, tmp_path: Path,
    ) -> None:
        bad = tmp_path / "metrics.json"
        bad.write_text("{not json", encoding="utf-8")
        m = MetricsCollector()
        assert m.load_from_disk(bad) == 0

    def test_malformed_attempt_entry_skipped(
        self, tmp_path: Path,
    ) -> None:
        # An entry missing a required field is silently skipped; valid
        # siblings still load. The collector never raises during resume.
        path = tmp_path / "metrics.json"
        path.write_text(json.dumps({
            "phase_attempts": [
                {"attempt": 1, "tokens_in": 10},  # no ``phase`` — drop
                {"phase": "plan", "attempt": 1, "tokens_in": 5,
                 "tokens_out": 7, "duration_s": 0.1},
            ],
        }), encoding="utf-8")
        m = MetricsCollector()
        assert m.load_from_disk(path) == 1
        assert [p.phase for p in m.phases] == ["plan"]


# ─────────────────────────────────────────────────────────────────────────────
# summary_line / summary_table
# ─────────────────────────────────────────────────────────────────────────────

class TestSummaryFormatting:
    def test_summary_line_basic(self) -> None:
        m = MetricsCollector()
        m.record_phase("plan", tokens_in=1000, tokens_out=2000, duration_s=10.0)
        line = m.summary_line()
        assert "3,000" in line     # total tokens
        assert "10.0s" in line

    def test_summary_line_total_only_uses_unknown_bucket(self) -> None:
        m = MetricsCollector()
        m.record_phase("plan", tokens_in=1000, tokens_out=2000, duration_s=10.0)
        m.record_phase("validate_plan", tokens_total=258_247, duration_s=3.0)
        line = m.summary_line()
        assert "Tokens: 261,247" in line
        assert "(in=1,000 out=2,000 unknown=258,247)" in line

    def test_summary_line_with_rounds(self) -> None:
        m = MetricsCollector()
        m.add_round(2)
        line = m.summary_line()
        assert "Rounds: 2" in line

    def test_summary_line_no_rounds_omitted(self) -> None:
        m = MetricsCollector()
        line = m.summary_line()
        assert "Rounds" not in line

    def test_summary_table_empty(self) -> None:
        m = MetricsCollector()
        assert "No phases" in m.summary_table()

    def test_summary_table_has_phases(self) -> None:
        m = MetricsCollector()
        m.record_phase(
            "plan", tokens_in=1000, tokens_out=2000,
            duration_s=5.0, model="opus", tool_calls=2,
        )
        m.record_phase("implement", tokens_in=5000, tokens_out=8000, duration_s=30.0, model="sonnet")
        table = m.summary_table()
        assert "plan" in table
        assert "implement" in table
        assert "Tools" in table
        assert "2" in table
        assert "TOTAL" in table


# ─────────────────────────────────────────────────────────────────────────────
# Cross-project rollup
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossRollup:
    """Aggregate per-sub-pipeline metrics.json dicts into one cross-run view."""

    @pytest.fixture(autouse=True)
    def _enable_accounting(self, accounting_on) -> None:
        pass

    def _metrics(
        self, *,
        tin: int, tout: int, dur: float,
        rounds: int = 0, cost: float | None = None,
    ) -> dict:
        d: dict = {
            "total_tokens_in":  tin,
            "total_tokens_out": tout,
            "total_tokens":     tin + tout,
            "total_duration_s": dur,
        }
        if rounds:
            d["total_rounds"] = rounds
        if cost is not None:
            d["total_cost_usd_equivalent"] = cost
        return d

    def test_empty_input_returns_marker(self) -> None:
        assert cross_summary_table({}) == "No metrics recorded."

    def test_table_sums_each_column(self) -> None:
        per = {
            "api": self._metrics(tin=1000, tout=500, dur=30.0, rounds=2, cost=0.10),
            "web": self._metrics(tin=200,  tout=300, dur=15.5, rounds=1, cost=0.05),
        }
        out = cross_summary_table(per)
        # Per-project rows present.
        assert "api" in out and "1,000" in out and "500" in out
        assert "web" in out and "200" in out and "300" in out
        # TOTAL row sums correctly.
        assert "TOTAL" in out
        assert "1,200" in out          # 1000 + 200
        assert "800" in out            # 500 + 300
        assert "2,000" in out          # 1500 + 500
        assert "45.5s" in out          # 30.0 + 15.5
        assert "runtime-reported $0.15" in out          # 0.10 + 0.05

    def test_table_omits_cost_column_when_no_phase_reported(self) -> None:
        per = {"api": self._metrics(tin=100, tout=200, dur=5.0)}
        out = cross_summary_table(per)
        assert "Cost ref" not in out
        assert "TOTAL" in out

    def test_line_aggregates_totals_and_cost(self) -> None:
        per = {
            "api": self._metrics(tin=10, tout=20, dur=1.0, cost=0.01),
            "web": self._metrics(tin=30, tout=40, dur=2.0, cost=0.02),
        }
        line = cross_summary_line(per)
        assert "Tokens: 100" in line       # (10+20)+(30+40)
        assert "in=40"  in line
        assert "out=60" in line
        assert "Time: 3.0s" in line
        assert "Cost ref: runtime-reported $0.03" in line

    def test_tolerates_missing_or_empty_per_project_dicts(self) -> None:
        per = {"api": {}, "web": self._metrics(tin=1, tout=2, dur=0.5)}
        out = cross_summary_table(per)
        assert "api" in out and "web" in out

    def test_cross_metrics_dict_shape_matches_single_run(self) -> None:
        """Cross ``metrics.json`` must surface the same top-level keys as a
        single-project run so ``load_historical_runs`` / ``orcho metrics`` /
        ``orcho evidence`` see it without per-reader branching.
        """
        per = {
            "api": self._metrics(tin=1000, tout=500, dur=30.0, rounds=2, cost=0.10),
            "web": self._metrics(tin=200,  tout=300, dur=15.5, rounds=1, cost=0.05),
        }
        d = cross_metrics_dict(per)
        # Top-level totals match MetricsCollector.as_dict shape.
        assert d["total_tokens_in"]  == 1200
        assert d["total_tokens_out"] == 800
        assert d["total_tokens"]     == 2000
        assert d["total_duration_s"] == 45.5
        assert d["total_rounds"]     == 3
        assert d["total_cost_usd_equivalent"] == 0.15
        # Per-project phases populated (one entry per sub-pipeline).
        assert set(d["phases"].keys()) == {"api", "web"}
        assert d["phases"]["api"]["tokens_in"] == 1000
        # Aggregation marker disambiguates a cross from a single run.
        assert d["cross_aggregation"]["sub_pipelines"] == ["api", "web"]
        # Cross-level phases are surfaced separately; when no cross_phases
        # are provided, the list is empty.
        assert d["cross_aggregation"]["cross_phases"] == []

    def test_cross_metrics_dict_omits_cost_when_no_phase_reports(self) -> None:
        per = {"api": self._metrics(tin=100, tout=200, dur=5.0)}
        d = cross_metrics_dict(per)
        assert "total_cost_usd_equivalent" not in d
        assert d["total_tokens"] == 300

    def test_cross_phases_fold_into_total_alongside_sub_pipelines(self) -> None:
        """Cross-level phases (cross_plan, cross_validate_plan, contract_check,
        cross_hypothesis) must show up as rows AND contribute to the TOTAL,
        not be dropped on the floor like before.
        """
        per = {
            "api": self._metrics(tin=1000, tout=500, dur=30.0, cost=0.10),
            "web": self._metrics(tin=200,  tout=300, dur=15.5, cost=0.05),
        }
        cross_phases = {
            "cross_plan":          {
                "tokens_in": 5000, "tokens_out": 800, "total_tokens": 5800,
                "duration_s": 12.0, "calls": 1, "cost_usd_equivalent": 0.07,
            },
            "cross_validate_plan": {
                "tokens_in": 4500, "tokens_out": 600, "total_tokens": 5100,
                "duration_s": 8.0, "calls": 1, "cost_usd_equivalent": 0.04,
            },
            "contract_check":      {
                "tokens_in": 3000, "tokens_out": 400, "total_tokens": 3400,
                "duration_s": 6.0, "calls": 2, "cost_usd_equivalent": 0.03,
            },
        }
        d = cross_metrics_dict(per, cross_phases)
        # Sub-pipeline + cross-level rows present in phases dict.
        assert set(d["phases"].keys()) == {
            "api", "web",
            "cross_plan", "cross_validate_plan", "contract_check",
        }
        assert d["phases"]["api"]["kind"] == "sub_pipeline"
        assert d["phases"]["cross_plan"]["kind"] == "cross_level"
        assert d["phases"]["contract_check"]["calls"] == 2
        # Totals include both sources.
        assert d["total_tokens_in"]  == 1000 + 200 + 5000 + 4500 + 3000
        assert d["total_tokens_out"] == 500 + 300 + 800 + 600 + 400
        assert d["total_tokens"]     == 1500 + 500 + 5800 + 5100 + 3400
        assert d["total_duration_s"] == 71.5
        assert d["total_cost_usd_equivalent"] == round(0.10 + 0.05 + 0.07 + 0.04 + 0.03, 4)
        assert d["cross_aggregation"]["cross_phases"] == [
            "cross_plan", "cross_validate_plan", "contract_check",
        ]

    def test_cross_summary_table_renders_both_sections(self) -> None:
        per = {"api": self._metrics(tin=1000, tout=500, dur=30.0, cost=0.10)}
        cross_phases = {
            "cross_plan": {
                "tokens_in": 5000, "tokens_out": 800, "total_tokens": 5800,
                "duration_s": 12.0, "calls": 1, "cost_usd_equivalent": 0.07,
            },
        }
        out = cross_summary_table(per, cross_phases)
        assert "Sub-pipelines:" in out
        assert "Cross-level phases:" in out
        assert "[api]" in out
        assert "cross_plan" in out
        # TOTAL line sums both sources.
        assert "6,000" in out         # 1000 + 5000
        assert "TOTAL" in out
        assert "$0.17" in out         # 0.10 + 0.07


# ─────────────────────────────────────────────────────────────────────────────
# load_historical_runs / format_history_table
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoricalRuns:
    def _make_run(self, runs_dir: Path, run_id: str, tokens: int, duration: float) -> None:
        """Create a fake run directory with meta.json + metrics.json."""
        d = runs_dir / run_id
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({
            "task": f"Task for {run_id}",
            "project": f"/projects/{run_id}",
            "timestamp": f"2026-05-02T10:{run_id[-2:]}:00",
        }))
        (d / "metrics.json").write_text(json.dumps({
            "total_tokens": tokens,
            "total_tokens_in": tokens // 3,
            "total_tokens_out": tokens - tokens // 3,
            "total_duration_s": duration,
            "phases": {},
        }))

    def _make_cross_run(self, runs_dir: Path, run_id: str, aliases: list[str]) -> None:
        """Create a fake cross-project run directory.

        Cross-project meta.json carries ``projects`` (dict of alias→path)
        instead of a single ``project`` string; the loader must render
        that as ``cross[alias1,alias2]`` rather than ``?``.
        """
        d = runs_dir / run_id
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({
            "task": f"Cross task for {run_id}",
            "projects": {a: f"/projects/{a}" for a in aliases},
            "timestamp": f"2026-05-02T11:{run_id[-2:]}:00",
        }))
        (d / "metrics.json").write_text(json.dumps({
            "total_tokens": 1000,
            "total_tokens_in": 600,
            "total_tokens_out": 400,
            "total_duration_s": 12.5,
            "phases": {},
        }))

    def test_cross_run_project_label_lists_aliases(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        self._make_cross_run(runs, "20260513_195842", aliases=["api", "web"])
        result = load_historical_runs(runs)
        assert len(result) == 1
        # Was reporting "?" before — now lists involved sub-project aliases.
        assert result[0].project == "cross[api,web]"

    def test_empty_runs_dir(self, tmp_path: Path) -> None:
        result = load_historical_runs(tmp_path / "nonexistent")
        assert result == []

    def test_load_single_run(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        self._make_run(runs, "20260502_100000", tokens=41000, duration=142.3)
        result = load_historical_runs(runs)
        assert len(result) == 1
        assert result[0].run_id == "20260502_100000"
        assert result[0].total_tokens == 41000
        assert result[0].total_duration_s == 142.3

    def test_sorted_newest_first(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        self._make_run(runs, "20260501_000000", tokens=1000, duration=10.0)
        self._make_run(runs, "20260502_000000", tokens=2000, duration=20.0)
        self._make_run(runs, "20260503_000000", tokens=3000, duration=30.0)
        result = load_historical_runs(runs)
        assert result[0].run_id == "20260503_000000"  # newest first
        assert result[-1].run_id == "20260501_000000"

    def test_last_n_limit(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        for i in range(5):
            self._make_run(runs, f"2026050{i}_000000", tokens=i * 100, duration=float(i))
        result = load_historical_runs(runs, last_n=3)
        assert len(result) == 3

    def test_malformed_run_skipped(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        # Bad run: metrics.json exists but is invalid JSON
        bad = runs / "20260501_bad"
        bad.mkdir()
        (bad / "meta.json").write_text("{}")
        (bad / "metrics.json").write_text("NOT JSON")
        # Good run
        self._make_run(runs, "20260502_good", tokens=1000, duration=5.0)
        result = load_historical_runs(runs)
        assert len(result) == 1
        assert result[0].run_id == "20260502_good"

    def test_format_history_table_empty(self) -> None:
        assert "No historical" in format_history_table([])

    def test_format_history_table_has_run_ids(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        self._make_run(runs, "20260502_100000", tokens=41000, duration=142.3)
        summaries = load_historical_runs(runs)
        table = format_history_table(summaries)
        assert "20260502_100000" in table
        assert "41,000" in table


# ─────────────────────────────────────────────────────────────────────────────
# T4 — observe-only handoff-advice usage attribution
#
# The advisor runs OUTSIDE the FSM phase loop. Its usage is surfaced as an
# additive, observe-only ``handoff_advice`` slot fed by the upper layer via
# ``record_advice_usage`` — NEVER folded into ``total_*`` / ``phases`` (no
# double counting), and ``core.observability.metrics`` stays unaware of
# ``pipeline.project``.
# ─────────────────────────────────────────────────────────────────────────────


class TestAdviceUsage:
    def test_record_advice_usage_additive_and_does_not_change_totals(
        self, accounting_on,
    ) -> None:
        m = MetricsCollector()
        m.record_phase(
            "review_changes", tokens_in=3000, tokens_out=300, duration_s=3.0,
            cost_usd=0.06,
        )
        before = m.as_dict()
        m.record_advice_usage(
            {"tokens_in": 800, "tokens_out": 200, "cost_usd_equivalent": 0.01},
        )
        after = m.as_dict()

        # Additive: surfaced under the observe-only ``handoff_advice`` key ...
        assert after["handoff_advice"] == {
            "tokens_in": 800, "tokens_out": 200, "cost_usd_equivalent": 0.01,
        }
        # ... while every total / phase rollup is byte-for-byte unchanged.
        assert after["total_tokens"] == before["total_tokens"]
        assert after["total_tokens_in"] == before["total_tokens_in"]
        assert after["total_tokens_out"] == before["total_tokens_out"]
        assert after["total_cost_usd_equivalent"] == before["total_cost_usd_equivalent"]
        assert after["phases"] == before["phases"]
        assert "handoff_advice" not in before
        # Advice usage never leaks into the per-phase rollup either.
        assert "handoff_advice" not in after["phases"]

    def test_no_advice_usage_means_no_key(self) -> None:
        m = MetricsCollector()
        m.record_phase("review_changes", tokens_in=10, tokens_out=2)
        assert "handoff_advice" not in m.as_dict()

    def test_record_advice_usage_ignores_empty_or_non_mapping(self) -> None:
        m = MetricsCollector()
        m.record_advice_usage({})            # usage unavailable → nothing
        m.record_advice_usage("nonsense")    # type: ignore[arg-type]
        m.record_advice_usage({"only_bool": True})  # no primitive numeric/str
        assert "handoff_advice" not in m.as_dict()

    def test_record_advice_usage_replace_semantics(self) -> None:
        m = MetricsCollector()
        m.record_advice_usage({"tokens_in": 10, "tokens_out": 5})
        m.record_advice_usage({"tokens_in": 99, "tokens_out": 88})
        # REPLACE, not merge: the upper layer re-derives the full aggregate.
        assert m.as_dict()["handoff_advice"] == {"tokens_in": 99, "tokens_out": 88}

    def test_advice_cost_scrubbed_when_accounting_disabled(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.infra import config
        monkeypatch.delenv("ORCHO_ACCOUNTING", raising=False)
        config._reset_config()
        try:
            m = MetricsCollector()
            m.record_advice_usage(
                {"tokens_in": 800, "tokens_out": 200, "cost_usd_equivalent": 0.01},
            )
            advice = m.as_dict()["handoff_advice"]
            # Cost stripped (never an invented/leaked dollar figure) ...
            assert "cost_usd_equivalent" not in advice
            # ... but token attribution survives.
            assert advice["tokens_in"] == 800
            assert advice["tokens_out"] == 200
        finally:
            config._reset_config()

    def test_advice_usage_round_trips_through_disk(
        self, accounting_on, tmp_path: Path,
    ) -> None:
        m = MetricsCollector()
        m.record_phase("review_changes", tokens_in=100, tokens_out=20)
        m.record_advice_usage(
            {"tokens_in": 800, "tokens_out": 200, "cost_usd_equivalent": 0.01},
        )
        m.save(tmp_path)

        resumed = MetricsCollector()
        resumed.load_from_disk(tmp_path / "metrics.json")
        d = resumed.as_dict()
        assert d["handoff_advice"] == {
            "tokens_in": 800, "tokens_out": 200, "cost_usd_equivalent": 0.01,
        }
        # Rehydration must not fold advice usage into the totals.
        assert d["total_tokens"] == 120

    def test_metrics_module_does_not_import_pipeline_project(self) -> None:
        # Layer boundary: core must never depend on pipeline.* . The usage is
        # pushed in via the primitive-only record_advice_usage API instead. We
        # scan IMPORT statements (not prose) so the module docstring may still
        # describe the upper layer.
        import ast

        import core.observability.metrics as metrics_mod
        tree = ast.parse(Path(metrics_mod.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(name.startswith("pipeline") for name in imported), (
            f"core.observability.metrics must not import pipeline.*: {imported}"
        )
