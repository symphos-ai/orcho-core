"""Admission policy for a verification gate handoff's ``available_actions``.

Pins the two questions :mod:`pipeline.project.gate_handoff_actions` answers:

* which menu a failure set earns (hygiene / env-retryable / repairable), and
* whether a *persisted* record proves enough for ``retry_verification`` to be
  offered at all — the engine re-executes exactly the persisted blocking set
  with no agent, so the record must name a complete, duplicate-free identity
  set, carry a ``receipt_evidence`` pointer on every element, and agree with
  its own findings about which commands are blocking.

Fail-closed is the invariant under test: every defect below removes the retry
from the menu rather than offering a re-execution the engine cannot address.
Pure — synthetic artifact dicts, no filesystem, no run object.
"""

from __future__ import annotations

import pytest

from pipeline.control.handoff_routing import GateIdentity
from pipeline.project import gate_handoff_actions as policy

# ── helpers ──────────────────────────────────────────────────────────────────


def _finding(command: str, kind: str = "env_failure") -> dict:
    return {
        "id": f"verification_gate_{kind}",
        "severity": "P3" if kind != "timeout" else "P1",
        "title": f"Verification gate {kind}",
        "body": f"{command} failed",
        "required_fix": "fix it",
        "failure_kind": kind,
        "command": command,
    }


def _identity(command: str, *, evidence: str | None = "ev/lint.json") -> dict:
    entry = {"command": command, "hook": "after_phase", "phase": "implement"}
    if evidence is not None:
        entry["receipt_evidence"] = evidence
    return entry


def _artifacts(
    *,
    findings: list | None = None,
    identities: list | None = None,
    primary: object = ...,
) -> dict:
    ids = (
        identities
        if identities is not None
        else [_identity("lint", evidence="ev/lint.json"),
              _identity("typecheck", evidence="ev/typecheck.json")]
    )
    art: dict = {
        "findings": (
            findings
            if findings is not None
            else [_finding("lint"), _finding("typecheck")]
        ),
        "gate_identities": ids,
    }
    if primary is ...:
        art["gate_identity"] = {
            "command": "lint", "hook": "after_phase", "phase": "implement",
        }
    elif primary is not None:
        art["gate_identity"] = primary
    return art


# ── identity parser ──────────────────────────────────────────────────────────


class TestPersistedGateIdentities:
    def test_parses_complete_set_primary_first(self) -> None:
        identities = policy.persisted_gate_identities(_artifacts())
        assert identities == (
            GateIdentity("lint", "after_phase", "implement"),
            GateIdentity("typecheck", "after_phase", "implement"),
        )

    def test_primary_is_moved_to_the_front(self) -> None:
        art = _artifacts(primary={
            "command": "typecheck", "hook": "after_phase", "phase": "implement",
        })
        identities = policy.persisted_gate_identities(art)
        assert identities is not None
        assert identities[0].command == "typecheck"
        assert {i.command for i in identities} == {"lint", "typecheck"}

    def test_receipt_evidence_is_ignored_by_the_identity_parser(self) -> None:
        """Identity is the triple. Whether it is *proven* is a separate
        question, so an unproven-but-well-formed set still parses."""
        bare = _artifacts(identities=[
            _identity("lint", evidence=None), _identity("typecheck", evidence=None),
        ])
        assert policy.persisted_gate_identities(bare) == (
            GateIdentity("lint", "after_phase", "implement"),
            GateIdentity("typecheck", "after_phase", "implement"),
        )

    def test_empty_phase_string_is_a_valid_identity(self) -> None:
        """``before_delivery`` gates carry an empty phase; only the type is
        load-bearing there, not the content."""
        art = {
            "gate_identities": [
                {"command": "lint", "hook": "before_delivery", "phase": ""},
            ],
            "gate_identity": {
                "command": "lint", "hook": "before_delivery", "phase": "",
            },
        }
        assert policy.persisted_gate_identities(art) == (
            GateIdentity("lint", "before_delivery", ""),
        )

    @pytest.mark.parametrize("artifacts", [
        None,
        "not a mapping",
        {},
        {"gate_identities": []},
        {"gate_identities": "lint"},
        {"gate_identities": ["lint"]},
    ])
    def test_malformed_container_returns_none(self, artifacts) -> None:
        assert policy.persisted_gate_identities(artifacts) is None

    @pytest.mark.parametrize("element", [
        {"command": "lint", "hook": "after_phase"},          # no phase
        {"command": "lint", "phase": "implement"},            # no hook
        {"hook": "after_phase", "phase": "implement"},        # no command
        {"command": "", "hook": "after_phase", "phase": "implement"},
        {"command": "lint", "hook": "", "phase": "implement"},
        {"command": "lint", "hook": "after_phase", "phase": 7},
    ])
    def test_incomplete_element_returns_none(self, element) -> None:
        art = _artifacts(identities=[element])
        assert policy.persisted_gate_identities(art) is None

    def test_duplicate_identity_returns_none(self) -> None:
        art = _artifacts(identities=[_identity("lint"), _identity("lint")])
        assert policy.persisted_gate_identities(art) is None

    def test_missing_primary_returns_none(self) -> None:
        art = _artifacts(primary=None)
        assert policy.persisted_gate_identities(art) is None

    def test_malformed_primary_returns_none(self) -> None:
        art = _artifacts(primary={"command": "lint", "hook": "after_phase"})
        assert policy.persisted_gate_identities(art) is None

    def test_primary_outside_the_set_returns_none(self) -> None:
        art = _artifacts(primary={
            "command": "vitest", "hook": "after_phase", "phase": "implement",
        })
        assert policy.persisted_gate_identities(art) is None


# ── evidence parser ──────────────────────────────────────────────────────────


class TestPersistedGateEvidence:
    def test_maps_every_identity_to_its_receipt(self) -> None:
        assert policy.persisted_gate_evidence(_artifacts()) == {
            GateIdentity("lint", "after_phase", "implement"): "ev/lint.json",
            GateIdentity("typecheck", "after_phase", "implement"):
                "ev/typecheck.json",
        }

    @pytest.mark.parametrize("evidence", [None, "", "   ", 7, {"path": "x"}])
    def test_any_unproven_element_returns_none(self, evidence) -> None:
        """Partial evidence is not evidence: one element without a usable
        pointer disqualifies the whole set."""
        entry = _identity("typecheck", evidence=None)
        if evidence is not None:
            entry["receipt_evidence"] = evidence
        art = _artifacts(identities=[_identity("lint"), entry])
        assert policy.persisted_gate_evidence(art) is None

    def test_malformed_identity_set_returns_none(self) -> None:
        art = _artifacts(identities=[_identity("lint"), _identity("lint")])
        assert policy.persisted_gate_evidence(art) is None


# ── findings classification ──────────────────────────────────────────────────


class TestFindingsClassification:
    @pytest.mark.parametrize("findings", [None, [], (), "env_failure", ["x"]])
    def test_malformed_findings_are_not_hygiene(self, findings) -> None:
        assert not policy.findings_are_hygiene(findings)

    def test_env_only_set_is_eligible(self) -> None:
        assert policy.env_retry_eligible(
            [_finding("lint"), _finding("typecheck")],
        )

    @pytest.mark.parametrize("kind", [
        "timeout", "provenance_failure", "unverifiable", "test_failure",
    ])
    def test_any_other_kind_is_not_eligible(self, kind) -> None:
        assert not policy.env_retry_eligible(
            [_finding("lint"), _finding("typecheck", kind)],
        )

    def test_finding_without_explicit_kind_is_not_eligible(self) -> None:
        """No severity proxy: a P3 finding that never said what it was is not
        proof of an environment failure."""
        bare = {"command": "lint", "severity": "P3"}
        assert not policy.env_retry_eligible([bare])

    @pytest.mark.parametrize("findings", [None, [], (), "env_failure", ["x"]])
    def test_malformed_findings_are_not_eligible(self, findings) -> None:
        assert not policy.env_retry_eligible(findings)

    def test_hygiene_reads_failure_kind_not_severity(self) -> None:
        # A timeout is agent-unfixable yet P1 — the severity proxy would offer
        # it a repair retry.
        assert policy.findings_are_hygiene([_finding("lint", "timeout")])
        assert not policy.findings_are_hygiene([_finding("lint", "test_failure")])

    def test_hygiene_falls_back_to_severity_without_a_kind(self) -> None:
        assert policy.findings_are_hygiene([{"severity": "P3"}])
        assert not policy.findings_are_hygiene([{"severity": "P1"}])

    def test_hygiene_requires_every_finding(self) -> None:
        assert not policy.findings_are_hygiene(
            [_finding("lint"), _finding("typecheck", "test_failure")],
        )


# ── admission ────────────────────────────────────────────────────────────────


class TestEnvRetryAdmissible:
    def test_env_only_set_with_full_evidence_is_admissible(self) -> None:
        assert policy.env_retry_admissible(_artifacts())

    @pytest.mark.parametrize("kind", ["timeout", "test_failure", "provenance_failure"])
    def test_mixed_kind_set_is_not_admissible(self, kind) -> None:
        art = _artifacts(findings=[_finding("lint"), _finding("typecheck", kind)])
        assert not policy.env_retry_admissible(art)

    @pytest.mark.parametrize("kind", ["provenance_failure", "timeout"])
    def test_single_non_env_kind_is_not_admissible(self, kind) -> None:
        art = _artifacts(
            findings=[_finding("lint", kind)],
            identities=[_identity("lint")],
        )
        assert not policy.env_retry_admissible(art)

    def test_findings_without_failure_kind_are_not_admissible(self) -> None:
        art = _artifacts(findings=[{"command": "lint", "severity": "P3"}])
        assert not policy.env_retry_admissible(art)

    @pytest.mark.parametrize("identities", [
        [],
        [{"command": "lint", "hook": "after_phase"}],
        [_identity("lint"), _identity("lint")],
    ])
    def test_malformed_identities_are_not_admissible(self, identities) -> None:
        art = _artifacts(identities=identities)
        assert not policy.env_retry_admissible(art)

    def test_primary_outside_the_set_is_not_admissible(self) -> None:
        art = _artifacts(primary={
            "command": "vitest", "hook": "after_phase", "phase": "implement",
        })
        assert not policy.env_retry_admissible(art)

    def test_malformed_primary_is_not_admissible(self) -> None:
        art = _artifacts(primary={"command": "lint", "hook": "after_phase"})
        assert not policy.env_retry_admissible(art)

    def test_element_without_receipt_evidence_is_not_admissible(self) -> None:
        art = _artifacts(identities=[
            _identity("lint"), _identity("typecheck", evidence=None),
        ])
        assert not policy.env_retry_admissible(art)

    def test_command_disagreement_is_not_admissible(self) -> None:
        """Findings and identities describing different command sets means the
        record cannot say which set the operator decided on."""
        art = _artifacts(
            findings=[_finding("lint"), _finding("vitest")],
            identities=[_identity("lint"), _identity("typecheck")],
        )
        assert not policy.env_retry_admissible(art)

    def test_identities_wider_than_findings_is_not_admissible(self) -> None:
        art = _artifacts(findings=[_finding("lint")])
        assert not policy.env_retry_admissible(art)

    @pytest.mark.parametrize("artifacts", [None, "nope", {}, {"findings": []}])
    def test_malformed_artifacts_are_not_admissible(self, artifacts) -> None:
        assert not policy.env_retry_admissible(artifacts)


# ── the menu ─────────────────────────────────────────────────────────────────


class _Profile:
    """Duck-typed stand-in; ``_repair_step`` is patched per test."""


@pytest.fixture
def with_repair(monkeypatch: pytest.MonkeyPatch):
    from pipeline.project import gate_repair

    monkeypatch.setattr(gate_repair, "_repair_step", lambda _profile: object())


@pytest.fixture
def without_repair(monkeypatch: pytest.MonkeyPatch):
    from pipeline.project import gate_repair

    monkeypatch.setattr(gate_repair, "_repair_step", lambda _profile: None)


class TestVerificationHandoffActions:
    def test_env_retryable_hygiene_leads_with_retry(self, with_repair) -> None:
        assert policy.verification_handoff_actions(
            _Profile(), hygiene=True, env_retryable=True,
        ) == ("retry_verification", "continue_with_waiver", "halt")

    def test_hygiene_without_env_retry_is_waiver_or_halt(self, with_repair) -> None:
        assert policy.verification_handoff_actions(
            _Profile(), hygiene=True, env_retryable=False,
        ) == ("continue_with_waiver", "halt")

    def test_env_retryable_defaults_off(self, with_repair) -> None:
        assert policy.verification_handoff_actions(
            _Profile(), hygiene=True,
        ) == ("continue_with_waiver", "halt")

    def test_repairable_set_with_repair_step_unchanged(self, with_repair) -> None:
        assert policy.verification_handoff_actions(
            _Profile(), hygiene=False, env_retryable=False,
        ) == ("continue", "retry_feedback", "halt", "continue_with_waiver")

    def test_repairable_set_without_repair_step_unchanged(
        self, without_repair,
    ) -> None:
        assert policy.verification_handoff_actions(
            _Profile(), hygiene=False, env_retryable=False,
        ) == ("continue", "halt", "continue_with_waiver")

    @pytest.mark.parametrize("env_retryable", [True, False])
    def test_non_hygiene_menu_never_gains_the_retry(
        self, with_repair, env_retryable,
    ) -> None:
        """``retry_verification`` is an env-only action: an agent-fixable
        member keeps the repair path and never earns a gate rerun."""
        actions = policy.verification_handoff_actions(
            _Profile(), hygiene=False, env_retryable=env_retryable,
        )
        assert "retry_verification" not in actions


# ── admission → menu, end to end over the persisted record ───────────────────


_TABLE = [
    pytest.param(_artifacts(), True, id="env-only-with-evidence"),
    pytest.param(
        _artifacts(findings=[_finding("lint"), _finding("typecheck", "timeout")]),
        False, id="env-plus-timeout",
    ),
    pytest.param(
        _artifacts(
            findings=[_finding("lint"), _finding("typecheck", "test_failure")],
        ),
        False, id="env-plus-test-failure",
    ),
    pytest.param(
        _artifacts(
            findings=[_finding("lint", "provenance_failure")],
            identities=[_identity("lint")],
        ),
        False, id="provenance-only",
    ),
    pytest.param(
        _artifacts(
            findings=[_finding("lint", "timeout")],
            identities=[_identity("lint")],
        ),
        False, id="timeout-only",
    ),
    pytest.param(
        _artifacts(findings=[{"command": "lint", "severity": "P3"}]),
        False, id="findings-without-failure-kind",
    ),
    pytest.param(_artifacts(identities=[]), False, id="empty-identities"),
    pytest.param(
        _artifacts(identities=[{"command": "lint", "phase": "implement"}]),
        False, id="identity-without-hook",
    ),
    pytest.param(
        _artifacts(identities=[_identity("lint"), _identity("lint")]),
        False, id="duplicate-identity",
    ),
    pytest.param(
        _artifacts(primary={
            "command": "vitest", "hook": "after_phase", "phase": "implement",
        }),
        False, id="primary-outside-set",
    ),
    pytest.param(
        _artifacts(primary={"command": "lint", "hook": "after_phase"}),
        False, id="primary-malformed",
    ),
    pytest.param(
        _artifacts(identities=[
            _identity("lint"), _identity("typecheck", evidence=None),
        ]),
        False, id="element-without-receipt-evidence",
    ),
    pytest.param(
        _artifacts(findings=[_finding("lint"), _finding("vitest")]),
        False, id="command-mismatch",
    ),
]


@pytest.mark.parametrize(("artifacts", "expect_retry"), _TABLE)
def test_admission_table_drives_the_menu(
    with_repair, artifacts: dict, expect_retry: bool,
) -> None:
    admissible = policy.env_retry_admissible(artifacts)
    assert admissible is expect_retry
    actions = policy.verification_handoff_actions(
        _Profile(),
        hygiene=policy.findings_are_hygiene(artifacts.get("findings")),
        env_retryable=admissible,
    )
    if expect_retry:
        assert actions[0] == "retry_verification"
    else:
        assert "retry_verification" not in actions
