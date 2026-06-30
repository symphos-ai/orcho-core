"""coverage.

Pins prompt-injection helpers + binding recorder:

* :func:`render_roster` — empty registry, sort stability, description
 truncation.
* :func:`render_skill_block` — wrapper attribute escaping, resource
 list rendering, body whitespace handling, subtask_id attribute.
* :func:`record_skill_binding` — bucket creation, ordering, captured
 metadata (source, checksum, activation).
"""
from __future__ import annotations

from pathlib import Path

from pipeline.skills import (
    SkillBinding,
    SkillPackage,
    record_skill_binding,
    render_roster,
    render_skill_block,
)
from pipeline.skills.types import ResourceManifestEntry


def _pkg(
    *,
    name: str = "backend",
    description: str = "REST endpoints",
    body: str = "Markdown body.",
    source: str = "project",
    checksum: str = "deadbeef",
    resources: tuple[ResourceManifestEntry, ...] = (),
) -> SkillPackage:
    return SkillPackage(
        name=name,
        description=description,
        root_dir=Path("/tmp/skill"),
        skill_md_path=Path("/tmp/skill/SKILL.md"),
        body=body,
        frontmatter={"name": name, "description": description},
        resources=(),
        source=source,
        checksum=checksum,
        resource_manifest=resources,
    )


# ── render_roster ─────────────────────────────────────────────────────


class TestRenderRoster:
    def test_empty_registry_returns_empty_string(self) -> None:
        assert render_roster({}) == ""

    def test_basic_listing(self) -> None:
        pkgs = {
            "alpha": _pkg(name="alpha", description="alpha skill"),
            "beta": _pkg(name="beta", description="beta skill"),
        }
        roster = render_roster(pkgs)
        # Header present.
        assert roster.startswith(
            "Available skills (route matching subtasks by putting the exact name "
            "in the `skill` field"
        )
        # Both skills listed with name + description.
        assert "- `alpha`: alpha skill" in roster
        assert "- `beta`: beta skill" in roster

    def test_sort_stability(self) -> None:
        # Mixed insertion order; output sorted alphabetically.
        pkgs = {
            "zebra": _pkg(name="zebra", description="z"),
            "alpha": _pkg(name="alpha", description="a"),
            "mike": _pkg(name="mike", description="m"),
        }
        roster = render_roster(pkgs)
        idx_alpha = roster.index("alpha")
        idx_mike = roster.index("mike")
        idx_zebra = roster.index("zebra")
        assert idx_alpha < idx_mike < idx_zebra

    def test_long_description_truncated(self) -> None:
        long_desc = " ".join(["word"] * 200)  # ~999 chars
        pkgs = {"x": _pkg(name="x", description=long_desc)}
        roster = render_roster(pkgs)
        body_line = next(
            line for line in roster.splitlines() if line.startswith("- `x`")
        )
        assert body_line.endswith("…")
        # Truncation respects the soft limit (240 chars + "…").
        assert len(body_line) < 300


# ── render_skill_block ────────────────────────────────────────────────


class TestRenderSkillBlock:
    def test_wrapper_carries_metadata(self) -> None:
        pkg = _pkg(
            name="backend",
            source="project",
            checksum="abc123",
            body="how to do the thing",
        )
        block = render_skill_block(pkg)
        assert block.startswith("<skill_content ")
        assert 'name="backend"' in block
        assert 'source="project"' in block
        assert 'checksum="abc123"' in block
        assert "how to do the thing" in block
        assert block.rstrip().endswith("</skill_content>")

    def test_subtask_id_attribute_optional(self) -> None:
        pkg = _pkg()
        without = render_skill_block(pkg)
        assert "subtask_id" not in without

        with_id = render_skill_block(pkg, subtask_id="task-1")
        assert 'subtask_id="task-1"' in with_id

    def test_resource_list_rendered(self) -> None:
        manifest = (
            ResourceManifestEntry(
                relative_path="scripts/check.sh",
                size_bytes=10,
                mtime_ns=1,
            ),
            ResourceManifestEntry(
                relative_path="references/routing.md",
                size_bytes=10,
                mtime_ns=1,
            ),
        )
        block = render_skill_block(_pkg(resources=manifest))
        assert "<skill_resources>" in block
        assert "<file>scripts/check.sh</file>" in block
        assert "<file>references/routing.md</file>" in block
        assert "</skill_resources>" in block

    def test_no_resources_omits_block(self) -> None:
        block = render_skill_block(_pkg())
        assert "<skill_resources>" not in block

    def test_body_whitespace_trimmed(self) -> None:
        pkg = _pkg(body="\n\n  body content  \n\n")
        block = render_skill_block(pkg)
        # Trailing blank lines / whitespace stripped before closing tag.
        assert "body content" in block
        # No double-blank between body and closing wrapper.
        assert "\n\n\n" not in block

    def test_xml_special_characters_escaped(self) -> None:
        # Defensive: skill names / sources / paths should never contain
        # < > & " in practice, but the wrapper must not be breakable.
        pkg = _pkg(
            name='hostile" name',
            source="bad<source>",
            checksum="dead&beef",
        )
        block = render_skill_block(pkg)
        assert '"hostile&quot; name"' in block
        assert '"bad&lt;source&gt;"' in block
        assert '"dead&amp;beef"' in block

    def test_xml_text_escaped_in_resource_paths(self) -> None:
        manifest = (
            ResourceManifestEntry(
                relative_path="scripts/bad<file>.sh",
                size_bytes=1,
                mtime_ns=1,
            ),
        )
        block = render_skill_block(_pkg(resources=manifest))
        assert "<file>scripts/bad&lt;file&gt;.sh</file>" in block


# ── record_skill_binding ──────────────────────────────────────────────


class TestRecordSkillBinding:
    def test_creates_bucket_on_first_call(self) -> None:
        extras: dict = {}
        binding = record_skill_binding(
            extras, _pkg(), activation="explicit", phase="implement",
        )
        assert isinstance(binding, SkillBinding)
        assert extras["skill_bindings"] == [binding]

    def test_appends_to_existing_bucket(self) -> None:
        extras: dict = {}
        record_skill_binding(extras, _pkg(name="a"), activation="explicit")
        record_skill_binding(extras, _pkg(name="b"), activation="explicit")
        names = [b.skill_name for b in extras["skill_bindings"]]
        assert names == ["a", "b"]

    def test_captures_source_and_checksum(self) -> None:
        pkg = _pkg(name="x", source="package:vendor", checksum="cafe")
        extras: dict = {}
        binding = record_skill_binding(
            extras, pkg, activation="user_requested", subtask_id="t1",
        )
        assert binding.source == "package:vendor"
        assert binding.checksum == "cafe"
        assert binding.activation == "user_requested"
        assert binding.subtask_id == "t1"
        assert binding.phase is None

    def test_activation_values_pass_through(self) -> None:
        # The dataclass is a plain str field; the canonical values are
        # documented but not enum-enforced. Pin the three the plan
        # mentions to catch drift if someone narrows the field later.
        extras: dict = {}
        for activation in ("explicit", "architect_selected", "user_requested"):
            record_skill_binding(extras, _pkg(), activation=activation)
        actual = [b.activation for b in extras["skill_bindings"]]
        assert actual == ["explicit", "architect_selected", "user_requested"]

    def test_existing_extras_preserved(self) -> None:
        extras: dict = {"loop_round": 3, "skill_bindings": []}
        record_skill_binding(extras, _pkg(), activation="explicit")
        assert extras["loop_round"] == 3
        assert len(extras["skill_bindings"]) == 1
