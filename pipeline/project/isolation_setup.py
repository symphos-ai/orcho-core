"""Per-run isolation setup for the project pipeline.

The typed project entry surface (``pipeline/project/app.py``) isolates a
run before any agent edits land: it resolves the effective git root,
decides the pre-run dirty intake (ADR 0044), materialises the per-run
worktree checkout (ADR 0033), resolves the sandbox policy (ADR 0034), and
publishes both the active-checkout and sandbox policies on ContextVars so
agent runtimes pick them up at dispatch. This module owns that work.

It is split in two because the session-init call (run_setup) sits between
the parts and consumes ``plan_source_run_id``:

* :func:`resolve_isolation_inputs` runs **before** session init. It
  resolves ``git_root``, the ``--from-run-plan`` correlation id, and the
  parent worktree snapshot — all inputs the session init + the worktree
  resolver need.
* :func:`setup_isolation` runs **after** session init. It mutates the
  (already-created) ``session`` dict in place and calls ``save_session``
  at the same points the inline body did, returns a typed
  :class:`IsolationSetup`, and signals an early halt via
  :attr:`IsolationSetup.halted` so the coordinator reproduces the
  ``return (session, output_dir, session_ts)`` early-exit without changing
  any session mutation or ``save_session`` ordering. Worktree / sandbox
  config errors raise under SILENT and ``print_error`` + ``sys.exit(2)``
  under TERMINAL, byte-identical to the inline body.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.infra import config
from pipeline.engine import save_session
from pipeline.project.types import PresentationPolicy
from pipeline.run_state.terminal import mark_run_halted


@dataclasses.dataclass(frozen=True)
class IsolationInputs:
    """Worktree / correlation inputs resolved before session init."""

    git_root: Path
    plan_source_run_id: str | None
    followup_parent_worktree: dict[str, Any] | None
    resume_worktree_decision: Any = None


@dataclasses.dataclass(frozen=True)
class IsolationSetup:
    """Resolved isolation state for one run.

    ``halted`` is ``True`` when the pre-run dirty intake (or its seed)
    halted the run; the coordinator returns ``(session, output_dir,
    session_ts)`` immediately and the remaining fields are unset. On the
    normal path ``halted`` is ``False`` and the worktree / sandbox /
    ContextVar fields are populated.
    """

    halted: bool
    worktree_ctx: Any = None
    git_cwd: str | None = None
    wt_cvar_token: Any = None
    sb_cvar_token: Any = None
    sandbox_policy: Any = None
    pre_run_dirty: Any = None


@contextlib.contextmanager
def scoped_isolation_id(session_ts: str):
    """Expose one run's resolved isolation id for its full lifecycle.

    The id is process-scoped because both worktree bootstrap and scheduled
    verification commands inherit ``os.environ``.  Restore the caller's
    ambient value on every exit so sequential direct, cross, and resumed runs
    cannot leak identity into one another.
    """
    previous = os.environ.get("ORCHO_ISOLATION_ID")
    os.environ["ORCHO_ISOLATION_ID"] = session_ts
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ORCHO_ISOLATION_ID", None)
        else:
            os.environ["ORCHO_ISOLATION_ID"] = previous


def resolve_isolation_inputs(
    *,
    project_path: Path,
    from_run_plan_loaded: Any | None,
    followup_parent_run_id: str | None,
    from_run_plan_parent_dir: Path | None,
    followup_parent_run_dir: Path | None,
    resume_from: str | None = None,
    output_dir: Path | None = None,
) -> IsolationInputs:
    """Resolve git root, ``--from-run-plan`` correlation id, parent
    worktree snapshot, and the checkpoint-resume retained-subject decision —
    the isolation inputs needed before session init.

    ``resume_from`` / ``output_dir`` drive the retained-subject classification
    (:func:`pipeline.project.resume_worktree.resolve_resume_worktree`). It is
    read here, strictly **before** ``init_run_session`` overwrites the prior
    run's ``meta.json``, so the persistent ``meta.worktree`` block (and the
    review-retry signal) survive to the worktree resolver.
    """
    # GWT-1: effective git root is read from the workspace config (ADR 0062)
    # so the project can be registered with a nested git_dir without touching
    # plugin.py. Falls back to project_path itself (no nested repo).
    from pipeline.project.project_aliases import load_workspace_project_git_dir as _lwpgd
    _git_dir_rel = _lwpgd(project_path).strip()
    git_root = project_path / _git_dir_rel if _git_dir_rel else project_path

    # ``--from-run-plan`` correlation: prefer the explicit
    # followup_parent_run_id (threaded by the CLI) and fall back to
    # the parent directory name when only the path was supplied
    # (e.g. by an embedder calling run_pipeline directly without
    # the CLI). Stamped on the session so meta.json carries it.
    plan_source_run_id: str | None = None
    if from_run_plan_loaded is not None:
        plan_source_run_id = (
            followup_parent_run_id
            or from_run_plan_parent_dir.name
        )

    followup_parent_worktree: dict[str, Any] | None = None
    if followup_parent_run_dir is not None:
        followup_parent_worktree = {}
        with contextlib.suppress(Exception):
            parent_meta_path = Path(followup_parent_run_dir) / "meta.json"
            parent_meta = json.loads(parent_meta_path.read_text(encoding="utf-8"))
            parent_worktree = parent_meta.get("worktree")
            if isinstance(parent_worktree, dict):
                followup_parent_worktree = parent_worktree

    # Checkpoint-resume retained subject (review-retry continuity). Classified
    # from the prior persistent meta + decision artifacts; ``None`` for fresh
    # runs, non-resumes, and the passthrough classes. Reading happens here
    # because ``init_run_session`` (next) overwrites ``meta.json``.
    from pipeline.project.resume_worktree import resolve_resume_worktree
    resume_worktree_decision = resolve_resume_worktree(
        resume_from=resume_from,
        output_dir=output_dir,
        project_dir=git_root,
    )

    return IsolationInputs(
        git_root=git_root,
        plan_source_run_id=plan_source_run_id,
        followup_parent_worktree=followup_parent_worktree,
        resume_worktree_decision=resume_worktree_decision,
    )


def setup_isolation(
    *,
    session: dict,
    output_dir: Path | None,
    session_ts: str,
    git_root: Path,
    followup_parent_worktree: dict[str, Any] | None,
    worktree_config_override: dict[str, Any] | None,
    v2_profile: Any,
    resume_mode: str | None,
    resume_from: str | None,
    no_interactive: bool,
    parent_run_id: str | None,
    project_alias: str | None,
    followup_parent_run_id: str | None,
    followup_parent_run_dir: str | Path | None,
    worktree_bootstrap_config: Any,
    presentation: PresentationPolicy,
    resume_worktree_decision: Any = None,
    from_run_plan_parent_dir: Path | None = None,
) -> IsolationSetup:
    """Resolve pre-run dirty intake, worktree isolation, and sandbox policy.

    Mutates ``session`` in place (``pre_run_dirty`` / ``worktree`` /
    ``sandbox`` keys, ``status`` / ``halt_reason`` on halt) and calls
    ``save_session`` at the same points the inline body did. Returns
    :class:`IsolationSetup`; ``halted=True`` means the caller must return
    the run early.
    """
    # ── GWT-1: resolve per-run worktree isolation (ADR 0033) ─────────────────
    # Resolution order: CLI override → profile.worktree_isolation → config.worktree.
    # When output_dir is None (no run dir on disk) we can't materialise a
    # checkout; force off so the resolver doesn't fail on a None path.
    # Worktree creation probes the actual git root (git_root) — that is
    # project_path/workspace_git_dir when set (ADR 0062), or project_path itself.
    from pipeline.engine.worktree import (
        WorktreeConfigError,
        resolve_worktree_for_run,
        set_active_worktree_checkout,
    )
    _effective_worktree_config: dict[str, Any]
    if worktree_config_override is not None:
        _effective_worktree_config = worktree_config_override
    elif output_dir is None:
        _effective_worktree_config = {"enabled": False}
    else:
        _effective_worktree_config = config.AppConfig.load().worktree
    _profile_isolation: str | None = getattr(v2_profile, "worktree_isolation", None)

    # ── Follow-up worktree continuity (diff-aware) ──────────────────────────
    # Decide BEFORE the worktree resolver — and therefore strictly before any
    # write phase — how a follow-up picks up its parent's change session. A
    # dirty parent worktree is reused; an undelivered diff that survives only
    # as a diff.patch artifact blocks the run (this run does not replay diff
    # artifacts and must not silently start on a clean HEAD); otherwise a fresh
    # HEAD checkout is allowed. Classification itself lives in
    # ``followup_worktree`` so this setup stays thin.
    # A direct ``--from-run-plan`` child stamps follow-up parent fields only for
    # lineage/meta correlation. Worktree continuity is a stricter follow-up
    # contract; plan-artifact continuation must allocate a fresh checkout.
    _effective_followup_parent_worktree = (
        followup_parent_worktree if resume_mode == "followup" else None
    )
    _followup_decision: Any = None
    if resume_mode == "followup":
        from pipeline.project.followup_worktree import classify_followup_worktree
        _parent_run_dir = (
            Path(followup_parent_run_dir)
            if followup_parent_run_dir is not None
            else None
        )
        # Plan-artifact continuation signal: the run is a plan-artifact
        # continuation ONLY when it was actually promoted to (or invoked as)
        # ``--from-run-plan`` — i.e. ``from_run_plan_parent_dir`` is now set on
        # the request (the single promotion chokepoint decided this, and already
        # rejected contradictory child profiles). Deriving the signal from the
        # promotion result — not merely from the parent owning a parsed_plan.json
        # — keeps this classification consistent with whether setup_profile
        # actually loaded the plan and stripped the planning block. The diff
        # branches still win first (strict continuity). ``source_checkout`` lets
        # the classifier tell an off-isolation / source-checkout parent from a
        # genuine isolated worktree.
        _plan_artifact_continuation = from_run_plan_parent_dir is not None
        _followup_decision = classify_followup_worktree(
            parent_run_dir=_parent_run_dir,
            followup_parent_worktree=followup_parent_worktree,
            parent_has_persisted_plan=_plan_artifact_continuation,
            source_checkout=git_root,
        )
        if _followup_decision.blocked:
            # Persist the blocked classification into the session worktree
            # block BEFORE stopping the run, so the decision (mode_label /
            # blocked / reason / diff_source) survives in meta.json even
            # though no checkout was materialised — the success path below
            # writes ``followup_continuity`` only after a worktree resolves.
            session.setdefault("worktree", {})["followup_continuity"] = (
                _followup_decision.to_dict()
            )
            if output_dir is not None:
                with contextlib.suppress(Exception):
                    save_session(output_dir, session)
            # Same surface as the worktree-config error branch below: SILENT
            # re-raises the typed error for the library caller; TERMINAL keeps
            # the red stderr line + rc=2 exit. Either way the run stops here,
            # before any checkout is materialised.
            if presentation is PresentationPolicy.SILENT:
                raise WorktreeConfigError(_followup_decision.block_message)
            from pipeline.project.app import print_error
            print_error(_followup_decision.block_message)
            sys.exit(2)
        _effective_followup_parent_worktree = (
            _followup_decision.effective_parent_worktree
        )

    # ── Checkpoint-resume retained-subject continuity (review-retry) ─────────
    # Decide BEFORE the worktree resolver — strictly before any write phase —
    # whether this checkpoint-resume must reuse the retained worktree that holds
    # a rejected diff. Classification lives in ``resume_worktree`` so this setup
    # stays thin. ``blocked`` (recorded worktree gone while a review-retry needs
    # it) stops the run with a recoverable operator error, naming the missing
    # path, before any checkout is materialised; otherwise the retained subject
    # is handed to the resolver to attach by recorded path (never wt_<run_id>).
    # Mutually exclusive with the follow-up path: a checkpoint resume carries
    # ``resume_from`` and ``resume_mode != 'followup'``.
    _resume_prior_worktree: dict[str, Any] | None = None
    if resume_worktree_decision is not None and resume_mode != "followup":
        if resume_worktree_decision.blocked:
            # Leave the run DECIDABLE and re-resumable, not torn. session-init
            # already overwrote meta with status='running' and dropped the
            # prior worktree subject. Restore the full prior ``meta.worktree``
            # block (path / isolation / base_ref) so a later resume — after the
            # operator restores the retained worktree — can find and reuse it,
            # and the recoverable error keeps naming the real path. Re-assert
            # the awaiting_phase_handoff status (the active handoff payload was
            # carried forward by session-init) so SDK decide/halt accept the
            # run and the atexit hook does not mark it 'interrupted'. The
            # ``resume_continuity`` sub-block is added additively for audit.
            _prior_block = resume_worktree_decision.prior_worktree
            if isinstance(_prior_block, dict):
                session["worktree"] = dict(_prior_block)
            session.setdefault("worktree", {})["resume_continuity"] = (
                resume_worktree_decision.to_dict()
            )
            if isinstance(session.get("phase_handoff"), dict):
                session["status"] = "awaiting_phase_handoff"
            if output_dir is not None:
                with contextlib.suppress(Exception):
                    save_session(output_dir, session)
            if presentation is PresentationPolicy.SILENT:
                raise WorktreeConfigError(resume_worktree_decision.block_message)
            from pipeline.project.app import print_error
            print_error(resume_worktree_decision.block_message)
            sys.exit(2)
        _resume_prior_worktree = resume_worktree_decision.retained_subject

    # ADR 0044: if the source checkout is dirty and this fresh run would
    # otherwise start from HEAD in an isolated worktree, resolve the
    # intake decision before creating the worktree. ``include`` applies
    # its snapshot immediately after the checkout exists.
    from pipeline.engine.pre_run_dirty import (
        PreRunDirtyIntake,
        apply_pre_run_dirty_seed,
        resolve_pre_run_dirty_intake,
    )
    if resume_mode == "followup" and _effective_followup_parent_worktree is not None:
        _pre_run_dirty = PreRunDirtyIntake(
            action="none",
            status="not_applicable",
            reason="follow-up continues parent worktree",
        )
    elif _resume_prior_worktree is not None:
        _pre_run_dirty = PreRunDirtyIntake(
            action="none",
            status="not_applicable",
            reason="checkpoint resume continues retained worktree",
        )
    else:
        _pre_run_dirty = resolve_pre_run_dirty_intake(
            project_dir=git_root,
            run_dir=output_dir,
            run_id=session_ts,
            pre_run_config=config.AppConfig.load().pre_run_dirty,
            worktree_config=_effective_worktree_config,
            profile_isolation=_profile_isolation,
            resume_from=resume_from,
            no_interactive=no_interactive,
        )
    if _pre_run_dirty.action != "none":
        session["pre_run_dirty"] = _pre_run_dirty.to_dict()
        if output_dir is not None:
            with contextlib.suppress(Exception):
                save_session(output_dir, session)
    if _pre_run_dirty.status in {"halted", "commit_failed"}:
        mark_run_halted(session, halt_reason="pre_run_dirty_halt")
        if output_dir is not None:
            with contextlib.suppress(Exception):
                save_session(output_dir, session)
        if presentation is PresentationPolicy.TERMINAL:
            from pipeline.project.app import print_error
            print_error(_pre_run_dirty_halt_message(_pre_run_dirty))
        return IsolationSetup(halted=True)

    # Cross children carry ``parent_run_id`` (the cross run id) + a stable
    # ``run_id`` = alias (the ``wt_<alias>`` path contract). The git branch
    # lives in the shared source-repo ref namespace, so a per-alias branch
    # collides on the second cross run in a workspace → ``git worktree add
    # -b`` fails → silent degrade to in-place (agent edits the source). Give
    # the branch a per-cross-run namespace so each cross run gets a fresh
    # branch while the checkout path stays ``wt_<alias>``.
    _branch_run_id: str | None = (
        f"{parent_run_id}__{session_ts}" if parent_run_id else None
    )
    try:
        _worktree_ctx = resolve_worktree_for_run(
            run_id=session_ts,
            project_dir=git_root,
            run_dir=output_dir or git_root,
            worktree_config=_effective_worktree_config,
            profile_isolation=_profile_isolation,
            followup_parent_worktree=_effective_followup_parent_worktree,
            followup_parent_run_id=followup_parent_run_id,
            resume_prior_worktree=_resume_prior_worktree,
            branch_run_id=_branch_run_id,
        )
    except WorktreeConfigError as exc:
        # ADR 0046 Phase C (site 5): under SILENT, re-raise the
        # typed config error; the library caller catches and renders.
        # Under TERMINAL, the CLI behaviour stays byte-identical
        # (red stderr line + rc=2 exit).
        if presentation is PresentationPolicy.SILENT:
            raise
        from pipeline.project.app import print_error
        print_error(f"Worktree config error: {exc}")
        sys.exit(2)
    # Cross worktree degrade is LOUD, never silent. A cross child
    # (``parent_run_id`` set) whose isolation was REQUESTED but degraded
    # (``git worktree add`` failed / stale unregistered path) carries a
    # ``degraded_reason``. Running implement/review in-place would edit the
    # user's SOURCE checkout while the cross gates diff the (empty)
    # worktree — the silent isolation-loss bug. Fail the child loudly so
    # the cross dispatch records the alias as failed instead of shipping
    # source edits. An explicit ``worktree.enabled=false`` is a clean off
    # (no ``degraded_reason``) and stays the operator's choice.
    if parent_run_id and _worktree_ctx.degraded_reason:
        raise WorktreeConfigError(
            f"cross child [{project_alias or session_ts}] worktree isolation "
            f"degraded and was refused: {_worktree_ctx.degraded_reason}. "
            f"Refusing to run implement/review in the source checkout "
            f"{git_root} — that would edit the user's tree while the cross "
            f"gates review an empty worktree. Most common cause: a leftover "
            f"orcho/run/* branch or worktree from a prior cross run; prune "
            f"it (git worktree remove / git branch -D) and rerun."
        )
    # Thread the worktree path as the agent working directory. When mode="off",
    # ctx.path == git_root so behaviour is identical to the pre-GWT-1 path
    # (ADR 0033). When active, ctx.path is the isolated checkout.
    if _pre_run_dirty.action == "include":
        if not _worktree_ctx.is_isolated:
            _pre_run_dirty = _pre_run_dirty.with_status(
                "seed_failed",
                error="pre-run dirty include requires an isolated worktree",
            )
        else:
            _pre_run_dirty = apply_pre_run_dirty_seed(
                _pre_run_dirty,
                project_dir=git_root,
                worktree_path=_worktree_ctx.path,
            )
        session["pre_run_dirty"] = _pre_run_dirty.to_dict()
        if output_dir is not None:
            with contextlib.suppress(Exception):
                save_session(output_dir, session)
        if _pre_run_dirty.status == "seed_failed":
            mark_run_halted(session, halt_reason="pre_run_dirty_seed_failed")
            if output_dir is not None:
                with contextlib.suppress(Exception):
                    save_session(output_dir, session)
            return IsolationSetup(halted=True)

    if worktree_bootstrap_config:
        _apply_worktree_bootstrap(
            config=worktree_bootstrap_config,
            session=session,
            output_dir=output_dir,
            git_root=git_root,
            worktree_ctx=_worktree_ctx,
            presentation=presentation,
        )

    git_cwd = str(_worktree_ctx.path)
    # Correction follow-ups have a stronger continuity contract than generic
    # follow-ups: they repair the retained parent change, never a newly-created
    # checkout. Verify the resolver honoured the exact recorded subject before
    # publishing the child worktree in durable metadata.
    if (
        resume_mode == "followup"
        and getattr(v2_profile, "name", None) == "correction"
        and _followup_decision is not None
        and _followup_decision.diff_source == "worktree"
    ):
        parent_path = (followup_parent_worktree or {}).get("path")
        try:
            exact_parent = parent_path is not None and Path(parent_path).resolve() == _worktree_ctx.path.resolve()
        except OSError:
            exact_parent = False
        if not exact_parent:
            raise WorktreeConfigError(
                "correction follow-up must reuse the exact retained parent worktree",
            )
    session["worktree"] = _worktree_ctx.to_dict()
    # Persist the follow-up continuity classification into the session
    # worktree block so the chosen mode (reuse / clean HEAD) stays
    # inspectable, and surface the mode line to the operator (TERMINAL
    # only — SILENT library callers read it from the session instead).
    if _followup_decision is not None:
        session["worktree"]["followup_continuity"] = _followup_decision.to_dict()
        if presentation is not PresentationPolicy.SILENT:
            from core.io.ansi import C, paint
            print(paint(f"worktree: {_followup_decision.mode_label}", C.CYAN))
    # Mirror the retained-subject decision into the session worktree block as an
    # additive ``resume_continuity`` sub-block so the reused path / source stays
    # inspectable in meta.json (the reuse path itself is recorded by the
    # resolver; this records WHY it was reused).
    if (
        _resume_prior_worktree is not None
        and resume_worktree_decision is not None
    ):
        session["worktree"]["resume_continuity"] = (
            resume_worktree_decision.to_dict()
        )
        if presentation is not PresentationPolicy.SILENT:
            from core.io.ansi import C, paint
            print(paint(f"worktree: {resume_worktree_decision.mode_label}", C.CYAN))
    # F2 fix: register the active checkout path via ContextVar so agent
    # runtimes can gate the destructive-git guardrail against the exact
    # validated path, not a fragile basename heuristic.
    _wt_cvar_token = set_active_worktree_checkout(
        str(_worktree_ctx.path) if _worktree_ctx.is_isolated else None
    )

    # ── ADR 0034: resolve per-run sandbox policy ─────────────────────────────
    # Global ``sandbox`` block from config.defaults.json + per-profile
    # override. Resolver returns a frozen :class:`SandboxPolicy`; we
    # store it on a ContextVar so every agent runtime picks it up at
    # dispatch via ``get_active_sandbox_policy()``. L1 only — the
    # schema accepts ``mode=off`` (no isolation) and ``mode=env``
    # (allowlist + rlimit + masking + child-process cleanup).
    from pipeline.sandbox import (
        detect_capabilities,
        set_active_sandbox_policy,
    )
    from pipeline.sandbox.resolver import (
        SandboxConfigError,
        resolve_sandbox_policy,
    )
    _sandbox_caps = detect_capabilities()
    _profile_sandbox = getattr(v2_profile, "sandbox", None)
    try:
        _sandbox_policy = resolve_sandbox_policy(
            global_config=config.AppConfig.load().sandbox,
            profile_override=_profile_sandbox,
        )
    except SandboxConfigError as exc:
        # ADR 0046 Phase C (site 6): same shape as site 5 — typed
        # config error re-raised under SILENT, CLI behaviour
        # preserved under TERMINAL.
        if presentation is PresentationPolicy.SILENT:
            raise
        from pipeline.project.app import print_error
        print_error(f"Sandbox config error: {exc}")
        sys.exit(2)
    # ADR 0034: the run-manifest block carries the resolved policy,
    # the env-allowlist effective union, a count of how many parent
    # env variables get stripped (computed once at resolve time —
    # ``os.environ`` is the only authoritative source and does not
    # change mid-run), the masking config snapshot, and the
    # capability detection result. The orchestrator may overwrite
    # ``limit_hit`` later when an agent dispatch trips a resource
    # cap; PR1 ships only the schema slot so SDK/MCP consumers
    # already see the shape.
    from pipeline.sandbox.backends._env_filter import compute_child_env
    _env_effective, _env_stripped = compute_child_env(
        dict(os.environ), _sandbox_policy,
    )
    session["sandbox"] = {
        "mode": _sandbox_policy.mode.value,
        "limits": {
            "cpu_seconds": _sandbox_policy.limits.cpu_seconds,
            "memory_mb": _sandbox_policy.limits.memory_mb,
            "open_files": _sandbox_policy.limits.open_files,
            "file_size_mb": _sandbox_policy.limits.file_size_mb,
        },
        "env_allowlist_effective": sorted(_env_effective.keys()),
        "env_stripped_count": _env_stripped,
        "masking": {
            "builtin_patterns": _sandbox_policy.masking.builtin_patterns,
            "custom_patterns": len(_sandbox_policy.masking.custom_patterns),
        },
        "capabilities": _sandbox_caps.to_manifest(),
        "limit_hit": None,
    }
    _sb_cvar_token = set_active_sandbox_policy(_sandbox_policy)

    return IsolationSetup(
        halted=False,
        worktree_ctx=_worktree_ctx,
        git_cwd=git_cwd,
        wt_cvar_token=_wt_cvar_token,
        sb_cvar_token=_sb_cvar_token,
        sandbox_policy=_sandbox_policy,
        pre_run_dirty=_pre_run_dirty,
    )


def _pre_run_dirty_halt_message(intake: Any) -> str:
    """Build a compact operator-facing explanation for a dirty-checkout halt."""
    reason = getattr(intake, "reason", None) or "dirty checkout requires intake"
    details = _format_pre_run_dirty_paths(
        "changed", getattr(intake, "changed_paths", ()),
    )
    details += _format_pre_run_dirty_paths(
        "untracked", getattr(intake, "untracked_paths", ()),
    )
    path_details = f" Paths: {'; '.join(details)}." if details else ""
    return (
        f"Dirty working tree halted before worktree creation: {reason}."
        f"{path_details} Commit or stash your changes, then rerun; or rerun "
        "with --no-worktree-isolation."
    )


def _format_pre_run_dirty_paths(
    label: str,
    paths: tuple[str, ...],
    *,
    limit: int = 3,
) -> list[str]:
    """Return a bounded path summary suitable for one terminal line."""
    if not paths:
        return []
    examples = ", ".join(paths[:limit])
    remaining = len(paths) - limit
    if remaining > 0:
        examples += f", +{remaining} more"
    return [f"{label} ({len(paths)}): {examples}"]


def _apply_worktree_bootstrap(
    *,
    config: Any,
    session: dict,
    output_dir: Path | None,
    git_root: Path,
    worktree_ctx: Any,
    presentation: PresentationPolicy,
) -> None:
    """Run plugin-declared bootstrap steps once a checkout is ready."""
    if not worktree_ctx.is_isolated:
        session["worktree_bootstrap"] = {
            "status": "skipped",
            "reason": "worktree isolation is off",
        }
        _save_session_quietly(output_dir, session)
        return

    from pipeline.engine.worktree_bootstrap import (
        WorktreeBootstrapError,
        run_worktree_bootstrap,
    )
    try:
        if presentation is PresentationPolicy.TERMINAL:
            reporter = _WorktreeBootstrapReporter(clock=time.monotonic)
            bootstrap_result = run_worktree_bootstrap(
                config,
                source_root=git_root,
                worktree_path=worktree_ctx.path,
                on_step=reporter.on_step,
            )
        else:
            bootstrap_result = run_worktree_bootstrap(
                config,
                source_root=git_root,
                worktree_path=worktree_ctx.path,
            )
    except WorktreeBootstrapError as exc:
        session["worktree_bootstrap"] = {
            "status": "failed",
            "error": str(exc),
        }
        mark_run_halted(session, halt_reason="worktree_bootstrap_failed")
        _save_session_quietly(output_dir, session)
        if presentation is PresentationPolicy.SILENT:
            raise
        from pipeline.project.app import print_error
        print_error(f"Worktree bootstrap failed: {exc}")
        sys.exit(2)
    if bootstrap_result.get("status") != "skipped":
        if presentation is PresentationPolicy.TERMINAL:
            reporter.finish()
        session["worktree_bootstrap"] = bootstrap_result
        _save_session_quietly(output_dir, session)


@dataclasses.dataclass
class _WorktreeBootstrapReporter:
    """Render transient bootstrap progress without changing durable state."""

    clock: Callable[[], float]
    started_at: float | None = None
    step_starts: dict[int, tuple[str, float]] = dataclasses.field(default_factory=dict)

    def on_step(
        self,
        stage: str,
        index: int,
        action: str,
        payload: Mapping[str, Any],
    ) -> None:
        if stage == "start":
            self._start(index, action, payload)
        elif stage == "complete":
            self._complete(index, payload)

    def _start(self, index: int, action: str, step: Mapping[str, Any]) -> None:
        now = self.clock()
        if self.started_at is None:
            from core.io.transcript import render_phase_header
            print(render_phase_header("SETUP", "Worktree bootstrap"))
            self.started_at = now
        label = _bootstrap_step_label(action, step)
        self.step_starts[index] = (label, now)
        print(f"  {index}. {label} …")

    def _complete(self, index: int, record: Mapping[str, Any]) -> None:
        label, started_at = self.step_starts.pop(index)
        elapsed = self.clock() - started_at
        status = "skipped" if record.get("status") == "skipped" else "done"
        print(f"  {index}. {label} {status} ({elapsed:.2f}s)")

    def finish(self) -> None:
        if self.started_at is not None:
            elapsed = self.clock() - self.started_at
            print(f"  Worktree bootstrap complete ({elapsed:.2f}s)")


def _bootstrap_step_label(action: str, step: Mapping[str, Any]) -> str:
    """Return a bounded, provider-neutral terminal label for one declaration."""
    if action == "run":
        command = step.get("run", step.get("command", step.get("cmd", "")))
        return _bounded_bootstrap_text(_format_bootstrap_command(command))
    if action == "copy":
        source = step.get("from", step.get("copy", ""))
        target = step.get("to")
        label = f"copy {source}" if not target else f"copy {source} → {target}"
        return _bounded_bootstrap_text(label)
    if action == "python":
        return _bounded_bootstrap_text(f"python {step.get('python', step.get('script', ''))}")
    if action == "shell":
        return _bounded_bootstrap_text(f"shell {step.get('shell', step.get('cmd', ''))}")
    return _bounded_bootstrap_text(action)


def _format_bootstrap_command(command: Any) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, Sequence) and not isinstance(command, bytes):
        return " ".join(str(part) for part in command)
    return str(command)


def _bounded_bootstrap_text(text: str, *, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def _save_session_quietly(output_dir: Path | None, session: dict) -> None:
    if output_dir is not None:
        with contextlib.suppress(Exception):
            save_session(output_dir, session)


__all__ = [
    "IsolationInputs",
    "IsolationSetup",
    "resolve_isolation_inputs",
    "setup_isolation",
]
