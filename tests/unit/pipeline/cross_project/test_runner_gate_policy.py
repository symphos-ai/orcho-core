"""Runner-side wiring of ``cross_gates`` policy.

Exercises the path where ``run_cross_pipeline`` consults
``get_cross_gate_policy`` + ``resolve_gate_decision`` to short-circuit
the Codex reviewer loop. Driven through the mock provider so no real
agent invocations occur; we assert on the resulting per-alias entries
and the session status / pending_gate fields.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agents.runtimes import MockAgentProvider
from pipeline.runtime import (
    ContractCheckMode,
    CrossGatePolicy,
    CrossGateRunPolicy,
    CrossGateSkipPolicy,
)

# Reuse the acceptance-suite helpers so the test set up matches the
# mock-flow conventions and we don't reinvent fixtures.
from tests.acceptance.test_full_mock_flow import (  # noqa: E402
    PLUGIN,
    _patches,
)


def _make_projects(tmp_path: Path) -> dict[str, Path]:
    out = {}
    for name in ("unity", "api"):
        p = tmp_path / name
        p.mkdir()
        out[name] = p
    return out


def _profile_with_cross_gates(profile, **policies) -> object:
    """Return a copy of ``profile`` with ``cross_gates`` overrides
    MERGED into the existing block.

    ``policies`` maps gate name → CrossGatePolicy. Keys not present in
    ``policies`` keep whatever the source profile already declared
    (advanced ships both gates strictly). This is load-bearing under
    the missing≡off semantic: replacing the whole block would drop
    the un-overridden gate to ``enabled=False`` and silently change
    which gates the test runs.
    """
    from dataclasses import replace

    merged = dict(profile.cross_gates)
    merged.update(policies)
    return replace(profile, cross_gates=merged)


def _patched_load_profiles(profile_returner):
    """Return a context manager that patches
    ``load_profiles_v2_with_plugins`` to inject the test profile."""
    return patch(
        "pipeline.profiles.loader.load_profiles_v2_with_plugins",
        side_effect=profile_returner,
    )


def _load_real_advanced_with_overrides(**policies) -> dict:
    """Load the shipped advanced profile and return a `{name: Profile}`
    map with an extra ``cross_gates`` block on it.

    Force-enable ``contract_check`` and ``cross_final_acceptance``
    as a baseline so behavioral assertions stay independent of the
    current ``_config/pipeline_profiles_v2.json`` enable flags. The
    baseline only flips ``enabled`` — mode/run/on_skip from the
    shipped policy are preserved (e.g. ``contract_check`` ships with
    ``mode="artifact_bundle"`` and the artifact-bundle invocation
    test depends on that mode survival). Per-test overrides in
    ``policies`` win over this baseline.
    """
    import dataclasses

    from core.infra.paths import CONFIG_DIR
    from pipeline.profiles.loader import load_profiles_v2
    from pipeline.runtime.profile import CrossGatePolicy

    profiles = load_profiles_v2(CONFIG_DIR / "pipeline_profiles_v2.json")
    advanced = profiles["feature"]
    baseline: dict[str, CrossGatePolicy] = {}
    for gate_name in ("contract_check", "cross_final_acceptance"):
        existing = advanced.cross_gates.get(gate_name)
        if existing is None:
            baseline[gate_name] = CrossGatePolicy(enabled=True)
        elif not existing.enabled:
            baseline[gate_name] = dataclasses.replace(existing, enabled=True)
    # Per-test overrides shadow the baseline; merge order matters.
    merged_policies = {**baseline, **policies}
    profiles["feature"] = _profile_with_cross_gates(
        advanced, **merged_policies,
    )
    return profiles


def _cross_run(*, tmp_path: Path, profile_map, projects, **kwargs):
    from pipeline.cross_project.orchestrator import run_cross_pipeline

    lp, hu, gd = _patches()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    provider = MockAgentProvider()

    with (
        lp, hu, gd,
        patch(
            "pipeline.cross_project.run_setup.load_plugin",
            return_value=PLUGIN,
        ),
        patch(
            "pipeline.profiles.loader.load_profiles_v2_with_plugins",
            return_value=profile_map,
        ),
    ):
        return run_cross_pipeline(
            task="Test gate policy",
            projects=projects,
            output_dir=run_dir,
            provider=provider,
            **kwargs,
        )


class TestContractCheckDisabledByPolicy:
    """``contract_check.enabled=false`` → per-alias skipped entries with
    source ``policy`` / reason ``policy_disabled``; no Codex call;
    cross_final_acceptance still runs and observes the skipped state."""

    def test_no_codex_calls(self, tmp_path: Path) -> None:
        profile_map = _load_real_advanced_with_overrides(
            contract_check=CrossGatePolicy(
                enabled=False,
                run=CrossGateRunPolicy.ALWAYS,
                on_skip=CrossGateSkipPolicy.ALLOW_WITH_GAP,
                mode=ContractCheckMode.ARTIFACT_BUNDLE.value,
            ),
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
        )
        cc = result["phases"]["contract_check"]
        for alias in result["phases"]["projects"]:
            entry = cc[alias]
            assert entry["approved"] is False
            assert entry["verdict"] == "SKIPPED"
            assert entry["skipped"] is True
            assert entry["skip_reason"] == "policy_disabled"
            assert entry["source"] == "policy"
            assert entry["on_skip"] == "allow_with_gap"
            assert "operator_feedback" not in entry


class TestContractCheckRunNever:
    def test_writes_policy_never_entries(self, tmp_path: Path) -> None:
        profile_map = _load_real_advanced_with_overrides(
            contract_check=CrossGatePolicy(
                enabled=True,
                run=CrossGateRunPolicy.NEVER,
                on_skip=CrossGateSkipPolicy.ALLOW,
                mode=None,
            ),
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
        )
        cc = result["phases"]["contract_check"]
        for entry in cc.values():
            assert entry["skip_reason"] == "policy_never"
            assert entry["source"] == "policy"
            assert entry["on_skip"] == "allow"


class TestContractCheckManualConfirmExplicitSkip:
    """``manual_confirm`` + ``--decision contract_check=skip`` must skip
    without prompting and carry ``operator_feedback`` into the entry.
    """

    def test_skip_with_feedback(self, tmp_path: Path) -> None:
        from pipeline.control import OperatorDecisionOverride

        profile_map = _load_real_advanced_with_overrides(
            contract_check=CrossGatePolicy(
                enabled=True,
                run=CrossGateRunPolicy.MANUAL_CONFIRM,
                on_skip=CrossGateSkipPolicy.ALLOW_WITH_GAP,
                mode=ContractCheckMode.ARTIFACT_BUNDLE.value,
            ),
        )
        overrides = (
            OperatorDecisionOverride(
                target="contract_check",
                decision="skip",
                feedback="Tiny docs-only change.",
            ),
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
            operator_decisions=overrides,
        )
        cc = result["phases"]["contract_check"]
        for entry in cc.values():
            assert entry["skip_reason"] == "operator_decision"
            assert entry["source"] == "operator"
            assert entry["operator_feedback"] == "Tiny docs-only change."


class TestContractCheckAlwaysExplicitSkip:
    """Explicit operator decisions win over the profile run policy.

    ``run=always`` means the default path is to run the gate, but a CLI
    ``--decision contract_check=skip`` must still be authoritative for
    this invocation. This protects the shared decision surface from
    becoming manual_confirm-only by accident.
    """

    def test_skip_override_beats_always_policy(self, tmp_path: Path) -> None:
        from pipeline.control import OperatorDecisionOverride

        profile_map = _load_real_advanced_with_overrides(
            contract_check=CrossGatePolicy(
                enabled=True,
                run=CrossGateRunPolicy.ALWAYS,
                on_skip=CrossGateSkipPolicy.ALLOW_WITH_GAP,
                mode=ContractCheckMode.ARTIFACT_BUNDLE.value,
            ),
        )
        overrides = (
            OperatorDecisionOverride(
                target="contract_check",
                decision="skip",
                feedback="Operator accepts the verification gap.",
            ),
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
            operator_decisions=overrides,
        )
        cc = result["phases"]["contract_check"]
        for entry in cc.values():
            assert entry["verdict"] == "SKIPPED"
            assert entry["skip_reason"] == "operator_decision"
            assert entry["source"] == "operator"
            assert entry["on_skip"] == "allow_with_gap"
            assert entry["operator_feedback"] == (
                "Operator accepts the verification gap."
            )


class TestContractCheckManualConfirmPauses:
    """``manual_confirm`` without override and with ``no_interactive=True``
    must persist a ``pending_gate`` block and return with status
    ``awaiting_gate_decision``."""

    def test_pause_writes_pending_gate(self, tmp_path: Path) -> None:
        profile_map = _load_real_advanced_with_overrides(
            contract_check=CrossGatePolicy(
                enabled=True,
                run=CrossGateRunPolicy.MANUAL_CONFIRM,
                on_skip=CrossGateSkipPolicy.ALLOW_WITH_GAP,
                mode=ContractCheckMode.ARTIFACT_BUNDLE.value,
            ),
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
            no_interactive=True,
        )
        assert result["status"] == "awaiting_gate_decision"
        assert "pending_gate" in result
        pg = result["pending_gate"]
        assert pg["name"] == "contract_check"
        assert pg["run_policy"] == "manual_confirm"
        assert pg["choices"] == ["run", "skip"]
        assert pg["on_skip"] == "allow_with_gap"


class TestCrossFinalAcceptanceDisabledAllowsDone:
    """When ``cross_final_acceptance`` is disabled by profile policy,
    the runner writes a skipped release-shape audit entry and lets the
    overall status finish ``done`` (no system release gate to fail)."""

    def test_run_finishes_done(self, tmp_path: Path) -> None:
        profile_map = _load_real_advanced_with_overrides(
            cross_final_acceptance=CrossGatePolicy(
                enabled=False,
                run=CrossGateRunPolicy.ALWAYS,
                on_skip=CrossGateSkipPolicy.BLOCK,
                mode=None,
            ),
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
        )
        assert result["status"] == "done"
        cfa = result["phases"]["cross_final_acceptance"]
        assert cfa["approved"] is False
        assert cfa["verdict"] == "SKIPPED"
        assert cfa["ship_ready"] is False
        assert cfa["skipped"] is True
        assert cfa["skip_reason"] == "policy_disabled"


class TestContractCheckArtifactBundleIsSingleCall:
    """Default ``contract_check.mode=artifact_bundle`` runs the reviewer
    once per cross-run, not once per alias. The single verdict is
    mirrored into every per-alias entry with
    ``source="artifact_bundle"`` so the persisted shape stays
    alias-keyed for evidence / MCP."""

    def test_single_call_mirrored_to_all_aliases(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Default policy already sets mode=artifact_bundle; we just
        # need to run with the shipped profile and observe call count.
        profile_map = _load_real_advanced_with_overrides()

        call_count = {"contract_check": 0}
        # ADR 0047 Phase D — three call surfaces use ``accumulate_phase_usage``:
        # ``app.py`` (top-level import alias), and ``planning_loop.py``'s
        # validate / plan / retry inner functions (fresh lazy imports at
        # each call). The canonical source is
        # ``pipeline.cross_project.usage``; patching both the canonical
        # root and the ``app.py`` local binding catches every lookup.
        from pipeline.cross_project import (
            contract_check as cross_contract,
            session_run as cross_session_run,
            usage as cross_usage,
        )

        real_accumulate = cross_usage.accumulate_phase_usage

        def _spy(target, phase, usage):
            if phase == "contract_check":
                call_count["contract_check"] += 1
            return real_accumulate(target, phase, usage)

        monkeypatch.setattr(cross_usage, "accumulate_phase_usage", _spy)
        monkeypatch.setattr(cross_session_run, "_accumulate_phase_usage", _spy)
        monkeypatch.setattr(cross_contract, "_accumulate_phase_usage", _spy)

        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
        )

        assert call_count["contract_check"] == 1, (
            "artifact_bundle must invoke contract_check exactly once"
        )
        cc = result["phases"]["contract_check"]
        for alias in result["phases"]["projects"]:
            assert alias in cc
            assert cc[alias]["source"] == "artifact_bundle"


class TestCrossFinalAcceptanceDisabledOperatorSkipEnforced:
    """When ``cross_final_acceptance`` is disabled by profile policy
    and an OPERATOR skipped ``contract_check`` (manual_confirm +
    ``--decision contract_check=skip``), the runner enforces
    ``on_skip``: ``block`` fails the run; ``allow_with_gap`` /
    ``allow`` finish ``done`` with the audit entry already recorded.

    Policy-driven skips (``source="policy"``: gate absent from the
    profile, ``run=never``, or ``enabled=false``) do NOT trigger the
    enforcement — the profile never asked the gate to run, so its
    ``on_skip`` is artefactual fallback, not an authored decision.
    The "lite + no cross_gates" use case relies on this: the run
    must finish ``done``, not ``failed``.
    """

    def test_operator_skip_on_skip_block_fails_run(
        self, tmp_path: Path,
    ) -> None:
        from pipeline.control import OperatorDecisionOverride

        profile_map = _load_real_advanced_with_overrides(
            contract_check=CrossGatePolicy(
                enabled=True,
                run=CrossGateRunPolicy.MANUAL_CONFIRM,
                on_skip=CrossGateSkipPolicy.BLOCK,
                mode=ContractCheckMode.ARTIFACT_BUNDLE.value,
            ),
            cross_final_acceptance=CrossGatePolicy(
                enabled=False,
                run=CrossGateRunPolicy.ALWAYS,
                on_skip=CrossGateSkipPolicy.BLOCK,
                mode=None,
            ),
        )
        overrides = (
            OperatorDecisionOverride(
                target="contract_check", decision="skip", feedback="x",
            ),
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
            operator_decisions=overrides,
        )
        assert result["status"] == "failed"
        assert "on_skip=block" in result.get("failure_reason", "")
        # ADR 0037: every non-``done`` cross terminal lands
        # ``halt_reason`` + ``evidence.json`` next to ``meta.json``.
        assert result["halt_reason"] == "cross_contract_check_blocking_skip"
        run_dir = tmp_path / "run"
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "failed"
        assert meta["halt_reason"] == "cross_contract_check_blocking_skip"
        assert (run_dir / "evidence.json").is_file()

    def test_operator_skip_allow_with_gap_finishes_done(
        self, tmp_path: Path,
    ) -> None:
        from pipeline.control import OperatorDecisionOverride

        profile_map = _load_real_advanced_with_overrides(
            contract_check=CrossGatePolicy(
                enabled=True,
                run=CrossGateRunPolicy.MANUAL_CONFIRM,
                on_skip=CrossGateSkipPolicy.ALLOW_WITH_GAP,
                mode=ContractCheckMode.ARTIFACT_BUNDLE.value,
            ),
            cross_final_acceptance=CrossGatePolicy(
                enabled=False,
                run=CrossGateRunPolicy.ALWAYS,
                on_skip=CrossGateSkipPolicy.BLOCK,
                mode=None,
            ),
        )
        overrides = (
            OperatorDecisionOverride(
                target="contract_check", decision="skip", feedback="x",
            ),
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profile_map,
            projects=_make_projects(tmp_path),
            operator_decisions=overrides,
        )
        assert result["status"] == "done"

    def test_no_cross_gates_block_finishes_done(self, tmp_path: Path) -> None:
        """The lite use case: a profile with NO ``cross_gates`` block
        resolves both gates to disabled-by-fallback. Both terminal
        gates are skipped with ``source="policy"``; the run must
        finish ``done``. Regressing to the old bogus enforcement
        (which blocked on policy-driven skips because the fallback
        carries ``on_skip=block``) would re-break ``orcho cross
        --profile lite``."""
        from core.infra.paths import CONFIG_DIR
        from pipeline.profiles.loader import load_profiles_v2

        # Load shipped catalogue and pick the no-cross_gates profile.
        # The test asserts the SEMANTIC of the missing block, not
        # the lite profile's other contents.
        profiles = load_profiles_v2(
            CONFIG_DIR / "pipeline_profiles_v2.json",
        )
        # Find any shipped profile that omits the block.
        target = next(
            (p for p in profiles.values() if not p.cross_gates),
            None,
        )
        assert target is not None, (
            "shipped catalogue must include at least one profile "
            "without cross_gates so this regression test runs"
        )
        # Inject as advanced so _cross_run uses it.
        profiles["feature"] = target.__class__(
            **{**target.__dict__, "name": "feature"},
        )
        result = _cross_run(
            tmp_path=tmp_path,
            profile_map=profiles,
            projects=_make_projects(tmp_path),
        )
        assert result["status"] == "done"
