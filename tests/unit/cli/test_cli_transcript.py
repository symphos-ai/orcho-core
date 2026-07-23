"""pin the CLI run-transcript shape.

Pure presentation tests. No subprocess, no agent, no filesystem. Each
test asserts that a specific renderer in :mod:`core.io.transcript`
preserves the data it was given and lays it out in the agreed shape —
no hidden truncation, no information loss.
"""
from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from core.io.ansi import (
    get_color_enabled,
    set_color_enabled,
    strip_ansi,
)
from core.io.transcript import (
    THIN_RULE,
    C,
    detect_registered_skill_use,
    format_thinking_text,
    render_agent_command,
    render_agent_invocation,
    render_cross_plan_block,
    render_cross_run_header,
    render_implement_summary,
    render_incoming_prompt,
    render_parse_failure,
    render_phase_header,
    render_plan_block,
    render_result,
    render_review_block,
    render_run_header,
    render_transcript_close,
    render_transcript_open,
)
from core.io.verification_header import GateRowView, VerificationHeaderView
from pipeline.prompts.turn import PromptSegment, PromptTraceView
from pipeline.prompts.types import PromptPart


def _make_trace_view(
    parts: tuple,
    text: str | None = None,
) -> PromptTraceView:
    """Build a PromptTraceView from an ordered sequence of PromptParts.

    Mirrors what PromptTurn.trace_view() returns: segments carry the
    body text with standard "\\n\\n" separator glue for non-first parts.
    """
    segs = []
    for idx, p in enumerate(parts):
        seg_text = p.body if idx == 0 else "\n\n" + p.body
        segs.append(
            PromptSegment(text=seg_text, part=p, segment_id=f"{p.kind}:{p.name}:{idx}")
        )
    if text is None:
        text = "\n\n".join(p.body for p in parts)
    return PromptTraceView(text=text, segments=tuple(segs))


def _strip(s: str) -> str:
    """Drop ANSI codes so assertions test layout, not color escapes."""
    return strip_ansi(s)


@pytest.fixture(autouse=True)
def _restore_color_override() -> Iterator[None]:
    """Color decision is module-level state; isolate every test."""
    before = get_color_enabled()
    try:
        yield
    finally:
        set_color_enabled(before)


@pytest.fixture(autouse=True)
def _reset_phase_continuity() -> Iterator[None]:
    """Phase-header continuity is module-level + phase-context state.

    Isolate every test so a header rendered in one test can't bleed a
    synthesised section title into an unrelated ``render_agent_invocation``
    assertion in another.
    """
    from core.io.transcript import reset_phase_header_continuity
    from core.observability import events
    from core.observability.logging import get_verbose, set_verbose

    verbose_before = get_verbose()
    reset_phase_header_continuity()
    events.clear_phase_context()
    try:
        yield
    finally:
        reset_phase_header_continuity()
        events.clear_phase_context()
        set_verbose(verbose_before)


@pytest.fixture(autouse=True)
def _live_transcript_mode() -> Iterator[None]:
    """Pin the full-fidelity (live) transcript shape for this file.

    The default run-output mode is ``summary`` — the compact append-only
    arc. Every renderer pinned here (phase header, review/plan/implement
    blocks, synthesised invocation titles) is the live/debug form, so
    force ``live`` for the whole module and restore afterwards. The
    summary grammar is pinned separately by the acceptance goldens.
    """
    from core.observability import logging as _logging

    before = _logging.get_output_mode()
    _logging._output_mode = "live"
    try:
        yield
    finally:
        _logging._output_mode = before


@pytest.fixture
def force_color() -> Iterator[None]:
    """Opt-in: force-enable color for tests that pin raw ANSI codes."""
    set_color_enabled(True)
    yield


def test_format_thinking_text_matches_tooling_transcript_style() -> None:
    out = format_thinking_text("First thought.\nSecond thought.\n")
    assert out == "  💬 Assistant: First thought.\n     Second thought.\n"


def test_format_thinking_text_promotes_registered_skill_use() -> None:
    out = format_thinking_text(
        "Использую `frontend-qa`, потому что задача затрагивает UI.\n"
        "Проверю форму.",
        skill_names={"frontend-qa"},
    )
    assert out == (
        "  🧠 Skill: frontend-qa — Использую `frontend-qa`, потому что "
        "задача затрагивает UI.\n"
        "     Проверю форму.\n"
    )


def test_detect_registered_skill_use_ignores_unregistered_names() -> None:
    assert (
        detect_registered_skill_use(
            "Использую `frontend-qa`, потому что задача затрагивает UI.",
            skill_names={"backend-qa"},
        )
        is None
    )


# ── Run header ─────────────────────────────────────────────────────────


def test_run_header_contains_all_model_and_effort_fields() -> None:
    out = _strip(render_run_header(
        run_id="REAL_ADV_2",
        project="/tmp/proj",
        task="Fix bug in calc.add",
        agents=[
            {"role": "PLAN",   "model": "claude-opus-4-7",   "effort": "high"},
            {"role": "BUILD",  "model": "claude-sonnet-4-6", "effort": "medium"},
            {"role": "REVIEW", "model": "gpt-5.4",           "effort": "medium"},
        ],
        profile="advanced",
        session_mode="auto",
        rounds=1,
        plan=True,
        output_log="/runs/REAL_ADV_2/output.log",
        events_log="/runs/REAL_ADV_2/events.jsonl",
        resumed=True,
    ))
    # Title carries run_id, profile, resumed marker.
    assert "Orcho Run" in out
    assert "REAL_ADV_2" in out
    assert "advanced" in out
    assert "resumed" in out
    # Identity and task preserved.
    assert "/tmp/proj" in out
    assert "Fix bug in calc.add" in out
    # Every model + effort appears.
    for model in ("claude-opus-4-7", "claude-sonnet-4-6", "gpt-5.4"):
        assert model in out
    for effort in ("high", "medium"):
        assert effort in out
    # State fields.
    assert "session" in out and "auto" in out
    assert "rounds=1" in out
    assert "plan=yes" in out
    # Log paths preserved.
    assert "/runs/REAL_ADV_2/output.log" in out
    assert "/runs/REAL_ADV_2/events.jsonl" in out


def test_run_header_surfaces_discovered_skills_line() -> None:
    out = _strip(render_run_header(
        run_id="R1",
        project="/tmp/proj",
        task="Split work",
        agents=[],
        profile="advanced",
        session_mode="auto",
        rounds=1,
        plan=True,
        plugin_line="Project",
        skills_line="2: quant-analytics-atas, quant-analytics-theory",
    ))

    assert "Plugin" in out
    assert "Project" in out
    assert "Skills" in out
    assert "2: quant-analytics-atas, quant-analytics-theory" in out


def test_run_header_surfaces_verification_block_when_present() -> None:
    view = VerificationHeaderView(
        mode="governed",
        envs=("ci",),
        gates=(
            GateRowView(
                gate="lint",
                timing="after_implement",
                run_mode="auto",
                policy="require",
                kind="cheap",
                when="after_implement",
            ),
            GateRowView(
                gate="test",
                timing="delivery",
                run_mode="auto",
                policy="warn",
                kind="unknown",
                when="pre-final",
            ),
        ),
        policy_source="auto-derived from mode/plugin defaults",
        effect="warn on missing/failed receipts",
        warned=True,
    )
    out = _strip(render_run_header(
        run_id="R1",
        project="/tmp/proj",
        task="Split work",
        agents=[],
        profile="advanced",
        session_mode="auto",
        rounds=1,
        plan=True,
        verification=view,
    ))

    assert "Verification" in out
    # Operator-facing dimensions are visually distinct, not a comma soup.
    assert "governed" in out
    # The gate matrix surfaces command identity and the orthogonal columns.
    assert "lint" in out
    assert "test" in out
    assert "after_implement" in out
    assert "auto-derived from mode/plugin defaults" in out
    assert "warn on missing/failed receipts" in out
    # The legacy dense one-liner and raw schedule jargon are gone.
    assert "Verification contract: work_mode=" not in out
    assert "schedule" not in out


def test_run_header_omits_verification_block_without_contract() -> None:
    out = _strip(render_run_header(
        run_id="R1",
        project="/tmp/proj",
        task="Split work",
        agents=[],
        profile="advanced",
        session_mode="auto",
        rounds=1,
        plan=True,
    ))

    assert "Verification" not in out


def test_cross_run_header_lists_projects_and_agents() -> None:
    out = _strip(render_cross_run_header(
        run_id="20260513_184335",
        task="Fix 500 error bug in user creation form",
        projects={"api": "/tmp/api", "web": "/tmp/web"},
        agents=[
            {"role": "CROSS_HYPOTHESIS", "model": "claude-opus-4-7", "effort": "high"},
            {"role": "CROSS_PLAN",       "model": "claude-opus-4-7", "effort": "high"},
            {"role": "CONTRACT_CHECK",   "model": "gpt-5.4",         "effort": "medium"},
        ],
        project_agents=[
            {"role": "IMPLEMENT",      "model": "claude-sonnet-4-6", "effort": "medium"},
            {"role": "REVIEW_CHANGES", "model": "gpt-5.4",          "effort": "medium"},
        ],
        cross_mode="full",
        rounds=2,
        output_log="/runs/x/output.log",
        events_log="/runs/x/events.jsonl",
    ))
    # Title block.
    assert "Orcho Cross-Run" in out
    assert "20260513_184335" in out
    assert "mode=full" in out
    # Task + project map.
    assert "Fix 500 error bug" in out
    assert "[api]" in out and "/tmp/api" in out
    assert "[web]" in out and "/tmp/web" in out
    assert "2 aliases" in out
    # Agents legends split cross-level phases from projected per-project phases.
    assert "Cross Agents" in out
    for role in ("CROSS_HYPOTHESIS", "CROSS_PLAN", "CONTRACT_CHECK"):
        assert role in out
    assert "Project Agents" in out
    assert "IMPLEMENT" in out
    assert "REVIEW_CHANGES" in out
    project_section = out.split("Project Agents", 1)[1].split("State", 1)[0]
    assert "CROSS_PLAN" not in project_section
    assert "claude-opus-4-7" in out
    assert "gpt-5.4" in out
    assert "claude-sonnet-4-6" in out
    assert "high" in out and "medium" in out
    # State + log paths preserved.
    assert "rounds_per_project=2" in out
    assert "/runs/x/events.jsonl" in out


def test_cross_run_header_surfaces_profile_projection() -> None:
    """Cross runs surface the requested profile, plan source, and the
    global / per-project projection up-front so an operator can see at a
    glance which work runs at the cross level versus inside each child.
    """
    out = _strip(render_cross_run_header(
        run_id="20260513_193935",
        task="Fix bug",
        projects={"api": "/tmp/api"},
        agents=[{"role": "CROSS_PLAN", "model": "claude-opus-4-7", "effort": "high"}],
        cross_mode="full",
        rounds=2,
        profile="advanced",
        plan_source="cross",
        projection="global + per-project",
    ))
    assert "Profile" in out
    assert "advanced" in out
    assert "Plan source" in out
    assert "cross" in out
    assert "Projection" in out
    assert "global + per-project" in out


def test_agent_invocation_surfaces_prompt_legend() -> None:
    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="role", name="systems_architect",
                source="core", body="ROLE",
            ),
            PromptPart(
                kind="task", name="cross_plan",
                source="core", body="TASK",
            ),
            PromptPart(
                kind="format", name="detailed",
                source="core", body="FORMAT",
            ),
        ),
        text="PROMPT",
    )

    out = _strip(render_agent_invocation(
        runtime="claude",
        model="claude-opus-4-7",
        effort="high",
        prompt="PROMPT",
        trace_view=trace_view,
        mode="read",
        cwd="/tmp/work",
    ))

    assert "runtime=claude" in out
    assert "role=systems_architect" in out
    assert "task=cross_plan" in out
    assert "format=detailed" in out
    assert "mode=read" in out


def test_agent_invocation_ignores_stale_prompt_legend() -> None:
    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="role", name="systems_architect",
                source="core", body="ROLE",
            ),
        ),
        text="OLDER PROMPT",
    )

    out = _strip(render_agent_invocation(
        runtime="claude",
        model="claude-opus-4-7",
        prompt="CURRENT PROMPT",
        trace_view=trace_view,
        mode="read",
    ))

    assert "role=systems_architect" not in out


def test_agent_invocation_marks_fresh_session_when_no_resume_id() -> None:
    out = _strip(render_agent_invocation(
        runtime="claude",
        model="claude-opus-4-7",
        mode="read",
        continue_session=False,
    ))

    assert "session=fresh" in out
    assert "session=stateless" not in out


def test_agent_invocation_marks_resume_session_with_full_id() -> None:
    out = _strip(render_agent_invocation(
        runtime="claude",
        model="claude-opus-4-7",
        mode="write",
        session_id="5ec5ee6b-295e-4bc7-9a93-66e88a31d043",
        continue_session=True,
    ))

    assert "session=resume:5ec5ee6b-295e-4bc7-9a93-66e88a31d043" in out


def test_agent_invocation_keeps_stateless_for_unknown_session_intent() -> None:
    out = _strip(render_agent_invocation(
        runtime="claude",
        model="claude-opus-4-7",
        mode="read",
    ))

    assert "session=stateless" in out


def test_run_header_surfaces_followup_parent() -> None:
    """Follow-up runs must visibly carry their parent linkage so the
    reader can tell at a glance the run isn't a fresh start."""
    out = _strip(render_run_header(
        run_id="20260518_173555",
        project="/tmp/proj",
        task="awesome quality",
        agents=[{"role": "PLAN", "model": "mock", "effort": "high"}],
        profile="lite",
        session_mode="stateless",
        rounds=1,
        plan=True,
        followup_parent_run_id="20260518_173520",
        followup_base_task="first task",
    ))
    # Title carries the follow-up chip with the parent run id.
    assert "follow-up of 20260518_173520" in out
    # Dedicated rows in the header body.
    assert "Follow-up of" in out
    assert "Base task" in out
    assert "first task" in out
    # Fresh-run header for the same args (no follow-up kwargs) must NOT
    # carry any follow-up artefacts — sanity guard against leakage.
    fresh = _strip(render_run_header(
        run_id="20260518_173555",
        project="/tmp/proj",
        task="awesome quality",
        agents=[{"role": "PLAN", "model": "mock", "effort": "high"}],
        profile="lite",
        session_mode="stateless",
        rounds=1,
        plan=True,
    ))
    assert "follow-up" not in fresh.lower()
    assert "Base task" not in fresh


def test_cross_run_header_surfaces_followup_parent() -> None:
    out = _strip(render_cross_run_header(
        run_id="20260518_180000",
        task="cross follow-up",
        projects={"api": "/tmp/api"},
        agents=[{"role": "CROSS_PLAN", "model": "mock", "effort": "high"}],
        cross_mode="full",
        rounds=1,
        followup_parent_run_id="20260518_170000",
        followup_base_task="original cross task",
    ))
    assert "follow-up of 20260518_170000" in out
    assert "Follow-up of" in out
    assert "Base task" in out
    assert "original cross task" in out


def test_run_header_does_not_print_giant_banner() -> None:
    out = _strip(render_run_header(
        run_id=None, project="/p", task="t", agents=[], profile="lite",
        session_mode="stateless", rounds=0, plan=False,
    ))
    # Heavy ════ × 60+ banners are gone — only the thin rule (and shorter
    # variants) remain.
    assert "═" * 30 not in out
    assert "=" * 30 not in out


# ── Phase header ───────────────────────────────────────────────────────


def test_phase_header_is_thin_and_carries_status() -> None:
    out = _strip(render_phase_header("0.plan", "PLAN", status="running"))
    assert "[0.plan] PLAN" in out
    assert "running" in out
    # Thin rule, no heavy banner.
    assert THIN_RULE in out
    assert "═" * 30 not in out


def test_phase_header_status_optional() -> None:
    out = _strip(render_phase_header("4", "final_acceptance"))
    assert "[4] final_acceptance" in out
    # No accidental "None" leak.
    assert "None" not in out


def test_agent_invocation_synthesises_header_when_no_banner() -> None:
    """A runtime invocation that fires without an immediately-preceding
    banner must still carry its section title (the structural invariant)."""
    from core.observability import events

    events.set_phase_context(
        phase="IMPLEMENT", phase_key="implement", round=1,
        title="IMPLEMENT -- developer applies the change",
    )

    out = _strip(render_agent_invocation(
        runtime="claude", model="claude-opus-4-7", mode="write",
    ))

    assert "[IMPLEMENT] IMPLEMENT -- developer applies the change" in out
    assert "runtime=claude" in out


def test_agent_invocation_marks_synthesised_debug_header() -> None:
    """Under --output debug, a synthesized phase title is a debug frame,
    not a second phase-start banner."""
    from core.observability import events
    from core.observability.logging import set_verbose

    set_verbose(True)
    events.set_phase_context(
        phase="CROSS_PLAN", phase_key="cross_plan", round=1,
        title="CROSS-PLAN -- Round 1/2",
    )

    out = _strip(render_agent_invocation(
        runtime="claude", model="claude-opus-4-7", mode="read",
    ))

    assert "[CROSS_PLAN] [[Debug]] CROSS-PLAN -- Round 1/2" in out
    assert "runtime=claude" in out


def test_agent_invocation_does_not_duplicate_existing_banner() -> None:
    """When the orchestration already printed a banner for the phase, the
    invocation must NOT prepend a second header."""
    from core.observability import events

    events.set_phase_context(
        phase="VALIDATE_PLAN", phase_key="validate_plan", round=1,
        title="VALIDATE PLAN -- reviewer audits the plan (round 1)",
    )
    # Caller prints the banner itself (output ignored here).
    render_phase_header(
        "VALIDATE_PLAN",
        "VALIDATE PLAN -- reviewer audits the plan (round 1)",
        color=C.YELLOW,
    )

    out = _strip(render_agent_invocation(
        runtime="codex", model="gpt-5.5", mode="read",
    ))

    # The runtime line stands alone — no synthesised header, no rule.
    assert "[VALIDATE_PLAN]" not in out
    assert THIN_RULE not in out
    assert "runtime=codex" in out


def test_mark_phase_header_fresh_suppresses_synthesised_banner() -> None:
    """A caller that rendered its own per-step title through another renderer
    (e.g. the subtask_dag ``ORCHO subtask N/M START`` marker) marks the phase
    header fresh, so the following invocation does NOT stack a redundant
    ``[IMPLEMENT] … developer applies the change`` banner on top of it."""
    from core.io.transcript import mark_phase_header_fresh
    from core.observability import events

    events.set_phase_context(
        phase="IMPLEMENT", phase_key="implement", round=1,
        title="IMPLEMENT -- developer applies the change",
    )
    # Simulate the subtask START marker having just printed its own title.
    mark_phase_header_fresh()

    out = _strip(render_agent_invocation(
        runtime="claude", model="claude-opus-4-7", mode="write",
    ))

    # No synthesised phase banner — the subtask marker already titled the step.
    assert "[IMPLEMENT]" not in out
    assert "runtime=claude" in out

    # Freshness is one-shot: the NEXT invocation with no intervening title
    # synthesises again (loop phases without their own banner still get one).
    out2 = _strip(render_agent_invocation(
        runtime="claude", model="claude-opus-4-7", mode="write",
    ))
    assert "[IMPLEMENT] IMPLEMENT -- developer applies the change" in out2


def test_agent_invocation_titles_every_iteration_in_a_loop() -> None:
    """Per-iteration loops (e.g. cross contract-check over projects) emit
    one banner then invoke per project — every invocation after the first
    must get its own section title, not just the one under the banner."""
    from core.observability import events

    events.set_phase_context(
        phase="CONTRACT_CHECK", phase_key="contract_check", round=1,
        title="CONTRACT CHECK -- Codex reviews cross-project consistency",
    )
    # Banner printed once before the loop.
    render_phase_header(
        "CONTRACT_CHECK",
        "CONTRACT CHECK -- Codex reviews cross-project consistency",
        color=C.YELLOW,
    )

    first = _strip(render_agent_invocation(
        runtime="codex", model="gpt-5.5", mode="read", cwd="/wt/web",
    ))
    second = _strip(render_agent_invocation(
        runtime="codex", model="gpt-5.5", mode="read", cwd="/wt/api",
    ))

    # First invocation sits under the explicit banner — no duplicate.
    assert "[CONTRACT_CHECK]" not in first
    # Second invocation has no banner above it, so it synthesises one.
    assert "[CONTRACT_CHECK] CONTRACT CHECK -- Codex reviews cross-project consistency" in second


# ── Agent command + transcript ─────────────────────────────────────────


def test_agent_command_block_keeps_command_cwd_model_effort() -> None:
    out = _strip(render_agent_command(
        agent="claude",
        model="claude-opus-4-7",
        effort="high",
        cwd="/tmp/proj",
        command="claude --print --model claude-opus-4-7 --effort high",
    ))
    assert "Command" in out
    # Command line itself remains visible exactly once in the block.
    assert out.count("claude --print --model claude-opus-4-7 --effort high") == 1
    assert "agent claude" in out
    assert "model claude-opus-4-7" in out
    assert "effort high" in out
    assert "cwd /tmp/proj" in out


def test_incoming_prompt_block_lists_seam_manifest_and_frames_each_part() -> None:
    ordered_parts = (
        PromptPart(kind="system_tail", name="review_json",
                   source="code-owned", body="TAIL BODY", version=2),
        PromptPart(kind="role", name="systems_architect",
                   source="core", body="ROLE BODY"),
        PromptPart(kind="format", name="terse",
                   source="core", body="FORMAT BODY"),
        PromptPart(kind="task", name="hypothesis",
                   source="project", body="TASK BODY"),
    )
    trace_view = _make_trace_view(
        ordered_parts,
        text="TAIL BODY\n\nROLE BODY\n\nFORMAT BODY\n\nTASK BODY",
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))

    # Section opens, then the composition manifest with every seam, then
    # framed bodies per part in physical wire order, then a closing rule.
    assert "Incoming prompt" in out
    assert "Composition" in out
    assert "output_contract=review_json@v2(code-owned)" in out
    assert "role=systems_architect(core)" in out
    assert "format=terse(core)" in out
    assert "task=hypothesis(project)" in out

    assert "OUTPUT-CONTRACT: review_json v2 (code-owned)" in out
    assert "SYSTEM-TAIL" not in out
    assert "ROLE: systems_architect (core)" in out
    assert "FORMAT: terse (core)" in out
    assert "TASK: hypothesis (project)" in out

    # Each frame's body content is preserved verbatim.
    for body in ("ROLE BODY", "TASK BODY", "FORMAT BODY", "TAIL BODY"):
        assert body in out

    # Frames render top-down in the physical wire order, not in the
    # legacy upper/tail bucket order.
    assert out.find("TAIL BODY") < out.find("ROLE BODY") < out.find("FORMAT BODY")
    assert out.find("FORMAT BODY") < out.find("TASK BODY")


def test_incoming_prompt_labels_protected_blocks_by_role_not_legacy_tail() -> None:
    """M10.5 made protected blocks physical-prefix candidates.

    Debug output should describe their role in the prompt, not the
    historical ``system_tail`` bucket name.
    """
    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="system_tail", name="change_handoff",
                source="code-owned", body="POLICY", version=1,
            ),
            PromptPart(
                kind="system_tail", name="plan_artifact_boundary",
                source="code-owned", body="CONTRACT", version=1,
            ),
            PromptPart(
                kind="system_tail", name="plan_json",
                source="code-owned", body="OUTPUT", version=1,
            ),
        ),
        text="POLICY\n\nCONTRACT\n\nOUTPUT",
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))

    assert "policy=change_handoff@v1(code-owned)" in out
    assert "contract=plan_artifact_boundary@v1(code-owned)" in out
    assert "output_contract=plan_json@v1(code-owned)" in out
    assert "POLICY: change_handoff v1 (code-owned)" in out
    assert "CONTRACT: plan_artifact_boundary v1 (code-owned)" in out
    assert "OUTPUT-CONTRACT: plan_json v1 (code-owned)" in out
    assert "SYSTEM-TAIL" not in out


def test_incoming_prompt_labels_plan_views_without_surface_id_noise() -> None:
    """Typed plan view frame headings should read as domain objects.

    The part ids stay visible in the manifest for debugging/cache
    correlation, but the body frame should not imply that
    ``validate_plan`` is the content itself.
    """
    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="plan_contract", name="typed_plan",
                source="artifact", body="## Plan Contract\n\nGoal.",
            ),
            PromptPart(
                kind="plan_tasks", name="execution_plan",
                source="artifact", body="## Tasks\n\n## Task T1: Do it.",
            ),
        ),
        text="## Plan Contract\n\nGoal.\n\n## Tasks\n\n## Task T1: Do it.",
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))

    assert "plan_contract=typed_plan(artifact)" in out
    assert "plan_tasks=execution_plan(artifact)" in out
    assert "PLAN CONTRACT (artifact)" in out
    assert "PLAN TASKS (artifact)" in out
    assert "PLAN-CONTRACT: typed_plan" not in out
    assert "PLAN-TASKS: execution_plan" not in out


def test_incoming_prompt_manifest_includes_tokens_and_cache_scope() -> None:
    # M14.4.5 — each manifest entry carries the part's estimated
    # token count + its cache scope so a reader sees which parts
    # dominate the prompt at a glance, without reading bodies.
    from pipeline.prompts.types import PromptCacheScope, PromptStability
    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="role", name="systems_architect", source="core",
                body="X" * 1200,  # ≥ 1k so the abbreviated form kicks in
                stability=PromptStability.STATIC,
                cache_scope=PromptCacheScope.GLOBAL,
            ),
            PromptPart(
                kind="task", name="hypothesis", source="project",
                body="Y" * 50,  # raw token count
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="task framing",
            ),
        ),
        text="A\n\nB",
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))
    # Manifest now annotates each entry with tokens + cache scope.
    assert "role=systems_architect(core)" in out
    assert "cache=global" in out
    assert "task=hypothesis(project)" in out
    assert "cache=none" in out


def test_incoming_prompt_emits_totals_line_with_class_breakdown() -> None:
    # M14.4.5 — sibling totals line after the manifest sums tokens
    # across parts and surfaces the M14.2 OutputClass breakdown so a
    # reader can spot when one class dominates the prompt budget.
    from pipeline.prompts.types import PromptCacheScope, PromptStability
    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="role", name="systems_architect", source="core",
                body="A" * 4_000,  # ~ re_fetchable, dominant
                stability=PromptStability.STATIC,
                cache_scope=PromptCacheScope.GLOBAL,
            ),
            PromptPart(
                kind="turn_input", name="plan_task", source="code-owned",
                body="B" * 400,  # decision_bearing, smaller
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="task framing",
            ),
        ),
        text="role\n\ntask",
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))
    assert "Totals" in out
    # The totals block carries the total + sorted class breakdown.
    # We don't pin the exact token count (depends on estimator)
    # but assert structure + dominance ordering: re_fetchable
    # (4_000 chars) > decision_bearing (400 chars).
    lines = out.splitlines()
    totals_line = next(ln for ln in lines if "Totals" in ln)
    assert "tok total" in totals_line
    re_fetchable_line = next(ln for ln in lines if "re_fetchable" in ln)
    decision_line = next(ln for ln in lines if "decision_bearing" in ln)
    assert lines.index(re_fetchable_line) < lines.index(
        decision_line,
    )


def test_incoming_prompt_frame_heading_carries_metrics() -> None:
    # M14.4.5 — the per-part frame heading repeats the metrics so
    # a reader scrolling through bodies still sees them without
    # scrolling back to the manifest.
    from pipeline.prompts.types import PromptCacheScope, PromptStability
    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="role", name="systems_architect", source="core",
                body="X" * 2_000,
                stability=PromptStability.STATIC,
                cache_scope=PromptCacheScope.GLOBAL,
            ),
        ),
        text="body",
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))
    # Frame heading now carries `· N tok · cache=global · class=re_fetchable`.
    heading = next(
        ln for ln in out.splitlines() if "ROLE: systems_architect" in ln
    )
    assert "tok" in heading
    assert "cache=global" in heading
    assert "class=re_fetchable" in heading
    assert "source=" in heading


def test_artifact_path_in_frame_heading_only() -> None:
    # The on-disk artifact path lives on the PromptPart as
    # metadata-only ``artifact_path``. The debug transcript surfaces
    # it as a separate ``path=...`` segment in the frame heading so
    # operators can correlate a part with its file. The path MUST
    # NOT appear in the body lines (that would mean either the
    # builder leaked it back into the wire, or the renderer printed
    # it twice — the body is wire-identical to what the model saw).
    from pipeline.prompts.types import PromptCacheScope, PromptStability

    # Sentinel chosen to be unique and not collide with any other
    # token in the rendered output (heading metrics, body, etc.).
    sentinel = "/tmp/ZZSENTINELPATH_frame_42.md"
    body = "Plain artifact body without any file references."

    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="artifact",
                name="validate_plan",
                source="artifact",
                body=body,
                artifact_path=sentinel,
                stability=PromptStability.TURN,
                cache_scope=PromptCacheScope.NONE,
                volatile_reason="test fixture",
            ),
        ),
        text=body,
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))

    # Defense against the "removed File: prefix but added heading"
    # double-leak regression: sentinel appears exactly once.
    assert out.count(sentinel) == 1

    # Locate the frame heading line — it carries the ARTIFACT kind
    # label and the ``path=...`` segment. Body lines are prefixed
    # with the vertical bar (``│``) by the frame renderer.
    lines = out.splitlines()
    heading_lines = [ln for ln in lines if "ARTIFACT: validate_plan" in ln]
    assert len(heading_lines) == 1, (
        "expected exactly one ARTIFACT frame heading"
    )
    heading = heading_lines[0]

    # The sentinel must live in the heading, formatted as a separate
    # ``path=...`` metadata segment (not nested inside another field).
    assert f"path={sentinel}" in heading

    # The sentinel must NOT appear in any body line. Body lines are
    # those between the frame open and close — they get the ``│``
    # vertical bar prefix from ``_render_prompt_frame``.
    body_lines = [ln for ln in lines if "│" in ln]
    body_text = "\n".join(body_lines)
    assert sentinel not in body_text, (
        f"artifact_path sentinel leaked into body lines: {body_text!r}"
    )


def test_incoming_prompt_totals_omits_empty_class_buckets() -> None:
    # The totals line only shows non-zero classes. A single-class
    # prompt should print `total · <class>=<n>`, not pad with
    # zero buckets for the other three classes.
    trace_view = _make_trace_view(
        (
            PromptPart(
                kind="role", name="systems_architect", source="core",
                body="X" * 1000,
            ),
        ),
        text="body",
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))
    lines = out.splitlines()
    # Only re_fetchable should appear (role => RE_FETCHABLE).
    assert any("re_fetchable" in ln for ln in lines)
    assert not any("decision_bearing" in ln for ln in lines)
    assert not any("persisted_artifact" in ln for ln in lines)
    assert not any("ephemeral" in ln for ln in lines)


def test_incoming_prompt_block_falls_back_to_raw_body_when_composition_missing() -> None:
    out = _strip(render_incoming_prompt(
        "raw\nprompt\nbody",
        trace_view=None,
    ))
    assert "Incoming prompt" in out
    assert "Composition  (unavailable)" in out
    assert "BODY" in out  # generic frame heading
    for line in ("raw", "prompt", "body"):
        assert line in out


def test_incoming_prompt_block_preserves_multiline_part_bodies() -> None:
    trace_view = _make_trace_view(
        (
            PromptPart(kind="role", name="systems_architect",
                       source="core", body="line1\nline2\nline3"),
        ),
        text="line1\nline2\nline3",
    )
    out = _strip(render_incoming_prompt("ignored", trace_view=trace_view))
    for line in ("line1", "line2", "line3"):
        assert line in out


def test_incoming_prompt_frame_body_has_no_leading_separator_glue() -> None:
    """Non-first segments bake leading "\\n\\n" separator glue into seg.text
    (wire-layout invariant). The frame renderer must lift that glue so the
    body starts directly under the heading — not with spurious blank lines.
    """
    trace_view = _make_trace_view(
        (
            PromptPart(kind="system_tail", name="change_handoff",
                       source="code-owned", body="POLICY BODY", version=1),
            PromptPart(kind="system_tail", name="plan_artifact_boundary",
                       source="code-owned", body="CONTRACT BODY", version=1),
            PromptPart(kind="role", name="systems_architect",
                       source="core", body="ROLE BODY"),
        ),
    )
    lines = _strip(render_incoming_prompt("ignored", trace_view=trace_view)).splitlines()
    # For every frame, the line immediately after the "┌─" heading must be a
    # body line ("│ <content>"), never a blank "│" glue line.
    heading_idxs = [i for i, ln in enumerate(lines) if "┌─" in ln]
    assert len(heading_idxs) == 3
    for i in heading_idxs:
        first_body = lines[i + 1]
        assert first_body.strip() not in ("", "│"), (
            f"frame body starts with a blank glue line: {first_body!r}"
        )
    # Bodies still present and in wire order.
    out = "\n".join(lines)
    assert out.find("POLICY BODY") < out.find("CONTRACT BODY") < out.find("ROLE BODY")


def test_transcript_open_close_frame_is_thin() -> None:
    open_block = _strip(render_transcript_open())
    close = _strip(render_transcript_close())
    assert "Transcript" in open_block
    assert THIN_RULE in open_block
    assert THIN_RULE in close


# ── Result ─────────────────────────────────────────────────────────────


def test_result_carries_exit_and_duration() -> None:
    out = _strip(render_result(0, 14.9))
    assert "Result" in out
    assert "exit 0" in out
    assert "14.9s" in out


def test_result_marks_failure_path(force_color: None) -> None:
    out = render_result(1, 2.5)
    # Red color escape present on failure (presence is enough — we don't
    # pin the ANSI value to keep palette swaps cheap). ``force_color``
    # opt-in pins this even when pytest stdout is non-TTY: auto-detect
    # would otherwise return plain text under capture.
    assert "\x1b[" in out
    plain = _strip(out)
    assert "exit 1" in plain
    assert "2.5s" in plain


def test_result_extra_slot_preserves_text() -> None:
    out = _strip(render_result(0, 1.0, extra="REJECTED P1: rollback missing"))
    assert "REJECTED" in out
    assert "rollback missing" in out


# ── Reviewer contract ─────────────────────────────────────────────────


def test_review_block_renders_verdict_summary_findings() -> None:
    review = {
        "verdict": "REJECTED",
        "short_summary": "P1: destructive git command may discard changes.",
        "findings": [
            {
                "id": "F1",
                "severity": "P1",
                "title": "Destructive git command can discard user changes",
                "file": "agents/runtimes/claude.py",
                "line": 257,
                "body": "The runtime allows a streamed Bash command "
                        "to run git checkout -- test_calc.py, which "
                        "can erase user-owned working-tree changes.",
                "required_fix": "Block destructive git commands in "
                                "all stream paths.",
            }
        ],
        "risks": ["If the integration system expects a fresh diff…"],
        "checks": ["Reviewed runtime stream guard"],
    }
    out = _strip(render_review_block(review, title="Hypothesis QA"))

    assert "Hypothesis QA" in out
    assert "verdict" in out and "REJECTED" in out
    assert "P1: destructive git command may discard changes." in out
    # Finding details preserved in full — no hidden truncation.
    assert "F1" in out
    assert "Destructive git command can discard user changes" in out
    assert "agents/runtimes/claude.py:257" in out
    assert "erase user-owned working-tree changes" in out
    # The reviewer-output renderer emits the literal "fix" label
    # (a Markdown bold prefix above the required_fix body). This is
    # output-format vocabulary, NOT a phase ID — ADR 0022's phase
    # rename does not touch the in-rubric "fix" label.
    assert "fix" in out
    assert "Block destructive git commands" in out
    # Risks + checks lists preserved.
    assert "Risks" in out
    assert "If the integration system expects" in out
    assert "Checks" in out
    assert "Reviewed runtime stream guard" in out


def test_review_block_handles_approved_with_no_findings() -> None:
    review = {
        "verdict": "APPROVED",
        "short_summary": "All checks passed.",
        "findings": [],
    }
    out = _strip(render_review_block(review))
    assert "APPROVED" in out
    assert "All checks passed." in out
    # No phantom Findings header when the list is empty.
    assert "Findings" not in out


def test_review_block_surfaces_parse_error() -> None:
    review = {
        "verdict": "REJECTED",
        "short_summary": "P1: contract parse error.",
        "findings": [],
        "parse_error": "review must be a JSON object, got list",
    }
    out = _strip(render_review_block(review))
    assert "parse_error" in out
    assert "review must be a JSON object" in out


def test_review_block_renders_release_gate_extras() -> None:
    """final_acceptance writes release-shape fields alongside the
    review-shape mirror (ADR 0025 dual-shape). The CLI renderer must
    surface ``ship_ready``, ``release_blockers`` (with
    ``why_blocks_release``), ``verification_gaps``, and
    ``contract_status`` — earlier behavior dropped them silently.
    """
    payload = {
        "verdict": "REJECTED",
        "ship_ready": False,
        "short_summary": "Required pytest is red.",
        "findings": [],
        "release_blockers": [
            {
                "id": "R1",
                "severity": "P1",
                "title": "Required pytest suite is red",
                "file": "tests/test_server_handlers.py",
                "line": 42,
                "body": "test_update_user_success fails after migration.",
                "required_fix": "Make pytest tests/ green.",
                "why_blocks_release": "Acceptance requires green pytest.",
            },
        ],
        "verification_gaps": [
            {
                "risk": "Schema drift on undocumented field.",
                "missing_evidence": "No regression test on PUT row shape.",
                "required_check": "Add a row-shape assertion.",
            },
        ],
        "contract_status": {
            "task_contract": "incomplete",
            "interfaces":    "compatible",
            "persistence":   "safe",
            "tests":         "weak",
        },
    }
    out = _strip(render_review_block(payload, title="Final acceptance"))

    # ship_ready chip surfaces alongside the verdict.
    assert "ship_ready" in out
    assert "no" in out

    # Release blockers replace the (empty) findings list — but the
    # finding-shape contents are still rendered, plus the release-tier
    # why_blocks_release line that a plain finding does not carry.
    assert "Release blockers" in out
    assert "Findings" not in out
    assert "R1" in out
    assert "Required pytest suite is red" in out
    assert "tests/test_server_handlers.py:42" in out
    assert "test_update_user_success fails after migration." in out
    assert "Make pytest tests/ green." in out
    assert "why blocks release" in out
    assert "Acceptance requires green pytest." in out

    # Verification gaps section emitted when non-empty.
    assert "Verification gaps" in out
    assert "Schema drift on undocumented field." in out
    assert "No regression test on PUT row shape." in out
    assert "Add a row-shape assertion." in out

    # Contract status block renders every aspect.
    assert "Contract status" in out
    assert "task_contract" in out and "incomplete" in out
    assert "interfaces" in out and "compatible" in out
    assert "persistence" in out and "safe" in out
    assert "tests" in out and "weak" in out


def test_review_block_skips_empty_verification_gaps() -> None:
    """Empty ``verification_gaps`` must not emit a phantom section
    header — same shape rule as ``findings`` / ``risks`` / ``checks``.
    """
    out = _strip(render_review_block({
        "verdict": "APPROVED",
        "ship_ready": True,
        "short_summary": "All green.",
        "findings": [],
        "release_blockers": [],
        "verification_gaps": [],
        "contract_status": {
            "task_contract": "satisfied",
            "interfaces":    "compatible",
            "persistence":   "safe",
            "tests":         "sufficient",
        },
    }, title="Final acceptance"))
    assert "Verification gaps" not in out
    # Empty release_blockers also must not emit a phantom header.
    assert "Release blockers" not in out
    # ship_ready chip and contract status block still show on APPROVED.
    assert "ship_ready" in out and "yes" in out
    assert "Contract status" in out
    assert "satisfied" in out and "sufficient" in out


def test_review_block_legacy_review_payload_unchanged() -> None:
    """A payload without any release keys must render exactly as
    before — validate_plan / review_changes / hypothesis QA paths
    keep their shape (review schema only).
    """
    out = _strip(render_review_block({
        "verdict": "APPROVED",
        "short_summary": "Plan is sound.",
        "findings": [],
        "risks": ["alpha", "beta"],
        "checks": ["read plan_contract", "checked owned files"],
    }))
    assert "ship_ready" not in out
    assert "Release blockers" not in out
    assert "Verification gaps" not in out
    assert "Contract status" not in out
    assert "Risks" in out and "alpha" in out and "beta" in out
    assert "Checks" in out and "read plan_contract" in out


def test_summary_values_are_bold_across_transcript_blocks(
    force_color: None,
) -> None:
    cases = [
        (
            render_review_block({
                "verdict": "APPROVED",
                "short_summary": "All checks passed.",
                "findings": [],
            }),
            "All checks passed.",
        ),
        (
            render_plan_block({
                "short_summary": "Verify that calc.add returns a + b.",
                "tasks": [],
            }),
            "Verify that calc.add returns a + b.",
        ),
        (
            render_implement_summary(summary="Implementation complete.\nDetails."),
            "Implementation complete.",
        ),
    ]

    for out, summary in cases:
        assert f"{C.BOLD}{summary}{C.RESET}" in out


def test_implement_summary_resumable_label_for_fresh_runs() -> None:
    out = _strip(render_implement_summary(session_id="abc12345xyz"))
    assert "session" in out
    assert "abc12345" in out
    assert "(resumable)" in out
    # No parent linkage on a fresh run; the alt label must stay hidden.
    assert "resumed from parent" not in out


def test_implement_summary_renders_subtask_progress_label() -> None:
    out = _strip(render_implement_summary(
        progress={"kind": "subtasks", "completed": 3, "total": 3},
    ))
    assert "progress" in out
    assert "3/3 subtasks done" in out
    assert "planned files exist" not in out


def test_implement_summary_renders_planned_file_progress_label() -> None:
    out = _strip(render_implement_summary(
        progress={"kind": "planned_files", "completed": 4, "total": 5},
    ))
    assert "4/5 planned files exist" in out


def test_implement_summary_surfaces_followup_parent_session_id() -> None:
    """Follow-up runs render both the new captured sid and the parent
    sid that was passed to --resume so a reviewer can confirm at a
    glance that the resume actually fired (not just that the new run
    has SOME session)."""
    out = _strip(render_implement_summary(
        session_id="newsess99fresh",
        followup_parent_session_id="parent00sid123",
    ))
    assert "newsess9" in out
    assert "resumed from parent" in out
    assert "parent00" in out
    # The fresh-run "(resumable)" label is replaced, not appended.
    assert "(resumable)" not in out


def test_implement_summary_parent_alone_without_session_id_renders_nothing() -> None:
    # Defensive: a parent sid without a current session_id is a runtime
    # bug, but we shouldn't crash or invent a misleading row.
    out = _strip(render_implement_summary(
        followup_parent_session_id="parent00sid123",
    ))
    assert "session" not in out
    assert "resumed from parent" not in out


# ── Plan contract ──────────────────────────────────────────────────────


def test_plan_block_renders_summary_context_and_tasks() -> None:
    plan = {
        "short_summary": "Verify that calc.add returns a + b.",
        "planning_context": "Investigation showed the bug is already fixed.\n"
                            "Earlier commit 12222b8 corrected the inversion.",
        "goal": "Confirm calc.add is correct and tests pass.",
        "owned_files": ["calc.py", "test_calc.py"],
        "commands_to_run": ["python -m unittest test_calc.py -v"],
        "risks": ["Don't change correct code", "Don't weaken tests"],
        "review_focus": ["calc.py returns a + b", "Tests cover floats"],
        "acceptance_criteria": ["calc.add returns a + b", "All tests pass"],
        "tasks": [
            {
                "id": "T1-verify",
                "goal": "Verify current state of calc.add.",
                "files": ["calc.py", "test_calc.py"],
                "depends_on": [],
            }
        ],
    }
    out = _strip(render_plan_block(plan))

    assert "Verify that calc.add returns a + b." in out
    assert "Confirm calc.add is correct" in out
    # Long planning context is in its own block, full text preserved.
    assert "Investigation showed the bug is already fixed." in out
    assert "Earlier commit 12222b8 corrected the inversion." in out
    # Contract surface preserved.
    assert "files" in out and "calc.py" in out and "test_calc.py" in out
    assert "commands" in out and "python -m unittest test_calc.py -v" in out
    assert "risks" in out and "2" in out
    assert "tasks" in out and "1" in out
    # Task block carries id + goal + files.
    assert "T1-verify" in out
    assert "Verify current state of calc.add." in out


def test_plan_block_falls_back_to_legacy_plan_summary_key() -> None:
    plan = {"plan_summary": "Old-shape summary.", "tasks": []}
    out = _strip(render_plan_block(plan))
    assert "Old-shape summary." in out


def test_plan_block_renders_full_lists_not_only_counters() -> None:
    """REA-3.6 follow-up: render_plan_block must surface every contract
 field as a bulleted list, not just a count. Each item — every
 acceptance criterion, every risk, every review-focus item — must be
 visible in the rendered block."""
    plan = {
        "short_summary": "Verify that calc.add returns a + b.",
        "goal": "Confirm calc.add is correct.",
        "owned_files": ["calc.py", "test_calc.py"],
        "commands_to_run": ["python -m pytest test_calc.py -q"],
        "acceptance_criteria": [
            "calc.add returns a + b for every input",
            "pytest exits 0 with '5 passed'",
            "calc.py contains no a - b regression",
        ],
        "risks": [
            "Don't modify calc.py — already correct",
            "Don't weaken existing tests",
            "Don't run git commit / push",
            "Don't use destructive git commands",
        ],
        "review_focus": [
            "calc.py:2 returns a + b",
            "All 5 tests pass",
            "Working tree has no extraneous edits",
        ],
        "tasks": [
            {
                "id": "T1-verify-fix",
                "goal": "Run tests and confirm calc.add is fixed.",
                "spec": "Open calc.py and verify add returns a + b.\n"
                        "Run pytest and confirm 5 passed.\n"
                        "Do not modify source.",
                "files": ["calc.py", "test_calc.py"],
                "depends_on": [],
                "done_criteria": [
                    "calc.py:2 contains return a + b",
                    "pytest returns 0 with 5 passed",
                ],
            },
        ],
    }
    out = _strip(render_plan_block(plan))

    # Every contract item visible as a bullet — not just a count.
    for criterion in plan["acceptance_criteria"]:
        assert criterion in out, f"missing acceptance row: {criterion!r}"
    for risk in plan["risks"]:
        assert risk in out, f"missing risk row: {risk!r}"
    for focus in plan["review_focus"]:
        assert focus in out, f"missing review_focus row: {focus!r}"
    for owned in plan["owned_files"]:
        assert owned in out
    for cmd in plan["commands_to_run"]:
        assert cmd in out

    # Task spec + done criteria fully expanded, not collapsed to a count.
    task = plan["tasks"][0]
    assert task["id"] in out
    assert task["goal"] in out
    assert "spec" in out
    assert "Open calc.py and verify add returns a + b." in out
    assert "Run pytest and confirm 5 passed." in out
    assert "done" in out
    for crit in task["done_criteria"]:
        assert crit in out


def test_plan_block_keeps_planning_context_unchanged_for_long_bodies() -> None:
    """The Planning Context block is not truncated regardless of size."""
    plan = {
        "plan_summary": "Long.",
        "planning_context": "PARA " * 1000,
        "tasks": [],
    }
    out = _strip(render_plan_block(plan))
    assert "PARA " * 1000 in out


def test_plan_block_renders_mcp_context_entries() -> None:
    plan = {
        "plan_summary": "x",
        "mcp_context": [{"source": "linear", "id": "ENG-42"}],
        "tasks": [],
    }
    out = _strip(render_plan_block(plan))
    assert "MCP Context" in out
    assert "ENG-42" in out


# ── Cross-plan block ──────────────────────────────────────────────────


def _sample_cross_plan() -> dict:
    return {
        "interface_contract":
            "POST /v1/events { kind: str, payload: dict }\n"
            "DB: events(kind TEXT, payload JSONB)",
        "implementation_order":
            "1. api — land schema + endpoint.\n"
            "2. client — wire EventSink.",
        "subtasks": [
            ("api", "Add POST /v1/events handler.\nfiles: api/routes/events.py"),
            ("client", "Emit event on level completion."),
            ("stats", None),
        ],
        "aliases_missing": ["stats"],
    }


def test_render_cross_plan_block_accepts_mapping() -> None:
    """The renderer must accept any ``Mapping[str, Any]`` — never the
    parser's ``ParsedCrossPlan`` dataclass directly. This pins the
    duck-typed input contract that keeps ``core.io.transcript`` free of
    ``pipeline.cross_project`` imports."""
    out = _strip(render_cross_plan_block(_sample_cross_plan()))
    assert "Cross-project plan" in out


def test_render_cross_plan_block_shows_all_sections() -> None:
    out = _strip(render_cross_plan_block(_sample_cross_plan()))
    assert "Interface Contract" in out
    assert "Per-Project Subtasks" in out
    assert "Implementation Order" in out
    # Section bodies preserved verbatim.
    assert "POST /v1/events" in out
    assert "DB: events(kind TEXT, payload JSONB)" in out
    assert "1. api — land schema + endpoint." in out
    assert "2. client — wire EventSink." in out
    # Each alias rendered.
    assert "[api]" in out
    assert "[client]" in out
    assert "[stats]" in out


def test_render_cross_plan_block_dims_missing_aliases() -> None:
    out = _strip(render_cross_plan_block(_sample_cross_plan()))
    assert "missing" in out
    assert "fell through to full task" in out


def test_render_cross_plan_block_preserves_multiline_bodies() -> None:
    out = _strip(render_cross_plan_block(_sample_cross_plan()))
    # Continuation line of the first subtask body must survive.
    assert "files: api/routes/events.py" in out
    # Multi-line implementation order rendered as multiple lines.
    lines_with_order = [
        line for line in out.splitlines() if "land schema + endpoint" in line
    ]
    assert lines_with_order, "implementation order body missing"


def test_render_cross_plan_block_title_override() -> None:
    out = _strip(
        render_cross_plan_block(_sample_cross_plan(), title="Cross-plan v2")
    )
    assert "Cross-plan v2" in out
    assert "Cross-project plan" not in out


# ── stdout-render suppression toggle ──────────────────────────────────


class TestSuppressAssistantJson:
    """The PLAN phase wraps the agent invocation in
    :func:`core.io.stdout_render.defer_assistant_json` so that
    assistant-text blocks containing the typed JSON contract are replaced
    with a one-line "contract prepared" marker in live stdout. The
    orchestrator parses the JSON and renders a structured plan block
    afterwards instead. Tool-use lines must keep streaming so the
    operator still sees what the agent did."""

    def _filter(self, content: list[dict]) -> str | None:
        import json as _json

        from agents.stream_parsers.claude_jsonl import (
            format_claude_line_for_stdout,
        )
        line = _json.dumps({
            "type": "assistant",
            "message": {"content": content},
        })
        return format_claude_line_for_stdout(line)

    def test_assistant_json_text_passes_through_in_debug_mode(self) -> None:
        # In ``--output debug`` JSON-shaped assistant text is visible
        # (verbose mode is the only path that exposes raw contracts to
        # the live transcript). ``defer_assistant_json()`` still overrides
        # that — see ``test_assistant_json_text_dropped_under_defer``.
        from core.observability.logging import set_verbose

        set_verbose(True)
        try:
            out = self._filter([
                {"type": "text", "text": '{"verdict":"APPROVED"}'},
            ])
        finally:
            set_verbose(False)
        assert out is not None
        assert "verdict" in out

    def test_assistant_json_text_dropped_by_default(self) -> None:
        # Outside ``--output debug`` JSON-shaped assistant text is replaced
        # by the same one-line placeholder used by phase wrapping.
        out = self._filter([{"type": "text", "text": '{"verdict":"APPROVED"}'}])
        assert out is not None
        assert "Contracted answer prepared" in out
        assert "verdict" not in out

    def test_assistant_json_text_dropped_under_defer(self) -> None:
        from core.io.stdout_render import defer_assistant_json

        with defer_assistant_json():
            out = self._filter([
                {"type": "text", "text": '{"short_summary":"…","tasks":[]}'},
            ])
        assert out is not None
        assert "Contracted answer prepared" in out

    def test_tool_use_lines_keep_streaming_under_defer(self) -> None:
        from core.io.stdout_render import defer_assistant_json

        with defer_assistant_json():
            out = self._filter([
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "/p/calc.py"}},
                {"type": "text", "text": '{"plan":"…"}'},
            ])
        assert out is not None
        assert "📖 Read" in out
        # JSON-looking text is replaced with a placeholder alongside.
        assert "plan" not in out
        assert "Contracted answer prepared" in out

    @pytest.mark.parametrize("command", [
        "echo '{\"foo\":1}'",
        "cat /etc/config.json",
        "jq '.tasks[]' plan.json",
        "python -m unittest test_calc.py -v",
    ])
    def test_tool_use_with_json_looking_input_still_streams(
        self, command: str,
    ) -> None:
        """Risk #2: a Bash invocation whose argv mentions JSON (cat
 config.json, echo '{...}', jq pipelines) is part of the live
 progress signal — the suppressor only drops the model's own
 JSON-shaped *text*, not tool-use lines with JSON in them."""
        from core.io.stdout_render import defer_assistant_json

        with defer_assistant_json():
            out = self._filter([
                {"type": "tool_use", "name": "Bash", "input": {"command": command}},
            ])
        assert out is not None
        assert "⚡ Bash" in out
        # The command argv survives even when it contains JSON braces.
        # We assert one anchor token that's unique per command rather
        # than echoing the whole argv (the summarizer may shorten it).
        for token in command.split() if "{" not in command else [command]:
            if token == command:
                # JSON-literal command — at least the leading token survives.
                assert command.split()[0] in out
                break
            if token.startswith(("'", '"')):
                continue
            assert token in out
            break

    def test_prose_assistant_text_keeps_streaming_under_defer(self) -> None:
        # Free-form prose (model thinking out loud, "Reading the files…")
        # is NOT a JSON contract — it keeps streaming so the operator
        # still gets feedback while we wait for the final JSON body.
        from core.io.stdout_render import defer_assistant_json

        with defer_assistant_json():
            out = self._filter([
                {"type": "text", "text": "Reading the files to verify."},
            ])
        assert out is not None
        assert "Reading the files" in out

    def test_defer_is_thread_local_and_resets_on_exit(self) -> None:
        from core.io.stdout_render import (
            defer_assistant_json,
            is_assistant_json_suppressed,
        )

        assert is_assistant_json_suppressed() is False
        with defer_assistant_json():
            assert is_assistant_json_suppressed() is True
        assert is_assistant_json_suppressed() is False

    def test_recovery_shape_keeps_summary_drops_trailing_contract(self) -> None:
        # The recovery shape — a human "## Summary" trailed by a contract
        # object — keeps the prose and replaces only the JSON guts.
        out = self._filter([{
            "type": "text",
            "text": (
                "## Summary\n\nWired the edit flow.\n\n"
                '{"type":"subtask_attestation","subtask_id":"T1"}'
            ),
        }])
        assert out is not None
        assert "Wired the edit flow" in out
        assert "Contracted answer prepared" in out
        assert "subtask_attestation" not in out


class TestSplitEmbeddedJsonContract:
    """:func:`core.io.stdout_render.split_embedded_json_contract` isolates a
    trailing JSON contract from a prose summary so the live transcript can
    hide the same payload the recovery parser extracts post-hoc."""

    def test_trailing_contract_after_prose_is_split(self) -> None:
        from core.io.stdout_render import split_embedded_json_contract

        prose, found = split_embedded_json_contract(
            'Done.\n\n{"verdict":"APPROVED","findings":[]}'
        )
        assert found is True
        assert prose.strip() == "Done."

    def test_trailing_fenced_contract_is_split(self) -> None:
        from core.io.stdout_render import split_embedded_json_contract

        prose, found = split_embedded_json_contract(
            'Summary text.\n\n```json\n{"verdict":"APPROVED"}\n```'
        )
        assert found is True
        assert prose.strip() == "Summary text."

    def test_leading_contract_is_not_the_embedded_case(self) -> None:
        from core.io.stdout_render import split_embedded_json_contract

        text = '{"verdict":"APPROVED"}'
        prose, found = split_embedded_json_contract(text)
        assert found is False
        assert prose == text

    def test_inline_json_with_trailing_prose_not_split(self) -> None:
        from core.io.stdout_render import split_embedded_json_contract

        text = 'Send {"a":1} then check the result.'
        prose, found = split_embedded_json_contract(text)
        assert found is False
        assert prose == text

    def test_plain_prose_not_split(self) -> None:
        from core.io.stdout_render import split_embedded_json_contract

        text = "Just a normal sentence, no contract here."
        prose, found = split_embedded_json_contract(text)
        assert found is False
        assert prose == text

    def test_brace_only_in_quoted_string_not_a_contract(self) -> None:
        from core.io.stdout_render import split_embedded_json_contract

        # A non-JSON brace expression in prose must not be mistaken for a
        # contract (raw_decode fails → no split).
        text = "Use the {name, email} fields from the form."
        prose, found = split_embedded_json_contract(text)
        assert found is False
        assert prose == text


# ── Parse-failure block ───────────────────────────────────────────────


def test_render_parse_failure_surfaces_title_error_and_raw_body() -> None:
    raw = '{"verdict": "MAYBE", "tasks": null}'
    out = _strip(render_parse_failure(
        title="PLAN",
        error="short_summary missing required key",
        raw_output=raw,
    ))
    assert "Parse failure — PLAN" in out
    assert "short_summary missing required key" in out
    assert "Raw output" in out
    # Full raw body present, no truncation.
    assert raw in out


def test_render_parse_failure_handles_empty_body() -> None:
    out = _strip(render_parse_failure(
        title="REVIEW", error="raw JSON parse failed", raw_output="",
    ))
    assert "Parse failure — REVIEW" in out
    assert "(empty)" in out


def test_render_parse_failure_preserves_long_raw_bodies() -> None:
    huge = "X" * 4000
    out = _strip(render_parse_failure(
        title="final_acceptance", error="schema violation", raw_output=huge,
    ))
    assert huge in out


# ── Cross-cutting: no information loss ─────────────────────────────────


@pytest.mark.parametrize(
    "renderer, kwargs, must_contain",
    [
        (
            render_review_block,
            {"review": {
                "verdict": "REJECTED",
                "short_summary": "x",
                "findings": [{
                    "id": "F1", "severity": "P0",
                    "title": "t", "body": "B" * 5000,
                    "required_fix": "fix",
                }],
            }},
            "B" * 5000,
        ),
        (
            render_plan_block,
            {"plan": {
                "plan_summary": "Long.",
                "planning_context": "C" * 2000,
                "tasks": [],
            }},
            "C" * 2000,
        ),
    ],
)
def test_no_silent_truncation_in_structured_blocks(
    renderer, kwargs, must_contain,
) -> None:
    out = _strip(renderer(**kwargs))
    assert must_contain in out, (
        f"renderer {renderer.__name__} silently truncated body"
    )


# ── color-policy migration coverage (T3) ──────────────────────────────


class _Stdout:
    """Minimal TextIO double — only ``isatty`` matters for the policy."""

    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


class TestTranscriptColorPolicy:
    """Pin the T3 contract for ``core.io.transcript`` after migration:

    every helper routes through :func:`core.io.ansi.paint`, so the
    rendered output adapts to NO_COLOR / non-TTY stdout / the process
    override without any caller having to thread a ``color`` flag.
    """

    def _phase_header(self) -> str:
        return render_phase_header("IMPLEMENT", "smoke", status="running")

    def test_non_tty_stdout_strips_ansi(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr("sys.stdout", _Stdout(is_tty=False))
        set_color_enabled(None)
        out = self._phase_header()
        assert "\x1b[" not in out
        assert "[IMPLEMENT] smoke" in out
        assert "running" in out

    def test_no_color_strips_ansi_even_on_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setattr("sys.stdout", _Stdout(is_tty=True))
        set_color_enabled(None)
        out = self._phase_header()
        assert "\x1b[" not in out

    def test_process_override_false_disables_color_on_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr("sys.stdout", _Stdout(is_tty=True))
        set_color_enabled(False)
        out = self._phase_header()
        assert "\x1b[" not in out

    def test_process_override_true_enables_color_off_tty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr("sys.stdout", _Stdout(is_tty=False))
        set_color_enabled(True)
        out = self._phase_header()
        assert "\x1b[" in out

    def test_helpers_render_plain_under_disabled_color(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Spot-check several private helpers in one go — _color_verdict,
        # _color_ship_ready, _color_contract_value, _status_chip,
        # _severity_chip all route through paint() now.
        monkeypatch.setenv("NO_COLOR", "1")
        set_color_enabled(None)
        out = render_review_block({
            "verdict": "APPROVED",
            "short_summary": "ok",
            "findings": [
                {
                    "id": "F1", "severity": "P1", "title": "x", "body": "",
                    "required_fix": "y",
                },
            ],
            "ship_ready": True,
            "contract_status": {
                "task_contract": "satisfied",
                "interfaces": "compatible",
                "persistence": "broken",
                "tests": "unclear",
            },
        })
        assert "\x1b[" not in out
        # Plain content survives.
        assert "APPROVED" in out
        assert "yes" in out
        assert "P1" in out
        assert "satisfied" in out
        assert "broken" in out
        assert "unclear" in out

    def test_helpers_emit_color_under_force_color_fixture(
        self, force_color: None,
    ) -> None:
        out = render_review_block({
            "verdict": "REJECTED",
            "short_summary": "blocker",
            "findings": [
                {
                    "id": "F1", "severity": "P0", "title": "x", "body": "",
                    "required_fix": "y",
                },
            ],
        })
        assert "\x1b[" in out
        # Verdict + severity chip both carry their palette colors.
        assert C.RED in out


# ── F2: scheduled-gate live blocks (T3) ────────────────────────────────────
#
# The live render is sourced ONLY from the durable routing-decision records the
# gate_repair recorder appended for a hook firing — never from the on-disk
# receipt directory. A gate found fresh-but-unexecuted on a hook surfaces as
# FRESH, never PASS and never an absent block.


def _decision(command, decision, *, hook="after_phase", phase="implement",
              receipt_path=None):
    return {
        "hook": hook, "phase": phase, "command": command,
        "gate_set": "core", "decision": decision, "receipt_path": receipt_path,
    }


class TestScheduledGateLiveBlock:
    def test_render_classifies_each_decision(self) -> None:
        from pipeline.project.verification_timeline import (
            render_scheduled_gate_live_block,
        )

        lines = render_scheduled_gate_live_block(
            [
                _decision("lint", "executed_pass", receipt_path="/r/lint.json"),
                _decision("types", "executed_fail", receipt_path="/r/types.json"),
                _decision("unit", "skipped_fresh"),
                _decision("e2e", "manual_available"),
            ],
            hook_label="after_phase(implement)",
        )
        body = "\n".join(lines)
        assert lines[0] == "Verification gates — after_phase(implement)"
        assert "lint executed_pass" in body
        assert "types executed_fail" in body
        assert "unit skipped_fresh" in body
        assert "e2e manual_available" in body
        # Receipt paths from executed decisions are surfaced.
        assert "/r/lint.json" in body and "/r/types.json" in body

    def test_render_empty_when_no_decisions(self) -> None:
        from pipeline.project.verification_timeline import (
            render_scheduled_gate_live_block,
        )

        assert render_scheduled_gate_live_block([], hook_label="before_delivery") == ()
        # Unrecognised decisions are ignored, not rendered as a bare header.
        assert render_scheduled_gate_live_block(
            [_decision("x", "bogus")], hook_label="before_delivery",
        ) == ()

    def test_fresh_receipt_on_disk_unexecuted_gate_renders_fresh_not_pass(
        self, tmp_path,
    ) -> None:
        """KEY: a required gate with a FRESH receipt already on disk that the hook
        did NOT execute is recorded ``skipped_fresh`` — the live block shows
        FRESH, never PASS (the renderer reads the decision, not the receipt dir),
        and is never an empty block."""
        import json

        from pipeline.evidence.verification_receipt import COMMAND_RECEIPTS_DIRNAME
        from pipeline.project.verification_timeline import (
            render_scheduled_gate_live_block,
        )

        # A genuine passing receipt sits on disk for ``unit``.
        rdir = tmp_path / COMMAND_RECEIPTS_DIRNAME
        rdir.mkdir(parents=True)
        (rdir / "unit.json").write_text(
            json.dumps({"kind": "verification_command", "command": "unit",
                        "exit_code": 0, "assertions": []}),
            encoding="utf-8",
        )

        # ...but the hook did not run it; the recorder wrote skipped_fresh.
        lines = render_scheduled_gate_live_block(
            [_decision("unit", "skipped_fresh", hook="before_delivery", phase="")],
            hook_label="before_delivery",
        )
        body = "\n".join(lines)
        assert lines != ()
        assert "unit skipped_fresh" in body
        assert "unit executed_pass" not in body

    def test_print_emits_separate_framed_block_in_terminal(
        self, capsys,
    ) -> None:
        from pipeline.project.run import _print_scheduled_gate_live_blocks
        from pipeline.project.types import PresentationPolicy

        run = SimpleNamespace(
            _presentation=PresentationPolicy.TERMINAL,
            state=SimpleNamespace(extras={}),
        )
        _print_scheduled_gate_live_blocks(run, [
            _decision("lint", "executed_pass", receipt_path="/r/lint.json"),
            _decision("unit", "skipped_fresh"),
        ])

        out = strip_ansi(capsys.readouterr().out)
        # A standalone framed block, not merged into the transcript.
        assert "+-- Official verification gates" in out
        assert "Verification gates — after_phase(implement)" in out
        assert "lint executed_pass" in out and "unit skipped_fresh" in out
        assert "/r/lint.json" in out

    def test_print_groups_decisions_by_hook_label(self, capsys) -> None:
        """A single seam can record two hooks (before_phase + before_delivery on
        a FINAL phase): each renders as its own framed block."""
        from pipeline.project.run import _print_scheduled_gate_live_blocks
        from pipeline.project.types import PresentationPolicy

        run = SimpleNamespace(
            _presentation=PresentationPolicy.TERMINAL,
            state=SimpleNamespace(extras={}),
        )
        _print_scheduled_gate_live_blocks(run, [
            _decision("lint", "executed_pass", hook="before_phase",
                      phase="final_acceptance"),
            _decision("smoke", "skipped_fresh", hook="before_delivery", phase=""),
        ])

        out = strip_ansi(capsys.readouterr().out)
        assert "Verification gates — before_phase(final_acceptance)" in out
        assert "Verification gates — before_delivery" in out
        assert out.count("+-- Official verification gates") == 2

    def test_print_noop_in_silent(self, capsys) -> None:
        from pipeline.project.run import _print_scheduled_gate_live_blocks
        from pipeline.project.types import PresentationPolicy

        run = SimpleNamespace(
            _presentation=PresentationPolicy.SILENT,
            state=SimpleNamespace(extras={}),
        )
        _print_scheduled_gate_live_blocks(
            run, [_decision("lint", "executed_pass")],
        )
        assert capsys.readouterr().out == ""

    def test_print_noop_without_decisions(self, capsys) -> None:
        from pipeline.project.run import _print_scheduled_gate_live_blocks
        from pipeline.project.types import PresentationPolicy

        run = SimpleNamespace(
            _presentation=PresentationPolicy.TERMINAL,
            state=SimpleNamespace(extras={}),
        )
        _print_scheduled_gate_live_blocks(run, [])
        assert capsys.readouterr().out == ""

    def test_status_tokens_are_tinted_by_semantics(
        self, force_color: None, capsys,
    ) -> None:
        from pipeline.project.run import (
            _GATE_STATUS_COLORS,
            _print_scheduled_gate_live_blocks,
        )
        from pipeline.project.types import PresentationPolicy

        colors = dict(_GATE_STATUS_COLORS)
        run = SimpleNamespace(
            _presentation=PresentationPolicy.TERMINAL,
            state=SimpleNamespace(extras={}),
        )
        _print_scheduled_gate_live_blocks(run, [
            _decision("lint", "executed_pass"),
            _decision("unit", "skipped_fresh"),
        ])
        out = capsys.readouterr().out
        # Status tokens carry their semantic palette color.
        assert colors["executed_pass"] in out
        assert colors["skipped_fresh"] in out


def test_on_phase_pre_prints_scheduled_block_from_recorded_delta(
    monkeypatch, capsys,
) -> None:
    """End-to-end seam: the recorded gate-event delta around
    ``evaluate_pre_phase_gates`` is rendered as its own framed live block."""
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    decision = _decision(
        "lint", "executed_pass", hook="before_phase", phase="implement",
        receipt_path="/r/lint.json",
    )
    reads = iter(([], [decision]))

    monkeypatch.setattr(
        "pipeline.project.gate_repair.evaluate_pre_phase_gates", lambda *_: None,
    )
    monkeypatch.setattr("pipeline.project.run._gate_events_list", lambda _run: next(reads))
    state = SimpleNamespace(phase_log={}, extras={})
    run = SimpleNamespace(
        _presentation=PresentationPolicy.TERMINAL,
        profile_name="advanced", state=state,
    )

    _PipelineRun._on_phase_pre(run, "implement", state)

    out = strip_ansi(capsys.readouterr().out)
    assert "Verification gates — before_phase(implement)" in out
    assert "lint executed_pass" in out
    assert "/r/lint.json" in out


def test_on_phase_pre_no_scheduled_block_without_decisions(
    monkeypatch, capsys,
) -> None:
    from pipeline.project.run import _PipelineRun
    from pipeline.project.types import PresentationPolicy

    monkeypatch.setattr(
        "pipeline.project.gate_repair.evaluate_pre_phase_gates",
        lambda run, phase: None,
    )
    state = SimpleNamespace(phase_log={}, extras={})
    run = SimpleNamespace(
        _presentation=PresentationPolicy.TERMINAL,
        profile_name="advanced", state=state,
    )

    _PipelineRun._on_phase_pre(run, "implement", state)

    assert "Verification gates" not in capsys.readouterr().out


def _implement_incomplete_handoff_summary(
    *, last_output: str, attestation: dict[str, str],
) -> str:
    """Render ``_print_summary`` for an implement+incomplete handoff to text."""
    import io

    from pipeline.control.handoff_prompt import _print_summary
    from pipeline.runtime.handoff import PhaseHandoffRequested
    from pipeline.runtime.roles import PhaseHandoffType

    signal = PhaseHandoffRequested(
        handoff_id="implement:implement_handoff:1",
        phase="implement",
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger="incomplete",
        verdict="INCOMPLETE",
        approved=False,
        round_extras_key="implement_handoff",
        round=1,
        loop_max_rounds=1,
        available_actions=(
            "continue", "retry_feedback", "halt", "continue_with_waiver",
        ),
        artifacts={
            "incomplete_subtasks": list(attestation),
            "attestation_incomplete": attestation,
            "missing_subtask_receipts": [],
        },
        last_output=last_output,
    )
    out = io.StringIO()
    _print_summary(signal, out)
    return strip_ansi(out.getvalue())


def test_implement_incomplete_handoff_is_decision_first_raw_secondary() -> None:
    # A real implementation gap: the decision-oriented digest (Why paused +
    # Recommended) must lead; the raw transcript is demoted under Details and
    # truncated — never a giant raw-output-first block.
    body = _implement_incomplete_handoff_summary(
        last_output="## subtask T2-wire\n" + "noise " * 400,
        attestation={"T2-wire": "criterion 3: tests not added"},
    )
    why = body.index("Why paused")
    recommended = body.index("Recommended")
    details = body.index("Details:")
    raw = body.index("Last implementation output")
    # Decision-first: digest + recommendation precede the demoted raw output.
    assert why < recommended < details < raw
    assert "Subtask: T2-wire" in body
    assert body.index("Subtask: T2-wire") < raw
    assert "retry_feedback" in body
    # Raw output is truncated, not dumped wholesale (no 400-token noise wall).
    assert "..." in body
    assert body.count("noise") < 50


def test_implement_incomplete_baseline_exception_transcript() -> None:
    # A baseline / pre-existing verification exception surfaces the exception
    # framing and the waiver recommendation, still decision-first.
    body = _implement_incomplete_handoff_summary(
        last_output="suite red but baseline also red",
        attestation={"T1-mod": "baseline-identical failure, unrelated"},
    )
    assert body.index("Why paused") < body.index("Last implementation output")
    assert "baseline / pre-existing, unrelated to this diff" in body
    assert "continue_with_waiver" in body
