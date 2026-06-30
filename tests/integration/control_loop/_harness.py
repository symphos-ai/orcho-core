"""Local control-loop harness drivers (test-only, strictly additive).

Each driver runs a real ``run_pipeline`` mock run (or the real
non-interactive delivery-defer settle path) with a FIXED run id in a temp run
dir, then returns ``(run_id, run_dir, meta)`` where ``meta`` is the *real*
artifact the run left on disk — never a hand-authored dict.

Covered lifecycle states (the names mirror the SDK ``condition`` vocabulary
in ``sdk/run_control/diagnosis.py``):

* ``active`` — running-meta captured mid-run, while ``status='running'`` is
  still on disk (before finalization). Captured by intercepting the first
  agent ``invoke`` (the plan agent) and reading ``meta.json`` from disk at
  that moment. The meta is the real product written by
  ``init_session_with_atexit`` in ``pipeline/project/bootstrap.py`` — it
  carries ``status='running'`` and no ``halt_reason`` / ``phase_handoff`` /
  ``parent_run_id``.
* ``resume_inert_terminal`` — clean review+release run → ``status='done'``.
* ``needs_decision`` — ``profile_name='planning'`` → the
  ``human_feedback_always`` handoff pauses non-interactively at
  ``status='awaiting_phase_handoff'`` with a ``phase_handoff`` in meta.
* ``needs_delivery_decision`` — ADR 0099/0100 non-interactive delivery defer
  (``commit.decision_mode='defer'`` + ``no_interactive``) parks an APPROVED
  release as a decidable ``commit_delivery_pending`` halt.
* ``correction_followup_required`` — APPROVED review/plan loop, REJECTED
  closing release gate → ``status='halted'`` /
  ``halt_reason='final_acceptance_rejected'``.
* ``failed`` — codex raises ``RuntimeError`` → the producer persists
  ``status='failed'`` to meta then re-raises; the driver catches and reads the
  persisted meta.

Determinism / serial-safety: mock provider with ``latency=0.0``, a fixed run
id, a temp run dir per driver, and a logging/event-singleton reset around
every run. The consuming test module marks the slice ``project_run`` +
``serial``.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agents.runtimes import MockAgentProvider
from pipeline.control.resume_context import load_resume_meta
from pipeline.plugins import PluginConfig
from pipeline.project_orchestrator import run_pipeline

# ── Fixed identifiers (determinism) ──────────────────────────────────────────

FIXED_RUN_ID = "20260502_000000"

PLUGIN = PluginConfig(
    name="Control Loop Harness Project",
    language="Python",
    architecture="FastAPI + SQLAlchemy",
    file_hints=["src/", "tests/"],
)


# ── Git fixture ──────────────────────────────────────────────────────────────

def init_git_repo(path: Path) -> None:
    """Initialize ``path`` as a git repo with one committed file.

    The pipeline engine requires ``project_dir`` to be a real git repo so
    worktree isolation can attach; a bare tmp dir hits a hard WorktreeConfig
    fail. Mirrors the acceptance fixture's local repo bootstrap.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=path, check=True,
    )
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


# ── Reviewer/release JSON payloads (local copies of the parser contract) ─────
# Defined locally rather than imported from ``tests/acceptance`` — those are
# test-private symbols, not a stable API. The shapes mirror the orcho review /
# release parser contracts.

def _approved_review_json(summary: str = "No blocking issues.") -> str:
    return json.dumps({
        "verdict": "APPROVED",
        "short_summary": summary,
        "findings": [],
        "risks": [],
        "checks": ["Reviewed change"],
    })


def _approved_release_json(summary: str = "Ship-ready.") -> str:
    """ADR 0025: release-gate APPROVED payload for the project
    ``final_acceptance`` reviewer prompt."""
    return json.dumps({
        "verdict": "APPROVED",
        "ship_ready": True,
        "short_summary": summary,
        "release_blockers": [],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces": "not_applicable",
            "persistence": "not_applicable",
            "tests": "sufficient",
        },
    })


def _rejected_release_json(
    summary: str = "Blocking defect in the delivery path.",
) -> str:
    """ADR 0025/0106: release-gate REJECTED payload. The review/plan loop
    stays APPROVED; only the closing release gate rejects, carrying a real
    blocker so the rejected terminal has blockers to surface."""
    return json.dumps({
        "verdict": "REJECTED",
        "ship_ready": False,
        "short_summary": summary,
        "release_blockers": [{
            "id": "RB1",
            "severity": "P0",
            "title": "Data loss on apply",
            "body": "The change drops a column without a migration guard.",
            "required_fix": "Guard the destructive write behind a migration.",
            "why_blocks_release": "Shipping would destroy user data.",
        }],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "incomplete",
            "interfaces": "broken",
            "persistence": "risky",
            "tests": "missing",
        },
    })


def _prompt_requests_release(prompt: str) -> bool:
    """Detect the ``release_json`` system-tail block in a reviewer prompt —
    matches the orcho renderer output exactly (a bare-token substring search
    would false-positive on a review prompt that mentions the word)."""
    return (
        'kind="contract"' in prompt
        and 'name="release_json"' in prompt
        and "<orcho:system-block " in prompt
    )


# ── Codex stubs (local) ──────────────────────────────────────────────────────

class _CleanCodex:
    """APPROVED review/plan loop AND APPROVED closing release gate."""

    model = "stub-codex"
    session_id: str | None = None

    def invoke(self, prompt: str, cwd: str, **_kw: Any) -> str:
        if _prompt_requests_release(prompt):
            return _approved_release_json()
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


class _ApprovedReviewRejectedReleaseCodex:
    """APPROVED review/plan loop, REJECTED closing release gate (ADR 0106).

    review_changes + validate_plan pass, so the run reaches
    ``final_acceptance`` with a real diff, where the release gate rejects —
    driving the rejected-release correction terminal.
    """

    model = "stub-codex"
    session_id: str | None = None

    def invoke(self, prompt: str, cwd: str, **_kw: Any) -> str:
        if _prompt_requests_release(prompt):
            return _rejected_release_json()
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


class _CrashingCodex:
    """Raises on every invoke — drives the producer's failure path so meta is
    persisted with ``status='failed'`` (then the exception re-raises)."""

    model = "stub-codex"
    session_id: str | None = None

    def invoke(self, prompt: str, cwd: str, **_kw: Any) -> str:
        raise RuntimeError("control-loop harness: forced codex crash")

    def reset_session(self) -> None:
        self.session_id = None


class _ApprovedReviewMalformedReleaseCodex:
    """APPROVED review/plan loop, MALFORMED closing release payload.

    review_changes + validate_plan pass; ``final_acceptance`` then receives
    non-JSON prose, which is a hard contract-parse failure. The final_acceptance
    handler treats that as a protocol break and calls ``state.stop`` — driving
    the **state.halt** terminal that ``finalization._resolve_terminal_status``
    settles to ``halted`` + the nested ``halt`` compat block (the first of the
    two migrated reducer sites).
    """

    model = "stub-codex"
    session_id: str | None = None

    def invoke(self, prompt: str, cwd: str, **_kw: Any) -> str:
        if _prompt_requests_release(prompt):
            return "not a release verdict — just prose the parser must reject"
        return _approved_review_json()

    def reset_session(self) -> None:
        self.session_id = None


# ── Provider builders (local) ────────────────────────────────────────────────

def build_clean_provider() -> MockAgentProvider:
    """Clean review + clean release → terminal ``done``."""
    p = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    p.codex = lambda model, **_kw: _CleanCodex()
    return p


def build_rejected_release_provider() -> MockAgentProvider:
    """APPROVED review/plan, REJECTED release → halted/final_acceptance_rejected."""
    p = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    p.codex = lambda model, **_kw: _ApprovedReviewRejectedReleaseCodex()
    return p


def build_crashing_provider() -> MockAgentProvider:
    """Codex raises → run reaches terminal ``failed``."""
    p = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    p.codex = lambda model, **_kw: _CrashingCodex()
    return p


def build_malformed_release_provider() -> MockAgentProvider:
    """APPROVED review/plan, MALFORMED release → state.halt → halted + nested halt."""
    p = MockAgentProvider(latency=0.0, test_pass_rate=1.0)
    p.codex = lambda model, **_kw: _ApprovedReviewMalformedReleaseCodex()
    return p


# ── External-surface patches (local; mirror the acceptance pattern) ──────────

def _patches() -> tuple:
    """Context managers that isolate the single-project pipeline from the OS.

    Local re-implementation of the acceptance ``_patches`` shape; the
    test-private acceptance symbol is intentionally not imported.
    """
    return (
        patch("pipeline.project.session_run.load_plugin", return_value=PLUGIN),
        patch("core.io.git_helpers.has_uncommitted", return_value=True),
        patch("core.io.git_helpers.git_diff_stat", return_value="1 file changed"),
    )


@contextlib.contextmanager
def _defer_delivery_config():
    """Force ``commit.decision_mode='defer'`` for the run (ADR 0100).

    Wraps the real ``AppConfig.load`` so every other config field stays
    file-loaded; only the commit decision mode is overridden. ``AppConfig`` is
    cached forever, so this returns a fresh ``dataclasses.replace`` copy with a
    *copied* commit dict — mutating the shared cached dict in place would leak
    ``decision_mode='defer'`` into every later run. Patches the import site the
    producer reads (``pipeline.project.run.config``).
    """
    import dataclasses

    from core.infra import config as _config

    real_load = _config.AppConfig.load

    def _deferred_load():
        cfg = real_load()
        new_commit = {**cfg.commit, "decision_mode": "defer"}
        return dataclasses.replace(cfg, commit=new_commit)

    with patch("pipeline.project.run.config.AppConfig.load", _deferred_load):
        yield


@contextlib.contextmanager
def _stub_delivery_diff():
    """Give the delivery resolver a real owned diff to park.

    The mock implement phase does not necessarily mutate the worktree, so the
    delivery resolver would otherwise see an empty diff and return
    ``not_applicable`` instead of a parkable ``pending``. Stubbing the three
    git surfaces (the same ones the defer unit test stubs) yields a real
    ``pending`` decision; the defer halt that follows is genuine producer
    output, not a hand-authored meta.
    """
    import pipeline.engine.commit_delivery as cd

    diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    with (
        patch.object(cd, "_run_owned_patch", lambda *_a, **_k: diff),
        patch.object(cd, "_changed_paths", lambda *_a, **_k: ("a.py",)),
        patch.object(cd, "_untracked_paths", lambda *_a, **_k: ()),
    ):
        yield


@contextlib.contextmanager
def _pin_run_id(run_id: str):
    """Pin ``$ORCHO_RUN_ID`` so a run's ``session_ts`` equals its own dir name.

    These tests execute inside an Orcho-managed run whose ambient
    ``$ORCHO_RUN_ID`` would otherwise win the ``session_ts`` priority chain
    (``bootstrap._resolve_session_ts``: ``resume_from`` → ``$ORCHO_RUN_ID`` →
    ``output_dir.name``), so every harness run — parent and child alike — would
    share that one ambient id. Pinning each run to its own dir name keeps the
    lineage ids distinct, so the cross-run ``superseded_by_followup.child_run_id``
    marker (which the seam derives from the child's ``session_ts``) references the
    actual child rather than the ambient supervisor id.
    """
    with patch.dict(os.environ, {"ORCHO_RUN_ID": run_id}):
        yield


@contextlib.contextmanager
def _mutate_isolated_worktree(content: str = "delivered-correction-marker\n"):
    """Write a real tracked-file change into a run's isolated worktree.

    The mock implement phase does not mutate the worktree, so the delivery
    resolver would otherwise compute an empty owned diff and return ``no_diff``
    — never reaching a *delivered* status. Mirrors the acceptance
    ``resolve_worktree_for_run`` injection (``test_worktree_e2e``): right after
    the per-run worktree is created we modify the tracked ``.gitkeep`` so git
    diff picks up a genuine run-owned change. That diff applies cleanly to the
    (clean) project checkout, so the default auto delivery commits it and
    ``commit_delivery.status`` settles to ``committed`` (∈ DELIVERED_STATUSES).
    """
    import pipeline.engine.worktree as _wt

    real_resolve = _wt.resolve_worktree_for_run

    def _resolve_and_mutate(**kwargs: Any) -> Any:
        ctx = real_resolve(**kwargs)
        if ctx.is_isolated:
            (ctx.path / ".gitkeep").write_text(content, encoding="utf-8")
        return ctx

    with patch(
        "pipeline.engine.worktree.resolve_worktree_for_run",
        side_effect=_resolve_and_mutate,
    ):
        yield


def _no_diff_rejected_patches() -> tuple:
    """Force the genuine verify-only no-diff REJECTED final-acceptance shape.

    These are LOCAL extra-patches layered on top of the shared ``_patches()``
    (which forces ``has_uncommitted=True``); they are NOT a change to that shared
    table. Together they model a run that produced no reviewable/deliverable diff
    yet whose implement evidence is incomplete:

    * ``final_acceptance._no_uncommitted_review_target`` → True drives the
      handler's synthetic no-diff branch (``_write_no_diff_final_acceptance``),
      which records ``review_target='not_applicable'`` + ``diff='none'``.
    * ``final_acceptance._implement_evidence_complete`` → False makes that
      synthetic verdict ``REJECTED`` (the no-diff *rejected* shape, not approved).
    * ``capture_run_diff_with_apply_check`` → None makes ``diff_path`` None, so
      finalization's post-delivery reducer (``_apply_no_diff_final_acceptance_outcome``
      → ``terminal_outcome.apply_no_diff_terminal``) actually reconciles the
      no-diff outcome instead of seeing a captured diff and short-circuiting.

    The resulting durable terminal is ``halted`` / ``final_acceptance_no_diff``
    with the ``no_op_outcome`` marker — the second migrated reducer site.
    """
    return (
        patch(
            "pipeline.phases.builtin.handlers.final_acceptance."
            "_no_uncommitted_review_target",
            return_value=True,
        ),
        patch(
            "pipeline.phases.builtin.handlers.final_acceptance."
            "_implement_evidence_complete",
            return_value=False,
        ),
        patch(
            "pipeline.engine.diff_apply_check.capture_run_diff_with_apply_check",
            return_value=None,
        ),
    )


# ── Logging / event singleton reset (serial-safety) ──────────────────────────

def _reset_run_globals() -> None:
    """Reset progress-log / agent-log / event-store globals around a run.

    These module singletons leak across runs in one process; resetting before
    and after every harness run keeps each run's event/log stream
    self-contained. Mirrors the acceptance test's ``_reset_test_globals``.
    """
    import agents.stream as _stream
    import core.observability.events as _events
    import core.observability.logging as _logging_module

    _logging_module._progress_log = None
    _stream._agent_log = None
    _events.clear_phase_context()
    _events.init_event_store(None)


# ── Running-meta captor (active state) ───────────────────────────────────────

class _RunningMetaCaptor:
    """Provider wrapper that snapshots ``meta.json`` at the first agent invoke.

    The engine resolves every agent through ``provider.resolve(...)`` (and the
    named ``claude``/``codex``/``gemini`` shims). Agent construction is
    side-effect free; the first real ``invoke()`` happens inside the first
    phase (``plan``), AFTER ``init_session_with_atexit`` has written the
    running-meta and BEFORE any finalization. Reading ``meta.json`` at that
    moment captures the real ``status='running'`` artifact from disk.
    """

    def __init__(self, inner: Any, run_dir: Path) -> None:
        self._inner = inner
        self._run_dir = run_dir
        self.captured: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:
        # Delegate everything not explicitly wrapped (e.g. ``run_tests``).
        return getattr(self._inner, name)

    def resolve(self, runtime: str, model: str, *, effort: str | None = None) -> Any:
        return self._wrap(self._inner.resolve(runtime, model, effort=effort))

    def claude(self, model: str, *, effort: str | None = None) -> Any:
        return self._wrap(self._inner.claude(model, effort=effort))

    def codex(self, model: str, *, effort: str | None = None) -> Any:
        return self._wrap(self._inner.codex(model, effort=effort))

    def gemini(self, model: str, *, effort: str | None = None) -> Any:
        return self._wrap(self._inner.gemini(model, effort=effort))

    def _wrap(self, agent: Any) -> Any:
        if getattr(agent, "_orcho_captor_wrapped", False):
            return agent
        real_invoke = agent.invoke
        captor = self

        def _invoke(*args: Any, **kwargs: Any) -> Any:
            if captor.captured is None:
                meta_path = captor._run_dir / "meta.json"
                if meta_path.exists():
                    captor.captured = json.loads(meta_path.read_text())
            return real_invoke(*args, **kwargs)

        agent.invoke = _invoke
        agent._orcho_captor_wrapped = True
        return agent


# ── Driver result + run helpers ──────────────────────────────────────────────

@dataclass(frozen=True)
class DriverResult:
    """A real run reduced to its durable read-model inputs."""

    run_id: str
    run_dir: Path
    meta: dict[str, Any]


def _make_run_dir(base: Path) -> Path:
    d = base / "runs" / FIXED_RUN_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_settled_meta(run_dir: Path) -> dict[str, Any]:
    """Read the real persisted ``meta.json`` via the prod resume helper."""
    resumed = load_resume_meta(run_dir)
    if resumed is None:
        raise AssertionError(f"no meta.json persisted at {run_dir}")
    return dict(resumed.meta)


def _run_once(
    *,
    task: str,
    project: Path,
    run_dir: Path,
    provider: Any,
    profile_name: str = "feature",
    max_rounds: int = 1,
    no_interactive: bool = False,
    extra_patches: tuple = (),
) -> dict:
    """Run one real mock pipeline with the OS-isolation patches applied."""
    _reset_run_globals()
    try:
        with contextlib.ExitStack() as stack:
            for cm in _patches():
                stack.enter_context(cm)
            for cm in extra_patches:
                stack.enter_context(cm)
            return run_pipeline(
                task=task,
                project_dir=str(project),
                output_dir=run_dir,
                max_rounds=max_rounds,
                profile_name=profile_name,
                no_interactive=no_interactive,
                provider=provider,
            )
    finally:
        _reset_run_globals()


def _setup(base: Path) -> tuple[Path, Path]:
    """Build a git project + run dir under ``base`` and return ``(project, run_dir)``."""
    project = base / "proj"
    init_git_repo(project)
    return project, _make_run_dir(base)


# ── Drivers (one per lifecycle state) ────────────────────────────────────────

def drive_resume_inert_terminal(base: Path) -> DriverResult:
    """Clean review+release → terminal ``status='done'`` (inert resume)."""
    project, run_dir = _setup(base)
    _run_once(
        task="Add structured logging",
        project=project,
        run_dir=run_dir,
        provider=build_clean_provider(),
    )
    return DriverResult(FIXED_RUN_ID, run_dir, _read_settled_meta(run_dir))


def drive_needs_decision(base: Path) -> DriverResult:
    """``profile_name='planning'`` → ``status='awaiting_phase_handoff'`` with a
    ``phase_handoff`` block (decidable human handoff)."""
    project, run_dir = _setup(base)
    _run_once(
        task="Plan a structured logging change",
        project=project,
        run_dir=run_dir,
        provider=build_clean_provider(),
        profile_name="planning",
        max_rounds=2,
        no_interactive=True,
    )
    return DriverResult(FIXED_RUN_ID, run_dir, _read_settled_meta(run_dir))


def drive_needs_delivery_decision(base: Path) -> DriverResult:
    """Non-interactive delivery defer (ADR 0099/0100) → ``status='halted'`` /
    ``halt_reason='commit_delivery_pending'`` with a decidable delivery gate."""
    project, run_dir = _setup(base)
    _run_once(
        task="Add structured logging",
        project=project,
        run_dir=run_dir,
        provider=build_clean_provider(),
        no_interactive=True,
        extra_patches=(_defer_delivery_config(), _stub_delivery_diff()),
    )
    return DriverResult(FIXED_RUN_ID, run_dir, _read_settled_meta(run_dir))


def drive_correction_followup_required(base: Path) -> DriverResult:
    """APPROVED review/plan, REJECTED release → ``status='halted'`` /
    ``halt_reason='final_acceptance_rejected'`` (correction follow-up)."""
    project, run_dir = _setup(base)
    _run_once(
        task="Add structured logging",
        project=project,
        run_dir=run_dir,
        provider=build_rejected_release_provider(),
    )
    return DriverResult(FIXED_RUN_ID, run_dir, _read_settled_meta(run_dir))


def drive_failed(base: Path) -> DriverResult:
    """Codex crashes → producer persists ``status='failed'`` then re-raises;
    the driver catches and reads the persisted meta."""
    project, run_dir = _setup(base)
    with contextlib.suppress(RuntimeError):
        _run_once(
            task="Add structured logging",
            project=project,
            run_dir=run_dir,
            provider=build_crashing_provider(),
        )
    return DriverResult(FIXED_RUN_ID, run_dir, _read_settled_meta(run_dir))


def drive_active(base: Path) -> DriverResult:
    """Capture the real running-meta mid-run (``status='running'`` on disk).

    Wraps the clean provider so the first agent ``invoke`` snapshots
    ``meta.json`` while the run is still live; the run then completes normally.
    The returned meta is the captured running-meta, not the settled terminal.
    """
    project, run_dir = _setup(base)
    captor = _RunningMetaCaptor(build_clean_provider(), run_dir)
    _run_once(
        task="Add structured logging",
        project=project,
        run_dir=run_dir,
        provider=captor,
    )
    if captor.captured is None:
        raise AssertionError(
            "running-meta was never captured — no agent invoke observed",
        )
    return DriverResult(FIXED_RUN_ID, run_dir, captor.captured)


# Registry of all drivers, keyed by the SDK ``condition`` family they target.
ALL_DRIVERS: dict[str, Callable[[Path], DriverResult]] = {
    "active": drive_active,
    "resume_inert_terminal": drive_resume_inert_terminal,
    "needs_decision": drive_needs_decision,
    "needs_delivery_decision": drive_needs_delivery_decision,
    "correction_followup_required": drive_correction_followup_required,
    "failed": drive_failed,
}


# ── Migrated-transition parity drivers (ADR 0115 slice 3b-1) ──────────────────
# Pin that the two finalization sites routed through the terminal-outcome reducer
# settle to the SAME durable terminal form as before the migration. Deliberately
# NOT registered in ``ALL_DRIVERS`` (which feeds the SDK condition/eviction
# matrices): these target the reducer's own terminal patch, so they are driven by
# dedicated parity tests rather than the shared state-matrix fixture.


def drive_state_halt_terminal(base: Path) -> DriverResult:
    """state.halt site (``_resolve_terminal_status``) → ``halted`` + nested halt.

    A malformed closing-release payload is a hard final_acceptance contract
    failure → ``state.stop`` → ``run.state.halt`` is set, so finalization's
    pre-delivery ``_resolve_terminal_status`` (now routing through
    ``terminal_outcome.resolve_terminal_outcome``) settles ``status='halted'``
    with the top-level ``halt_reason`` AND the nested ``halt`` compat block
    (``{reason, phase}``) consumers read via ``halt.phase``.
    """
    project, run_dir = _setup(base)
    with contextlib.suppress(RuntimeError):
        _run_once(
            task="Add structured logging",
            project=project,
            run_dir=run_dir,
            provider=build_malformed_release_provider(),
        )
    return DriverResult(FIXED_RUN_ID, run_dir, _read_settled_meta(run_dir))


def drive_no_diff_rejected_terminal(base: Path) -> DriverResult:
    """no-diff site (``_apply_no_diff_final_acceptance_outcome``) → halted no-op.

    A verify-only run with no diff target and incomplete implement evidence →
    synthetic REJECTED final acceptance + no captured diff (``diff_path=None``).
    Finalization's post-delivery reducer (``terminal_outcome.apply_no_diff_terminal``)
    settles ``status='halted'`` / ``halt_reason='final_acceptance_no_diff'`` and
    records the ``no_op_outcome`` display marker. The pre-delivery status was
    ``done`` (no ``state.halt``), so there is NO nested ``halt`` block here —
    distinguishing this reducer branch from the state.halt one.
    """
    project, run_dir = _setup(base)
    _run_once(
        task="Verify the change; produce no new diff",
        project=project,
        run_dir=run_dir,
        provider=build_clean_provider(),
        extra_patches=_no_diff_rejected_patches(),
    )
    return DriverResult(FIXED_RUN_ID, run_dir, _read_settled_meta(run_dir))


# ── Lineage drivers (T3): parent + child runs in one runs_dir ─────────────────
# These need two sibling runs with DISTINCT dir names under one runs_dir, so
# they don't use FIXED_RUN_ID. Both ids are timestamp-sortable (the SDK selects
# the newest follow-up child by id order).

LINEAGE_PARENT_RUN_ID = "20260101_000000"
LINEAGE_CHILD_RUN_ID = "20260102_000000"


def _run_pipeline_isolated(*, provider: Any, extra_patches: tuple = (), **kwargs: Any) -> dict:
    """Run ``run_pipeline(**kwargs)`` with OS-isolation patches + global reset.

    The general-purpose sibling of ``_run_once``: it forwards arbitrary
    ``run_pipeline`` kwargs (e.g. follow-up / from-run-plan lineage params) that
    the fixed-shape ``_run_once`` does not expose.
    """
    _reset_run_globals()
    try:
        with contextlib.ExitStack() as stack:
            for cm in _patches():
                stack.enter_context(cm)
            for cm in extra_patches:
                stack.enter_context(cm)
            return run_pipeline(provider=provider, **kwargs)
    finally:
        _reset_run_globals()


@dataclass(frozen=True)
class SupersededResult:
    """Parent run with a live follow-up child (drives ``superseded_by_child``).

    ``parent_run_id`` is the run to diagnose; ``child_run_id`` is the active
    follow-up the SDK must recommend resuming instead.
    """

    runs_dir: Path
    parent_run_id: str
    parent_meta: dict[str, Any]
    child_run_id: str
    child_meta: dict[str, Any]


@dataclass(frozen=True)
class RecoverResult:
    """Terminal child whose source run is resumable (drives
    ``recover_via_source_run``).

    ``child_run_id`` is the terminal run to diagnose; ``source_running_meta`` is
    the parent's real captured running-meta, supplied through the
    ``run_diagnosis(source_meta=...)`` supervisor seam so the source reads as a
    live, resumable checkpoint.
    """

    runs_dir: Path
    child_run_id: str
    child_meta: dict[str, Any]
    source_run_id: str
    source_running_meta: dict[str, Any]


def drive_superseded_by_child(base: Path) -> SupersededResult:
    """Real parent run + a live (non-terminal) follow-up child in one runs_dir.

    The parent is a clean ``done`` run. The child is a ``planning`` follow-up
    (``resume_mode='followup'``, ``parent_run_id`` stamped) that pauses
    non-interactively at ``awaiting_phase_handoff`` — a status that is NOT a
    terminal-resume-parent, so ``detect_active_followup_child`` sees it as a
    live child and the SDK diagnoses the parent as ``superseded_by_child``.
    """
    runs_dir = base / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    project = base / "proj"
    init_git_repo(project)

    parent_dir = runs_dir / LINEAGE_PARENT_RUN_ID
    parent_dir.mkdir()
    _run_pipeline_isolated(
        provider=build_clean_provider(),
        task="Parent run",
        project_dir=str(project),
        output_dir=parent_dir,
        max_rounds=1,
    )

    child_dir = runs_dir / LINEAGE_CHILD_RUN_ID
    child_dir.mkdir()
    _run_pipeline_isolated(
        provider=build_clean_provider(),
        task="Follow-up child",
        project_dir=str(project),
        output_dir=child_dir,
        profile_name="planning",
        no_interactive=True,
        max_rounds=2,
        resume_mode="followup",
        followup_parent_run_id=LINEAGE_PARENT_RUN_ID,
        followup_parent_run_dir=str(parent_dir),
    )

    return SupersededResult(
        runs_dir=runs_dir,
        parent_run_id=LINEAGE_PARENT_RUN_ID,
        parent_meta=_read_settled_meta(parent_dir),
        child_run_id=LINEAGE_CHILD_RUN_ID,
        child_meta=_read_settled_meta(child_dir),
    )


def drive_recover_via_source_run(base: Path) -> RecoverResult:
    """Real terminal child whose source run is resumable via the source seam.

    The parent is a ``planning`` run: it persists ``parsed_plan.json`` (so the
    child can hydrate via ``from_run_plan``) and its real running-meta is
    captured mid-run. The child is a ``from_run_plan`` ``feature`` run that
    reaches a clean terminal ``done`` and stamps ``plan_source_run_id`` →
    parent. Diagnosing the child with ``source_meta={parent: running_meta}``
    resolves the source as a live, resumable checkpoint →
    ``recover_via_source_run``.
    """
    runs_dir = base / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    project = base / "proj"
    init_git_repo(project)

    parent_dir = runs_dir / LINEAGE_PARENT_RUN_ID
    parent_dir.mkdir()
    captor = _RunningMetaCaptor(build_clean_provider(), parent_dir)
    _run_pipeline_isolated(
        provider=captor,
        task="Parent plan",
        project_dir=str(project),
        output_dir=parent_dir,
        profile_name="planning",
        no_interactive=True,
    )
    if captor.captured is None:
        raise AssertionError("source running-meta was never captured")

    child_dir = runs_dir / LINEAGE_CHILD_RUN_ID
    child_dir.mkdir()
    _run_pipeline_isolated(
        provider=build_clean_provider(),
        task="Child implementation",
        project_dir=str(project),
        output_dir=child_dir,
        profile_name="feature",
        max_rounds=1,
        from_run_plan_parent_dir=parent_dir,
    )

    return RecoverResult(
        runs_dir=runs_dir,
        child_run_id=LINEAGE_CHILD_RUN_ID,
        child_meta=_read_settled_meta(child_dir),
        source_run_id=LINEAGE_PARENT_RUN_ID,
        source_running_meta=captor.captured,
    )


def drive_parent_superseded_by_followup_delivery(
    base: Path, *, child_delivers: bool = True,
) -> SupersededResult:
    """Real rejected-FA parent + a ``from_run_plan`` child, both settled.

    Reproduces the stale-correction bug on a real core→SDK lineage (not seeded
    meta) and proves the no-return guarantee both ways:

    (a) Parent — a real ``feature`` run whose closing release gate REJECTS, so it
        dead-ends at ``halted`` / ``final_acceptance_rejected``. Its plan phase
        persists ``parsed_plan.json``, so the child can hydrate via
        ``from_run_plan``.
    (b) Child — a real ``from_run_plan`` ``feature`` run hydrated off the parent
        (which stamps ``plan_source_run_id`` → parent). With ``child_delivers``
        it runs the clean provider AND mutates its isolated worktree, so the
        default auto delivery commits a genuine run-owned diff
        (``commit_delivery.status='committed'`` ∈ DELIVERED) — driving the
        cross-run parent supersede. The NEGATIVE control runs the rejected
        provider and no worktree mutation, so the child itself rejects and its
        delivery is ``not_applicable`` (∉ DELIVERED); the seam's 'child
        delivered' precondition then leaves the parent halted/rejected.

    Returns the parent + child meta read AFTER both runs settle — the parent meta
    reflects any cross-run rewrite the child's finalize performed on disk.
    """
    runs_dir = base / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    project = base / "proj"
    init_git_repo(project)

    parent_dir = runs_dir / LINEAGE_PARENT_RUN_ID
    parent_dir.mkdir()
    _run_pipeline_isolated(
        provider=build_rejected_release_provider(),
        task="Add structured logging",
        project_dir=str(project),
        output_dir=parent_dir,
        profile_name="feature",
        max_rounds=1,
        extra_patches=(_pin_run_id(LINEAGE_PARENT_RUN_ID),),
    )

    child_dir = runs_dir / LINEAGE_CHILD_RUN_ID
    child_dir.mkdir()
    child_extra: tuple = (_pin_run_id(LINEAGE_CHILD_RUN_ID),)
    if child_delivers:
        child_provider: Any = build_clean_provider()
        child_extra += (_mutate_isolated_worktree(),)
    else:
        child_provider = build_rejected_release_provider()
    _run_pipeline_isolated(
        provider=child_provider,
        task="Deliver the planned correction",
        project_dir=str(project),
        output_dir=child_dir,
        profile_name="feature",
        max_rounds=1,
        from_run_plan_parent_dir=parent_dir,
        extra_patches=child_extra,
    )

    return SupersededResult(
        runs_dir=runs_dir,
        parent_run_id=LINEAGE_PARENT_RUN_ID,
        parent_meta=_read_settled_meta(parent_dir),
        child_run_id=LINEAGE_CHILD_RUN_ID,
        child_meta=_read_settled_meta(child_dir),
    )
