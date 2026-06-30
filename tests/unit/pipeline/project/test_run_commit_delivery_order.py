"""Narrow ordering test: prompt block precedes outcome line.

``_run_commit_delivery`` first delegates to ``resolve_commit_delivery``
(which prints the journey prompt block, including "What do you want to
do?"), then — after the operator picks an action — delegates to
``apply_commit_delivery`` and finally renders the outcome via
``render_delivery_outcome``. This test pins that order by driving the
skip branch end-to-end (no ``finalize_with_terminal_output``, no
``apply``/``transport`` machinery — skip short-circuits in
``apply_commit_delivery`` at the top).

Operator input is injected by wrapping ``resolve_commit_delivery``
itself: the wrapper forces ``input_fn=lambda _: '3'`` (the skip
choice) and forwards everything else to the real implementation, so
the real ``_prompt_action`` runs and the real ``render_delivery_outcome``
prints the outcome. ``builtins.input``, ``_prompt_action``, and
``render_delivery_outcome`` are intentionally NOT monkeypatched —
the test would lose its meaning if either were stubbed.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline.engine.commit_delivery as cd
from core.io.ansi import strip_ansi
from pipeline.project.run import _PipelineRun


def test_prompt_block_precedes_outcome_on_skip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)

    # 1) Wrap resolve_commit_delivery so the real implementation runs but
    #    operator input is forced to '3' (skip). This is the contracted
    #    injection point — it preserves the call-time import inside
    #    _run_commit_delivery (the import resolves the attribute at call
    #    time, so the monkeypatch applied here is picked up).
    real_resolve = cd.resolve_commit_delivery

    def resolve_with_input(**kwargs: object) -> cd.CommitDeliveryDecision:
        kwargs["input_fn"] = lambda _prompt: "3"
        return real_resolve(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cd, "resolve_commit_delivery", resolve_with_input)

    # 2) Stub only the resolve-side helpers that touch git / TTY — every
    #    one of them is strictly upstream of _prompt_action, so the
    #    prompt itself still runs unmocked.
    monkeypatch.setattr(cd, "stdio_interactive", lambda: True)
    monkeypatch.setattr(
        cd,
        "_run_owned_patch",
        lambda *_a, **_kw: (
            "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        ),
    )
    monkeypatch.setattr(cd, "_changed_paths", lambda *_a, **_kw: ("a.py",))
    monkeypatch.setattr(cd, "_untracked_paths", lambda *_a, **_kw: ())

    # 3) Make AppConfig.load() deterministic so the test does not depend
    #    on the developer's ~/.config/orcho.json. add_untracked=False
    #    skips the untracked-transport branch in apply_commit_delivery.
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "add_untracked": False},
        ),
    )

    # 4) Minimal _PipelineRun stub — only attributes _run_commit_delivery
    #    reads up to the skip-branch return path.
    stub = SimpleNamespace(
        output_dir=run_dir,
        session={"status": "done"},
        project_path=project_dir,
        parent_run_id=None,
        project_alias=None,
        no_interactive=False,
        worktree_context=None,
        session_ts="20260603_000000",
        _commit_delivery_baseline=lambda: "HEAD",
    )

    _PipelineRun._run_commit_delivery(stub, diff_cwd=project_dir)

    out = strip_ansi(capsys.readouterr().out)

    assert "What do you want to do?" in out, (
        f"prompt block missing from stdout: {out!r}"
    )
    assert "Delivery skipped" in out, (
        f"outcome line missing from stdout: {out!r}"
    )
    assert out.index("What do you want to do?") < out.index(
        "Delivery skipped",
    ), (
        "outcome printed before prompt — ordering contract violated:\n"
        f"{out!r}"
    )


def test_pipeline_run_wires_llm_commit_message_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)

    class _CommitMessageAgent:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def invoke(self, prompt: str, cwd: str, **kwargs: object) -> str:
            self.calls.append({"prompt": prompt, "cwd": cwd, **kwargs})
            return (
                '{"subject": "fix(delivery): generated message", '
                '"body": "", "type": "fix", "scope": "delivery", '
                '"breaking": false}'
            )

    agent = _CommitMessageAgent()
    generated_messages: list[str | None] = []

    def resolve_with_generator(**kwargs: object) -> cd.CommitDeliveryDecision:
        decision = cd.CommitDeliveryDecision(
            action="approve",
            status="pending",
            run_id="r1",
            decision_id="r1",
            project_path=project_dir,
            source_path=worktree,
            baseline_ref="HEAD",
            release_summary="summary fallback",
            patch_text="diff --git a/a.py b/a.py\n",
            changed_paths=("a.py",),
            decided_at="2026-06-04T00:00:00+00:00",
        )
        generator = kwargs["commit_message_generator"]
        assert callable(generator)
        generated_messages.append(generator(decision))
        return decision

    monkeypatch.setattr(cd, "resolve_commit_delivery", resolve_with_generator)
    monkeypatch.setattr(
        cd,
        "apply_commit_delivery",
        lambda decision, **_kwargs: cd.CommitDeliveryDecision(
            action="approve",
            status="committed",
            run_id=decision.run_id,
            decision_id=decision.decision_id,
            project_path=decision.project_path,
            source_path=decision.source_path,
            baseline_ref=decision.baseline_ref,
            final_message=generated_messages[-1],
            commit_message_strategy="llm_generate",
            decided_at=decision.decided_at,
        ),
    )
    monkeypatch.setattr(
        "pipeline.project.run.config.AppConfig.load",
        lambda: SimpleNamespace(
            commit={"enabled": True, "default_strategy": "llm_generate"},
            task_language="Russian",
        ),
    )

    stub = SimpleNamespace(
        output_dir=run_dir,
        session={"status": "done"},
        project_path=project_dir,
        parent_run_id=None,
        project_alias=None,
        no_interactive=True,
        worktree_context=None,
        session_ts="r1",
        phase_config=SimpleNamespace(final_acceptance_agent=agent),
        _commit_delivery_baseline=lambda: "HEAD",
    )

    _PipelineRun._run_commit_delivery(stub, diff_cwd=worktree)

    assert generated_messages == ["fix(delivery): generated message\n"]
    assert agent.calls
    assert agent.calls[0]["cwd"] == str(worktree)
    assert agent.calls[0]["mutates_artifacts"] is False
    assert agent.calls[0]["continue_session"] is False
    assert "Run diff:" in str(agent.calls[0]["prompt"])
    assert stub.session["commit_delivery"]["strategy"] == "llm_generate"
    assert (
        stub.session["commit_delivery"]["final_message"]
        == "fix(delivery): generated message\n"
    )
