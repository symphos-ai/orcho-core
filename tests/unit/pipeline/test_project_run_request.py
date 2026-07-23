"""Unit tests for ``pipeline.project.types`` — ADR 0042 Phase B + ADR 0046 Phase B.

The dataclass mirrors ``run_pipeline`` **modulo
``_REQUEST_ONLY_FIELDS``** (currently ``{"presentation",
"render_phase_outputs", ...}``). These tests
lock the contract so drift in either direction surfaces here, not in
some distant integration test:

* The string form of ``inspect.signature(run_pipeline)`` matches a
  pinned reference — defaults, annotations, parameter ordering, and
  return type are all locked. The wrapper does NOT grow a
  ``presentation`` kwarg.
* The string form of ``inspect.signature(run_project_pipeline)`` also
  has a pin: exactly ``(request: ProjectRunRequest) -> ProjectRunResult``
  — no ``deps``, no ``presentation`` kwarg on the function.
* ``set(ProjectRunRequest fields) - _REQUEST_ONLY_FIELDS ==
  set(run_pipeline parameters)`` — a renamed parameter on either side
  fails fast. New request-only fields join the allowlist with an
  explicit ADR justification, not silent expansion.
* ``ProjectRunRequest.from_kwargs`` rejects unknown kwargs (the
  integration point cannot silently drop a renamed parameter on the
  floor) and accepts request-only fields.
* ``__post_init__`` coerces ``presentation="silent"`` → enum and
  rejects ``SILENT`` + ``no_interactive=False`` with ``ValueError``
  (ADR 0046 hard invariant).
* ``ProjectRunDeps`` is absent after ADR 0042 Phase J — the empty
  placeholder seam was retired when Phase I did not consume it.
"""

from __future__ import annotations

import inspect

import pytest

from pipeline.project.constants import DEFAULT_PROFILE_NAME
from pipeline.project.types import (
    PresentationPolicy,
    ProjectRunRequest,
    ProjectRunResult,
)
from pipeline.project_orchestrator import run_pipeline

# ── request-only field allowlist (ADR 0046 Phase B) ───────────────────────
#
# Fields on ``ProjectRunRequest`` that do NOT exist on the legacy
# ``run_pipeline`` 28-kwarg back-compat surface. The wrapper signature
# is frozen by ADR 0042 Phase J; new behaviour-shaping fields live on
# the typed request only. Each addition to this set MUST cite the ADR
# that justifies it.
#
#   * ``presentation`` — ADR 0046. PresentationPolicy.TERMINAL (default)
#     preserves CLI/SDK back-compat byte-identical; PresentationPolicy.SILENT
#     drives the headless library path.
#   * ``render_phase_outputs`` — follow-up to ADR 0046. Cross terminal
#     dispatch keeps child runs SILENT for banners/finalization while
#     allowing mono-run parity for parsed phase response blocks.
#   * ``preallocated_output_dir`` — ADR 0144. A parent cross coordinator
#     may write a handoff into a fresh child directory without turning that
#     child into a checkpoint resume.
#   * ``auto_waiver_allowed`` — ADR 0073. Operator-set opt-in that lets
#     the implement-phase substance-repair fallback record a synthetic
#     waiver and continue instead of pausing; request-only because the
#     ``run_pipeline`` back-compat surface is frozen.
#   * ``unattended`` — ADR 0120. CLI-only autonomy signal for explicit
#     ``--no-interactive`` runs. Supervisors may still use
#     ``no_interactive=True`` without opting out of pending handoffs.
_REQUEST_ONLY_FIELDS = {
    "presentation", "render_phase_outputs", "auto_waiver_allowed",
    "unattended", "preallocated_output_dir",
}

# ── pinned signature reference ────────────────────────────────────────────
#
# Regeneration recipe — **must be run under pytest, not python -c**, because
# ``model``'s default is ``config.phase_model("implement",
# "claude-opus-4-8[1m]")`` and the config-layer stack resolves to different
# values in shell vs. pytest (workspace overrides apply in shell; the
# fallback ``"claude-opus-4-8[1m]"`` applies in pytest). The pin below is
# the pytest-time value; rolling it would mask that distinction.
#
# To regenerate:
#
#     .venv/bin/python -m pytest tests/unit/pipeline/test_project_run_request.py \
#         -k 'test_run_pipeline_signature_matches_pinned_reference' -vv
#
# read the "ACTUAL" block from the AssertionError, paste it into
# ``PINNED_RUN_PIPELINE_SIGNATURE`` below.
#
# A test failure here means the public ``run_pipeline`` surface drifted.
# Either roll the change back (if accidental) or regenerate this string
# deliberately as part of the same commit that changed the signature.

PINNED_RUN_PIPELINE_SIGNATURE = (
    "(task: str, project_dir: str, max_rounds: int = 1, "
    "model: str = 'claude-opus-4-8[1m]', "
    "output_dir: pathlib.Path | None = None, dry_run: bool = False, "
    "phase_config: agents.registry.PhaseAgentConfig | None = None, "
    "session_mode: agents.protocols.SessionMode = <SessionMode.AUTO: 'auto'>, "
    "profile_name: str = 'feature', "
    "ma_artifacts_dir_override: str | None = None, "
    "provider: 'AgentProvider | None' = None, "
    "resume_from: str | None = None, attachments: tuple = (), "
    "parent_run_id: str | None = None, project_alias: str | None = None, "
    "hypothesis_enabled: bool | None = None, "
    "profile_obj: 'Any | None' = None, plan_source: str = 'local', "
    "handoff_path: str | None = None, resume_mode: str | None = None, "
    "followup_parent_run_id: str | None = None, "
    "followup_parent_run_dir: str | None = None, "
    "followup_parent_status: str | None = None, "
    "followup_base_task: str | None = None, "
    "followup_session_seeds: dict[str, str] | None = None, "
    "followup_child_status: str | None = None, "
    "followup_active_handoff_id: str | None = None, "
    "no_interactive: bool = False, "
    "from_run_plan_parent_dir: 'Path | None' = None, "
    "worktree_config_override: dict[str, typing.Any] | None = None) -> dict"
)


# ── signature lock ─────────────────────────────────────────────────────────


class TestSignatureLock:
    def test_run_pipeline_signature_matches_pinned_reference(self) -> None:
        """Locks defaults + annotations + ordering + return type.

        If config defaults change (e.g. ``config.phase_model``) this
        will trip. That's by design — regenerate the pinned string
        deliberately as part of the change that moved the default.
        """
        actual = str(inspect.signature(run_pipeline))
        assert actual == PINNED_RUN_PIPELINE_SIGNATURE, (
            "\n=== EXPECTED ===\n"
            f"{PINNED_RUN_PIPELINE_SIGNATURE}\n"
            "=== ACTUAL ===\n"
            f"{actual}\n"
            "=== END ===\n"
            "If you intentionally changed run_pipeline, regenerate "
            "PINNED_RUN_PIPELINE_SIGNATURE using the recipe in the "
            "module docstring."
        )


# ── field-name parity (modulo _REQUEST_ONLY_FIELDS) ───────────────────────


class TestRequestFieldParity:
    def test_field_names_equal_run_pipeline_params_modulo_request_only(
        self,
    ) -> None:
        """ADR 0046: the dataclass mirrors ``run_pipeline`` **modulo
        ``_REQUEST_ONLY_FIELDS``**. ``presentation`` and
        ``render_phase_outputs`` are request-only
        (the wrapper's signature is frozen by ADR 0042 Phase J); any
        future request-only field joins ``_REQUEST_ONLY_FIELDS`` with
        an ADR citation, otherwise this test trips.
        """
        from dataclasses import fields

        sig = inspect.signature(run_pipeline)
        run_pipeline_params = set(sig.parameters)
        request_fields = {f.name for f in fields(ProjectRunRequest)}
        request_fields_modulo = request_fields - _REQUEST_ONLY_FIELDS
        assert request_fields_modulo == run_pipeline_params, (
            "field parity drift modulo _REQUEST_ONLY_FIELDS:\n"
            f"  missing in dataclass: {run_pipeline_params - request_fields_modulo}\n"
            f"  extra in dataclass:   {request_fields_modulo - run_pipeline_params}\n"
            f"  _REQUEST_ONLY_FIELDS = {sorted(_REQUEST_ONLY_FIELDS)}"
        )

    def test_field_count_matches_parameter_count_plus_request_only(
        self,
    ) -> None:
        """Belt-and-braces alongside the set equality above."""
        from dataclasses import fields

        sig = inspect.signature(run_pipeline)
        assert (
            len(fields(ProjectRunRequest))
            == len(sig.parameters) + len(_REQUEST_ONLY_FIELDS)
        )

    def test_request_only_fields_are_actually_request_only(self) -> None:
        """Sanity: every name in ``_REQUEST_ONLY_FIELDS`` is on
        ``ProjectRunRequest`` and is NOT on ``run_pipeline``. If a name
        slips out of one side or both, the allowlist is stale and the
        parity guard above would silently widen its tolerance.
        """
        from dataclasses import fields

        sig = inspect.signature(run_pipeline)
        run_pipeline_params = set(sig.parameters)
        request_fields = {f.name for f in fields(ProjectRunRequest)}
        for name in _REQUEST_ONLY_FIELDS:
            assert name in request_fields, (
                f"_REQUEST_ONLY_FIELDS contains {name!r} but no such "
                f"field on ProjectRunRequest"
            )
            assert name not in run_pipeline_params, (
                f"_REQUEST_ONLY_FIELDS contains {name!r} but "
                f"run_pipeline DOES accept it as a kwarg — drop the "
                f"name from the allowlist (it's no longer "
                f"request-only)"
            )


# ── default parity for the live-evaluated defaults ────────────────────────


class TestDefaultParity:
    def test_model_default_matches_orchestrator(self) -> None:
        """``model`` evaluates at import time; both modules must
        resolve to the same string."""
        sig = inspect.signature(run_pipeline)
        assert ProjectRunRequest.__dataclass_fields__["model"].default == (
            sig.parameters["model"].default
        )

    def test_profile_name_default_matches_constants(self) -> None:
        sig = inspect.signature(run_pipeline)
        assert (
            ProjectRunRequest.__dataclass_fields__["profile_name"].default
            == DEFAULT_PROFILE_NAME
            == sig.parameters["profile_name"].default
        )

    def test_session_mode_default_is_auto(self) -> None:
        from agents.protocols import SessionMode

        assert (
            ProjectRunRequest.__dataclass_fields__["session_mode"].default
            is SessionMode.AUTO
        )


# ── from_kwargs helper ────────────────────────────────────────────────────


class TestFromKwargs:
    def test_accepts_full_run_pipeline_kwarg_set(self) -> None:
        """Build a request using every declared kwarg with non-default
        sentinel values where simple (no special construction)."""
        req = ProjectRunRequest.from_kwargs(
            task="t", project_dir="/tmp/x",
        )
        assert req.task == "t"
        assert req.project_dir == "/tmp/x"
        # Default cascade verified by TestDefaultParity above.

    def test_rejects_unknown_kwarg(self) -> None:
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            ProjectRunRequest.from_kwargs(
                task="t",
                project_dir="/tmp/x",
                this_field_does_not_exist=42,
            )

    def test_unknown_message_lists_the_offending_names(self) -> None:
        with pytest.raises(TypeError) as excinfo:
            ProjectRunRequest.from_kwargs(
                task="t", project_dir="/tmp/x",
                bogus_a=1, bogus_b=2,
            )
        msg = str(excinfo.value)
        assert "bogus_a" in msg and "bogus_b" in msg


# ── result surface ────────────────────────────────────────────────────────


class TestResult:
    def test_result_is_constructable_from_minimal_fields(self) -> None:
        result = ProjectRunResult(
            session={"status": "done"},
            output_dir=None,
            run_id="r1",
        )
        assert result.session["status"] == "done"
        assert result.run_id == "r1"

    def test_project_run_deps_removed_in_phase_j(self) -> None:
        """Pin the Phase J cleanup: ``ProjectRunDeps`` was retired per
        ADR 0042 r4 P2 ("empty ceremonial seams must not survive past
        J"). A later ADR re-introduces a typed injection point when
        it has a concrete contract to ship; the import surface stays
        ``ProjectRunRequest`` + ``ProjectRunResult`` until then.
        """
        from pipeline.project import types as _types

        assert not hasattr(_types, "ProjectRunDeps"), (
            "ProjectRunDeps re-appeared on pipeline.project.types — "
            "Phase J removed it per ADR 0042 r4 P2. If this is a "
            "deliberate new injection point, update the ADR + this "
            "test together."
        )


# ── import discipline (ADR 0042 guard) ────────────────────────────────────


class TestImportDiscipline:
    def test_types_module_does_not_import_from_project_orchestrator(
        self,
    ) -> None:
        """``pipeline.project.types`` must not transitively pull in
        ``pipeline.project_orchestrator`` — that would defeat the
        layering rule and create import cycles in later phases.
        """
        import ast
        import pathlib

        types_src = pathlib.Path(
            "pipeline/project/types.py",
        ).read_text(encoding="utf-8")
        tree = ast.parse(types_src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod != "pipeline.project_orchestrator", (
                    f"types.py imports from pipeline.project_orchestrator "
                    f"at line {node.lineno}; ADR 0042 forbids this."
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "pipeline.project_orchestrator", (
                        f"types.py imports pipeline.project_orchestrator "
                        f"at line {node.lineno}; ADR 0042 forbids this."
                    )


# ── PresentationPolicy + __post_init__ (ADR 0046 Phase B) ─────────────────


PINNED_RUN_PROJECT_PIPELINE_SIGNATURE = (
    "(request: pipeline.project.types.ProjectRunRequest) "
    "-> pipeline.project.types.ProjectRunResult"
)


class TestPresentationPolicy:
    """ADR 0046 Phase B pins.

    The policy enum + ``__post_init__`` invariant are the surface a
    library caller has to trust. The runtime checks across the silent
    path (``self.presentation is PresentationPolicy.SILENT``) all
    assume the field holds the enum, not a raw string — coercion in
    ``__post_init__`` is what keeps that assumption true for
    ``from_kwargs`` callers and embedders that wire requests from
    JSON.
    """

    def test_default_is_terminal(self) -> None:
        """Back-compat anchor: every existing caller stays TERMINAL."""
        req = ProjectRunRequest(task="t", project_dir="/tmp/x")
        assert req.presentation is PresentationPolicy.TERMINAL

    def test_silent_without_no_interactive_is_rejected(self) -> None:
        """SILENT + interactive prompt is a contradiction; reject."""
        with pytest.raises(
            ValueError, match="requires no_interactive=True",
        ):
            ProjectRunRequest(
                task="t", project_dir="/tmp/x",
                presentation=PresentationPolicy.SILENT,
            )

    def test_silent_with_no_interactive_succeeds(self) -> None:
        req = ProjectRunRequest(
            task="t", project_dir="/tmp/x",
            presentation=PresentationPolicy.SILENT,
            no_interactive=True,
        )
        assert req.presentation is PresentationPolicy.SILENT

    def test_string_silent_coerces_to_enum(self) -> None:
        """``from_kwargs(presentation="silent")`` is a real call path
        (JSON / argparse Namespace bridges). The coercion makes
        ``is PresentationPolicy.SILENT`` work; without it the runtime
        check would quietly return False and the silent path would
        never fire."""
        req = ProjectRunRequest.from_kwargs(
            task="t", project_dir="/tmp/x",
            presentation="silent",
            no_interactive=True,
        )
        assert req.presentation is PresentationPolicy.SILENT
        assert isinstance(req.presentation, PresentationPolicy)

    def test_string_terminal_coerces_to_enum(self) -> None:
        req = ProjectRunRequest.from_kwargs(
            task="t", project_dir="/tmp/x",
            presentation="terminal",
        )
        assert req.presentation is PresentationPolicy.TERMINAL

    def test_invalid_presentation_string_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid presentation policy"):
            ProjectRunRequest.from_kwargs(
                task="t", project_dir="/tmp/x",
                presentation="loud",
            )

    def test_enum_importable_from_pipeline_project(self) -> None:
        """ADR 0046 § Decision: ``PresentationPolicy`` re-exported at
        package root for ergonomic consumer imports
        (``from pipeline.project import PresentationPolicy``)."""
        from pipeline.project import (
            PresentationPolicy as PolicyFromPackageRoot,
        )

        assert PolicyFromPackageRoot is PresentationPolicy
        assert PolicyFromPackageRoot.SILENT.value == "silent"
        assert PolicyFromPackageRoot.TERMINAL.value == "terminal"

    def test_from_kwargs_accepts_presentation(self) -> None:
        """The parity test allows ``presentation`` in
        ``_REQUEST_ONLY_FIELDS``, but ``from_kwargs`` also has to
        accept it — the kwarg validation walks declared fields, not
        ``run_pipeline`` params. Pin that explicitly.
        """
        req = ProjectRunRequest.from_kwargs(
            task="t", project_dir="/tmp/x",
            presentation=PresentationPolicy.SILENT,
            no_interactive=True,
        )
        assert req.presentation is PresentationPolicy.SILENT

    def test_from_kwargs_accepts_render_phase_outputs(self) -> None:
        req = ProjectRunRequest.from_kwargs(
            task="t",
            project_dir="/tmp/x",
            render_phase_outputs=True,
        )
        assert req.render_phase_outputs is True

    def test_auto_waiver_allowed_default_false(self) -> None:
        """ADR 0073: opt-in defaults off — a plain request never lets the
        implement fallback auto-waive."""
        req = ProjectRunRequest(task="t", project_dir="/tmp/x")
        assert req.auto_waiver_allowed is False

    def test_from_kwargs_accepts_auto_waiver_allowed(self) -> None:
        req = ProjectRunRequest.from_kwargs(
            task="t",
            project_dir="/tmp/x",
            auto_waiver_allowed=True,
        )
        assert req.auto_waiver_allowed is True


# ── run_project_pipeline signature lock (ADR 0046 Phase B) ────────────────


class TestRunProjectPipelineSignatureLock:
    def test_signature_matches_pinned_reference(self) -> None:
        """ADR 0046 stop condition #3 — the typed boundary stays
        ``(request: ProjectRunRequest) -> ProjectRunResult``. No
        ``deps`` parameter (Phase J retired the empty seam), no
        ``presentation`` kwarg (lives on the request only). If a
        future ADR re-introduces ``deps`` with a real contract, this
        test trips deliberately — regenerate the pin in the same
        commit as that ADR.

        Regeneration recipe (pytest, not python -c — same reasoning
        as ``run_pipeline``'s recipe above):

            .venv/bin/python -m pytest \\
                tests/unit/pipeline/test_project_run_request.py \\
                -k 'TestRunProjectPipelineSignatureLock' -vv

        Read the ACTUAL block, paste into
        ``PINNED_RUN_PROJECT_PIPELINE_SIGNATURE``.
        """
        from pipeline.project.app import run_project_pipeline

        actual = str(inspect.signature(run_project_pipeline))
        assert actual == PINNED_RUN_PROJECT_PIPELINE_SIGNATURE, (
            "\n=== EXPECTED ===\n"
            f"{PINNED_RUN_PROJECT_PIPELINE_SIGNATURE}\n"
            "=== ACTUAL ===\n"
            f"{actual}\n"
            "=== END ===\n"
            "If you intentionally changed run_project_pipeline, "
            "regenerate the pin using the recipe in this test's "
            "docstring."
        )
