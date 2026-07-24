"""Provider-neutral delivery publishing tests (ADR 0121, T1).

Covers the :func:`publish_delivery` orchestrator in isolation and its single
integration point in :func:`apply_commit_delivery` (only reachable on an
``approve`` + ``worktree_branch`` publish). A registered provider is injected by
monkeypatching the entry-point discovery so no real plugin is installed and no
provider binary (``gh`` / ``git push``) is ever executed.

The final ``test_engine_modules_never_shell_a_git_provider`` guard scans every
``pipeline/engine/**/*.py`` module (except the future provider package) to hold
the boundary that provider execution lives only inside a provider plugin.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.io.git_helpers import create_worktree
from pipeline.engine import delivery_publish
from pipeline.engine.commit_delivery import (
    apply_commit_delivery,
    resolve_commit_delivery,
)
from pipeline.engine.delivery_branch import DeliveryPrIntent
from pipeline.engine.delivery_publish import (
    DELIVERY_PROVIDER_GROUP,
    PublishResult,
    collect_delivery_setup_hints,
    normalize_publish_gate,
    publish_delivery,
)

# --- fakes ---------------------------------------------------------------


class _FakeProvider:
    """In-test :class:`DeliveryPublisher` that records every ``publish`` call."""

    def __init__(
        self,
        *,
        pr_url: str | None = "https://example.invalid/pr/1",
        raises: bool = False,
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[SimpleNamespace] = []
        self._pr_url = pr_url
        self._raises = raises
        self._warnings = warnings

    def publish(
        self,
        pr_intent: DeliveryPrIntent,
        *,
        branch: str,
        cwd: Path,
        remote: str,
    ) -> PublishResult:
        self.calls.append(
            SimpleNamespace(
                pr_intent=pr_intent, branch=branch, cwd=cwd, remote=remote
            )
        )
        if self._raises:
            raise RuntimeError("provider blew up")
        return PublishResult(
            pushed=True, pr_url=self._pr_url, warnings=self._warnings
        )


class _ResultProvider:
    """Provider fake that deliberately returns an arbitrary result object."""

    def __init__(self, result: object) -> None:
        self._result = result

    def publish(self, *_: object, **__: object) -> object:
        return self._result


def _register(monkeypatch: pytest.MonkeyPatch, **providers: object) -> None:
    """Monkeypatch entry-point discovery to return the given providers."""

    def _fake_discover(group: str, **_: object) -> dict[str, object]:
        assert group == DELIVERY_PROVIDER_GROUP
        return dict(providers)

    monkeypatch.setattr(delivery_publish, "discover_entry_points", _fake_discover)


def _pr_intent() -> DeliveryPrIntent:
    return DeliveryPrIntent(
        branch="orcho/deliver/r1-x",
        base="main",
        title="Add x",
        suggested_command="git push -u origin orcho/deliver/r1-x",
        body="Add x\n\ndetails",
    )


def _publish_result_missing_fields() -> PublishResult:
    """Create a malformed dataclass instance missing its optional fields."""
    result = object.__new__(PublishResult)
    object.__setattr__(result, "pushed", True)
    return result


# --- publish_delivery() orchestrator (no git) ----------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", "off"),
        (" AUTO ", "auto"),
        ("AlWaYs", "always"),
        ("", "auto"),
        ("invalid", "auto"),
        (None, "auto"),
        (True, "auto"),
    ],
)
def test_normalize_publish_gate_accepts_only_supported_modes(
    raw: object, expected: str
) -> None:
    assert normalize_publish_gate(raw) == expected


def test_publish_off_never_resolves_or_invokes_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()

    def _explode(*_: object, **__: object) -> dict[str, object]:
        raise AssertionError("provider discovery must not run when publish=off")

    monkeypatch.setattr(delivery_publish, "discover_entry_points", _explode)

    result = publish_delivery(
        _pr_intent(),
        branch="orcho/deliver/r1-x",
        cwd=Path("."),
        commit_config={"publish": "off"},
    )

    assert result == PublishResult(pushed=False)
    assert provider.calls == []


def test_publish_auto_invokes_registered_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(pr_url="https://example.invalid/pr/7")
    _register(monkeypatch, github=provider)

    result = publish_delivery(
        _pr_intent(),
        branch="orcho/deliver/r1-x",
        cwd=Path("/run/wt"),
        remote="origin",
        commit_config={"publish": "auto"},
    )

    assert result.pushed is True
    assert result.pr_url == "https://example.invalid/pr/7"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call.branch == "orcho/deliver/r1-x"
    assert call.cwd == Path("/run/wt")
    assert call.remote == "origin"


def test_publish_always_uses_the_same_provider_resolution_as_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    _register(monkeypatch, github=provider)

    result = publish_delivery(
        _pr_intent(),
        branch="orcho/deliver/r1-x",
        cwd=Path("/run/wt"),
        commit_config={"publish": "always"},
    )

    assert result.pushed is True
    assert len(provider.calls) == 1


def test_publish_no_provider_degrades_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch)  # empty registry

    result = publish_delivery(
        _pr_intent(),
        branch="orcho/deliver/r1-x",
        cwd=Path("."),
        commit_config={"publish": "auto"},
    )

    assert result == PublishResult(pushed=False)


def test_publish_always_without_provider_matches_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch)

    auto = publish_delivery(
        _pr_intent(), branch="orcho/deliver/r1-x", cwd=Path("."),
        commit_config={"publish": "auto"},
    )
    always = publish_delivery(
        _pr_intent(), branch="orcho/deliver/r1-x", cwd=Path("."),
        commit_config={"publish": "always"},
    )

    assert always == auto == PublishResult(pushed=False)


def test_publish_provider_error_becomes_warning_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch, github=_FakeProvider(raises=True))

    result = publish_delivery(
        _pr_intent(),
        branch="orcho/deliver/r1-x",
        cwd=Path("."),
        commit_config={"publish": "auto"},
    )

    assert result.pushed is False
    assert result.pr_url is None
    assert result.warnings
    assert "provider blew up" in result.warnings[0]


@pytest.mark.parametrize(
    "malformed",
    [
        PublishResult(pushed=True, pr_url=123),  # type: ignore[arg-type]
        PublishResult(pushed=True, warnings="provider warning"),  # type: ignore[arg-type]
        PublishResult(pushed=True, warnings=["provider warning"]),  # type: ignore[arg-type]
        _publish_result_missing_fields(),
    ],
    ids=["pr-url-int", "warnings-str", "warnings-list", "missing-fields"],
)
def test_publish_malformed_provider_result_degrades_to_typed_warning(
    monkeypatch: pytest.MonkeyPatch, malformed: object
) -> None:
    _register(monkeypatch, github=_ResultProvider(malformed))

    result = publish_delivery(
        _pr_intent(),
        branch="orcho/deliver/r1-x",
        cwd=Path("."),
        commit_config={"publish": "auto"},
    )

    assert result == PublishResult(
        pushed=False,
        warnings=("delivery publish provider returned an invalid result",),
    )


def test_publish_selects_named_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = _FakeProvider(pr_url="https://example.invalid/pr/chosen")
    other = _FakeProvider(pr_url="https://example.invalid/pr/other")
    _register(monkeypatch, github=chosen, gitlab=other)

    result = publish_delivery(
        _pr_intent(),
        branch="orcho/deliver/r1-x",
        cwd=Path("."),
        commit_config={"publish": "auto", "publish_provider": "gitlab"},
    )

    assert result.pr_url == "https://example.invalid/pr/other"
    assert len(other.calls) == 1
    assert chosen.calls == []


def test_publish_ambiguous_registry_warns_without_invoking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _FakeProvider()
    b = _FakeProvider()
    _register(monkeypatch, github=a, gitlab=b)

    result = publish_delivery(
        _pr_intent(),
        branch="orcho/deliver/r1-x",
        cwd=Path("."),
        commit_config={"publish": "auto"},
    )

    assert result.pushed is False
    assert result.warnings
    assert a.calls == [] and b.calls == []


# --- integration through apply_commit_delivery (real git worktree) -------


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True
    )
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return _head(repo)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _session(summary: str = "feat: update app") -> dict:
    return {
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "short_summary": summary,
            },
        },
    }


def _make_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    result = create_worktree(
        repo=repo,
        base_ref=_head(repo),
        target_path=run_dir / "checkout",
        branch_name="orcho/run/r1",
    )
    assert result.ok, result.error
    worktree = run_dir / "checkout"
    (worktree / "app.txt").write_text("base\nrun\n", encoding="utf-8")
    return repo, worktree, run_dir


def _apply(
    repo: Path,
    worktree: Path,
    run_dir: Path,
    *,
    action: str = "approve",
    publish: str = "auto",
    branch_policy: str = "worktree_branch",
):
    commit_config: dict = {
        "enabled": True,
        "auto_in_ci": action,
        "add_untracked": True,
        "branch_policy": branch_policy,
        "publish": publish,
    }
    decision = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=worktree,
        run_dir=run_dir,
        run_id="r1",
        session=_session(),
        commit_config=commit_config,
        no_interactive=True,
        baseline_ref="HEAD",
    )
    return apply_commit_delivery(decision, run_dir=run_dir, commit_config=commit_config)


def test_invoke_on_approve_threads_pr_url_into_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider(pr_url="https://example.invalid/pr/42")
    _register(monkeypatch, github=provider)
    repo, worktree, run_dir = _make_run(tmp_path)

    delivered = _apply(repo, worktree, run_dir, action="approve")

    assert delivered.status == "committed"
    assert len(provider.calls) == 1
    assert provider.calls[0].branch == delivered.delivery_branch
    assert "PR opened: https://example.invalid/pr/42" in delivered.delivery_notices


def test_not_invoked_on_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider()
    _register(monkeypatch, github=provider)
    repo, worktree, run_dir = _make_run(tmp_path)

    delivered = _apply(repo, worktree, run_dir, action="apply")

    assert delivered.status == "applied_uncommitted"
    assert provider.calls == []


def test_publish_off_does_not_invoke_provider_but_still_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider()
    _register(monkeypatch, github=provider)
    repo, worktree, run_dir = _make_run(tmp_path)

    delivered = _apply(repo, worktree, run_dir, action="approve", publish="off")

    assert delivered.status == "committed"
    assert delivered.delivery_branch is not None
    assert provider.calls == []
    assert any("ready" in n for n in delivered.delivery_notices)


def test_commit_on_branch_always_publishes_target_checkout_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider(
        pr_url="https://example.invalid/pr/always",
        warnings=("provider warning",),
    )
    _register(monkeypatch, github=provider)
    repo, _, run_dir = _make_run(tmp_path)
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    from pipeline.engine import commit_delivery

    original_outcome = commit_delivery._resolve_delivery_branch_outcome

    def _with_branch_warning(*args: object, **kwargs: object):
        return replace(
            original_outcome(*args, **kwargs), warnings=("branch warning",)
        )

    monkeypatch.setattr(
        commit_delivery, "_resolve_delivery_branch_outcome", _with_branch_warning
    )
    delivered = _apply(
        repo,
        repo,
        run_dir,
        publish="always",
        branch_policy="protect_default",
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha == _head(repo)
    assert delivered.published_commit_sha is None
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call.branch == delivered.delivery_branch
    assert call.pr_intent == delivered.pr_intent
    assert call.cwd == repo
    assert call.remote == "origin"
    assert delivered.pr_url == "https://example.invalid/pr/always"
    assert delivered.to_dict()["pr_url"] == delivered.pr_url
    assert f"PR opened: {delivered.pr_url}" in delivered.delivery_notices
    assert delivered.delivery_warnings == ("branch warning", "provider warning")


@pytest.mark.parametrize("raises", [False, True])
def test_commit_on_branch_always_degrades_to_branch_ready_after_publish_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    if raises:
        _register(monkeypatch, github=_FakeProvider(raises=True))
    else:
        _register(monkeypatch)
    repo, _, run_dir = _make_run(tmp_path)
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply(
        repo,
        repo,
        run_dir,
        publish="always",
        branch_policy="protect_default",
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha == _head(repo)
    assert delivered.pr_url is None
    assert any("is ready; open a pull request" in n for n in delivered.delivery_notices)
    if raises:
        assert any("provider blew up" in warning for warning in delivered.delivery_warnings)


def test_commit_on_branch_auto_keeps_local_payload_and_skips_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider()
    _register(monkeypatch, github=provider)
    repo, _, run_dir = _make_run(tmp_path)
    (repo / "app.txt").write_text("base\nrun\n", encoding="utf-8")

    delivered = _apply(
        repo,
        repo,
        run_dir,
        publish="auto",
        branch_policy="protect_default",
    )

    assert delivered.status == "committed"
    assert delivered.commit_sha == _head(repo)
    assert delivered.delivery_branch is not None
    assert delivered.pr_url is None
    assert delivered.delivery_notices == ()
    assert provider.calls == []


def test_commit_in_place_always_does_not_publish_delivery_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider()
    _register(monkeypatch, github=provider)
    repo, worktree, run_dir = _make_run(tmp_path)

    delivered = _apply(
        repo,
        worktree,
        run_dir,
        publish="always",
        branch_policy="bypass",
    )

    assert delivered.status == "committed"
    assert delivered.delivery_branch is None
    assert delivered.pr_intent is None
    assert delivered.pr_url is None
    assert delivered.delivery_warnings == ()
    assert delivered.delivery_notices == ()
    assert provider.calls == []


def test_degrade_no_provider_notices_without_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch)  # empty registry
    repo, worktree, run_dir = _make_run(tmp_path)

    delivered = _apply(repo, worktree, run_dir, action="approve")

    assert delivered.status == "committed"
    assert delivered.delivery_branch is not None
    assert any("ready" in n for n in delivered.delivery_notices)
    assert delivered.delivery_warnings == ()


def test_provider_error_records_warning_and_stays_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(monkeypatch, github=_FakeProvider(raises=True))
    repo, worktree, run_dir = _make_run(tmp_path)

    delivered = _apply(repo, worktree, run_dir, action="approve")

    assert delivered.status == "committed"
    assert any("provider blew up" in w for w in delivered.delivery_warnings)


# --- collect_delivery_setup_hints() (provider-neutral, no git) -----------


class _HintProvider:
    """Provider exposing an optional ``setup_hint`` (string / None / raises)."""

    def __init__(
        self, *, hint: str | None = "install the thing", raises: bool = False
    ) -> None:
        self._hint = hint
        self._raises = raises
        self.calls: list[Path] = []

    def publish(self, *_: object, **__: object) -> PublishResult:  # pragma: no cover
        return PublishResult(pushed=False)

    def setup_hint(self, project_dir: Path) -> str | None:
        self.calls.append(project_dir)
        if self._raises:
            raise RuntimeError("hint probe blew up")
        return self._hint


class _NoHintProvider:
    """Legal provider WITHOUT a ``setup_hint`` method (optional capability)."""

    def publish(self, *_: object, **__: object) -> PublishResult:  # pragma: no cover
        return PublishResult(pushed=False)


def test_collect_hints_gathers_from_capable_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _HintProvider(hint="install gh and authenticate")
    _register(monkeypatch, github=provider)

    hints = collect_delivery_setup_hints(Path("/proj"))

    assert hints == ["install gh and authenticate"]
    assert provider.calls == [Path("/proj")]


def test_collect_hints_skips_provider_without_setup_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch, plain=_NoHintProvider())

    assert collect_delivery_setup_hints(Path("/proj")) == []


def test_collect_hints_skips_none_returning_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch, github=_HintProvider(hint=None))

    assert collect_delivery_setup_hints(Path("/proj")) == []


def test_collect_hints_swallows_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = _HintProvider(hint="do the good setup")
    bad = _HintProvider(raises=True)
    _register(monkeypatch, boom=bad, ok=good)

    # The raising provider is silently skipped; the healthy one still surfaces.
    assert collect_delivery_setup_hints(Path("/proj")) == ["do the good setup"]


def test_collect_hints_deduplicates_in_discovery_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(
        monkeypatch,
        a=_HintProvider(hint="same hint"),
        b=_HintProvider(hint="other hint"),
        c=_HintProvider(hint="same hint"),
    )

    hints = collect_delivery_setup_hints(Path("/proj"))

    assert hints == ["same hint", "other hint"]


def test_collect_hints_empty_registry_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch)  # no providers

    assert collect_delivery_setup_hints(Path("/proj")) == []


def test_collect_hints_discovery_failure_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("discovery is down")

    monkeypatch.setattr(delivery_publish, "discover_entry_points", _explode)

    assert collect_delivery_setup_hints(Path("/proj")) == []


def test_collect_hints_non_callable_setup_hint_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A provider whose ``setup_hint`` attribute is not callable must not crash
    # the collector; it is treated the same as a provider without the method.
    weird = SimpleNamespace(setup_hint="not-callable")
    _register(monkeypatch, weird=weird)

    assert collect_delivery_setup_hints(Path("/proj")) == []


# --- guard: provider execution lives only in a provider plugin -----------


# argv-shaped detectors — deliberately NOT matching the plain-``git`` suggested
# command string or the ``gh`` / ``glab`` prose in ADR 0119 docstrings.
_PROVIDER_SHELL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gh argv", re.compile(r"""["']gh["']\s*[,)\]]""")),
    ("gh pr create", re.compile(r"gh pr create")),
    ("glab argv", re.compile(r"""["']glab["']\s*[,)\]]""")),
    ("git push argv", re.compile(r"""["']git["']\s*,\s*["']push["']""")),
)


def _provider_shell_hits(text: str) -> list[str]:
    return [label for label, pat in _PROVIDER_SHELL_PATTERNS if pat.search(text)]


def test_guard_detector_flags_provider_shell_forms() -> None:
    # The guard must actually fire on real provider-execution argv, so a
    # regression that shells a provider from any engine module is caught.
    assert _provider_shell_hits('subprocess.run(["gh", "pr", "create"])')
    assert _provider_shell_hits('run(["git", "push", "origin", branch])')
    assert _provider_shell_hits('cmd = "gh pr create --fill"')
    assert _provider_shell_hits('subprocess.run(["glab", "mr", "create"])')
    # ...and must NOT flag the provider-neutral ADR 0119 surface.
    assert not _provider_shell_hits(
        'suggested_command=f"git push -u {remote} {branch}"'
    )
    assert not _provider_shell_hits("core names no ``gh`` / ``glab`` binary")


def test_engine_modules_never_shell_a_git_provider() -> None:
    engine_dir = Path(delivery_publish.__file__).resolve().parent
    offenders: dict[str, list[str]] = {}
    for path in sorted(engine_dir.rglob("*.py")):
        rel = path.relative_to(engine_dir).as_posix()
        # The provider package is the ONE allowed home for gh / git push.
        if rel == "delivery_providers/github.py":
            continue
        if path.name.startswith("test_"):
            continue
        hits = _provider_shell_hits(path.read_text(encoding="utf-8"))
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "gh / git push executed outside the delivery provider plugin: "
        f"{offenders}"
    )
