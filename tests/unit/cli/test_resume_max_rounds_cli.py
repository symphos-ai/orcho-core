"""``orcho run --resume`` keeps the round budget the run was started with.

The defect this pins: ``--max-rounds`` carried an argparse ``default=1``,
so ``main()`` could not tell "the operator did not pass the flag" from
"the operator asked for one round". A resume that omitted the flag
therefore fed ``max_rounds=1`` into the run config, silently shrinking a
multi-round implement/review/repair loop — and ``bootstrap`` then wrote
that shrunken value back over the run's own
``checkpoints.db:run_meta.config_json``, destroying the record of what
was originally requested.

The resolution rule (explicit flag → persisted budget → 1) lives in
``pipeline.control.resume_budget`` and is unit-tested in
``tests/unit/pipeline/control/test_resume_budget.py``. This module pins
the CLI wiring that a resolver-only test cannot see: argparse must hand
the resolver ``None`` for an absent flag, the persisted store must be
read from the *resumed* run's dir, and the resolved value must be what
reaches ``run_pipeline``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from pipeline.checkpoint import CheckpointStore

pytestmark = [pytest.mark.project_run]

_RUN_ID = "20260101_000000"


class _CapturingRunPipeline:
    """Stand-in for ``run_pipeline`` that records the kwargs it was given."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {"status": "done"}


class TestResumeMaxRounds:
    @pytest.fixture(autouse=True)
    def _isolated_workspace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ):
        """Pin the workspace env and a valid project dir.

        ``main()`` resolves a workspace and the resumed run dir before it
        reaches ``run_pipeline``; without isolation it would leak host
        state or fail on a missing project.
        """
        runspace = tmp_path / "runspace"
        self._runs = runspace / "runs"
        self._runs.mkdir(parents=True)
        monkeypatch.setenv("ORCHO_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ORCHO_RUNSPACE", str(runspace))
        from core.infra import config as _config
        _config._reset_config()

        project = tmp_path / "proj"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname='p'\n")
        self._project = project
        yield
        shutil.rmtree(runspace, ignore_errors=True)
        _config._reset_config()

    def _seed_resumable_run(self, config: dict | None) -> Path:
        """Write the on-disk shape a bare ``--resume`` accepts.

        ``status`` is a non-terminal pause so the run classifies as a
        CHECKPOINT continuation, and ``meta.json`` carries no active
        ``phase_handoff`` so the resume preflight is a no-op.
        """
        run_dir = self._runs / _RUN_ID
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps({
                "status": "interrupted",
                "task": "demo",
                "project": str(self._project),
                "profile": "feature",
            }),
            encoding="utf-8",
        )
        if config is not None:
            store = CheckpointStore(run_dir / "checkpoints.db", run_id=_RUN_ID)
            store.save_config(config)
            store.close()
        return run_dir

    def _resume(
        self, monkeypatch: pytest.MonkeyPatch, extra_argv: list[str] | None = None,
    ) -> _CapturingRunPipeline:
        """Drive ``main()`` through a bare checkpoint resume."""
        from pipeline.project import cli

        fake = _CapturingRunPipeline()
        monkeypatch.setattr("pipeline.project.cli.run_pipeline", fake)

        argv = ["--resume", _RUN_ID, "--no-interactive", "--mock", *(extra_argv or ())]
        saved_argv = sys.argv
        sys.argv = ["orchestrator", *argv]
        try:
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
                assert code in (0, None), f"resume exited {code!r}"
        finally:
            sys.argv = saved_argv
        assert fake.kwargs, "run_pipeline was never reached"
        return fake

    # ── (a) resume without the flag inherits the persisted budget ─────────

    def test_resume_without_the_flag_inherits_the_persisted_budget(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_resumable_run({"task": "demo", "max_rounds": 4})

        fake = self._resume(monkeypatch)

        assert fake.kwargs["max_rounds"] == 4, (
            "resume dropped the operator's budget back to the argparse "
            "default; the repair loop would silently run one round"
        )

    def test_inheritance_is_announced_to_the_operator(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A budget that changes without being asked for is what made
        this defect invisible; the inherited one is stated out loud."""
        self._seed_resumable_run({"task": "demo", "max_rounds": 4})

        self._resume(monkeypatch)

        assert "--max-rounds 4" in capsys.readouterr().out

    # ── (b) an explicit flag on the resume wins ───────────────────────────

    def test_explicit_flag_overrides_the_persisted_budget(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_resumable_run({"task": "demo", "max_rounds": 4})

        fake = self._resume(monkeypatch, ["--max-rounds", "2"])

        assert fake.kwargs["max_rounds"] == 2

    def test_explicit_one_round_is_not_mistaken_for_an_absent_flag(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--max-rounds 1`` against a persisted 4 must narrow the run.

        The old ``default=1`` made these two inputs indistinguishable,
        so honouring the persisted value would have been indistinguishable
        from ignoring the operator.
        """
        self._seed_resumable_run({"task": "demo", "max_rounds": 4})

        fake = self._resume(monkeypatch, ["--max-rounds", "1"])

        assert fake.kwargs["max_rounds"] == 1

    # ── (c) nothing persisted → previous behaviour ────────────────────────

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(None, id="no-checkpoint-store"),
            pytest.param({"task": "demo"}, id="config-without-max-rounds"),
            pytest.param({"task": "demo", "max_rounds": 0}, id="degenerate-value"),
        ],
    )
    def test_no_persisted_budget_keeps_the_previous_default(
        self, monkeypatch: pytest.MonkeyPatch, config: dict | None,
    ) -> None:
        """Runs recorded before budget capture, or with a corrupt value,
        resume exactly as they did before: the fix narrows a silent loss,
        it does not invent a budget."""
        self._seed_resumable_run(config)

        fake = self._resume(monkeypatch)

        assert fake.kwargs["max_rounds"] == 1

    def test_fresh_run_still_defaults_to_one_round(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The default is unchanged for a run that is not a resume."""
        from pipeline.project import cli

        fake = _CapturingRunPipeline()
        monkeypatch.setattr("pipeline.project.cli.run_pipeline", fake)

        saved_argv = sys.argv
        sys.argv = [
            "orchestrator", "--task", "demo", "--project", str(self._project),
            "--mock", "--no-interactive",
        ]
        try:
            try:
                cli.main()
            except SystemExit as exc:
                assert exc.code in (0, None)
        finally:
            sys.argv = saved_argv

        assert fake.kwargs["max_rounds"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# ``orcho run`` → orchestrator argv
# ─────────────────────────────────────────────────────────────────────────────


class TestOrchoRunArgvForwarding:
    """``orcho run`` is the surface an operator actually types.

    It re-parses its own ``--max-rounds`` and forwards the namespace as
    orchestrator argv, so its default is the second half of the same
    defect: a ``default=1`` there re-materialises the flag on every
    resume, and the orchestrator can then never tell that the operator
    left it off.
    """

    def _argv(self, argv: list[str]) -> list[str]:
        from cli.orcho import build_parser
        from sdk.runner import build_orch_argv_from_args

        args = build_parser().parse_args(argv)
        return build_orch_argv_from_args(args)

    def test_absent_flag_is_not_re_materialised_on_the_wire(self) -> None:
        argv = self._argv(["run", "--resume", _RUN_ID, "--profile", "feature"])

        assert "--max-rounds" not in argv, (
            "orcho run re-emitted a budget the operator never passed, so the "
            f"orchestrator cannot inherit the run's own. argv={argv}"
        )

    def test_explicit_flag_is_forwarded(self) -> None:
        argv = self._argv([
            "run", "--resume", _RUN_ID, "--profile", "feature", "--max-rounds", "4",
        ])

        assert argv[argv.index("--max-rounds") + 1] == "4"
