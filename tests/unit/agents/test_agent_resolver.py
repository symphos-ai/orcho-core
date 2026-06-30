"""
Per-subtask agent resolution.

Covers the runtime / model resolution chain after

 Runtime: runtime_override (PhaseStep.overrides["runtime"])
 > fallback_runtime
 > "claude"

 Model: subtask.model > fallback_model

Skill metadata never selects runtime or model — only ``subtask.model``
(authored by the architect) is an explicit per-subtask override.
"""

from __future__ import annotations

from pathlib import Path

from agents.entities import SubTask
from agents.registry import AgentRegistry
from pipeline.agent_resolver import resolve_subtask_agent
from pipeline.plugins import PluginConfig
from pipeline.skills import SkillPackage

# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeAgent:
    def __init__(self, model: str, runtime: str):
        self.model = model
        self.runtime = runtime
        self.session_id = None

    def run(self, prompt, cwd, *, continue_session=False):  # pragma: no cover - not exercised
        return ""

    def reset_session(self):  # pragma: no cover
        pass


def _registry_capturing(captured: list[tuple[str, str]]) -> AgentRegistry:
    """Build a registry where developer factories record what was asked for."""
    r = AgentRegistry()

    def factory(runtime_name: str):
        def make(model: str, _effort: str | None = None):
            captured.append((runtime_name, model))
            return _FakeAgent(model=model, runtime=runtime_name)
        return make

    r.register("claude", factory("claude"))
    r.register("codex", factory("codex"))
    r.register("gemini", factory("gemini"))
    return r


def _skill_pkg(
    name: str = "alpha", description: str = "alpha skill",
) -> SkillPackage:
    """Build a minimal portable SkillPackage. Skill never carries
 runtime/model — that is the whole point of the R9 invariant we're
 asserting here."""
    return SkillPackage(
        name=name,
        description=description,
        root_dir=Path("/tmp/skills") / name,
        skill_md_path=Path("/tmp/skills") / name / "SKILL.md",
        body=f"BODY for {name}",
        frontmatter={"name": name, "description": description},
        source="project",
        checksum=f"sha256:{name}",
    )


def _plugin(
    *,
    skills: dict[str, SkillPackage] | None = None,
) -> PluginConfig:
    p = PluginConfig()
    if skills:
        p.skill_registry = skills
    return p


# ── Model chain ───────────────────────────────────────────────────────────────

def test_subtask_model_wins_over_fallback() -> None:
    captured: list[tuple[str, str]] = []
    registry = _registry_capturing(captured)
    sub = SubTask(id="t1", goal="g", model="claude-opus-explicit")

    resolved = resolve_subtask_agent(
        sub, _plugin(), registry,
        fallback_runtime="claude", fallback_model="claude-haiku",
    )

    assert resolved.model == "claude-opus-explicit"
    assert captured == [("claude", "claude-opus-explicit")]


def test_fallback_model_used_when_subtask_omits_model() -> None:
    captured: list[tuple[str, str]] = []
    registry = _registry_capturing(captured)
    sub = SubTask(id="t1", goal="g")

    resolved = resolve_subtask_agent(
        sub, _plugin(), registry,
        fallback_runtime="claude", fallback_model="claude-sonnet",
    )

    assert resolved.model == "claude-sonnet"
    assert resolved.runtime == "claude"


# ── Runtime chain ─────────────────────────────────────────────────────────────

def test_runtime_override_beats_fallback() -> None:
    """``PhaseStep.overrides["runtime"]`` wins the runtime chain."""
    captured: list[tuple[str, str]] = []
    registry = _registry_capturing(captured)
    sub = SubTask(id="t1", goal="g")

    resolved = resolve_subtask_agent(
        sub, _plugin(), registry,
        runtime_override="gemini",
        fallback_runtime="claude",
        fallback_model="m",
    )

    assert resolved.runtime == "gemini"
    assert captured == [("gemini", "m")]


def test_fallback_runtime_used_when_no_step_override() -> None:
    """No step-level override preserves the AppConfig-derived fallback."""
    captured: list[tuple[str, str]] = []
    registry = _registry_capturing(captured)
    plugin = _plugin()
    sub = SubTask(id="t1", goal="g")

    resolved = resolve_subtask_agent(
        sub, plugin, registry,
        runtime_override=None,
        fallback_runtime="codex",
        fallback_model="m",
    )

    assert resolved.runtime == "codex"


# ── Skill resolution (R9 portability) ─────────────────────────────────────────

def test_skill_does_not_change_runtime_or_model() -> None:
    """SkillPackage carries no provider/model — it must not perturb the
 resolved (runtime, model) pair regardless of which skill is bound."""
    captured: list[tuple[str, str]] = []
    registry = _registry_capturing(captured)
    plugin = _plugin(skills={"backend": _skill_pkg("backend")})
    sub = SubTask(id="t1", goal="g", skill="backend")

    resolved = resolve_subtask_agent(
        sub, plugin, registry,
        fallback_runtime="claude", fallback_model="m",
    )

    assert resolved.skill is not None and resolved.skill.name == "backend"
    assert resolved.runtime == "claude"
    assert resolved.model == "m"


def test_skill_binding_recorded_when_skill_resolves() -> None:
    captured: list[tuple[str, str]] = []
    registry = _registry_capturing(captured)
    plugin = _plugin(skills={"alpha": _skill_pkg("alpha")})
    sub = SubTask(id="task-7", goal="g", skill="alpha")

    resolved = resolve_subtask_agent(
        sub, plugin, registry,
        fallback_runtime="claude", fallback_model="m",
    )

    assert resolved.binding is not None
    assert resolved.binding.skill_name == "alpha"
    assert resolved.binding.activation == "architect_selected"
    assert resolved.binding.subtask_id == "task-7"
    assert resolved.binding.checksum == "sha256:alpha"


def test_no_binding_when_subtask_has_no_skill() -> None:
    captured: list[tuple[str, str]] = []
    registry = _registry_capturing(captured)
    sub = SubTask(id="t", goal="g")

    resolved = resolve_subtask_agent(
        sub, _plugin(), registry,
        fallback_runtime="claude", fallback_model="m",
    )

    assert resolved.skill is None
    assert resolved.binding is None
    assert resolved.skill_unresolved is None


def test_unknown_skill_name_recorded_and_falls_back() -> None:
    captured: list[tuple[str, str]] = []
    registry = _registry_capturing(captured)
    plugin = _plugin(skills={"real": _skill_pkg("real")})
    sub = SubTask(id="t", goal="g", skill="typo")

    resolved = resolve_subtask_agent(
        sub, plugin, registry,
        fallback_runtime="claude", fallback_model="fallback-model",
    )

    assert resolved.skill is None
    assert resolved.binding is None
    assert resolved.skill_unresolved == "typo"
    assert resolved.model == "fallback-model"
