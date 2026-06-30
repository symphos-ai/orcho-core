"""
pipeline/engine/worktree.py — orcho-managed worktree lifecycle (GWT-1).

Domain-level orchestration of git worktree primitives from
:mod:`core.io.git_helpers`. The single-project and cross-project
orchestrators consume :func:`resolve_worktree_for_run` at run init
to obtain a :class:`WorktreeContext`, then route every agent
``invoke(prompt, cwd=...)`` to that context's ``path`` so the user's
source checkout is never mutated by the agent.

See ADR 0033 for the design contract: per-run isolation is the v1
default; per-phase opt-in is schema-valid but runtime-rejected in
v1; ``destructive_git`` guardrail is relaxed inside the orcho
worktree (the worktree IS the agent's playground) and remains
active everywhere else.

This module is pure domain — no I/O of its own beyond
:mod:`core.io.git_helpers`. No subprocess, no os.environ reads.
Caller threads config + paths in; module decides mode + creates /
records / tears down.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from core.io.git_helpers import (
    GitOpResult,
    create_worktree as _create_worktree,
    remove_worktree as _remove_worktree,
)

IsolationMode = Literal["off", "per_run", "per_phase"]

# Supported isolation values. ``per_phase`` is schema-valid (so profile
# JSON can declare intent ahead of v1 implementation) but
# :func:`resolve_worktree_for_run` raises on it — better an explicit
# v1-not-implemented error than a silent fallback that confuses
# DAG-2 callers later.
_KNOWN_ISOLATION_MODES: frozenset[str] = frozenset({"off", "per_run", "per_phase"})

# v1 ships only per-run. Profile-level ``per_phase`` declaration is
# tolerated at load time (the field is reserved) but cannot run.
_V1_SUPPORTED_MODES: frozenset[str] = frozenset({"off", "per_run"})

_DEFAULT_RETENTION_DAYS = 7


class WorktreeConfigError(ValueError):
    """Raised when the worktree config / profile combination is
    structurally invalid (unknown mode, malformed retention, etc.).

    Distinct from a runtime failure to create the worktree (which
    surfaces via :class:`GitOpResult.ok = False`): config errors are
    bugs in the operator's config or profile JSON, not failures of
    the underlying git operation.
    """


@dataclass(frozen=True, slots=True)
class WorktreeContext:
    """Identity and lifecycle metadata for a run's orcho-managed worktree.

    Threading model: the orchestrator constructs one
    :class:`WorktreeContext` at run init via
    :func:`resolve_worktree_for_run` and stores it on the run state.
    Every agent invocation reads ``path`` for ``cwd``. The teardown
    step at run completion calls :func:`teardown_worktree`. The
    context is frozen because mid-run mutation would invalidate the
    cwd contract for already-issued agent calls.

    Shape:

    * ``mode="off"`` — no isolation. ``path == project_dir``.
      ``branch is None``. ``base_ref`` is best-effort (current HEAD
      of project_dir) but the field stays informational. This is
      the legacy pre-GWT-1 behaviour and remains the escape valve
      via ``--no-worktree-isolation`` or ``worktree.enabled=false``.

    * ``mode="per_run"`` — one orcho worktree per fresh run/change
      journey, lifetime = run lifetime + retention TTL. ``path`` lives
      under ``<workspace>/runspace/worktrees/<worktree_id>/checkout``.
      Follow-up runs attach to the parent worktree instead of creating
      a new checkout, preserving provider cwd continuity while keeping
      each run's artefacts in its own ``runs/<run_id>/`` directory.
      ``branch == "orcho/run/<root_run_id>"``. The branch is owned by
      orcho; the operator should not commit to it manually.

    * ``mode="per_phase"`` — schema-valid in v1, runtime-rejected.
      Reserved for DAG-2 parallel waves where each phase forks its
      own worktree from the previous phase's HEAD.

    ``degraded_reason`` is populated for non-fatal isolation
    failures the resolver can still produce — currently a rogue
    pre-existing directory at the target worktree path. The
    fatal cases (``project_dir`` is not a git repo / has no
    commits / git binary unavailable) now raise
    :class:`WorktreeConfigError` instead of degrading silently,
    because later phases would otherwise edit the user's tree
    while review gates ``git diff`` an empty target and pass an
    unreviewed change.
    """

    mode: IsolationMode
    project_dir: Path
    path: Path
    base_ref: str
    branch: str | None = None
    retention_until: str | None = None
    degraded_reason: str | None = None
    worktree_id: str | None = None
    kind: str | None = None
    root_run_id: str | None = None
    source_repo_path: str | None = None
    source_start_head: str | None = None
    manifest_path: Path | None = None

    @property
    def is_isolated(self) -> bool:
        """True when agent cwd differs from the user's source checkout.

        Inverse of ``mode == "off"`` for readability at call sites
        (e.g. command_guard skips destructive-git checks when this
        is True).
        """
        return self.mode != "off" and self.path != self.project_dir

    def to_dict(self) -> dict[str, Any]:
        """Wire-friendly projection for ``meta.json`` / evidence.

        Mirrors the ADR 0033 wire shape:
        ``{isolation, path, base_ref, branch_ref, retention_until,
          degraded_reason}``.

        ``path`` is rendered as a string for JSON friendliness;
        callers that need a ``Path`` re-construct it on read.
        """
        out: dict[str, Any] = {
            "isolation": self.mode,
            "path": str(self.path),
            "base_ref": self.base_ref,
            "branch_ref": self.branch,
        }
        if self.worktree_id is not None:
            out["worktree_id"] = self.worktree_id
        if self.kind is not None:
            out["kind"] = self.kind
        if self.root_run_id is not None:
            out["root_run_id"] = self.root_run_id
        if self.source_repo_path is not None:
            out["source_repo_path"] = self.source_repo_path
        if self.source_start_head is not None:
            out["source_start_head"] = self.source_start_head
        if self.manifest_path is not None:
            out["manifest_path"] = str(self.manifest_path)
        if self.retention_until is not None:
            out["retention_until"] = self.retention_until
        if self.degraded_reason is not None:
            out["degraded_reason"] = self.degraded_reason
        return out


def resolve_worktree_for_run(
    *,
    run_id: str,
    project_dir: Path,
    run_dir: Path,
    worktree_config: dict[str, Any] | None = None,
    profile_isolation: str | None = None,
    followup_parent_worktree: Mapping[str, Any] | None = None,
    followup_parent_run_id: str | None = None,
    resume_prior_worktree: Mapping[str, Any] | None = None,
    branch_run_id: str | None = None,
) -> WorktreeContext:
    """Resolve and (if isolated) create the run-owned worktree.

    Inputs:
      * ``run_id``  — the worktree **identity**: drives the checkout id
        (``wt_<run_id>``) and thus the on-disk path. Stays stable per
        logical sub-run so artifact/path consumers can find it.
      * ``branch_run_id``  — optional override for the orcho **branch**
        name (``orcho/run/<branch_run_id>``); falls back to ``run_id``
        when unset. Decoupled from ``run_id`` because the git branch
        lives in the SHARED source-repo ref namespace, so it must be
        unique across runs even when ``run_id`` is intentionally stable.
        Cross children pass ``<parent_run_id>__<alias>`` here: their
        ``run_id`` is the bare alias (stable per alias for the
        ``wt_<alias>`` path contract finalization/delivery rely on), but
        a stable branch would collide on ``git worktree add -b`` on the
        second cross run in a workspace and silently degrade to in-place
        (the agent would then edit the user's source checkout).
      * ``project_dir``  — user's source checkout. Worktree creation runs
        ``git worktree add`` against this repo.
      * ``run_dir``  — destination root for run artefacts. Fresh isolated
        worktrees land under the sibling ``worktrees/`` directory.
      * ``worktree_config``  — the ``commit.defaults["worktree"]`` block
        (or whatever the caller resolved from layered config). Reads
        ``enabled``, ``isolation``, ``retention_days``. Missing keys
        fall back to safe defaults: ``enabled=true``, ``isolation=
        per_run``, ``retention_days=7``.
      * ``profile_isolation``  — optional per-profile override. Profile
        JSON may carry ``worktree_isolation: per_run | per_phase`` to
        tighten beyond the global config. ``per_phase`` parses but
        v1 rejects it at runtime.

    Resolution order: profile_isolation (if set) → worktree_config
    isolation → "per_run" default. ``worktree.enabled=false`` short-
    circuits everything to mode="off".

    Failure modes:
      * Unknown mode in config or profile  →
        :class:`WorktreeConfigError`.
      * ``per_phase`` in v1  →
        :class:`WorktreeConfigError` ("reserved for DAG-2").
      * ``git worktree add`` fails  → returns
        ``WorktreeContext(mode="off", degraded_reason=<git error>)``.
        Caller decides whether to abort or proceed in user's checkout.
      * ``project_dir`` not a git repo with isolation requested  →
        :class:`WorktreeConfigError`. Degrading silently here would
        let later phases edit the user's tree while the review gates
        ``git diff`` an empty non-repo and pass everything.
    """
    config = worktree_config or {}
    if config.get("enabled") is False:
        return _off_context(project_dir)

    mode = _resolve_effective_mode(
        worktree_config=config, profile_isolation=profile_isolation,
    )
    if mode == "off":
        return _off_context(project_dir)

    if mode == "per_phase":
        # Schema accepts; v1 runtime rejects. Better explicit error
        # than silent fallback because per-phase callers (future
        # DAG-2 wave runners) need to know they're on a code path
        # that doesn't exist yet.
        raise WorktreeConfigError(
            "worktree_isolation='per_phase' is reserved for DAG-2; "
            "v1 ships per_run only. Pass 'per_run' or 'off'."
        )

    retention_until = _compute_retention_timestamp(
        retention_days=config.get("retention_days", _DEFAULT_RETENTION_DAYS),
    )

    # per_run path: create worktree (or reuse an existing one on resume).
    base_ref, base_ref_failure = _resolve_base_ref(project_dir)
    if base_ref is None:
        # Hard-fail instead of degrading to ``_off_context``: without
        # an isolated checkout, later phases would edit the user's
        # tree directly while the review gates try to ``git diff`` a
        # missing target and see nothing — the run looks ``ok`` while
        # the change is actually unreviewed. The remediation depends on
        # what specifically failed: a non-repo wants a different fix
        # than an empty repo, which in turn differs from a missing
        # git binary.
        if base_ref_failure == "no_commits":
            raise WorktreeConfigError(
                f"project_dir is a git repository but has no commits "
                f"yet: {project_dir}. Worktree isolation needs an "
                "initial commit. Make one (e.g. `git commit --allow-empty "
                "-m init`) or disable isolation via "
                "`worktree.enabled = false` in your workspace config.",
            )
        if base_ref_failure == "git_unavailable":
            raise WorktreeConfigError(
                f"git is unavailable when probing {project_dir}: "
                "worktree isolation requires a working `git` binary. "
                "Install or fix git in PATH, or disable isolation via "
                "`worktree.enabled = false`.",
            )
        # ``not_a_repo`` is the default — and also the legacy fall-
        # through for anything _resolve_base_ref couldn't classify.
        raise WorktreeConfigError(
            f"project_dir is not a git repository: {project_dir}. "
            "Worktree isolation requires a real git repo. Pass "
            "--project <repo-root>, run `git init` inside the project, "
            "or run `orcho workspace init` to register projects.",
        )

    if followup_parent_worktree is not None:
        parent_ctx = _context_from_recorded_worktree(
            run_id=run_id,
            parent_run_id=followup_parent_run_id,
            project_dir=project_dir,
            parent_worktree=followup_parent_worktree,
            retention_until=retention_until,
        )
        if parent_ctx is not None:
            return parent_ctx
        parent_path = followup_parent_worktree.get("path")
        raise WorktreeConfigError(
            "follow-up parent worktree is not available; refusing to "
            f"start a fresh checkout and lose change-session continuity "
            f"(path={parent_path!r})"
        )

    # Checkpoint-resume retained subject (review-retry continuity). Attach by
    # the recorded worktree path — identical mechanics to a follow-up parent —
    # so the resumed run reuses the exact worktree that holds the rejected
    # diff, NOT a fresh ``wt_<run_id>``. The retained subject is classified
    # upstream (``pipeline.project.resume_worktree``); the resolver only ever
    # receives it when the recorded path was already verified available, so a
    # ``None`` attach here is a genuine race and refuses rather than degrading.
    if resume_prior_worktree is not None:
        prior_ctx = _context_from_recorded_worktree(
            run_id=run_id,
            parent_run_id=_string_value(resume_prior_worktree, "root_run_id"),
            project_dir=project_dir,
            parent_worktree=resume_prior_worktree,
            retention_until=retention_until,
        )
        if prior_ctx is not None:
            return prior_ctx
        prior_path = resume_prior_worktree.get("path")
        raise WorktreeConfigError(
            "retained resume worktree is not available; refusing to start a "
            "fresh checkout and lose the rejected diff subject under review "
            f"(path={prior_path!r})"
        )

    worktree_id = _worktree_id_for_run(run_id)
    target_path = _worktree_checkout_path(run_dir=run_dir, worktree_id=worktree_id)
    branch_name = f"orcho/run/{branch_run_id or run_id}"
    manifest_path = target_path.parent / "manifest.json"

    # Reuse detection: when target_path already exists (e.g. retained worktree
    # from a prior run that is now being resumed), skip create_worktree.
    # All three checks must hold to treat it as a valid orcho worktree:
    #   (a) path exists, (b) .git is a file (worktree gitlink, not a bare repo),
    #   (c) path appears in the source repo's worktree list.
    # If path exists but fails any check, fall through to degraded "off" mode
    # without touching or deleting the existing directory.
    if target_path.exists():
        _git_link = target_path / ".git"
        if _git_link.is_file():
            from core.io.git_helpers import _run_git as _rg  # noqa: PLC0415
            _wl_rc, _wl_out, _ = _rg(
                ["worktree", "list", "--porcelain"], cwd=str(project_dir),
            )
            if _wl_rc == 0:
                _listed = {
                    str(Path(line[len("worktree "):].strip()).resolve())
                    for line in _wl_out.splitlines()
                    if line.startswith("worktree ")
                }
                if str(target_path.resolve()) in _listed:
                    _rr, _sha, _ = _rg(
                        ["rev-parse", "HEAD"], cwd=str(target_path),
                    )
                    _reuse_base_ref = _sha.strip() if _rr == 0 else base_ref
                    _write_manifest(
                        manifest_path,
                        _manifest_payload(
                            worktree_id=worktree_id,
                            kind="primary",
                            root_run_id=run_id,
                            attached_run_ids=[run_id],
                            source_repo_path=str(project_dir),
                            source_start_head=_reuse_base_ref,
                            checkout_path=target_path,
                            branch_ref=branch_name,
                            base_ref=_reuse_base_ref,
                            retention_until=retention_until,
                        ),
                    )
                    return WorktreeContext(
                        mode="per_run",
                        project_dir=project_dir,
                        path=target_path,
                        base_ref=_reuse_base_ref,
                        branch=branch_name,
                        retention_until=retention_until,
                        worktree_id=worktree_id,
                        kind="primary",
                        root_run_id=run_id,
                        source_repo_path=str(project_dir),
                        source_start_head=_reuse_base_ref,
                        manifest_path=manifest_path,
                    )
        return _off_context(
            project_dir,
            degraded_reason=(
                f"{target_path} exists but is not a registered orcho worktree "
                f"(expected a .git gitlink file with a matching worktree entry); "
                f"falling back to user's checkout"
            ),
        )

    result: GitOpResult = _create_worktree(
        repo=project_dir,
        base_ref=base_ref,
        target_path=target_path,
        branch_name=branch_name,
    )
    if not result.ok:
        return _off_context(
            project_dir,
            degraded_reason=(
                f"git worktree add failed ({result.error}); "
                f"falling back to user's checkout"
            ),
        )

    _write_manifest(
        manifest_path,
        _manifest_payload(
            worktree_id=worktree_id,
            kind="primary",
            root_run_id=run_id,
            attached_run_ids=[run_id],
            source_repo_path=str(project_dir),
            source_start_head=base_ref,
            checkout_path=target_path,
            branch_ref=branch_name,
            base_ref=base_ref,
            retention_until=retention_until,
        ),
    )
    return WorktreeContext(
        mode="per_run",
        project_dir=project_dir,
        path=target_path,
        base_ref=base_ref,
        branch=branch_name,
        retention_until=retention_until,
        worktree_id=worktree_id,
        kind="primary",
        root_run_id=run_id,
        source_repo_path=str(project_dir),
        source_start_head=base_ref,
        manifest_path=manifest_path,
    )


def teardown_worktree(
    ctx: WorktreeContext, *, retain: bool = False,
) -> GitOpResult:
    """Remove the orcho worktree at end of run lifecycle.

    ``retain=True`` skips the actual removal — the worktree directory
    stays on disk past the run for retention TTL inspection. ``orcho
    gc`` (added in a later PR) sweeps retained worktrees past their
    ``retention_until``. The bookkeeping inside the source repo's
    ``.git/worktrees/`` is left in place either way; a stale entry
    is harmless and the next ``git worktree prune`` reclaims it.

    Off-mode contexts are no-ops — ``mode="off"`` means there was
    no orcho worktree to tear down.

    Returns the underlying :class:`GitOpResult` so callers can log
    teardown failures without raising. A run that ships should never
    abort on teardown — the run-owned diff has already been captured
    in evidence by the time this is called.
    """
    if ctx.mode == "off":
        return GitOpResult(ok=True)
    if retain:
        return GitOpResult(
            ok=True,
            error=(
                f"retained worktree at {ctx.path} (retention_until="
                f"{ctx.retention_until})"
            ),
            path=ctx.path,
        )
    return _remove_worktree(
        ctx.path, repo=ctx.project_dir, force=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _off_context(
    project_dir: Path, *, degraded_reason: str | None = None,
) -> WorktreeContext:
    """Construct the "no isolation" context with cwd == project_dir."""
    sha, _ = _resolve_base_ref(project_dir)
    base_ref = sha or ""
    return WorktreeContext(
        mode="off",
        project_dir=project_dir,
        path=project_dir,
        base_ref=base_ref,
        branch=None,
        degraded_reason=degraded_reason,
    )


def _worktree_id_for_run(run_id: str) -> str:
    return f"wt_{run_id}"


def _worktree_root_for_run_dir(run_dir: Path) -> Path:
    """Return the physical-worktree root next to ``runs/``.

    Production run dirs are ``<workspace>/runspace/runs/<run_id>``; unit
    tests often pass arbitrary temp dirs. In production, place physical
    checkouts under ``<workspace>/runspace/worktrees``. For arbitrary
    run dirs, use a sibling ``worktrees`` dir next to the run dir.
    """
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent / "worktrees"
    return run_dir.parent / "worktrees"


def _worktree_checkout_path(*, run_dir: Path, worktree_id: str) -> Path:
    return _worktree_root_for_run_dir(run_dir) / worktree_id / "checkout"


def _manifest_payload(
    *,
    worktree_id: str,
    kind: str,
    root_run_id: str,
    attached_run_ids: list[str],
    source_repo_path: str,
    source_start_head: str,
    checkout_path: Path,
    branch_ref: str | None,
    base_ref: str,
    retention_until: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "worktree_id": worktree_id,
        "kind": kind,
        "root_run_id": root_run_id,
        "attached_run_ids": attached_run_ids,
        "source_repo_path": source_repo_path,
        "source_start_head": source_start_head,
        "checkout_path": str(checkout_path),
        "branch_ref": branch_ref,
        "base_ref": base_ref,
    }
    if retention_until is not None:
        payload["retention_until"] = retention_until
    return payload


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _string_value(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _attached_run_ids(manifest: Mapping[str, Any], *, root_run_id: str) -> list[str]:
    raw = manifest.get("attached_run_ids")
    if not isinstance(raw, list):
        return [root_run_id]
    ids = [v for v in raw if isinstance(v, str) and v.strip()]
    return ids or [root_run_id]


def _context_from_recorded_worktree(
    *,
    run_id: str,
    parent_run_id: str | None,
    project_dir: Path,
    parent_worktree: Mapping[str, Any],
    retention_until: str | None,
) -> WorktreeContext | None:
    """Attach a run to a recorded physical worktree (follow-up or resume).

    Shared by the follow-up parent path and the checkpoint-resume retained
    subject: both reuse an existing worktree by its recorded path instead of
    creating a fresh ``wt_<run_id>`` checkout. ``None`` means the recorded
    metadata is incomplete or no longer points at a valid worktree. Callers
    treat that as a continuity failure instead of silently starting from a
    fresh checkout.
    """
    parent_path_raw = _string_value(parent_worktree, "path")
    if parent_path_raw is None:
        return None
    parent_path = Path(parent_path_raw)
    if not registered_worktree_exists(project_dir=project_dir, path=parent_path):
        return None

    root_run_id = (
        _string_value(parent_worktree, "root_run_id")
        or parent_run_id
        or run_id
    )
    worktree_id = (
        _string_value(parent_worktree, "worktree_id")
        or _worktree_id_for_run(root_run_id)
    )
    kind = _string_value(parent_worktree, "kind") or "primary"
    branch = (
        _string_value(parent_worktree, "branch_ref")
        or f"orcho/run/{root_run_id}"
    )
    parent_sha, _ = _resolve_base_ref(parent_path)
    base_ref = (
        _string_value(parent_worktree, "base_ref")
        or parent_sha
        or ""
    )
    source_repo_path = (
        _string_value(parent_worktree, "source_repo_path")
        or str(project_dir)
    )
    source_start_head = (
        _string_value(parent_worktree, "source_start_head")
        or base_ref
    )
    manifest_path_raw = _string_value(parent_worktree, "manifest_path")
    manifest_path = (
        Path(manifest_path_raw)
        if manifest_path_raw is not None
        else parent_path.parent / "manifest.json"
    )
    manifest = _read_manifest(manifest_path)
    attached = _attached_run_ids(manifest, root_run_id=root_run_id)
    if run_id not in attached:
        attached.append(run_id)
    _write_manifest(
        manifest_path,
        _manifest_payload(
            worktree_id=worktree_id,
            kind=kind,
            root_run_id=root_run_id,
            attached_run_ids=attached,
            source_repo_path=source_repo_path,
            source_start_head=source_start_head,
            checkout_path=parent_path,
            branch_ref=branch,
            base_ref=base_ref,
            retention_until=retention_until,
        ),
    )
    return WorktreeContext(
        mode="per_run",
        project_dir=project_dir,
        path=parent_path,
        base_ref=base_ref,
        branch=branch,
        retention_until=retention_until,
        worktree_id=worktree_id,
        kind=kind,
        root_run_id=root_run_id,
        source_repo_path=source_repo_path,
        source_start_head=source_start_head,
        manifest_path=manifest_path,
    )


def registered_worktree_exists(*, project_dir: Path, path: Path) -> bool:
    """True when ``path`` is a registered orcho worktree of ``project_dir``.

    All three checks must hold: the path exists, its ``.git`` is a gitlink
    file (not a bare repo), and it appears in the source repo's ``git
    worktree list``. Public so the resume-continuity classifier can probe a
    recorded worktree's availability before handing it to the resolver.
    """
    if not path.exists() or not (path / ".git").is_file():
        return False
    from core.io.git_helpers import _run_git as _rg  # noqa: PLC0415
    rc, out, _ = _rg(["worktree", "list", "--porcelain"], cwd=str(project_dir))
    if rc != 0:
        return False
    listed = {
        str(Path(line[len("worktree "):].strip()).resolve())
        for line in out.splitlines()
        if line.startswith("worktree ")
    }
    return str(path.resolve()) in listed


def _resolve_effective_mode(
    *, worktree_config: dict[str, Any], profile_isolation: str | None,
) -> IsolationMode:
    """Pick the effective mode from layered inputs.

    Precedence (highest first):
    1. Explicit per-profile override (``profile_isolation``).
    2. Global config ``worktree.isolation``.
    3. Default: ``per_run`` (the GWT-1 mainline).

    Unknown values surface as :class:`WorktreeConfigError` rather
    than silently downgrading — typos in profile JSON shouldn't
    convert ``per_runn`` into ``off`` without the operator noticing.
    """
    candidate = profile_isolation or worktree_config.get("isolation") or "per_run"
    if not isinstance(candidate, str):
        raise WorktreeConfigError(
            f"worktree.isolation must be a string, "
            f"got {type(candidate).__name__}"
        )
    if candidate not in _KNOWN_ISOLATION_MODES:
        raise WorktreeConfigError(
            f"worktree.isolation must be one of "
            f"{sorted(_KNOWN_ISOLATION_MODES)}, got {candidate!r}"
        )
    # mypy / type checker: ``candidate`` is now one of the literal
    # values defined in ``IsolationMode``.
    return candidate  # type: ignore[return-value]


def _resolve_base_ref(
    project_dir: Path,
) -> tuple[str | None, str | None]:
    """Return ``(sha, failure_reason)`` for ``project_dir``'s HEAD.

    On success: ``(sha, None)``. On failure: ``(None, reason)`` where
    reason is one of:

      * ``"not_a_repo"`` — ``project_dir`` is not a git checkout.
      * ``"no_commits"`` — git repo exists but has no initial commit.
      * ``"git_unavailable"`` — git binary missing or invocation failed.

    Callers translate the reason into a user-facing message —
    different failures need different remediation (e.g. ``git commit``
    vs ``orcho workspace init``), so the resolver classifies but does
    not format.
    """
    from core.io.git_helpers import _run_git  # local import: keep module-level surface clean

    rc, stdout, stderr = _run_git(
        ["rev-parse", "HEAD"], cwd=str(project_dir),
    )
    if rc == 0:
        sha = stdout.strip()
        if sha:
            return sha, None
        return None, "no_commits"
    # rc=-1 → ``_run_git`` couldn't even invoke git (missing binary or
    # unreadable cwd). Surface that distinctly from "git ran and said no".
    if rc < 0:
        return None, "git_unavailable"
    # git ran and refused. Empty-repo errors mention "ambiguous argument
    # 'HEAD'" or "unknown revision"; everything else is the "not a git
    # repository" family. Stderr matching is brittle across git versions,
    # so probe explicitly with ``rev-parse --is-inside-work-tree`` to
    # distinguish the two without depending on the rev-parse error text.
    inside_rc, inside_out, _ = _run_git(
        ["rev-parse", "--is-inside-work-tree"], cwd=str(project_dir),
    )
    if inside_rc == 0 and inside_out.strip() == "true":
        return None, "no_commits"
    # Defensive: a stderr that explicitly mentions an empty repo still
    # routes to ``no_commits`` even if the probe above was inconclusive.
    if "does not have any commits yet" in stderr or "unknown revision" in stderr:
        return None, "no_commits"
    return None, "not_a_repo"


def _compute_retention_timestamp(*, retention_days: Any) -> str | None:
    """Convert a retention-days config value to an ISO8601 timestamp.

    ``None`` / ``0`` / negative → no retention (worktree GC-able
    immediately after the run finishes). Non-integer → raises so
    config typos surface loudly.
    """
    if retention_days is None:
        return None
    if isinstance(retention_days, bool) or not isinstance(retention_days, int):
        raise WorktreeConfigError(
            f"worktree.retention_days must be a non-negative integer, "
            f"got {type(retention_days).__name__}"
        )
    if retention_days <= 0:
        return None
    expires = datetime.now(UTC) + timedelta(days=retention_days)
    return expires.isoformat(timespec="seconds")


# ── Active-checkout ContextVar (F2 / ADR 0033) ─────────────────────────────
# The orchestrator sets this once per run to the worktree checkout path (or
# None when isolation is off). Agent runtimes read it to gate the
# destructive-git guardrail — the check is ``cwd == get_active_worktree_checkout()``
# rather than a fragile basename heuristic. ContextVar provides per-thread
# (per-execution-context) isolation, so concurrent parallel sub-runs each
# see their own value without global lock contention.

_active_checkout: ContextVar[str | None] = ContextVar(
    "_orcho_active_checkout", default=None
)


def set_active_worktree_checkout(path: str | None) -> Token[str | None]:
    """Record the active worktree checkout path for this execution context.

    Call this in the orchestrator after :func:`resolve_worktree_for_run`.
    Pass the token to :func:`reset_active_worktree_checkout` in the run's
    cleanup / ``finalize()`` path to avoid leaking across runs in the same
    thread (important for tests).
    """
    return _active_checkout.set(path)


def reset_active_worktree_checkout(token: Token[str | None]) -> None:
    """Reset the ContextVar to its pre-run state using the stored token."""
    _active_checkout.reset(token)


def get_active_worktree_checkout() -> str | None:
    """Return the active worktree checkout path, or None if isolation is off."""
    return _active_checkout.get()


__all__ = [
    "IsolationMode",
    "WorktreeConfigError",
    "WorktreeContext",
    "get_active_worktree_checkout",
    "registered_worktree_exists",
    "reset_active_worktree_checkout",
    "resolve_worktree_for_run",
    "set_active_worktree_checkout",
    "teardown_worktree",
]
