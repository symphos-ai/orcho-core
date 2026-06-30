"""Orchestration-thin run-control service (Stage 5).

:class:`RunService` is a delegating facade over surfaces that already
own their behaviour:

- ``start`` dispatches a typed request to ``run_project_pipeline`` /
  ``run_cross_project_pipeline`` by type — no orchestration is copied;
- ``snapshot`` / ``events`` are thin pass-throughs over the existing
  read model (:func:`sdk.run_control.snapshots.load_run_snapshot`,
  :func:`sdk.run_control.events.read_run_events` /
  :func:`~sdk.run_control.events.tail_run_events`);
- ``decide_handoff`` delegates to :func:`sdk.phase_handoff.phase_handoff_decide`
  with no validation of its own;
- ``resume`` reuses the canonical resume-context helpers
  (latest-selection, run-kind classification, mode classification,
  terminal-parent rejection, follow-up field mapping) and delegates the
  actual run to ``run_project_pipeline``;
- ``cancel`` returns a typed :class:`RunControlUnsupported` — core has
  no run supervisor.

The service never builds prompts, invokes agents, reads terminal
output, or parses a renderer. Dependencies are injected as callables
(defaulting to the real functions) so tests drive it with fakes; the
real defaults lazy-import their targets, keeping the constructor
side-effect free and free of any local CLI binary.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sdk.run_control.types import (
    CancelCommand,
    DeliveryDecisionCommand,
    DeliveryDecisionResult,
    PhaseHandoffDecisionCommand,
    ResumeCommand,
    RunControlUnsupported,
    RunEvent,
    RunSnapshot,
)
from sdk.runs import _CWD_DEFAULT

if TYPE_CHECKING:
    from pipeline.cross_project.app_types import CrossRunResult
    from pipeline.project.types import ProjectRunResult
    from pipeline.run_state.types import RunStateValidationReport
    from sdk.phase_handoff import PhaseHandoffDecision

__all__ = ["RunService"]


# ── Lazy real-function defaults (constructor stays side-effect free) ─────────

def _default_start_project(request: Any) -> ProjectRunResult:
    from pipeline.project.app import run_project_pipeline

    return run_project_pipeline(request)


def _default_start_cross(request: Any) -> CrossRunResult:
    from pipeline.cross_project.app import run_cross_project_pipeline

    return run_cross_project_pipeline(request)


def _default_decide(**kwargs: Any) -> PhaseHandoffDecision:
    from sdk.phase_handoff import phase_handoff_decide

    return phase_handoff_decide(**kwargs)


def _default_decide_delivery(**kwargs: Any) -> DeliveryDecisionResult:
    from sdk.run_control.delivery import decide_delivery

    return decide_delivery(**kwargs)


def _default_snapshot_loader(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> RunSnapshot:
    from sdk.run_control.snapshots import load_run_snapshot

    return load_run_snapshot(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )


def _default_events_reader(
    run_id: str,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> tuple[RunEvent, ...]:
    from sdk.run_control.events import read_run_events

    return read_run_events(
        run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
    )


def _default_events_tailer(
    run_id: str,
    *,
    since_seq: int = 0,
    poll: float = 0.3,
    stop_predicate: Callable[[], bool] | None = None,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> Iterator[RunEvent]:
    from sdk.run_control.events import tail_run_events

    return tail_run_events(
        run_id,
        since_seq=since_seq,
        poll=poll,
        stop_predicate=stop_predicate,
        workspace=workspace,
        runs_dir=runs_dir,
        cwd=cwd,
    )


def _default_state_validator(run_dir: Path | str) -> RunStateValidationReport:
    from pipeline.run_state.consistency import validate_run_state

    return validate_run_state(run_dir)


class RunService:
    """Delegating facade for run start / observe / decide / resume.

    Every dependency is a constructor-injected callable defaulting to the
    real function. The constructor only stores references — it imports
    nothing heavy and touches no filesystem or CLI binary.
    """

    def __init__(
        self,
        *,
        start_project: Callable[[Any], Any] = _default_start_project,
        start_cross: Callable[[Any], Any] = _default_start_cross,
        decide: Callable[..., Any] = _default_decide,
        decide_delivery: Callable[..., Any] = _default_decide_delivery,
        snapshot_loader: Callable[..., RunSnapshot] = _default_snapshot_loader,
        events_reader: Callable[..., tuple[RunEvent, ...]] = _default_events_reader,
        events_tailer: Callable[..., Iterator[RunEvent]] = _default_events_tailer,
        state_validator: Callable[..., RunStateValidationReport] = (
            _default_state_validator
        ),
    ) -> None:
        self._start_project = start_project
        self._start_cross = start_cross
        self._decide = decide
        self._decide_delivery = decide_delivery
        self._snapshot_loader = snapshot_loader
        self._events_reader = events_reader
        self._events_tailer = events_tailer
        self._state_validator = state_validator

    # ── start ────────────────────────────────────────────────────────────

    def start(self, request: Any) -> Any:
        """Dispatch a typed run request to the matching app entrypoint.

        ``ProjectRunRequest`` → ``run_project_pipeline`` (returns
        ``ProjectRunResult``); ``CrossRunRequest`` →
        ``run_cross_project_pipeline`` (returns ``CrossRunResult``). An
        unknown request type raises :class:`TypeError`.
        """
        from pipeline.cross_project.app_types import CrossRunRequest
        from pipeline.project.types import ProjectRunRequest

        if isinstance(request, ProjectRunRequest):
            return self._start_project(request)
        if isinstance(request, CrossRunRequest):
            return self._start_cross(request)
        raise TypeError(
            "RunService.start: unsupported request type "
            f"{type(request).__name__!r}; expected ProjectRunRequest or "
            "CrossRunRequest."
        )

    # ── observe ──────────────────────────────────────────────────────────

    def snapshot(
        self,
        run_id: str | None = None,
        *,
        workspace: Path | str | None = None,
        runs_dir: Path | str | None = None,
        cwd: Path | str | None | object = _CWD_DEFAULT,
    ) -> RunSnapshot:
        """Thin wrapper over :func:`load_run_snapshot`."""
        return self._snapshot_loader(
            run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
        )

    def events(
        self,
        run_id: str,
        *,
        since_seq: int = 0,
        tail: bool = False,
        poll: float = 0.3,
        stop_predicate: Callable[[], bool] | None = None,
        workspace: Path | str | None = None,
        runs_dir: Path | str | None = None,
        cwd: Path | str | None | object = _CWD_DEFAULT,
    ) -> tuple[RunEvent, ...] | Iterator[RunEvent]:
        """Read (``tail=False``) or live-tail (``tail=True``) run events.

        ``since_seq`` / ``poll`` / ``stop_predicate`` apply only to the
        tail path, mirroring :func:`tail_run_events`; the read path
        returns every recorded event in seq order.
        """
        if tail:
            return self._events_tailer(
                run_id,
                since_seq=since_seq,
                poll=poll,
                stop_predicate=stop_predicate,
                workspace=workspace,
                runs_dir=runs_dir,
                cwd=cwd,
            )
        return self._events_reader(
            run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd,
        )

    def validate_state(
        self,
        run_id_or_dir: str | Path | None = None,
        *,
        workspace: Path | str | None = None,
        runs_dir: Path | str | None = None,
        cwd: Path | str | None | object = _CWD_DEFAULT,
    ) -> RunStateValidationReport:
        """Diagnose run-state consistency for a run (strictly read-only).

        Optional diagnostic surface: the check runs only when this method
        is invoked and never mutates orchestration, resume, or handoff
        state. It delegates entirely to
        :func:`pipeline.run_state.consistency.validate_run_state`; this
        method owns no diagnostic logic of its own.

        ``run_id_or_dir`` resolution mirrors :meth:`snapshot`: an existing
        run directory (a ``Path`` / ``str`` for which ``is_dir()`` holds)
        is used directly — the hermetic path for tests and direct callers;
        anything else is treated as a run id and resolved via
        :func:`sdk.runs.find_run` (``None`` selects the newest run), with
        ``workspace`` / ``runs_dir`` / ``cwd`` propagated unchanged.
        """
        if run_id_or_dir is not None and Path(run_id_or_dir).is_dir():
            run_dir = Path(run_id_or_dir)
        else:
            from sdk.runs import find_run

            run_dir = find_run(
                run_id_or_dir,
                workspace=workspace,
                runs_dir=runs_dir,
                cwd=cwd,
            ).run_dir
        return self._state_validator(run_dir)

    # ── decide ───────────────────────────────────────────────────────────

    def decide_handoff(
        self, command: PhaseHandoffDecisionCommand,
    ) -> PhaseHandoffDecision:
        """Delegate to :func:`phase_handoff_decide` with no extra validation."""
        return self._decide(**command.to_decide_kwargs())

    def decide_delivery(
        self, command: DeliveryDecisionCommand,
    ) -> DeliveryDecisionResult:
        """Delegate to :func:`sdk.run_control.delivery.decide_delivery`.

        Thin pass-through (mirroring :meth:`decide_handoff`): the injected
        callable owns the policy replay and run finalization; the service adds
        no validation of its own.
        """
        return self._decide_delivery(
            run_id=command.run_id,
            action=command.action,
            note=command.note,
        )

    # ── resume ───────────────────────────────────────────────────────────

    def resume(
        self, command: ResumeCommand,
    ) -> Any | RunControlUnsupported:
        """Resume a single-project run (checkpoint or follow-up).

        Strict order, reusing the canonical resume-context helpers:

        1. resolve ``runs_dir`` via :func:`sdk.runs.find_runs_dir` from the
           command's ``workspace`` / ``runs_dir`` / ``cwd`` (never ambient
           config);
        2. normalise ``run_id`` — ``None`` / ``"latest"`` select the
           newest project run via :func:`resolve_latest_run` *before* any
           filesystem access for an explicit id;
        3. load the parent ``meta.json``;
        4. classify run kind via :func:`meta_run_kind`: anything but a
           single-project run (cross / unclassifiable) returns a typed
           ``cross-resume-not-in-slice`` unsupported *before* project/task
           resolution;
        5. classify resume mode from the CLI-equivalent inputs;
        6. for CHECKPOINT, reject any terminal parent via the canonical
           :func:`is_terminal_resume_parent` (success + phase-handoff halt
           + commit-decision halt + commit-decision fix);
        7. restore ``project_dir`` (explicit > meta) and ``task``;
        8. choose ``output_dir`` by mode — CHECKPOINT continues into the
           parent run dir with ``resume_from`` set; FOLLOWUP mints a new
           run dir with ``resume_mode='followup'``;
        9. map the five follow-up slots via :func:`build_followup_resume_fields`;
        10. build a ``ProjectRunRequest`` and delegate to
            ``run_project_pipeline``.
        """
        from pipeline.control.resume_context import (
            ResumeMode,
            build_followup_resume_fields,
            classify_resume_mode,
            is_terminal_commit_decision_fix,
            is_terminal_final_acceptance_rejected,
            is_terminal_resume_parent,
            load_resume_meta,
            meta_run_kind,
            resolve_latest_run,
            resolve_project,
            resolve_task,
        )
        from pipeline.project.types import ProjectRunRequest
        from sdk.runs import find_runs_dir

        runs_dir = find_runs_dir(
            workspace=command.workspace,
            runs_dir=command.runs_dir,
            cwd=command.cwd,
        )

        if command.run_id is None or command.run_id == "latest":
            resolved_run_id = resolve_latest_run(
                runs_dir=runs_dir,
                kind="run",
                prefer_incomplete=command.task is None,
                require_existing_project=True,
            )
        else:
            resolved_run_id = command.run_id

        resumed = load_resume_meta(runs_dir / resolved_run_id)
        meta = resumed.meta if resumed is not None else {}
        if meta_run_kind(meta) != "run":
            return RunControlUnsupported(
                operation="resume", reason="cross-resume-not-in-slice",
            )

        resume_mode = classify_resume_mode(
            resume=resolved_run_id,
            explicit_task=command.task,
            explicit_task_file=None,
        )

        if (
            resume_mode == ResumeMode.CHECKPOINT
            and is_terminal_resume_parent(meta)
        ):
            if (
                is_terminal_commit_decision_fix(meta)
                or is_terminal_final_acceptance_rejected(meta)
            ):
                # Correction-required terminal (fix-marked / rejected dead-end):
                # neither a checkpoint continuation nor this bare task-less resume
                # advances the run. The actionable path is a from_run_plan
                # follow-up carrying the held diff, so return the correction-
                # specific outcome (keeps the resume surface consistent with
                # diagnose / delivery_gate, all pointing at the follow-up) rather
                # than the generic not-checkpointable refusal.
                return RunControlUnsupported(
                    operation="resume", reason="correction-followup-required",
                )
            return RunControlUnsupported(
                operation="resume", reason="terminal-parent-not-checkpointable",
            )

        project_dir = resolve_project(
            explicit_project=command.project, resumed=resumed,
        )
        task = resolve_task(
            explicit_task=command.task,
            explicit_task_file=None,
            resumed=resumed,
        )

        if resume_mode == ResumeMode.CHECKPOINT:
            output_dir = runs_dir / resolved_run_id
            resume_from: str | None = resolved_run_id
            resume_mode_value: str | None = None
        else:
            if command.output_dir is not None:
                output_dir = Path(command.output_dir)
            else:
                new_run_id = (
                    command.output_run_id
                    or datetime.now().strftime("%Y%m%d_%H%M%S")
                )
                output_dir = runs_dir / new_run_id
            resume_from = None
            resume_mode_value = ResumeMode.FOLLOWUP.value

        followup = build_followup_resume_fields(
            resume_mode=resume_mode,
            resume_run_id=resolved_run_id,
            resumed=resumed,
        )

        # ADR 0101 / T2: fix the operator runtime/model override into durable
        # meta BEFORE the ``ProjectRunRequest`` is built, so the resume reads a
        # persisted record (no silent fallback). The override lands in the run
        # dir the resume will re-enter (``output_dir``); the pipeline re-reads
        # and applies it to the named phase. Validation rejects a non-candidate
        # pair, so an invalid override aborts resume here rather than starting a
        # run with the wrong runtime.
        if command.runtime_override is not None:
            from sdk.run_control.runtime_override import persist_runtime_override

            persist_runtime_override(
                output_dir,
                phase=command.runtime_override.phase,
                runtime=command.runtime_override.runtime,
                model=command.runtime_override.model,
            )

        request_kwargs: dict[str, Any] = {
            "task": task,
            "project_dir": project_dir,
            "output_dir": output_dir,
            "resume_from": resume_from,
            "resume_mode": resume_mode_value,
            "followup_parent_run_id": followup.parent_run_id,
            "followup_parent_run_dir": followup.parent_run_dir,
            "followup_parent_status": followup.parent_status,
            "followup_base_task": followup.base_task,
            "followup_session_seeds": followup.session_seeds,
        }
        if command.max_rounds is not None:
            request_kwargs["max_rounds"] = command.max_rounds
        if command.model is not None:
            request_kwargs["model"] = command.model
        if command.profile_name is not None:
            request_kwargs["profile_name"] = command.profile_name

        return self._start_project(ProjectRunRequest(**request_kwargs))

    # ── cancel ───────────────────────────────────────────────────────────

    def cancel(self, command: CancelCommand) -> RunControlUnsupported:
        """No run supervisor in core — return a typed unsupported result."""
        return RunControlUnsupported(
            operation="cancel", reason="no-core-supervisor",
        )
