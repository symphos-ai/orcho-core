"""
codemap builder + prompt injection.

These tests exercise the regex-based fallback parsers in core.context. The
optional tree-sitter path adds accuracy but isn't required for correctness;
the regex path runs unconditionally and has to handle the common shapes for
each language we support.

The output format is line-oriented and stable, so we assert on substrings
rather than full equality — that keeps tests resilient when we tighten the
header line later.
"""

import textwrap
from pathlib import Path

from core.context import build_repo_map, inject_context


# ────────────────────────────────────────────────────────────────────────────
#  build_repo_map — language coverage
# ────────────────────────────────────────────────────────────────────────────
class TestBuildRepoMapPython:
    def test_finds_top_level_class(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text("class Foo:\n    pass\n")
        out = build_repo_map(tmp_path)
        assert "class Foo" in out
        assert "mod.py" in out

    def test_finds_methods_qualified_with_class(self, tmp_path: Path) -> None:
        (tmp_path / "svc.py").write_text(textwrap.dedent("""
            class Service:
                def do_work(self): pass
                def helper(self): pass
        """))
        out = build_repo_map(tmp_path)
        assert "method Service.do_work" in out
        assert "method Service.helper" in out

    def test_top_level_function_not_classified_as_method(self, tmp_path: Path) -> None:
        (tmp_path / "tools.py").write_text(textwrap.dedent("""
            def standalone():
                pass

            class Box:
                def open(self): pass
        """))
        out = build_repo_map(tmp_path)
        assert "function standalone" in out
        assert "method Box.open" in out

    def test_nested_class_does_not_eat_outer(self, tmp_path: Path) -> None:
        (tmp_path / "n.py").write_text(textwrap.dedent("""
            class Outer:
                def outer_method(self): pass
                class Inner:
                    def inner_method(self): pass
                def back_to_outer(self): pass
        """))
        out = build_repo_map(tmp_path)
        assert "method Outer.outer_method" in out
        assert "method Inner.inner_method" in out
        # After the nested class scope ends, outer methods come back
        assert "method Outer.back_to_outer" in out


class TestBuildRepoMapCSharp:
    def test_finds_class_and_method(self, tmp_path: Path) -> None:
        (tmp_path / "Foo.cs").write_text(textwrap.dedent("""
            namespace App
            {
                public class Bar
                {
                    public void DoStuff() { }
                    private int Helper(int x) { return x; }
                }
            }
        """))
        out = build_repo_map(tmp_path)
        assert "class Bar" in out
        assert "method Bar.DoStuff" in out
        assert "method Bar.Helper" in out
        assert "Foo.cs" in out

    def test_struct_and_interface(self, tmp_path: Path) -> None:
        (tmp_path / "Types.cs").write_text(textwrap.dedent("""
            public interface IThing { void Run(); }
            public struct Vec { public int X; }
        """))
        out = build_repo_map(tmp_path)
        assert "class IThing" in out
        assert "class Vec" in out


class TestBuildRepoMapPhp:
    def test_finds_class_method_and_function(self, tmp_path: Path) -> None:
        (tmp_path / "code.php").write_text(textwrap.dedent("""
            <?php
            class Repo {
                public function find($id) { return null; }
                private function load() {}
            }

            function helperFn() {}
        """))
        out = build_repo_map(tmp_path)
        assert "class Repo" in out
        assert "method Repo.find" in out
        assert "method Repo.load" in out
        assert "function helperFn" in out


# ────────────────────────────────────────────────────────────────────────────
#  Walk + filtering
# ────────────────────────────────────────────────────────────────────────────
class TestWalkFiltering:
    def test_skips_known_build_dirs(self, tmp_path: Path) -> None:
        # Should be SKIPPED
        for skip in ("__pycache__", ".venv", "node_modules", "vendor", ".git"):
            d = tmp_path / skip
            d.mkdir()
            (d / "junk.py").write_text("class ShouldNotAppear: pass")

        # Should appear
        (tmp_path / "real.py").write_text("class Real: pass")

        out = build_repo_map(tmp_path)
        assert "class Real" in out
        assert "ShouldNotAppear" not in out

    def test_respects_max_depth(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "deep.py").write_text("class Deep: pass")

        # depth=1 means only root + 1 level of subdirs — nothing inside d/
        out = build_repo_map(tmp_path, max_depth=1)
        assert "Deep" not in out

        # depth=5 reaches it
        out_deep = build_repo_map(tmp_path, max_depth=5)
        assert "class Deep" in out_deep

    def test_returns_empty_when_no_matches(self, tmp_path: Path) -> None:
        (tmp_path / "data.json").write_text("{}")  # not a parseable file
        assert build_repo_map(tmp_path) == ""

    def test_returns_empty_for_missing_dir(self, tmp_path: Path) -> None:
        assert build_repo_map(tmp_path / "does_not_exist") == ""

    def test_languages_filter_narrows_scope(self, tmp_path: Path) -> None:
        (tmp_path / "py_one.py").write_text("class Py: pass")
        (tmp_path / "cs_one.cs").write_text("public class Cs {}")

        only_py = build_repo_map(tmp_path, languages=["python"])
        assert "class Py" in only_py
        assert "class Cs" not in only_py

        only_cs = build_repo_map(tmp_path, languages=["c_sharp"])
        assert "class Cs" in only_cs
        assert "class Py" not in only_cs


# ────────────────────────────────────────────────────────────────────────────
#  inject_context
# ────────────────────────────────────────────────────────────────────────────
class TestInjectContext:
    def test_empty_repo_map_returns_prompt_unchanged(self) -> None:
        prompt = "do the thing"
        assert inject_context(prompt, "") == prompt

    def test_appends_repo_map_with_delimiters(self) -> None:
        out = inject_context("do the thing", "class Foo")
        assert "do the thing" in out
        assert "REPO MAP" in out
        assert "class Foo" in out
        assert "END REPO MAP" in out

    def test_repo_map_appended_after_prompt(self) -> None:
        out = inject_context("HEAD", "TAIL_CONTENT")
        assert out.index("HEAD") < out.index("TAIL_CONTENT")

    def test_does_not_modify_input(self) -> None:
        prompt = "original"
        repo_map = "class X"
        result = inject_context(prompt, repo_map)
        # The input string should be unchanged; result is a new value
        assert prompt == "original"
        assert result != prompt
