"""Public scheduled-verification documentation remains loadable and current."""

from __future__ import annotations

import re
from pathlib import Path

from pipeline.plugins import PLUGIN_RELATIVE_PATH, PluginConfig, load_plugin
from pipeline.verification_contract import VerificationContract

ROOT = Path(__file__).resolve().parents[4]
PUBLIC_SURFACES = (
    ROOT / "README.md",
    ROOT / "docs/user/00_getting_started.md",
    ROOT / "docs/user/03_workspaces.md",
    ROOT / "docs/README.md",
    ROOT / "docs/expert/01_plugin.md",
)


def _plugin_blocks(path: Path) -> list[str]:
    return [
        block for block in re.findall(r"```python\n(.*?)```", path.read_text(), re.S)
        if "PLUGIN =" in block
    ]


def test_public_surfaces_link_to_scheduled_verification_guide() -> None:
    for path in PUBLIC_SURFACES:
        assert "scheduled_verification.md" in path.read_text(), path


def test_public_plugin_snippets_load_without_ignored_keys(tmp_path, capsys) -> None:
    snippets = [
        block
        for path in (ROOT / "README.md", ROOT / "docs/user/03_workspaces.md", ROOT / "docs/expert/01_plugin.md", ROOT / "docs/guides/scheduled_verification.md")
        for block in _plugin_blocks(path)
    ]
    assert snippets
    for index, snippet in enumerate(snippets):
        project = tmp_path / str(index)
        plugin_path = project / PLUGIN_RELATIVE_PATH
        plugin_path.parent.mkdir(parents=True)
        plugin_path.write_text(snippet)
        plugin = load_plugin(str(project))
        assert isinstance(plugin, PluginConfig)
        assert "ignoring unknown PLUGIN keys" not in capsys.readouterr().err
        if plugin.verification:
            assert VerificationContract.from_plugin(plugin) is not None


def test_public_onboarding_uses_current_verification_vocabulary() -> None:
    text = "\n".join(path.read_text() for path in PUBLIC_SURFACES)
    for stale in ("tech_stack", "test_runner", "cheap", "default_cheap"):
        assert stale not in text
    assert "build_prompt_extra=\"Run:" not in text


def test_public_cost_vocabulary_matches_the_typed_contract() -> None:
    for path in (ROOT / "README.md", ROOT / "docs/guides/scheduled_verification.md"):
        text = path.read_text()
        for cost in ("fast", "moderate", "slow", "unknown"):
            assert f"`{cost}`" in text, path

    readme = (ROOT / "README.md").read_text()
    readme_cost = readme.split("Cost is evidence metadata:", 1)[1].split(
        "See the practical", 1,
    )[0]
    guide = (ROOT / "docs/guides/scheduled_verification.md").read_text()
    cost_section = guide.split("## 3. Classify cost from evidence", 1)[1].split(
        "## 4. Group, select, and schedule gates", 1,
    )[0]
    for text in (readme_cost, cost_section):
        assert "`broad`" not in text
        assert "`manual`" not in text
