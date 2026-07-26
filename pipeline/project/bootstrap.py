"""Run-bootstrap setup for the project pipeline.

ADR 0042 Phase C. Owns the pre-dispatch setup the project pipeline
runs once per ``run_pipeline`` invocation:

* ``infer_workspace_from_project`` — walk up from the project dir to
  find the ``workspace-orchestrator/`` directory runs land in.
* ``materialized_run_artifacts`` / ``assert_fresh_run_dir_available``
  / ``RunIdCollisionError`` — fail fast when a fresh run id collides
  with an existing materialised run.
* ``resolve_run_id_and_setup_logging`` — pick the run id (resume >
  ``$ORCHO_RUN_ID`` > direct caller output dir > minted timestamp), wire
  ``setup_run_logging``,
  emit ``run.start``.
* ``init_session_with_atexit`` — build the in-memory session dict,
  persist ``meta.json`` early, register the graceful-exit hook,
  carry-forward an active ``phase_handoff`` payload from a prior
  ``meta.json`` and refuse to resume a halted run
  (``PhaseHandoffHaltedError``).
* ``init_checkpoint_with_resume`` — initialise the SQLite checkpoint
  store and hydrate the session's phase log from a prior resume.

Stop condition (ADR 0042 #11): this module must NOT accept agents /
profile / runtime-shaped dependencies as parameters. Anything that
mutates ``PhaseAgentConfig`` slots or needs a profile object is
session-setup, not bootstrap, and belongs in ``profile_dispatch.py``
(Phase E) or a future ``session_setup.py``. The
``_apply_followup_session_seeds`` helper that operates on
``PhaseAgentConfig`` therefore stays out of this module by design.

Note on patch surface: ``load_plugin``, ``has_uncommitted``,
``git_diff_stat`` continue to be re-exported from
``pipeline.project_orchestrator`` for the documented Phase 5d-fixup
test-patch pattern (see ``pipeline.lifecycle._DefaultGitHelpers``).
Bootstrap helpers do not call those three directly; they are used by
``run_pipeline`` proper which stays in the orchestrator module.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.protocols import SessionMode
from core.observability import events as _events, logging as _logging
from core.observability.logging import success
from pipeline.checkpoint import CheckpointStore
from pipeline.engine import (
    is_sub_pipeline as _is_sub_pipeline_check,
    save_session,
    setup_run_logging,
)
from pipeline.plugins import PluginConfig
from pipeline.project.types import PresentationPolicy
from pipeline.run_state.terminal import mark_run_interrupted

# ── exceptions ────────────────────────────────────────────────────────────


class RunIdCollisionError(RuntimeError):
    """Raised when a fresh run would overwrite an existing run directory."""


class PhaseHandoffHaltedError(RuntimeError):
    """Raised when ``run_pipeline(resume_from=...)`` targets a run whose
    prior phase-handoff decision halted it.

    The SDK's ``phase_handoff_decide(action="halt")`` is the terminal
    transition for a paused run: ``meta.status`` flips to ``halted``,
    ``meta.halt_reason`` records ``phase_handoff_halt``, and the active
    payload is cleared. Re-launching the same run id after halt would
    bypass that contract — bootstrap refuses to dispatch and surfaces
    this error so the caller starts a new run instead.

    Phase D (handoff service) imports this class from here too: the
    in-dispatch resume helper raises it when it discovers a torn-write
    halt mid-run.
    """


# ── workspace inference ───────────────────────────────────────────────────


def infer_workspace_from_project(project_path: str | None) -> Path | None:
    """Walk up from ``project_path`` looking for a ``workspace-orchestrator/``
    directory; return its absolute path or ``None``.

    The standard layout for orcho-managed multi-project workspaces is::

        <root>/
          ├── project_a/                     ← --project
          ├── project_b/
          └── workspace-orchestrator/        ← runs land here

    Walk-up handles both the immediate-sibling case and projects nested
    one extra level (``<root>/sub/proj``). Symlinks are resolved so a
    project living under ``~/projects/foo`` symlinked from elsewhere
    still finds the right workspace.
    """
    if not project_path:
        return None
    try:
        p = Path(project_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not p.exists():
        return None
    for ancestor in [p, *p.parents]:
        candidate = ancestor / "workspace-orchestrator"
        if candidate.is_dir():
            return candidate
    return None


@dataclass(frozen=True)
class WorkspaceDrift:
    """Resolved env-vs-cwd workspace mismatch for guard warnings.

    ``env_workspace`` is what ``$ORCHO_WORKSPACE`` points at; ``cwd_workspace``
    is the workspace-orchestrator directory found by walking up from the
    caller's cwd. Both are absolute, ``resolve()``-canonicalised paths.
    """

    env_workspace: Path
    cwd_workspace: Path


def autoderive_workspace_from_cwd(cwd: str | Path | None = None) -> Path | None:
    """Override ``$ORCHO_WORKSPACE`` / ``$ORCHO_RUNSPACE`` with the cwd
    walk-up workspace when one exists and disagrees with env.

    Symmetric with the explicit ``--project`` branch in ``orcho run``: an
    operator standing inside a workspace tree gets that workspace, not
    whatever stale env var their shell carries from a prior session.
    Resume / from-run-plan get the same treatment — a run id is local
    to the workspace you're standing in.

    Side effects when override fires:
      * ``os.environ["ORCHO_WORKSPACE"]`` / ``ORCHO_RUNSPACE`` written;
      * ``core.infra.config._reset_config()`` invalidated so downstream
        lookups see the new values;
      * yellow/bold warning printed when the override displaces a
        non-empty env var (foot-gun notice);
      * plain ``↳`` info line when no env was set (no warning colour).

    Returns the resolved workspace path when an override happened (or
    when env already matched cwd walk-up), ``None`` when cwd walk-up
    found no workspace and the caller should fall back to env or
    explicit flags.
    """
    cwd_ws = infer_workspace_from_project(str(cwd) if cwd is not None else os.getcwd())
    if cwd_ws is None:
        return None
    env_raw = os.environ.get("ORCHO_WORKSPACE")
    env_ws = Path(env_raw).expanduser().resolve() if env_raw else None
    if env_ws == cwd_ws.resolve():
        return cwd_ws
    # Override env in place. Lazy-import config to keep bootstrap free
    # of a hard config dependency at module load.
    from core.infra import config as _config
    os.environ["ORCHO_WORKSPACE"] = str(cwd_ws)
    os.environ["ORCHO_RUNSPACE"] = str(cwd_ws / "runspace")
    _config._reset_config()
    if env_raw:
        from core.io.ansi import C as _C
        print(
            f"{_C.YELLOW}{_C.BOLD}  ⚠ workspace overridden: "
            f"using cwd walk-up {cwd_ws}{_C.RESET}",
        )
        print(
            f"{_C.YELLOW}    "
            f"(stale $ORCHO_WORKSPACE = {env_raw} "
            f"ignored — re-source orcho-env.sh to clear)"
            f"{_C.RESET}",
        )
    else:
        print(f"  ↳ workspace auto-derived from cwd: {cwd_ws}")
    return cwd_ws


def detect_workspace_drift(cwd: str | Path | None = None) -> WorkspaceDrift | None:
    """Return drift info when ``$ORCHO_WORKSPACE`` disagrees with cwd walk-up.

    Catches the classic "forgot to re-source orcho-env.sh" foot-gun: the
    operator's shell still carries an old ``ORCHO_WORKSPACE`` (often
    pointing into ``/tmp`` from a disposable demo), but they're working
    in a freshly bootstrapped workspace under a different prefix. The
    run silently lands in the stale workspace instead of the obvious
    one next to their project tree.

    Returns ``None`` when no drift can be diagnosed:
      * ``ORCHO_WORKSPACE`` unset (no env baseline to compare against);
      * cwd walk-up finds no ``workspace-orchestrator/`` sibling (caller
        is somewhere unrelated, can't infer intent);
      * both resolve to the same absolute path.

    Callers (CLI entry points) decide how to surface the warning so
    this helper stays import-cheap and testable.
    """
    env_raw = os.environ.get("ORCHO_WORKSPACE")
    if not env_raw:
        return None
    try:
        env_ws = Path(env_raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    cwd_path = str(cwd) if cwd is not None else os.getcwd()
    cwd_ws = infer_workspace_from_project(cwd_path)
    if cwd_ws is None:
        return None
    if env_ws == cwd_ws.resolve():
        return None
    return WorkspaceDrift(env_workspace=env_ws, cwd_workspace=cwd_ws)


# ── fresh-run dir guard ───────────────────────────────────────────────────


_RUN_DIR_MATERIALIZED_FILES = frozenset({
    "events.jsonl",
    "meta.json",
    "checkpoints.db",
    "metrics.json",
    "progress.log",
    "output.log",
    "evidence.json",
    "evidence.md",
})
_RUN_DIR_MATERIALIZED_GLOBS = (
    "plan_*.md",
    "todo_*.md",
    "cross_plan.md",
)


def materialized_run_artifacts(run_dir: Path) -> list[str]:
    """Return artifacts proving ``run_dir`` already belongs to a run."""
    if not run_dir.exists() or not run_dir.is_dir():
        return []

    found: set[str] = set()
    for name in _RUN_DIR_MATERIALIZED_FILES:
        if (run_dir / name).exists():
            found.add(name)
    for pattern in _RUN_DIR_MATERIALIZED_GLOBS:
        for path in run_dir.glob(pattern):
            if path.is_file():
                found.add(path.name)
    return sorted(found)


def assert_fresh_run_dir_available(
    output_dir: Path | None,
    *,
    resume_from: str | None,
    preallocated_output_dir: bool = False,
) -> None:
    """Fail fast when a fresh run id points at an existing materialized run.

    Empty/pre-created directories are allowed so external supervisors can
    mint an id and directory before spawning Orcho. A typed parent coordinator
    can additionally mark its own handoff-artifact directory as preallocated;
    this does not imply checkpoint-resume semantics. All other reuse of a
    directory that already contains run artifacts requires ``--resume``.
    """
    if output_dir is None or resume_from or preallocated_output_dir:
        return
    artifacts = materialized_run_artifacts(output_dir)
    if not artifacts:
        return
    sample = ", ".join(artifacts[:5])
    if len(artifacts) > 5:
        sample += f", +{len(artifacts) - 5} more"
    raise RunIdCollisionError(
        f"run id already exists at {output_dir}; found existing run artifacts: "
        f"{sample}. Use --resume {output_dir.name!r} to continue this run, "
        "or choose a new --run-id for a fresh run."
    )


# ── run-id resolution + logging setup ────────────────────────────────────


def resolve_run_id_and_setup_logging(
    *,
    task: str,
    project_dir: str,
    resume_from: str | None,
    output_dir: Path | None,
    profile_name: str,
    parent_run_id: str | None = None,
    project_alias: str | None = None,
    plan_source: str = "local",
    projected_profile: str | None = None,
    presentation: PresentationPolicy = PresentationPolicy.TERMINAL,
    preallocated_output_dir: bool = False,
) -> str:
    """Resolve the run_id (resume > $ORCHO_RUN_ID > direct caller output
    dir > minted timestamp), set up file-based run logging, and emit
    ``run.start`` to the event-store.

    Returns the resolved ``session_ts`` used as checkpoint key, meta.json
    folder name, and event_store run_dir tag. See P2.5 contract for the
    full priority chain — ``$ORCHO_RUN_ID`` lets external supervisors
    pre-create the run folder and have the spawned process keep the same
    identifier.

    ``profile_name`` (str) is recorded directly on the ``run.start``
    event's ``profile`` field.
    """
    _env_run_id = os.environ.get("ORCHO_RUN_ID", "").strip() or None
    _direct_run_id = output_dir.name if output_dir is not None else None
    session_ts = (
        resume_from
        or _env_run_id
        or _direct_run_id
        or datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    assert_fresh_run_dir_available(
        output_dir,
        resume_from=resume_from,
        preallocated_output_dir=preallocated_output_dir,
    )
    is_sub_pipeline = False
    if (
        output_dir is not None
        and _is_sub_pipeline_check()
        and _logging._progress_log
    ):
        parent_run_dir = _logging._progress_log.parent.resolve()
        current_run_dir = output_dir.resolve()
        try:
            current_run_dir.relative_to(parent_run_dir)
        except ValueError:
            is_sub_pipeline = False
        else:
            is_sub_pipeline = current_run_dir != parent_run_dir
    setup_run_logging(
        output_dir,
        session_ts,
        is_sub_pipeline=is_sub_pipeline,
        is_resume=bool(resume_from),
        # ADR 0046 Phase F follow-up (site 18 — inventory miss caught
        # by Phase F test 3): the two grey ``📄 Live output`` /
        # ``📡 Events`` chips inside ``setup_run_logging`` are CLI
        # courtesy lines; silent callers (cross / future direct-library
        # UI) suppress. ``set_progress_log`` / ``init_event_store`` /
        # ``set_agent_log`` always fire (ADR 0046 stop #9 — file +
        # event sinks are never gated by presentation).
        terminal=presentation is PresentationPolicy.TERMINAL,
    )
    # REA-3.6: child runs spawned by the cross orchestrator carry
    # ``parent_run_id`` + ``project_alias`` so MCP / evidence consumers can
    # reconstruct the parent → children timeline from events alone.
    payload: dict[str, Any] = {
        "task":     task[:500],
        "run_kind": "single_project",
        "project":  str(Path(project_dir).resolve()),
        "profile":  profile_name,
    }
    if parent_run_id:
        payload["parent_run_id"] = parent_run_id
    if project_alias:
        payload["project_alias"] = project_alias
    # ``plan_source`` and ``projected_profile`` distinguish a regular
    # single-project run from a child run dispatched by the cross
    # orchestrator with a projected child profile. Recording both lets
    # MCP / evidence reconstruct the cross-vs-mono context without
    # filesystem inference.
    if plan_source and plan_source != "local":
        payload["plan_source"] = plan_source
    if projected_profile:
        payload["projected_profile"] = projected_profile
    _events.emit("run.start", **payload)
    return session_ts


# ── session init + atexit guard + halted-resume refusal ───────────────────


def init_session_with_atexit(
    *,
    task: str,
    project_path: Path,
    plugin: PluginConfig,
    model: str,
    profile_name: str,
    session_mode: SessionMode,
    change_handoff: str,
    output_dir: Path | None,
    plan_source: str = "local",
    projected_profile: str | None = None,
    resume_mode: str | None = None,
    followup_parent_run_id: str | None = None,
    followup_parent_run_dir: str | None = None,
    followup_parent_status: str | None = None,
    followup_base_task: str | None = None,
    plan_source_run_id: str | None = None,
) -> dict:
    """Build the session dict, write meta.json early, register the atexit
    hook that marks status="interrupted" on abnormal exit.

    The atexit hook captures ``output_dir`` and the returned ``session``
    via default-args. When the orchestrator later mutates
    ``session["status"]`` on normal finish, the hook reads the updated
    value and stays a no-op. SIGKILL bypasses atexit entirely — there
    the early meta.json write is the only safety net.

    Raises :class:`PhaseHandoffHaltedError` when ``resume_from`` points
    at a run whose prior meta.json records a phase-handoff halt; halt
    is terminal by contract, so re-launching the same run id would
    silently degrade into "fresh dispatch on a halted run".
    """
    session = {
        "task": task,
        "project": str(project_path),
        "plugin": plugin.name,
        "model": model,
        "profile": profile_name,
        "plan_source": plan_source,
        "change_handoff": change_handoff,
        "session_mode_requested": session_mode.value,
        "timestamp": datetime.now().isoformat(),
        "status": "running",
        "phases": {},
    }
    if projected_profile:
        session["projected_profile"] = projected_profile
    # Follow-up context: persisted so MCP / dashboards can reconstruct
    # the parent → follow-up timeline from meta.json alone. Only written
    # when the run is actually a follow-up; checkpoint resumes and fresh
    # runs leave these keys absent.
    #
    # First slice scope: this is *metadata-only*. The new run starts with
    # an empty session and a fresh agent context — there is no runtime
    # continuation of the parent's chained sessions. A future slice can
    # consume ``parent_run_dir`` / ``base_task`` to seed prompt context
    # or chain sessions; this code path persists the linkage so that
    # future work doesn't need a meta.json migration.
    if resume_mode:
        session["resume_mode"] = resume_mode
    if followup_parent_run_id:
        session["parent_run_id"] = followup_parent_run_id
    if followup_parent_run_dir:
        session["parent_run_dir"] = followup_parent_run_dir
    if followup_parent_status:
        session["parent_status"] = followup_parent_status
    if followup_base_task:
        session["base_task"] = followup_base_task
    # ``--from-run-plan`` derivation: when the plan was inherited from
    # a parent run, stamp ``plan_source_run_id`` so evidence /
    # dashboards / SDK consumers can correlate child → parent without
    # rescanning the disk. ``plan_source="run"`` is set separately
    # (above) — the two fields together describe the source.
    if plan_source_run_id:
        session["plan_source_run_id"] = plan_source_run_id
    # Phase 4 resume: read prior meta.json BEFORE overwriting it so we
    # can (a) carry forward the active ``phase_handoff`` payload to the
    # dispatch-time resume helper, and (b) refuse to resume a run that
    # was already terminated via a phase-handoff halt — halt is terminal
    # by contract, and the SDK clears ``meta.phase_handoff`` as part of
    # the halt transition, so without this guard the lost payload would
    # silently degrade into "fresh dispatch on a halted run". Fresh runs
    # (no prior meta) and resumes from non-paused / non-halted states
    # are no-ops.
    if output_dir is not None:
        prior_meta_path = output_dir / "meta.json"
        if prior_meta_path.exists():
            try:
                prior_meta = json.loads(
                    prior_meta_path.read_text(encoding="utf-8"),
                )
            except (OSError, json.JSONDecodeError):
                prior_meta = None
            if isinstance(prior_meta, dict):
                prior_status = prior_meta.get("status")
                prior_reason = prior_meta.get("halt_reason")
                if (
                    prior_status == "halted"
                    and prior_reason == "phase_handoff_halt"
                ):
                    raise PhaseHandoffHaltedError(
                        f"Cannot resume run {output_dir.name!r}: a prior "
                        "phase-handoff decision halted the run "
                        f"(status={prior_status!r}, "
                        f"halt_reason={prior_reason!r}). Halt is "
                        "terminal — start a new run instead."
                    )
                if (
                    prior_status == "halted"
                    and prior_reason == "phase_handoff_unattended_halt"
                ):
                    from pipeline.project.resume_control import (
                        prepare_unattended_handoff_rearm,
                    )

                    rearm = prepare_unattended_handoff_rearm(prior_meta)
                    session["phase_handoff"] = rearm.handoff
                    session["phase_handoff_unattended"] = prior_meta[
                        "phase_handoff_unattended"
                    ]
                    # Internal, one-pass signal for profile dispatch. It is
                    # removed by the ordinary pause tail before the re-armed
                    # state becomes durable.
                    session["_resume_unattended_handoff_rearm"] = True
                prior_handoff = prior_meta.get("phase_handoff")
                if isinstance(prior_handoff, dict) and not session.get(
                    "_resume_unattended_handoff_rearm"
                ):
                    session["phase_handoff"] = prior_handoff
                # ADR 0101 / T2 (fix F1): carry a persisted operator
                # ``runtime_override`` forward into the fresh session so the
                # ``save_session`` below — which rewrites ``meta.json`` from the
                # new session dict — does not drop it. ``RunService.resume``
                # persisted the record into this run dir's ``meta.json`` before
                # resume; ``_read_persisted_runtime_override`` already consumed
                # it for runtime resolution, but the durable record must also
                # survive the resume so a later resume / SDK / evidence read
                # still sees the operator's decision. Idempotent: a plain resume
                # (no record) leaves the key absent.
                prior_override = prior_meta.get("runtime_override")
                if isinstance(prior_override, dict):
                    session["runtime_override"] = prior_override
                prior_phases = prior_meta.get("phases")
                if isinstance(prior_phases, dict):
                    session["phases"].update(prior_phases)
    if output_dir:
        with contextlib.suppress(OSError):
            save_session(output_dir, session)

        def _on_exit_mark_interrupted(
            _output_dir=output_dir, _session=session,
        ) -> None:
            if _session.get("status") == "running":
                # ``halt_reason`` mirrors the SDK halt path + finalize
                # state.halt path so any non-``done`` terminal status
                # carries a non-null reason. ``"interrupted"`` is the
                # honest minimal label: atexit fires on graceful
                # SIGTERM, KeyboardInterrupt, unhandled exception, and
                # parent-process death — without a signal handler we
                # cannot distinguish, so a more specific tag (e.g.
                # ``"cancelled_sigterm"``) would over-claim. Downstream
                # consumers (SDK resume-gate, MCP wire, dashboards)
                # that key off ``meta.halt_reason`` now see something
                # for this class of terminations too. An active
                # ``phase_handoff`` is preserved — an interrupted run
                # with an undecided handoff needs an operator decision.
                mark_run_interrupted(
                    _session, interrupted_at=datetime.now().isoformat(),
                )
                with contextlib.suppress(Exception):
                    save_session(_output_dir, _session)
        atexit.register(_on_exit_mark_interrupted)
    return session


# ── checkpoint hydration ─────────────────────────────────────────────────


def _resume_decision_replay(
    run_dir: Path | None,
) -> tuple[str | None, str | None]:
    """Best-effort ``(action, feedback)`` for the summary resume replay field.

    Reads the active handoff id from ``meta.phase_handoff`` and loads the
    persisted decision artifact — the SAME artifact the resume replays — via
    the shared decision loader. Returns ``(None, None)`` when the resume is
    not a handoff/retry_feedback replay or the decision cannot be read yet, so
    the presenter simply omits the decision-replay field.

    The round *result* is intentionally not surfaced here: this runs during
    checkpoint hydration, before the replayed round executes, so the outcome
    is not yet known. Only the persisted decision (action + operator feedback)
    is real at this point.
    """
    if run_dir is None:
        return None, None
    meta_path = run_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    handoff = meta.get("phase_handoff")
    if not isinstance(handoff, dict):
        return None, None
    handoff_id = handoff.get("id")
    if not handoff_id:
        return None, None
    try:
        from pipeline.project.handoff import load_handoff_decision_validated
        decision = load_handoff_decision_validated(run_dir, str(handoff_id))
    except Exception:
        # Presentation-only: a missing/corrupt artifact still fails the real
        # resume path downstream — the banner must not be the crash site.
        return None, None
    return decision.action, (decision.feedback or None)


def init_checkpoint_with_resume(
    *,
    output_dir: Path | None,
    resume_from: str | None,
    session_ts: str,
    task: str,
    project_path: Path,
    model: str,
    profile_name: str,
    max_rounds: int,
    change_handoff: str,
    session: dict,
    presentation: PresentationPolicy = PresentationPolicy.TERMINAL,
) -> CheckpointStore | None:
    """Initialise CheckpointStore + hydrate ``session`` with resume-state's
    phase log when ``resume_from`` matches a prior run.

    Without hydration, e.g. a ``task``-profile resume after a validate_plan
    quality-gate Approve would emit a meta.json without the rejected
    validate_plan rounds — even though they are right there in
    checkpoints.db.

    ``presentation`` (ADR 0046 Phase C, site 15) gates the
    "Resuming from checkpoint: …" ``success(...)`` line. Default
    ``TERMINAL`` preserves CLI / SDK back-compat byte-identical;
    library callers that built a request with ``presentation=SILENT``
    thread it through so the resume banner stays silent. The
    checkpoint hydration itself (state load + session merge + meta
    rebuild) is unchanged under either policy.
    """
    if not output_dir:
        return None
    _ckpt = CheckpointStore(
        output_dir / "checkpoints.db",
        run_id=resume_from or session_ts,
    )
    _ckpt.save_config({
        "task": task,
        "project": str(project_path),
        "model": model,
        "profile": profile_name,
        "change_handoff": change_handoff,
        "max_rounds": max_rounds,
    })
    if resume_from:
        _resumed_state = _ckpt.load(resume_from)
        if _resumed_state.completed:
            if presentation is PresentationPolicy.TERMINAL:
                from core.observability.logging import get_output_mode
                if get_output_mode() == "summary":
                    # Summary: a one-line resume banner via the presenter. The
                    # decision-replay field is sourced from the actually
                    # persisted handoff decision artifact (the same one resume
                    # replays), not a synthetic state field — a plain
                    # quality-gate resume with no active handoff omits it.
                    from core.io import summary_lines
                    _action, _fb = _resume_decision_replay(output_dir)
                    print(summary_lines.resume_line(
                        len(_resumed_state.completed),
                        decision_action=_action,
                        decision_feedback=_fb,
                    ))
                else:
                    success(
                        f"Resuming from checkpoint: {len(_resumed_state.completed)} "
                        f"phases completed ({', '.join(_resumed_state.completed)})"
                    )
            for _ph_key, _ph_val in (_resumed_state.phases or {}).items():
                session["phases"].setdefault(_ph_key, _ph_val)
            meta_path = output_dir / "meta.json"
            with contextlib.suppress(OSError, json.JSONDecodeError):
                _meta = json.loads(meta_path.read_text(encoding="utf-8"))
                _meta_phases = _meta.get("phases")
                if isinstance(_meta_phases, dict):
                    for _ph_key, _ph_val in _meta_phases.items():
                        session["phases"].setdefault(_ph_key, _ph_val)
            # The canonical signal that a run paused on a phase handoff
            # lives in ``meta.phase_handoff`` (written by the handoff
            # pause helper) and is consumed by ``orcho_run_resume`` plus
            # the SDK readers.
    return _ckpt


# ── future typed result (Phase I consumer; remove in J if unused) ────────


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Typed bundle of the bootstrap outputs Phase I's app facade will
    return to its callers.

    Phase C ships this type but does NOT construct a
    ``BootstrapResult`` from the helpers yet — ``run_pipeline`` keeps
    calling them individually for now. Phase I composes them into a
    single ``init_project_run(request) -> BootstrapResult`` function
    and Phase J removes this dataclass if Phase I does not actually
    consume it (same discipline as ``ProjectRunDeps``).
    """

    session_ts: str
    output_dir: Path | None
    run_id: str
    session: dict
    checkpoint: Any  # CheckpointStore | None
    is_resume: bool


__all__ = [
    "BootstrapResult",
    "PhaseHandoffHaltedError",
    "RunIdCollisionError",
    "WorkspaceDrift",
    "assert_fresh_run_dir_available",
    "autoderive_workspace_from_cwd",
    "detect_workspace_drift",
    "infer_workspace_from_project",
    "init_checkpoint_with_resume",
    "init_session_with_atexit",
    "materialized_run_artifacts",
    "resolve_run_id_and_setup_logging",
]
