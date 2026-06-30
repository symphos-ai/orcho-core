"""Durable machine-readable artefacts for ``ParsedPlan``.

The PLAN phase already persists a human-readable ``plan_<run>_r<n>.md``
via :func:`pipeline.phases.builtin._render_and_store_plan_artifact`.
That markdown is a deterministic projection — useful for dashboards,
evidence bundles, and humans, but **not** a round-trip-safe source of
truth: the parser supports a legacy markdown fallback that can recover
*some* of a plan from prose, but it cannot fully reconstruct the typed
REA-1 contract fields (``goal`` / ``acceptance_criteria`` /
``owned_files`` / ``commands_to_run`` / ``risks`` / ``review_focus`` /
``mcp_context``) or per-subtask ``done_criteria`` / ``owned_files`` /
``architectural_decision`` in every case.

This module ships the missing piece: a side-by-side JSON artefact that
*is* round-trip-safe. Architecture invariant (from the over-run-plan follow-up and change-semantics planning record (internal)):

* ``ParsedPlan`` is the canonical execution contract.
* ``parsed_plan.json`` is the durable machine source.
* ``plan_<run>_r<n>.md`` is a human projection only.

The writer produces two files per attempt under ``run_dir/``:

* ``plan_<run_id>_r<attempt>.json`` — the per-attempt artefact, mirrors
  the markdown sibling so attempt-N's parsed view is recoverable for
  evidence / debugging even after replan rewrites the plan.
* ``parsed_plan.json`` — the *latest* parsed plan. Replaced atomically
  on every write (``os.replace``). This is the path future consumers
  (cross-project hand-off, ``--from-run-plan`` hydration) read from.

The reader is intentionally strict: missing / corrupt / schema-invalid
inputs raise. **No markdown fallback.** The whole point of the JSON
artefact is to be the durable source of truth — silently falling back
to markdown would mask a real corruption and produce a partially-
reconstructed plan, which is worse than failing fast.

This module is dependency-free runtime data plumbing. Writers and
readers stay pure: no events, no globals, no implicit run_dir lookup.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agents.entities import SubTask
from core.contracts.plan_schema import PlanSchemaError, validate_plan_dict
from pipeline.plan_parser import ParsedPlan, PlanParseError, validate_dag

# Schema version for the durable artefact. Bumped only on a breaking
# shape change; additive fields can land without a bump.
PARSED_PLAN_ARTIFACT_VERSION = 1

# Latest-pointer filename. Stable across attempts; replaced atomically.
LATEST_FILENAME = "parsed_plan.json"


class ParsedPlanArtifactError(ValueError):
    """Raised by :func:`load_parsed_plan_artifact` for any failure mode.

    Failure modes covered (all raise this exception):

    * the file does not exist;
    * the file is not valid JSON;
    * the envelope does not carry a recognised schema version;
    * the plan body fails ``validate_plan_dict``;
    * the reconstructed subtask DAG fails ``validate_dag``
      (duplicate ids, dangling references, cycles).

    Callers MUST NOT silently degrade to reading ``plan_<run>_r<n>.md``
    on these errors — the markdown is a projection, not a round-trip-
    safe source. Recovering a partial plan from markdown would mask the
    real corruption and silently change the run's contract.
    """


# ── Serialisation ───────────────────────────────────────────────────────────


def parsed_plan_to_dict(plan: ParsedPlan) -> dict[str, Any]:
    """Project a :class:`ParsedPlan` onto a JSON-serialisable envelope.

    The envelope shape is:

    .. code-block:: json

        {
          "artifact_version": 1,
          "plan": { ... validate_plan_dict-compatible body ... }
        }

    The inner ``plan`` body is intentionally validator-compatible with
    :func:`core.contracts.plan_schema.validate_plan_dict` so a future
    reader is free to either round-trip through this module or feed
    the inner body directly to the existing schema validator without
    needing a second contract.
    """
    return {
        "artifact_version": PARSED_PLAN_ARTIFACT_VERSION,
        "plan": _plan_body_to_dict(plan),
    }


def _plan_body_to_dict(plan: ParsedPlan) -> dict[str, Any]:
    """Build the validator-compatible inner plan body."""
    body: dict[str, Any] = {
        "short_summary": plan.short_summary,
        "planning_context": plan.planning_context,
        "tasks": [_subtask_to_dict(s) for s in plan.subtasks],
    }
    # REA-1 typed contract — emit only populated fields so an empty
    # contract round-trips as an empty contract (the schema validator
    # treats all of these as optional).
    if plan.goal:
        body["goal"] = plan.goal
    if plan.acceptance_criteria:
        body["acceptance_criteria"] = list(plan.acceptance_criteria)
    if plan.owned_files:
        body["owned_files"] = list(plan.owned_files)
    if plan.allowed_modifications:
        body["allowed_modifications"] = list(plan.allowed_modifications)
    if plan.commands_to_run:
        body["commands_to_run"] = list(plan.commands_to_run)
    if plan.risks:
        body["risks"] = list(plan.risks)
    if plan.review_focus:
        body["review_focus"] = list(plan.review_focus)
    if plan.mcp_context:
        # mcp_context entries are already plain dicts; copy defensively
        # so caller mutations cannot reach the in-memory plan.
        body["mcp_context"] = [dict(m) for m in plan.mcp_context]
    return body


def _subtask_to_dict(s: SubTask) -> dict[str, Any]:
    """Serialise one subtask. Empty fields are omitted for compactness.

    The validator and ``_subtask_from_dict`` both treat missing fields
    as empty, so omitting them is round-trip safe.
    """
    out: dict[str, Any] = {
        "id": s.id,
        "goal": s.goal,
    }
    if s.spec:
        out["spec"] = s.spec
    if s.files:
        out["files"] = list(s.files)
    if s.skill is not None:
        out["skill"] = s.skill
    if s.model is not None:
        out["model"] = s.model
    if s.depends_on:
        out["depends_on"] = list(s.depends_on)
    if s.done_criteria:
        out["done_criteria"] = list(s.done_criteria)
    if s.owned_files:
        out["owned_files"] = list(s.owned_files)
    if s.allowed_modifications:
        out["allowed_modifications"] = list(s.allowed_modifications)
    if s.architectural_decision:
        out["architectural_decision"] = True
    return out


# ── Deserialisation ─────────────────────────────────────────────────────────


def parsed_plan_from_dict(data: Mapping[str, Any]) -> ParsedPlan:
    """Reconstruct a :class:`ParsedPlan` from a dict produced by
    :func:`parsed_plan_to_dict`.

    Strict on shape:

    * the envelope must declare ``artifact_version``; an unknown
      version is rejected (we never silently downgrade);
    * the inner plan body must pass ``validate_plan_dict``;
    * the rebuilt subtask DAG must pass ``validate_dag``.

    All failures surface as :class:`ParsedPlanArtifactError`.
    """
    if not isinstance(data, Mapping):
        raise ParsedPlanArtifactError(
            f"parsed plan artifact must be a JSON object, got {type(data).__name__}",
        )
    version = data.get("artifact_version")
    if version != PARSED_PLAN_ARTIFACT_VERSION:
        raise ParsedPlanArtifactError(
            f"unsupported parsed plan artifact_version: {version!r} "
            f"(expected {PARSED_PLAN_ARTIFACT_VERSION})",
        )
    body = data.get("plan")
    if not isinstance(body, Mapping):
        raise ParsedPlanArtifactError(
            "parsed plan artifact missing 'plan' object",
        )

    try:
        validate_plan_dict(dict(body))
    except PlanSchemaError as e:
        raise ParsedPlanArtifactError(
            f"parsed plan body failed schema validation: {e}",
        ) from e

    subtasks = tuple(_subtask_from_dict(t) for t in body["tasks"])
    try:
        validate_dag(subtasks)
    except PlanParseError as e:
        raise ParsedPlanArtifactError(
            f"parsed plan DAG is invalid: {e}",
        ) from e

    return ParsedPlan(
        short_summary=body.get("short_summary", ""),
        planning_context=body.get("planning_context", ""),
        subtasks=subtasks,
        # The artefact is the durable machine source; surface that
        # provenance honestly on the reconstructed plan instead of
        # re-using ``"json"`` (which means "freshly parsed from
        # architect output"). Consumers reading ``source`` can
        # tell whether the plan came from a live architect call or
        # from a persisted artefact.
        source="artifact",
        goal=(body.get("goal") or None),
        acceptance_criteria=tuple(body.get("acceptance_criteria") or ()),
        owned_files=tuple(body.get("owned_files") or ()),
        allowed_modifications=tuple(body.get("allowed_modifications") or ()),
        commands_to_run=tuple(body.get("commands_to_run") or ()),
        risks=tuple(body.get("risks") or ()),
        review_focus=tuple(body.get("review_focus") or ()),
        mcp_context=tuple(body.get("mcp_context") or ()),
    )


def _subtask_from_dict(t: Mapping[str, Any]) -> SubTask:
    """Mirror of :func:`pipeline.plan_parser._subtask_from_dict`."""
    return SubTask(
        id=str(t["id"]).strip(),
        goal=str(t["goal"]).strip(),
        spec=str(t.get("spec") or "").strip(),
        files=tuple(t.get("files") or ()),
        skill=(t.get("skill") or None),
        model=(t.get("model") or None),
        depends_on=tuple(t.get("depends_on") or ()),
        done_criteria=tuple(t.get("done_criteria") or ()),
        owned_files=tuple(t.get("owned_files") or ()),
        allowed_modifications=tuple(t.get("allowed_modifications") or ()),
        architectural_decision=bool(t.get("architectural_decision") or False),
    )


# ── Filesystem I/O ──────────────────────────────────────────────────────────


def write_parsed_plan_artifact(
    run_dir: Path,
    plan: ParsedPlan,
    *,
    attempt: int,
) -> Path:
    """Persist *plan* under *run_dir* as machine-readable JSON.

    Two files are produced per call:

    * ``plan_<run_id>_r<attempt>.json`` — the per-attempt artefact.
      The filename mirrors the human markdown sibling (``plan_<run_id>_
      r<attempt>.md``) so evidence consumers can join the two by
      attempt number.
    * ``parsed_plan.json`` — the *latest* plan. Replaced atomically
      via ``os.replace`` after writing to a temp file in the same
      directory. The atomic swap means a concurrent reader (resume
      path, cross hand-off) never sees a torn write.

    Returns the path to the per-attempt JSON file. Caller is
    responsible for ensuring ``run_dir`` exists (the function
    will fail loudly if it does not — silently ``mkdir`` would
    let a misconfigured caller scatter artefacts).

    The function does NOT emit observability events. The PLAN phase
    handler already emits ``artifact.created`` for the markdown
    sibling; pairing those one-to-one with JSON siblings is the
    natural follow-up, but that wiring belongs in the phase handler
    where the events surface lives, not here.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(
            f"run_dir does not exist or is not a directory: {run_dir}",
        )
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")

    payload = parsed_plan_to_dict(plan)
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    run_id = run_dir.name or "run"
    attempt_path = run_dir / f"plan_{run_id}_r{attempt}.json"
    attempt_path.write_text(encoded, encoding="utf-8")

    # Atomic latest-pointer swap. Temp file lives in the same
    # directory so the rename is a single-filesystem operation
    # (``os.replace`` is atomic when source and target are on the
    # same FS). A concurrent reader sees either the old or the new
    # contents — never a half-written file.
    latest_path = run_dir / LATEST_FILENAME
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{LATEST_FILENAME}.", suffix=".tmp", dir=str(run_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
        os.replace(tmp_name, latest_path)
    except Exception:
        # Best-effort cleanup so a failed write does not leave the
        # tmp file behind. The per-attempt file is intentionally
        # left in place (it was a successful write before the
        # latest-pointer step).
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    return attempt_path


def load_parsed_plan_artifact(run_dir: Path) -> ParsedPlan:
    """Load the latest parsed plan persisted under *run_dir*.

    Reads ``run_dir/parsed_plan.json`` and reconstructs the
    :class:`ParsedPlan`. Strict on every failure mode (see
    :class:`ParsedPlanArtifactError`):

    * missing file → ``ParsedPlanArtifactError`` (does NOT fall back
      to reading ``plan_<run>_r<n>.md``);
    * invalid JSON → ``ParsedPlanArtifactError``;
    * unsupported / missing ``artifact_version`` →
      ``ParsedPlanArtifactError``;
    * schema-invalid plan body → ``ParsedPlanArtifactError``;
    * DAG-invalid subtask set → ``ParsedPlanArtifactError``.

    No markdown fallback is allowed. The architecture invariant is
    that ``parsed_plan.json`` is the canonical machine source; if
    it is unreadable, the run has no valid persisted plan and the
    caller must surface that to the operator rather than silently
    reconstruct a partial plan from prose.
    """
    latest_path = run_dir / LATEST_FILENAME
    if not latest_path.is_file():
        raise ParsedPlanArtifactError(
            f"parsed plan artifact not found at {latest_path}",
        )
    try:
        raw = latest_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ParsedPlanArtifactError(
            f"parsed plan artifact unreadable: {e}",
        ) from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ParsedPlanArtifactError(
            f"parsed plan artifact is not valid JSON: {e}",
        ) from e
    return parsed_plan_from_dict(data)


def resolve_parent_run_dir(
    spec: str,
    *,
    runs_dir: Path | None = None,
) -> Path:
    """Resolve a ``--from-run-plan`` spec to a parent run directory.

    Accepts either form:

    * a bare run id (``20260522_120000``) — joined against
      *runs_dir* (which must be supplied for this shape);
    * an absolute or relative filesystem path to a run directory.

    The resolved directory must exist, must be a directory, and must
    contain a ``parsed_plan.json`` artefact. All three checks fail
    loudly via :class:`ParsedPlanArtifactError` with a message naming
    the offending spec — operators get a clear diagnostic instead of
    a downstream ``FileNotFoundError`` from the loader.

    This resolver does **not** load the plan; it only locates the
    parent run directory. Callers feed the result into
    :func:`load_parsed_plan_artifact` to actually hydrate the plan.
    Keeping the two steps separate lets the resolver fail fast
    (bad spec, wrong workspace) before the loader's slower JSON +
    schema + DAG validation runs.
    """
    if not spec or not isinstance(spec, str):
        raise ParsedPlanArtifactError(
            "from-run-plan spec must be a non-empty string",
        )

    # Path-shaped spec wins outright: forward slash, leading dot, or
    # absolute. Run-id shape is a bare basename with no path separator.
    path_candidate = Path(spec).expanduser()
    if path_candidate.is_absolute() or "/" in spec or spec.startswith("."):
        resolved = path_candidate.resolve()
    elif runs_dir is not None:
        resolved = (runs_dir / spec).resolve()
    else:
        raise ParsedPlanArtifactError(
            f"from-run-plan spec {spec!r} looks like a run id but no "
            "runs_dir was supplied; pass --workspace or use an explicit "
            "path",
        )

    if not resolved.exists():
        raise ParsedPlanArtifactError(
            f"from-run-plan parent run dir not found: {resolved}",
        )
    if not resolved.is_dir():
        raise ParsedPlanArtifactError(
            f"from-run-plan parent run path is not a directory: {resolved}",
        )
    if not (resolved / LATEST_FILENAME).is_file():
        raise ParsedPlanArtifactError(
            f"from-run-plan parent run {resolved} has no "
            f"{LATEST_FILENAME!r} — that run did not persist a "
            "machine-readable plan (older run, dry-run, or planning "
            "failed). Re-run the plan profile against the same task to "
            "produce a parsed_plan.json.",
        )
    return resolved


__all__ = [
    "LATEST_FILENAME",
    "PARSED_PLAN_ARTIFACT_VERSION",
    "ParsedPlanArtifactError",
    "load_parsed_plan_artifact",
    "parsed_plan_from_dict",
    "parsed_plan_to_dict",
    "resolve_parent_run_dir",
    "write_parsed_plan_artifact",
]
