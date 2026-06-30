"""
Boundary tests for composable prompt parts.

ADR 0009 deliberately separates four layers:

 ``roles/*.md`` — persona, project anchor.
 ``tasks/*.md`` — what to do (procedure, checks, ``$variables``).
 ``formats/*.md`` — presentation/detail/style only (role-agnostic).
 ``pipeline.prompts.contracts`` system-tail blocks —
 code-owned policy and parser contracts:
 ``change_handoff`` ``review_target`` ``review_json``
 ``plan_json`` ``authoring_language``.

The system-tail layer owns everything that must NOT be droppable by a
project override of a user-editable part:

 language posture (``authoring_language`` body covers prose +
 code; ``review_json``'s ``body_language`` covers JSON body fields);
 safety/orchestration policy (destructive git, preserve user-owned,
 change handoff mode, review target mode);
 parser contracts (JSON-only output, schema, enum values).

A correct ``formats/*.md`` is reusable across roles and tasks: a
reviewer, developer, architect, or technical editor can all pick the
same preset and combine it with their own role+task.

These tests are the structural guardrail: they fail CI when a future
edit re-introduces any of the leaks Phases 2.2 / 2.4 / 3 / 6A fixed.
"""

import re
from pathlib import Path

from core.infra.paths import PROMPTS_DIR as _PROMPTS_DIR

_FORMATS_DIR = _PROMPTS_DIR / "formats"
_ROLES_DIR = _PROMPTS_DIR / "roles"
_TASKS_DIR = _PROMPTS_DIR / "tasks"

_PARSER_CONTRACT_TOKENS = (
    "json",
    "schema",
    "system-tail",
    "system-block",
    "review_json",
    "release_json",
    "change_handoff",
    "review_target",
    "approved",
    "rejected",
    "p0",
    "p1",
    "p2",
    "p3",
    "exactly one",
    "return a structured",
)

# Role-bound or task-bound vocabulary. A generic preset should never
# reference a specific persona or a specific phase of work — those
# concepts live in ``roles/*.md`` and ``tasks/*.md``. Keep the list
# narrow: generic words like ``task`` or ``verification`` may legitimately
# appear in a handoff-style preset.
_ROLE_BOUND_TOKENS = (
    "reviewer",
    "developer",
    "architect",
    "code review",
    "code-review",
    "task contract",
    "code change",
    "code changes",
    "decompose",
    "git ",
    "working-tree",
)
# Note: ``findings``, ``diff``, ``verification``, ``task`` are intentionally
# NOT in this list. A generic detailed/handoff preset legitimately uses
# them ("separate findings", "what was verified", "what changed", "the
# diff between"). The forbidden list targets words that BIND the preset
# to a specific role/persona/phase.

_TASK_VARIABLE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


def test_formats_directory_carries_no_parser_contract() -> None:
    """``formats/*.md`` must not define parser-required output shape."""
    leaks: list[tuple[str, str]] = []
    for prompt_file in sorted(_FORMATS_DIR.glob("*.md")):
        body = prompt_file.read_text(encoding="utf-8").casefold()
        for token in _PARSER_CONTRACT_TOKENS:
            if token in body:
                leaks.append((prompt_file.name, token))

    assert not leaks, (
        "formats/*.md must not carry parser-contract tokens. "
        "Move machine output shape to pipeline.prompts.contracts.\n"
        + "\n".join(f"  {name}: {token!r}" for name, token in leaks)
    )


def test_formats_directory_carries_no_role_or_task_binding() -> None:
    """``formats/*.md`` must be role-agnostic reusable presets.

 A reviewer-specific findings phrasing or a developer-specific
 edit-discipline rule belongs in ``roles/*.md`` or ``tasks/*.md``,
 not in the format slot. Format presets must combine cleanly with
 any role + any task.
 """
    leaks: list[tuple[str, str]] = []
    for prompt_file in sorted(_FORMATS_DIR.glob("*.md")):
        body = prompt_file.read_text(encoding="utf-8").casefold()
        for token in _ROLE_BOUND_TOKENS:
            if token in body:
                leaks.append((prompt_file.name, token))

    assert not leaks, (
        "formats/*.md must be role-agnostic reusable presets. "
        "Move role/task-bound content to roles/*.md or tasks/*.md.\n"
        + "\n".join(f"  {name}: {token!r}" for name, token in leaks)
    )


def test_formats_directory_carries_no_task_variables() -> None:
    """Task-specific interpolation belongs to roles/tasks, not formats."""
    leaks: list[tuple[str, str]] = []
    for prompt_file in sorted(_FORMATS_DIR.glob("*.md")):
        body = prompt_file.read_text(encoding="utf-8")
        for match in _TASK_VARIABLE_RE.finditer(body):
            leaks.append((prompt_file.name, match.group(0)))

    assert not leaks, (
        "formats/*.md must be presentation/detail/style only. "
        "Move task-specific variables to roles/*.md or tasks/*.md.\n"
        + "\n".join(f"  {name}: {token!r}" for name, token in leaks)
    )


# ── Positive: shipped generic presets are role-agnostic + combinable ─────

def test_shipped_generic_presets_exist() -> None:
    """The four generic role-agnostic presets ship in core."""
    expected = {"terse", "compact", "detailed", "bullets", "handoff"}
    actual = {f.stem for f in _FORMATS_DIR.glob("*.md")}
    missing = expected - actual
    assert not missing, f"Missing shipped generic format presets: {missing}"


def test_handoff_preset_renders_with_build_and_fix_specs() -> None:
    """The handoff preset must combine with developer task families."""
    from pipeline.plugins import PluginConfig
    from pipeline.prompts import build_prompt, fix_prompt
    from pipeline.runtime import PromptSpec

    plugin = PluginConfig()
    build_out = build_prompt(
        "Test task", "/project", plugin,
        prompt_spec=PromptSpec(
            role="implementation_engineer", task="implement", format="handoff",
        ),
    ).text
    fix_out = fix_prompt(
        "Test task", "critique", "/project", plugin,
        prompt_spec=PromptSpec(
            role="implementation_engineer", task="repair_changes", format="handoff",
        ),
    ).text
    # The handoff preset contributes its persona-agnostic guidance.
    assert "next agent" in build_out.lower() or "maintainer" in build_out.lower()
    assert "next agent" in fix_out.lower() or "maintainer" in fix_out.lower()


def test_detailed_preset_renders_with_review_spec() -> None:
    """The detailed preset must combine with reviewer task families."""
    from pipeline.plugins import PluginConfig
    from pipeline.prompts import review_focus
    from pipeline.runtime import PromptSpec

    out = review_focus(
        "Test task", PluginConfig(),
        prompt_spec=PromptSpec(
            task="code_review", role="code_reviewer", format="detailed",
        ),
    ).text
    # Detailed preset contributes its detail/separation guidance.
    assert "detailed response" in out.lower() or "separate findings" in out.lower()


# ── Cross-layer config-owned policy guard ─────────────────────────────────
#
# Phases 2.2 / 2.4 / 3 / 6A established: language posture, safety policy,
# parser contracts live in ``pipeline.prompts.contracts`` system-tail
# blocks — never in user-editable role/task/format parts. The shipped
# system-tail blocks are: ``authoring_language``, ``change_handoff``,
# ``review_target``, ``review_json``, ``plan_json``.
#
# The tests below pin that any leak of those concepts back into
# user-editable parts (under any one of the three subdirectories) fails
# CI. This is the structural lever that prevents an agent — including
# the one editing prompts right now — from accidentally re-introducing
# the leak. If you find yourself wanting to add language directives
# or safety policy to a user-editable part, the right move is to
# extend the corresponding system-tail block instead.

_CONFIG_OWNED_POLICY_TOKENS = (
    # Config-owned language variables — ``cfg.task_language``,
    # ``cfg.plan_language``, etc. ship via ``authoring_language_strategy``
    # / ``review_json_contract(body_language=...)`` / ``plan_json_contract``,
    # NEVER via a template variable in a user-editable part.
    "$task_language",
    "$body_language",
    "$artifact_language",
    "$input_language",
    # Phrased language directives — same anti-pattern, no ``$`` form.
    # The phrasings below mirror what the user-editable templates
    # historically shipped before / 6A moved them out.
    "reply in $",
    "respond in $",
    # Parser contracts — the JSON-only output shape lives in the
    # ``review_json`` / ``plan_json`` system-tail blocks. The phrases
    # below are anchors in those blocks; they must not appear in a
    # user-editable part.
    "exactly one json object",
    "no ```json``` fence",
    "return a structured",
    # Old prose verdict protocol must not appear in user-editable parts.
    "verdict: approved",
    "verdict: rejected",
    # System-tail block names — referencing them from a user-editable
    # part means the part is describing orchestration policy, not
    # role/task/format. Those concepts belong in the block itself.
    "system-tail",
    "system-block",
    "authoring_language",
    "change_handoff",
    "review_target",
    "review_json",
    "release_json",
    "plan_json",
    "cross_subtask_blocks",
    # Safety policy phrases owned by ``change_handoff_strategy`` /
    # ``review_target_strategy``. If a project author needs to talk
    # about destructive git, extend the system-tail block — don't
    # echo the policy into a user-editable part.
    "destructive git",
    "preserve user-owned",
    "user-owned working-tree",
)


def _scan_for_leaks(
    directory: Path, forbidden: tuple[str, ...],
) -> list[tuple[str, str]]:
    leaks: list[tuple[str, str]] = []
    for prompt_file in sorted(directory.glob("*.md")):
        body = prompt_file.read_text(encoding="utf-8").casefold()
        for token in forbidden:
            if token in body:
                leaks.append((prompt_file.name, token))
    return leaks


def test_roles_carry_no_config_owned_policy() -> None:
    """``roles/*.md`` is persona + project anchor only.

 Language posture, safety policy, parser contracts — none of those
 belong in a role file. The role describes who the agent IS, not
 how Orcho enforces output shape or orchestration.
 """
    leaks = _scan_for_leaks(_ROLES_DIR, _CONFIG_OWNED_POLICY_TOKENS)
    assert not leaks, (
        "roles/*.md must not carry config-owned policy tokens. "
        "Move language / safety / parser-contract content to the "
        "matching system-tail block in pipeline.prompts.contracts.\n"
        + "\n".join(f"  {name}: {token!r}" for name, token in leaks)
    )


def test_tasks_carry_no_config_owned_policy() -> None:
    """``tasks/*.md`` is procedure + checks + ``$body`` insertion.

 Same rule as roles: tasks describe what to do, not the language
 the agent responds in, not the JSON shape, not the safety
 policy. Those live in system-tail.
 """
    leaks = _scan_for_leaks(_TASKS_DIR, _CONFIG_OWNED_POLICY_TOKENS)
    assert not leaks, (
        "tasks/*.md must not carry config-owned policy tokens. "
        "Move language / safety / parser-contract content to the "
        "matching system-tail block in pipeline.prompts.contracts.\n"
        + "\n".join(f"  {name}: {token!r}" for name, token in leaks)
    )


def test_formats_carry_no_config_owned_policy() -> None:
    """``formats/*.md`` already had its own narrower guards above
 (no parser contracts, no role/task binding, no template
 variables). The config-owned-policy check rounds it out so all
 three user-editable subdirectories share one structural floor.
 """
    leaks = _scan_for_leaks(_FORMATS_DIR, _CONFIG_OWNED_POLICY_TOKENS)
    assert not leaks, (
        "formats/*.md must not carry config-owned policy tokens. "
        "Move language / safety / parser-contract content to the "
        "matching system-tail block in pipeline.prompts.contracts.\n"
        + "\n".join(f"  {name}: {token!r}" for name, token in leaks)
    )


# ---------------------------------------------------------------------------
# Orchestrator-topology guard ( / 7.9e / 8 follow-up).
#
# Rendered prompt text must speak agent vocabulary, not orchestrator
# vocabulary. Criterion:
#
#  A line that tells the agent what action to take or what form the
#  response takes is prompt. Keep.
#  A line that explains why the pipeline is shaped this way is
#  documentation. Move to docstring / ADR.
#
# Two scopes:
#  1. Strict — code-owned ``contract_templates.py`` bodies. No phase
#  names, no brand, no impl-detail terms. The criterion is absolute
#  here because these strings are appended to every rendered prompt.
#  2. Looser — user-editable ``_prompts/{roles,tasks,formats}/*.md``.
#  Task-instruction headers may legitimately use uppercase verbs that
#  overlap with phase names (``TASK TO DECOMPOSE``, ``TASK TO PLAN``)
#  those are imperatives naming the action, not references to
#  internal phases. This scope only flags the orchestrator brand
#  and pipeline-implementation terms.
# ---------------------------------------------------------------------------

_TOPOLOGY_TERMS_CONTRACT_BODIES = re.compile(
    r"\b("
    r"Orcho"
    r"|REVIEW(?!_TARGET| target| schema)"
    r"|FIX"
    r"|FINAL_ACCEPTANCE"
    r"|VALIDATE_PLAN"
    r"|BUILD"
    r"|DECOMPOSE"
    r"|HYPOTHESIS"
    r"|REPLAN"
    r"|pipeline"
    r"|orchestrat"
    r"|downstream"
    r"|upstream"
    r"|authoring phase"
    r"|next phase"
    r"|workflow"
    r")\b"
)

# Looser: brand + impl-detail terms only. Phase-name verbs are allowed
# in user-editable parts as task-instruction headers.
_TOPOLOGY_TERMS_USER_PARTS = re.compile(
    r"\b("
    r"Orcho"
    r"|pipeline"
    r"|orchestrat"
    r"|downstream"
    r"|upstream"
    r"|authoring phase"
    r")\b"
)


def test_no_orchestrator_topology_in_contract_template_bodies() -> None:
    """Every rendered system-tail body must speak agent vocabulary.

 Pipeline topology terms (brand names, phase names, implementation
 detail like ``pipeline``, ``orchestrat*``, ``downstream``) have no
 operative meaning for the agent and burn tokens explaining the
 system's internals instead of telling the agent what to do.

 See ADR 0009 (orchestrator-name leak excision) and
 7.9e (final topology grep-pass).
 """
    from pipeline.prompts.contract_templates import SYSTEM_PROMPT_TEMPLATES

    leaks: list[tuple[str, int, str, str]] = []
    for key, template in SYSTEM_PROMPT_TEMPLATES.items():
        for i, line in enumerate(template.body.splitlines(), 1):
            for match in _TOPOLOGY_TERMS_CONTRACT_BODIES.findall(line):
                leaks.append((key, i, match, line.strip()))
    assert not leaks, (
        "Orchestrator topology terms found in rendered contract bodies.\n"
        "Bodies must speak agent vocabulary; pipeline topology belongs "
        "in docstrings and ADRs, not in prompt text the LLM sees.\n"
        + "\n".join(f"  {k}:{i} {m!r}: {line}" for k, i, m, line in leaks)
    )


def test_no_orchestrator_brand_in_user_editable_parts() -> None:
    """User-editable ``_prompts/{roles,tasks,formats}/*.md`` must not
 name the orchestrator brand or leak implementation detail.

 Looser than the contract-body guard: task-instruction headers like
 ``TASK TO DECOMPOSE: $task`` legitimately use uppercase verbs that
 overlap with phase names — those are imperatives naming the action,
 not references to internal phases. This guard only flags the
 orchestrator brand (``Orcho``) and pipeline-implementation terms
 (``pipeline``, ``orchestrat*``, ``downstream``, ``upstream``,
 ``authoring phase``).

 The agent must not learn the system's brand or internal topology
 from the prompt. That information belongs in developer-facing docs
 (e.g. ``_prompts/README.md``), not in rendered prompt text.
 """
    leaks: list[tuple[str, int, str, str]] = []
    for layer in (_ROLES_DIR, _TASKS_DIR, _FORMATS_DIR):
        for path in sorted(layer.glob("*.md")):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                for match in _TOPOLOGY_TERMS_USER_PARTS.findall(line):
                    rel = path.relative_to(_PROMPTS_DIR)
                    leaks.append((str(rel), i, match, line.strip()))
    assert not leaks, (
        "Orchestrator brand or implementation-detail terms found in "
        "user-editable prompt parts. The agent should not learn the "
        "system's brand or topology from the prompt. Move such "
        "explanations to developer docs (_prompts/README.md) and "
        "keep the prompt text agent-vocabulary only.\n"
        + "\n".join(f"  {p}:{i} {m!r}: {line}" for p, i, m, line in leaks)
    )


def test_no_orchestrator_topology_in_minimal_intents() -> None:
    """The ablation MINIMAL arm renders code-owned intents from
 ``pipeline.prompts.minimal_intents``. Same agent-vocabulary
 discipline as ``SYSTEM_PROMPT_TEMPLATES``: no orchestrator brand,
 no uppercase phase names, no implementation-detail terms.

 Render each intent with placeholder-style inputs and scan the
 resulting strings with the strict topology regex used for
 contract-template bodies.
 """
    from pipeline.prompts import minimal_intents as mi

    samples: list[tuple[str, str]] = [
        ("plan_intent", mi.plan_intent(
            "do thing", ma_artifacts_dir=".orcho/artifacts",
            extra_step="4. extra rule",
        )),
        ("replan_intent", mi.replan_intent("do thing", "the critique")),
        ("decompose_intent", mi.decompose_intent(
            "do thing", skill_roster_block="AVAILABLE SKILLS: x",
            extra_step="Project rule: y",
        )),
        ("hypothesis_intent", mi.hypothesis_intent(
            "do thing", codemap="src/foo.py",
        )),
        ("readonly_plan_intent", mi.readonly_plan_intent(
            "do thing", codemap="src/foo.py",
        )),
        ("build_intent", mi.build_intent(
            "do thing", ma_artifacts_dir=".orcho/artifacts",
            extra_step="5. extra rule",
        )),
        ("fix_intent", mi.fix_intent("do thing", "review body here")),
        ("review_focus_intent", mi.review_focus_intent(
            "do thing", extra_checks="Project checks: z",
        )),
        ("plan_review_focus_intent", mi.plan_review_focus_intent(
            "do thing", extra_checks="Project checks: z",
        )),
        ("hypothesis_review_focus_intent",
            mi.hypothesis_review_focus_intent("do thing")),
        ("runtime_review_uncommitted_intent",
            mi.runtime_review_uncommitted_intent(focus="races")),
        ("cross_plan_intent", mi.cross_plan_intent(
            "do thing", paths_list="  [api] /p/api",
            cross_artifacts_dir="/c/dir",
        )),
        ("cross_plan_review_focus_intent",
            mi.cross_plan_review_focus_intent(
                "do thing", aliases="api, web", artifact_block="plan body",
            )),
    ]

    leaks: list[tuple[str, int, str, str]] = []
    for name, rendered in samples:
        for i, line in enumerate(rendered.splitlines(), 1):
            for match in _TOPOLOGY_TERMS_CONTRACT_BODIES.findall(line):
                leaks.append((name, i, match, line.strip()))
    assert not leaks, (
        "Orchestrator topology terms found in minimal intent renders.\n"
        "Minimal intents are code-owned but must speak agent "
        "vocabulary — pipeline topology has no operative meaning "
        "for the agent.\n"
        + "\n".join(f"  {n}:{i} {m!r}: {line}" for n, i, m, line in leaks)
    )


# ---------------------------------------------------------------------------
# A5.2a: prompt builders carry no runtime-role fallback.
# ---------------------------------------------------------------------------


def test_no_runtime_role_fallback_in_prompt_builders() -> None:
    """Prompt rendering must not consult runtime ``AgentRole`` names.

 A5.2a removed the bridge that used to translate old dispatch slots
 into prompt personas:

 **Prompt roles** (``systems_architect`` /
 ``implementation_engineer`` / ``code_reviewer`` /
 ``product_owner``, plus project overrides) drive persona
 selection. They live on ``PromptSpec.role``.
 Older ``AgentRole`` dispatch vocabulary is not part of prompt
 composition and profile steps do not expose it as a prompt
 fallback.

 No mapping, no fallback, no convenience hook. A spec without an
 explicit prompt role raises at ``part_names()`` time.

 This test pins the boundary in code: ``pipeline/prompts/builders.py``
 must not reference ``role_fallback`` / ``fallback_role`` / any
 runtime-role literal as a prompt-rendering input. If a future
 edit tries to re-introduce the leak (even as a "small
 convenience"), CI fails here.
 """
    builders_path = (
        Path(__file__).parents[4]
        / "pipeline" / "prompts" / "builders.py"
    )
    source = builders_path.read_text()

    forbidden_patterns = (
        "role_fallback",          # legacy builder parameter
        "fallback_role",          # legacy composer kwarg
        '"developer"',            # runtime role as literal default
        '"architect"',            # runtime role as literal default
        '"reviewer"',             # runtime role as literal default
    )
    leaks: list[tuple[int, str, str]] = []
    for i, line in enumerate(source.splitlines(), 1):
        for pat in forbidden_patterns:
            if pat in line:
                leaks.append((i, pat, line.strip()))
    assert not leaks, (
        "pipeline/prompts/builders.py must not carry runtime-role "
        "fallback or literal runtime-role role names. Builders use "
        "prompt-taxonomy roles via PromptSpec.role.\n"
        + "\n".join(f"  line {i} {p!r}: {line}" for i, p, line in leaks)
    )


# ---------------------------------------------------------------------------
# M3: protected contract blocks are code-owned and have no project /
# workspace override slot. The user-editable layer
# (``_prompts/{roles,tasks,formats}/``) cannot host a file that
# replaces a system-tail contract — that is the boundary that keeps
# parser-critical text out of reach of project overrides.
# ---------------------------------------------------------------------------


def test_no_override_slot_exists_for_protected_contract_names() -> None:
    """No project / workspace override slot exists for any protected
    contract name. The system-tail layer is code-owned.

    Project overrides resolve under ``.orcho/multiagent/prompts/{roles,
    tasks,formats}/<name>.md``. None of the protected-contract names
    (``plan_json``, ``review_json``, ``release_json``,
    ``change_handoff``, ``review_target``, ``authoring_language``,
    ``plan_artifact_boundary``, ``cross_subtask_blocks``,
    ``coding_agent_compaction``) may exist as a markdown file under
    any of those subdirectories — an accidental file there would
    otherwise look like a "valid" override but get silently ignored,
    hiding intent.
    """
    protected_names = {
        "plan_json",
        "review_json",
        "release_json",
        "change_handoff",
        "review_target",
        "authoring_language",
        "plan_artifact_boundary",
        "cross_subtask_blocks",
        "coding_agent_compaction",
    }
    leaks: list[Path] = []
    for sub in ("roles", "tasks", "formats"):
        for name in protected_names:
            candidate = _PROMPTS_DIR / sub / f"{name}.md"
            if candidate.exists():
                leaks.append(candidate)
    assert not leaks, (
        "Protected contract names must not appear as user-editable "
        "prompt files; their owners are factories under "
        "pipeline.prompts.contracts. Move the file out of _prompts/.\n"
        + "\n".join(f"  {p}" for p in leaks)
    )


def test_operator_waiver_part_carries_code_owned_reconciliation_policy() -> None:
    """The ``continue_with_waiver`` channel is boundary-safe: the operator
    verdict rides as a typed ``operator_waiver`` TURN part (``source=
    "operator"``, distinct from reviewer critique) and the reconciliation
    policy that tells the reviewer not to reopen waived findings is
    injected from the code-owned ``contracts`` factory — there is no
    user-editable role/task/format override slot for it, so a project
    prompt override cannot weaken or drop the rule."""
    from pipeline import prompts

    env = prompts.runtime_review_uncommitted_prompt(
        "check change", project_dir="/proj",
        operator_waiver="Operator verdict: accepted risk.",
    ).envelope()
    assert env is not None

    waiver_parts = [p for p in env.parts if p.name == "operator_waiver"]
    assert len(waiver_parts) == 1
    part = waiver_parts[0]
    # Distinct ownership marker — not user-editable, not reviewer critique.
    assert part.source == "operator"
    # Code-owned policy is embedded in the body and cannot be overridden.
    assert "OPERATOR WAIVER" in part.body
    assert "MUST NOT reopen the waived" in part.body
    assert "Operator verdict: accepted risk." in part.body

    # No user-editable override slot for the reconciliation policy.
    for sub in ("roles", "tasks", "formats"):
        assert not (_PROMPTS_DIR / sub / "operator_waiver.md").exists()


def test_protected_blocks_carry_code_owned_source_at_the_seam() -> None:
    """Each protected contract block surfaces at the builder gateway
    as a ``PromptPart`` with ``source="code-owned"`` — the marker the
    transcript renderer and the M2 envelope partitioner use to tell
    apart user-editable parts from code-owned ones.

    Asserted directly through ``_render_prompt_output``'s sidecar so
    the test does not depend on which specific phase produces the
    block, only that the gateway preserves the ownership marker.
    """
    from pipeline import prompts
    from pipeline.plugins import PluginConfig

    env = prompts.plan_prompt("Fix calc.add", "/proj", PluginConfig()).envelope()
    assert env is not None

    system_tail_parts = [p for p in env.parts if p.kind == "system_tail"]
    assert system_tail_parts, "expected at least one system_tail part"
    for part in system_tail_parts:
        assert part.source == "code-owned", (
            f"system_tail part {part.name!r} lost its code-owned "
            f"source marker (got {part.source!r})"
        )
