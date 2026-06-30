"""ADR 0047 Phase C — guard the cross-project app-boundary types.

Four contract groups locked here:

1. **`PresentationPolicy` re-export identity** — promoting the enum
   from :mod:`pipeline.project.types` to :mod:`pipeline.presentation`
   must preserve identity: ``pipeline.project.types.PresentationPolicy
   is pipeline.presentation.PresentationPolicy``. All 7 ADR 0046
   importers continue resolving the SAME object.

2. **`run_cross_pipeline` signature lock** — pinned to the
   regenerated-from-pytest reference string (23 parameters). Drift
   in either direction (rename / add / remove / type change / default
   change) trips the lock. Regeneration recipe in the test docstring.

3. **Field parity** — ``fields(CrossRunRequest) - _REQUEST_ONLY_FIELDS
   == set(inspect.signature(run_cross_pipeline).parameters)``. With
   ``_REQUEST_ONLY_FIELDS = {"presentation"}``, the cross request
   carries 24 fields = 23 params + 1 request-only.

4. **`TestCrossPresentationPolicy`** — 8 cases mirroring ADR 0046's
   project Phase B contract: default TERMINAL, SILENT+no_interactive=False
   raises, SILENT+no_interactive=True succeeds, string coercion works
   for both values, invalid string raises, frozen-write-protection
   holds, `from_kwargs` accepts `presentation`, enum importable from
   both home + re-export sites.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from pipeline.cross_project.app_types import CrossRunRequest, CrossRunResult
from pipeline.cross_project.orchestrator import run_cross_pipeline

# ── ADR 0047 D1 — `_REQUEST_ONLY_FIELDS` allowlist ────────────────────
#
# `presentation` is the only field on CrossRunRequest that is NOT on
# the legacy 23-kwarg `run_cross_pipeline` wrapper. Any future addition
# to this set requires a same-commit parity test update + ADR 0047
# stop #9 reference.
_REQUEST_ONLY_FIELDS = {"presentation"}


# ── PINNED_RUN_CROSS_PIPELINE_SIGNATURE ───────────────────────────────
#
# The literal `str(inspect.signature(run_cross_pipeline))`. Regenerate
# canonically by running the failing signature-lock test below: pytest
# captures the test env (notably ``ORCHO_DISABLE_LOCAL_CONFIG=1`` set by
# ``tests/conftest.py``) and prints the ACTUAL block in the assertion
# diff. Paste that diff's ACTUAL string back into this constant. The
# canonical path is pytest, not ``python -c …`` — Phase C's first draft
# pinned ``model: str = 'claude-opus-4-8[1m]'`` from a manual
# ``python -c`` capture under an active local-config override; the
# test re-runs in a different env and the strings diverged. Always
# regenerate from the actual failing test to match the lock's env.
#
# ADR 0047 stop #1: drift here is the load-bearing failure — the typed
# CrossRunRequest field set is keyed against this exact 23-param shape.
PINNED_RUN_CROSS_PIPELINE_SIGNATURE = (
    "(task: str, projects: dict[str, pathlib.Path], max_rounds: int = 1, "
    "model: str = 'claude-opus-4-8[1m]', output_dir: pathlib.Path | None = None, "
    "dry_run: bool = False, mock: bool = False, "
    "provider: 'AgentProvider | None' = None, "
    "phase_config: agents.registry.PhaseAgentConfig | None = None, "
    "cross_mode: str = 'full', plan_file: str | None = None, "
    "resume_from: str | None = None, hypothesis_enabled: bool | None = None, "
    "profile_name: str = 'feature', "
    "operator_decisions: 'tuple | None' = None, "
    "no_interactive: bool = False, resumed_meta: 'dict | None' = None, "
    "resume_mode: str | None = None, "
    "followup_parent_run_id: str | None = None, "
    "followup_parent_run_dir: str | None = None, "
    "followup_parent_status: str | None = None, "
    "followup_base_task: str | None = None, "
    "followup_session_seeds_per_alias: 'dict[str, dict[str, str]] | None' = None) "
    "-> dict"
)


# ── Group 1: PresentationPolicy promotion identity ────────────────────


class TestPresentationPolicyPromotionIdentity:
    """ADR 0047 D1 — `PresentationPolicy` moved from
    `pipeline.project.types` to `pipeline.presentation`. The 7 existing
    importers must continue resolving the SAME enum object."""

    def test_re_export_identity_holds(self) -> None:
        """`pipeline.project.types.PresentationPolicy` is the same
        object as `pipeline.presentation.PresentationPolicy`. Identity
        check — equality is not enough (re-defined enum would
        compare-equal by value but break ``is`` checks across the
        ADR 0046 codebase)."""
        from pipeline.presentation import PresentationPolicy as P_neutral
        from pipeline.project.types import PresentationPolicy as P_reexport

        assert P_reexport is P_neutral, (
            "PresentationPolicy must be the SAME object via both import "
            "paths. ADR 0046 sites (project app, run, bootstrap, handoff, "
            "profile_dispatch, cross project_dispatch, types itself) all "
            "compare with ``is`` — re-defining the enum breaks them."
        )

    def test_re_export_preserves_enum_values(self) -> None:
        """TERMINAL / SILENT values unchanged across the promotion."""
        from pipeline.project.types import PresentationPolicy

        assert PresentationPolicy.TERMINAL.value == "terminal"
        assert PresentationPolicy.SILENT.value == "silent"

    def test_pipeline_project_top_level_re_export_still_works(self) -> None:
        """ADR 0046 Phase B added a top-level package re-export at
        ``pipeline.project.PresentationPolicy``. That convenience
        import must keep resolving to the same object after the
        promotion."""
        from pipeline.presentation import PresentationPolicy as P_neutral
        from pipeline.project import PresentationPolicy as P_pkg

        assert P_pkg is P_neutral


# ── Group 2: run_cross_pipeline signature lock ────────────────────────


class TestRunCrossPipelineSignatureLock:
    def test_signature_matches_pinned_reference(self) -> None:
        """ADR 0047 stop #1 — ``run_cross_pipeline`` stays 23 params,
        byte-identical to the literal pinned in
        :const:`PINNED_RUN_CROSS_PIPELINE_SIGNATURE`. The typed
        ``CrossRunRequest`` is keyed against this exact shape; any
        drift breaks the parity test below.

        Regeneration recipe — pytest is the canonical path (NOT
        ``python -c``; the latter ran without
        ``ORCHO_DISABLE_LOCAL_CONFIG=1`` once during the Phase C draft
        and captured the wrong ``model`` default):

            .venv/bin/python -m pytest \\
                tests/unit/pipeline/cross_project/test_cross_run_request.py \\
                -k 'TestRunCrossPipelineSignatureLock' -vv

        Read the ACTUAL block from the assertion diff, paste into
        ``PINNED_RUN_CROSS_PIPELINE_SIGNATURE`` above. Done in the
        SAME commit as the signature change.
        """
        actual = str(inspect.signature(run_cross_pipeline))
        assert actual == PINNED_RUN_CROSS_PIPELINE_SIGNATURE, (
            "\n=== EXPECTED ===\n"
            f"{PINNED_RUN_CROSS_PIPELINE_SIGNATURE}\n"
            "=== ACTUAL ===\n"
            f"{actual}\n"
            "=== END ===\n"
            "If you intentionally changed run_cross_pipeline, "
            "regenerate the pin using the recipe in this test's "
            "docstring."
        )

    def test_param_count_is_23(self) -> None:
        """Sanity check on the ADR 0047 Phase A.2 finding (the prior
        draft had a phantom 26-param count that the reviewer caught)."""
        params = inspect.signature(run_cross_pipeline).parameters
        assert len(params) == 23, (
            f"run_cross_pipeline should have exactly 23 parameters; "
            f"got {len(params)}: {list(params)}"
        )


# ── Group 3: CrossRunRequest field parity ─────────────────────────────


class TestCrossRequestFieldParity:
    def test_field_names_equal_run_cross_pipeline_params_modulo_request_only(
        self,
    ) -> None:
        """ADR 0047 D8: ``presentation`` is request-only (lives on the
        typed CrossRunRequest because CLI / cross / future direct-library
        UI consumers build requests programmatically; the legacy
        ``run_cross_pipeline`` 23-kwarg wrapper keeps its frozen
        signature). Any future request-only field joins
        ``_REQUEST_ONLY_FIELDS``; everything else must stay 1:1."""
        sig = inspect.signature(run_cross_pipeline)
        run_cross_pipeline_params = set(sig.parameters)
        request_fields = {f.name for f in fields(CrossRunRequest)}
        assert request_fields - _REQUEST_ONLY_FIELDS == run_cross_pipeline_params, (
            f"missing in dataclass: "
            f"{run_cross_pipeline_params - (request_fields - _REQUEST_ONLY_FIELDS)}; "
            f"extra in dataclass: "
            f"{(request_fields - _REQUEST_ONLY_FIELDS) - run_cross_pipeline_params}"
        )

    def test_field_count_matches_parameter_count_plus_request_only(self) -> None:
        """23 params + 1 request-only = 24 fields."""
        sig = inspect.signature(run_cross_pipeline)
        expected = len(sig.parameters) + len(_REQUEST_ONLY_FIELDS)
        actual = len(fields(CrossRunRequest))
        assert actual == expected, (
            f"CrossRunRequest field count drifted: expected {expected} "
            f"({len(sig.parameters)} params + {len(_REQUEST_ONLY_FIELDS)} "
            f"request-only); got {actual}."
        )

    def test_request_only_fields_allowlist_locked(self) -> None:
        """If a new request-only field is added, the allowlist must
        grow in the same commit. ADR 0047 stop #9."""
        assert {"presentation"} == _REQUEST_ONLY_FIELDS, (
            f"_REQUEST_ONLY_FIELDS drifted: {_REQUEST_ONLY_FIELDS}. "
            "Update both the parity tests and ADR 0047 stop #9 in the "
            "same commit when adding a request-only field."
        )


# ── Group 4: CrossRunRequest presentation semantics ───────────────────


class TestCrossPresentationPolicy:
    """Mirrors ADR 0046 ``TestPresentationPolicy`` for the cross side.
    Same 8 cases applied to ``CrossRunRequest``."""

    def test_default_is_terminal(self) -> None:
        from pipeline.presentation import PresentationPolicy

        req = CrossRunRequest(task="t", projects={"a": Path("/tmp")})
        assert req.presentation is PresentationPolicy.TERMINAL

    def test_silent_with_no_interactive_false_raises(self) -> None:
        from pipeline.presentation import PresentationPolicy

        with pytest.raises(ValueError, match="no_interactive=True"):
            CrossRunRequest(
                task="t", projects={"a": Path("/tmp")},
                presentation=PresentationPolicy.SILENT,
                # no_interactive defaults to False
            )

    def test_silent_with_no_interactive_true_succeeds(self) -> None:
        from pipeline.presentation import PresentationPolicy

        req = CrossRunRequest(
            task="t", projects={"a": Path("/tmp")},
            presentation=PresentationPolicy.SILENT,
            no_interactive=True,
        )
        assert req.presentation is PresentationPolicy.SILENT
        assert req.no_interactive is True

    def test_string_silent_coerces_to_enum(self) -> None:
        from pipeline.presentation import PresentationPolicy

        req = CrossRunRequest(
            task="t", projects={"a": Path("/tmp")},
            presentation="silent",
            no_interactive=True,
        )
        # Identity check — coercion must produce the canonical enum
        # object, not a string that compares-equal.
        assert req.presentation is PresentationPolicy.SILENT

    def test_string_terminal_coerces_to_enum(self) -> None:
        from pipeline.presentation import PresentationPolicy

        req = CrossRunRequest(
            task="t", projects={"a": Path("/tmp")},
            presentation="terminal",
        )
        assert req.presentation is PresentationPolicy.TERMINAL

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid presentation policy"):
            CrossRunRequest(
                task="t", projects={"a": Path("/tmp")},
                presentation="loud",
            )

    def test_frozen_write_protection_holds(self) -> None:
        """Frozen dataclass — direct attribute writes raise."""
        req = CrossRunRequest(task="t", projects={"a": Path("/tmp")})
        with pytest.raises(FrozenInstanceError):
            req.presentation = "silent"  # type: ignore[misc]

    def test_from_kwargs_accepts_presentation(self) -> None:
        """The integration helper accepts ``presentation`` even though
        it's not on ``run_cross_pipeline``'s signature. Phase D's
        back-compat wrapper builds the request via this path."""
        from pipeline.presentation import PresentationPolicy

        req = CrossRunRequest.from_kwargs(
            task="t",
            projects={"a": Path("/tmp")},
            presentation=PresentationPolicy.SILENT,
            no_interactive=True,
        )
        assert req.presentation is PresentationPolicy.SILENT

    def test_from_kwargs_rejects_unknown_keyword(self) -> None:
        """Unknown kwargs raise ``TypeError`` so a renamed parameter
        cannot silently drop on the floor."""
        with pytest.raises(TypeError, match="unexpected keyword"):
            CrossRunRequest.from_kwargs(
                task="t",
                projects={"a": Path("/tmp")},
                bogus_param=True,
            )


# ── Group 4.5: run_cross_project_pipeline signature lock (ADR 0047 D) ──


PINNED_RUN_CROSS_PROJECT_PIPELINE_SIGNATURE = (
    "(request: pipeline.cross_project.app_types.CrossRunRequest) "
    "-> pipeline.cross_project.app_types.CrossRunResult"
)


class TestRunCrossProjectPipelineSignatureLock:
    def test_signature_matches_pinned_reference(self) -> None:
        """ADR 0047 stop conditions — the typed cross boundary stays
        ``(request: CrossRunRequest) -> CrossRunResult``. No
        ``presentation`` kwarg on the function itself (it lives on
        the request), no ``deps`` parameter, no widening to accept
        the legacy 23 kwargs. If a future ADR re-introduces a
        runtime injection seam, regenerate the pin in the same
        commit as that ADR.

        Regeneration recipe — pytest (NOT ``python -c``; the latter
        does not source ``ORCHO_DISABLE_LOCAL_CONFIG=1`` from
        ``tests/conftest.py`` and runs in a different env than the
        lock):

            .venv/bin/python -m pytest \\
                tests/unit/pipeline/cross_project/test_cross_run_request.py \\
                -k 'TestRunCrossProjectPipelineSignatureLock' -vv

        Read the ACTUAL block from the assertion diff and paste into
        ``PINNED_RUN_CROSS_PROJECT_PIPELINE_SIGNATURE`` above.
        """
        from pipeline.cross_project.app import run_cross_project_pipeline

        actual = str(inspect.signature(run_cross_project_pipeline))
        assert actual == PINNED_RUN_CROSS_PROJECT_PIPELINE_SIGNATURE, (
            "\n=== EXPECTED ===\n"
            f"{PINNED_RUN_CROSS_PROJECT_PIPELINE_SIGNATURE}\n"
            "=== ACTUAL ===\n"
            f"{actual}\n"
            "=== END ===\n"
            "If you intentionally changed run_cross_project_pipeline, "
            "regenerate the pin using the recipe in this test's "
            "docstring."
        )


# ── Group 5: CrossRunResult shape ─────────────────────────────────────


class TestCrossRunResult:
    def test_carries_session_output_dir_run_id(self) -> None:
        result = CrossRunResult(
            session={"status": "done"},
            output_dir=Path("/tmp/run"),
            run_id="20260526_120000",
        )
        assert result.session == {"status": "done"}
        assert result.output_dir == Path("/tmp/run")
        assert result.run_id == "20260526_120000"

    def test_output_dir_and_run_id_nullable(self) -> None:
        """Edge case — bootstrap may return None for output_dir when
        ``run_cross_pipeline(output_dir=None)`` (legacy SDK path)."""
        result = CrossRunResult(session={}, output_dir=None, run_id=None)
        assert result.output_dir is None
        assert result.run_id is None

    def test_frozen(self) -> None:
        result = CrossRunResult(session={}, output_dir=None, run_id="x")
        with pytest.raises(FrozenInstanceError):
            result.run_id = "y"  # type: ignore[misc]
