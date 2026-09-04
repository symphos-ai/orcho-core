"""Resume inheritance for the implement/review/repair round budget.

``pipeline.control.resume_budget`` is the single owner of *how* a
resume recovers the ``max_rounds`` the run was started with. Both
frontends route through it — the ``orcho-run`` CLI and the SDK launcher
— so the precedence rule and the "nothing persisted" set are pinned once
here rather than once per frontend.

The two properties that matter:

* an explicit flag always wins, because re-passing ``--max-rounds`` on a
  resume is how an operator deliberately changes the remaining budget;
* every degenerate persisted value degrades to "nothing to inherit"
  (the caller's own default) rather than raising — the callers are
  launchers on a resume path, where an exception would turn a silently
  shrunk budget into a failed resume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.checkpoint import CheckpointStore
from pipeline.control.resume_budget import (
    persisted_max_rounds,
    resolve_resume_max_rounds,
)

pytestmark = [pytest.mark.project_run]


def _seed(tmp_path: Path, config: dict | None, *, run_id: str = "run") -> Path:
    """Write a run dir, optionally with a checkpoint store holding ``config``."""
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    if config is not None:
        store = CheckpointStore(run_dir / "checkpoints.db", run_id=run_id)
        store.save_config(config)
        store.close()
    return run_dir


# ─────────────────────────────────────────────────────────────────────────────
# persisted_max_rounds — reading the run's own record
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistedMaxRounds:
    def test_reads_the_budget_bootstrap_recorded(self, tmp_path: Path) -> None:
        run_dir = _seed(tmp_path, {"task": "t", "max_rounds": 4})

        assert persisted_max_rounds(run_dir, "run") == 4

    def test_does_not_create_a_checkpoint_store(self, tmp_path: Path) -> None:
        """A run dir with no store must stay without one.

        The probe runs on the resume path of a launcher; fabricating
        ``checkpoints.db`` for a run that never wrote one would invent
        state rather than read it.
        """
        run_dir = _seed(tmp_path, None)

        assert persisted_max_rounds(run_dir, "run") is None
        assert not (run_dir / "checkpoints.db").exists()

    @pytest.mark.parametrize(
        ("config", "reason"),
        [
            pytest.param(None, "no store at all", id="no-store"),
            pytest.param({"task": "t"}, "store predates budget capture", id="no-key"),
            pytest.param({"max_rounds": 0}, "non-positive", id="zero"),
            pytest.param({"max_rounds": -3}, "non-positive", id="negative"),
            pytest.param({"max_rounds": "4"}, "non-integer", id="string"),
            pytest.param({"max_rounds": 2.5}, "non-integer", id="float"),
            pytest.param({"max_rounds": None}, "non-integer", id="null"),
            pytest.param({"max_rounds": True}, "bool is an int subclass", id="bool"),
        ],
    )
    def test_degenerate_values_mean_nothing_to_inherit(
        self, tmp_path: Path, config: dict | None, reason: str,
    ) -> None:
        run_dir = _seed(tmp_path, config)

        assert persisted_max_rounds(run_dir, "run") is None, reason

    def test_unknown_run_id_returns_none(self, tmp_path: Path) -> None:
        run_dir = _seed(tmp_path, {"max_rounds": 4})

        assert persisted_max_rounds(run_dir, "some-other-run") is None


# ─────────────────────────────────────────────────────────────────────────────
# resolve_resume_max_rounds — precedence
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveResumeMaxRounds:
    def test_resume_without_the_flag_inherits_the_persisted_budget(
        self, tmp_path: Path,
    ) -> None:
        """The defect: omitting the flag on a resume shrank the loop to
        the frontend's default and then overwrote the run's record of
        what was originally asked for."""
        run_dir = _seed(tmp_path, {"max_rounds": 4})

        resolved = resolve_resume_max_rounds(
            explicit=None, run_dir=run_dir, run_id="run", default=1,
        )

        assert resolved.value == 4
        assert resolved.inherited is True

    def test_explicit_flag_beats_the_persisted_budget(self, tmp_path: Path) -> None:
        """Re-passing the flag is how an operator changes the remaining
        budget on purpose; the persisted value is a fallback, not an
        override."""
        run_dir = _seed(tmp_path, {"max_rounds": 4})

        resolved = resolve_resume_max_rounds(
            explicit=2, run_dir=run_dir, run_id="run", default=1,
        )

        assert resolved.value == 2
        assert resolved.inherited is False

    def test_explicit_flag_equal_to_the_default_still_beats_it(
        self, tmp_path: Path,
    ) -> None:
        """``--max-rounds 1`` against a persisted 4 must narrow the run.

        This is the case an argparse ``default=1`` could not express:
        "operator asked for one round" and "operator asked for nothing"
        were the same value, which is why the budget was lost.
        """
        run_dir = _seed(tmp_path, {"max_rounds": 4})

        resolved = resolve_resume_max_rounds(
            explicit=1, run_dir=run_dir, run_id="run", default=1,
        )

        assert resolved.value == 1
        assert resolved.inherited is False

    @pytest.mark.parametrize(
        ("config", "id_"),
        [
            pytest.param(None, "no-store", id="no-store"),
            pytest.param({"task": "t"}, "no-key", id="no-key"),
            pytest.param({"max_rounds": 0}, "degenerate", id="degenerate"),
        ],
    )
    def test_nothing_persisted_falls_back_to_the_default(
        self, tmp_path: Path, config: dict | None, id_: str,
    ) -> None:
        """A run with no persisted value behaves exactly as before."""
        run_dir = _seed(tmp_path, config)

        resolved = resolve_resume_max_rounds(
            explicit=None, run_dir=run_dir, run_id="run", default=1,
        )

        assert resolved.value == 1
        assert resolved.inherited is False

    @pytest.mark.parametrize(
        ("run_dir_given", "run_id_given"),
        [
            pytest.param(False, False, id="fresh-run"),
            pytest.param(False, True, id="run-id-without-dir"),
            pytest.param(True, False, id="dir-without-run-id"),
        ],
    )
    def test_no_run_to_inherit_from_falls_back_to_the_default(
        self, tmp_path: Path, run_dir_given: bool, run_id_given: bool,
    ) -> None:
        """FRESH runs and follow-ups (a *new* run) inherit nothing."""
        seeded = _seed(tmp_path, {"max_rounds": 4})

        resolved = resolve_resume_max_rounds(
            explicit=None,
            run_dir=seeded if run_dir_given else None,
            run_id="run" if run_id_given else None,
            default=1,
        )

        assert resolved.value == 1
        assert resolved.inherited is False

    def test_default_is_supplied_by_the_caller(self, tmp_path: Path) -> None:
        """The module does not own the frontend's default, so a caller
        with a different one is not silently overridden."""
        run_dir = _seed(tmp_path, None)

        resolved = resolve_resume_max_rounds(
            explicit=None, run_dir=run_dir, run_id="run", default=3,
        )

        assert resolved.value == 3
