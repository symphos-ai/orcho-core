"""Delivery branch-policy resolver (ADR 0119).

This is the single home for the ``branch_policy`` × isolation decision that the
post-release commit site (:mod:`pipeline.engine.commit_delivery`) consumes. It
owns four responsibilities and nothing else:

1. **Default-branch detection** — resolve the target repo's default branch from
   ``refs/remotes/<remote>/HEAD`` with a ``main`` → ``master`` fallback. A
   ``named`` target overrides detection.
2. **Policy resolution** — map ``commit.branch_policy``
   (``worktree_branch`` | ``protect_default`` | ``named`` | ``bypass``, default
   ``worktree_branch``) against isolation. In-place is ``_same_checkout(source,
   project)`` — the same discriminator the participants layer uses to stamp
   ``isolation = off | per_run``. The full ADR 0119 table is implemented here,
   including ``worktree_branch`` degrading to ``protect_default`` for an
   in-place run and an in-place run on a *non-default* branch committing onto
   that branch.
3. **worktree_branch publish** — for an isolated (``per_run``) run, publish the
   run's own branch as ``orcho/deliver/<run_id>-<slug>``: fetch + rebase onto a
   fresh ``origin/<default>``, and leave the canonical checkout's ``HEAD`` and
   working tree untouched. A rebase conflict is not fatal (publish un-rebased +
   warning); offline / no remote degrades to a local branch + notice.
4. **PR-intent construction** — build the provider-neutral
   :class:`DeliveryPrIntent` (``branch`` / ``base`` / ``title`` / suggested
   command). Core never shells ``gh`` / ``glab`` and never pushes or opens a PR;
   the suggested command is plain ``git``.

The resolver returns a typed :class:`DeliveryBranchOutcome` — the commit site
acts on the ``plan`` (``publish`` skips the in-checkout commit; ``commit_*``
drive it) rather than threading loose locals. ``bypass`` is a *policy choice*
that reproduces the prior "commit onto current HEAD" behavior, not a second code
path.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

BranchPolicy = Literal["worktree_branch", "protect_default", "named", "bypass"]
# What the commit site should do once the branch is resolved:
#   ``publish``          — the run branch was published to local refs already;
#                          do NOT commit into the target checkout (commit_sha
#                          stays absent for a pure worktree_branch publish).
#   ``commit_on_branch`` — check out ``commit_branch`` (creating it off the
#                          default tip if absent) in the target checkout, then
#                          commit there.
#   ``commit_in_place``  — commit onto the target's current HEAD without any
#                          branch switch (in-place feature branch, or bypass).
BranchPlan = Literal["publish", "commit_on_branch", "commit_in_place"]

_VALID_POLICIES: frozenset[str] = frozenset(
    {"worktree_branch", "protect_default", "named", "bypass"}
)
_DEFAULT_POLICY: BranchPolicy = "worktree_branch"

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_BRANCH_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SLUG_MAX_LEN = 40


@dataclass(frozen=True, slots=True)
class DeliveryPrIntent:
    """Provider-neutral pull-request intent produced by core (ADR 0119).

    Core records the *intent* — branch, base, a title lifted from the release
    summary, and a suggested plain-``git`` command — but never pushes or opens a
    pull request. A git-provider plugin owns that step.
    """

    branch: str
    base: str
    title: str
    suggested_command: str
    # ADR 0121 — full pull-request body a provider plugin may use (lifted from
    # the release summary). Kept OUT of ``to_dict`` so the durable/wire shape is
    # byte-identical to the ADR 0119 record; it is an in-process hint for the
    # publisher only.
    body: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "branch": self.branch,
            "base": self.base,
            "title": self.title,
            "suggested_command": self.suggested_command,
        }


@dataclass(frozen=True, slots=True)
class DeliveryBranchOutcome:
    """Resolved delivery-branch decision + any publish side effects.

    ``plan`` is the discriminator the commit site branches on. ``delivery_branch``
    is the published/publishable branch record (``None`` only for ``bypass``);
    ``commit_branch`` is set only for ``commit_on_branch``. ``pr_intent`` is
    ``None`` for ``bypass`` (byte-identical prior behavior) and populated
    otherwise.
    """

    policy: BranchPolicy
    plan: BranchPlan
    default_branch: str
    base_ref: str
    commit_branch: str | None = None
    delivery_branch: str | None = None
    pr_intent: DeliveryPrIntent | None = None
    published: bool = False
    rebased: bool = False
    warnings: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()

    @property
    def commits_into_checkout(self) -> bool:
        """True when the commit site must run ``git commit`` in the target.

        ``publish`` already produced the delivery branch over the run worktree,
        so the canonical checkout is left untouched and no ``commit_sha`` is
        produced.
        """
        return self.plan != "publish"


def normalize_branch_policy(raw: object) -> BranchPolicy:
    """Coerce a configured ``branch_policy`` to a known value.

    Unknown / missing values fall back to the ADR 0119 default
    ``worktree_branch`` so a misconfiguration never silently weakens the
    default-branch invariant.
    """
    value = str(raw or "").strip()
    if value in _VALID_POLICIES:
        return value  # type: ignore[return-value]
    return _DEFAULT_POLICY


def detect_default_branch(git_root: Path, *, remote: str = "origin") -> str:
    """Resolve the repo's default branch (ADR 0119 detection order).

    ``refs/remotes/<remote>/HEAD`` first, then a ``main`` → ``master`` fallback
    against local heads and remote-tracking refs, finally ``main`` as a last
    resort so callers always get a usable name.
    """
    head = _git_stdout(git_root, ["symbolic-ref", f"refs/remotes/{remote}/HEAD"])
    if head:
        name = head.strip().rsplit("/", 1)[-1]
        if name:
            return name
    for cand in ("main", "master"):
        if _ref_exists(git_root, f"refs/heads/{cand}") or _ref_exists(
            git_root, f"refs/remotes/{remote}/{cand}"
        ):
            return cand
    return "main"


def resolve_delivery_branch(
    *,
    source_path: Path,
    project_path: Path,
    run_id: str,
    base_ref: str,
    branch_policy: object,
    named_branch: str | None = None,
    release_summary: str = "",
    remote: str = "origin",
) -> DeliveryBranchOutcome:
    """Resolve the delivery branch for a finished run (ADR 0119).

    ``source_path`` is the run's worktree and ``project_path`` the delivery
    target (git root). The run is *in-place* exactly when they are the same
    checkout. For an isolated ``worktree_branch`` run the publish (rebase the run
    branch onto a fresh default and create ``orcho/deliver/<run_id>-<slug>`` in
    local refs) is executed by :func:`publish_delivery_branch` — that operates
    over the disposable run worktree and never touches the canonical checkout.
    An isolated ``protect_default`` run instead commits onto a fresh delivery
    branch checked out in the target repo.
    """
    policy = normalize_branch_policy(branch_policy)
    in_place = _same_checkout(source_path, project_path)
    default_branch = detect_default_branch(project_path, remote=remote)

    if policy == "bypass":
        # Explicit opt-out: reproduce the prior behavior — commit onto the
        # target's current HEAD (including the default branch). No published
        # branch, no PR intent, so the serialized decision stays byte-identical
        # to the pre-ADR-0119 no-op path.
        return DeliveryBranchOutcome(
            policy="bypass",
            plan="commit_in_place",
            default_branch=default_branch,
            base_ref=base_ref,
        )

    if policy == "named":
        target = (named_branch or "").strip()
        if target:
            return DeliveryBranchOutcome(
                policy="named",
                plan="commit_on_branch",
                default_branch=default_branch,
                base_ref=base_ref,
                commit_branch=target,
                delivery_branch=target,
                pr_intent=_build_pr_intent(
                    target, default_branch, release_summary, remote=remote
                ),
            )
        # A ``named`` policy with no branch supplied is a misconfiguration; fall
        # back to protecting the default branch rather than committing onto it.
        return _resolve_protect_default(
            source_path=source_path,
            project_path=project_path,
            run_id=run_id,
            base_ref=base_ref,
            in_place=in_place,
            default_branch=default_branch,
            release_summary=release_summary,
            remote=remote,
            requested_policy="named",
            extra_notices=(
                "branch_policy=named requires a target branch; "
                "degraded to protect_default",
            ),
        )

    # worktree_branch / protect_default.
    return _resolve_protect_default(
        source_path=source_path,
        project_path=project_path,
        run_id=run_id,
        base_ref=base_ref,
        in_place=in_place,
        default_branch=default_branch,
        release_summary=release_summary,
        remote=remote,
        requested_policy=policy,
    )


def checkout_delivery_branch(
    project_path: Path,
    branch: str,
    *,
    base_ref: str | None = None,
) -> str | None:
    """Check out ``branch`` in ``project_path`` for a ``commit_on_branch`` plan.

    Switches to ``branch`` when it already exists (carrying any staged/working-
    tree delivery over). When absent the branch is created off ``base_ref`` —
    the run's baseline commit-ish (ADR 0119), NOT the target checkout's current
    HEAD nor its local default branch — so the delivery branch is anchored to
    the exact base delivery point. This holds even when ``base_ref`` is a bare
    commit SHA / seed ref rather than a local branch head, and even when the
    default branch advanced or the checkout sits on a *different* branch between
    the run baseline and approve: those extra commits never leak into the
    delivery branch (and therefore the PR range). ``base_ref`` is used whenever
    it resolves to any commit-ish; otherwise the branch is created off the
    current HEAD. The commit site calls this at the commit point so branch
    mechanics stay in this module. Returns ``None`` on success or a
    human-readable error string.
    """
    if _ref_exists(project_path, f"refs/heads/{branch}"):
        result = _run_git(project_path, ["checkout", branch])
    elif base_ref and _ref_exists(project_path, base_ref):
        result = _run_git(project_path, ["checkout", "-b", branch, base_ref])
    else:
        result = _run_git(project_path, ["checkout", "-b", branch])
    return None if result.ok else result.error


def publish_delivery_branch(
    *,
    source_path: Path,
    project_path: Path,
    outcome: DeliveryBranchOutcome,
    remote: str = "origin",
) -> DeliveryBranchOutcome:
    """Execute the ``publish`` plan: rebase + publish the run's delivery branch.

    Separated from :func:`resolve_delivery_branch` so the commit site can
    materialise the run's work as a commit on the run branch (in the disposable
    run worktree) *before* the rebase — the run's changes are otherwise
    uncommitted working-tree diffs. Operates entirely over ``source_path`` (the
    run worktree) and shared-object-store refs; the canonical ``project_path``
    checkout is never touched. Returns a new outcome with
    ``published`` / ``rebased`` / ``warnings`` / ``notices`` filled. A non-publish
    outcome is returned unchanged (defensive no-op).
    """
    if outcome.plan != "publish" or not outcome.delivery_branch:
        return outcome
    result = _publish_run_branch(
        run_worktree=source_path,
        project_git_root=project_path,
        deliver_branch=outcome.delivery_branch,
        default_branch=outcome.default_branch,
        remote=remote,
    )
    return replace(
        outcome,
        published=result.published,
        rebased=result.rebased,
        warnings=outcome.warnings + result.warnings,
        notices=outcome.notices + result.notices,
    )


# --- internal resolution -------------------------------------------------


def _resolve_protect_default(
    *,
    source_path: Path,
    project_path: Path,
    run_id: str,
    base_ref: str,
    in_place: bool,
    default_branch: str,
    release_summary: str,
    remote: str,
    requested_policy: BranchPolicy,
    extra_notices: tuple[str, ...] = (),
) -> DeliveryBranchOutcome:
    """worktree_branch / protect_default resolution (ADR 0119 table).

    For an isolated run the two policies diverge: ``worktree_branch`` publishes
    the run's own branch and never touches a checkout (so ``commit_sha`` stays
    absent), while ``protect_default`` commits onto a fresh delivery branch
    checked out in the target repo (``commit_sha`` populated). For an in-place
    run the default branch is guarded (a fresh ``orcho/deliver/…`` when HEAD is
    the default, else a commit onto the current feature branch).
    """
    notices = extra_notices
    if not in_place:
        deliver = _delivery_branch_name(run_id, release_summary)
        if requested_policy == "worktree_branch":
            # per_run worktree_branch: the delivery is the run's own branch and
            # nothing is committed into a checkout, so ``commit_sha`` stays
            # absent. Resolution is pure — the actual rebase/publish runs later
            # in :func:`publish_delivery_branch` (after the commit site has
            # materialised the run's work as a commit on the run branch), so
            # ``published`` / ``rebased`` stay ``False`` until then.
            return DeliveryBranchOutcome(
                policy="worktree_branch",
                plan="publish",
                default_branch=default_branch,
                base_ref=base_ref,
                delivery_branch=deliver,
                pr_intent=_build_pr_intent(
                    deliver, default_branch, release_summary, remote=remote
                ),
                notices=notices,
            )
        # per_run protect_default (or a ``named`` policy degraded to it): commit
        # onto a fresh delivery branch checked out in the target repo. The
        # default branch is never the commit target, so the invariant holds, and
        # a real commit is produced so ``commit_sha`` stays populated — only a
        # pure ``worktree_branch`` publish leaves it absent.
        return DeliveryBranchOutcome(
            policy="protect_default",
            plan="commit_on_branch",
            default_branch=default_branch,
            base_ref=base_ref,
            commit_branch=deliver,
            delivery_branch=deliver,
            pr_intent=_build_pr_intent(
                deliver, default_branch, release_summary, remote=remote
            ),
            notices=notices,
        )

    # in-place: worktree_branch has no run branch to publish, so it degrades to
    # protect_default.
    if requested_policy == "worktree_branch":
        notices = notices + (
            "worktree_branch requires an isolated (per_run) run; "
            "degraded to protect_default for this in-place delivery",
        )

    current = _current_branch(project_path)
    if current is not None and current != default_branch:
        # In-place on a non-default branch: the operator ran on their own
        # feature branch deliberately — commit onto it. Only the default branch
        # is guarded.
        return DeliveryBranchOutcome(
            policy="protect_default",
            plan="commit_in_place",
            default_branch=default_branch,
            base_ref=base_ref,
            delivery_branch=current,
            pr_intent=_build_pr_intent(
                current, default_branch, release_summary, remote=remote
            ),
            notices=notices,
        )

    # HEAD is the default branch (or detached): protect it by committing onto a
    # fresh delivery branch instead.
    deliver = _delivery_branch_name(run_id, release_summary)
    return DeliveryBranchOutcome(
        policy="protect_default",
        plan="commit_on_branch",
        default_branch=default_branch,
        base_ref=base_ref,
        commit_branch=deliver,
        delivery_branch=deliver,
        pr_intent=_build_pr_intent(
            deliver, default_branch, release_summary, remote=remote
        ),
        notices=notices,
    )


@dataclass(frozen=True, slots=True)
class _PublishResult:
    published: bool
    rebased: bool
    warnings: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()


def _publish_run_branch(
    *,
    run_worktree: Path,
    project_git_root: Path,
    deliver_branch: str,
    default_branch: str,
    remote: str,
) -> _PublishResult:
    """Publish the run branch as ``deliver_branch`` rebased onto fresh default.

    All git ops run over the disposable ``run_worktree`` (whose HEAD is the run
    branch) and over ref creation in the shared object store — the canonical
    checkout at ``project_git_root`` is never checked out or mutated. Core does
    not push; publishing means creating the rebased branch in local refs.
    """
    warnings: list[str] = []
    notices: list[str] = []

    target = _rebase_target(
        project_git_root,
        default_branch=default_branch,
        remote=remote,
        notices=notices,
    )

    # Create the delivery branch at the run branch tip, checked out in the run
    # worktree (never the canonical checkout).
    created = _run_git(run_worktree, ["checkout", "-B", deliver_branch])
    if not created.ok:
        warnings.append(
            f"could not create delivery branch {deliver_branch}: {created.error}"
        )
        return _PublishResult(
            published=False,
            rebased=False,
            warnings=tuple(warnings),
            notices=tuple(notices),
        )

    rebased = False
    if target is not None:
        rebase = _run_git(run_worktree, ["rebase", target])
        if rebase.ok:
            rebased = True
        else:
            conflicts = _git_lines(
                run_worktree, ["diff", "--name-only", "--diff-filter=U"]
            )
            _run_git(run_worktree, ["rebase", "--abort"])
            conflict_desc = ", ".join(conflicts) if conflicts else "unknown paths"
            warnings.append(
                f"rebase of {deliver_branch} onto {target} conflicted "
                f"({conflict_desc}); published un-rebased"
            )

    return _PublishResult(
        published=True,
        rebased=rebased,
        warnings=tuple(warnings),
        notices=tuple(notices),
    )


def _rebase_target(
    project_git_root: Path,
    *,
    default_branch: str,
    remote: str,
    notices: list[str],
) -> str | None:
    """Best-effort ref to rebase the delivery branch onto.

    Fetches ``<remote>/<default>`` when a remote is configured so the PR range
    contains only the run's commit. Offline / no-remote degrades to the local
    default branch (or no rebase at all) with a "push when a remote is
    available" notice.
    """
    has_remote = _run_git(project_git_root, ["remote", "get-url", remote]).ok
    if has_remote:
        fetched = _run_git(project_git_root, ["fetch", remote, default_branch])
        if fetched.ok and _ref_exists(
            project_git_root, f"refs/remotes/{remote}/{default_branch}"
        ):
            return f"{remote}/{default_branch}"
        notices.append(
            f"offline: could not fetch {remote}/{default_branch}; "
            "publishing delivery branch to local refs "
            "(push when a remote is available)"
        )
    else:
        notices.append(
            f"no '{remote}' remote configured; publishing delivery branch to "
            "local refs (push when a remote is available)"
        )
    if _ref_exists(project_git_root, f"refs/heads/{default_branch}"):
        return default_branch
    return None


def _build_pr_intent(
    branch: str,
    base: str,
    release_summary: str,
    *,
    remote: str,
) -> DeliveryPrIntent:
    """Build the provider-neutral PR intent (ADR 0119).

    The suggested command is plain ``git`` — core names no ``gh`` / ``glab``
    binary and encodes no provider API.
    """
    title = _pr_title(release_summary, branch)
    return DeliveryPrIntent(
        branch=branch,
        base=base,
        title=title,
        suggested_command=f"git push -u {remote} {branch}",
        body=(release_summary or "").strip(),
    )


# --- helpers -------------------------------------------------------------


def _delivery_branch_name(run_id: str, release_summary: str) -> str:
    slug = _slugify(release_summary)
    return f"orcho/deliver/{_safe_branch_component(run_id)}-{slug}"


def _slugify(text: str) -> str:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    slug = _SLUG_STRIP_RE.sub("-", first.lower()).strip("-")
    if len(slug) > _SLUG_MAX_LEN:
        slug = slug[:_SLUG_MAX_LEN].rstrip("-")
    return slug or "delivery"


def _safe_branch_component(value: str) -> str:
    safe = _BRANCH_STRIP_RE.sub("-", str(value)).strip("-._")
    return safe or "run"


def _pr_title(release_summary: str, branch: str) -> str:
    summary = (release_summary or "").strip()
    if summary:
        return summary.splitlines()[0].strip()
    return f"Orcho delivery {branch}"


def _same_checkout(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _current_branch(git_root: Path) -> str | None:
    """Current branch name, or ``None`` when HEAD is detached."""
    name = _git_stdout(git_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not name:
        return None
    name = name.strip()
    if not name or name == "HEAD":
        return None
    return name


def _ref_exists(git_root: Path, ref: str) -> bool:
    return _run_git(git_root, ["rev-parse", "--verify", "--quiet", ref]).ok


@dataclass(frozen=True, slots=True)
class _GitResult:
    ok: bool
    stdout: str = ""
    error: str | None = None


def _git_stdout(cwd: Path, args: list[str]) -> str | None:
    result = _run_git(cwd, args)
    return result.stdout if result.ok else None


def _git_lines(cwd: Path, args: list[str]) -> tuple[str, ...]:
    out = _git_stdout(cwd, args)
    if out is None:
        return ()
    return tuple(line for line in out.splitlines() if line.strip())


def _run_git(cwd: Path, args: list[str]) -> _GitResult:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=60.0,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as exc:
        return _GitResult(ok=False, error=f"git binary not found: {exc}")
    except OSError as exc:
        return _GitResult(ok=False, error=f"git invocation failed: {exc}")
    except subprocess.TimeoutExpired:
        return _GitResult(ok=False, error="git command timed out after 60s")
    if proc.returncode != 0:
        return _GitResult(
            ok=False,
            error=proc.stderr.strip() or proc.stdout.strip() or f"rc={proc.returncode}",
        )
    return _GitResult(ok=True, stdout=proc.stdout)


__all__ = [
    "BranchPlan",
    "BranchPolicy",
    "DeliveryBranchOutcome",
    "DeliveryPrIntent",
    "checkout_delivery_branch",
    "detect_default_branch",
    "normalize_branch_policy",
    "publish_delivery_branch",
    "resolve_delivery_branch",
]
