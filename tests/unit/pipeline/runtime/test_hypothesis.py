"""Hypothesis loop + plan-file validation.

Three layers covered here:

1. Hypothesis prompts run through the unified :meth:`IAgentRuntime.invoke`
   on Claude — no write permissions, codemap threaded through.

2. ``run_hypothesis_loop`` (``pipeline.engine.hypothesis``) — engine
   helper. Verifies the matrix: valid hypothesis on first try → returns
   it; invalid → retry → valid → returns it; all attempts rejected →
   returns ``None``; ``max_hypotheses=0`` short-circuits.

3. ``_validate_plan_file_paths`` — existence check used for the
   ``[implement k/N]`` progress label.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agents as agents_module
from agents.entities import SubTask
from agents.protocols import IAgentRuntime
from pipeline.engine.hypothesis import (
    _validate_hypothesis,
    format_rejected_hypothesis_feedback,
    format_validated_hypothesis_context,
    run_hypothesis_loop,
)
from pipeline.plan_parser import ParsedPlan
from pipeline.project.runtime_setup import (
    _validate_plan_file_paths,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _stream_result(stdout: str = "ok", returncode: int = 0, stderr: str = "", duration: float = 0.1):
    """Shape returned by ``agents._stream_run``."""
    return (stdout, returncode, stderr, duration)


def _approved_review_json(summary: str = "No blocking issues.") -> str:
    return json.dumps({
        "verdict": "APPROVED",
        "short_summary": summary,
        "findings": [],
        "risks": [],
        "checks": ["Reviewed hypothesis"],
    })


def _rejected_review_json(
    summary: str = "P1: missing constraint.",
    *,
    body: str = "The hypothesis misses a required constraint.",
) -> str:
    return json.dumps({
        "verdict": "REJECTED",
        "short_summary": summary,
        "findings": [{
            "id": "F1",
            "severity": "P1",
            "title": "Missing constraint",
            "body": body,
            "required_fix": "Address the missing constraint.",
        }],
        "risks": [],
        "checks": ["Reviewed hypothesis"],
    })


# ════════════════════════════════════════════════════════════════════════════
#  ClaudeAgent.invoke for the hypothesis prompt
# ════════════════════════════════════════════════════════════════════════════


class TestClaudeHypothesizeViaInvoke:
    @pytest.fixture
    def claude(self, mock_claude_bin: None) -> agents_module.ClaudeAgent:
        return agents_module.ClaudeAgent(model="claude-sonnet-test")

    @pytest.fixture
    def mock_stream(self, monkeypatch) -> MagicMock:
        mock = MagicMock(return_value=_stream_result("hypothesis text"))
        monkeypatch.setattr(agents_module, "_stream_run", mock)
        return mock

    def _hypothesis_prompt(self, task: str, cwd: str, codemap: str = "") -> str:
        from pipeline.prompts.builders import hypothesis_prompt
        turn = hypothesis_prompt(task, cwd, codemap=codemap)
        return turn.text if hasattr(turn, "text") else turn

    def test_returns_stdout(self, claude, mock_stream) -> None:
        prompt = self._hypothesis_prompt("fix the X bug", "/proj")
        assert claude.invoke(prompt, "/proj") == "hypothesis text"

    def test_read_only_drops_write_flags(self, claude, mock_stream) -> None:
        """Hypothesis is read-only. ``mutates_artifacts`` defaults to False,
        so the Claude write flags must be absent."""
        prompt = self._hypothesis_prompt("fix the X bug", "/proj")
        claude.invoke(prompt, "/proj")
        cmd = mock_stream.call_args[0][0]
        assert "--dangerously-skip-permissions" not in cmd
        assert "acceptEdits" not in cmd

    def test_read_only_default_does_not_resume(self, claude, mock_stream) -> None:
        """Without ``continue_session=True`` the runtime never adds ``--resume``,
        even when a previous session was captured. The orchestrator passes
        ``continue_session=False`` for the hypothesis call."""
        claude.session_id = "sess_abc123"
        prompt = self._hypothesis_prompt("task", "/proj")
        claude.invoke(prompt, "/proj")
        cmd = mock_stream.call_args[0][0]
        assert "--resume" not in cmd

    def test_includes_codemap_in_prompt(self, claude, mock_stream) -> None:
        prompt = self._hypothesis_prompt("task", "/proj", codemap="MapText")
        claude.invoke(prompt, "/proj")
        cmd = mock_stream.call_args[0][0]
        rendered = cmd[-1]
        assert "MapText" in rendered
        assert "task" in rendered

    def test_satisfies_agent_runtime_protocol(self, claude) -> None:
        assert isinstance(claude, IAgentRuntime)

    def test_forwards_cwd_to_subprocess(self, claude, mock_stream) -> None:
        """Регрессия: hypothesize должен запускать claude в проектной
 директории, а не в orcho engine. Иначе задачи вида "оценить
 commits abc/def" уходят в чужой git и валятся с bad object."""
        prompt = self._hypothesis_prompt("evaluate commits abc def", "/path/to/project")
        claude.invoke(prompt, "/path/to/project")
        kwargs = mock_stream.call_args.kwargs
        assert kwargs["cwd"] == "/path/to/project"

    def test_prompt_carries_task_language(self, claude, mock_stream) -> None:
        """Regression: hypothesis prompt must thread ``task_language``
 from ``AppConfig`` (engine default: English). Without it the
 agent loses the configured authoring-language directive."""
        prompt = self._hypothesis_prompt("evaluate commits abc def", "/proj")
        assert "English" in prompt


# ════════════════════════════════════════════════════════════════════════════
#  Test doubles: queue-driven IAgentRuntime fakes
# ════════════════════════════════════════════════════════════════════════════


class _QueuedArchitect:
    """Architect runtime double: returns hypotheses from a queue."""
    model = "fake-model"
    session_id: str | None = None

    def __init__(self, hypotheses: list[str]) -> None:
        self._hypotheses = list(hypotheses)
        self.calls = 0
        self.last_cwd: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        self.calls += 1
        self.last_cwd = cwd
        if self._hypotheses:
            return self._hypotheses.pop(0)
        return "exhausted"

    def reset_session(self) -> None:
        self.session_id = None


class _QueuedReviewer:
    """Reviewer runtime double: returns critiques from a queue."""
    model = "qa-model"
    session_id: str | None = None

    def __init__(self, verdicts: list[str]) -> None:
        self._verdicts = list(verdicts)
        self.calls = 0
        self.last_prompt: str | None = None

    def invoke(
        self, prompt: str, cwd: str, *,
        mutates_artifacts: bool = False,
        continue_session: bool = False,
        attachments: tuple = (),
    ) -> str:
        self.calls += 1
        self.last_prompt = prompt
        if self._verdicts:
            return self._verdicts.pop(0)
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


# ════════════════════════════════════════════════════════════════════════════
#  run_hypothesis_loop
# ════════════════════════════════════════════════════════════════════════════


class TestRunResearchLoop:
    def test_valid_hypothesis_first_try_returns_it(self, project_dir: str) -> None:
        arch = _QueuedArchitect(["use a state machine"])
        qa = _QueuedReviewer([_approved_review_json()])
        approved, attempts = run_hypothesis_loop(
            arch, qa, "task", project_dir, codemap="", max_hypotheses=3,
        )
        assert approved == "use a state machine"
        assert len(attempts) == 1
        assert attempts[0]["approved"] is True
        assert arch.calls == 1
        assert qa.calls == 1

    def test_invoke_receives_str_not_prompt_turn(self, project_dir: str) -> None:
        # ADR 0060 / Bug-3 class: the hypothesis generation + QA paths must
        # serialize the PromptTurn to ``turn.text`` at the invoke boundary,
        # never hand a PromptTurn object to the runtime.
        seen_types: list[type] = []

        class _StrictArchitect(_QueuedArchitect):
            def invoke(self, prompt, cwd, **kw):
                seen_types.append(type(prompt))
                assert isinstance(prompt, str), (
                    f"plan_agent.invoke got {type(prompt).__name__}, expected str"
                )
                return super().invoke(prompt, cwd, **kw)

        class _StrictReviewer(_QueuedReviewer):
            def invoke(self, prompt, cwd, **kw):
                seen_types.append(type(prompt))
                assert isinstance(prompt, str), (
                    f"qa_agent.invoke got {type(prompt).__name__}, expected str"
                )
                return super().invoke(prompt, cwd, **kw)

        arch = _StrictArchitect(["use a state machine"])
        qa = _StrictReviewer([_approved_review_json()])
        approved, _ = run_hypothesis_loop(
            arch, qa, "task", project_dir, codemap="", max_hypotheses=1,
        )
        assert approved == "use a state machine"
        assert seen_types == [str, str]  # both gen + QA invoked with str

    def test_generation_publishes_prompt_turn_to_trace(self, project_dir: str) -> None:
        # The invoke boundary must publish the effective PromptTurn so the
        # runtime adapter's debug transcript is honest (single-take slot).
        from core.observability import prompt_trace

        captured: list[object] = []

        class _CapturingArchitect(_QueuedArchitect):
            def invoke(self, prompt, cwd, **kw):
                captured.append(prompt_trace.peek_last_prompt_turn())
                return super().invoke(prompt, cwd, **kw)

        arch = _CapturingArchitect(["hyp"])
        qa = _QueuedReviewer([_approved_review_json()])
        run_hypothesis_loop(
            arch, qa, "task", project_dir, codemap="", max_hypotheses=1,
        )
        from pipeline.prompts.turn import PromptTurn
        assert captured and isinstance(captured[0], PromptTurn)
        assert captured[0].text  # non-empty wire string

    def test_labels_print_before_runtime_invocations(
        self,
        project_dir: str,
        capsys,
    ) -> None:
        class _EchoArchitect(_QueuedArchitect):
            def invoke(self, prompt: str, cwd: str, **kwargs) -> str:
                print("-> runtime=claude")
                return super().invoke(prompt, cwd, **kwargs)

        class _EchoReviewer(_QueuedReviewer):
            def invoke(self, prompt: str, cwd: str, **kwargs) -> str:
                print("-> runtime=codex")
                return super().invoke(prompt, cwd, **kwargs)

        arch = _EchoArchitect(["field mismatch in payload"])
        qa = _EchoReviewer([_approved_review_json()])

        run_hypothesis_loop(
            arch, qa, "task", project_dir, codemap="", max_hypotheses=1,
        )

        out = capsys.readouterr().out
        assert out.index("Hypothesis (attempt 1):") < out.index("-> runtime=claude")
        assert out.index("-> runtime=claude") < out.index(
            "Hypothesis output (attempt 1):"
        )
        assert out.index("Hypothesis output (attempt 1):") < out.index(
            "field mismatch in payload"
        )
        assert out.index("-> runtime=claude") < out.index("field mismatch in payload")
        assert out.index("Hypothesis QA (attempt 1):") < out.index("-> runtime=codex")
        assert out.index("-> runtime=codex") < out.index("verdict")

    def test_retries_until_valid(self, project_dir: str) -> None:
        arch = _QueuedArchitect(["bad guess", "still bad", "third time lucky"])
        qa = _QueuedReviewer([
            _rejected_review_json("P1: this hypothesis misses X"),
            _rejected_review_json("P1: still missing Y"),
            _approved_review_json(),
        ])
        approved, attempts = run_hypothesis_loop(
            arch, qa, "task", project_dir, codemap="", max_hypotheses=3,
        )
        assert approved == "third time lucky"
        assert len(attempts) == 3
        assert [a["approved"] for a in attempts] == [False, False, True]
        assert arch.calls == 3

    def test_all_rejected_returns_none(self, project_dir: str) -> None:
        arch = _QueuedArchitect(["a", "b", "c"])
        qa = _QueuedReviewer([
            _rejected_review_json("P1: missing edge cases"),
            _rejected_review_json("P1: wrong direction"),
            _rejected_review_json("P1: ignores constraint Z"),
        ])
        approved, attempts = run_hypothesis_loop(
            arch, qa, "task", project_dir, codemap="", max_hypotheses=3,
        )
        assert approved is None
        assert len(attempts) == 3
        assert all(a["approved"] is False for a in attempts)

    def test_max_hypotheses_zero_short_circuits(self, project_dir: str) -> None:
        """max_hypotheses=0 -> never call invoke, never call QA."""
        arch = _QueuedArchitect(["never used"])
        qa = _QueuedReviewer([_approved_review_json()])
        approved, attempts = run_hypothesis_loop(
            arch, qa, "task", project_dir, codemap="", max_hypotheses=0,
        )
        assert approved is None
        assert attempts == []
        assert arch.calls == 0
        assert qa.calls == 0

    def test_qa_runtime_error_treated_as_rejection(self, project_dir: str) -> None:
        """If the reviewer CLI explodes, we should NOT crash the pipeline —
 treat the attempt as rejected and let the loop retry."""
        arch = _QueuedArchitect(["first", "second"])

        class _BoomThenOK:
            model = "qa"
            session_id: str | None = None

            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, prompt, cwd, **kw):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("codex exited 2")
                return _approved_review_json()

            def reset_session(self) -> None:
                self.session_id = None

        qa = _BoomThenOK()
        approved, attempts = run_hypothesis_loop(
            arch, qa, "task", project_dir, codemap="", max_hypotheses=3,
        )
        assert approved == "second"
        assert len(attempts) == 2
        assert attempts[0]["approved"] is False
        assert "validate_hypothesis error" in attempts[0]["critique"]


# ════════════════════════════════════════════════════════════════════════════
#  _validate_hypothesis (direct unit test)
# ════════════════════════════════════════════════════════════════════════════


class _PromptSnoopReviewer:
    """Reviewer double that captures every (prompt, cwd) invoke received,
    and returns a constant response.

    Also peeks the active prompt composition and records the
    ``artifact_path`` of any artifact part so callers can correlate
    invocations with the on-disk artefact location without regexing
    the prompt string (the path lives on the PromptPart as
    metadata-only ``artifact_path``, never in wire bytes)."""
    model = "qa"
    session_id: str | None = None

    def __init__(self, response: str | None = None) -> None:
        self._response = response or _approved_review_json()
        self.captured: list[dict] = []

    def invoke(self, prompt: str, cwd: str, **kw):
        from core.observability import prompt_trace as _trace
        artifact_path: str | None = None
        turn = _trace.take_last_prompt_turn()
        if turn is not None:
            artifact_part = next(
                (p for p in turn.parts if p.kind == "artifact"),
                None,
            )
            if artifact_part is not None:
                artifact_path = artifact_part.artifact_path
        self.captured.append({
            "prompt": prompt, "cwd": cwd,
            "artifact_path": artifact_path, **kw,
        })
        return self._response

    def reset_session(self) -> None:
        self.session_id = None


def _const_reviewer(response: str):
    """Reviewer that returns the same string for every invocation."""
    class _R:
        model = "qa"
        session_id: str | None = None
        def invoke(self, prompt, cwd, **kw): return response
        def reset_session(self) -> None: self.session_id = None
    return _R()


class TestValidateHypothesis:
    def test_temp_file_is_cleaned_up(self, project_dir: str) -> None:
        """The temp .md file must NOT linger.

        The on-disk path lives on the artifact PromptPart as
        ``artifact_path`` metadata (not in the wire prompt body, so we
        cannot regex it out of the prompt string anymore). We peek
        the upper composition from ``prompt_trace`` during invoke to
        recover the path off the artifact PromptPart, then assert the
        temp file exists at call time and is cleaned up after.
        """
        from core.observability import prompt_trace as _trace
        captured: dict = {}

        class _ExistenceChecker:
            model = "qa"
            session_id: str | None = None
            def invoke(self, prompt, cwd, **kw):
                turn = _trace.take_last_prompt_turn()
                assert turn is not None, (
                    "prompt turn must be set by builder before invoke"
                )
                artifact_part = next(
                    (p for p in turn.parts if p.kind == "artifact"),
                    None,
                )
                assert artifact_part is not None, (
                    "validate_hypothesis must publish an artifact part"
                )
                path = artifact_part.artifact_path
                assert path is not None, (
                    "artifact part must carry artifact_path metadata"
                )
                captured["path"] = path
                assert Path(path).exists()
                return _approved_review_json()
            def reset_session(self) -> None: self.session_id = None

        qa = _ExistenceChecker()
        approved, _critique, _review = _validate_hypothesis(qa, "h", "task", project_dir)
        assert approved is True
        assert "path" in captured
        # File should be gone after the call.
        assert not Path(captured["path"]).exists()

    def test_prose_contract_violation_is_rejected(self, project_dir: str) -> None:
        """Pure prose is a contract failure, not a fallback review."""
        approved, critique, _review = _validate_hypothesis(
            _const_reviewer("this is wrong"),
            "h", "task", project_dir,
        )
        assert approved is False
        assert "parse error" in critique
        assert "this is wrong" in critique

    def test_json_approved_contract_is_honored(self, project_dir: str) -> None:
        """REA-3.5: a valid APPROVED JSON review must approve the hypothesis
        even when the human-readable body is in Russian."""
        payload = json.dumps({
            "verdict": "APPROVED",
            "short_summary": "Гипотеза правдоподобна; подход жизнеспособен.",
            "findings": [],
            "checks": ["Прочитал гипотезу"],
        })
        approved, critique, _review = _validate_hypothesis(
            _const_reviewer(payload), "h", "task", project_dir,
        )
        assert approved is True
        assert "правдоподоб" in critique
        assert critique.startswith("# Hypothesis QA")

    def test_json_rejected_contract_overrides_positive_words(self, project_dir: str) -> None:
        """Body says 'looks good' but the typed verdict is REJECTED — the
        contract field, not prose vibes, must drive the gate."""
        payload = json.dumps({
            "verdict": "REJECTED",
            "short_summary": "P1: missed file ownership constraint.",
            "findings": [{
                "id": "F1",
                "severity": "P1",
                "title": "Looks good at first glance, but misses file ownership",
                "body": "The hypothesis does not address who owns the changed files.",
                "required_fix": "State which module/file the change touches.",
            }],
        })
        approved, critique, _review = _validate_hypothesis(
            _const_reviewer(payload), "h", "task", project_dir,
        )
        assert approved is False
        assert "REJECTED" in critique
        assert "file ownership" in critique

    def test_malformed_contract_is_rejected(self, project_dir: str) -> None:
        """Structured-but-broken output must hard-fail the gate, not silently
        degrade into prose parsing."""
        approved, critique, _review = _validate_hypothesis(
            _const_reviewer("[]"), "h", "task", project_dir,
        )
        assert approved is False
        assert "parse error" in critique

    def test_prompt_carries_review_json_contract(self, project_dir: str) -> None:
        """The runtime sees a fully-composed prompt that wraps the
        hypothesis temp file content + the typed JSON review contract."""
        qa = _PromptSnoopReviewer(
            response='{"verdict":"APPROVED","short_summary":"ok","findings":[]}'
        )
        _validate_hypothesis(qa, "h", "task", project_dir)

        prompt = qa.captured[0]["prompt"]
        # System-owned JSON contract is what enforces output shape.
        assert '<orcho:system-block kind="contract" name="review_json" version="1">' in prompt
        assert "exactly one JSON object" in prompt
        assert "short_summary" in prompt
        # Protocol enum values stay English exactly as listed in the schema.
        assert "APPROVED" in prompt
        assert "REJECTED" in prompt

    def test_temp_file_NOT_in_project_dir(self, project_dir: str, tmp_path: Path) -> None:
        """Regression: hypothesis temp.md must NOT be created inside the
        project tree. Unity / Xcode / hot-reload watchers spawn .meta
        siblings for any new file under the tree.

        Two scenarios:
          1. Event-store active → file lands in run_dir.
          2. No event-store → file lands in $TMPDIR (default tempfile dir).
        Neither is allowed to be under project_dir.
        """
        from core.observability import events as evstore

        qa = _PromptSnoopReviewer()

        # Scenario 1: with event-store
        run_dir = tmp_path / "runs" / "20260504_140000"
        run_dir.mkdir(parents=True)
        evstore.init_event_store(run_dir)
        try:
            _validate_hypothesis(qa, "h", "task", project_dir)
        finally:
            evstore.init_event_store(None)

        # Scenario 2: without event-store (system tempdir fallback)
        _validate_hypothesis(qa, "h", "task", project_dir)

        # Recover the embedded tmp paths from the artifact_path
        # metadata captured during each invoke (the path is no longer
        # in the wire prompt body — it lives on the artifact
        # PromptPart as ``artifact_path``).
        paths = [
            rec["artifact_path"] for rec in qa.captured
            if rec.get("artifact_path") is not None
        ]
        assert paths, "no artifact_path captured across invocations"

        proj = Path(project_dir).resolve()
        for p in paths:
            assert proj not in Path(p).resolve().parents, (
                f"hypothesis temp file leaked into project tree: {p}"
            )
        # Sanity: at least one of them should have been the run_dir.
        assert any(run_dir in Path(p).resolve().parents for p in paths)


# ════════════════════════════════════════════════════════════════════════════
#  format_validated_hypothesis_context
# ════════════════════════════════════════════════════════════════════════════


def test_validated_hypothesis_context_names_plan_level_semantics() -> None:
    rendered = format_validated_hypothesis_context("field mismatch in payload")

    assert rendered.startswith(
        "\n\nVALIDATED HYPOTHESIS (QA-approved planning context):"
    )
    assert "planning input, not execution approval" in rendered
    assert "incorporate this direction" in rendered
    assert "verify/falsify its riskiest assumption early" in rendered
    assert "explain why it diverges" in rendered
    assert "field mismatch in payload" in rendered


# ════════════════════════════════════════════════════════════════════════════
#  format_rejected_hypothesis_feedback
# ════════════════════════════════════════════════════════════════════════════


def _rejected_attempt(
    *,
    attempt: int = 1,
    hypothesis: str = "Rename email_address to email on producer side.",
    summary: str = "Missing persistence change for users.project_id.",
    finding_title: str = "Persistence gap not addressed",
    finding_body: str = "Schema lacks a project_id column.",
    finding_fix: str = "Add a migration that introduces users.project_id.",
    risk: str = "Producer rename may break downstream consumers.",
    check: str = "Reviewed api.payload.USER_FIELDS and web consumer.",
) -> dict:
    return {
        "attempt": attempt,
        "hypothesis": hypothesis,
        "approved": False,
        "critique": "fallback critique prose",
        "review": {
            "verdict": "REJECTED",
            "short_summary": summary,
            "findings": [{
                "id": "F1", "severity": "P1",
                "title": finding_title,
                "body": finding_body,
                "required_fix": finding_fix,
            }],
            "risks": [risk],
            "checks": [check],
        },
    }


class TestRejectedHypothesisFeedback:
    def test_headline_marks_block_as_negative_context(self) -> None:
        out = format_rejected_hypothesis_feedback([_rejected_attempt()])
        assert "REJECTED HYPOTHESIS FEEDBACK" in out
        assert "not validated direction" in out
        # Must NOT borrow the approved wording — that's the bug we're avoiding.
        assert "VALIDATED HYPOTHESIS" not in out
        assert "QA-approved planning context" not in out

    def test_includes_hypothesis_summary_findings_risks_checks(self) -> None:
        out = format_rejected_hypothesis_feedback([_rejected_attempt(
            hypothesis="HYPOTHESIS_X",
            summary="SUMMARY_X",
            finding_title="TITLE_X",
            finding_body="BODY_X",
            finding_fix="FIX_X",
            risk="RISK_X",
            check="CHECK_X",
        )])
        for needle in (
            "HYPOTHESIS_X", "SUMMARY_X",
            "TITLE_X", "BODY_X", "FIX_X",
            "RISK_X", "CHECK_X",
        ):
            assert needle in out, f"missing field: {needle}"

    def test_ignores_critique_when_no_structured_review(self) -> None:
        attempt = {
            "attempt": 1,
            "hypothesis": "Try X",
            "approved": False,
            "critique": "Reviewer rejected with PROSE_ONLY_TEXT",
            "review": {},
        }
        out = format_rejected_hypothesis_feedback([attempt])
        assert "Try X" in out
        assert "PROSE_ONLY_TEXT" not in out

    def test_renders_multiple_attempts_in_order(self) -> None:
        out = format_rejected_hypothesis_feedback([
            _rejected_attempt(attempt=1, hypothesis="HYP_ONE"),
            _rejected_attempt(attempt=2, hypothesis="HYP_TWO"),
        ])
        idx_one = out.index("HYP_ONE")
        idx_two = out.index("HYP_TWO")
        assert idx_one < idx_two
        assert "Rejected attempt #1" in out
        assert "Rejected attempt #2" in out


# ════════════════════════════════════════════════════════════════════════════
#  _validate_plan_file_paths
# ════════════════════════════════════════════════════════════════════════════


class TestValidatePlanFilePaths:
    def test_empty_plan_returns_empty_lists(self, project_dir: str) -> None:
        plan = ParsedPlan(short_summary="", planning_context="", subtasks=(), source="test")
        existing, missing = _validate_plan_file_paths(plan, project_dir)
        assert existing == []
        assert missing == []

    def test_splits_existing_and_missing(self, project_dir: str) -> None:
        # Create one of the two files
        (Path(project_dir) / "src").mkdir()
        (Path(project_dir) / "src" / "real.py").write_text("# real")
        plan = ParsedPlan(
            short_summary="",
            planning_context="",
            source="test",
            subtasks=(
                SubTask(id="t1", goal="touch real", files=("src/real.py",)),
                SubTask(id="t2", goal="touch missing", files=("src/missing.py",)),
            ),
        )
        existing, missing = _validate_plan_file_paths(plan, project_dir)
        assert existing == ["src/real.py"]
        assert missing == ["src/missing.py"]


@pytest.fixture(autouse=True)
def _live_output_mode_for_full_transcript():
    """Pin the full live transcript shape (T2 summary reconciliation).

    ``summary`` is the default run-output mode — the compact append-only
    arc that collapses phase headers to ``▶ <phase>`` and the review /
    plan / implement outcome blocks to single lines. These tests assert
    the full-fidelity transcript, so force ``live`` (rendering only; no
    echo / verbose / trace side effects) and restore afterwards.
    """
    from core.observability import logging as _logging

    _before = _logging.get_output_mode()
    _logging._output_mode = "live"
    try:
        yield
    finally:
        _logging._output_mode = _before
