"""
pipeline/agent_resolver.py — Resolve the executing agent for a single SubTask.

The team-lead PLAN can attach a ``skill`` and/or a direct ``model`` override to
each subtask. At execution time the runner asks this module which concrete
``IAgentRuntime`` to instantiate and what skill package to feed into the
prompt builder.

Resolution split (R9 / R10 invariants):

* **Skill metadata never selects runtime or model.** A :class:`SkillPackage`
  is portable instructional content; only the runtime resolver chooses
  the execution mechanism.
* **Runtime chain** (highest first)::

      runtime_override                                # PhaseStep.overrides["runtime"]
        or fallback_runtime                           # AppConfig phase-derived
        or "claude"

* **Model chain** — architect-authored ``subtask.model`` is the only
  per-subtask override; it is the explicit exception to "skill metadata
  cannot select execution policy"::

      subtask.model
        or fallback_model                             # AppConfig phase-derived

When ``subtask.skill`` references an unknown skill, the resolver records
the unresolved name and falls back to the runtime/model chain without a
skill body. This keeps a single typo from aborting a 50-task DAG;
DECOMPOSE_QA is the right place to catch unknown skills before execution
if strictness is desired.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.entities import SubTask
from agents.protocols import IAgentRuntime
from agents.registry import AgentRegistry
from pipeline.plugins import PluginConfig
from pipeline.skills import SkillBinding, SkillPackage


@dataclass(frozen=True)
class ResolvedAgent:
    """Outcome of resolving a SubTask against the skill registry + phase fallback.

    ``agent`` is the live instance the runner should call. ``runtime`` and
    ``model`` are the resolved coordinates for telemetry and per-subtask
    checkpoints. ``skill`` is the :class:`SkillPackage` actually used (may
    be ``None`` if the subtask did not reference one or the reference was
    unresolved); its ``body`` is what the prompt builder injects via
    :func:`pipeline.skills.render_skill_block`. ``binding`` carries the
    audit record (``activation="architect_selected"``) when a skill was
    bound; the dag runner accumulates these into
    :class:`pipeline.dag_runner.DagRunResult`.
    """
    agent: IAgentRuntime
    runtime: str
    model: str
    skill: SkillPackage | None
    skill_unresolved: str | None  # name the architect requested but couldn't be resolved
    binding: SkillBinding | None


def resolve_subtask_agent(
    subtask: SubTask,
    plugin: PluginConfig,
    registry: AgentRegistry,
    *,
    runtime_override: str | None = None,
    fallback_runtime: str,
    fallback_model: str,
) -> ResolvedAgent:
    """Pick the (runtime, model, skill) tuple for ``subtask`` and instantiate.

    Args:
        subtask: parsed SubTask from the architect's plan.
        plugin: project plugin config (carries ``skill_registry`` and
            project context).
        registry: agent runtime registry.
        runtime_override: ``PhaseStep.overrides["runtime"]`` from the
            implement step. Highest-priority entry in the runtime chain.
        fallback_runtime: phase-derived default runtime (typically the
            implement phase agent's runtime, which already incorporates
            ``AppConfig.phase_runtime_map``).
        fallback_model: phase-derived default model.
    """
    skill, unresolved = _lookup_skill(subtask.skill, plugin)

    runtime = (
        runtime_override
        or fallback_runtime
        or "claude"
    )
    model = subtask.model or fallback_model

    agent = registry.resolve(model, runtime)

    binding: SkillBinding | None = None
    if skill is not None:
        binding = SkillBinding(
            skill_name=skill.name,
            activation="architect_selected",
            source=skill.source,
            checksum=skill.checksum,
            subtask_id=subtask.id,
        )

    return ResolvedAgent(
        agent=agent,
        runtime=runtime,
        model=model,
        skill=skill,
        skill_unresolved=unresolved,
        binding=binding,
    )


def _lookup_skill(
    name: str | None,
    plugin: PluginConfig,
) -> tuple[SkillPackage | None, str | None]:
    """Return ``(skill_or_none, unresolved_name_or_none)``.

    ``unresolved_name_or_none`` lets the runner emit a single warning per
    bad reference without making lookup a hard failure — DECOMPOSE_QA can
    promote it to a failure later if needed.
    """
    if not name:
        return None, None
    pkg = plugin.skill_registry.get(name)
    if pkg is None:
        return None, name
    return pkg, None
