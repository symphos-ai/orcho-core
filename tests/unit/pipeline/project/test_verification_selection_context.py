# SPDX-License-Identifier: Apache-2.0
"""Tests for the single run-scoped scheduled-gate selection context."""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.plugins import PluginConfig
from pipeline.project.verification_selection_context import selection_context_for_run
from pipeline.verification_contract import PlaceholderContext, VerificationContract


def _contract() -> VerificationContract:
    contract = VerificationContract.from_plugin(PluginConfig(verification={
        "commands": {"cli-sdk-unit": {"run": "pytest tests/unit/cli"}},
        "gate_sets": {"cli": {"commands": ["cli-sdk-unit"]}},
        "selection": [{"paths": ["tests/unit/cli/**"], "include": ["cli"]}],
    }))
    assert contract is not None
    return contract


def test_selection_context_reads_run_scoped_placeholder_checkout(monkeypatch) -> None:
    run = SimpleNamespace(state=SimpleNamespace(extras={
        "verification_placeholders": PlaceholderContext(checkout="/run-worktree"),
        "verification_task_kind": "bugfix",
        "verification_operator_sets": ["fast"],
    }))
    seen: list[str] = []
    monkeypatch.setattr(
        "core.io.git_helpers.git_changed_files",
        lambda checkout: seen.append(str(checkout)) or ["tests/unit/cli/test_x.py"],
    )

    context = selection_context_for_run(run, _contract())

    assert seen == ["/run-worktree"]
    assert context.touched_paths == ("tests/unit/cli/test_x.py",)
    assert context.task_kind == "bugfix"
    assert context.operator_sets == ("fast",)


def test_selection_context_falls_back_to_effective_run_worktree(monkeypatch) -> None:
    run = SimpleNamespace(
        state=SimpleNamespace(extras={"verification_placeholders": PlaceholderContext(checkout="")}),
        _effective_diff_cwd=lambda: "/effective-worktree",
    )
    monkeypatch.setattr(
        "core.io.git_helpers.git_changed_files",
        lambda checkout: ["tests/unit/cli/test_y.py"] if checkout == "/effective-worktree" else [],
    )

    assert selection_context_for_run(run, _contract()).touched_paths == (
        "tests/unit/cli/test_y.py",
    )
