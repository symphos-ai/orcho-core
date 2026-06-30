# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the typed participant substrate (ADR 0112 §1, increment B).

Covers the mono seed from resolved paths, the derived ``IsolatedSource`` form
(``None`` for single-checkout, the isolated form otherwise — the derivation the
production resolver now reads through the set), provisional cross participants and
post-dispatch ``editable_checkout`` binding, lookup by identity, and the degraded
isolation-off path.
"""
from __future__ import annotations

import os

import pytest

from pipeline.engine.worktree_source import IsolatedSource
from pipeline.participants import (
    PRIMARY_ALIAS,
    Participant,
    ParticipantSet,
)


def test_module_does_not_import_cross_project() -> None:
    """The substrate must be mono-importable: no cross_project in its source."""
    from pathlib import Path

    from pipeline import participants as mod

    text = Path(mod.__file__).read_text(encoding="utf-8")
    assert "cross_project" not in text


def test_for_mono_builds_single_participant_from_paths(tmp_path) -> None:
    project = str(tmp_path / "proj")
    checkout = str(tmp_path / "wt")
    pset = ParticipantSet.for_mono(
        checkout=checkout,
        project=project,
        base_ref="main",
        delivery_target=project,
    )
    assert len(pset) == 1
    participant = next(iter(pset))
    assert isinstance(participant, Participant)
    assert participant.alias == PRIMARY_ALIAS
    assert participant.repo == project
    assert participant.editable_checkout == checkout
    assert participant.base_ref == "main"
    assert participant.delivery_target == project
    assert participant.is_bound


def test_isolated_source_single_checkout_is_none(tmp_path) -> None:
    """checkout == project → derived source is None (byte-identical single-checkout)."""
    project = str(tmp_path / "proj")
    pset = ParticipantSet.for_mono(checkout=project, project=project)
    participant = next(iter(pset))
    assert pset.isolated_source_for(participant) is None


def test_isolated_source_isolated_form(tmp_path) -> None:
    """checkout != project → derived source is the per_run isolated form the
    production resolver reads through the set."""
    project = str(tmp_path / "proj")
    checkout = str(tmp_path / "wt")
    pset = ParticipantSet.for_mono(checkout=checkout, project=project)
    participant = next(iter(pset))
    derived = pset.isolated_source_for(participant)
    assert derived == IsolatedSource(
        isolation="per_run",
        worktree_path=checkout,
        source_repo_path=project,
    )
    assert derived is not None and derived.is_isolated


def test_for_mono_honours_worktree_meta_block(tmp_path) -> None:
    """An explicit session['worktree'] block takes precedence over the path gap."""
    project = str(tmp_path / "proj")
    checkout = str(tmp_path / "checkout")
    wt = str(tmp_path / "meta-wt")
    pset = ParticipantSet.for_mono(
        checkout=checkout,
        project=project,
        worktree={
            "isolation": "per_run",
            "path": wt,
            "source_repo_path": project,
        },
    )
    participant = next(iter(pset))
    assert participant.editable_checkout == wt
    assert participant.delivery_target == project
    src = pset.isolated_source_for(participant)
    assert src == IsolatedSource(
        isolation="per_run",
        worktree_path=wt,
        source_repo_path=project,
    )


def test_provisional_then_bind_editable_checkout(tmp_path) -> None:
    """A provisional cross participant has no checkout until bound post-dispatch."""
    canonical = str(tmp_path / "child-src")
    child_wt = str(tmp_path / "runspace" / "child-wt")
    pset = ParticipantSet(isolation="per_run")
    provisional = pset.add_provisional(
        alias="child",
        repo=canonical,
        base_ref="main",
        delivery_target=canonical,
    )
    assert provisional.editable_checkout == ""
    assert not provisional.is_bound

    # Provisional + declared regime → declared but not bound: the resolver must
    # fail closed, not fall back to the sibling.
    src = pset.isolated_source_for(provisional)
    assert src is not None and src.is_declared and not src.is_isolated

    bound = pset.bind_editable_checkout("child", child_wt)
    assert bound.editable_checkout == child_wt
    assert bound.is_bound
    # The set now reflects the real isolated child path, not the canonical sibling.
    assert pset.get("child").editable_checkout == child_wt
    bound_src = pset.isolated_source_for("child")
    assert bound_src is not None and bound_src.is_isolated
    assert bound_src.worktree_path == child_wt
    assert bound_src.source_repo_path == canonical


def test_bind_unknown_participant_raises() -> None:
    pset = ParticipantSet(isolation="per_run")
    with pytest.raises(KeyError):
        pset.bind_editable_checkout("nope", "/tmp/x")


def test_lookup_by_identity(tmp_path) -> None:
    """Lookup resolves by alias, repo path, editable checkout, and delivery target."""
    project = str(tmp_path / "proj")
    checkout = str(tmp_path / "wt")
    pset = ParticipantSet.for_mono(
        checkout=checkout,
        project=project,
        delivery_target=project,
    )
    participant = next(iter(pset))
    assert pset.get(PRIMARY_ALIAS) is participant
    assert pset.get(project) is participant
    assert pset.get(checkout) is participant
    # Realpath normalisation: a non-normalised path still resolves.
    assert pset.get(os.path.join(project, "..", os.path.basename(project))) is participant
    assert pset.get("/no/such/path") is None


def test_degraded_isolation_off(tmp_path) -> None:
    """isolation-off → editable_checkout == delivery_target and no isolated source."""
    project = str(tmp_path / "proj")
    pset = ParticipantSet.for_mono(
        checkout=project,
        project=project,
        delivery_target=project,
    )
    participant = next(iter(pset))
    assert pset.isolation == "off"
    assert participant.editable_checkout == participant.delivery_target
    assert pset.isolated_source_for(participant) is None
