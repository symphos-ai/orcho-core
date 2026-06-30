"""Account-identity diagnostics threaded into the run header.

Covers the run-setup probe orchestration (``resolve_phase_identities``) and the
header rendering (``render_run_header``). The probe is best-effort and
diagnostic only; these tests defend the load-bearing invariants:

* It runs at most once per distinct agent **instance** (dedup), and never
  collapses two instances of the same runtime name — different accounts on the
  same runtime must each surface, since hiding that is the exact failure mode
  the feature exists to catch.
* It is a no-op when disabled (dry-run / non-TERMINAL), firing no probe.
* A probe that raises never breaks run setup.
* The header shows ``account=...`` only for available identities.
"""

from __future__ import annotations

from agents.registry import PhaseAgentConfig
from agents.runtimes.identity import RuntimeIdentity
from core.io.transcript import render_run_header
from pipeline.project.run_setup import resolve_phase_identities


class _FakeAgent:
    def __init__(self, runtime: str, identity=None, *, raises: bool = False) -> None:
        self.runtime = runtime
        self._identity = identity
        self._raises = raises
        self.calls = 0

    def probe_identity(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError("status command exploded")
        return self._identity


def _available(runtime: str, label: str, email: str) -> RuntimeIdentity:
    return RuntimeIdentity(
        runtime=runtime, source="runtime_status", available=True,
        provider="anthropic", account_label=label, email=email,
    )


def _config(**overrides) -> PhaseAgentConfig:
    """Build a PhaseAgentConfig, defaulting every unspecified slot to a shared
    sentinel agent (so callers control only the slots under test)."""
    shared = overrides.pop("_default", _FakeAgent("claude", None))
    slots = {
        "plan_agent": shared,
        "validate_plan_agent": shared,
        "implement_agent": shared,
        "review_changes_agent": shared,
        "repair_changes_agent": shared,
        "repair_escalation_agent": shared,
        "final_acceptance_agent": shared,
    }
    slots.update(overrides)
    return PhaseAgentConfig(**slots)


class TestResolvePhaseIdentities:
    def test_disabled_fires_no_probe(self) -> None:
        agent = _FakeAgent("claude", _available("claude", "Org", "a@b.c"))
        cfg = _config(_default=agent)
        out = resolve_phase_identities(cfg, enabled=False)
        assert out == {}
        assert agent.calls == 0  # the lazy/side-effect-free invariant

    def test_none_config_is_empty(self) -> None:
        assert resolve_phase_identities(None, enabled=True) == {}

    def test_available_identity_surfaces_per_phase(self) -> None:
        plan = _FakeAgent("claude", _available("claude", "Smart-gamma", "sales@x.io"))
        cfg = _config(plan_agent=plan, _default=_FakeAgent("codex", None))
        out = resolve_phase_identities(cfg, enabled=True)
        assert out["plan"].account_label == "Smart-gamma"
        assert out["plan"].email == "sales@x.io"

    def test_dedup_by_instance_probes_once(self) -> None:
        # implement + repair_changes share ONE instance → probe once.
        shared = _FakeAgent("claude", _available("claude", "Org", "a@b.c"))
        cfg = _config(
            implement_agent=shared,
            repair_changes_agent=shared,
            _default=_FakeAgent("codex", None),
        )
        resolve_phase_identities(cfg, enabled=True)
        assert shared.calls == 1

    def test_same_runtime_name_different_accounts_not_collapsed(self) -> None:
        # F2: two DIFFERENT claude instances under different accounts must both
        # surface — collapsing by runtime name would hide the mismatch.
        plan = _FakeAgent("claude", _available("claude", "Org-A", "a@x.io"))
        implement = _FakeAgent("claude", _available("claude", "Org-B", "b@x.io"))
        cfg = _config(
            plan_agent=plan,
            implement_agent=implement,
            _default=_FakeAgent("codex", None),
        )
        out = resolve_phase_identities(cfg, enabled=True)
        assert out["plan"].account_label == "Org-A"
        assert out["implement"].account_label == "Org-B"
        assert plan.calls == 1
        assert implement.calls == 1

    def test_unavailable_identity_is_omitted(self) -> None:
        plan = _FakeAgent("codex", RuntimeIdentity.unavailable("codex", "x"))
        cfg = _config(plan_agent=plan, _default=_FakeAgent("codex", None))
        out = resolve_phase_identities(cfg, enabled=True)
        assert "plan" not in out

    def test_raising_probe_does_not_break_setup(self) -> None:
        boom = _FakeAgent("claude", None, raises=True)
        good = _FakeAgent("claude", _available("claude", "Org", "a@b.c"))
        cfg = _config(plan_agent=boom, implement_agent=good,
                      _default=_FakeAgent("codex", None))
        out = resolve_phase_identities(cfg, enabled=True)  # must not raise
        assert "plan" not in out
        assert out["implement"].account_label == "Org"


class TestRenderHeaderAccountHint:
    def _render(self, agents) -> str:
        return render_run_header(
            run_id="r1", project="/p", task="t", agents=agents,
            profile="feature", session_mode="auto", rounds=2, plan=True,
        )

    def test_available_account_is_shown(self) -> None:
        out = self._render([
            {"role": "PLAN", "model": "claude-opus-4-8", "effort": "high",
             "account": "account=Smart-gamma / sales@x.io"},
        ])
        assert "account=Smart-gamma / sales@x.io" in out

    def test_provider_default_org_label_renders_as_email_only(self) -> None:
        identity = RuntimeIdentity(
            runtime="claude",
            source="runtime_status",
            available=True,
            provider="anthropic",
            account_label="jekccs@gmail.com's Organization",
            email="jekccs@gmail.com",
        )

        out = self._render([
            {"role": "PLAN", "model": "claude-opus-4-8", "effort": "high",
             "account": identity.hint()},
        ])

        assert "account=jekccs@gmail.com" in out
        assert "Organization /" not in out

    def test_missing_account_renders_nothing_extra(self) -> None:
        out = self._render([
            {"role": "PLAN", "model": "claude-opus-4-8", "effort": "high"},
        ])
        assert "account=" not in out
