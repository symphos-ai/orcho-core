"""AC4 guard: operator-facing surfaces stay on ``task_language`` (ADR 0121, T5).

``content_language`` governs only the *outward* delivery artifacts (commit
message, PR title/body). The operator-facing surfaces must stay on the operator
language (``task_language``) regardless of ``content_language``:

* the final-acceptance *release contract* (``runtime_review_uncommitted_prompt``
  with ``output_contract='release'``) renders its body-language directive on
  ``cfg.task_language`` — never ``content_language``;
* the operator review preview (``render_review_block``, used by
  ``_print_review_preview``) echoes the stored ``short_summary`` verbatim, so an
  operator-language summary is displayed unchanged.

These pin the decoupling so a future change cannot silently route the operator
surfaces through the outward-delivery language.
"""
from __future__ import annotations

import pytest

from core.infra.config import AppConfig
from core.io.transcript import render_review_block
from pipeline import prompts


def test_release_contract_rides_task_language_not_content_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Operator language Russian; outward-delivery language English (default).
    monkeypatch.setenv("TASK_LANGUAGE", "Russian")
    monkeypatch.setenv("CONTENT_LANGUAGE", "English")
    AppConfig.load.cache_clear()
    try:
        cfg = AppConfig.load()
        assert cfg.task_language == "Russian"
        assert cfg.content_language == "English"

        text = prompts.runtime_review_uncommitted_prompt(
            "check ship readiness",
            project_dir="/project",
            output_contract="release",
        ).text

        # The release-contract directive rides the OPERATOR language.
        assert "required_check) in Russian." in text
        # ...and content_language never leaks into the release contract.
        assert "required_check) in English." not in text
    finally:
        AppConfig.load.cache_clear()


def test_operator_review_preview_echoes_operator_language_summary() -> None:
    # The operator TTY preview renderer is language-neutral: it prints the
    # stored short_summary verbatim, so an operator-language (Russian) summary
    # is surfaced unchanged — never re-authored into the outward language.
    summary = "Изменения готовы к отгрузке; тесты покрывают основные пути."
    block = render_review_block(
        {"verdict": "APPROVED", "short_summary": summary, "findings": []},
        title="Final acceptance",
    )
    assert summary in block
