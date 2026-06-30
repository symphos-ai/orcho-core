"""SubTask extensions."""
from agents.entities import SubTask


class TestSubTaskExtensions:
    def test_owned_files_default_empty(self) -> None:
        s = SubTask(id="s1", goal="x")
        assert s.owned_files == ()

    def test_owned_files_glob_patterns(self) -> None:
        s = SubTask(
            id="s1",
            goal="implement controller",
            owned_files=("src/Controller/**", "tests/Controller/**"),
        )
        assert "src/Controller/**" in s.owned_files

    def test_architectural_decision_default_false(self) -> None:
        assert SubTask(id="s1", goal="x").architectural_decision is False

    def test_architectural_decision_flag(self) -> None:
        s = SubTask(
            id="s1",
            goal="introduce CQRS",
            architectural_decision=True,
        )
        assert s.architectural_decision is True

    def test_frozen(self) -> None:
        s = SubTask(id="s1", goal="x")
        import dataclasses

        import pytest
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.id = "x2"  # type: ignore[misc]
