"""Mock runtime plugin hints use the canonical read-only plugin loader."""

from pathlib import Path

from agents.runtimes._strategy import _load_plugin_hints


def test_plugin_hints_do_not_write_bytecode_into_project(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".orcho" / "multiagent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        "PLUGIN = {"
        "'name': 'Smoke', "
        "'language': 'Python', "
        "'file_hints': ['src/']"
        "}\n",
        encoding="utf-8",
    )

    assert _load_plugin_hints(str(tmp_path)) == ("Smoke", ["src/"], "Python")
    assert not (plugin_dir / "__pycache__").exists()


def test_missing_plugin_keeps_empty_mock_hint_context(tmp_path: Path) -> None:
    assert _load_plugin_hints(str(tmp_path)) == ("", [], "")
