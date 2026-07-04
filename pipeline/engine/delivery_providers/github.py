"""Built-in GitHub delivery provider (ADR 0121).

Implements the :class:`~pipeline.engine.delivery_publish.DeliveryPublisher`
protocol for GitHub. This module is the single home for provider shell-outs: it
detects the ``gh`` CLI, verifies authentication, pushes the already-created
delivery branch, and opens a pull request over the existing signed commit via
``gh pr create``. It NEVER creates commits — the delivery commit was produced
upstream (ADR 0119) and this provider only publishes it.

Every failure mode — ``gh`` missing, auth failure, offline / push failure, or a
failed ``gh pr create`` — is captured as a :class:`PublishResult` warning; the
``publish`` method never raises. The subprocess runner is injectable so tests
drive the provider against a stub (or a fake ``gh`` on a temporary ``PATH``)
without touching a real network or opening a real pull request.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pipeline.engine.delivery_branch import DeliveryPrIntent
from pipeline.engine.delivery_publish import PublishResult

__all__ = ["CommandResult", "CommandRunner", "GitHubDeliveryProvider"]

# A URL as printed by ``gh pr create`` on success (github.com or an enterprise
# host); the command emits the pull-request URL on stdout.
_PR_URL_RE = re.compile(r"https?://\S+")

_GH_MISSING = (
    "gh CLI not found; delivery branch not pushed and no pull request opened; "
    "push the branch and open a pull request manually"
)

# Seconds before a provider shell-out is abandoned (network calls can hang).
_COMMAND_TIMEOUT = 120.0


def _is_github_remote(url: str) -> bool:
    """True when ``url`` points at a ``github.com`` git remote.

    Accepts the two forms Orcho emits and consumes:

    * ssh scp-like — ``git@github.com:owner/repo`` (optionally ``.git``)
    * https — ``https://github.com/owner/repo`` (optionally ``.git``)

    The host must be exactly ``github.com`` (case-insensitive). GitHub
    Enterprise / self-hosted hosts (e.g. ``github.example.com``) are
    intentionally out of scope: we do not try to guess self-hosted install
    hostnames here, so they read as non-github and keep the generic path.
    """
    return _remote_host(url) == "github.com"


def _remote_host(url: str) -> str | None:
    """Best-effort lowercase host for a git remote URL, or ``None``.

    Handles scheme URLs (``https://``, ``ssh://``) via :func:`urlsplit` and the
    scp-like ``[user@]host:path`` shorthand that has no scheme.
    """
    text = (url or "").strip()
    if not text:
        return None
    if "://" in text:
        try:
            host = urlsplit(text).hostname
        except ValueError:
            return None
        return host.lower() if host else None
    # scp-like syntax: ``[user@]host:path`` (host ends at the first ``:``).
    match = re.match(r"^(?:[^@/]+@)?([^/:]+):", text)
    return match.group(1).lower() if match else None


def _gh_install_hint() -> str:
    """Platform-appropriate one-liner for installing the ``gh`` CLI.

    Text only — this NEVER installs anything. On macOS it suggests Homebrew;
    elsewhere it points at the official download page rather than guessing a
    package manager.
    """
    if sys.platform == "darwin":
        return "install the gh CLI with `brew install gh`"
    return "install the gh CLI from https://cli.github.com"


def _gh_setup_recommendation() -> str:
    """Full setup recommendation: how to install/authenticate + what it buys.

    Descriptive text only; forming it performs no installation, authentication,
    or PATH changes.
    """
    return (
        f"GitHub remote detected: {_gh_install_hint()} and authenticate it "
        "(gh auth login) so Orcho can auto-push the delivery branch + open a "
        "PR on delivery"
    )


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a single provider shell-out."""

    ok: bool
    stdout: str = ""
    stderr: str = ""


# Injectable runner: given an argv and a working directory, run it and report a
# :class:`CommandResult`. The default is a real ``subprocess.run``; tests pass a
# stub so no provider binary or network is touched.
CommandRunner = Callable[[Sequence[str], Path], CommandResult]


def _default_runner(argv: Sequence[str], cwd: Path) -> CommandResult:
    """Run ``argv`` in ``cwd`` with terminal prompts disabled.

    Mirrors ``pipeline.engine.delivery_branch._run_git``: a missing binary,
    OS error, or timeout is reported as a non-ok :class:`CommandResult` rather
    than raised, so the provider can map it to a warning.
    """
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as exc:
        return CommandResult(ok=False, stderr=f"{argv[0] if argv else '?'} not found: {exc}")
    except OSError as exc:
        return CommandResult(ok=False, stderr=f"invocation failed: {exc}")
    except subprocess.TimeoutExpired:
        return CommandResult(
            ok=False, stderr=f"{argv[0] if argv else '?'} timed out after {_COMMAND_TIMEOUT:.0f}s"
        )
    return CommandResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


class GitHubDeliveryProvider:
    """Push a delivery branch and open a GitHub pull request (ADR 0121).

    The delivery branch already exists in ``cwd`` (the run worktree) over an
    already-signed commit; this provider pushes it and opens a pull request. It
    creates no commits. Construct with a custom ``runner`` to stub the shell in
    tests.
    """

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        self._runner: CommandRunner = runner or _default_runner

    def publish(
        self,
        pr_intent: DeliveryPrIntent,
        *,
        branch: str,
        cwd: Path,
        remote: str,
    ) -> PublishResult:
        """Push ``branch`` to ``remote`` and open a pull request.

        Returns a :class:`PublishResult`; never raises. Ordering: detect ``gh``
        → auth check → ``git push`` → ``gh pr create``. Each failed step short-
        circuits into a warning. A successful push followed by a failed pull-
        request creation returns ``pushed=True, pr_url=None`` with a warning, so
        the branch reaching the remote is not lost.
        """
        try:
            return self._publish(pr_intent, branch=branch, cwd=Path(cwd), remote=remote)
        except Exception as exc:  # noqa: BLE001 — a provider must never crash the run
            return PublishResult(
                pushed=False,
                warnings=(f"github delivery provider error: {exc}",),
            )

    def setup_hint(self, project_dir: Path) -> str | None:
        """Recommend gh setup when a GitHub remote exists but gh is not ready.

        Optional :class:`~pipeline.engine.delivery_publish.DeliveryPublisher`
        capability (ADR 0121, T1). Returns a human-readable recommendation only
        when ``project_dir``'s ``origin`` is a github.com remote AND the ``gh``
        CLI is missing or unauthenticated — the case where this provider *could*
        help but is not ready. Returns ``None`` for a non-github remote, when gh
        is already usable, or on any error. Best-effort and text-only: it never
        installs, authenticates, pushes, commits, or raises.
        """
        try:
            cwd = Path(project_dir)
            origin = self._run(["git", "remote", "get-url", "origin"], cwd)
            if not origin.ok or not _is_github_remote(origin.stdout):
                return None
            if self._gh_ready(cwd):
                return None
            return _gh_setup_recommendation()
        except Exception:  # noqa: BLE001 — setup hints are strictly best-effort
            return None

    # --- internal ---------------------------------------------------------

    def _publish(
        self,
        pr_intent: DeliveryPrIntent,
        *,
        branch: str,
        cwd: Path,
        remote: str,
    ) -> PublishResult:
        if not self._gh_available(cwd):
            return PublishResult(
                pushed=False,
                warnings=(self._gh_missing_warning(remote, cwd),),
            )

        auth = self._run(["gh", "auth", "status"], cwd)
        if not auth.ok:
            return PublishResult(
                pushed=False,
                warnings=(
                    "gh authentication check failed; delivery branch not pushed "
                    f"and no pull request opened: {_detail(auth)}",
                ),
            )

        push = self._run(["git", "push", "-u", remote, branch], cwd)
        if not push.ok:
            return PublishResult(
                pushed=False,
                warnings=(
                    f"git push of {branch} to {remote} failed; delivery branch "
                    f"not pushed: {_detail(push)}",
                ),
            )

        created = self._run(
            [
                "gh", "pr", "create",
                "--head", branch,
                "--base", pr_intent.base,
                "--title", pr_intent.title,
                "--body", pr_intent.body or pr_intent.title,
            ],
            cwd,
        )
        if not created.ok:
            return PublishResult(
                pushed=True,
                pr_url=None,
                warnings=(
                    f"delivery branch {branch} pushed but gh pr create failed; "
                    f"no pull request opened: {_detail(created)}",
                ),
            )

        pr_url = _extract_pr_url(created.stdout)
        if not pr_url:
            return PublishResult(
                pushed=True,
                pr_url=None,
                warnings=(
                    f"delivery branch {branch} pushed and gh pr create succeeded "
                    "but no pull request URL was returned",
                ),
            )
        return PublishResult(pushed=True, pr_url=pr_url)

    def _gh_missing_warning(self, remote: str, cwd: Path) -> str:
        """Warning text when ``gh`` is unavailable at push time.

        On a github.com ``remote`` the generic message is sharpened with an
        install hint so the operator knows how to enable auto-push + PR opening.
        On any other remote the provider-neutral :data:`_GH_MISSING` stays.
        """
        if not self._remote_is_github(remote, cwd):
            return _GH_MISSING
        return (
            "gh CLI not found; delivery branch not pushed and no pull request "
            f"opened. {_gh_install_hint()} and authenticate it (gh auth login) "
            "to enable auto-push + open a PR on delivery"
        )

    def _remote_is_github(self, remote: str, cwd: Path) -> bool:
        """True when ``remote`` resolves to a github.com URL in ``cwd``."""
        url = self._run(["git", "remote", "get-url", remote], cwd)
        return url.ok and _is_github_remote(url.stdout)

    def _gh_ready(self, cwd: Path) -> bool:
        """True only when ``gh`` is installed AND authenticated in ``cwd``."""
        if not self._gh_available(cwd):
            return False
        return self._run(["gh", "auth", "status"], cwd).ok

    def _gh_available(self, cwd: Path) -> bool:
        return self._run(["gh", "--version"], cwd).ok

    def _run(self, argv: Sequence[str], cwd: Path) -> CommandResult:
        return self._runner(argv, cwd)


def _detail(result: CommandResult) -> str:
    return (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()[0]


def _extract_pr_url(stdout: str) -> str | None:
    matches = _PR_URL_RE.findall(stdout or "")
    return matches[-1] if matches else None
