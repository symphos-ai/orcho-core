"""Unit tests for agents/entities.py."""

from __future__ import annotations

import dataclasses

import pytest

from agents.entities import SubTask


class TestSubTask:
    def test_construct_minimal(self) -> None:
        sub = SubTask(id="t1", goal="Do work")
        assert sub.id == "t1"
        assert sub.goal == "Do work"
        assert sub.files == ()
        assert sub.depends_on == ()

    def test_construct_with_dag_fields(self) -> None:
        sub = SubTask(
            id="apply-fix",
            goal="Apply fix",
            spec="Patch the target file",
            files=("src/app.py",),
            skill="python",
            model="claude-sonnet",
            depends_on=("inspect",),
            done_criteria=("Tests pass",),
            owned_files=("src/app.py",),
            architectural_decision=True,
        )

        assert sub.files == ("src/app.py",)
        assert sub.depends_on == ("inspect",)
        assert sub.done_criteria == ("Tests pass",)
        assert sub.owned_files == ("src/app.py",)
        assert sub.architectural_decision is True

    def test_is_frozen(self) -> None:
        sub = SubTask(id="t1", goal="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            sub.goal = "y"  # type: ignore[misc]
