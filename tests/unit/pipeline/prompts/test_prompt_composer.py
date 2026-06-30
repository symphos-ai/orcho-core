from __future__ import annotations

from pathlib import Path

import pytest

from core.io.prompt_loader import _PROJECT_PROMPTS_SUBPATH, reload_cache
from core.observability import prompt_trace
from pipeline.prompts.composer import (
    PromptSpec,
    render_composed_prompt,
    render_prompt_parts,
)


@pytest.fixture(autouse=True)
def clear_prompt_cache():
    reload_cache()
    yield
    reload_cache()


@pytest.fixture
def fake_core(tmp_path: Path, monkeypatch) -> Path:
    core_dir = tmp_path / "core_prompts"
    (core_dir / "roles").mkdir(parents=True)
    (core_dir / "tasks").mkdir()
    (core_dir / "formats").mkdir()
    monkeypatch.setattr("core.io.prompt_loader._CORE_PROMPTS", core_dir)
    reload_cache()
    return core_dir


def test_renders_role_task_and_format_parts(fake_core: Path) -> None:
    (fake_core / "roles" / "code_reviewer.md").write_text("ROLE $task")
    (fake_core / "tasks" / "code_review.md").write_text("TASK $task")
    (fake_core / "formats" / "review_findings.md").write_text("FORMAT")

    rendered = render_prompt_parts(
        PromptSpec(
            task="code_review", role="code_reviewer", format="review_findings",
        ),
        variables={"task": "fix add"},
    )

    assert rendered == "ROLE fix add\n\nTASK fix add\n\nFORMAT"


def test_render_requires_explicit_role(fake_core: Path) -> None:
    """A5.2a: there is no runtime-role fallback at the composer
 layer. A spec without an explicit ``role`` raises rather than
 silently picking a default — the boundary keeps prompt rendering
 independent of execution routing."""
    (fake_core / "tasks" / "code_review.md").write_text("TASK $task")
    with pytest.raises(ValueError, match="PromptSpec.role is required"):
        render_composed_prompt(
            PromptSpec(task="code_review"),
            variables={"task": "fix add"},
        )


def test_project_override_of_composable_part_wins(
    fake_core: Path,
    tmp_path: Path,
) -> None:
    """ADR 0009 project/workspace overrides are supported
 only at the composable-part level. A project override of
 ``tasks/code_review.md`` must win over the core file.
 """
    (fake_core / "roles" / "code_reviewer.md").write_text("CORE-ROLE")
    (fake_core / "tasks" / "code_review.md").write_text("CORE-TASK")

    project = tmp_path / "project"
    override_dir = project / _PROJECT_PROMPTS_SUBPATH / "tasks"
    override_dir.mkdir(parents=True)
    (override_dir / "code_review.md").write_text("PROJECT-TASK $task")

    rendered = render_composed_prompt(
        PromptSpec(task="code_review", role="code_reviewer"),
        project_dir=project,
        variables={"task": "fix add"},
    )

    assert "CORE-ROLE" in rendered      # core role still resolves
    assert "PROJECT-TASK fix add" in rendered  # project task wins
    assert "CORE-TASK" not in rendered  # core task masked by override


def test_render_prompt_parts_records_upper_seams_for_debug_trace(
    fake_core: Path,
    tmp_path: Path,
) -> None:
    """Composer side-channel: every rendered part is captured with its
    resolution source so the runtime adapter can surface the seam
    structure in ``--output debug``. Project overrides are reported as
    ``project``; core falls back to ``core``."""
    (fake_core / "roles" / "code_reviewer.md").write_text("CORE-ROLE")
    (fake_core / "tasks" / "code_review.md").write_text("CORE-TASK")
    (fake_core / "formats" / "review_findings.md").write_text("FORMAT")

    project = tmp_path / "project"
    override_dir = project / _PROJECT_PROMPTS_SUBPATH / "tasks"
    override_dir.mkdir(parents=True)
    (override_dir / "code_review.md").write_text("PROJECT-TASK")

    # Clear stale side-channel state so the assertion isolates this call.
    prompt_trace.take_last_upper()

    render_prompt_parts(
        PromptSpec(
            task="code_review",
            role="code_reviewer",
            format="review_findings",
        ),
        project_dir=project,
    )

    seams = prompt_trace.take_last_upper()
    assert seams is not None
    assert [(p.kind, p.name, p.source) for p in seams] == [
        ("role", "code_reviewer", "core"),
        ("task", "code_review", "project"),
        ("format", "review_findings", "core"),
    ]
    # The captured body is the rendered text of each part — kept so the
    # debug renderer can frame the section without re-rendering.
    assert seams[1].body == "PROJECT-TASK"


def test_take_last_upper_clears_slot_after_consume() -> None:
    """The side-channel is single-take: a subsequent invocation that
    didn't go through the composer (e.g. a raw runtime call) must read
    ``None`` rather than a stale composition from the previous phase."""
    prompt_trace.set_last_upper(())  # seed with empty tuple
    assert prompt_trace.take_last_upper() == ()
    assert prompt_trace.take_last_upper() is None


def test_legacy_root_flat_override_no_longer_renders(
    fake_core: Path,
    tmp_path: Path,
) -> None:
    """ADR 0009 legacy root-level flat overrides are no
 longer supported. A project that places a flat
 ``reviewer_code_review.md`` at the prompts root has zero effect
 on the composed render — there is no fallback path that reads it.
 """
    (fake_core / "roles" / "code_reviewer.md").write_text("CORE-ROLE")
    (fake_core / "tasks" / "code_review.md").write_text("CORE-TASK $task")

    project = tmp_path / "project"
    override_dir = project / _PROJECT_PROMPTS_SUBPATH
    override_dir.mkdir(parents=True)
    # Drop a stale flat-style override into the prompts root.
    (override_dir / "reviewer_code_review.md").write_text(
        "STALE-FLAT-OVERRIDE — should not affect render"
    )

    rendered = render_composed_prompt(
        PromptSpec(task="code_review", role="code_reviewer"),
        project_dir=project,
        variables={"task": "fix add"},
    )

    # Composed parts render from core; the stale flat override is
    # silently ignored.
    assert "CORE-ROLE" in rendered
    assert "CORE-TASK fix add" in rendered
    assert "STALE-FLAT-OVERRIDE" not in rendered


# ---------------------------------------------------------------------------
# M11.5 Fix 3 — placeholder classifier
# ---------------------------------------------------------------------------


class TestPlaceholderClassifierM11_5:
    """The composer's placeholder classifier must:

    1. Match both ``$name`` and ``${name}`` forms (the trailing
       ``\\b`` after ``}`` in the pre-M11.5 regex silently dropped the
       brace form on most real bodies).
    2. Treat project/run-scoped variables (``$context``,
       ``$project_dir``, ``$focus``, ``$bundle_markdown``, etc.) as
       non-globally-cacheable so project-specific content cannot
       leak into the stable prefix.
    3. Keep purely static prose at the M1 default (STATIC / GLOBAL).
    """

    def test_brace_form_task_variable_classified_as_turn(
        self, fake_core: Path,
    ) -> None:
        (fake_core / "roles" / "rev.md").write_text("Static role body.")
        (fake_core / "tasks" / "code_review.md").write_text(
            "Address ${task} carefully.",
        )

        prompt_trace.take_last_upper()
        render_prompt_parts(
            PromptSpec(task="code_review", role="rev"),
            variables={"task": "fix add"},
        )
        parts = prompt_trace.take_last_upper() or ()
        task = next(p for p in parts if p.kind == "task")
        # ``${task}`` must be classified TURN/NONE — the pre-M11.5
        # regex dropped this form because of a trailing ``\b`` after
        # the close brace.
        assert task.stability.value == "turn"
        assert task.cache_scope.value == "none"
        assert task.volatile_reason

    def test_brace_form_project_dir_classified_as_run_workspace(
        self, fake_core: Path, tmp_path: Path,
    ) -> None:
        (fake_core / "roles" / "rev.md").write_text(
            "Workspace anchor: ${project_dir}.",
        )
        (fake_core / "tasks" / "tk.md").write_text("Static task body.")

        prompt_trace.take_last_upper()
        # ``project_dir`` is a render_prompt kwarg, not a template
        # variable — pass it via the dedicated parameter so it
        # substitutes into the role body without colliding with
        # render_prompt's own keyword.
        render_prompt_parts(
            PromptSpec(task="tk", role="rev"),
            project_dir=tmp_path,
        )
        parts = prompt_trace.take_last_upper() or ()
        role = next(p for p in parts if p.kind == "role")
        assert role.stability.value == "run"
        assert role.cache_scope.value == "workspace"
        assert role.volatile_reason

    def test_role_with_context_and_project_dir_not_static_global(
        self, fake_core: Path, tmp_path: Path,
    ) -> None:
        # Mirrors the shipped systems_architect / implementation_engineer
        # role templates that reference both vars in their first lines.
        (fake_core / "roles" / "rev.md").write_text(
            "Project at: $project_dir\n$context\n\nStatic persona prose.",
        )
        (fake_core / "tasks" / "tk.md").write_text("Static task.")

        prompt_trace.take_last_upper()
        render_prompt_parts(
            PromptSpec(task="tk", role="rev"),
            project_dir=tmp_path,
            variables={"context": "ctx"},
        )
        role = next(
            p for p in (prompt_trace.take_last_upper() or ()) if p.kind == "role"
        )
        # The role must NOT be STATIC/GLOBAL — project-specific
        # substitutions force a RUN/WORKSPACE classification so the
        # M2 partitioner excludes it from the global prefix.
        assert not (
            role.stability.value == "static" and role.cache_scope.value == "global"
        )

    def test_review_uncommitted_task_with_focus_classified_as_turn(
        self, fake_core: Path,
    ) -> None:
        (fake_core / "roles" / "rev.md").write_text("Static role.")
        (fake_core / "tasks" / "review_uncommitted.md").write_text(
            "Focus: $focus\n\nReview the working tree.",
        )

        prompt_trace.take_last_upper()
        render_prompt_parts(
            PromptSpec(task="review_uncommitted", role="rev"),
            variables={"focus": "tests"},
        )
        parts = prompt_trace.take_last_upper() or ()
        task = next(p for p in parts if p.kind == "task")
        # ``$focus`` is now a turn variable: review_uncommitted is a
        # per-call surface, not a persona.
        assert task.stability.value == "turn"
        assert task.cache_scope.value == "none"

    def test_no_static_global_part_contains_run_or_turn_text(
        self, fake_core: Path, tmp_path: Path,
    ) -> None:
        # Inverse of the prefix-leak guard: assemble a composition
        # whose templates reference every M11.5 turn/run var and
        # confirm no resulting part is STATIC/GLOBAL.
        (fake_core / "roles" / "rev.md").write_text(
            "Project: $project_dir\n$context",
        )
        (fake_core / "tasks" / "tk.md").write_text("Task: $task\nDiff: $diff")
        (fake_core / "formats" / "fmt.md").write_text("Focus: ${focus}")

        prompt_trace.take_last_upper()
        render_prompt_parts(
            PromptSpec(task="tk", role="rev", format="fmt"),
            project_dir=tmp_path,
            variables={
                "context": "c",
                "task": "T", "diff": "D", "focus": "F",
            },
        )
        parts = prompt_trace.take_last_upper() or ()
        for p in parts:
            assert not (
                p.stability.value == "static"
                and p.cache_scope.value == "global"
            ), (
                f"Part {p.id!r} classified STATIC/GLOBAL but body "
                f"contains run/turn substitution variable."
            )
