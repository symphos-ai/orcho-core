"""Resume / follow-up profile-integrity parity (T1).

Regression coverage for the dogfood bug where an ambient
``ORCHO_PIPELINE`` silently hijacked an inherited durable profile on
resume / follow-up. The invariant under test:

* On a fresh explicit start the ``ORCHO_PIPELINE`` A/B override still
  applies (we did not delete the A/B knob).
* On a resume / follow-up the inherited durable ``meta['profile']`` —
  already resolved by :func:`resolve_resume_profile` — wins; an ambient
  ``ORCHO_PIPELINE`` cannot displace it.
* An explicit ``--profile`` beats both the durable meta and the env.

Each "would-fail-before" case is built from the same two-step chain the
CLI runs: ``resolve_resume_profile`` (durable inheritance) →
``setup_profile`` (run-time projection). Before the fix
``setup_profile`` re-applied ``ORCHO_PIPELINE`` unconditionally, so the
resume / follow-up cases below would have resolved to ``task`` instead
of the inherited ``feature``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.control.resume_context import (
    ResumedMeta,
    ResumeMode,
    build_followup_resume_fields,
    resolve_resume_profile,
)
from pipeline.project import PresentationPolicy
from pipeline.project.constants import DEFAULT_PROFILE_NAME
from pipeline.project.followup_worktree import (
    FollowupPlanContinuationError,
    resolve_followup_plan_promotion,
)
from pipeline.project.profile_setup import setup_profile
from pipeline.project.session_run import _is_fresh_explicit_start
from pipeline.project.types import ProjectRunRequest


def _setup(profile_name: str, *, allow_env_override: bool):
    """Run ``setup_profile`` with the side-effect-free SILENT presentation."""
    return setup_profile(
        profile_name=profile_name,
        profile_obj=None,
        from_run_plan_parent_dir=None,
        plan_source="local",
        handoff_path=None,
        max_rounds=1,
        presentation=PresentationPolicy.SILENT,
        allow_env_override=allow_env_override,
    )


class TestFreshStartIntentClassification:
    """``_is_fresh_explicit_start`` is the single gate that decides whether
    the env A/B override is honoured. It must be True only for a brand-new
    top-level run."""

    def test_plain_fresh_run_is_fresh(self) -> None:
        req = ProjectRunRequest(task="t", project_dir="/p")
        assert _is_fresh_explicit_start(req) is True

    def test_checkpoint_resume_is_not_fresh(self) -> None:
        req = ProjectRunRequest(
            task="t", project_dir="/p", resume_from="20260101_000000_aaaaaa",
        )
        assert _is_fresh_explicit_start(req) is False

    def test_followup_mode_is_not_fresh(self) -> None:
        req = ProjectRunRequest(
            task="t", project_dir="/p", resume_mode="followup",
        )
        assert _is_fresh_explicit_start(req) is False

    def test_followup_parent_is_not_fresh(self) -> None:
        req = ProjectRunRequest(
            task="t", project_dir="/p",
            followup_parent_run_id="20260101_000000_aaaaaa",
        )
        assert _is_fresh_explicit_start(req) is False

    def test_lineage_child_is_not_fresh(self) -> None:
        req = ProjectRunRequest(
            task="t", project_dir="/p",
            parent_run_id="20260101_000000_aaaaaa",
        )
        assert _is_fresh_explicit_start(req) is False


class TestResumeProfileSurvivesAmbientEnv:
    """feature → resume → feature, even with ``ORCHO_PIPELINE=task`` set and
    no ``--profile``. Would have resolved to ``task`` before the fix."""

    def test_resume_inherits_feature_over_ambient_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_PIPELINE", "task")
        # Step 1 (CLI): durable inheritance resolves to feature.
        resumed = ResumedMeta(
            path=Path("/runs/r/meta.json"),
            meta={"profile": "feature"},
        )
        inherited = resolve_resume_profile(
            explicit_profile=None,
            resumed=resumed,
            fresh_default=DEFAULT_PROFILE_NAME,
        )
        assert inherited == "feature"
        # Step 2 (run): a resume request is not a fresh start, so the env
        # override is gated off and the inherited profile survives.
        req = ProjectRunRequest(
            task="t", project_dir="/p",
            profile_name=inherited,
            resume_from="20260101_000000_aaaaaa",
        )
        result = _setup(req.profile_name, allow_env_override=_is_fresh_explicit_start(req))
        assert result.resolved_profile_name == "feature"
        assert result.v2_profile.name == "feature"
        assert result.env_profile_override_applied is False

    def test_explicit_profile_beats_meta_and_env_on_resume(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_PIPELINE", "task")
        resumed = ResumedMeta(
            path=Path("/runs/r/meta.json"),
            meta={"profile": "feature"},
        )
        # Explicit --profile small_task wins over both durable meta and env.
        inherited = resolve_resume_profile(
            explicit_profile="small_task",
            resumed=resumed,
            fresh_default=DEFAULT_PROFILE_NAME,
        )
        assert inherited == "small_task"
        req = ProjectRunRequest(
            task="t", project_dir="/p",
            profile_name=inherited,
            resume_from="20260101_000000_aaaaaa",
        )
        result = _setup(req.profile_name, allow_env_override=_is_fresh_explicit_start(req))
        assert result.resolved_profile_name == "small_task"
        assert result.env_profile_override_applied is False


class TestFollowupProfileInheritance:
    """feature → follow-up → feature. The follow-up inherits the parent's
    durable profile via ``resolve_resume_profile``; ``setup_profile`` then
    keeps it despite an ambient ``ORCHO_PIPELINE``."""

    def test_followup_inherits_feature_over_ambient_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_PIPELINE", "task")
        parent = ResumedMeta(
            path=Path("/runs/parent/meta.json"),
            meta={
                "profile": "feature",
                "status": "done",
                "task": "parent task",
            },
        )
        # Follow-up profile inheritance flows through resolve_resume_profile.
        inherited = resolve_resume_profile(
            explicit_profile=None,
            resumed=parent,
            fresh_default=DEFAULT_PROFILE_NAME,
        )
        assert inherited == "feature"
        # build_followup_resume_fields carries the parent context forward
        # (it intentionally does not carry profile — that is the resolver's
        # job — but it must not lose the parent lineage that lets the child
        # run as a follow-up at all).
        fields = build_followup_resume_fields(
            resume_mode=ResumeMode.FOLLOWUP,
            resume_run_id="20260101_000000_parent",
            resumed=parent,
        )
        assert fields.base_task == "parent task"
        assert fields.parent_run_id == "20260101_000000_parent"
        # The follow-up child run resolves its profile with the env gated off.
        req = ProjectRunRequest(
            task="follow-up task", project_dir="/p",
            profile_name=inherited,
            resume_mode="followup",
            followup_parent_run_id="20260101_000000_parent",
        )
        result = _setup(req.profile_name, allow_env_override=_is_fresh_explicit_start(req))
        assert result.resolved_profile_name == "feature"
        assert result.v2_profile.name == "feature"
        assert result.env_profile_override_applied is False


class TestFreshStartAbEnvPreserved:
    """The fix narrows the env override — it does not delete it. A genuine
    fresh explicit start still honours ``ORCHO_PIPELINE`` as an A/B knob."""

    def test_fresh_run_still_honors_ab_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_PIPELINE", "task")
        req = ProjectRunRequest(task="t", project_dir="/p", profile_name="feature")
        assert _is_fresh_explicit_start(req) is True
        result = _setup(req.profile_name, allow_env_override=_is_fresh_explicit_start(req))
        assert result.resolved_profile_name == "task"
        assert result.v2_profile.name == "task"
        assert result.env_profile_override_applied is True

    def test_env_equal_to_requested_is_noop_provenance(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An env value equal to the requested profile is not a real
        override; provenance must report it as not-applied so dashboards /
        assertions do not see a phantom A/B switch."""
        monkeypatch.setenv("ORCHO_PIPELINE", "feature")
        result = _setup("feature", allow_env_override=True)
        assert result.resolved_profile_name == "feature"
        assert result.env_profile_override_applied is False


class TestLegacyAdvancedIsDead:
    """T2 guard-lock: the legacy ``advanced`` profile name is dead and can
    never revive as a default, alias, or resume/follow-up fallback.

    ``advanced`` was the pre-``feature`` recipe name; ``feature`` absorbed its
    recipe (see ``pipeline/project/constants.py`` history comments — those are
    historical documentation, not live values). These tests pin that the name
    resolves nowhere: it is absent from the catalogue, dies loudly on an
    explicit start, and is never produced by the resume/follow-up resolver.
    """

    @staticmethod
    def _v2_catalogue() -> dict:
        from core.infra.paths import CONFIG_DIR
        from pipeline.profiles.loader import load_profiles_v2_with_plugins

        return load_profiles_v2_with_plugins(
            CONFIG_DIR / "pipeline_profiles_v2.json",
        )

    def test_v2_catalogue_has_no_advanced(self) -> None:
        catalogue = self._v2_catalogue()
        assert "advanced" not in catalogue
        # Sanity: the catalogue did load real profiles (so the absence above
        # is a true negative, not an empty/broken load).
        assert "feature" in catalogue

    def test_start_with_advanced_raises_valueerror_listing_names(self) -> None:
        with pytest.raises(ValueError, match=r"Unknown pipeline profile 'advanced'") as exc:
            _setup("advanced", allow_env_override=False)
        message = str(exc.value)
        assert "Available profiles:" in message
        # The available list is the real catalogue, not a hardcoded stub.
        for name in sorted(self._v2_catalogue()):
            assert name in message
        # And it must never offer the dead name back as a suggestion.
        assert "advanced" not in message.split("Available profiles:", 1)[1]

    def test_fresh_explicit_start_with_env_advanced_dies_loudly(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even the A/B env knob cannot revive ``advanced``: a fresh start with
        ``ORCHO_PIPELINE=advanced`` resolves the name then fails loudly — it
        never silently falls back to a live profile."""
        monkeypatch.setenv("ORCHO_PIPELINE", "advanced")
        with pytest.raises(ValueError, match=r"Unknown pipeline profile 'advanced'"):
            _setup("feature", allow_env_override=True)

    def test_no_alias_table_rewrites_advanced_to_a_live_profile(self) -> None:
        """There is no legacy→v2 alias map silently rewriting ``advanced``: the
        name resolver passes it through verbatim (so it then fails to resolve),
        rather than aliasing it onto a live profile like ``feature``."""
        from pipeline.project.profile_setup import _resolve_profile_name

        assert _resolve_profile_name("advanced", allow_env_override=False) == "advanced"

    def test_resume_inherit_never_yields_advanced_under_ambient_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_PIPELINE", "advanced")
        resumed = ResumedMeta(
            path=Path("/runs/r/meta.json"), meta={"profile": "feature"},
        )
        result = resolve_resume_profile(
            explicit_profile=None,
            resumed=resumed,
            fresh_default=DEFAULT_PROFILE_NAME,
        )
        assert result == "feature"
        assert result != "advanced"

    def test_fresh_default_is_never_advanced_under_ambient_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_PIPELINE", "advanced")
        result = resolve_resume_profile(
            explicit_profile=None, resumed=None, fresh_default=DEFAULT_PROFILE_NAME,
        )
        assert result == DEFAULT_PROFILE_NAME
        assert result != "advanced"

    def test_followup_inheritance_never_yields_advanced(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ORCHO_PIPELINE", "advanced")
        parent = ResumedMeta(
            path=Path("/runs/parent/meta.json"),
            meta={"profile": "feature", "status": "done", "task": "t"},
        )
        inherited = resolve_resume_profile(
            explicit_profile=None, resumed=parent, fresh_default=DEFAULT_PROFILE_NAME,
        )
        fields = build_followup_resume_fields(
            resume_mode=ResumeMode.FOLLOWUP,
            resume_run_id="20260101_000000_parent",
            resumed=parent,
        )
        assert inherited == "feature"
        assert inherited != "advanced"
        # The follow-up field carrier never smuggles an 'advanced' profile in.
        assert "advanced" not in (fields.base_task or "")


def _plan_only_parent_dir(tmp_path: Path) -> Path:
    """A parent run dir that is a pure plan-only continuation candidate.

    Holds a non-empty ``parsed_plan.json`` (the durable-plan precondition) and
    no ``diff.patch`` / worktree meta, so ``resolve_followup_plan_promotion``
    classifies it as ``diff_source='plan_artifact'`` and reaches the child-
    profile contradiction check (the line the env override could hijack).
    """
    parent_dir = tmp_path / "parent_run"
    parent_dir.mkdir()
    (parent_dir / "parsed_plan.json").write_text(
        '{"steps": [{"title": "do"}]}', encoding="utf-8",
    )
    return parent_dir


class TestFollowupPromotionSurvivesAmbientEnv:
    """The plan-only follow-up promotion chokepoint runs *before* profile
    setup (at request assembly). It must inherit the parent's durable profile
    and ignore an ambient ``ORCHO_PIPELINE`` — otherwise a stale A/B env value
    re-targets the promotion to a plan-only / review-only profile and the run
    is wrongly blocked as a false continuation.

    Before the fix ``resolve_followup_plan_promotion`` resolved the child
    profile name *with* env override, so ``ORCHO_PIPELINE='planning'`` turned
    an inherited ``feature`` follow-up into a contradictory ``planning`` one and
    raised :class:`FollowupPlanContinuationError`.
    """

    @pytest.mark.parametrize("ambient_env", ["planning", "task", "advanced"])
    def test_inherited_feature_promotes_despite_ambient_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambient_env: str,
    ) -> None:
        monkeypatch.setenv("ORCHO_PIPELINE", ambient_env)
        parent_dir = _plan_only_parent_dir(tmp_path)
        # Inherited durable profile is 'feature' (has implement/review phases),
        # so promotion must succeed regardless of the ambient env value.
        promoted = resolve_followup_plan_promotion(
            resume_mode="followup",
            explicit_from_run_plan_parent_dir=None,
            followup_parent_run_dir=parent_dir,
            profile_name="feature",
            profile_obj=None,
            project_dir=None,
        )
        assert promoted == parent_dir

    def test_contradictory_inherited_profile_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guard itself is intact: a genuinely contradictory *inherited*
        profile (plan-only) still raises — the fix narrows the env override, it
        does not disable the contradiction check."""
        monkeypatch.delenv("ORCHO_PIPELINE", raising=False)
        parent_dir = _plan_only_parent_dir(tmp_path)
        with pytest.raises(FollowupPlanContinuationError):
            resolve_followup_plan_promotion(
                resume_mode="followup",
                explicit_from_run_plan_parent_dir=None,
                followup_parent_run_dir=parent_dir,
                profile_name="planning",
                profile_obj=None,
                project_dir=None,
            )
