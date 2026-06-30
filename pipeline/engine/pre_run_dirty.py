"""Pre-run dirty checkout intake (ADR 0044).

The worktree resolver creates isolated run checkouts from ``HEAD``. This
module owns the decision that happens immediately before that resolver:
when the source checkout is dirty, should the new run inherit that diff,
start clean from ``HEAD``, commit first, or stop?
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from core.io.git_helpers import apply_patch_to_checkout
from core.io.journey_prompt import (
    bold,
    default_chip,
    divider,
    help_line,
    is_color_active,
    title,
)
from core.io.terminal_input import stdio_interactive

PreRunDirtyAction = Literal["none", "include", "exclude", "commit", "halt"]
PreRunDirtyStatus = Literal[
    "clean",
    "disabled",
    "not_applicable",
    "seed_pending",
    "seeded",
    "seed_failed",
    "excluded",
    "committed",
    "commit_failed",
    "halted",
]

_ACTIONS = frozenset({"include", "exclude", "commit", "halt"})
_UNTRACKED_POLICIES = frozenset({"prompt", "all", "none"})


@dataclass(frozen=True, slots=True)
class PreRunDirtyIntake:
    """Resolved pre-run dirty intake decision and optional seed payload."""

    action: PreRunDirtyAction
    status: PreRunDirtyStatus
    dirty: bool = False
    reason: str | None = None
    source_head: str | None = None
    patch_text: str = ""
    patch_path: Path | None = None
    changed_paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    selected_untracked_paths: tuple[str, ...] = ()
    seed_tree_sha: str | None = None
    commit_sha: str | None = None
    error: str | None = None
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action": self.action,
            "status": self.status,
            "dirty": self.dirty,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.source_head:
            out["source_head"] = self.source_head
        if self.patch_path:
            out["patch_path"] = str(self.patch_path)
        if self.changed_paths:
            out["changed_paths"] = list(self.changed_paths)
        if self.untracked_paths:
            out["untracked_paths"] = list(self.untracked_paths)
        if self.selected_untracked_paths:
            out["selected_untracked_paths"] = list(self.selected_untracked_paths)
        if self.seed_tree_sha:
            out["seed_tree_sha"] = self.seed_tree_sha
        if self.commit_sha:
            out["commit_sha"] = self.commit_sha
        if self.error:
            out["error"] = self.error
        if self.decided_at:
            out["decided_at"] = self.decided_at
        return out

    def with_status(
        self,
        status: PreRunDirtyStatus,
        *,
        error: str | None = None,
        reason: str | None = None,
    ) -> PreRunDirtyIntake:
        return PreRunDirtyIntake(
            action=self.action,
            status=status,
            dirty=self.dirty,
            reason=reason if reason is not None else self.reason,
            source_head=self.source_head,
            patch_text=self.patch_text,
            patch_path=self.patch_path,
            changed_paths=self.changed_paths,
            untracked_paths=self.untracked_paths,
            selected_untracked_paths=self.selected_untracked_paths,
            seed_tree_sha=self.seed_tree_sha,
            commit_sha=self.commit_sha,
            error=error,
            decided_at=self.decided_at,
        )


def resolve_pre_run_dirty_intake(
    *,
    project_dir: Path,
    run_dir: Path | None,
    run_id: str,
    pre_run_config: Mapping[str, Any] | None,
    worktree_config: Mapping[str, Any] | None,
    profile_isolation: str | None,
    resume_from: str | None,
    no_interactive: bool,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> PreRunDirtyIntake:
    """Decide how a dirty project checkout should seed a new isolated run.

    This function runs before ``resolve_worktree_for_run``. It only
    snapshots / commits the source checkout; applying an ``include``
    snapshot happens after the isolated worktree exists via
    :func:`apply_pre_run_dirty_seed`.
    """
    cfg = dict(pre_run_config or {})
    if cfg.get("enabled") is False:
        return PreRunDirtyIntake(
            action="none", status="disabled", reason="pre_run_dirty disabled",
        )
    if run_dir is None or resume_from:
        return PreRunDirtyIntake(
            action="none",
            status="not_applicable",
            reason="no fresh run directory" if run_dir is None else "resume run",
        )
    if not _would_use_isolated_worktree(
        worktree_config=worktree_config, profile_isolation=profile_isolation,
    ):
        return PreRunDirtyIntake(
            action="none",
            status="not_applicable",
            reason="worktree isolation is off",
        )

    head = (_git_stdout(project_dir, ["rev-parse", "HEAD"]) or "").strip()
    if not head:
        return PreRunDirtyIntake(
            action="none",
            status="not_applicable",
            reason="project checkout is not a git repository",
        )

    status = _dirty_status(project_dir)
    if not status.changed_paths and not status.untracked_paths:
        return PreRunDirtyIntake(
            action="none", status="clean", dirty=False, source_head=head,
        )

    interactive = (not no_interactive) and stdio_interactive()
    default_action = str(
        cfg.get(
            "interactive_default" if interactive else "non_interactive_default",
            "include" if interactive else "halt",
        )
    )
    if default_action not in _ACTIONS:
        default_action = "include" if interactive else "halt"

    action = (
        _prompt_action(
            default_action=default_action,
            changed_paths=status.changed_paths,
            untracked_paths=status.untracked_paths,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        if interactive
        else default_action
    )

    decided_at = datetime.now(UTC).isoformat()
    if action == "halt":
        return PreRunDirtyIntake(
            action="halt",
            status="halted",
            dirty=True,
            reason="pre-run dirty intake halted before worktree creation",
            source_head=head,
            changed_paths=tuple(status.changed_paths),
            untracked_paths=tuple(status.untracked_paths),
            decided_at=decided_at,
        )

    if action == "exclude":
        return PreRunDirtyIntake(
            action="exclude",
            status="excluded",
            dirty=True,
            reason="operator chose to start from HEAD",
            source_head=head,
            changed_paths=tuple(status.changed_paths),
            untracked_paths=tuple(status.untracked_paths),
            decided_at=decided_at,
        )

    if action == "commit":
        return _commit_dirty_checkout(
            project_dir=project_dir,
            run_id=run_id,
            source_head=head,
            changed_paths=tuple(status.changed_paths),
            untracked_paths=tuple(status.untracked_paths),
            decided_at=decided_at,
        )

    selected_untracked = _select_untracked(
        policy=str(cfg.get("include_untracked", "prompt")),
        untracked_paths=status.untracked_paths,
        interactive=interactive,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    patch_text = _git_stdout(project_dir, ["diff", "--binary", "HEAD"]) or ""
    patch_path = _write_seed_artifacts(
        run_dir=run_dir,
        patch_text=patch_text,
        changed_paths=status.changed_paths,
        untracked_paths=status.untracked_paths,
        selected_untracked_paths=selected_untracked,
    )
    return PreRunDirtyIntake(
        action="include",
        status="seed_pending",
        dirty=True,
        reason="seed new run worktree with project checkout diff",
        source_head=head,
        patch_text=patch_text,
        patch_path=patch_path,
        changed_paths=tuple(status.changed_paths),
        untracked_paths=tuple(status.untracked_paths),
        selected_untracked_paths=tuple(selected_untracked),
        decided_at=decided_at,
    )


def apply_pre_run_dirty_seed(
    intake: PreRunDirtyIntake,
    *,
    project_dir: Path,
    worktree_path: Path,
) -> PreRunDirtyIntake:
    """Apply an ``include`` intake snapshot to a newly-created worktree."""
    if intake.action != "include":
        return intake
    if intake.patch_text.strip():
        check = apply_patch_to_checkout(
            worktree_path, intake.patch_text, check_only=True,
        )
        if not check.ok:
            return intake.with_status("seed_failed", error=check.error)
        applied = apply_patch_to_checkout(worktree_path, intake.patch_text)
        if not applied.ok:
            return intake.with_status("seed_failed", error=applied.error)

    for rel in intake.selected_untracked_paths:
        if not _safe_relative_path(rel):
            return intake.with_status(
                "seed_failed", error=f"unsafe untracked path {rel!r}",
            )
        src = project_dir / rel
        dst = worktree_path / rel
        if not src.is_file():
            return intake.with_status(
                "seed_failed", error=f"untracked source missing: {rel}",
            )
        if dst.exists():
            return intake.with_status(
                "seed_failed", error=f"seed destination already exists: {rel}",
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    seed_tree = _write_current_tree(worktree_path)
    if not seed_tree.ok:
        return intake.with_status("seed_failed", error=seed_tree.error)

    return PreRunDirtyIntake(
        action=intake.action,
        status="seeded",
        dirty=intake.dirty,
        reason=intake.reason,
        source_head=intake.source_head,
        patch_text=intake.patch_text,
        patch_path=intake.patch_path,
        changed_paths=intake.changed_paths,
        untracked_paths=intake.untracked_paths,
        selected_untracked_paths=intake.selected_untracked_paths,
        seed_tree_sha=seed_tree.stdout.strip(),
        commit_sha=intake.commit_sha,
        error=None,
        decided_at=intake.decided_at,
    )


@dataclass(frozen=True, slots=True)
class _DirtyStatus:
    changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _GitResult:
    ok: bool
    stdout: str = ""
    error: str | None = None


def _would_use_isolated_worktree(
    *,
    worktree_config: Mapping[str, Any] | None,
    profile_isolation: str | None,
) -> bool:
    cfg = dict(worktree_config or {})
    if cfg.get("enabled") is False:
        return False
    mode = profile_isolation or cfg.get("isolation") or "per_run"
    return mode != "off"


def _dirty_status(project_dir: Path) -> _DirtyStatus:
    raw = _git_stdout(
        project_dir, ["status", "--porcelain=v1", "--untracked-files=normal"],
    )
    if raw is None:
        return _DirtyStatus((), ())

    changed: list[str] = []
    untracked: list[str] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        path = path.strip()
        if not path:
            continue
        if code == "??":
            untracked.append(path)
        else:
            changed.append(path)
    return _DirtyStatus(tuple(changed), tuple(untracked))


def _commit_dirty_checkout(
    *,
    project_dir: Path,
    run_id: str,
    source_head: str,
    changed_paths: tuple[str, ...],
    untracked_paths: tuple[str, ...],
    decided_at: str,
) -> PreRunDirtyIntake:
    add = _run_git(project_dir, ["add", "-A"])
    if not add.ok:
        return PreRunDirtyIntake(
            action="commit",
            status="commit_failed",
            dirty=True,
            source_head=source_head,
            changed_paths=changed_paths,
            untracked_paths=untracked_paths,
            error=add.error,
            decided_at=decided_at,
        )
    message = f"chore: checkpoint dirty work before orcho run {run_id}"
    commit = _run_git(project_dir, ["commit", "-m", message])
    if not commit.ok:
        error = commit.error
        reset = _run_git(project_dir, ["reset"])
        if not reset.ok:
            error = f"{error}; failed to unstage checkout after commit failure: {reset.error}"
        return PreRunDirtyIntake(
            action="commit",
            status="commit_failed",
            dirty=True,
            source_head=source_head,
            changed_paths=changed_paths,
            untracked_paths=untracked_paths,
            error=error,
            decided_at=decided_at,
        )
    new_head = (_git_stdout(project_dir, ["rev-parse", "HEAD"]) or "").strip()
    return PreRunDirtyIntake(
        action="commit",
        status="committed",
        dirty=True,
        reason="committed project checkout before run",
        source_head=source_head,
        changed_paths=changed_paths,
        untracked_paths=untracked_paths,
        commit_sha=new_head,
        decided_at=decided_at,
    )


def _write_current_tree(worktree_path: Path) -> _GitResult:
    add = _run_git(worktree_path, ["add", "-A"])
    if not add.ok:
        return add
    tree = _run_git(worktree_path, ["write-tree"])
    reset = _run_git(worktree_path, ["reset"])
    if not tree.ok:
        return tree
    if not reset.ok:
        return reset
    return tree


def _write_seed_artifacts(
    *,
    run_dir: Path,
    patch_text: str,
    changed_paths: tuple[str, ...],
    untracked_paths: tuple[str, ...],
    selected_untracked_paths: tuple[str, ...],
) -> Path | None:
    seed_dir = run_dir / "pre_run_dirty"
    seed_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "changed_paths": list(changed_paths),
        "untracked_paths": list(untracked_paths),
        "selected_untracked_paths": list(selected_untracked_paths),
    }
    (seed_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    if not patch_text.strip():
        return None
    patch_path = seed_dir / "seed.patch"
    patch_path.write_text(patch_text, encoding="utf-8")
    return patch_path


def _select_untracked(
    *,
    policy: str,
    untracked_paths: tuple[str, ...],
    interactive: bool,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> tuple[str, ...]:
    if not untracked_paths:
        return ()
    if policy not in _UNTRACKED_POLICIES:
        policy = "prompt"
    if policy == "all":
        return untracked_paths
    if policy == "none" or not interactive:
        return ()
    color = is_color_active()
    output_fn("")
    output_fn(f"  {bold('Untracked files detected:', color=color)}")
    for path in untracked_paths:
        output_fn(f"  ?? {path}")
    output_fn(
        f"  {help_line('Untracked files are not part of the diff against HEAD;', color=color)}",
    )
    output_fn(
        f"  {help_line('decide whether the new isolated run should copy them in.', color=color)}",
    )
    prompt_text = (
        f"  Include all untracked files in this run seed? "
        f"[y/{bold('N', color=color)}] "
    )
    answer = input_fn(prompt_text)
    return untracked_paths if answer.strip().lower() in {"y", "yes"} else ()


_PRE_RUN_OPTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("1", "📥", "include",
     "Seed the new isolated run worktree with the current uncommitted diff."),
    ("2", "🚪", "exclude",
     "Start the run from HEAD; leave the checkout changes untouched here."),
    ("3", "📝", "commit",
     "Commit the current checkout first, then start the run from that commit."),
    ("4", "🛑", "halt",
     "Stop before the run starts. Nothing is seeded, committed, or written."),
)

_PRE_RUN_ALIASES: dict[str, str] = {
    "1": "include", "i": "include", "include": "include",
    "2": "exclude", "e": "exclude", "exclude": "exclude",
    "3": "commit",  "c": "commit",  "commit": "commit",
    "4": "halt",    "h": "halt",    "halt": "halt",
}


def _prompt_action(
    *,
    default_action: str,
    changed_paths: tuple[str, ...],
    untracked_paths: tuple[str, ...],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str:
    color = is_color_active()
    output_fn("")
    output_fn(divider(color=color))
    output_fn(
        f"  {title('Pre-run intake — uncommitted changes in checkout', color=color)}",
    )
    output_fn(divider(color=color))
    output_fn(
        f"  {help_line('An isolated run starts from HEAD by default. Decide how the', color=color)}",
    )
    output_fn(
        f"  {help_line('current checkout state should feed (or not) into the new run.', color=color)}",
    )
    output_fn("")
    if changed_paths:
        output_fn(f"  {bold('Tracked changes:', color=color)}")
        for path in changed_paths:
            output_fn(f"  M  {path}")
    if untracked_paths:
        output_fn(f"  {bold('Untracked files:', color=color)}")
        for path in untracked_paths:
            output_fn(f"  ?? {path}")
    output_fn("")
    output_fn(f"  {bold('What do you want to do?', color=color)}")
    output_fn("")
    for num, glyph, name, helptext in _PRE_RUN_OPTIONS:
        label = f"{num}) {glyph} {bold(name, color=color)}"
        if name == default_action:
            label = f"{label}  {default_chip(color=color)}"
        output_fn(f"    {label}")
        output_fn(f"       {help_line(helptext, color=color)}")
    output_fn("")
    aliases = {**_PRE_RUN_ALIASES, "": default_action}
    while True:
        prompt_text = (
            f"  Choose [{bold(default_action, color=color)}] "
            f"(1/2/3/4 or name): "
        )
        raw = input_fn(prompt_text).strip().lower()
        action = aliases.get(raw)
        if action:
            return action
        output_fn(
            f"    {help_line('Please choose include, exclude, commit, or halt.', color=color)}",
        )


def _safe_relative_path(path: str) -> bool:
    p = Path(path)
    return not p.is_absolute() and ".." not in p.parts


def _git_stdout(cwd: Path, args: list[str]) -> str | None:
    result = _run_git(cwd, args)
    return result.stdout if result.ok else None


def _run_git(cwd: Path, args: list[str]) -> _GitResult:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as exc:
        return _GitResult(ok=False, error=f"git binary not found: {exc}")
    except OSError as exc:
        return _GitResult(ok=False, error=f"git invocation failed: {exc}")
    except subprocess.TimeoutExpired:
        return _GitResult(ok=False, error="git command timed out after 30s")
    if r.returncode != 0:
        return _GitResult(
            ok=False, error=r.stderr.strip() or r.stdout.strip() or f"rc={r.returncode}",
        )
    return _GitResult(ok=True, stdout=r.stdout)


__all__ = [
    "PreRunDirtyIntake",
    "apply_pre_run_dirty_seed",
    "resolve_pre_run_dirty_intake",
]
