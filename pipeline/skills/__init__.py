"""pipeline.skills — Agent Skills package model (R9 redesign).

Skills are **portable instruction packages** following the Agent Skills
open standard (SKILL.md frontmatter + directory layout). orcho consumes
skills produced for any compatible client (Claude Code, Forge, OpenAI),
without requiring an orcho-specific format.

Phase 1 (this commit) introduces the type contracts. Phase 7 wires the
discovery chain, loader, and prompt injection.

See `docs/adr/0010-agent-skills-standard.md` (Phase 7) for rationale.
"""
from pipeline.skills.discover import ENTRY_POINTS_GROUP, discover_skills
from pipeline.skills.inject import (
    record_skill_binding,
    render_roster,
    render_skill_block,
)
from pipeline.skills.loader import (
    SkillParseError,
    discover_skills_in_root,
    load_skill_package,
    parse_skill_md,
)
from pipeline.skills.migrate import (
    LegacySkillMigrationReport,
    MigratedSkill,
    migrate_legacy_skills,
)
from pipeline.skills.runtime_scope import (
    configure_agent_skill_scope,
    configure_phase_agent_skill_scope,
)
from pipeline.skills.types import (
    ResourceManifestEntry,
    SkillBinding,
    SkillPackage,
    SkillResourceBinding,
    SkillTrustPolicy,
)

__all__ = [
    "ENTRY_POINTS_GROUP",
    "LegacySkillMigrationReport",
    "MigratedSkill",
    "ResourceManifestEntry",
    "SkillBinding",
    "SkillPackage",
    "SkillParseError",
    "SkillResourceBinding",
    "SkillTrustPolicy",
    "configure_agent_skill_scope",
    "configure_phase_agent_skill_scope",
    "discover_skills",
    "discover_skills_in_root",
    "load_skill_package",
    "migrate_legacy_skills",
    "parse_skill_md",
    "record_skill_binding",
    "render_roster",
    "render_skill_block",
]
