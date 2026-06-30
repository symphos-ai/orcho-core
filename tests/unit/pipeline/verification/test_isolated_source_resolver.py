"""Focused unit tests for the fail-closed isolated-source resolver (T1).

Pins the resolver contract (:mod:`pipeline.engine.worktree_source`) and its two
wiring points: ``{dependency:X}`` redirection in
:func:`pipeline.verification_contract.placeholder_context_for` and the verify-env
cwd binding in :func:`pipeline.verification_env.resolve_env_runtime`.

The three load-bearing guarantees, per the T1 done-criteria:
  * isolated repo  → worktree path,
  * sibling / unresolved source for an isolated repo → hard error,
  * no isolation (or genuine external dependency) → ambient/sibling fallback.

Real on-disk temp dirs (no mocks) so ``realpath`` normalisation — including the
macOS ``/var`` → ``/private/var`` symlink — matches the production path math.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.engine.worktree_source import (
    IsolatedSource,
    IsolatedSourceError,
    identifies_isolated_repo,
    isolated_source_from_meta,
    resolve_isolated_repo_source,
)
from pipeline.verification_contract import (
    VerificationContract,
    placeholder_context_for,
)
from pipeline.verification_env import resolve_env_runtime


def _make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Return (canonical_sibling, worktree_checkout), both materialised."""
    sibling = tmp_path / "canonical" / "orcho-core"
    worktree = tmp_path / "worktrees" / "wt_run" / "checkout"
    sibling.mkdir(parents=True)
    worktree.mkdir(parents=True)
    return sibling, worktree


def _isolated(sibling: Path, worktree: Path, *, isolation: str = "per_run") -> IsolatedSource:
    return IsolatedSource(
        isolation=isolation,
        worktree_path=str(worktree),
        source_repo_path=str(sibling),
    )


# ── isolated_source_from_meta ──────────────────────────────────────────────


class TestFromMeta:
    def test_none_for_missing_block(self) -> None:
        assert isolated_source_from_meta(None) is None

    def test_none_for_empty_block(self) -> None:
        assert isolated_source_from_meta({}) is None

    def test_off_block_is_not_isolated(self) -> None:
        src = isolated_source_from_meta(
            {"isolation": "off", "path": "/x", "source_repo_path": "/x"},
        )
        assert src is not None
        assert src.is_isolated is False

    def test_per_run_block_is_isolated(self) -> None:
        src = isolated_source_from_meta(
            {
                "isolation": "per_run",
                "path": "/wt/checkout",
                "source_repo_path": "/canonical/repo",
            },
        )
        assert src is not None
        assert src.is_isolated is True
        assert src.worktree_path == "/wt/checkout"
        assert src.source_repo_path == "/canonical/repo"


# ── resolve_isolated_repo_source ───────────────────────────────────────────


class TestResolver:
    def test_isolated_repo_redirects_to_worktree(self, tmp_path: Path) -> None:
        sibling, worktree = _make_dirs(tmp_path)
        isolated = _isolated(sibling, worktree)

        out = resolve_isolated_repo_source(
            repo_name="orcho-core", candidate=str(sibling), isolated=isolated,
        )
        assert Path(out) == worktree

    def test_no_isolation_falls_back_to_candidate(self, tmp_path: Path) -> None:
        sibling, worktree = _make_dirs(tmp_path)
        # isolation == off → not isolated → ambient fallback.
        isolated = _isolated(sibling, worktree, isolation="off")
        out = resolve_isolated_repo_source(
            repo_name="orcho-core", candidate=str(sibling), isolated=isolated,
        )
        assert out == str(sibling)

    def test_none_isolated_falls_back_to_candidate(self, tmp_path: Path) -> None:
        sibling, _ = _make_dirs(tmp_path)
        out = resolve_isolated_repo_source(
            repo_name="orcho-core", candidate=str(sibling), isolated=None,
        )
        assert out == str(sibling)

    def test_external_dependency_keeps_sibling(self, tmp_path: Path) -> None:
        sibling, worktree = _make_dirs(tmp_path)
        external = tmp_path / "canonical" / "some-other-repo"
        external.mkdir(parents=True)
        isolated = _isolated(sibling, worktree)

        out = resolve_isolated_repo_source(
            repo_name="some-other-repo", candidate=str(external), isolated=isolated,
        )
        # A different repo than the isolated one — sibling fallback stays legal.
        assert out == str(external)

    def test_unresolved_worktree_is_hard_error(self, tmp_path: Path) -> None:
        sibling, _ = _make_dirs(tmp_path)
        # Declared isolation (``per_run``) whose worktree path is unresolved
        # (empty): isolation is in force but the verify source cannot bind to a
        # worktree. This must hard-error fail-closed, NOT silently fall back to the
        # canonical sibling (ADR 0112 §3 / review F2). ``is_declared`` is the
        # in-force signal; ``is_isolated`` (a usable path) is still False.
        isolated = IsolatedSource(
            isolation="per_run", worktree_path="", source_repo_path=str(sibling),
        )
        assert isolated.is_declared is True
        assert isolated.is_isolated is False
        with pytest.raises(IsolatedSourceError) as exc:
            resolve_isolated_repo_source(
                repo_name="orcho-core", candidate=str(sibling), isolated=isolated,
            )
        # Reason must be diagnosable: repo name + the unresolved-worktree marker.
        assert "orcho-core" in str(exc.value)
        assert "<unresolved>" in str(exc.value)

    def test_off_isolation_empty_path_falls_back(self, tmp_path: Path) -> None:
        sibling, _ = _make_dirs(tmp_path)
        # Contrast with the hard-error above: when isolation is OFF (or unset), an
        # empty worktree path is a genuine single-checkout run, so the ambient
        # sibling fallback stays legal and nothing raises.
        isolated = IsolatedSource(
            isolation="off", worktree_path="", source_repo_path=str(sibling),
        )
        assert isolated.is_declared is False
        out = resolve_isolated_repo_source(
            repo_name="orcho-core", candidate=str(sibling), isolated=isolated,
        )
        assert out == str(sibling)

    def test_worktree_equal_to_sibling_is_hard_error(self, tmp_path: Path) -> None:
        sibling, _ = _make_dirs(tmp_path)
        # Degraded isolation: the "worktree" path points back at the canonical
        # sibling. Verifying there would pass a clean tree vacuously → fail closed.
        isolated = IsolatedSource(
            isolation="per_run",
            worktree_path=str(sibling),
            source_repo_path=str(sibling),
        )
        with pytest.raises(IsolatedSourceError) as exc:
            resolve_isolated_repo_source(
                repo_name="orcho-core", candidate=str(sibling), isolated=isolated,
            )
        # Reason must be diagnosable: repo name + sibling path.
        assert "orcho-core" in str(exc.value)
        assert str(sibling) in str(exc.value)

    def test_identifies_isolated_repo(self, tmp_path: Path) -> None:
        sibling, worktree = _make_dirs(tmp_path)
        isolated = _isolated(sibling, worktree)
        assert identifies_isolated_repo(str(sibling), isolated) is True
        assert identifies_isolated_repo(str(worktree), isolated) is False
        assert identifies_isolated_repo(str(sibling), None) is False


# ── placeholder_context_for: {dependency:X} redirection ────────────────────


def _contract_with_dep(dep_path: str) -> VerificationContract:
    return VerificationContract(
        dependency_repos={"orcho-core": {"path": dep_path}},
        verification_envs={},
        commands={},
        schedule=(),
        default_env="",
        required=(),
        work_mode="",
    )


class TestPlaceholderDependencyRedirect:
    def test_isolated_dependency_points_at_worktree(self, tmp_path: Path) -> None:
        sibling, worktree = _make_dirs(tmp_path)
        contract = _contract_with_dep(str(sibling))

        ctx = placeholder_context_for(
            contract,
            checkout=str(worktree),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
            worktree={
                "isolation": "per_run",
                "path": str(worktree),
                "source_repo_path": str(sibling),
            },
        )
        assert Path(ctx.dependencies["orcho-core"]) == worktree
        assert ctx.isolated_source is not None

    def test_single_checkout_dependency_unchanged(self, tmp_path: Path) -> None:
        sibling, _ = _make_dirs(tmp_path)
        contract = _contract_with_dep(str(sibling))

        ctx = placeholder_context_for(
            contract,
            checkout=str(sibling),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
        )
        assert ctx.dependencies["orcho-core"] == str(sibling)
        assert ctx.isolated_source is None

    def test_threaded_participant_set_is_source_of_truth(self, tmp_path: Path) -> None:
        """A threaded run-scoped ParticipantSet overrides path-gap derivation.

        The path inputs alone (``checkout`` == ``project``) would derive NO isolated
        source. With the run-scoped set threaded, the placeholder reads ITS primary
        participant — the isolated worktree — and the dependency redirects to it.
        This breaks if ``placeholder_context_for`` rebuilds an independent set from
        the paths instead of reading the threaded one (review F1).
        """
        from pipeline.participants import ParticipantSet

        sibling, worktree = _make_dirs(tmp_path)
        contract = _contract_with_dep(str(sibling))
        # The seeded set carries the real isolated worktree even though the path
        # inputs below collapse onto the canonical sibling.
        pset = ParticipantSet.for_mono(
            checkout=str(worktree), project=str(sibling),
        )

        ctx = placeholder_context_for(
            contract,
            checkout=str(sibling),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
            participant_set=pset,
        )
        assert ctx.isolated_source is not None
        assert Path(ctx.isolated_source.worktree_path) == worktree
        assert Path(ctx.dependencies["orcho-core"]) == worktree

    def test_empty_participant_set_falls_back_to_paths(self, tmp_path: Path) -> None:
        """An empty threaded set is ignored — path-gap derivation (back-compat)."""
        from pipeline.participants import ParticipantSet

        sibling, worktree = _make_dirs(tmp_path)
        contract = _contract_with_dep(str(sibling))
        ctx = placeholder_context_for(
            contract,
            checkout=str(worktree),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
            worktree={
                "isolation": "per_run",
                "path": str(worktree),
                "source_repo_path": str(sibling),
            },
            participant_set=ParticipantSet(isolation="per_run"),
        )
        assert Path(ctx.dependencies["orcho-core"]) == worktree
        assert ctx.isolated_source is not None

    def test_degraded_isolated_dependency_hard_errors(self, tmp_path: Path) -> None:
        sibling, _ = _make_dirs(tmp_path)
        contract = _contract_with_dep(str(sibling))

        with pytest.raises(IsolatedSourceError):
            placeholder_context_for(
                contract,
                checkout=str(sibling),
                project=str(sibling),
                workspace=str(tmp_path),
                run_dir=None,
                worktree={
                    "isolation": "per_run",
                    "path": str(sibling),  # degraded: points at the sibling
                    "source_repo_path": str(sibling),
                },
            )


# ── resolve_env_runtime: cwd binding ───────────────────────────────────────


class TestEnvCwdBinding:
    def test_isolated_cwd_defaults_to_worktree(self, tmp_path: Path) -> None:
        sibling, worktree = _make_dirs(tmp_path)
        contract = _contract_with_dep(str(sibling))
        ctx = placeholder_context_for(
            contract,
            checkout=str(worktree),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
            worktree={
                "isolation": "per_run",
                "path": str(worktree),
                "source_repo_path": str(sibling),
            },
        )
        # No declared cwd: eff_cwd uses ctx.checkout (the worktree) — already the
        # right tree, and the fail-closed pass leaves it untouched.
        _python, eff_cwd, _env, _ov = resolve_env_runtime({}, ctx)
        assert Path(eff_cwd) == worktree

    def test_isolated_cwd_to_project_redirects_to_worktree(self, tmp_path: Path) -> None:
        sibling, worktree = _make_dirs(tmp_path)
        contract = _contract_with_dep(str(sibling))
        ctx = placeholder_context_for(
            contract,
            checkout=str(worktree),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
            worktree={
                "isolation": "per_run",
                "path": str(worktree),
                "source_repo_path": str(sibling),
            },
        )
        # An env that explicitly declares cwd at the canonical sibling must be
        # redirected to the worktree, not silently run against the clean tree.
        _python, eff_cwd, _env, _ov = resolve_env_runtime({"cwd": "{project}"}, ctx)
        assert Path(eff_cwd) == worktree

    def test_single_checkout_cwd_unchanged(self, tmp_path: Path) -> None:
        sibling, _ = _make_dirs(tmp_path)
        contract = _contract_with_dep(str(sibling))
        ctx = placeholder_context_for(
            contract,
            checkout=str(sibling),
            project=str(sibling),
            workspace=str(tmp_path),
            run_dir=None,
        )
        _python, eff_cwd, _env, _ov = resolve_env_runtime({}, ctx)
        assert eff_cwd == str(sibling)
