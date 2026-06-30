"""pipeline.skills.types — Phase 1 type contracts for the R9 skill model.

Skill = portable instructions package (Agent Skills SKILL.md format).
Profile / PhaseStep / per-phase runtime config = orcho execution policy.

Skills NEVER select runtime / provider / model. Execution mechanism is
owned by orcho-core; skills supply only the instructional content.
``metadata.orcho.runtime`` rogue frontmatter — ignored / warned.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResourceManifestEntry:
    """One resource entry in a SkillPackage manifest.

    Body is NOT hashed at discovery (progressive disclosure preserved —
    architect roster sees only name+description, full body at execution,
    resources read on-demand).
    """
    relative_path: str   # relative to SkillPackage.root_dir
    size_bytes: int
    mtime_ns: int        # filesystem mtime for cheap change detection


@dataclass(frozen=True)
class SkillResourceBinding:
    """Recorded ONLY when a resource was actually loaded by the agent
    during execution. Captures content hash at load time.

    Persisted in ``session['skill_resource_bindings']``.
    """
    skill_name: str
    relative_path: str
    sha256: str          # hashed at load (after cat scripts/foo.py etc.)
    size_bytes: int
    loaded_at_phase: str | None = None
    loaded_at_subtask_id: str | None = None


@dataclass(frozen=True)
class SkillPackage:
    """Portable Agent Skills package. Loaded once at project setup;
    immutable for run reproducibility.

    ``checksum`` covers the canonical SKILL.md (frontmatter + body) plus
    the resource manifest entries (relative_path + size + mtime).
    Resource bodies are NOT included in checksum at discovery time —
    progressive disclosure means we don't read full resource content
    unless the agent loads it (recorded as SkillResourceBinding).
    """
    name: str
    description: str
    root_dir: Path
    skill_md_path: Path
    body: str                                  # markdown content (no frontmatter)
    frontmatter: dict[str, Any]                # parsed YAML frontmatter
    resources: tuple[Path, ...] = ()           # scripts/ + references/ + assets/
    source: str = "unknown"                    # "package:<id>" | "user" | "project"
                                               # | "claude-compat" | "forge-compat"
    checksum: str = ""
    resource_manifest: tuple[ResourceManifestEntry, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("SkillPackage.name required")
        if not self.description or not self.description.strip():
            raise ValueError(
                f"SkillPackage {self.name!r}: description required (Agent "
                "Skills mandates description for selection)"
            )


@dataclass(frozen=True)
class SkillBinding:
    """Event record: skill X applied to phase/subtask Y. Persisted in
    ``session['skill_bindings']`` and emitted as ``skill.activated`` event
    for reproducibility audit.
    """
    skill_name: str
    activation: str       # "explicit" | "architect_selected" | "user_requested"
    source: str           # mirrors SkillPackage.source
    checksum: str         # mirrors SkillPackage.checksum at binding time
    phase: str | None = None
    subtask_id: str | None = None


@dataclass(frozen=True)
class SkillTrustPolicy:
    """Per-source trust gate for skill loading (autonomous-run security).

    Defaults: package + user + workspace skills load by default
    (reasonably trusted: author signs entry_points packages, user controls
    own home dir). Project and compat sources are OFF by default —
    cloned untrusted projects can ship malicious SKILL.md instructing
    the agent to leak data, exfil credentials, etc.

    Opt-in via:
      * CLI flag ``--trust-project-skills``
      * Config ``skill_trust.trust_project = true`` in
        ``_config/config.local.json``
      * ENV ``ORCHO_TRUST_PROJECT_SKILLS=1``
    """
    trust_packages: bool = True
    trust_user: bool = True
    trust_workspace: bool = True
    trust_project: bool = False
    trust_compat_claude: bool = False
    trust_compat_forge: bool = False
