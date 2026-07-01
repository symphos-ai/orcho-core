# SPDX-License-Identifier: Apache-2.0
"""Soft-degradation + private-helper coverage for ``delivery_scope``.

Sibling of ``test_delivery_scope.py`` (which is at the ~700-line norm and owns
the pure-classification + real-repo integration groups). This file holds the
scope-coercion branch, the soft-degradation paths (every one driven by
monkeypatching a *collaborator* / lazily-imported function, never the
function-under-test), and direct calls into the private helpers. No real git
repos are needed: these branches are pure-computational or collaborator-faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pipeline.engine.delivery_scope as ds
from pipeline.engine.delivery_scope import (
    DELIVERY_SCOPE_VIOLATION,
    _known_workspace_aliases,
    _load_durable_plan,
    _persist_companion_bases,
    _recorded_companion_delivery,
    _resolve_alias_path,
    _resolve_workspace,
    _safe_resolve,
    assess_delivery_scope,
    collect_sibling_changes,
    evaluate_delivery_scope,
    primary_alias_for,
    record_companion_bases_at_detection,
)
from pipeline.runtime.run_shape import DeliveryScope

# ── (121) assess_delivery_scope: string scope is coerced ──────────────────────


def test_assess_delivery_scope_coerces_string_scope() -> None:
    """Line 121: a raw string scope is coerced to ``DeliveryScope`` and classed.

    Passing ``"strict_mono"`` (not a ``DeliveryScope``) with sibling changes must
    coerce, then classify as a strict-mono violation — proving the coercion fed
    the real enum branch rather than silently no-op'ing.
    """
    result = assess_delivery_scope(
        scope="strict_mono",  # type: ignore[arg-type]
        sibling_changes={"sib": ["sib/a.py"]},
    )

    assert result.scope is DeliveryScope.STRICT_MONO
    assert result.blocked is True
    assert result.blocker == DELIVERY_SCOPE_VIOLATION


# ── (193, 201-202) collect_sibling_changes soft degradation ───────────────────


def test_collect_sibling_changes_skips_blank_and_non_string_aliases(
    tmp_path: Path,
) -> None:
    """Line 193: empty/whitespace/non-string aliases are skipped, yielding {}."""
    out = collect_sibling_changes(
        delivery_projects=["", "   ", 123],  # type: ignore[list-item]
        primary_project_dir=tmp_path / "primary",
        workspace="ws",  # non-empty → no workspace I/O
    )

    assert out == {}


def test_collect_sibling_changes_swallows_git_changed_files_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 201-202: a raising ``git_changed_files`` degrades to no entry."""
    sib = tmp_path / "sib"
    monkeypatch.setattr(ds, "_resolve_alias_path", lambda alias, ws: sib)

    def _boom(_repo: str) -> list[str]:
        raise RuntimeError("git exploded")

    monkeypatch.setattr(ds, "git_changed_files", _boom)

    out = collect_sibling_changes(
        delivery_projects=["sib"],
        primary_project_dir=tmp_path / "primary",
        workspace="ws",
    )

    assert out == {}


# ── (225, 229) primary_alias_for soft degradation ─────────────────────────────


def test_primary_alias_for_returns_empty_when_primary_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line 225: an unresolvable primary dir yields ''."""
    monkeypatch.setattr(ds, "_safe_resolve", lambda path: None)

    assert primary_alias_for(["a"], tmp_path) == ""


def test_primary_alias_for_skips_blank_aliases(tmp_path: Path) -> None:
    """Line 229: blank aliases are skipped, so the result falls through to ''."""
    result = primary_alias_for(
        ["", "   "], tmp_path, workspace="ws",
    )

    assert result == ""


# ── (269-270, 325-326) evaluate_delivery_scope degradation ────────────────────


def test_evaluate_delivery_scope_invalid_scope_returns_none(
    tmp_path: Path,
) -> None:
    """Lines 269-270: an invalid ``delivery_scope`` value degrades to None."""
    session = {"auto_detect": {"delivery_scope": "bogus-scope"}}

    result = evaluate_delivery_scope(
        session=session, primary_project_dir=tmp_path,
    )

    assert result is None


def test_evaluate_delivery_scope_swallows_collaborator_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 325-326: an exception from a companion collaborator degrades to None."""
    import pipeline.engine.companion_scope as companion_scope

    def _boom(**kwargs: object) -> object:
        raise RuntimeError("derive exploded")

    monkeypatch.setattr(companion_scope, "derive_companion_aliases", _boom)

    session = {"auto_detect": {"delivery_scope": "strict_mono"}}
    result = evaluate_delivery_scope(
        session=session, primary_project_dir=tmp_path,
    )

    assert result is None


# ── (358, 378, 388-389) record_companion_bases_at_detection degradation ───────


@pytest.mark.parametrize(
    "session",
    [{}, {"auto_detect": {}}, {"auto_detect": {"delivery_scope": ""}}],
)
def test_record_companion_bases_no_scope_is_noop(
    tmp_path: Path, session: dict,
) -> None:
    """Line 358: a session with no usable ``delivery_scope`` is a strict no-op."""
    before = {k: dict(v) if isinstance(v, dict) else v for k, v in session.items()}

    assert record_companion_bases_at_detection(
        session=session, primary_project_dir=tmp_path,
    ) is None
    assert session == before


def test_record_companion_bases_no_companions_returns_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line 378: an empty companion-alias set returns before any capture."""
    import pipeline.engine.companion_scope as companion_scope

    monkeypatch.setattr(
        companion_scope, "derive_companion_aliases", lambda **kw: (),
    )
    captured: list[object] = []
    monkeypatch.setattr(
        companion_scope, "capture_companion_bases",
        lambda **kw: captured.append(kw) or {},
    )

    auto: dict = {"delivery_scope": "strict_mono"}
    record_companion_bases_at_detection(
        session={"auto_detect": auto}, primary_project_dir=tmp_path,
    )

    assert captured == []  # capture never reached
    assert "companion_base_revisions" not in auto


def test_record_companion_bases_swallows_collaborator_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 388-389: a raising collaborator degrades to a silent return."""
    import pipeline.engine.companion_scope as companion_scope

    def _boom(**kwargs: object) -> object:
        raise RuntimeError("derive exploded")

    monkeypatch.setattr(companion_scope, "derive_companion_aliases", _boom)

    auto: dict = {"delivery_scope": "strict_mono"}
    assert record_companion_bases_at_detection(
        session={"auto_detect": auto}, primary_project_dir=tmp_path,
    ) is None
    assert "companion_base_revisions" not in auto


# ── (404) _load_durable_plan ──────────────────────────────────────────────────


def test_load_durable_plan_none_run_dir_is_none() -> None:
    """Line 404: a ``None`` run_dir short-circuits to None."""
    assert _load_durable_plan(None) is None


# ── (422-423) _known_workspace_aliases degradation ────────────────────────────


def test_known_workspace_aliases_swallows_loader_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 422-423: a raising alias loader degrades to an empty tuple."""
    def _boom(*, workspace: object) -> object:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(
        "pipeline.project.project_aliases.load_workspace_project_aliases", _boom,
    )

    assert _known_workspace_aliases(tmp_path, "ws") == ()


# ── (446-456) _recorded_companion_delivery coercion ───────────────────────────


@pytest.mark.parametrize("session", [{}, {"companion_delivery": "not-a-mapping"}])
def test_recorded_companion_delivery_absent_is_empty(session: dict) -> None:
    """Lines 446-448: a missing/non-Mapping block yields {}."""
    assert _recorded_companion_delivery(session) == {}


def test_recorded_companion_delivery_coerces_entries() -> None:
    """Lines 449-456: Mapping/str shas accepted, blanks + bad aliases dropped."""
    session = {
        "companion_delivery": {
            "a": {"commit_sha": "abc123"},   # Mapping with commit_sha
            "b": "def456",                    # bare string sha
            "c": {"commit_sha": "   "},       # whitespace sha → dropped
            "d": "   ",                        # whitespace sha → dropped
            "": "ffff",                       # empty alias → dropped
            5: "eeee",                         # non-string alias → dropped
        },
    }

    assert _recorded_companion_delivery(session) == {"a": "abc123", "b": "def456"}


# ── (474) _persist_companion_bases ────────────────────────────────────────────


def test_persist_companion_bases_non_dict_auto_is_noop() -> None:
    """Line 474: a non-dict ``auto_detect`` block is left untouched."""
    session = {"auto_detect": "not-a-dict"}

    assert _persist_companion_bases(session, {}, {"a": "sha"}) is None
    assert session == {"auto_detect": "not-a-dict"}


def test_persist_companion_bases_merges_new_over_recorded() -> None:
    """Positive contrast: merged bases are written under ``auto_detect``."""
    auto: dict = {}
    session = {"auto_detect": auto}

    _persist_companion_bases(session, {"a": "old"}, {"a": "new", "b": "two"})

    assert auto["companion_base_revisions"] == {"a": "new", "b": "two"}


# ── (482, 485-486) _safe_resolve ──────────────────────────────────────────────


def test_safe_resolve_none_is_none() -> None:
    """Line 482: ``None`` input resolves to None."""
    assert _safe_resolve(None) is None


def test_safe_resolve_swallows_resolve_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 485-486: an OSError from ``Path.resolve`` degrades to None."""
    def _boom(self: Path, *args: object, **kwargs: object) -> Path:
        raise OSError("cannot resolve")

    monkeypatch.setattr(Path, "resolve", _boom)

    assert _safe_resolve(tmp_path / "x") is None


# ── (495-496) _resolve_alias_path degradation ─────────────────────────────────


def test_resolve_alias_path_swallows_resolver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 495-496: a raising ``resolve_project_alias`` degrades to None."""
    def _boom(alias: str, *, workspace: object) -> object:
        raise RuntimeError("alias resolver exploded")

    monkeypatch.setattr(
        "pipeline.project.project_aliases.resolve_project_alias", _boom,
    )

    assert _resolve_alias_path("a", "ws") is None


# ── (515-517, 522-523) _resolve_workspace fallback chain ──────────────────────


def test_resolve_workspace_returns_inferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line 515: a successful inference is returned directly."""
    monkeypatch.setattr(
        "pipeline.project.bootstrap.infer_workspace_from_project",
        lambda _p: "/inferred/ws",
    )

    assert _resolve_workspace("/proj", None) == "/inferred/ws"


def test_resolve_workspace_falls_through_inference_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 516-517: an inference error falls through to the config fallback."""
    def _boom(_p: str) -> object:
        raise RuntimeError("inference exploded")

    monkeypatch.setattr(
        "pipeline.project.bootstrap.infer_workspace_from_project", _boom,
    )
    monkeypatch.setattr(
        "core.infra.config.get_workspace_dir", lambda: "/configured/ws",
    )

    assert _resolve_workspace("/proj", None) == "/configured/ws"


def test_resolve_workspace_returns_none_when_config_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 522-523: a raising ``get_workspace_dir`` degrades to None."""
    monkeypatch.setattr(
        "pipeline.project.bootstrap.infer_workspace_from_project",
        lambda _p: None,
    )

    def _boom() -> object:
        raise RuntimeError("no workspace configured")

    monkeypatch.setattr("core.infra.config.get_workspace_dir", _boom)

    assert _resolve_workspace("/proj", None) is None
