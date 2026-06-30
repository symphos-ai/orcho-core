"""Durable machine-readable plan artefact: round-trip + hard-fail.

Pins the contract that ``pipeline.plan_artifacts`` is the canonical
machine source for a parsed plan persisted on disk:

* :func:`parsed_plan_to_dict` / :func:`parsed_plan_from_dict` is a true
  round trip — every typed REA-1 contract field, every subtask field
  (including ``done_criteria`` / ``owned_files`` / ``architectural_decision``),
  and the subtask DAG (with ``depends_on``) come back byte-for-byte
  equivalent.
* :func:`write_parsed_plan_artifact` writes BOTH ``plan_<run>_r<n>.json``
  (per-attempt sibling of the human markdown) AND ``parsed_plan.json``
  (latest pointer). The latest pointer is replaced atomically.
* :func:`load_parsed_plan_artifact` hard-fails on every corruption /
  schema mismatch / DAG violation. **No markdown fallback** — that
  would silently degrade to an unvalidated, partial plan and mask
  the real failure.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.entities import SubTask
from pipeline.plan_artifacts import (
    LATEST_FILENAME,
    PARSED_PLAN_ARTIFACT_VERSION,
    ParsedPlanArtifactError,
    load_parsed_plan_artifact,
    parsed_plan_from_dict,
    parsed_plan_to_dict,
    resolve_parent_run_dir,
    write_parsed_plan_artifact,
)
from pipeline.plan_parser import ParsedPlan

# ── helpers ─────────────────────────────────────────────────────────────────


def _full_plan() -> ParsedPlan:
    """A plan with every typed contract slot + every subtask field
    populated so round-trip tests catch any silent dropper."""
    return ParsedPlan(
        short_summary="Short summary text.",
        planning_context="Longer planning context body across\nmultiple lines.",
        subtasks=(
            SubTask(
                id="t1",
                goal="Investigate the surface",
                spec="Read the existing handler, list call sites.",
                files=("src/handler.py", "tests/test_handler.py"),
                skill="backend-python",
                model="claude-opus-4-7",
                done_criteria=("List of call sites is captured",),
                owned_files=("src/handler.py",),
                architectural_decision=False,
            ),
            SubTask(
                id="t2",
                goal="Apply the fix",
                spec="Implement minimal change in handler.",
                files=("src/handler.py",),
                skill="backend-python",
                model=None,
                depends_on=("t1",),
                done_criteria=(
                    "Fix is in place",
                    "Existing tests still pass",
                ),
                owned_files=("src/handler.py",),
                architectural_decision=True,
            ),
        ),
        source="json",
        goal="Fix the X bug without breaking Y",
        acceptance_criteria=(
            "X no longer reproduces with the original repro",
            "Y still works for the documented case",
        ),
        owned_files=("src/handler.py", "tests/test_handler.py"),
        commands_to_run=("pytest tests/test_handler.py -q",),
        risks=("Could regress related code path",),
        review_focus=("Symmetry with existing handler contract",),
        mcp_context=(
            {"server": "context7", "library": "stdlib", "topic": "X"},
        ),
        parse_warnings=(),
    )


def _minimal_plan() -> ParsedPlan:
    """A plan with only the required fields populated. Covers the
    'omit empty fields' branches in the serialiser."""
    return ParsedPlan(
        short_summary="Minimal plan.",
        planning_context="Minimal.",
        subtasks=(
            SubTask(
                id="only",
                goal="Do the thing.",
            ),
        ),
        source="json",
    )


# ── Round-trip ──────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_full_plan_round_trips_field_for_field(self) -> None:
        plan = _full_plan()
        restored = parsed_plan_from_dict(parsed_plan_to_dict(plan))

        # Plan-level typed contract.
        assert restored.short_summary == plan.short_summary
        assert restored.planning_context == plan.planning_context
        assert restored.goal == plan.goal
        assert restored.acceptance_criteria == plan.acceptance_criteria
        assert restored.owned_files == plan.owned_files
        assert restored.commands_to_run == plan.commands_to_run
        assert restored.risks == plan.risks
        assert restored.review_focus == plan.review_focus
        assert restored.mcp_context == plan.mcp_context

        # Subtask shape and DAG.
        assert len(restored.subtasks) == len(plan.subtasks)
        for orig, back in zip(plan.subtasks, restored.subtasks, strict=True):
            assert back == orig

    def test_minimal_plan_round_trips(self) -> None:
        plan = _minimal_plan()
        restored = parsed_plan_from_dict(parsed_plan_to_dict(plan))
        assert restored.short_summary == plan.short_summary
        assert restored.subtasks[0].id == "only"
        assert restored.subtasks[0].goal == "Do the thing."
        # Empty fields stay empty after the omit-then-default round trip.
        assert restored.acceptance_criteria == ()
        assert restored.subtasks[0].files == ()
        assert restored.subtasks[0].depends_on == ()
        assert restored.subtasks[0].architectural_decision is False

    def test_source_is_artifact_after_round_trip(self) -> None:
        """``source`` becomes ``"artifact"`` on the reconstructed plan
        so consumers can tell the plan was loaded from disk, not
        produced fresh by an architect call."""
        plan = _full_plan()
        restored = parsed_plan_from_dict(parsed_plan_to_dict(plan))
        assert restored.source == "artifact"

    def test_envelope_carries_artifact_version(self) -> None:
        payload = parsed_plan_to_dict(_full_plan())
        assert payload["artifact_version"] == PARSED_PLAN_ARTIFACT_VERSION

    def test_empty_contract_fields_are_omitted_from_payload(self) -> None:
        """Optional REA-1 contract fields should not appear in the
        serialised envelope when empty — the validator treats absent
        and empty as equivalent, and omitting keeps the artefact
        compact."""
        payload = parsed_plan_to_dict(_minimal_plan())
        body = payload["plan"]
        for omitted in (
            "goal",
            "acceptance_criteria",
            "owned_files",
            "commands_to_run",
            "risks",
            "review_focus",
            "mcp_context",
        ):
            assert omitted not in body, (
                f"{omitted!r} should be omitted when empty, got "
                f"{body.get(omitted)!r}"
            )


# ── allowed_modifications round-trip + type rejection ───────────────────────


class TestAllowedModificationsRoundTrip:
    """T4: ``allowed_modifications`` mirrors ``owned_files`` mechanics at
    both the plan level and the per-subtask level — round-trip safe,
    omitted-when-empty, and type-rejected (never coerced)."""

    def test_both_levels_round_trip(self) -> None:
        plan = ParsedPlan(
            short_summary="Plan with companion mods.",
            planning_context="ctx",
            subtasks=(
                SubTask(
                    id="t1",
                    goal="Update deps",
                    allowed_modifications=("package-lock.json — derived",),
                ),
            ),
            source="json",
            allowed_modifications=("golden/*.snap — regenerated",),
        )
        restored = parsed_plan_from_dict(parsed_plan_to_dict(plan))

        assert restored.allowed_modifications == plan.allowed_modifications
        assert restored.subtasks[0].allowed_modifications == (
            "package-lock.json — derived",
        )

    def test_empty_allowed_modifications_omitted_from_payload(self) -> None:
        plan = ParsedPlan(
            short_summary="No companion mods.",
            planning_context="ctx",
            subtasks=(SubTask(id="t1", goal="x"),),
            source="json",
        )
        payload = parsed_plan_to_dict(plan)
        assert "allowed_modifications" not in payload["plan"]
        assert "allowed_modifications" not in payload["plan"]["tasks"][0]

    def test_plan_level_as_string_rejected_not_coerced(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "bad_plan_allowed"
        run_dir.mkdir()
        body = {
            "short_summary": "Plan",
            "planning_context": "ctx",
            "tasks": [{"id": "t1", "goal": "Do work"}],
            # Bare string instead of list[str] — must reject, never
            # coerce into a tuple of characters.
            "allowed_modifications": "abc",
        }
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({
                "artifact_version": PARSED_PLAN_ARTIFACT_VERSION,
                "plan": body,
            }),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "allowed_modifications" in str(exc.value)

    def test_task_level_with_non_string_entry_rejected(
        self, tmp_path: Path,
    ) -> None:
        run_dir = tmp_path / "bad_task_allowed"
        run_dir.mkdir()
        body = {
            "short_summary": "Plan",
            "planning_context": "ctx",
            "tasks": [
                {
                    "id": "t1",
                    "goal": "Do work",
                    "allowed_modifications": ["ok.lock", 42],
                },
            ],
        }
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({
                "artifact_version": PARSED_PLAN_ARTIFACT_VERSION,
                "plan": body,
            }),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "allowed_modifications" in str(exc.value)


# ── Filesystem writer ───────────────────────────────────────────────────────


class TestWriteParsedPlanArtifact:
    def test_writes_per_attempt_and_latest_pointer(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260522_120000"
        run_dir.mkdir()
        attempt_path = write_parsed_plan_artifact(
            run_dir, _full_plan(), attempt=1,
        )
        assert attempt_path.name == "plan_20260522_120000_r1.json"
        assert attempt_path.is_file()
        latest = run_dir / LATEST_FILENAME
        assert latest.is_file()
        # Same content in both files for the latest attempt.
        assert attempt_path.read_text() == latest.read_text()

    def test_second_attempt_keeps_first_attempt_artifact(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260522_130000"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _full_plan(), attempt=1)
        write_parsed_plan_artifact(run_dir, _minimal_plan(), attempt=2)

        first = run_dir / "plan_20260522_130000_r1.json"
        second = run_dir / "plan_20260522_130000_r2.json"
        latest = run_dir / LATEST_FILENAME

        # Per-attempt artefacts are immutable; only the latest pointer flips.
        assert first.is_file()
        assert second.is_file()
        assert first.read_text() != second.read_text()
        assert latest.read_text() == second.read_text()

    def test_latest_pointer_is_atomic_swap(self, tmp_path: Path) -> None:
        """The latest pointer is replaced via os.replace, so the
        directory never contains a half-written ``parsed_plan.json``.
        We can't easily race-test atomicity in a unit test, but we
        can assert there are no stray ``.tmp`` files left around
        after a successful write — the cleanest visible symptom of
        the atomic-swap implementation working."""
        run_dir = tmp_path / "20260522_140000"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _full_plan(), attempt=1)
        tmp_residue = list(run_dir.glob(".parsed_plan.json.*.tmp"))
        assert tmp_residue == [], (
            f"atomic-swap left tmp residue behind: {tmp_residue}"
        )

    def test_missing_run_dir_is_loud(self, tmp_path: Path) -> None:
        """The writer refuses to silently mkdir a missing run_dir —
        that would let a misconfigured caller scatter artefacts under
        an unexpected path. Loud failure forces the caller to set up
        the run dir explicitly."""
        with pytest.raises(FileNotFoundError):
            write_parsed_plan_artifact(
                tmp_path / "does_not_exist", _full_plan(), attempt=1,
            )

    def test_zero_or_negative_attempt_rejected(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260522_150000"
        run_dir.mkdir()
        with pytest.raises(ValueError):
            write_parsed_plan_artifact(run_dir, _full_plan(), attempt=0)
        with pytest.raises(ValueError):
            write_parsed_plan_artifact(run_dir, _full_plan(), attempt=-1)


# ── Filesystem reader: hard-fail on every corruption mode ────────────────────


class TestLoadParsedPlanArtifactHardFail:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "empty_run"
        run_dir.mkdir()
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "not found" in str(exc.value)

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "broken_json"
        run_dir.mkdir()
        (run_dir / LATEST_FILENAME).write_text(
            "{this is not valid json", encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "not valid JSON" in str(exc.value)

    def test_missing_envelope_version_raises(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "no_version"
        run_dir.mkdir()
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({"plan": {"short_summary": "x"}}),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "artifact_version" in str(exc.value)

    def test_unknown_envelope_version_raises(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "wrong_version"
        run_dir.mkdir()
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({"artifact_version": 999, "plan": {}}),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "artifact_version" in str(exc.value)

    def test_schema_invalid_plan_body_raises(self, tmp_path: Path) -> None:
        """A body that does not satisfy validate_plan_dict must fail —
        no silent partial reconstruction."""
        run_dir = tmp_path / "bad_schema"
        run_dir.mkdir()
        bad_body = {
            # Missing required fields like "tasks" / "short_summary";
            # validate_plan_dict raises PlanSchemaError.
            "planning_context": "ctx",
        }
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({
                "artifact_version": PARSED_PLAN_ARTIFACT_VERSION,
                "plan": bad_body,
            }),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "schema" in str(exc.value).lower()

    def test_dag_invalid_plan_raises(self, tmp_path: Path) -> None:
        """A schema-valid plan whose subtask DAG has a cycle or a
        dangling dependency must fail — same hard-fail policy as
        live architect output."""
        run_dir = tmp_path / "bad_dag"
        run_dir.mkdir()
        plan_with_cycle = {
            "short_summary": "Plan",
            "planning_context": "ctx",
            "tasks": [
                {"id": "a", "goal": "A", "depends_on": ["b"]},
                {"id": "b", "goal": "B", "depends_on": ["a"]},  # cycle
            ],
        }
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({
                "artifact_version": PARSED_PLAN_ARTIFACT_VERSION,
                "plan": plan_with_cycle,
            }),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "DAG" in str(exc.value) or "cycle" in str(exc.value).lower()

    def test_owned_files_as_string_rejected_not_coerced(
        self, tmp_path: Path,
    ) -> None:
        """Regression: an artefact with ``owned_files`` as a bare
        string (instead of a list) used to slip through validation
        and then ``tuple("abc")`` would silently coerce it to
        ``("a", "b", "c")`` inside ``_subtask_from_dict``. That
        broke the "hard-fail on schema-invalid" invariant — a
        malformed PR4 hydration path could end up with a SubTask
        whose ``owned_files`` is a tuple of characters. Validator
        now rejects upfront.
        """
        run_dir = tmp_path / "bad_owned_files"
        run_dir.mkdir()
        body = {
            "short_summary": "Plan",
            "planning_context": "ctx",
            "tasks": [
                {
                    "id": "t1",
                    "goal": "Do work",
                    # Bare string instead of list[str] — the silent
                    # coercion bug used to make this "abc" become
                    # ("a", "b", "c").
                    "owned_files": "abc",
                },
            ],
        }
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({
                "artifact_version": PARSED_PLAN_ARTIFACT_VERSION,
                "plan": body,
            }),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        # Message must point at owned_files specifically so the
        # operator can fix the artefact.
        assert "owned_files" in str(exc.value)

    def test_architectural_decision_as_string_rejected_not_coerced(
        self, tmp_path: Path,
    ) -> None:
        """Regression: ``architectural_decision: "false"`` used to
        evaluate ``bool("false")`` → ``True`` in the loader's
        defensive cast, silently flipping the flag. Validator now
        rejects any non-bool value strictly.
        """
        run_dir = tmp_path / "bad_arch_decision"
        run_dir.mkdir()
        body = {
            "short_summary": "Plan",
            "planning_context": "ctx",
            "tasks": [
                {
                    "id": "t1",
                    "goal": "Do work",
                    # String "false" is the most dangerous form — a
                    # naive ``bool(...)`` cast promotes it to True.
                    "architectural_decision": "false",
                },
            ],
        }
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({
                "artifact_version": PARSED_PLAN_ARTIFACT_VERSION,
                "plan": body,
            }),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "architectural_decision" in str(exc.value)

    def test_owned_files_with_non_string_entry_rejected(
        self, tmp_path: Path,
    ) -> None:
        """``owned_files`` must be a list of strings end-to-end. A
        list with a non-string element (e.g. ``[1, 2]``) is the
        second-most-common shape error after the bare-string form."""
        run_dir = tmp_path / "bad_owned_files_entry"
        run_dir.mkdir()
        body = {
            "short_summary": "Plan",
            "planning_context": "ctx",
            "tasks": [
                {
                    "id": "t1",
                    "goal": "Do work",
                    "owned_files": ["ok.py", 42],
                },
            ],
        }
        (run_dir / LATEST_FILENAME).write_text(
            json.dumps({
                "artifact_version": PARSED_PLAN_ARTIFACT_VERSION,
                "plan": body,
            }),
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError) as exc:
            load_parsed_plan_artifact(run_dir)
        assert "owned_files" in str(exc.value)

    def test_no_markdown_fallback(self, tmp_path: Path) -> None:
        """When ``parsed_plan.json`` is missing, the loader must NOT
        silently degrade to reading the sibling ``plan_<run>_r<n>.md``
        — that markdown is a projection and parsing it back would
        lose typed contract fields. The whole point of the artefact
        is to be the canonical machine source; missing it is a hard
        error."""
        run_dir = tmp_path / "20260522_160000"
        run_dir.mkdir()
        # Sibling markdown exists, but the JSON pointer is missing.
        (run_dir / "plan_20260522_160000_r1.md").write_text(
            "# Implementation Plan\n\n## Tasks\n\n## Task t1: Do it\n",
            encoding="utf-8",
        )
        with pytest.raises(ParsedPlanArtifactError):
            load_parsed_plan_artifact(run_dir)


# ── End-to-end: writer → reader equivalence ─────────────────────────────────


class TestEndToEnd:
    def test_writer_reader_round_trip(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260522_170000"
        run_dir.mkdir()
        original = _full_plan()
        write_parsed_plan_artifact(run_dir, original, attempt=1)
        restored = load_parsed_plan_artifact(run_dir)

        # Plan-level fields match.
        assert restored.short_summary == original.short_summary
        assert restored.planning_context == original.planning_context
        assert restored.goal == original.goal
        assert restored.acceptance_criteria == original.acceptance_criteria
        assert restored.owned_files == original.owned_files
        assert restored.commands_to_run == original.commands_to_run
        assert restored.risks == original.risks
        assert restored.review_focus == original.review_focus
        assert restored.mcp_context == original.mcp_context
        # Subtasks match.
        assert restored.subtasks == original.subtasks

    def test_latest_reflects_last_attempt(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "20260522_180000"
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _full_plan(), attempt=1)
        write_parsed_plan_artifact(run_dir, _minimal_plan(), attempt=2)

        restored = load_parsed_plan_artifact(run_dir)
        assert restored.subtasks[0].id == "only"
        assert restored.short_summary == "Minimal plan."


# ── resolve_parent_run_dir (PR4) ────────────────────────────────────────────


class TestResolveParentRunDir:
    """``resolve_parent_run_dir`` locates a parent run directory by either
    a bare run id or an explicit path, hard-failing with a clear
    message if the resolved directory does not exist, is not a
    directory, or does not contain ``parsed_plan.json``."""

    def _seed_parent(self, tmp_path: Path, run_id: str) -> Path:
        """Helper: build a run_dir with a valid parsed_plan.json so the
        resolver's existence + content checks pass."""
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / run_id
        run_dir.mkdir()
        write_parsed_plan_artifact(run_dir, _full_plan(), attempt=1)
        return run_dir

    def test_resolves_by_run_id_against_runs_dir(self, tmp_path: Path) -> None:
        parent = self._seed_parent(tmp_path, "20260523_120000")
        resolved = resolve_parent_run_dir(
            "20260523_120000",
            runs_dir=parent.parent,
        )
        assert resolved == parent.resolve()

    def test_resolves_by_absolute_path(self, tmp_path: Path) -> None:
        parent = self._seed_parent(tmp_path, "20260523_130000")
        resolved = resolve_parent_run_dir(str(parent.resolve()))
        assert resolved == parent.resolve()

    def test_resolves_by_path_with_separator_without_runs_dir(
        self, tmp_path: Path,
    ) -> None:
        """A spec containing ``/`` is treated as a path even without
        ``runs_dir`` — the resolver does not need a workspace to find
        an explicit path."""
        parent = self._seed_parent(tmp_path, "20260523_135000")
        # Construct a path-shaped string (contains '/') without anchoring
        # at an absolute prefix; resolve() turns it absolute.
        spec = str(parent.resolve())
        resolved = resolve_parent_run_dir(spec)
        assert resolved == parent.resolve()

    def test_run_id_without_runs_dir_raises(self) -> None:
        """A bare run id (no path separator) with no ``runs_dir``
        cannot be resolved — the resolver must NOT silently treat it
        as a cwd-relative path. Operators usually mean a workspace
        lookup and want a clear diagnostic."""
        with pytest.raises(ParsedPlanArtifactError) as exc:
            resolve_parent_run_dir("20260523_140000")
        assert "no runs_dir" in str(exc.value)

    def test_missing_run_dir_raises(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        with pytest.raises(ParsedPlanArtifactError) as exc:
            resolve_parent_run_dir(
                "20260523_150000",
                runs_dir=runs_dir,
            )
        assert "not found" in str(exc.value)

    def test_non_directory_target_raises(self, tmp_path: Path) -> None:
        # Spec points at a file, not a directory.
        f = tmp_path / "not_a_dir.txt"
        f.write_text("hello", encoding="utf-8")
        with pytest.raises(ParsedPlanArtifactError) as exc:
            resolve_parent_run_dir(str(f))
        assert "not a directory" in str(exc.value)

    def test_dir_without_parsed_plan_json_raises(self, tmp_path: Path) -> None:
        """Run dir exists but has no ``parsed_plan.json`` — happens
        for older runs (pre-PR2), dry-runs, or failed-planning runs.
        The resolver names the offending dir and hints at re-running
        the plan profile so the operator can self-recover."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "20260523_160000"
        run_dir.mkdir(parents=True)
        with pytest.raises(ParsedPlanArtifactError) as exc:
            resolve_parent_run_dir(
                "20260523_160000",
                runs_dir=runs_dir,
            )
        msg = str(exc.value)
        assert "parsed_plan.json" in msg
        # Hint references the recovery path so an operator can fix it.
        assert "Re-run the plan profile" in msg

    def test_empty_spec_rejected(self) -> None:
        with pytest.raises(ParsedPlanArtifactError) as exc:
            resolve_parent_run_dir("")
        assert "non-empty string" in str(exc.value)
