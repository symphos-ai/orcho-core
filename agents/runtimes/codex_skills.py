"""Provider-native Codex skill-scope projection.

Orcho owns its skill discovery policy, while the Codex CLI independently scans
``$HOME/.agents/skills``. This module projects Orcho's user-scope decision onto
Codex's official per-skill ``skills.config`` override without changing
``HOME``/``CODEX_HOME`` (and therefore without disturbing authentication,
configuration, or resumable session storage).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodexSkillScope:
    """Effective provider-native skill scope for one Codex runtime."""

    include_user_skills: bool = False

    def config_args(self, *, home_dir: Path | None = None) -> list[str]:
        """Return Codex CLI config args enforcing this scope.

        Codex currently exposes per-skill enablement, not a source-level user
        toggle. When user skills are outside the effective Orcho scope, emit
        one deterministic array override disabling every directly installed
        ``$HOME/.agents/skills/<name>/SKILL.md`` package.
        """
        if self.include_user_skills:
            return []
        skill_files = _user_skill_files(home_dir or Path.home())
        if not skill_files:
            return []
        entries = ",".join(
            f"{{path={json.dumps(str(path))},enabled=false}}" for path in skill_files
        )
        return ["-c", f"skills.config=[{entries}]"]


def _user_skill_files(home_dir: Path) -> tuple[Path, ...]:
    """List canonical user skill entry files in deterministic order."""
    root = home_dir / ".agents" / "skills"
    try:
        children = tuple(root.iterdir())
    except OSError:
        return ()
    return tuple(
        sorted(
            (
                child / "SKILL.md"
                for child in children
                if child.is_dir() and (child / "SKILL.md").is_file()
            ),
            key=lambda path: str(path),
        ),
    )
