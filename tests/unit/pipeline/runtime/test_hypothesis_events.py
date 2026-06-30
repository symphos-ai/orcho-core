"""
research.py emits structured events.

Покрывает 3 новых event kinds которые UI использует для hypothesis-карточек:
hypothesis.proposed (текст гипотезы, attempt, max)
hypothesis.verdict (approved, critique, reviewer_provider)
hypothesis.exhausted (только если все попытки rejected)

Без этих событий UI не может показать "что именно тестировали и почему отклонили".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.observability import events as _events
from pipeline.engine.hypothesis import maybe_run_hypothesis, run_hypothesis_loop

# ── stub agents (минимально удовлетворяют протоколы) ────────────────────────

class _RejectingArchitect:
    """IAgentRuntime stub: always returns the same hypothesis text."""

    def __init__(self, text: str = "Try approach X") -> None:
        self._text = text
        self.model = "stub"
        self.session_id: str | None = None

    def invoke(self, prompt: str, cwd: str, **kw) -> str:
        return self._text

    def reset_session(self) -> None:
        self.session_id = None


class _ApprovingArchitect(_RejectingArchitect):
    pass


class _CriticReviewer:
    """Reject every hypothesis with a fixed critique string."""

    def __init__(self, critique: str) -> None:
        self._critique = critique
        self.model = "stub-qa"
        self.session_id: str | None = None

    def invoke(self, prompt: str, cwd: str, **kw) -> str:
        return self._critique

    def reset_session(self) -> None:
        self.session_id = None


def _approved_review_json(summary: str = "Approved by JSON contract.") -> str:
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": summary,
        "findings":      [],
    })


def _rejected_review_json(summary: str = "Rejected by JSON contract.") -> str:
    return json.dumps({
        "verdict":       "REJECTED",
        "short_summary": summary,
        "findings":      [{
            "id": "F1",
            "severity": "P2",
            "title": "Rejected hypothesis",
            "body": summary,
            "required_fix": "Revise the hypothesis.",
        }],
    })


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def event_store(tmp_path: Path):
    """Свежий event-store на каждый тест. После теста — close."""
    run_dir = tmp_path / "runs" / "20260504_test"
    _events.init_event_store(run_dir)
    yield run_dir
    # Cleanup: drop file pointer so следующий тест начнёт с чистого state.
    _events.init_event_store(None)


def _events_in(run_dir: Path) -> list:
    return _events.read_all(run_dir)


# ── tests ────────────────────────────────────────────────────────────────────


class TestHypothesisProposed:
    def test_emits_one_proposed_per_attempt_with_full_text(
        self, event_store: Path, tmp_path: Path,
    ) -> None:
        # Reject все попытки → loop пройдёт max раз
        run_hypothesis_loop(
            plan_agent=_RejectingArchitect("Refactor adapter into facade"),
            qa_agent=_CriticReviewer(_rejected_review_json(
                "Missing edge case for null payload"
            )),
            task="Add caching",
            cwd=str(tmp_path),
            codemap="",
            max_hypotheses=2,
        )

        proposed = [e for e in _events_in(event_store) if e.kind == "hypothesis.proposed"]
        assert len(proposed) == 2, "должны быть 2 proposed event-а — по одному на attempt"
        assert proposed[0].payload["attempt"] == 1
        assert proposed[1].payload["attempt"] == 2
        assert proposed[0].payload["max"] == 2
        # Полный текст гипотезы — UI рисует его в карточке.
        assert proposed[0].payload["text"] == "Refactor adapter into facade"


class TestHypothesisVerdict:
    def test_rejected_verdict_carries_critique_and_reviewer_provider(
        self, event_store: Path, tmp_path: Path,
    ) -> None:
        run_hypothesis_loop(
            plan_agent=_RejectingArchitect(),
            qa_agent=_CriticReviewer(_rejected_review_json(
                "Approach ignores concurrent writes"
            )),
            task="X",
            cwd=str(tmp_path),
            codemap="",
            max_hypotheses=1,
        )

        verdicts = [e for e in _events_in(event_store) if e.kind == "hypothesis.verdict"]
        assert len(verdicts) == 1
        v = verdicts[0].payload
        assert v["approved"] is False
        # Критика — главный сигнал ПОЧЕМУ отклонено, UI рендерит в expander.
        assert "concurrent writes" in v["critique"]
        # QA-провайдер виден — UI показывает badge "QA: codex".
        assert v["reviewer_provider"] == "_CriticReviewer"

    def test_approved_verdict_short_circuits_loop(
        self, event_store: Path, tmp_path: Path,
    ) -> None:
        run_hypothesis_loop(
            plan_agent=_ApprovingArchitect(),
            qa_agent=_CriticReviewer(_approved_review_json()),
            task="X",
            cwd=str(tmp_path),
            codemap="",
            max_hypotheses=3,
        )

        verdicts = [e for e in _events_in(event_store) if e.kind == "hypothesis.verdict"]
        # approved на 1 → loop останавливается, всего 1 verdict, не 3
        assert len(verdicts) == 1
        assert verdicts[0].payload["approved"] is True


class TestExhaustedTerminator:
    def test_exhausted_emitted_when_all_rejected(
        self, event_store: Path, tmp_path: Path,
    ) -> None:
        run_hypothesis_loop(
            plan_agent=_RejectingArchitect(),
            qa_agent=_CriticReviewer(_rejected_review_json("nope")),
            task="X",
            cwd=str(tmp_path),
            codemap="",
            max_hypotheses=2,
        )

        exhausted = [e for e in _events_in(event_store) if e.kind == "hypothesis.exhausted"]
        assert len(exhausted) == 1
        assert exhausted[0].payload["attempts"] == 2
        assert exhausted[0].payload["max"] == 2

    def test_exhausted_NOT_emitted_when_approved(
        self, event_store: Path, tmp_path: Path,
    ) -> None:
        run_hypothesis_loop(
            plan_agent=_ApprovingArchitect(),
            qa_agent=_CriticReviewer(_approved_review_json()),
            task="X",
            cwd=str(tmp_path),
            codemap="",
            max_hypotheses=3,
        )

        exhausted = [e for e in _events_in(event_store) if e.kind == "hypothesis.exhausted"]
        assert exhausted == []


class TestEventOrdering:
    def test_proposed_always_before_verdict_per_attempt(
        self, event_store: Path, tmp_path: Path,
    ) -> None:
        """UI reducer полагается на порядок: сначала видит "in flight",
 потом получает verdict и обновляет badge."""
        run_hypothesis_loop(
            plan_agent=_RejectingArchitect(),
            qa_agent=_CriticReviewer(_rejected_review_json("rejected reason")),
            task="X",
            cwd=str(tmp_path),
            codemap="",
            max_hypotheses=2,
        )

        relevant = [e for e in _events_in(event_store)
                    if e.kind in ("hypothesis.proposed", "hypothesis.verdict")]
        # Ожидаем: proposed#1, verdict#1, proposed#2, verdict#2
        assert [(e.kind, e.payload["attempt"]) for e in relevant] == [
            ("hypothesis.proposed", 1),
            ("hypothesis.verdict",  1),
            ("hypothesis.proposed", 2),
            ("hypothesis.verdict",  2),
        ]


class TestOverrideEnabled:
    """``--no-hypothesis`` / ``--hypothesis`` CLI flags bypass config.

    The override path matters because operators want to skip the gut-check
    on feature tasks ("add a field") without editing ``config.local.json``.
    """

    def test_override_false_skips_even_when_config_enabled(
        self, event_store: Path, tmp_path: Path, monkeypatch,
    ) -> None:
        from core.infra import config as _cfg
        monkeypatch.setattr(
            _cfg.AppConfig, "load",
            classmethod(lambda _cls: type("A", (), {
                "hypothesis": {"enabled": True, "max_attempts": 3},
            })()),
        )
        result, attempts = maybe_run_hypothesis(
            task="X",
            cwd=str(tmp_path),
            codemap="",
            dry_run=False,
            plan_agent=_RejectingArchitect(),
            qa_agent=_CriticReviewer(_rejected_review_json("nope")),
            override_enabled=False,
        )
        assert result is None
        assert attempts == []
        proposed = [e for e in _events_in(event_store)
                    if e.kind == "hypothesis.proposed"]
        assert proposed == []

    def test_override_true_runs_even_when_config_disabled(
        self, event_store: Path, tmp_path: Path, monkeypatch,
    ) -> None:
        from core.infra import config as _cfg
        monkeypatch.setattr(
            _cfg.AppConfig, "load",
            classmethod(lambda _cls: type("A", (), {
                "hypothesis": {"enabled": False, "max_attempts": 3},
                "task_language": "English",
            })()),
        )
        _, attempts = maybe_run_hypothesis(
            task="X",
            cwd=str(tmp_path),
            codemap="",
            dry_run=False,
            plan_agent=_ApprovingArchitect(),
            qa_agent=_CriticReviewer(_approved_review_json()),
            override_enabled=True,
            override_max_attempts=1,
        )
        assert len(attempts) == 1
        assert attempts[0]["approved"] is True

    def test_override_none_falls_through_to_config(
        self, event_store: Path, tmp_path: Path, monkeypatch,
    ) -> None:
        from core.infra import config as _cfg
        monkeypatch.setattr(
            _cfg.AppConfig, "load",
            classmethod(lambda _cls: type("A", (), {
                "hypothesis": {"enabled": False, "max_attempts": 3},
            })()),
        )
        result, attempts = maybe_run_hypothesis(
            task="X",
            cwd=str(tmp_path),
            codemap="",
            dry_run=False,
            plan_agent=_RejectingArchitect(),
            qa_agent=_CriticReviewer("nope"),
            override_enabled=None,
        )
        assert result is None
        assert attempts == []
