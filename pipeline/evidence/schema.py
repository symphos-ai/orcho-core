"""pipeline.evidence.schema — REA-3 evidence bundle schema v1.

The bundle is a portable JSON document a third party can read with the
stdlib ``json`` module — no orcho imports required (REA-3 DoD). This
module defines the on-disk schema, the schema_version slot, and a
:func:`validate_bundle` helper for tests + downstream consumers.

Schema design choices:

* **Lower-bound contract.** Every field listed below must be present on
  every bundle so consumers can treat absence as a bug. Optional
  enrichments (REA-5 mcp context, future review.findings) extend the
  document by *adding* keys, never by making baseline keys conditional.
* **Stable typing.** Lists are always lists (empty when nothing to
  show), dicts always dicts. ``None`` is permitted only on the
  explicitly-typed string slots (``goal``, ``error_summary``).
* **No raw event timeline.** ``events.jsonl`` already exists next to
  the bundle; duplicating it would inflate evidence.json and tempt
  consumers to reconstruct state from raw events instead of using the
  bundle's pre-rolled rollups. The bundle records derived rollups
  (per-phase, gates, commands, artifacts) plus the raw-events path.

Wire-format stability rule:

* Adding a new top-level key is OK at any version.
* Removing or repurposing a key is a ``schema_version`` bump.
* The first real bundle version is ``"1"``; ``"0-placeholder"``
  remains the REA-0 stub when collection fails or runs early.

Out-of-wire kinds (ADR 0080 MCP falsifier): Stage 3 native command-receipts
(``verification_command`` kind) and Stage 2 env-assertion receipts live in their
own run-dir directories and are deliberately NOT part of this v1 bundle. The
collector reads only ``verification_receipts/``, so neither kind reaches this
schema or any MCP resource projecting the bundle (e.g. ``orcho_run_evidence``).
Adding a command-receipt therefore does not touch the MCP wire — see the
falsifier test in ``tests/unit/pipeline/evidence/test_verification_receipt.py``.
"""
from __future__ import annotations

from typing import Any, Final

#: Current bundle schema version. Bump when removing / repurposing keys.
EVIDENCE_SCHEMA_VERSION: Final[str] = "1"

#: Top-level required keys. Mirrors :func:`validate_bundle`.
REQUIRED_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "run_id",
        "run_dir",
        "status",
        "created_at",
        "task",
        "profile",
        "plan",
        "phases",
        "gates",
        "commands",
        "artifacts",
        "metrics",
        "errors",
        # M12-C3: durable prompt-render trace summary. Always present —
        # empty list when the run produced no covered records.
        "prompt_render",
        "raw_events_path",
    }
)

#: Required keys on each entry of the ``prompt_render`` list.
#: Strict — every entry is one trace summary built from the M12-C2
#: durable shape via ``summarize_trace_for_evidence``.
REQUIRED_PROMPT_RENDER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "phase",
        # ``phase_key`` mirrors the session-key phase the writer
        # passed to ``_session_aware_invoke``. Equal to ``phase`` for
        # most surfaces; CHAIN repair_changes is the one exception
        # where ``phase_key="implement"`` (the repair reuses the
        # implement physical session) but ``phase="repair_changes"``.
        "phase_key",
        "trace_surface",
        "attempt",
        "round",
        # ``continue_session`` distinguishes round-1 fresh sessions
        # from round-N resumed sessions without cross-referencing
        # ``runner.log``. Writer-stamped at invoke time; reflects
        # the ``continue_session`` flag forwarded to the runtime.
        "continue_session",
        "source_path",
        "render_mode",
        "session_split",
        "execution_mode",
        "surface_id",
        "surface_count",
        "session_scope",
        "session_run_id",
        "session_runtime",
        "session_model",
        "provider_session_id",
        "selected_count",
        "omitted_count",
        # ADR 0026 delta drop: count of parts omitted from the wire on a
        # resumed turn because the runtime already holds them in history
        # (0 on full renders). Counts-only, like selected/omitted.
        "delta_dropped_count",
        "prefix_hash",
        "payload_hash",
        "wire_chars",
    }
)

#: Required keys on the embedded plan contract record.
REQUIRED_PLAN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "source",                 # "json" | "markdown" | "absent"
        "short_summary",
        "planning_context",
        "subtask_count",
        "has_contract",
        "goal",                   # str | None
        "acceptance_criteria",
        "owned_files",
        "commands_to_run",
        "risks",
        "review_focus",
        "mcp_context",
    }
)

#: Required keys on each entry of the ``phases`` list.
REQUIRED_PHASE_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "title", "outcome", "attempt", "started_at", "ended_at"}
)

#: Required keys on each entry of the ``gates`` list.
REQUIRED_GATE_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "kind", "outcome", "duration_s"}
)

#: Required keys on each entry of the ``commands`` list.
REQUIRED_COMMAND_KEYS: Final[frozenset[str]] = frozenset(
    {"argv_summary", "cwd", "exit_code", "duration_s", "outcome"}
)

#: Required keys on each entry of the ``artifacts`` list.
REQUIRED_ARTIFACT_KEYS: Final[frozenset[str]] = frozenset(
    {"path", "kind", "size_bytes"}
)

APPLY_CHECK_STATUSES: Final[frozenset[str]] = frozenset(
    {"pass", "fail", "degraded"}
)

#: Stable error-kind vocabulary the collector emits into ``bundle.errors``.
#: A documentation/allow-list surface — adding a kind here is additive and
#: never bumps ``schema_version``. ``validate_bundle`` does NOT reject an
#: unlisted kind (the ``errors`` list stays open for plugin breadcrumbs); the
#: set exists so consumers and tests have a single greppable reference for the
#: kinds orcho itself produces. ``command_stalled`` (ADR 0103) is the durable
#: record for a stalled command on both the terminal idle-timeout path and the
#: live non-terminal unsafe-process-polling path.
KNOWN_ERROR_KINDS: Final[frozenset[str]] = frozenset(
    {
        "run_halted",
        "run_failed",
        "plan_parse_error",
        "phase_handoff_requested",
        "phase_handoff_waiver",
        "verification_gate_waived",
        "implement_delivery",
        "command_stalled",
    }
)

#: Required keys on a ``command_stalled`` error record (ADR 0103). Validated
#: additively: only records whose ``kind == "command_stalled"`` are checked, so
#: every other (open) error-kind is untouched. ``terminal`` discriminates the
#: idle-timeout escalation (``True``) from the live non-terminal risk flag
#: (``False``); ``recovery_actions`` carries the durable interrupt/resume/halt
#: verb set shared with the SDK projection.
REQUIRED_COMMAND_STALLED_ERROR_KEYS: Final[frozenset[str]] = frozenset(
    {"kind", "phase", "reason", "elapsed_s", "terminal", "recovery_actions"}
)

#: Required keys on each row of the additive ``criterion_matrix`` (ADR 0188).
REQUIRED_CRITERION_ROW_KEYS: Final[frozenset[str]] = frozenset(
    {
        "criterion_id",
        "intent",
        "verify",
        "executors",
        "method",
        "proof_refs",
        "state",
        "reason",
        "blocking",
    }
)

#: Required keys on ``criterion_matrix.summary``.
REQUIRED_CRITERION_SUMMARY_KEYS: Final[frozenset[str]] = frozenset(
    {"total", "blocking_open", "ready", "counts_by_state", "pending_human_ids"}
)

#: The proof-reference kinds a criterion row may cite.
CRITERION_PROOF_KINDS: Final[frozenset[str]] = frozenset(
    {"receipt", "finding", "claim", "human_decision"}
)

#: Required keys on the ``metrics`` rollup.
REQUIRED_METRICS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "total_tokens",
        "total_tokens_in",
        "total_tokens_out",
        "total_duration_s",
        "total_rounds",
    }
)


class EvidenceSchemaError(ValueError):
    """Raised when a bundle dict does not match the v1 schema."""


def validate_bundle(bundle: Any) -> None:
    """Validate ``bundle`` against the v1 schema. Raises on mismatch.

    No-op on placeholder bundles (``schema_version="0-placeholder"``)
    so a run that halted before the collector could compose the full
    bundle still parses. Consumers must check ``schema_version`` before
    relying on the rolled-up fields.
    """
    if not isinstance(bundle, dict):
        raise EvidenceSchemaError(
            f"bundle must be an object, got {type(bundle).__name__}"
        )
    version = bundle.get("schema_version")
    if version not in (EVIDENCE_SCHEMA_VERSION, "0-placeholder"):
        raise EvidenceSchemaError(
            f"unsupported schema_version: {version!r}"
        )
    if version == "0-placeholder":
        # REA-0 stub — only the placeholder fields are guaranteed.
        return

    missing = sorted(REQUIRED_TOP_LEVEL_KEYS - bundle.keys())
    if missing:
        raise EvidenceSchemaError(
            f"bundle missing required top-level keys: {missing}"
        )

    plan = bundle["plan"]
    if not isinstance(plan, dict):
        raise EvidenceSchemaError("bundle.plan must be an object")
    plan_missing = sorted(REQUIRED_PLAN_KEYS - plan.keys())
    if plan_missing:
        raise EvidenceSchemaError(
            f"bundle.plan missing required keys: {plan_missing}"
        )

    _validate_list_entries(
        bundle["phases"], "phases", REQUIRED_PHASE_KEYS,
    )
    _validate_list_entries(
        bundle["gates"], "gates", REQUIRED_GATE_KEYS,
    )
    _validate_list_entries(
        bundle["commands"], "commands", REQUIRED_COMMAND_KEYS,
    )
    _validate_list_entries(
        bundle["artifacts"], "artifacts", REQUIRED_ARTIFACT_KEYS,
    )
    _validate_artifact_apply_checks(bundle["artifacts"])
    _validate_prompt_render(bundle["prompt_render"])

    metrics = bundle["metrics"]
    if not isinstance(metrics, dict):
        raise EvidenceSchemaError("bundle.metrics must be an object")
    metrics_missing = sorted(REQUIRED_METRICS_KEYS - metrics.keys())
    if metrics_missing:
        raise EvidenceSchemaError(
            f"bundle.metrics missing required keys: {metrics_missing}"
        )

    errors = bundle["errors"]
    if not isinstance(errors, list):
        raise EvidenceSchemaError("bundle.errors must be a list")
    _validate_command_stalled_errors(errors)

    # ADR 0093: ``handoff_advice`` is an additive, OPTIONAL top-level key —
    # not in REQUIRED_TOP_LEVEL_KEYS and not gated on a schema_version bump.
    # When present it carries the normalized advice digest; validate only its
    # light outer shape (``calls`` list + ``summary`` dict) so a malformed
    # writer is caught without freezing the per-call field set.
    if "handoff_advice" in bundle:
        _validate_handoff_advice(bundle["handoff_advice"])

    # ADR 0188: ``criterion_matrix`` is additive and OPTIONAL. An absent key is
    # a legacy bundle; ``null`` is never valid; a present value is validated
    # strictly, including the explicit empty matrix.
    if "criterion_matrix" in bundle:
        _validate_criterion_matrix(bundle["criterion_matrix"], plan)


def _validate_criterion_matrix(matrix: Any, plan: Any = None) -> None:
    """Strict shape check for the criterion matrix (ADR 0188 §3-§4)."""
    from pipeline.criterion_matrix import CRITERION_STATE_ORDER

    if not isinstance(matrix, dict):
        raise EvidenceSchemaError(
            "bundle.criterion_matrix must be an object (null is never written)"
        )
    missing = sorted({"rows", "summary"} - matrix.keys())
    if missing:
        raise EvidenceSchemaError(
            f"bundle.criterion_matrix missing required keys: {missing}"
        )
    rows = matrix["rows"]
    if not isinstance(rows, list):
        raise EvidenceSchemaError("bundle.criterion_matrix.rows must be a list")
    for i, row in enumerate(rows):
        _validate_criterion_row(row, f"bundle.criterion_matrix.rows[{i}]")

    summary = matrix["summary"]
    if not isinstance(summary, dict):
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary must be an object"
        )
    summary_missing = sorted(REQUIRED_CRITERION_SUMMARY_KEYS - summary.keys())
    if summary_missing:
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary missing required keys: "
            f"{summary_missing}"
        )
    _require_count(summary, "total", "bundle.criterion_matrix.summary")
    _require_count(summary, "blocking_open", "bundle.criterion_matrix.summary")
    if not isinstance(summary["ready"], bool):
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary.ready must be a bool"
        )

    counts = summary["counts_by_state"]
    if not isinstance(counts, dict):
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary.counts_by_state must be an object"
        )
    order = [s for s in CRITERION_STATE_ORDER if s in counts]
    if list(counts) != order:
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary.counts_by_state keys must follow "
            f"the canonical state order {order}, got {list(counts)}"
        )
    for state, count in counts.items():
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise EvidenceSchemaError(
                "bundle.criterion_matrix.summary.counts_by_state values must "
                f"be positive integers; {state!r} is {count!r}"
            )

    # Every summary number is a fact about ``rows``; a summary that disagrees
    # with the rows it summarises is worse than no summary, because readiness
    # consumes it directly.
    if summary["total"] != len(rows):
        raise EvidenceSchemaError(
            f"bundle.criterion_matrix.summary.total is {summary['total']} but "
            f"there are {len(rows)} rows"
        )
    tallied: dict[str, int] = {}
    for row in rows:
        tallied[row["state"]] = tallied.get(row["state"], 0) + 1
    if dict(counts) != tallied:
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary.counts_by_state does not match "
            f"the rows: {dict(counts)} vs {tallied}"
        )
    blocking = sum(1 for row in rows if row["blocking"])
    if summary["blocking_open"] != blocking:
        raise EvidenceSchemaError(
            f"bundle.criterion_matrix.summary.blocking_open is "
            f"{summary['blocking_open']} but {blocking} rows are blocking"
        )
    if summary["ready"] is not (summary["blocking_open"] == 0):
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary.ready must equal "
            "blocking_open == 0"
        )

    pending = summary["pending_human_ids"]
    if not isinstance(pending, list):
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary.pending_human_ids must be a list"
        )
    expected_pending = [
        row["criterion_id"] for row in rows
        if row["verify"] == "human" and row["state"] == "pending"
    ]
    if pending != expected_pending:
        raise EvidenceSchemaError(
            "bundle.criterion_matrix.summary.pending_human_ids must be the "
            f"pending human criteria in plan order; expected "
            f"{expected_pending}, got {pending}"
        )

    _validate_criterion_rows_against_plan(rows, plan)


def _validate_criterion_rows_against_plan(rows: list, plan: Any) -> None:
    """Cross-check the matrix against the accepted plan it claims to describe.

    ADR 0188: *exactly one row per criterion, in plan order*. Validating the
    rows only against themselves lets a bundle drop a blocking criterion and
    still report ``ready`` — the persisted evidence, the SDK, and MCP would all
    inherit that false readiness without recomputing anything.

    ``plan.source`` is the authority signal, not the criteria list itself. A
    projected plan record (``"json"`` / ``"markdown"``) states the accepted
    contract, so an explicitly empty ``acceptance_criteria`` is a real claim —
    "this plan declares no criteria" — and the matrix must then be the explicit
    empty matrix. Only ``source == "absent"`` means the bundle carries no plan
    projection to check against (no ``plan.parsed`` event), and only that case
    is skipped. A malformed criteria list is treated the same way: a projection
    that cannot be read asserts nothing.
    """
    if not isinstance(plan, dict):
        return
    if plan.get("source") == "absent":
        return
    criteria = plan.get("acceptance_criteria")
    if not isinstance(criteria, list):
        return
    if not all(isinstance(c, dict) for c in criteria):
        return

    loc = "bundle.criterion_matrix.rows"
    if len(rows) != len(criteria):
        raise EvidenceSchemaError(
            f"{loc} must have exactly one row per accepted-plan criterion; "
            f"the plan declares {len(criteria)} "
            f"({[c.get('id') for c in criteria]}) but the matrix has "
            f"{len(rows)} ({[r.get('criterion_id') for r in rows]})"
        )
    if not criteria:
        return
    seen: set[str] = set()
    for i, (row, criterion) in enumerate(zip(rows, criteria, strict=True)):
        if row["criterion_id"] in seen:
            raise EvidenceSchemaError(
                f"{loc}[{i}] repeats criterion_id {row['criterion_id']!r}"
            )
        seen.add(row["criterion_id"])
        if row["criterion_id"] != criterion.get("id"):
            raise EvidenceSchemaError(
                f"{loc}[{i}].criterion_id is {row['criterion_id']!r} but the "
                f"accepted plan has {criterion.get('id')!r} at that position; "
                "rows follow plan order"
            )
        for key, plan_key in (("intent", "intent"), ("verify", "verify")):
            if row[key] != criterion.get(plan_key):
                raise EvidenceSchemaError(
                    f"{loc}[{i}].{key} is {row[key]!r} but the accepted plan "
                    f"criterion {row['criterion_id']} declares "
                    f"{criterion.get(plan_key)!r}"
                )
        expected_method = _expected_method(criterion)
        if row["method"] != expected_method:
            raise EvidenceSchemaError(
                f"{loc}[{i}].method does not project the accepted plan "
                f"criterion {row['criterion_id']}; expected {expected_method}, "
                f"got {row['method']}"
            )


def _expected_method(criterion: dict) -> dict:
    """The one method projection a plan criterion admits (ADR 0188 §4)."""
    verify = criterion.get("verify")
    if verify == "executable":
        return {
            "kind": "gates",
            "gate_refs": list(criterion.get("gate_refs") or ()),
        }
    if verify == "human":
        return {
            "kind": "manual",
            "instructions": criterion.get("human_instructions"),
        }
    return {"kind": "inspection"}


def _require_count(summary: dict, key: str, loc: str) -> None:
    value = summary[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvidenceSchemaError(
            f"{loc}.{key} must be a non-negative integer, got {value!r}"
        )


def _validate_criterion_row(row: Any, loc: str) -> None:
    from core.contracts.criteria import VERIFY_CLASSES
    from pipeline.criterion_matrix import (
        AGENT_STATES,
        CRITERION_STATES,
        EXECUTABLE_STATES,
        HUMAN_STATES,
    )

    if not isinstance(row, dict):
        raise EvidenceSchemaError(f"{loc} must be an object")
    missing = sorted(REQUIRED_CRITERION_ROW_KEYS - row.keys())
    if missing:
        raise EvidenceSchemaError(f"{loc} missing required keys: {missing}")

    for key in ("criterion_id", "intent"):
        if not isinstance(row[key], str) or not row[key].strip():
            raise EvidenceSchemaError(f"{loc}.{key} must be a non-empty string")
    if not isinstance(row["reason"], str):
        raise EvidenceSchemaError(f"{loc}.reason must be a string")
    if not isinstance(row["blocking"], bool):
        raise EvidenceSchemaError(f"{loc}.blocking must be a bool")

    if row["verify"] not in VERIFY_CLASSES:
        raise EvidenceSchemaError(
            f"{loc}.verify must be one of {list(VERIFY_CLASSES)}, got "
            f"{row['verify']!r}"
        )
    executors = row["executors"]
    if not isinstance(executors, list) or not executors:
        raise EvidenceSchemaError(f"{loc}.executors must be a non-empty list")
    if any(not isinstance(x, str) or not x.strip() for x in executors):
        raise EvidenceSchemaError(
            f"{loc}.executors must contain only non-empty strings"
        )
    if row["state"] not in CRITERION_STATES:
        raise EvidenceSchemaError(f"{loc}.state is not a known state")

    # The verification class fixes which method shape, which states, and which
    # blocking rule a row may carry. Anything else is a contradictory row.
    allowed_states, allowed_kind = {
        "executable": (EXECUTABLE_STATES, "gates"),
        "agent_assertion": (AGENT_STATES, "inspection"),
        "human": (HUMAN_STATES, "manual"),
    }[row["verify"]]
    if row["state"] not in allowed_states:
        raise EvidenceSchemaError(
            f"{loc}.state {row['state']!r} is not valid for verify "
            f"{row['verify']!r} (allowed: {sorted(allowed_states)})"
        )

    method = row["method"]
    if not isinstance(method, dict):
        raise EvidenceSchemaError(f"{loc}.method must be an object")
    kind = method.get("kind")
    expected = {
        "gates": {"kind", "gate_refs"},
        "inspection": {"kind"},
        "manual": {"kind", "instructions"},
    }.get(kind)
    if expected is None:
        raise EvidenceSchemaError(
            f"{loc}.method.kind must be gates|inspection|manual, got {kind!r}"
        )
    if kind != allowed_kind:
        raise EvidenceSchemaError(
            f"{loc}.method.kind must be {allowed_kind!r} for verify "
            f"{row['verify']!r}, got {kind!r}"
        )
    if set(method) != expected:
        raise EvidenceSchemaError(
            f"{loc}.method for kind {kind!r} must have exactly keys "
            f"{sorted(expected)}, got {sorted(method)}"
        )
    if kind == "gates":
        _validate_criterion_gate_refs(method["gate_refs"], f"{loc}.method")
    if kind == "manual" and (
        not isinstance(method["instructions"], str)
        or not method["instructions"].strip()
    ):
        raise EvidenceSchemaError(
            f"{loc}.method.instructions must be a non-empty string"
        )

    expected_blocking = {
        "executable": row["state"] != "proven",
        "agent_assertion": False,
        "human": row["state"] != "accepted",
    }[row["verify"]]
    if row["blocking"] is not expected_blocking:
        raise EvidenceSchemaError(
            f"{loc}.blocking must be {expected_blocking} for verify "
            f"{row['verify']!r} in state {row['state']!r}"
        )

    proof_refs = row["proof_refs"]
    if not isinstance(proof_refs, list):
        raise EvidenceSchemaError(f"{loc}.proof_refs must be a list")
    for j, ref in enumerate(proof_refs):
        if not isinstance(ref, dict) or set(ref) != {"kind", "id"}:
            raise EvidenceSchemaError(
                f"{loc}.proof_refs[{j}] must have exactly keys ['id', 'kind']"
            )
        if ref["kind"] not in CRITERION_PROOF_KINDS:
            raise EvidenceSchemaError(
                f"{loc}.proof_refs[{j}].kind is not a known proof kind"
            )
        if not isinstance(ref["id"], str) or not ref["id"].strip():
            raise EvidenceSchemaError(
                f"{loc}.proof_refs[{j}].id must be a non-empty string"
            )
    _validate_criterion_proof(row, loc)


def _validate_criterion_proof(row: dict, loc: str) -> None:
    """Pin which proof a row's ``(verify, state)`` pair may and must carry.

    A global "kind is one of four" check is not enough: it lets a ``proven``
    executable row ship with no receipt at all, and lets a ``pending`` human
    row cite proof it does not have. ADR 0188 fixes the pairing — receipts
    prove executables, claims/findings are advisory only, and a human decision
    is exactly the validated chain head.
    """
    verify, state = row["verify"], row["state"]
    kinds = [ref["kind"] for ref in row["proof_refs"]]

    if verify == "executable":
        if any(kind != "receipt" for kind in kinds):
            raise EvidenceSchemaError(
                f"{loc}.proof_refs for an executable criterion may only cite "
                f"receipts, got {sorted(set(kinds))}"
            )
        if state == "proven":
            gate_refs = row["method"]["gate_refs"]
            if len(kinds) != len(gate_refs):
                raise EvidenceSchemaError(
                    f"{loc} is proven but cites {len(kinds)} receipt(s) for "
                    f"{len(gate_refs)} gate identity(ies); a passing gate "
                    "without a canonical receipt is not proof"
                )
        return

    if verify == "agent_assertion":
        if any(kind not in {"claim", "finding"} for kind in kinds):
            raise EvidenceSchemaError(
                f"{loc}.proof_refs for an agent assertion may only cite "
                f"claims or findings, got {sorted(set(kinds))}"
            )
        if state == "advisory" and not kinds:
            raise EvidenceSchemaError(
                f"{loc} is advisory but cites no claim or finding"
            )
        if state == "pending" and kinds:
            raise EvidenceSchemaError(
                f"{loc} is pending but cites proof {kinds}"
            )
        return

    # human
    if state == "pending":
        if kinds:
            raise EvidenceSchemaError(
                f"{loc} is pending but cites proof {kinds}"
            )
        return
    if kinds != ["human_decision"]:
        raise EvidenceSchemaError(
            f"{loc} is {state} and must cite exactly one human_decision "
            f"(the validated chain head), got {kinds}"
        )


def _validate_criterion_gate_refs(gate_refs: Any, loc: str) -> None:
    """Validate a row's gate identities through the criterion schema itself."""
    from core.contracts.criteria import (
        CriterionSchemaError,
        validate_acceptance_criteria,
    )

    if not isinstance(gate_refs, list) or not gate_refs:
        raise EvidenceSchemaError(f"{loc}.gate_refs must be a non-empty list")
    # Reuse the plan-contract validator so the row can never carry an identity
    # shape the plan schema would have rejected.
    try:
        validate_acceptance_criteria(
            [{
                "id": "C1",
                "intent": "gate identity shape check",
                "verify": "executable",
                "gate_refs": gate_refs,
            }],
            where=loc,
        )
    except CriterionSchemaError as e:
        raise EvidenceSchemaError(f"{loc}.gate_refs is invalid: {e}") from e


def _validate_command_stalled_errors(errors: list) -> None:
    """Validate the shape of any ``command_stalled`` error records (ADR 0103).

    Additive + targeted: only records whose ``kind == "command_stalled"`` are
    checked; every other error-kind in the open ``errors`` list is left
    untouched, so this never rejects a pre-existing or plugin breadcrumb. Each
    command_stalled record must carry the durable diagnostic fields and a
    boolean ``terminal`` flag plus a ``recovery_actions`` list, so both the
    terminal and the live non-terminal path validate identically.
    """
    for i, entry in enumerate(errors):
        if not isinstance(entry, dict) or entry.get("kind") != "command_stalled":
            continue
        loc = f"bundle.errors[{i}]"
        missing = sorted(REQUIRED_COMMAND_STALLED_ERROR_KEYS - entry.keys())
        if missing:
            raise EvidenceSchemaError(
                f"{loc} (command_stalled) missing required keys: {missing}"
            )
        if not isinstance(entry["terminal"], bool):
            raise EvidenceSchemaError(
                f"{loc} (command_stalled).terminal must be a bool, got "
                f"{type(entry['terminal']).__name__}"
            )
        if not isinstance(entry["recovery_actions"], list):
            raise EvidenceSchemaError(
                f"{loc} (command_stalled).recovery_actions must be a list"
            )


def _validate_handoff_advice(advice: Any) -> None:
    """Light shape check for the additive ``handoff_advice`` digest (ADR 0093).

    Deliberately loose: the per-call field set is owned by the normalizer
    (``pipeline.project.handoff_advice_evidence``) and may grow additively, so
    this only asserts the outer envelope — ``calls`` is a list of objects and
    ``summary`` is an object — leaving the key fully optional.
    """
    if not isinstance(advice, dict):
        raise EvidenceSchemaError(
            f"bundle.handoff_advice must be an object, got "
            f"{type(advice).__name__}"
        )
    calls = advice.get("calls")
    if not isinstance(calls, list):
        raise EvidenceSchemaError("bundle.handoff_advice.calls must be a list")
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            raise EvidenceSchemaError(
                f"bundle.handoff_advice.calls[{i}] must be an object, got "
                f"{type(call).__name__}"
            )
    if not isinstance(advice.get("summary"), dict):
        raise EvidenceSchemaError(
            "bundle.handoff_advice.summary must be an object"
        )


def _validate_list_entries(
    entries: Any, name: str, required: frozenset[str],
) -> None:
    if not isinstance(entries, list):
        raise EvidenceSchemaError(f"bundle.{name} must be a list")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise EvidenceSchemaError(
                f"bundle.{name}[{i}] must be an object, got "
                f"{type(entry).__name__}"
            )
        missing = sorted(required - entry.keys())
        if missing:
            raise EvidenceSchemaError(
                f"bundle.{name}[{i}] missing required keys: {missing}"
            )


def _validate_artifact_apply_checks(artifacts: Any) -> None:
    for i, artifact in enumerate(artifacts):
        apply_check = artifact.get("apply_check")
        if apply_check is None:
            continue
        if not isinstance(apply_check, dict):
            raise EvidenceSchemaError(
                f"bundle.artifacts[{i}].apply_check must be an object"
            )
        status = apply_check.get("status")
        if status not in APPLY_CHECK_STATUSES:
            raise EvidenceSchemaError(
                f"bundle.artifacts[{i}].apply_check.status must be one "
                f"of {sorted(APPLY_CHECK_STATUSES)}, got {status!r}"
            )
        _validate_optional_str(
            apply_check, i, "reason", allow_none=False,
        )
        for key in ("cwd", "patch_path", "baseline_ref", "stdout", "stderr", "detail"):
            _validate_optional_str(apply_check, i, key, allow_none=True)
        command = apply_check.get("command")
        if command is not None and (
            not isinstance(command, list)
            or not all(isinstance(part, str) for part in command)
        ):
            raise EvidenceSchemaError(
                f"bundle.artifacts[{i}].apply_check.command "
                "must be a list of strings"
            )
        for key in ("stdout_truncated", "stderr_truncated"):
            if key in apply_check and not isinstance(apply_check[key], bool):
                raise EvidenceSchemaError(
                    f"bundle.artifacts[{i}].apply_check.{key} must be a bool"
                )


def _validate_optional_str(
    apply_check: dict[str, Any],
    artifact_index: int,
    key: str,
    *,
    allow_none: bool,
) -> None:
    if key not in apply_check:
        return
    value = apply_check[key]
    if allow_none and value is None:
        return
    if not isinstance(value, str):
        raise EvidenceSchemaError(
            f"bundle.artifacts[{artifact_index}].apply_check.{key} "
            "must be a string"
        )


# ── prompt_render strict schema (M12-C5) ─────────────────────────────────────
#
# The ``prompt_render`` section claims a strict contract: no extras,
# typed values, no raw prompt body. The plain ``_validate_list_entries``
# check is too soft — it accepts entries with extra keys (which is
# how a leaky writer would smuggle prompt text or part-key arrays
# into evidence) and accepts wrong-typed values (e.g.
# ``wire_chars=None``). ``_validate_prompt_render`` enforces the
# full contract.

#: Keys that must NEVER appear in a prompt_render entry. Catches
#: accidental raw prompt content or M12-C2 source artifacts that
#: the evidence projection deliberately strips.
_PROMPT_RENDER_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset({
    "prompt",
    "prompt_text",
    "wire_prompt",
    "body",
    "selected_part_keys",
    "omitted_part_keys",
    "delta_dropped_part_keys",
    # Source-side artifacts that the M12-C2 normalizer renames /
    # re-flattens before the evidence projection.
    "session_key",
    "physical_session_key",
})

#: Type expectations per field. ``None`` is permitted on the
#: explicitly-typed nullable slots only. ``bool`` is rejected for
#: int slots because ``isinstance(True, int)`` is True in Python
#: and would silently let a wrong-typed value through.
_PROMPT_RENDER_STR_FIELDS: Final[frozenset[str]] = frozenset({
    "phase",
    "phase_key",
    "trace_surface",
    "source_path",
    "render_mode",
    "session_split",
    "execution_mode",
    "prefix_hash",
    "payload_hash",
})
#: Optional-bool fields. The writer stamps ``continue_session`` on
#: every modern invocation, but synthetic fixtures and legacy
#: sources without a writer stamp surface as ``None`` — keep the
#: slot Optional so those bundles still validate. Explicit
#: ``bool`` check keeps an int from silently slipping through
#: (``isinstance(True, int)`` is True).
_PROMPT_RENDER_OPT_BOOL_FIELDS: Final[frozenset[str]] = frozenset({
    "continue_session",
})
_PROMPT_RENDER_INT_FIELDS: Final[frozenset[str]] = frozenset({
    "wire_chars",
    "selected_count",
    "omitted_count",
    "delta_dropped_count",
    "surface_count",
})
_PROMPT_RENDER_OPT_INT_FIELDS: Final[frozenset[str]] = frozenset({
    "attempt",
    "round",
})
_PROMPT_RENDER_OPT_STR_FIELDS: Final[frozenset[str]] = frozenset({
    "surface_id",
    "session_scope",
    "session_run_id",
    "session_runtime",
    "session_model",
    "provider_session_id",
})


def _validate_prompt_render(entries: Any) -> None:
    """Strict validation for ``evidence["prompt_render"]``.

    Beyond the baseline ``_validate_list_entries`` check this:

    - Rejects entries that contain ANY key outside the required set
      (an unrecognized key is a writer bug or a leak attempt; either
      way the operator should see it).
    - Rejects keys whose names are known leak vectors (raw prompt
      bodies, source part-key arrays, source-shape session_key).
    - Enforces value types per field: required strings, required
      ints (rejecting ``bool``), and Optional[int] / Optional[str]
      slots.
    """
    if not isinstance(entries, list):
        raise EvidenceSchemaError(
            "bundle.prompt_render must be a list",
        )
    for i, entry in enumerate(entries):
        loc = f"bundle.prompt_render[{i}]"
        if not isinstance(entry, dict):
            raise EvidenceSchemaError(
                f"{loc} must be an object, got {type(entry).__name__}"
            )

        # 1. Required keys present.
        keys = set(entry.keys())
        missing = sorted(REQUIRED_PROMPT_RENDER_KEYS - keys)
        if missing:
            raise EvidenceSchemaError(
                f"{loc} missing required keys: {missing}"
            )

        # 2. No forbidden leak vectors.
        leaked = sorted(_PROMPT_RENDER_FORBIDDEN_KEYS & keys)
        if leaked:
            raise EvidenceSchemaError(
                f"{loc} carries forbidden key(s): {leaked} — "
                "raw prompt content and source-shape artifacts must "
                "not appear in the evidence projection"
            )

        # 3. No extras at all (closed schema).
        extras = sorted(keys - REQUIRED_PROMPT_RENDER_KEYS)
        if extras:
            raise EvidenceSchemaError(
                f"{loc} has unexpected key(s): {extras}"
            )

        # 4. Type validation per field.
        for field in _PROMPT_RENDER_STR_FIELDS:
            value = entry[field]
            if not isinstance(value, str):
                raise EvidenceSchemaError(
                    f"{loc}.{field} must be str, got "
                    f"{type(value).__name__}"
                )
        for field in _PROMPT_RENDER_INT_FIELDS:
            value = entry[field]
            # ``isinstance(True, int)`` is True — reject bool explicitly
            # so a wrong-typed flag does not slip through the int slot.
            if not isinstance(value, int) or isinstance(value, bool):
                raise EvidenceSchemaError(
                    f"{loc}.{field} must be int, got "
                    f"{type(value).__name__}"
                )
        for field in _PROMPT_RENDER_OPT_BOOL_FIELDS:
            value = entry[field]
            if value is None:
                continue
            if not isinstance(value, bool):
                raise EvidenceSchemaError(
                    f"{loc}.{field} must be bool or None, got "
                    f"{type(value).__name__}"
                )
        for field in _PROMPT_RENDER_OPT_INT_FIELDS:
            value = entry[field]
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise EvidenceSchemaError(
                    f"{loc}.{field} must be int or None, got "
                    f"{type(value).__name__}"
                )
        for field in _PROMPT_RENDER_OPT_STR_FIELDS:
            value = entry[field]
            if value is None:
                continue
            if not isinstance(value, str):
                raise EvidenceSchemaError(
                    f"{loc}.{field} must be str or None, got "
                    f"{type(value).__name__}"
                )
