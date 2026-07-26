"""Unit tests for :class:`sdk.run_control.service.RunService`.

The service is orchestration-thin: it dispatches typed requests, delegates
reads, forwards decide kwargs, and reuses the canonical resume-context
helpers. These tests drive it with injected fakes (no real pipeline run,
no agents) and a hermetic tmp ``runs_dir`` passed with ``cwd=None`` so no
ambient workspace / walk-up leaks in.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.cross_project.app_types import CrossRunRequest
from pipeline.project.types import ProjectRunRequest, ProjectRunResult
from sdk.run_control.service import RunService
from sdk.run_control.types import (
    CancelCommand,
    PhaseHandoffDecisionCommand,
    ResumeCommand,
    RunControlUnsupported,
    RuntimeOverride,
)

# ── helpers ──────────────────────────────────────────────────────────────────


class _Recorder:
    """Callable that records its calls and returns a fixed sentinel."""

    def __init__(self, result: object = None) -> None:
        self.result = result
        self.args: list[tuple] = []
        self.kwargs: list[dict] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.args.append(args)
        self.kwargs.append(kwargs)
        return self.result


def _make_run(runs_dir: Path, run_id: str, meta: dict) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return run_dir


def _project_meta(
    *,
    project: str | None = "/proj",
    task: str = "parent task",
    status: str = "interrupted",
    **extra: object,
) -> dict:
    meta: dict = {"task": task, "status": status}
    if project is not None:
        meta["project"] = project
    meta.update(extra)
    return meta


def _service(**overrides: object) -> RunService:
    """Build a RunService whose dependencies are all recording fakes
    unless explicitly overridden."""
    defaults: dict = {
        "start_project": _Recorder("project-result"),
        "start_cross": _Recorder("cross-result"),
        "decide": _Recorder("decision"),
        "snapshot_loader": _Recorder("snapshot"),
        "events_reader": _Recorder(("event",)),
        "events_tailer": _Recorder(iter(())),
    }
    defaults.update(overrides)
    return RunService(**defaults)  # type: ignore[arg-type]


# ── start ────────────────────────────────────────────────────────────────────


class TestStart:
    def test_project_request_dispatches_to_start_project(self) -> None:
        start_project = _Recorder("project-result")
        start_cross = _Recorder("cross-result")
        svc = _service(start_project=start_project, start_cross=start_cross)
        req = ProjectRunRequest(task="t", project_dir="/p")
        out = svc.start(req)
        assert out == "project-result"
        assert start_project.args == [(req,)]
        assert start_cross.args == []

    def test_cross_request_dispatches_to_start_cross(self) -> None:
        start_project = _Recorder("project-result")
        start_cross = _Recorder("cross-result")
        svc = _service(start_project=start_project, start_cross=start_cross)
        req = CrossRunRequest(task="t", projects={"api": Path("/a")})
        out = svc.start(req)
        assert out == "cross-result"
        assert start_cross.args == [(req,)]
        assert start_project.args == []

    def test_unknown_request_type_raises_typeerror(self) -> None:
        svc = _service()
        with pytest.raises(TypeError, match="unsupported request type"):
            svc.start(object())


# ── snapshot / events ──────────────────────────────────────────────────────


class TestObserve:
    def test_snapshot_delegates_with_context(self, tmp_path: Path) -> None:
        loader = _Recorder("snap")
        svc = _service(snapshot_loader=loader)
        out = svc.snapshot("RID", runs_dir=tmp_path, cwd=None)
        assert out == "snap"
        assert loader.args == [("RID",)]
        assert loader.kwargs[0] == {
            "workspace": None, "runs_dir": tmp_path, "cwd": None,
        }

    def test_events_read_path_uses_reader(self, tmp_path: Path) -> None:
        reader = _Recorder(("e",))
        tailer = _Recorder(iter(()))
        svc = _service(events_reader=reader, events_tailer=tailer)
        out = svc.events("RID", runs_dir=tmp_path, cwd=None)
        assert out == ("e",)
        assert reader.args == [("RID",)]
        assert tailer.args == []

    def test_events_tail_path_uses_tailer(self, tmp_path: Path) -> None:
        reader = _Recorder(("e",))
        marker = iter(("tailed",))
        tailer = _Recorder(marker)
        svc = _service(events_reader=reader, events_tailer=tailer)
        out = svc.events(
            "RID", tail=True, since_seq=5, runs_dir=tmp_path, cwd=None,
        )
        assert out is marker
        assert reader.args == []
        assert tailer.kwargs[0]["since_seq"] == 5


# ── decide_handoff ───────────────────────────────────────────────────────────


class TestDecideHandoff:
    def test_forwards_to_decide_kwargs(self) -> None:
        decide = _Recorder("decision")
        svc = _service(decide=decide)
        cmd = PhaseHandoffDecisionCommand(
            run_id="RID",
            handoff_id="HID",
            action="retry_feedback",
            feedback="fix it",
            note="n",
        )
        out = svc.decide_handoff(cmd)
        assert out == "decision"
        assert decide.kwargs == [cmd.to_decide_kwargs()]


# ── decide_delivery ──────────────────────────────────────────────────────────


class TestDecideDelivery:
    def test_forwards_command_fields(self) -> None:
        from sdk.run_control import DeliveryDecisionCommand

        decide_delivery = _Recorder("delivery-result")
        svc = _service(decide_delivery=decide_delivery)
        cmd = DeliveryDecisionCommand(run_id="RID", action="approve", note="n")

        out = svc.decide_delivery(cmd)

        assert out == "delivery-result"
        assert decide_delivery.kwargs == [
            {"run_id": "RID", "action": "approve", "note": "n"},
        ]


# ── resume ───────────────────────────────────────────────────────────────────


class TestResume:
    def test_returns_existing_project_run_result_without_new_wire_type(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        _make_run(runs, "20260101_000000", _project_meta(project=str(project)))
        result = ProjectRunResult(
            session={"status": "halted", "halt_reason": "resume_control_refusal"},
            output_dir=runs / "20260101_000000", run_id="20260101_000000",
        )
        svc = _service(start_project=_Recorder(result))

        out = svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))

        assert out is result

    def test_uses_passed_runs_dir_without_ambient(self, tmp_path: Path) -> None:
        # F1: workspace=None, cwd=None — resolution comes solely from the
        # passed runs_dir; no ambient config / walk-up leaks in.
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(runs, "20260101_000000", _project_meta(project="/from-meta"))
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        out = svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))
        assert out == "ran"
        req = start_project.args[0][0]
        assert isinstance(req, ProjectRunRequest)
        assert req.project_dir == "/from-meta"

    def test_run_id_none_selects_latest(self, tmp_path: Path) -> None:
        # F2: run_id=None is equivalent to "latest" and is normalised to
        # latest-selection before any Path access (no Path/None crash).
        runs = tmp_path / "runs"
        runs.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        _make_run(runs, "20260101_000000", _project_meta(project=str(project)))
        _make_run(runs, "20260514_120000", _project_meta(project=str(project)))
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        out = svc.resume(ResumeCommand(run_id=None, runs_dir=runs, cwd=None))
        assert out == "ran"
        req = start_project.args[0][0]
        # Newest run picked; checkpoint continues into it.
        assert req.resume_from == "20260514_120000"
        assert req.output_dir == runs / "20260514_120000"

    def test_latest_literal_selects_latest(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        _make_run(runs, "20260101_000000", _project_meta(project=str(project)))
        _make_run(runs, "20260514_120000", _project_meta(project=str(project)))
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        svc.resume(ResumeCommand(run_id="latest", runs_dir=runs, cwd=None))
        req = start_project.args[0][0]
        assert req.resume_from == "20260514_120000"

    def test_latest_skips_stale_deleted_project(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        _make_run(
            runs,
            "20260609_125615",
            _project_meta(project=str(project)),
        )
        _make_run(
            runs,
            "20260610_003038",
            _project_meta(project=str(tmp_path / "deleted-project")),
        )
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        svc.resume(ResumeCommand(run_id="latest", runs_dir=runs, cwd=None))
        req = start_project.args[0][0]
        assert req.resume_from == "20260609_125615"

    def test_cross_run_rejected_before_resolve(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(
            runs, "20260101_000000",
            {"task": "t", "projects": {"api": "/a"}, "status": "interrupted"},
        )
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        out = svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))
        assert out == RunControlUnsupported(
            operation="resume", reason="cross-resume-not-in-slice",
        )
        assert start_project.args == []

    def test_checkpoint_builds_request_from_meta(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(
            runs, "20260101_000000",
            _project_meta(project="/from-meta", task="meta task"),
        )
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))
        req = start_project.args[0][0]
        assert req.task == "meta task"
        assert req.project_dir == "/from-meta"
        assert req.resume_from == "20260101_000000"
        assert req.output_dir == runs / "20260101_000000"
        assert req.resume_mode is None
        assert req.followup_parent_run_id is None
        assert req.followup_parent_run_dir is None
        assert req.followup_parent_status is None
        assert req.followup_base_task is None
        assert req.followup_session_seeds is None

    def test_checkpoint_output_dir_is_parent_run_dir(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        run_dir = _make_run(runs, "20260101_000000", _project_meta())
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))
        req = start_project.args[0][0]
        assert req.output_dir == run_dir

    def test_followup_with_task_builds_new_run(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(
            runs, "20260101_000000",
            _project_meta(
                project="/from-meta",
                task="parent task",
                phases={"plan": [{"session_id": "plan-1"}]},
            ),
        )
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        svc.resume(ResumeCommand(
            run_id="20260101_000000",
            task="follow up work",
            output_run_id="20260202_000000",
            runs_dir=runs,
            cwd=None,
        ))
        req = start_project.args[0][0]
        assert req.task == "follow up work"
        assert req.resume_mode == "followup"
        assert req.resume_from is None
        assert req.output_dir == runs / "20260202_000000"
        assert req.followup_parent_run_id == "20260101_000000"
        assert req.followup_parent_run_dir == str(
            (runs / "20260101_000000").resolve()
        )
        assert req.followup_base_task == "parent task"
        assert req.followup_session_seeds == {"plan": "plan-1"}

    def test_followup_output_dir_override(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(runs, "20260101_000000", _project_meta())
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        explicit = tmp_path / "elsewhere"
        svc.resume(ResumeCommand(
            run_id="20260101_000000",
            task="follow up",
            output_dir=explicit,
            runs_dir=runs,
            cwd=None,
        ))
        req = start_project.args[0][0]
        assert req.output_dir == explicit

    def test_explicit_project_respected(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(runs, "20260101_000000", _project_meta(project="/from-meta"))
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        svc.resume(ResumeCommand(
            run_id="20260101_000000",
            project="/explicit",
            runs_dir=runs,
            cwd=None,
        ))
        req = start_project.args[0][0]
        assert req.project_dir == "/explicit"

    def test_optional_fields_passed_through(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(runs, "20260101_000000", _project_meta())
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        svc.resume(ResumeCommand(
            run_id="20260101_000000",
            max_rounds=3,
            model="claude-test",
            profile_name="advanced",
            runs_dir=runs,
            cwd=None,
        ))
        req = start_project.args[0][0]
        assert req.max_rounds == 3
        assert req.model == "claude-test"
        assert req.profile_name == "advanced"

    @pytest.mark.parametrize(
        "extra",
        [
            {"status": "done"},
            {"status": "halted", "halt_reason": "commit_decision_halt"},
            {"status": "halted", "halt_reason": "phase_handoff_halt"},
        ],
    )
    def test_checkpoint_terminal_parent_rejected(
        self, tmp_path: Path, extra: dict,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        meta = _project_meta()
        meta.update(extra)
        _make_run(runs, "20260101_000000", meta)
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        out = svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))
        assert out == RunControlUnsupported(
            operation="resume", reason="terminal-parent-not-checkpointable",
        )
        assert start_project.args == []

    @pytest.mark.parametrize(
        "halt_reason",
        [
            "commit_decision_fix",
            # Rejected final-acceptance dead-ends (T1 vocabulary): a bare
            # task-less resume is meaningless — the actionable path is a
            # from_run_plan follow-up. Shape mirrors run 20260626_165338_90fb22
            # (halted + project, no projects/phase_handoff/commit gate/parent).
            "final_acceptance_rejected",
            "final_acceptance_no_diff",
        ],
    )
    def test_checkpoint_correction_terminal_requires_followup(
        self, tmp_path: Path, halt_reason: str,
    ) -> None:
        # A correction-required terminal returns the correction-specific outcome
        # (pointing at a from_run_plan follow-up), not the generic
        # not-checkpointable refusal — keeping the resume surface consistent with
        # diagnose / delivery_gate.
        runs = tmp_path / "runs"
        runs.mkdir()
        meta = _project_meta()
        meta.update({"status": "halted", "halt_reason": halt_reason})
        _make_run(runs, "20260101_000000", meta)
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        out = svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))
        assert out == RunControlUnsupported(
            operation="resume", reason="correction-followup-required",
        )
        assert start_project.args == []

    def test_terminal_parent_followup_still_allowed(self, tmp_path: Path) -> None:
        # A terminal parent is only rejected for CHECKPOINT; supplying a
        # task makes it a FOLLOWUP, which is fine.
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(runs, "20260101_000000", _project_meta(status="done"))
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        out = svc.resume(ResumeCommand(
            run_id="20260101_000000", task="next", runs_dir=runs, cwd=None,
        ))
        assert out == "ran"
        assert start_project.args[0][0].resume_mode == "followup"

    def test_non_terminal_halted_checkpoint_resumes(self, tmp_path: Path) -> None:
        # Positive control: a halted run whose halt_reason is NOT in the
        # terminal vocabulary stays checkpoint-resumable — start_project runs
        # and the request continues into the existing run dir.
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(
            runs, "20260101_000000",
            _project_meta(status="halted", halt_reason="parse_error"),
        )
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        out = svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))
        assert out == "ran"
        req = start_project.args[0][0]
        assert req.resume_from == "20260101_000000"
        assert req.resume_mode is None


class TestResumeRuntimeOverride:
    """ADR 0101 / T2 — a runtime override on the resume command is validated
    and persisted into durable meta BEFORE the ``ProjectRunRequest`` is built."""

    def test_override_persisted_before_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        run_dir = _make_run(
            runs, "20260101_000000", _project_meta(project=str(project)),
        )

        import sdk.run_control.runtime_override as ro
        monkeypatch.setattr(
            ro, "configured_replacement_candidates",
            lambda phase: [{"runtime": "codex", "model": "gpt5"}],
        )

        captured: dict = {}

        def _start(req: object) -> str:
            # Read meta AT request-dispatch time → proves persist ran first.
            captured["meta"] = json.loads(
                (run_dir / "meta.json").read_text(encoding="utf-8"),
            )
            captured["req"] = req
            return "ran"

        svc = _service(start_project=_start)
        out = svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
            runtime_override=RuntimeOverride(
                phase="plan", runtime="codex", model="gpt5",
            ),
        ))

        assert out == "ran"
        # The durable record was on disk by the time the request was built.
        record = captured["meta"]["runtime_override"]
        assert record["phase"] == "plan"
        assert record["runtime"] == "codex"
        assert record["model"] == "gpt5"
        assert isinstance(captured["req"], ProjectRunRequest)
        # output_dir (the resumed run dir) is where the override landed.
        assert captured["req"].output_dir == run_dir

    def test_invalid_override_pair_aborts_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        _make_run(runs, "20260101_000000", _project_meta(project=str(project)))

        import sdk.run_control.runtime_override as ro
        from sdk.run_control.runtime_override import RuntimeOverrideError
        monkeypatch.setattr(
            ro, "configured_replacement_candidates",
            lambda phase: [{"runtime": "codex", "model": "gpt5"}],
        )
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        with pytest.raises(RuntimeOverrideError):
            svc.resume(ResumeCommand(
                run_id="20260101_000000", runs_dir=runs, cwd=None,
                runtime_override=RuntimeOverride(
                    phase="plan", runtime="codex", model="not-a-candidate",
                ),
            ))
        # No run started — the override is rejected before ProjectRunRequest.
        assert start_project.args == []

    def test_plain_resume_writes_no_override(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        run_dir = _make_run(
            runs, "20260101_000000", _project_meta(project=str(project)),
        )
        start_project = _Recorder("ran")
        svc = _service(start_project=start_project)
        svc.resume(ResumeCommand(
            run_id="20260101_000000", runs_dir=runs, cwd=None,
        ))
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert "runtime_override" not in meta


# ── cancel ───────────────────────────────────────────────────────────────────


class TestCancel:
    def test_cancel_is_unsupported(self) -> None:
        svc = _service()
        out = svc.cancel(CancelCommand(run_id="RID"))
        assert out == RunControlUnsupported(
            operation="cancel", reason="no-core-supervisor",
        )


# ── validate_state ───────────────────────────────────────────────────────────


def _write_events(run_dir: Path, lines: list[dict]) -> None:
    run_dir.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _write_decision(run_dir: Path, name: str, decision: dict) -> None:
    dd = run_dir / "phase_handoff_decisions"
    dd.mkdir(exist_ok=True)
    dd.joinpath(f"{name}.json").write_text(json.dumps(decision), encoding="utf-8")


def _handoff_event(handoff_id: str, phase: str = "validate_plan") -> dict:
    return {
        "seq": 1,
        "ts": "t",
        "kind": "phase.handoff_requested",
        "phase": phase,
        "payload": {"handoff_id": handoff_id, "phase": phase},
    }


def _dir_files(run_dir: Path) -> set[Path]:
    return {p for p in run_dir.rglob("*") if p.is_file()}


def _codes(report: object) -> set[str]:
    return {issue.code for issue in report.issues}  # type: ignore[attr-defined]


class TestValidateState:
    def test_clean_run_is_ok_with_no_issues(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260101_000000"
        run_dir.mkdir()
        _write_events(run_dir, [
            {"seq": 1, "ts": "t", "kind": "run.start", "phase": None,
             "payload": {"task": "t", "run_kind": "single_project"}},
            {"seq": 2, "ts": "t", "kind": "phase.start", "phase": "PLAN",
             "payload": {"title": "PLAN"}},
            {"seq": 3, "ts": "t", "kind": "phase.end", "phase": "PLAN",
             "payload": {"title": "PLAN", "outcome": "ok"}},
        ])
        (run_dir / "meta.json").write_text(
            json.dumps({"status": "running"}), encoding="utf-8",
        )
        # Real default validator via a direct run-dir path (no find_run).
        report = RunService().validate_state(run_dir)
        assert report.ok
        assert report.issues == ()

    def test_interrupted_with_active_handoff(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260101_000000"
        run_dir.mkdir()
        _write_events(run_dir, [_handoff_event("h1")])
        (run_dir / "meta.json").write_text(
            json.dumps(
                {"status": "interrupted", "phase_handoff": {"id": "h1"}}
            ),
            encoding="utf-8",
        )
        report = RunService().validate_state(run_dir)
        assert "interrupted_with_active_handoff" in _codes(report)
        severity = next(
            i.severity
            for i in report.issues
            if i.code == "interrupted_with_active_handoff"
        )
        assert severity == "warning"

    def test_terminal_halt_with_stale_handoff(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260101_000000"
        run_dir.mkdir()
        _write_events(run_dir, [_handoff_event("h1")])
        (run_dir / "meta.json").write_text(
            json.dumps({"status": "halted", "phase_handoff": {"id": "h1"}}),
            encoding="utf-8",
        )
        report = RunService().validate_state(run_dir)
        assert "terminal_with_stale_handoff" in _codes(report)

    def test_validate_state_is_read_only(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260101_000000"
        run_dir.mkdir()
        _write_events(run_dir, [_handoff_event("h1")])
        (run_dir / "meta.json").write_text(
            json.dumps({"status": "halted", "phase_handoff": {"id": "h1"}}),
            encoding="utf-8",
        )
        _write_decision(run_dir, "h1", {"action": "continue", "handoff_id": "h1"})

        meta_before = (run_dir / "meta.json").read_bytes()
        decision_path = run_dir / "phase_handoff_decisions" / "h1.json"
        decision_before = decision_path.read_bytes()
        files_before = _dir_files(run_dir)

        RunService().validate_state(run_dir)

        assert (run_dir / "meta.json").read_bytes() == meta_before
        assert decision_path.read_bytes() == decision_before
        assert _dir_files(run_dir) == files_before

    def test_run_id_resolved_via_find_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 'RID' is not an existing directory, so validate_state must resolve
        # it via sdk.runs.find_run, propagating workspace/runs_dir/cwd, and
        # hand find_run(...).run_dir to the validator.
        resolved_dir = tmp_path / "resolved_run"
        resolved_dir.mkdir()
        find_run = _Recorder(SimpleNamespace(run_dir=resolved_dir))
        monkeypatch.setattr("sdk.runs.find_run", find_run)

        validator = _Recorder("report")
        svc = RunService(state_validator=validator)
        out = svc.validate_state(
            "RID", workspace="/ws", runs_dir=tmp_path, cwd=None,
        )

        assert out == "report"
        assert find_run.args == [("RID",)]
        assert find_run.kwargs[0] == {
            "workspace": "/ws", "runs_dir": tmp_path, "cwd": None,
        }
        assert validator.args == [(resolved_dir,)]

    def test_delegates_to_injected_validator(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260101_000000"
        run_dir.mkdir()
        validator = _Recorder("the-report")
        svc = RunService(state_validator=validator)
        out = svc.validate_state(run_dir)
        assert out == "the-report"
        assert validator.args == [(run_dir,)]
