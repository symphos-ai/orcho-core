"""pipeline.cross_project.types — Phase 1 type contracts.

R5 refactor (per Codex first-pass review): ContractValidation reads
**artifacts** through ``ArtifactSelector`` list, not raw session dicts.
Sessions persist on disk per project sub-run; cross-project state in
memory carries only ``ProjectRunRef``s (alias → ref).

Validation invariants enforced at construction:
  * alias matches /^[a-z][a-z0-9_]*$/ (filesystem-safe)
  * canonical project_dir uniqueness (caught at CrossProjectProfile init)
  * depends_on closures + cycle check (Phase 1 stub; full Kahn check
    arrives with Milestone 13's plan_parser integration)
  * contract names unique
  * fires_after refers to known aliases
  * inputs reference known aliases
  * parallelism ≥ 1
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pipeline.runtime import (
    AgentRole,
    FailStrategy,
    GateKind,
    HumanReview,
    QualityGate,
)


class ProjectStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"     # dependency failed → never started


class WhenPolicy(StrEnum):
    """Precondition semantics for contract firing (per Codex R6)."""
    ALL_SUCCEEDED = "all_succeeded"   # all pre-projects must succeed
    ALL_FINISHED = "all_finished"     # finished (succeeded OR failed)
    ANY_FINISHED = "any_finished"     # at least one finished


class BlockedPolicy(StrEnum):
    """What to do when a contract's `when` condition is unsatisfiable."""
    SKIP = "skip"   # contract logged as blocked, runtime continues
    FAIL = "fail"   # contract marked failed, on_fail strategy applies
    HALT = "halt"   # cross-project run halts immediately


@dataclass(frozen=True)
class ProjectRunRef:
    """Stable in-memory reference to a project sub-run. Replaces the
    legacy ``sessions: dict[alias, dict]`` shape that bled session
    internals into cross-project orchestrator state (R5).

    Contract validators read artifacts via ``artifact_index[name]`` →
    relative path under ``project_dir``.
    """
    alias: str
    run_id: str
    project_dir: str
    artifact_index: dict[str, str]  # logical name → relative path
    status: ProjectStatus
    failed_phase: str | None = None  # populated when status=FAILED


@dataclass(frozen=True)
class ArtifactSelector:
    """Declarative reference to a named artifact for contract validation.

    Validator resolves to concrete path through
    ``ProjectRunRef.artifact_index[artifact_name]``.
    """
    project_alias: str
    artifact_name: str       # key in artifact_index
    optional: bool = False   # if missing → skip contract instead of fail


@dataclass(frozen=True)
class ProjectStep:
    """One project in a cross-project run. Analogous to PhaseStep but
    at the whole-project level.

    Invariants:
      * alias matches /^[a-z][a-z0-9_]*$/ (filesystem-safe identifier)
      * project_dir non-empty
      * no self-dependency in depends_on
      * overrides keys whitelisted (model, effort, dry_run, max_rounds)
        — not arbitrary
    """
    alias: str
    project_dir: str
    profile: str                              # profile name (e.g. "feature")
    task_template: str
    depends_on: tuple[str, ...] = ()
    overrides: dict[str, Any] | None = None

    _ALIAS_RE: ClassVar[re.Pattern] = re.compile(r"^[a-z][a-z0-9_]*$")
    _OVERRIDE_KEYS: ClassVar[frozenset] = frozenset({
        "model", "effort", "dry_run", "max_rounds",
    })

    def __post_init__(self) -> None:
        if not self._ALIAS_RE.match(self.alias):
            raise ValueError(
                f"ProjectStep.alias {self.alias!r} invalid: must match "
                f"/^[a-z][a-z0-9_]*$/"
            )
        if not self.project_dir.strip():
            raise ValueError(f"ProjectStep {self.alias!r}: project_dir required")
        if self.alias in self.depends_on:
            raise ValueError(f"ProjectStep {self.alias!r}: self-dependency")
        if self.overrides:
            unknown = set(self.overrides) - self._OVERRIDE_KEYS
            if unknown:
                raise ValueError(
                    f"ProjectStep {self.alias!r}: unknown override keys "
                    f"{sorted(unknown)}, allowed: {sorted(self._OVERRIDE_KEYS)}"
                )


@dataclass(frozen=True)
class ContractValidation:
    """Cross-project verification gate. Reads artifacts (R5) via
    ``ArtifactSelector`` list. Symmetrical with ``QualityGate`` but
    scope = inter-project + artifact-based inputs.
    """
    name: str
    kind: GateKind
    on_fail: FailStrategy
    inputs: tuple[ArtifactSelector, ...]
    fires_after: tuple[str, ...]                 # project aliases (DAG)
    when: WhenPolicy = WhenPolicy.ALL_SUCCEEDED   # R6
    on_blocked: BlockedPolicy = BlockedPolicy.SKIP
    config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ContractValidation.name required")
        if not self.fires_after:
            raise ValueError(
                f"ContractValidation {self.name!r}: fires_after cannot be empty"
            )
        if not self.inputs:
            raise ValueError(
                f"ContractValidation {self.name!r}: inputs required (artifact-"
                "coupled per R5)"
            )


@dataclass(frozen=True)
class CrossPlanStep:
    """Initial cross-project planning step (R5). Workspace-level architect
    agent reads all project_dirs + workspace_skill + user task; produces
    per-project task derivation and initial contract spec list.

    Output stored as cross_plan.md in run_dir.
    """
    skill: str | None = None              # workspace-level skill name
    role: AgentRole = AgentRole.ARCHITECT
    prompt_template: str | None = None
    quality_gates: tuple[QualityGate, ...] = ()
    human_review: HumanReview | None = None


@dataclass(frozen=True)
class ContractResult:
    """Outcome of one ContractValidation invocation. Persisted in
    cross-project session for audit.
    """
    contract_name: str
    passed: bool
    output: str
    duration_s: float
    kind: GateKind
    when_satisfied: bool = True
    cost_usd: float | None = None


def _all_ordered_by_depends_on(
    aliases: list[str],
    projects: tuple[ProjectStep, ...],
) -> bool:
    """Return True iff every pair of `aliases` is ordered through the
    transitive depends_on closure (no parallel waves on shared repo).
    Helper for CrossProjectProfile.__post_init__ duplicate-project_dir check.
    """
    by_alias = {p.alias: p for p in projects}

    def transitive_deps(start: str) -> set[str]:
        visited: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            step = by_alias.get(current)
            if step is None:
                continue
            stack.extend(step.depends_on)
        return visited

    for i, a in enumerate(aliases):
        for b in aliases[i + 1:]:
            if b not in transitive_deps(a) and a not in transitive_deps(b):
                return False
    return True


@dataclass(frozen=True)
class CrossProjectProfile:
    """Orchestrates N project sub-runs + cross-project contract
    validations + optional cross-planning phase.
    """
    name: str
    description: str
    projects: tuple[ProjectStep, ...]
    contracts: tuple[ContractValidation, ...] = ()
    planning: CrossPlanStep | None = None
    parallelism: int = 1

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("CrossProjectProfile.name required")
        if not self.projects:
            raise ValueError(f"CrossProjectProfile {self.name!r}: no projects")
        if self.parallelism < 1:
            raise ValueError(
                f"CrossProjectProfile {self.name!r}: parallelism must be ≥1, "
                f"got {self.parallelism}"
            )

        aliases = [p.alias for p in self.projects]
        if len(set(aliases)) != len(aliases):
            duplicates = {a for a in aliases if aliases.count(a) > 1}
            raise ValueError(
                f"CrossProjectProfile {self.name!r}: duplicate aliases "
                f"{sorted(duplicates)}"
            )

        # N6: catch duplicate canonical project_dir UNLESS chained by depends_on
        from pathlib import Path as _Path
        canonical = {p.alias: str(_Path(p.project_dir).resolve()) for p in self.projects}
        rev: dict[str, list[str]] = {}
        for alias, c in canonical.items():
            rev.setdefault(c, []).append(alias)
        for canon_dir, dup_aliases in rev.items():
            if (len(dup_aliases) > 1
                    and not _all_ordered_by_depends_on(dup_aliases, self.projects)):
                raise ValueError(
                    f"CrossProjectProfile {self.name!r}: aliases "
                    f"{dup_aliases} share canonical project_dir "
                    f"{canon_dir} without depends_on ordering — would "
                    "race on artifact writes / git commits. Either "
                    "dedupe or chain via depends_on."
                )

        contract_names = [c.name for c in self.contracts]
        if len(set(contract_names)) != len(contract_names):
            duplicates = {n for n in contract_names if contract_names.count(n) > 1}
            raise ValueError(
                f"CrossProjectProfile {self.name!r}: duplicate contract names "
                f"{sorted(duplicates)}"
            )

        for p in self.projects:
            unknown = [d for d in p.depends_on if d not in aliases]
            if unknown:
                raise ValueError(
                    f"ProjectStep {p.alias!r} depends_on unknown aliases: "
                    f"{unknown}"
                )

        # Kahn's algorithm: detect cycles in the depends_on DAG.
        in_degree: dict[str, int] = {p.alias: 0 for p in self.projects}
        for p in self.projects:
            for _dep in p.depends_on:
                in_degree[p.alias] += 1
        ready = [a for a, deg in in_degree.items() if deg == 0]
        visited: set[str] = set()
        while ready:
            node = ready.pop(0)
            if node in visited:
                continue
            visited.add(node)
            for p in self.projects:
                if node in p.depends_on and p.alias not in visited:
                    in_degree[p.alias] -= 1
                    if in_degree[p.alias] == 0:
                        ready.append(p.alias)
        if len(visited) != len(aliases):
            unvisited = sorted(set(aliases) - visited)
            raise ValueError(
                f"CrossProjectProfile {self.name!r}: depends_on contains a "
                f"cycle involving aliases {unvisited}"
            )

        for c in self.contracts:
            unknown_after = [a for a in c.fires_after if a not in aliases]
            if unknown_after:
                raise ValueError(
                    f"ContractValidation {c.name!r}: fires_after refers to "
                    f"unknown aliases: {unknown_after}"
                )
            for sel in c.inputs:
                if sel.project_alias not in aliases:
                    raise ValueError(
                        f"ContractValidation {c.name!r}: input refers to "
                        f"unknown alias {sel.project_alias!r}"
                    )
