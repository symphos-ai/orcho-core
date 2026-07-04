"""Built-in GitHub delivery provider tests (ADR 0121, T2).

Every scenario drives :class:`GitHubDeliveryProvider` against an injected stub
runner (or a fake ``gh`` / ``git`` on a temporary ``PATH``) so no provider
binary is really executed, no network is touched, and no real pull request is
opened. The provider must never raise and must never create a commit.
"""
from __future__ import annotations

import importlib.metadata
import os
import stat
import tomllib
from collections.abc import Sequence
from pathlib import Path

from pipeline.engine.delivery_branch import DeliveryPrIntent
from pipeline.engine.delivery_providers.github import (
    CommandResult,
    GitHubDeliveryProvider,
)
from pipeline.entry_points import discover_entry_points

_GROUP = "orcho.delivery_providers"


def _intent() -> DeliveryPrIntent:
    return DeliveryPrIntent(
        branch="orcho/deliver/r1-add-x",
        base="main",
        title="Add x",
        suggested_command="git push -u origin orcho/deliver/r1-add-x",
        body="Add x\n\nlonger body",
    )


class _StubRunner:
    """Records argv and answers gh/git shell-outs per configured outcome."""

    def __init__(
        self,
        *,
        gh_available: bool = True,
        authed: bool = True,
        push_ok: bool = True,
        pr_ok: bool = True,
        pr_stdout: str = "https://github.com/acme/widget/pull/9\n",
    ) -> None:
        self.calls: list[list[str]] = []
        self._gh_available = gh_available
        self._authed = authed
        self._push_ok = push_ok
        self._pr_ok = pr_ok
        self._pr_stdout = pr_stdout

    def __call__(self, argv: Sequence[str], cwd: Path) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["gh", "--version"]:
            return CommandResult(
                ok=self._gh_available,
                stdout="gh version 2.0.0" if self._gh_available else "",
                stderr="" if self._gh_available else "command not found: gh",
            )
        if argv[:3] == ["gh", "auth", "status"]:
            return CommandResult(
                ok=self._authed,
                stderr="" if self._authed else "not logged in to any GitHub hosts",
            )
        if argv[:2] == ["git", "push"]:
            return CommandResult(
                ok=self._push_ok,
                stderr="" if self._push_ok else "fatal: unable to access remote",
            )
        if argv[:3] == ["gh", "pr", "create"]:
            return CommandResult(
                ok=self._pr_ok,
                stdout=self._pr_stdout if self._pr_ok else "",
                stderr="" if self._pr_ok else "pull request create failed",
            )
        return CommandResult(ok=True)

    def ran(self, prefix: list[str]) -> bool:
        return any(call[: len(prefix)] == prefix for call in self.calls)


# --- behavior scenarios (stub runner) ------------------------------------


def test_success_pushes_and_returns_pr_url(tmp_path: Path) -> None:
    runner = _StubRunner(pr_stdout="https://github.com/acme/widget/pull/42\n")
    provider = GitHubDeliveryProvider(runner=runner)

    result = provider.publish(
        _intent(), branch="orcho/deliver/r1-add-x", cwd=tmp_path, remote="origin"
    )

    assert result.pushed is True
    assert result.pr_url == "https://github.com/acme/widget/pull/42"
    assert result.warnings == ()
    # Pushed the delivery branch; opened a PR over the existing commit.
    assert runner.ran(["git", "push", "-u", "origin", "orcho/deliver/r1-add-x"])
    assert runner.ran(["gh", "pr", "create"])
    # The provider must NEVER create a commit.
    assert not runner.ran(["git", "commit"])


def test_gh_missing_degrades_without_pushing(tmp_path: Path) -> None:
    runner = _StubRunner(gh_available=False)
    provider = GitHubDeliveryProvider(runner=runner)

    result = provider.publish(
        _intent(), branch="orcho/deliver/r1-add-x", cwd=tmp_path, remote="origin"
    )

    assert result.pushed is False
    assert result.pr_url is None
    assert result.warnings and "gh CLI not found" in result.warnings[0]
    # No push attempted when gh is unavailable.
    assert not runner.ran(["git", "push"])
    assert not runner.ran(["gh", "pr", "create"])


def test_auth_failure_becomes_warning(tmp_path: Path) -> None:
    runner = _StubRunner(authed=False)
    provider = GitHubDeliveryProvider(runner=runner)

    result = provider.publish(
        _intent(), branch="orcho/deliver/r1-add-x", cwd=tmp_path, remote="origin"
    )

    assert result.pushed is False
    assert result.warnings and "authentication" in result.warnings[0]
    assert not runner.ran(["git", "push"])


def test_push_failure_becomes_warning(tmp_path: Path) -> None:
    runner = _StubRunner(push_ok=False)
    provider = GitHubDeliveryProvider(runner=runner)

    result = provider.publish(
        _intent(), branch="orcho/deliver/r1-add-x", cwd=tmp_path, remote="origin"
    )

    assert result.pushed is False
    assert result.warnings and "git push" in result.warnings[0]
    # push failed → do not attempt to open a PR.
    assert not runner.ran(["gh", "pr", "create"])


def test_pr_create_failure_keeps_push_but_warns(tmp_path: Path) -> None:
    runner = _StubRunner(pr_ok=False)
    provider = GitHubDeliveryProvider(runner=runner)

    result = provider.publish(
        _intent(), branch="orcho/deliver/r1-add-x", cwd=tmp_path, remote="origin"
    )

    # Branch reached the remote; only PR creation failed.
    assert result.pushed is True
    assert result.pr_url is None
    assert result.warnings and "pull request" in result.warnings[0]
    assert runner.ran(["git", "push"])


def test_pr_create_success_without_url_warns(tmp_path: Path) -> None:
    runner = _StubRunner(pr_stdout="Warning: something noisy\n")
    provider = GitHubDeliveryProvider(runner=runner)

    result = provider.publish(
        _intent(), branch="orcho/deliver/r1-add-x", cwd=tmp_path, remote="origin"
    )

    assert result.pushed is True
    assert result.pr_url is None
    assert result.warnings


def test_publish_never_raises_on_runner_exception(tmp_path: Path) -> None:
    def _boom(argv: Sequence[str], cwd: Path) -> CommandResult:
        raise RuntimeError("subprocess exploded")

    provider = GitHubDeliveryProvider(runner=_boom)

    result = provider.publish(
        _intent(), branch="orcho/deliver/r1-add-x", cwd=tmp_path, remote="origin"
    )

    assert result.pushed is False
    assert result.warnings and "subprocess exploded" in result.warnings[0]


# --- default runner against fake gh / git on a temporary PATH ------------


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_default_runner_against_fake_path_binaries(
    tmp_path: Path, monkeypatch,
) -> None:
    # Fake ``gh`` / ``git`` shims on PATH — exercises the real subprocess runner
    # end-to-end while touching neither a network nor a real repository.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(
        bin_dir / "gh",
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "pr create") echo "https://github.com/acme/widget/pull/7"; exit 0;;\n'
        "esac\n"
        "exit 0\n",
    )
    _write_exec(bin_dir / "git", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    provider = GitHubDeliveryProvider()  # default (real subprocess) runner
    result = provider.publish(
        _intent(), branch="orcho/deliver/r1-add-x", cwd=tmp_path, remote="origin"
    )

    assert result.pushed is True
    assert result.pr_url == "https://github.com/acme/widget/pull/7"
    assert result.warnings == ()


# --- entry-point registration --------------------------------------------


def test_github_entry_point_declared_in_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[4] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    group = data["project"]["entry-points"][_GROUP]
    assert (
        group["github"]
        == "pipeline.engine.delivery_providers.github:GitHubDeliveryProvider"
    )


def test_github_provider_resolves_through_registry(monkeypatch) -> None:
    # Resolve the declared entry-point value through the shared discovery helper
    # (the editable install's metadata predates this group, so inject the exact
    # pyproject declaration rather than depend on a reinstall).
    pyproject = Path(__file__).resolve().parents[4] / "pyproject.toml"
    value = tomllib.loads(pyproject.read_text(encoding="utf-8"))[
        "project"
    ]["entry-points"][_GROUP]["github"]
    ep = importlib.metadata.EntryPoint(name="github", value=value, group=_GROUP)
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda group: (ep,) if group == _GROUP else (),
    )

    resolved = discover_entry_points(_GROUP)

    assert "github" in resolved
    provider = resolved["github"]
    assert isinstance(provider, GitHubDeliveryProvider)
    # Satisfies the DeliveryPublisher protocol structurally.
    assert callable(provider.publish)
