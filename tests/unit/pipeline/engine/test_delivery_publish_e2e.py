"""End-to-end delivery-publish closed cycle (ADR 0121, T3).

Unlike ``test_delivery_publish.py`` (which injects an in-test ``_FakeProvider``),
this module drives the **registered** built-in
:class:`~pipeline.engine.delivery_providers.github.GitHubDeliveryProvider` over a
REAL git worktree, against a FAKE ``gh``. The provider's subprocess boundary is
its injectable ``runner``: ``gh`` argv is answered by an in-test fake (version /
auth ok, ``pr create`` prints a pull-request URL), while every other argv —
crucially ``git push`` — is delegated to the provider's real
``_default_runner``. So the branch is genuinely created, genuinely rebased, and
genuinely pushed to a fake **bare** remote; only ``gh`` is faked, and no real
network is touched.

Two paths are proven:

* happy path — the delivery branch ``orcho/deliver/<run_id>-<slug>`` is created,
  pushed to the bare remote, and a pull request is "opened" via fake ``gh``; the
  ``pr_url`` lands in the persisted ``delivery_notices`` (and ``to_dict()``); the
  delivery commit stays signed-off and the provider adds no commits.
* degradation — ``gh`` missing: the local delivery branch and a "branch ready"
  notice remain, status stays ``committed``, the commit is still signed, and the
  provider makes no push and no commit.
"""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from core.infra.config import AppConfig
from core.io.git_helpers import create_worktree
from pipeline.commit_message_parser import parse_commit_message
from pipeline.engine import delivery_publish
from pipeline.engine.commit_delivery import (
    apply_commit_delivery,
    render_commit_message_prompt,
    resolve_commit_delivery,
)
from pipeline.engine.delivery_providers.github import (
    CommandResult,
    GitHubDeliveryProvider,
    _default_runner,
)
from pipeline.engine.delivery_publish import DELIVERY_PROVIDER_GROUP

pytestmark = [pytest.mark.git_worktree, pytest.mark.serial]

_RUN_ID = "r1"
_PR_URL = "https://github.com/acme/repo/pull/7"


# --- fake gh runner ------------------------------------------------------


def _gh_ok_runner(argv: Sequence[str], cwd: Path) -> CommandResult:
    """Answer ``gh`` argv; delegate everything else (``git``) to the real shell.

    ``gh --version`` / ``gh auth status`` succeed and ``gh pr create`` prints a
    pull-request URL, but a real ``git push`` still reaches the bare remote — so
    the closed cycle is genuine and only ``gh`` is faked.
    """
    argv = list(argv)
    if argv and argv[0] == "gh":
        sub = argv[1:3]
        if sub[:1] == ["--version"] or sub[:1] == ["auth"]:
            return CommandResult(ok=True, stdout="ok")
        if sub == ["pr", "create"]:
            return CommandResult(ok=True, stdout=f"{_PR_URL}\n")
        return CommandResult(ok=False, stderr=f"unexpected gh argv: {argv}")
    return _default_runner(argv, cwd)


def _gh_missing_runner(argv: Sequence[str], cwd: Path) -> CommandResult:
    """Mimic a missing ``gh`` binary; ``git`` argv still reaches the real shell.

    Matches the ``_default_runner`` FileNotFoundError shape for ``gh --version``
    so the provider takes its "gh CLI not found" degradation branch without any
    PATH surgery, while a real ``git`` remains available for the push it never
    reaches.
    """
    argv = list(argv)
    if argv and argv[0] == "gh":
        return CommandResult(ok=False, stderr="gh not found: no such file")
    return _default_runner(argv, cwd)


def _register(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    """Resolve the built-in provider through the entry-point discovery seam."""

    def _fake_discover(group: str, **_: object) -> dict[str, object]:
        assert group == DELIVERY_PROVIDER_GROUP
        return {"github": provider}

    monkeypatch.setattr(delivery_publish, "discover_entry_points", _fake_discover)


# --- real repo + remote + run worktree -----------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a source repo on ``main`` wired to a bare ``origin`` remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    _git(repo, "config", "user.email", "test@orcho.invalid")
    _git(repo, "config", "user.name", "Orcho Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote


def _make_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo, _remote = _init_repo_with_remote(tmp_path)
    run_dir = tmp_path / "run"
    head = _git(repo, "rev-parse", "HEAD")
    result = create_worktree(
        repo=repo,
        base_ref=head,
        target_path=run_dir / "checkout",
        branch_name=f"orcho/run/{_RUN_ID}",
    )
    assert result.ok, result.error
    worktree = run_dir / "checkout"
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    return repo, worktree, run_dir


def _session(summary: str = "feat: update app") -> dict:
    return {
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "short_summary": summary,
            },
        },
    }


def _deliver(
    repo: Path, worktree: Path, run_dir: Path, *, publish: str = "auto"
):
    commit_config: dict = {
        "enabled": True,
        "auto_in_ci": "approve",
        "add_untracked": True,
        "branch_policy": "worktree_branch",
        "publish": publish,
    }
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id=_RUN_ID,
        session=_session(),
        commit_config=commit_config,
        no_interactive=True,
        baseline_ref="HEAD",
    )
    return apply_commit_delivery(
        decision, run_dir=run_dir, commit_config=commit_config
    )


# --- closed cycle: branch pushed to a bare remote + PR opened ------------


def test_worktree_branch_publish_pushes_and_opens_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, GitHubDeliveryProvider(runner=_gh_ok_runner))
    repo, worktree, run_dir = _make_run(tmp_path)
    remote = tmp_path / "remote.git"

    delivered = _deliver(repo, worktree, run_dir)

    # Delivery landed as a published branch, not a canonical-checkout commit.
    assert delivered.status == "committed"
    assert delivered.commit_sha is None
    branch = delivered.delivery_branch
    assert branch is not None
    assert branch.startswith(f"orcho/deliver/{_RUN_ID}-")

    # The branch really reached the bare remote, at the same tip as locally.
    local_tip = _git(repo, "rev-parse", branch)
    remote_tip = _git(remote, "rev-parse", f"refs/heads/{branch}")
    assert remote_tip == local_tip

    # pr_url is persisted in delivery_notices AND in the durable to_dict() view.
    pr_notice = f"PR opened: {_PR_URL}"
    assert pr_notice in delivered.delivery_notices
    assert pr_notice in delivered.to_dict()["delivery_notices"]
    assert delivered.delivery_warnings == ()

    # The delivery commit stayed signed-off (DCO) and the provider added NO
    # commits: exactly one run commit sits over the base, and it carries the
    # Signed-off-by trailer that ``git commit -s`` produced upstream.
    base = _git(repo, "rev-parse", "main")
    run_commits = _git(repo, "rev-list", "--count", f"{base}..{branch}")
    assert run_commits == "1"
    body = _git(repo, "log", "-1", "--format=%B", branch)
    assert "Signed-off-by:" in body

    # The durable audit artifact was written and the canonical checkout is clean.
    assert delivered.artifact_path is not None
    assert delivered.artifact_path.is_file()
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(repo, "status", "--porcelain") == ""


# --- degradation: gh missing → local branch + notice, still committed ----


def test_gh_missing_degrades_to_local_branch_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, GitHubDeliveryProvider(runner=_gh_missing_runner))
    repo, worktree, run_dir = _make_run(tmp_path)
    remote = tmp_path / "remote.git"

    delivered = _deliver(repo, worktree, run_dir)

    # Still a successful local delivery: branch created, status committed.
    assert delivered.status == "committed"
    branch = delivered.delivery_branch
    assert branch is not None
    assert branch.startswith(f"orcho/deliver/{_RUN_ID}-")
    assert _git(repo, "rev-parse", "--verify", "--quiet", branch)

    # A "branch ready" notice replaces the PR notice; the missing-gh warning is
    # surfaced but non-fatal. No PR URL anywhere.
    assert any("ready" in n for n in delivered.delivery_notices)
    assert not any("PR opened" in n for n in delivered.delivery_notices)
    assert any("gh CLI not found" in w for w in delivered.delivery_warnings)

    # gh missing → the provider never pushed: the branch is absent on the remote.
    absent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert absent.returncode != 0

    # The commit is still signed-off and the provider added no commits.
    base = _git(repo, "rev-parse", "main")
    assert _git(repo, "rev-list", "--count", f"{base}..{branch}") == "1"
    assert "Signed-off-by:" in _git(repo, "log", "-1", "--format=%B", branch)


# --- T5 --mock smoke: default config, operator=ru → English outward artifacts ─


def test_mock_publish_outward_authors_english_commit_title_and_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default publish-outward config with a Russian operator language must ship
    an English outward commit + PR (content_language default), carrying the run
    id and the content_language message — never the operator release summary.

    Emulates the run.py generator closure: the outward commit prompt is rendered
    with ``body_language=cfg.content_language`` and a stub agent returns a
    content_language commit-JSON. The default config (``release_summary`` +
    ``publish=auto`` + ``worktree_branch``) still forces content_language
    authorship on this publish-outward path (ADR 0121, T4), and the PR
    title/body derive from that English message (T3).
    """
    monkeypatch.setenv("TASK_LANGUAGE", "Russian")   # operator language
    monkeypatch.delenv("CONTENT_LANGUAGE", raising=False)  # content unset → English
    AppConfig.load.cache_clear()
    try:
        cfg = AppConfig.load()
        assert cfg.task_language == "Russian"
        assert cfg.content_language == "English"

        prompts_seen: list[str] = []

        class _CommitMessageAgent:
            def invoke(self, prompt: str, cwd: str, **_: object) -> str:
                prompts_seen.append(prompt)
                return (
                    '{"subject": "fix(delivery): english outward message", '
                    '"body": "Detailed English body.", "type": "fix", '
                    '"scope": "delivery", "breaking": false}'
                )

        agent = _CommitMessageAgent()

        def generator(decision: object) -> str:
            # Mirror the run.py closure: content_language body-language directive.
            prompt = render_commit_message_prompt(
                decision, body_language=cfg.content_language,
            )
            raw = agent.invoke(
                prompt,
                str(decision.source_path),  # type: ignore[attr-defined]
                mutates_artifacts=False,
                continue_session=False,
            )
            return parse_commit_message(raw).render()

        _register(monkeypatch, GitHubDeliveryProvider(runner=_gh_ok_runner))
        repo, worktree, run_dir = _make_run(tmp_path)

        commit_config: dict = {
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
            "branch_policy": "worktree_branch",  # default publishable policy
            "publish": "auto",
            "default_strategy": "release_summary",  # default — NOT llm_generate
        }
        decision = resolve_commit_delivery(
            project_dir=repo,
            source_worktree=worktree,
            run_dir=run_dir,
            run_id=_RUN_ID,
            session=_session("Русское операторское резюме"),  # operator summary
            commit_config=commit_config,
            no_interactive=True,
            baseline_ref="HEAD",
            commit_message_generator=generator,
        )

        # (1) The outward commit prompt carried the English (content_language)
        # directive even though the operator language is Russian, and the forced
        # content_language authorship produced the English commit message.
        assert prompts_seen, "generator must be forced on the publish-outward path"
        assert "in English" in prompts_seen[0]
        assert "in Russian" not in prompts_seen[0]
        assert decision.commit_message_strategy == "llm_generate"
        assert decision.final_message is not None
        assert decision.final_message.splitlines()[0] == (
            "fix(delivery): english outward message"
        )

        delivered = apply_commit_delivery(
            decision, run_dir=run_dir, commit_config=commit_config,
        )
        assert delivered.status == "committed"
        intent = delivered.pr_intent
        assert intent is not None

        # (2) PR title == the commit headline.
        assert intent.title == "fix(delivery): english outward message"
        # (3) PR body carries the run id and the content_language message body,
        # and NOT the operator (Russian) release summary.
        assert _RUN_ID in intent.body
        assert "Detailed English body." in intent.body
        assert "Русское" not in intent.body
        assert "Русское" not in intent.title

        # The published commit itself is the English outward message.
        branch = delivered.delivery_branch
        assert branch is not None
        commit_body = _git(repo, "log", "-1", "--format=%B", branch)
        assert "fix(delivery): english outward message" in commit_body
        assert "Русское" not in commit_body
    finally:
        AppConfig.load.cache_clear()
