"""tests/acceptance/test_golden_scenario.py — REA-0 acceptance baseline.

The golden scenario is the canonical end-to-end mock run that future REA
roadmap milestones must keep green:

* REA-0 (this milestone): full multi-phase loop fires deterministically
  on a small fixture project; ``evidence.json`` placeholder is written.
* REA-1: typed plan contract — same scenario with plan validation.
* REA-2: event spine — same scenario, asserted against canonical event
  vocabulary.
* REA-3: evidence bundle — same scenario, full bundle composed.
* REA-4: MCP control loop — same scenario, exercised via MCP tools.

Keep this test tight. New phase types or event kinds added to the
pipeline must extend the assertions here so a regression in the golden
loop fails fast in CI.

The fixture lives at ``examples/golden-api/``. It is intentionally tiny
but resembles a real fix: a validation function with a known bug plus a
test suite encoding the desired behaviour. The acceptance test below
copies the fixture into ``tmp_path``, ``git init``s it, runs the
pipeline in mock mode, and asserts the canonical timeline + run dir
shape.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pipeline.evidence import (
    EVIDENCE_FILE_NAME,
    EVIDENCE_SCHEMA_VERSION_PLACEHOLDER,
)

GOLDEN_FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "golden-api"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


@pytest.fixture
def golden_project(tmp_path: Path) -> Path:
    """Materialise ``examples/golden-api/`` into a fresh git repo.

    A git repo is required because the orchestrator reads working-tree
    state during REVIEW / FIX phases. Files are copied (not symlinked)
    so the fixture isn't mutated by the run.
    """
    project = tmp_path / "golden-api"
    shutil.copytree(GOLDEN_FIXTURE, project)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "golden@example.com")
    _git(project, "config", "user.name", "Golden")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    return project


@pytest.fixture
def golden_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Workspace dir holding ``runs/<run_id>/`` outputs.

    Setting ``ORCHO_WORKSPACE`` so the subprocess CLI resolves it via the
    same env-var contract real users use.

    ``ORCHO_RUNSPACE`` is pinned to ``<ws>/runspace`` as well — the runs-dir
    resolver (``core.infra.platform``) reads ``ORCHO_RUNSPACE`` *before*
    ``ORCHO_WORKSPACE/runspace``, so an ambient ``ORCHO_RUNSPACE`` inherited
    from an enclosing run (e.g. when the suite executes inside an Orcho
    orchestration) would otherwise win and land the fixed ``GOLDEN_REA0``
    run in the real workspace, colliding with prior artifacts. Overriding
    both together — mirroring ``pipeline/project/cli.py`` — keeps the
    fixture hermetic regardless of the caller's environment.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    monkeypatch.setenv("ORCHO_RUNSPACE", str(ws / "runspace"))
    return ws


# ─────────────────────────────────────────────────────────────────────────────
# REA-0 acceptance assertions
# ─────────────────────────────────────────────────────────────────────────────


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _expected_phase_kinds() -> set[str]:
    """Phase kinds the feature golden run must exercise.

    Source of truth: ``pipeline/runtime/roles.py:ProfileKind`` and the shipped
    ``feature`` profile in ``core/_config/pipeline_profiles_v2.json``. Keep
    this set in sync — additions / renames in either place must update
    this assertion. The shipped ``feature`` profile currently sets
    ``hypothesis.attempts=0``, so HYPOTHESIS is intentionally absent here.
    """
    return {
        "PLAN",
        "VALIDATE_PLAN",
        "IMPLEMENT",
        "REVIEW_CHANGES",
        "REPAIR_CHANGES",
        "FINAL_ACCEPTANCE",
    }


def _run_golden(project: Path) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """Invoke the orcho CLI as a real subprocess.

    Sub-process invocation (instead of in-process ``run_pipeline``)
    matches the contributor-facing one-command workflow advertised in
    the fixture README. It also flushes any module-level state that
    would otherwise leak between tests.
    """
    run_id = "GOLDEN_REA0"
    proc = subprocess.run(
        [
            sys.executable, "-m", "pipeline.project_orchestrator",
            "--project", str(project),
            "--task", "Fix validation bug in sample API",
            "--mock", "--profile", "feature",
            "--mock-validate-plan-reject", "1",
            "--max-rounds", "2",
            "--run-id", run_id,
        ],
        check=True, capture_output=True, text=True,
    )
    workspace = Path(__import__("os").environ["ORCHO_WORKSPACE"])
    return workspace / "runspace" / "runs" / run_id, proc


def test_golden_scenario_full_loop(
    golden_project: Path, golden_workspace: Path,
) -> None:
    """REA-0 acceptance baseline: feature profile + mock plan-QA reject.

    Asserts the canonical timeline (every feature-profile phase fires),
    terminal status is ``done``, and the REA-0 evidence placeholder is
    written under the run dir. This is the contract every future REA
    milestone must preserve.
    """
    run_dir, _proc = _run_golden(golden_project)

    # 1. Canonical run dir artifacts present.
    for name in ("meta.json", "events.jsonl", "metrics.json", "progress.log"):
        assert (run_dir / name).is_file(), f"missing {name} in {run_dir}"

    # 2. Terminal session status.
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["status"] == "done", f"unexpected status: {meta['status']!r}"

    # 3. Every advanced-profile phase fired.
    events = _read_events(run_dir)
    assert events, "events.jsonl empty"
    fired_phase_kinds = {
        e["payload"].get("phase_kind")
        for e in events
        if e.get("kind") == "phase.start" and e.get("payload", {}).get("phase_kind")
    }
    missing = _expected_phase_kinds() - fired_phase_kinds
    assert not missing, (
        f"feature profile did not fire phases: {sorted(missing)}; "
        f"observed: {sorted(fired_phase_kinds)}"
    )

    # 4. validate_plan reject + replan loop landed (mock-validate-plan-reject=1 forces
    #    one rejected attempt followed by an approval).
    validate_plan_verdicts = [
        e for e in events if e.get("kind") == "validate_plan.verdict"
    ]
    assert len(validate_plan_verdicts) == 2, (
        f"expected 2 validate_plan verdicts (reject + approve), got "
        f"{len(validate_plan_verdicts)}"
    )
    assert validate_plan_verdicts[0]["payload"]["approved"] is False
    assert validate_plan_verdicts[1]["payload"]["approved"] is True

    # 5. Event seq is strictly monotonic (event spine integrity).
    seqs = [e.get("seq") for e in events if e.get("seq") is not None]
    assert seqs == sorted(seqs), "event seq is not monotonic"
    assert seqs == list(range(seqs[0], seqs[-1] + 1)), (
        f"event seq has gaps: {seqs}"
    )

    # 6. run.start opens the timeline; run.end appears with status=done.
    # ``run.end`` is no longer guaranteed to be the *last* event because
    # post-finalize artifact writes (REA-0 evidence placeholder) emit
    # ``artifact.created`` after ``run.end`` is recorded.
    assert events[0]["kind"] == "run.start"
    run_end = next(
        (e for e in reversed(events) if e["kind"] == "run.end"), None,
    )
    assert run_end is not None, "run.end event missing"
    assert run_end["payload"]["status"] == "done"


def test_golden_scenario_event_spine_carries_rea2_kinds(
    golden_project: Path, golden_workspace: Path,
) -> None:
    """REA-2: every event family the golden scenario can produce fires.

    Asserts the canonical timeline (already covered by
    ``test_golden_scenario_full_loop``) plus the REA-2 vocabulary
    additions: ``plan.parsed`` after PLAN, ``artifact.created`` for
    plan_*.md and evidence.json. ``gate.*`` and ``command.*`` are
    profile-dependent (the feature profile's tests gate); not
    required to fire here, but each must satisfy its schema if it
    does (covered separately by ``test_event_kinds``).
    """
    run_dir, _proc = _run_golden(golden_project)
    events = _read_events(run_dir)
    by_kind: dict[str, list[dict]] = {}
    for e in events:
        by_kind.setdefault(e.get("kind", ""), []).append(e)

    # plan.parsed fires after the PLAN handler runs parse_plan().
    plan_parsed = by_kind.get("plan.parsed", [])
    assert plan_parsed, (
        "plan.parsed event missing — REA-2 wiring broken; "
        f"observed kinds: {sorted(by_kind)}"
    )
    payload = plan_parsed[-1]["payload"]
    assert payload["source"] in ("json", "markdown")
    assert payload["subtask_count"] >= 1
    assert payload["has_contract"] is True
    assert payload["acceptance_criteria_count"] >= 1

    # Mock plan_*.md write fires artifact.created (per attempt).
    plan_artifacts = [
        e for e in by_kind.get("artifact.created", [])
        if e["payload"].get("artifact_kind") == "plan"
    ]
    assert plan_artifacts, "no plan-artifact.created events"
    for evt in plan_artifacts:
        assert evt["payload"]["path"].endswith(".md")
        assert evt["payload"]["size_bytes"] >= 1

    # evidence.json placeholder write also fires artifact.created.
    evidence_artifacts = [
        e for e in by_kind.get("artifact.created", [])
        if e["payload"].get("artifact_kind") == "evidence"
    ]
    assert evidence_artifacts, "no evidence-artifact.created event"
    assert evidence_artifacts[-1]["payload"]["path"].endswith("evidence.json")


def test_golden_scenario_writes_full_evidence_bundle(
    golden_project: Path, golden_workspace: Path,
) -> None:
    """REA-3: a finished golden run produces a v1 evidence bundle.

    Asserts the bundle:
      * carries ``schema_version="1"``;
      * embeds the architect's plan-contract surface (goal +
        acceptance_criteria + commands_to_run);
      * folds the phase / gate / artifact rollups derived from the
        event spine;
      * passes its own ``validate_bundle`` contract;
      * ships an ``evidence.md`` sidecar.
    """
    from pipeline.evidence import (
        EVIDENCE_FILE_NAME,
        EVIDENCE_MD_FILE_NAME,
        EVIDENCE_SCHEMA_VERSION,
        validate_bundle,
    )

    run_dir, _proc = _run_golden(golden_project)

    json_path = run_dir / EVIDENCE_FILE_NAME
    md_path = run_dir / EVIDENCE_MD_FILE_NAME
    assert json_path.is_file(), f"evidence.json missing in {run_dir}"
    assert md_path.is_file(), f"evidence.md missing in {run_dir}"

    bundle = json.loads(json_path.read_text())
    validate_bundle(bundle)
    assert bundle["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert bundle["status"] == "done"
    assert bundle["task"] == "Fix validation bug in sample API"
    assert bundle["profile"] == "feature"

    plan = bundle["plan"]
    assert plan["has_contract"] is True
    assert plan["goal"], "plan goal missing"
    assert plan["subtask_count"] >= 1
    assert plan["acceptance_criteria"], "plan acceptance_criteria empty"

    # Phase names in events are "PHASE N" / "DONE"; the human-readable
    # phase identity lives in ``title`` (e.g. "PLAN -- Claude Code …").
    phase_titles = " | ".join(p["title"] for p in bundle["phases"])
    assert "PLAN" in phase_titles
    assert "IMPLEMENT" in phase_titles
    assert "FINAL ACCEPTANCE" in phase_titles

    artifact_kinds = {a["kind"] for a in bundle["artifacts"]}
    assert "plan" in artifact_kinds
    # NB: ``evidence`` artifacts emitted *by* write_bundle land in
    # events.jsonl after collect_evidence has snapshotted it, so the
    # bundle deliberately does not list itself.

    md = md_path.read_text()
    assert "# Run evidence" in md
    assert "## Plan" in md
    assert "## Phase timeline" in md


def test_golden_scenario_evidence_carries_baseline_fields(
    golden_project: Path, golden_workspace: Path,
) -> None:
    """REA-0 → REA-3: ``evidence.json`` always carries the run-id /
    status / created_at baseline contract regardless of whether the
    bundle is the v1 composition or the placeholder fallback.

    The placeholder slot is now exercised only by the fallback path
    (``write_bundle_or_placeholder``); a finished golden run produces
    the full v1 bundle.
    """
    run_dir, _proc = _run_golden(golden_project)

    evidence_path = run_dir / EVIDENCE_FILE_NAME
    assert evidence_path.is_file(), f"evidence.json missing at {evidence_path}"

    payload = json.loads(evidence_path.read_text())
    # Either flavour must carry these baseline fields:
    assert payload["run_id"] == "GOLDEN_REA0"
    assert payload["status"] == "done"
    assert payload["run_dir"]                # absolute path string
    assert "T" in payload["created_at"]      # ISO-8601 timestamp
    # In the happy-path golden run the v1 composition succeeds.
    assert payload["schema_version"] in {"1", EVIDENCE_SCHEMA_VERSION_PLACEHOLDER}


def test_golden_scenario_typed_plan_contract_propagates_to_phase_prompts(
    golden_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REA-1: typed plan views reach BUILD / REVIEW / FIX / FINAL_ACCEPTANCE prompts.

    Uses an in-process pipeline run with the canonical mock provider
    plus pytest monkeypatch spies on ``_MockClaude.run`` and
    ``_MockCodex.review_uncommitted`` / ``review_file``. Each spy
    captures the prompt the agent received; after the run we assert
    every captured prompt for non-PLAN phases carries both rendered
    typed plan views: the ``## Plan Contract`` block and the ``## Tasks``
    decomposition with at least one architect-emitted subtask id.

    This test is the live propagation check: a regression that
    silently drops the contract or task decomposition from any prompt fails here,
    independent of whether the JSON schema validation still runs
    elsewhere.
    """
    from agents.runtimes._strategy import (
        MockAgentProvider,
        _MockClaude,
        _MockCodex,
    )
    from pipeline.plugins import PluginConfig
    from pipeline.project_orchestrator import run_pipeline

    captured: list[tuple[str, str]] = []   # (label, prompt)

    real_claude_invoke = _MockClaude.invoke
    real_codex_invoke = _MockCodex.invoke

    def spy_claude_invoke(self, prompt, cwd="", **kw):
        captured.append(("claude.invoke", prompt))
        return real_claude_invoke(self, prompt, cwd, **kw)

    def spy_codex_invoke(self, prompt, cwd="", **kw):
        captured.append(("codex.invoke", prompt))
        return real_codex_invoke(self, prompt, cwd, **kw)

    monkeypatch.setattr(_MockClaude, "invoke", spy_claude_invoke)
    monkeypatch.setattr(_MockCodex, "invoke", spy_codex_invoke)

    monkeypatch.setattr(
        "pipeline.project.session_run.load_plugin",
        lambda *_a, **_kw: PluginConfig(name="golden-api", language="Python"),
    )
    monkeypatch.setattr(
        "core.io.git_helpers.has_uncommitted",
        lambda *_a, **_kw: True,
    )
    monkeypatch.setattr(
        "core.io.git_helpers.git_diff_stat",
        lambda *_a, **_kw: "1 file changed",
    )

    out_dir = golden_project.parent / "rea1_run"
    out_dir.mkdir()
    monkeypatch.chdir(golden_project)

    run_pipeline(
        task="Fix validation bug in sample API",
        project_dir=str(golden_project),
        output_dir=out_dir,
        provider=MockAgentProvider(latency=0.0),
        max_rounds=1,
        profile_name="feature",
    )

    # PLAN is the first claude.invoke (architect emits the plan markdown).
    # Every claude.invoke after that is implement or repair_changes and
    # must carry the rendered typed plan views. Codex invokes after
    # the first claude.invoke are validate_plan / review_changes /
    # final_acceptance and must also carry the same views.
    contract_signature = "## Plan Contract"
    contract_value = "Deliver a focused, verified change for"  # mock's goal
    tasks_signature = "## Tasks"
    task_value = "Task inspect-target"  # mock's first subtask id

    # PLAN is the first claude.invoke that isn't the hypothesis-generation
    # step (which fires before PLAN and has its own distinctive prompt).
    plan_idx = next(
        (
            i for i, (label, prompt) in enumerate(captured)
            if label == "claude.invoke"
            and "Produce a SHORT hypothesis" not in prompt
        ),
        None,
    )
    assert plan_idx is not None, "no PLAN claude.invoke captured"

    post_plan = captured[plan_idx + 1 :]
    # Strip out hypothesis-validation codex invokes — they also fire before
    # PLAN-time and don't carry the Plan Contract.
    post_plan_codex = [
        p for label, p in post_plan
        if label == "codex.invoke" and "hypothesis_" not in p
    ]
    post_plan_claude = [p for label, p in post_plan if label == "claude.invoke"]

    assert post_plan_codex, "no post-PLAN codex invoke prompts captured"
    assert post_plan_claude, "no post-PLAN claude.invoke (BUILD/FIX) prompts captured"

    for i, prompt in enumerate(post_plan_codex):
        assert contract_signature in prompt, (
            f"post-PLAN codex prompt #{i} missing Plan Contract:\n{prompt[:400]}"
        )
        assert contract_value in prompt, (
            f"post-PLAN codex prompt #{i} missing architect-emitted goal"
        )
        assert tasks_signature in prompt, (
            f"post-PLAN codex prompt #{i} missing Plan Tasks:\n{prompt[:400]}"
        )
        assert task_value in prompt, (
            f"post-PLAN codex prompt #{i} missing architect-emitted subtask"
        )

    # P2: the BUILD (developer) phase runs subtask_dag, so claude.invoke
    # prompts are per-subtask. The first subtask carries the Plan Contract as
    # background to open the implementation context; follow-up subtasks carry
    # only a compact reference so the plan-wide acceptance/risk text does not
    # keep blurring the current executable scope. The full ``## Tasks``
    # decomposition is intentionally replaced by the compact
    # ``## Execution Plan Context`` DAG map (id / goal / depends_on only — no
    # sibling specs). The architect-emitted subtask id still appears, now as a
    # map line rather than a ``## Task`` heading.
    map_signature = "## Execution Plan Context"
    subtask_id_value = "inspect-target"  # mock's first subtask id
    first_claude, *followup_claude = post_plan_claude
    assert contract_signature in first_claude, (
        f"first post-PLAN claude.invoke missing Plan Contract:\n"
        f"{first_claude[:400]}"
    )
    assert contract_value in first_claude, (
        "first post-PLAN claude.invoke missing architect-emitted goal"
    )
    for i, prompt in enumerate(followup_claude, start=1):
        assert contract_signature not in prompt, (
            f"follow-up post-PLAN claude.invoke #{i} repeated full Plan "
            f"Contract:\n{prompt[:400]}"
        )
        assert contract_value not in prompt, (
            f"follow-up post-PLAN claude.invoke #{i} repeated "
            "architect-emitted goal"
        )
        assert "full Plan Contract was sent with the first subtask prompt" in prompt, (
            f"follow-up post-PLAN claude.invoke #{i} missing compact Plan "
            f"Contract reference:\n{prompt[:400]}"
        )
    for i, prompt in enumerate(post_plan_claude):
        assert map_signature in prompt, (
            f"post-PLAN claude.invoke #{i} missing Execution Plan Context "
            f"map:\n{prompt[:400]}"
        )
        assert subtask_id_value in prompt, (
            f"post-PLAN claude.invoke #{i} missing architect-emitted subtask id"
        )
        # The full sibling decomposition must NOT leak into a subtask prompt.
        assert tasks_signature not in prompt, (
            f"post-PLAN claude.invoke #{i} should carry the compact map, not "
            f"the full ## Tasks decomposition:\n{prompt[:400]}"
        )


def test_golden_scenario_malformed_contract_halts_active_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REA-1: malformed structured plan halts the active pipeline before BUILD.

    Replaces the mock architect's planning output with one that carries
    a JSON fence whose ``acceptance_criteria`` is the wrong type. The
    PLAN handler must run :func:`parse_plan`, detect the
    :class:`PlanSchemaError`, call ``state.stop()``, and skip BUILD.
    BUILD's spy assertion proves the halt landed before any developer
    invocation.
    """
    from agents.runtimes._strategy import (
        MockAgentProvider,
        _MockClaude,
    )
    from pipeline.plugins import PluginConfig
    from pipeline.project_orchestrator import run_pipeline

    bad_plan = (
        "# Plan\n\n"
        "```json\n"
        '{\n'
        '  "short_summary": "bad plan",\n'
        '  "planning_context": "bad plan",\n'
        '  "tasks": [{"id": "t1", "goal": "y"}],\n'
        '  "acceptance_criteria": "should be a list"\n'
        '}\n'
        "```\n"
    )

    # Active PLAN handler calls the architect's ``invoke()`` to produce
    # plan markdown. Patch ``invoke`` so the PLAN call returns the
    # malformed plan; hypothesis-generation invokes (which run before
    # PLAN) keep their normal hypothesize output. Track non-hypothesis
    # invokes so the test can assert BUILD / FIX never fired.
    invocations: list[str] = []
    real_invoke = _MockClaude.invoke

    def spy_invoke(self, prompt, cwd="", **kw):
        if "Produce a SHORT hypothesis" in prompt:
            return real_invoke(self, prompt, cwd, **kw)
        invocations.append(prompt)
        return bad_plan if len(invocations) == 1 else "should-not-run"

    monkeypatch.setattr(_MockClaude, "invoke", spy_invoke)
    monkeypatch.setattr(
        "pipeline.project.session_run.load_plugin",
        lambda *_a, **_kw: PluginConfig(name="x"),
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

    session = run_pipeline(
        task="should halt",
        project_dir=str(project),
        output_dir=out_dir,
        provider=MockAgentProvider(latency=0.0),
        max_rounds=1,
        profile_name="feature",
    )

    assert session["status"] == "halted"
    assert "acceptance_criteria" in session.get("halt", {}).get("reason", "")

    # Exactly one architect call (PLAN) — BUILD/FIX must NOT have been
    # invoked. The PLAN handler stopped the run when parse_plan()
    # rejected the malformed contract.
    assert len(invocations) == 1, (
        f"expected exactly one architect call (PLAN), got "
        f"{len(invocations)}: {[p[:80] for p in invocations]!r}"
    )
    plan_log = session.get("phases", {}).get("plan")
    plan_entries = plan_log if isinstance(plan_log, list) else [plan_log]
    assert any(
        isinstance(e, dict) and "acceptance_criteria" in (e.get("parse_error") or "")
        for e in plan_entries
    ), f"plan log missing parse_error breadcrumb: {plan_log!r}"
    metrics = session.get("metrics", {})
    assert "plan" in metrics.get("phases", {}), (
        "halted PLAN consumed an agent call and must still be recorded "
        f"in metrics: {metrics!r}"
    )
    assert metrics.get("total_tokens", 0) > 0

    events = [
        json.loads(line)
        for line in (out_dir / "events.jsonl").read_text().splitlines()
    ]
    run_end = [e for e in events if e["kind"] == "run.end"][-1]
    assert run_end["payload"]["status"] == "halted"
    assert "acceptance_criteria" in run_end["payload"]["halt_reason"]
    plan_end = [
        e for e in events
        if e["kind"] == "phase.end" and e.get("phase") == "PLAN"
    ][-1]
    assert plan_end["payload"]["outcome"].startswith("halted:")

    evidence = json.loads((out_dir / "evidence.json").read_text())
    assert evidence["status"] == "halted"
    assert any(
        e["kind"] == "run_halted" and "acceptance_criteria" in e["message"]
        for e in evidence["errors"]
    )


def test_golden_scenario_malformed_contract_rejected_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REA-1: parse_plan rejects malformed structured plans synchronously.

    Direct ``parse_plan()`` test — complements the active-pipeline
    halt assertion above. Together they prove a malformed contract
    fails both at the parser layer and at the orchestrator layer.
    """
    from core.contracts.plan_schema import PlanSchemaError
    from pipeline.plan_parser import parse_plan

    bad_plan = (
        '```json\n'
        '{\n'
        '  "short_summary": "x",\n'
        '  "planning_context": "x",\n'
        '  "tasks": [{"id": "t1", "goal": "y"}],\n'
        '  "acceptance_criteria": "should be a list"\n'
        '}\n'
        '```\n'
    )
    with pytest.raises(PlanSchemaError, match="acceptance_criteria"):
        parse_plan(bad_plan)
