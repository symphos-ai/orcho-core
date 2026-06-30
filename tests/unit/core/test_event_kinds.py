"""REA-2 event vocabulary schema tests.

Three layers:

1. **Vocabulary completeness** — every :class:`EventKind` member has a
 :data:`REQUIRED_PAYLOAD_KEYS` entry. Catches regressions where a new
 kind ships without a contract.
2. **Validator behaviour** — :func:`validate_payload` rejects payloads
 missing required fields and tolerates ad-hoc / plugin kinds.
3. **Live emit coverage** — captures every event the canonical golden
 scenario fires and asserts that each canonical kind passes its own
 contract. This is the regression net for "did we emit a payload that
 our own schema would reject".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.observability.event_kinds import (
    REQUIRED_PAYLOAD_KEYS,
    EventKind,
    EventSchemaError,
    validate_payload,
)


@pytest.fixture(autouse=True)
def _reset_logging_globals():
    """Reset progress.log / agent.log singletons around each test so
 the event-store initializer doesn't mistake a stale prior run for
 a sub-pipeline and skip ``init_event_store``."""
    import agents.stream as _stream
    import core.observability.logging as _core_logging
    _core_logging._progress_log = None
    _stream._agent_log = None
    yield
    _core_logging._progress_log = None
    _stream._agent_log = None


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary completeness
# ─────────────────────────────────────────────────────────────────────────────


def test_every_event_kind_has_required_payload_entry() -> None:
    """Every member of :class:`EventKind` must declare its contract."""
    missing = [k for k in EventKind if k not in REQUIRED_PAYLOAD_KEYS]
    assert not missing, (
        f"event kinds missing REQUIRED_PAYLOAD_KEYS entry: {missing}"
    )


def test_required_payload_keys_only_references_known_kinds() -> None:
    """Reverse direction: REQUIRED_PAYLOAD_KEYS only references valid kinds."""
    extras = [k for k in REQUIRED_PAYLOAD_KEYS if k not in set(EventKind)]
    assert not extras, (
        f"REQUIRED_PAYLOAD_KEYS references unknown kinds: {extras}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Validator behaviour
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_payload_accepts_single_project_run_start() -> None:
    validate_payload(
        "run.start",
        {
            "task": "x",
            "run_kind": "single_project",
            "project": "/p",
            "profile": "feature",
        },
    )


def test_validate_payload_rejects_missing_required_field() -> None:
    with pytest.raises(EventSchemaError, match="task"):
        validate_payload(
            "run.start",
            {
                "run_kind": "single_project",
                "project": "/p",
                "profile": "feature",
            },
        )


def test_validate_payload_accepts_extra_fields() -> None:
    """Extra fields are fine — the contract is a *lower* bound."""
    validate_payload(
        "run.start",
        {
            "task": "x",
            "run_kind": "single_project",
            "project": "/p",
            "profile": "feature",
            "extra": 1,
        },
    )


class TestRea36ChildRunLinkage:
    """REA-3.6: child runs spawned by the cross orchestrator carry
 parent_run_id + project_alias on their run.start payloads."""

    BASE = {
        "task":     "x",
        "run_kind": "single_project",
        "project":  "/p",
        "profile":  "task",
    }

    def test_linkage_fields_optional(self) -> None:
        # Standalone single-project runs omit the linkage entirely.
        validate_payload("run.start", dict(self.BASE))

    def test_linkage_fields_accepted_when_paired(self) -> None:
        validate_payload(
            "run.start",
            {**self.BASE, "parent_run_id": "20260508_133045", "project_alias": "api"},
        )

    def test_linkage_fields_rejected_when_only_one_set(self) -> None:
        with pytest.raises(EventSchemaError, match="parent_run_id and project_alias"):
            validate_payload(
                "run.start", {**self.BASE, "parent_run_id": "20260508_133045"},
            )
        with pytest.raises(EventSchemaError, match="parent_run_id and project_alias"):
            validate_payload("run.start", {**self.BASE, "project_alias": "api"})

    def test_linkage_fields_must_be_non_empty_strings(self) -> None:
        with pytest.raises(EventSchemaError, match="parent_run_id"):
            validate_payload(
                "run.start",
                {**self.BASE, "parent_run_id": "", "project_alias": "api"},
            )
        with pytest.raises(EventSchemaError, match="project_alias"):
            validate_payload(
                "run.start",
                {**self.BASE, "parent_run_id": "p", "project_alias": 123},
            )


def test_validate_payload_accepts_cross_project_run_start() -> None:
    validate_payload(
        "run.start",
        {
            "task": "x",
            "run_kind": "cross_project",
            "projects": [
                {"alias": "api", "path": "/repo/api"},
                {"alias": "web", "path": "/repo/web"},
            ],
            "cross_mode": "full",
            "profile": "feature",
            "plan_source": "cross",
            "projected_profile": "feature#project",
        },
    )


def test_validate_payload_rejects_malformed_cross_project_run_start() -> None:
    with pytest.raises(EventSchemaError, match="projects"):
        validate_payload(
            "run.start",
            {
                "task": "x",
                "run_kind": "cross_project",
                "projects": ["api:/repo/api"],
                "cross_mode": "full",
                "profile": "feature",
                "plan_source": "cross",
            },
        )

    with pytest.raises(EventSchemaError, match="cross_mode"):
        validate_payload(
            "run.start",
            {
                "task": "x",
                "run_kind": "cross_project",
                "projects": [{"alias": "api", "path": "/repo/api"}],
                "cross_mode": "bad",
                "profile": "feature",
                "plan_source": "cross",
            },
        )


def test_validate_payload_rejects_unknown_run_kind() -> None:
    with pytest.raises(EventSchemaError, match="run_kind"):
        validate_payload("run.start", {"task": "x", "run_kind": "workspace"})


def test_validate_payload_silently_skips_unknown_kinds() -> None:
    """Plugin / experimental kinds are advisory, not enforced."""
    validate_payload("plugin.custom_event", {"any": "thing"})


def test_validate_payload_rea2_new_kinds_required_fields() -> None:
    """REA-2 additions carry the contract we documented in the module."""
    with pytest.raises(EventSchemaError, match="source"):
        validate_payload("plan.parsed", {"subtask_count": 1, "has_contract": True})

    with pytest.raises(EventSchemaError, match="gate_kind"):
        validate_payload("gate.start", {"name": "tests"})

    with pytest.raises(EventSchemaError, match="outcome"):
        validate_payload(
            "gate.end", {"name": "tests", "duration_s": 0.1},
        )

    with pytest.raises(EventSchemaError, match="cwd"):
        validate_payload("command.start", {"argv_summary": "pytest -q"})

    with pytest.raises(EventSchemaError, match="exit_code"):
        validate_payload(
            "command.end", {"duration_s": 0.1, "outcome": "ok"},
        )

    with pytest.raises(EventSchemaError, match="artifact_kind"):
        validate_payload("artifact.created", {"path": "/x"})

    with pytest.raises(EventSchemaError, match="guardrail"):
        validate_payload("agent.guardrail", {"agent": "claude", "action": "abort"})

    with pytest.raises(EventSchemaError, match="skill_name"):
        validate_payload("agent.skill_use", {"text": "Using `frontend-qa`."})
    with pytest.raises(EventSchemaError, match="text"):
        validate_payload("agent.skill_use", {"skill_name": "frontend-qa"})
    validate_payload(
        "agent.skill_use",
        {"skill_name": "frontend-qa", "text": "Using `frontend-qa`."},
    )
    with pytest.raises(EventSchemaError, match="format"):
        validate_payload("agent.contract_ready", {"agent": "PLAN"})
    validate_payload(
        "agent.contract_ready",
        {"agent": "PLAN", "format": "json"},
    )

    # agent.mcp_tool_call requires server / tool_name / status; each missing
    # field must fail loudly so the wire format stays load-bearing for the UI.
    with pytest.raises(EventSchemaError, match="server"):
        validate_payload(
            "agent.mcp_tool_call",
            {"tool_name": "t", "status": "completed"},
        )
    with pytest.raises(EventSchemaError, match="tool_name"):
        validate_payload(
            "agent.mcp_tool_call",
            {"server": "s", "status": "completed"},
        )
    with pytest.raises(EventSchemaError, match="status"):
        validate_payload(
            "agent.mcp_tool_call",
            {"server": "s", "tool_name": "t"},
        )
    validate_payload(
        "agent.mcp_tool_call",
        {"server": "s", "tool_name": "t", "status": "completed"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Live emit coverage from the canonical golden scenario
# ─────────────────────────────────────────────────────────────────────────────


def test_canonical_emit_sites_pass_their_own_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the canonical mock pipeline; every emitted canonical-vocabulary
 event must satisfy its declared payload contract.

 Catches the regression "I emitted ``plan.parsed`` but forgot one of
 its required fields" — failure point would be a downstream
 consumer breaking on read, surfaced as a unit test instead.
 """
    from agents.runtimes._strategy import MockAgentProvider
    from core.observability import events as _events
    from pipeline.plugins import PluginConfig
    from pipeline.project_orchestrator import run_pipeline

    monkeypatch.setattr(
        "pipeline.project.session_run.load_plugin",
        lambda *_a, **_kw: PluginConfig(name="schema-coverage"),
    )
    monkeypatch.setattr(
        "core.io.git_helpers.has_uncommitted",
        lambda *_a, **_kw: True,
    )
    monkeypatch.setattr(
        "core.io.git_helpers.git_diff_stat",
        lambda *_a, **_kw: "1 file changed",
    )

    from tests.conftest import init_git_repo
    project = tmp_path / "proj"
    init_git_repo(project)
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    monkeypatch.chdir(project)

    run_pipeline(
        task="cover schema",
        project_dir=str(project),
        output_dir=out_dir,
        provider=MockAgentProvider(latency=0.0),
        max_rounds=1,
        profile_name="feature",
    )

    events = _events.read_all(out_dir)
    canonical = set(EventKind)
    by_kind: dict[str, int] = {}

    for evt in events:
        if evt.kind not in canonical:
            continue
        by_kind[evt.kind] = by_kind.get(evt.kind, 0) + 1
        # Raises EventSchemaError if the live payload missed required
        # fields — that's exactly the contract this test enforces.
        validate_payload(evt.kind, evt.payload)

    # Sanity floor: confirm the run actually exercised the lifecycle
    # vocabulary, otherwise the validation above is vacuous.
    assert by_kind.get("run.start") == 1
    assert by_kind.get("run.end") == 1
    assert by_kind.get("phase.start", 0) >= 3
    assert by_kind.get("phase.end", 0) >= 3
    assert by_kind.get("plan.parsed", 0) >= 1
    run_start = next(evt for evt in events if evt.kind == "run.start")
    assert run_start.payload["run_kind"] == "single_project"
    assert run_start.payload["profile"] == "feature"


def test_cross_project_run_start_emit_site_passes_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-project ``run.start`` uses the first-class cross payload.

 This is the live regression net for the cross emitter: it must not
 satisfy the schema by pretending to be a single-project run.
 """
    from agents.runtimes._strategy import MockAgentProvider
    from core.observability import events as _events
    from pipeline.cross_project import profile_projection as _projection
    from pipeline.cross_project.orchestrator import run_cross_pipeline
    from pipeline.plugins import PluginConfig

    # Phase 5+6: the built-in ``advanced`` profile declares non-bypass
    # ``handoff``; cross projection fail-fast refuses it (cross handoff
    # support is a later slice). This event-emitter regression test
    # only exercises the ``run.start`` payload shape, not the handoff
    # contract — suppress the guard locally so the legacy emit path
    # remains reachable. Mirror of the conftest-level suppression used
    # by cross_project and acceptance test suites.
    monkeypatch.setattr(
        _projection,
        "_reject_non_bypass_handoff_in_projection",
        lambda profile_name, global_steps, project_steps: None,
    )
    monkeypatch.setattr(
        _projection, "_reject_non_bypass_handoff", lambda profile: None,
    )

    monkeypatch.setattr(
        "pipeline.cross_project.run_setup.load_plugin",
        lambda *_a, **_kw: PluginConfig(name="schema-coverage"),
    )

    projects = {
        "api": tmp_path / "api",
        "web": tmp_path / "web",
    }
    from tests.conftest import init_git_repo
    for project in projects.values():
        init_git_repo(project)
    out_dir = tmp_path / "cross_run"
    out_dir.mkdir()

    run_cross_pipeline(
        task="cover cross schema",
        projects=projects,
        output_dir=out_dir,
        provider=MockAgentProvider(latency=0.0),
        cross_mode="plan",
    )

    events = _events.read_all(out_dir)
    run_start = next(evt for evt in events if evt.kind == "run.start")
    validate_payload(run_start.kind, run_start.payload)
    assert run_start.payload["run_kind"] == "cross_project"
    assert run_start.payload["cross_mode"] == "plan"
    assert run_start.payload["profile"] == "feature"
    assert run_start.payload["plan_source"] == "cross"
    # Projected profile is None for "plan"-only mode because the projection
    # has no project_steps — the cross runner stops at cross_plan.md.
    assert run_start.payload["projects"] == [
        {"alias": "api", "path": str(projects["api"].resolve())},
        {"alias": "web", "path": str(projects["web"].resolve())},
    ]
