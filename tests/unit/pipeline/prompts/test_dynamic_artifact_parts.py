"""Tests for the dynamic file-validation PromptPart surfaces.

Two flows historically attached a reviewed file as a separate
``TURN`` / ``NONE`` :class:`PromptPart`:

* ``hypothesis_file_review_prompt`` (validate_hypothesis) — still
  ships an inline ``artifact:validate_hypothesis`` part. The file
  is reviewed in place; the on-disk path lives in metadata-only
  ``PromptPart.artifact_path`` so it never leaks into wire bytes.

* ``plan_file_review_prompt`` (validate_plan) — PR3 cutover. The
  monolithic ``artifact:validate_plan`` part is gone from the
  normal path. Instead two typed views rendered from
  :class:`~pipeline.plan_parser.ParsedPlan` ship:
  ``plan_contract:typed_plan`` (the *what*) and
  ``plan_tasks:execution_plan`` (the *how*). The on-disk
  ``plan_*.md`` is now presentation-only evidence; reviewer reads
  typed views, not parsed prose. ``ParsedPlan`` is the canonical
  contract — see the over-run-plan follow-up and change-semantics planning record (internal).
"""

from __future__ import annotations

import pytest

from agents.entities import SubTask
from pipeline import prompts
from pipeline.plan_parser import ParsedPlan
from pipeline.plugins import PluginConfig
from pipeline.prompts.types import PromptCacheScope, PromptStability

# Sentinel paths chosen so they cannot collide with project_dir, task
# text, file body, role/task/format prose, or any other static prompt
# part. ``assert sentinel not in wire`` then traces ANY leak of the
# path into wire bytes — never a false-positive substring match.
_HYP_PATH_SENTINEL = "/tmp/ZZSENTINELPATH_hypothesis_42.md"




def _artifact_part(env, phase: str):
    """Pluck the unique ``artifact:<phase>`` part out of a render envelope."""
    matches = [
        p for p in env.parts
        if p.kind == "artifact" and p.name == phase
    ]
    assert len(matches) == 1, (
        f"expected exactly one artifact:{phase} part, got {len(matches)}"
    )
    return matches[0]


def _plan_fixture(
    *,
    with_contract: bool = True,
    body_sentinel: str = "Reviewed plan body sentinel.",
) -> ParsedPlan:
    """Build a ParsedPlan with rich plan-level + per-task data.

    Used by every plan-side test so the assertions can target known
    fields (and their absence on the wire when expected). Embedding
    ``body_sentinel`` in the planning context gives tests one
    guaranteed-unique substring to count against.
    """
    kwargs: dict = dict(
        short_summary="Short summary text.",
        planning_context=f"Context: {body_sentinel}",
        subtasks=(
            SubTask(
                id="t1",
                goal="Investigate the surface",
                spec="Read existing handler, list call sites.",
                files=("src/handler.py",),
                skill="backend-python",
                done_criteria=("Surface mapped",),
            ),
            SubTask(
                id="t2",
                goal="Apply the fix",
                spec="Implement the minimal change.",
                files=("src/handler.py",),
                depends_on=("t1",),
                done_criteria=("Fix in place",),
            ),
        ),
        source="json",
    )
    if with_contract:
        kwargs.update(
            goal="Fix X without breaking Y",
            acceptance_criteria=("X no longer reproduces",),
            owned_files=("src/handler.py",),
            commands_to_run=("pytest tests/test_handler.py -q",),
            risks=("Could regress nearby path",),
            review_focus=("Symmetry with existing handler",),
        )
    return ParsedPlan(**kwargs)


# ---------------------------------------------------------------------------
# validate_hypothesis: still inline-artifact, untouched by PR3.
# ---------------------------------------------------------------------------


class TestValidateHypothesisArtifactPart:
    """``hypothesis_file_review_prompt`` still attaches a TURN/NONE
    artifact part whose body is the reviewed hypothesis file. The
    PR1 invariants (no path leak, path in metadata) still hold."""

    def test_artifact_part_metadata_turn_none(self) -> None:
        env = prompts.hypothesis_file_review_prompt(
            _HYP_PATH_SENTINEL,
            "# Hypothesis\n\nLikely payload mismatch.",
            "Fix calc.add",
            project_dir="/project",
        ).envelope()
        assert env is not None
        part = _artifact_part(env, "validate_hypothesis")
        assert part.stability is PromptStability.TURN
        assert part.cache_scope is PromptCacheScope.NONE
        assert part.volatile_reason
        assert part.id == "artifact:validate_hypothesis"
        assert part.source == "artifact"
        # Trace/evidence pointer: the on-disk path lives in
        # ``artifact_path`` metadata, never in ``body``.
        assert part.artifact_path == _HYP_PATH_SENTINEL

    def test_artifact_lands_in_turn_payload(self) -> None:
        env = prompts.hypothesis_file_review_prompt(
            _HYP_PATH_SENTINEL, "body", "task", project_dir="/project",
        ).envelope()
        assert env is not None
        payload_ids = {p.id for p in env.turn_payload_parts}
        prefix_ids = {p.id for p in env.stable_prefix_parts}
        assert "artifact:validate_hypothesis" in payload_ids
        assert "artifact:validate_hypothesis" not in prefix_ids

    def test_artifact_body_once_path_absent(self) -> None:
        body_sentinel = "Likely payload mismatch."
        out = prompts.hypothesis_file_review_prompt(
            _HYP_PATH_SENTINEL, body_sentinel, "Fix calc.add",
            project_dir="/project",
        ).text
        # Body still travels on the wire — once, in the artifact part.
        assert out.count(body_sentinel) == 1
        # Path NEVER appears in the wire bytes.
        assert _HYP_PATH_SENTINEL not in out
        # Old composition smell stays gone.
        assert "File: " not in out

    def test_artifact_path_is_not_hash_bearing(self) -> None:
        """Same body + different artifact_path → identical wire +
        identical prefix_hash + identical payload_hash. Load-bearing
        invariant: ``artifact_path`` is metadata, not hash-bearing."""
        body_sentinel = "Identical body content for cache-stability check."

        turn_a = prompts.hypothesis_file_review_prompt(
            "/tmp/path_A.md", body_sentinel, "Same task",
            project_dir="/project",
        )
        wire_a = turn_a.text
        env_a = turn_a.envelope()
        assert env_a is not None

        turn_b = prompts.hypothesis_file_review_prompt(
            "/tmp/path_B_completely_different.md",
            body_sentinel, "Same task",
            project_dir="/project",
        )
        wire_b = turn_b.text
        env_b = turn_b.envelope()
        assert env_b is not None

        # Sanity: the two parts really do carry different artifact_path
        # metadata — otherwise the test would trivially pass for the
        # wrong reason.
        assert (
            _artifact_part(env_a, "validate_hypothesis").artifact_path
            != _artifact_part(env_b, "validate_hypothesis").artifact_path
        )
        assert wire_a == wire_b
        assert env_a.prefix_hash == env_b.prefix_hash
        assert env_a.payload_hash == env_b.payload_hash

    def test_uses_normal_validation_task(self) -> None:
        """The wrapper drives the normal ``validate_hypothesis`` task;
        the deleted ``validate_hypothesis_file`` markdown must not
        appear in the rendered upper section."""
        env = prompts.hypothesis_file_review_prompt(
            "/tmp/h.md", "body", "task", project_dir="/project",
        ).envelope()
        assert env is not None
        task_part_names = {p.name for p in env.parts if p.kind == "task"}
        assert "validate_hypothesis" in task_part_names
        assert "validate_hypothesis_file" not in task_part_names


# ---------------------------------------------------------------------------
# validate_plan: PR3 cutover. Monolithic artifact part is gone;
# typed plan_contract + plan_tasks views ship instead.
# ---------------------------------------------------------------------------


class TestValidatePlanCompositionSplit:
    """``plan_file_review_prompt`` emits two typed PromptParts rendered
    from the parsed plan:

    * ``plan_contract:typed_plan`` — *what*: goal, acceptance criteria,
      owned files, commands, risks, review focus.
    * ``plan_tasks:execution_plan`` — *how*: per-subtask decomposition.

    The monolithic ``artifact:validate_plan`` part is not emitted on
    this path anymore. Cross-handoff / diff-only-fallback surfaces are
    out of scope here.
    """

    def test_emits_plan_contract_and_plan_tasks_parts(self) -> None:
        env = prompts.plan_file_review_prompt(
            _plan_fixture(),
            "Fix calc.add",
            PluginConfig(),
            project_dir="/project",
        ).envelope()
        assert env is not None

        kind_ids = {p.id for p in env.parts}
        assert "plan_contract:typed_plan" in kind_ids
        assert "plan_tasks:execution_plan" in kind_ids
        # The monolithic artifact part is GONE on this path.
        assert "artifact:validate_plan" not in kind_ids

    def test_typed_parts_are_turn_none(self) -> None:
        """Both typed views are TURN / NONE so the M2 partitioner
        keeps them out of the cacheable prefix and the M6 delta
        selector always re-sends them when the plan changes."""
        env = prompts.plan_file_review_prompt(
            _plan_fixture(),
            "Fix calc.add",
            PluginConfig(),
            project_dir="/project",
        ).envelope()
        assert env is not None

        for target_id in (
            "plan_contract:typed_plan",
            "plan_tasks:execution_plan",
        ):
            matches = [p for p in env.parts if p.id == target_id]
            assert len(matches) == 1, f"missing exactly one {target_id}"
            part = matches[0]
            assert part.stability is PromptStability.TURN
            assert part.cache_scope is PromptCacheScope.NONE
            assert part.volatile_reason
            assert part.source == "artifact"

    def test_typed_parts_land_in_turn_payload(self) -> None:
        """Both views must surface in the turn-payload partition,
        never in the stable prefix (mirrors the old artifact-part
        invariant for the new typed surfaces)."""
        env = prompts.plan_file_review_prompt(
            _plan_fixture(),
            "Fix calc.add",
            PluginConfig(),
            project_dir="/project",
        ).envelope()
        assert env is not None

        payload_ids = {p.id for p in env.turn_payload_parts}
        prefix_ids = {p.id for p in env.stable_prefix_parts}
        for target_id in (
            "plan_contract:typed_plan",
            "plan_tasks:execution_plan",
        ):
            assert target_id in payload_ids
            assert target_id not in prefix_ids

    def test_no_filesystem_path_in_wire(self) -> None:
        """The on-disk plan markdown is presentation-only evidence;
        its location must never appear in the wire bytes. The new
        signature does not even accept a path, but this is a
        defense-in-depth check that any incidental path-shaped
        token does not slip through (e.g. through extra_checks or
        a plugin override)."""
        out = prompts.plan_file_review_prompt(
            _plan_fixture(),
            "Fix calc.add",
            PluginConfig(),
            project_dir="/project",
        ).text
        for forbidden in (
            "/tmp/plan",
            "plan_artifact_path",
            "File: ",
        ):
            assert forbidden not in out, (
                f"unexpected path-shaped leak in wire: {forbidden!r}"
            )

    def test_plan_contract_part_carries_typed_contract_body(self) -> None:
        """The ``plan_contract:typed_plan`` body must carry the typed
        contract fields rendered from the plan (goal / acceptance
        criteria / etc.). This is what the reviewer reads as the
        *what* of the plan."""
        plan = _plan_fixture()
        env = prompts.plan_file_review_prompt(
            plan, "Fix calc.add", PluginConfig(), project_dir="/project",
        ).envelope()
        assert env is not None
        contract = next(
            p for p in env.parts if p.id == "plan_contract:typed_plan"
        )
        assert plan.goal in contract.body
        for criterion in plan.acceptance_criteria:
            assert criterion in contract.body

    def test_plan_tasks_part_carries_subtask_decomposition(self) -> None:
        """The ``plan_tasks:execution_plan`` body must list every
        subtask id + goal. This is the *how* of the plan."""
        plan = _plan_fixture()
        env = prompts.plan_file_review_prompt(
            plan, "Fix calc.add", PluginConfig(), project_dir="/project",
        ).envelope()
        assert env is not None
        tasks_part = next(
            p for p in env.parts if p.id == "plan_tasks:execution_plan"
        )
        for subtask in plan.subtasks:
            assert subtask.id in tasks_part.body
            assert subtask.goal in tasks_part.body

    def test_plan_with_no_contract_omits_contract_part(self) -> None:
        """Pre-REA-1 plans (no typed contract fields populated)
        render no contract section. In that case the
        ``plan_contract:typed_plan`` part is omitted so the wire
        does not carry an empty ``## Plan Contract`` heading.
        ``plan_tasks:execution_plan`` is still emitted — the
        decomposition is always relevant."""
        env = prompts.plan_file_review_prompt(
            _plan_fixture(with_contract=False),
            "Fix calc.add",
            PluginConfig(),
            project_dir="/project",
        ).envelope()
        assert env is not None
        kind_ids = {p.id for p in env.parts}
        assert "plan_contract:typed_plan" not in kind_ids
        assert "plan_tasks:execution_plan" in kind_ids

    def test_uses_normal_validate_plan_task(self) -> None:
        """The wrapper drives the normal ``validate_plan`` task; the
        deleted ``validate_plan_file`` markdown must not appear."""
        env = prompts.plan_file_review_prompt(
            _plan_fixture(),
            "task",
            PluginConfig(),
            project_dir="/project",
        ).envelope()
        assert env is not None
        task_part_names = {p.name for p in env.parts if p.kind == "task"}
        assert "validate_plan" in task_part_names
        assert "validate_plan_file" not in task_part_names


# ---------------------------------------------------------------------------
# Catalog absence: the deleted file-specific markdown tasks no longer
# exist as resolvable prompt names.
# ---------------------------------------------------------------------------


class TestDeletedTasksAbsent:
    @pytest.mark.parametrize(
        "name", ["tasks/validate_hypothesis_file", "tasks/validate_plan_file"],
    )
    def test_deleted_task_template_no_longer_resolves(
        self, name: str,
    ) -> None:
        from core.io import prompt_loader

        # Core resolution chain must report no match.
        assert prompt_loader.resolution_source(name) is None
        # And listing the catalog must not include it.
        assert name not in prompt_loader.list_core_prompts()
