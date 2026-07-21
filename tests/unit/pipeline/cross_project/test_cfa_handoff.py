"""Phase A1 + A2 — cross_final_acceptance handoff payload builder +
checkpoint schema + ``evaluate_cfa_gate`` fresh-pause / resume-decide
flow.

These tests pin the wire shape of the new ``"cfa"`` handoff kind so MCP
/ SDK / UI consumers that route on ``phase_handoff_kind`` /
``phase_handoff_id`` / ``round_extras_key`` work for CFA the same way
they already work for ``cross_plan`` and project-proxy handoffs.

A2 (gate helper + resume routing) and A3 (CLI dispatch) build on this
surface. The contract pinned here:

* Id prefix is ``cfa:`` with grammar ``cfa:cross_final_acceptance:<N>``.
* ``round_extras_key`` is ``"cross_final_acceptance"``.
* ``available_actions`` narrow by source:
    - ``"agent"`` → full ``[continue, retry_feedback, halt]``.
    - ``"precondition"`` / ``"parse_error"`` → ``[continue, halt]``.
* ``artifacts`` carries enough to render the pause without re-invoking
  the reviewer: verdict, summary, source, release_blockers (and the
  parse_error text on the parse_error branch).
* ``last_output`` is bounded by the existing
  ``_CROSS_HANDOFF_LAST_OUTPUT_MAX`` truncation discipline.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline.cross_project.handoff_payloads import (
    _CROSS_HANDOFF_LAST_OUTPUT_MAX,
    CFA_ROUND_KEY,
    build_cfa_handoff_payload,
)

# ── helpers ───────────────────────────────────────────────────────────


def _make_parsed(
    *,
    verdict: str = "REJECTED",
    short_summary: str = "Bundle rejected by reviewer.",
    blockers: list[dict] | None = None,
) -> SimpleNamespace:
    """Shape-match :class:`pipeline.release_parser.ParsedRelease`'s
    fields the builder reads: ``verdict``, ``short_summary``,
    ``blockers_as_dicts()``."""
    return SimpleNamespace(
        verdict=verdict,
        short_summary=short_summary,
        blockers_as_dicts=lambda: list(blockers or []),
    )


def _make_cfa_result(
    *,
    source: str = "agent",
    parsed: SimpleNamespace | None = None,
    raw_output: str = "raw release JSON output",
    parse_error: str | None = None,
) -> SimpleNamespace:
    """Shape-match the fields the builder reads off
    :class:`pipeline.cross_project.final_acceptance.CrossFinalAcceptanceResult`."""
    return SimpleNamespace(
        parsed=parsed if parsed is not None else _make_parsed(),
        source=source,
        raw_output=raw_output,
        parse_error=parse_error,
    )


# ── id / round grammar ────────────────────────────────────────────────


def test_cfa_handoff_id_prefix_and_round_grammar() -> None:
    """The handoff id must follow ``cfa:cross_final_acceptance:<N>`` so
    the cross CLI resume router (added in A3) and post-mortem tooling
    can route off the prefix AND parse the round suffix the same way
    they parse cross_plan ids."""
    payload = build_cfa_handoff_payload(
        round_n=1,
        max_rounds=2,
        cfa_result=_make_cfa_result(),
    )
    assert payload["id"] == "cfa:cross_final_acceptance:1"
    assert payload["phase"] == "cross_final_acceptance"
    assert payload["round"] == 1
    assert payload["loop_max_rounds"] == 2
    assert payload["round_extras_key"] == CFA_ROUND_KEY
    assert payload["round_extras_key"] == "cross_final_acceptance"


def test_cfa_handoff_advances_round_on_retry() -> None:
    """retry_feedback re-pause must bump the round suffix (and not
    overwrite the round-1 entry). A2's gate helper does the
    persistence; this test pins the builder produces distinct ids for
    sequential rounds so consumers can tell them apart."""
    p1 = build_cfa_handoff_payload(
        round_n=1, max_rounds=3, cfa_result=_make_cfa_result(),
    )
    p2 = build_cfa_handoff_payload(
        round_n=2, max_rounds=3, cfa_result=_make_cfa_result(),
    )
    assert p1["id"] != p2["id"]
    assert p2["id"] == "cfa:cross_final_acceptance:2"
    assert p2["round"] == 2


# ── available_actions narrowing per source ────────────────────────────


def test_cfa_payload_omits_retry_feedback_until_a2c() -> None:
    """Until A2c builds the feedback-aware reviewer, ``retry_feedback``
    is narrowed out of the payload for ALL sources (incl. ``"agent"``,
    where the reviewer ran). Advertising it would dead-end the operator
    in a ``NotImplementedError``. Recovery contract = continue / halt.
    A2c re-adds retry_feedback for source=="agent"."""
    for src in ("agent", "precondition", "parse_error"):
        payload = build_cfa_handoff_payload(
            round_n=1, max_rounds=2,
            cfa_result=_make_cfa_result(source=src),
        )
        assert payload["available_actions"] == ["continue", "halt"], (
            f"source={src!r} must narrow to [continue, halt] until A2c"
        )
        assert "retry_feedback" not in payload["available_actions"]


def test_cfa_handoff_precondition_source_omits_retry_feedback() -> None:
    """``source == "precondition"`` means CFA REJECTED before invoking
    the reviewer (e.g. missing per-alias release verdict, contract_check
    blocking skip). retry_feedback has no reviewer to feed and must be
    omitted from the action set; the operator's only positive action is
    ``continue`` (override) or ``halt`` (give up)."""
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=2,
        cfa_result=_make_cfa_result(source="precondition"),
    )
    assert payload["available_actions"] == ["continue", "halt"]
    assert "retry_feedback" not in payload["available_actions"]


def test_cfa_handoff_parse_error_source_omits_retry_feedback() -> None:
    """``source == "parse_error"`` — the reviewer returned output we
    could not parse. Retrying with the same prompt is unlikely to
    yield a different shape, so retry_feedback is also omitted; the
    operator decision narrows to continue or halt."""
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=2,
        cfa_result=_make_cfa_result(source="parse_error",
                                    parse_error="invalid JSON"),
    )
    assert payload["available_actions"] == ["continue", "halt"]


# ── artifacts: enough to render without re-invoking reviewer ──────────


def test_cfa_handoff_artifacts_carries_resume_state() -> None:
    """``artifacts`` must persist enough fields that a fresh process
    can render the pause UI on resume without re-invoking the
    reviewer: verdict, short_summary, source, release_blockers
    list. Identical shape to what
    ``session["phases"]["cross_final_acceptance"]`` carries for the
    non-pause REJECT path (consumers reading either field see the
    same data)."""
    blockers = [
        {
            "id": "B1",
            "severity": "release",
            "title": "Contract drift on /api/users",
            "body": "Field name mismatch.",
            "required_fix": "Rename web field to email.",
            "why_blocks_release": "Persistence depends on email key.",
        },
    ]
    cfa_result = _make_cfa_result(
        parsed=_make_parsed(
            verdict="REJECTED",
            short_summary="Bundle rejected: contract drift detected.",
            blockers=blockers,
        ),
        source="agent",
    )
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=2, cfa_result=cfa_result,
    )

    artifacts = payload["artifacts"]
    assert artifacts["verdict"] == "REJECTED"
    assert artifacts["short_summary"] == (
        "Bundle rejected: contract drift detected."
    )
    assert artifacts["source"] == "agent"
    assert artifacts["release_blockers"] == blockers
    assert "parse_error" not in artifacts


def test_cfa_handoff_parse_error_includes_error_text_in_artifacts() -> None:
    """On the ``parse_error`` branch the artifacts carry the parser's
    exception text so the operator has forensic context. The
    structural decision is still ``continue`` / ``halt`` (narrowed
    actions), but the parse error message tells the operator WHY they
    cannot retry the reviewer to fix the gate."""
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=2,
        cfa_result=_make_cfa_result(
            source="parse_error",
            parse_error="Expected one JSON object, got 0.",
        ),
    )
    assert payload["artifacts"]["parse_error"] == (
        "Expected one JSON object, got 0."
    )


# ── last_output truncation ────────────────────────────────────────────


def test_cfa_handoff_last_output_truncated() -> None:
    """``last_output`` must respect the existing cross-handoff
    truncation discipline so the persisted ``meta.phase_handoff``
    field stays bounded. The CFA path reuses the same upper bound as
    cross_plan."""
    big_output = "x" * (_CROSS_HANDOFF_LAST_OUTPUT_MAX + 5000)
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=2,
        cfa_result=_make_cfa_result(raw_output=big_output),
    )
    assert len(payload["last_output"]) == _CROSS_HANDOFF_LAST_OUTPUT_MAX


def test_cfa_handoff_raw_output_override() -> None:
    """Callers (the gate helper at A2) may want to pass a different
    raw text than ``cfa_result.raw_output`` (e.g. the multi-round
    history's current attempt). The ``raw_output=`` kwarg overrides."""
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=2,
        cfa_result=_make_cfa_result(raw_output="from cfa_result"),
        raw_output="from caller",
    )
    assert payload["last_output"] == "from caller"


# ── shared type + trigger fields ──────────────────────────────────────


def test_cfa_handoff_type_and_trigger_match_cross_plan_shape() -> None:
    """The ``type`` + ``trigger`` + ``approved`` fields are byte-
    identical to the cross_plan payload so MCP / SDK consumers that
    already route on these fields work unchanged for CFA. The only
    cross_plan / CFA differentiators are the id prefix + phase tag +
    round_extras_key."""
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=2, cfa_result=_make_cfa_result(),
    )
    assert payload["type"] == "human_feedback_on_reject"
    assert payload["trigger"] == "rejected"
    assert payload["approved"] is False
    assert payload["verdict"] == "REJECTED"


def test_cfa_handoff_empty_summary_yields_empty_string_not_none() -> None:
    """Defensive: a ParsedRelease whose short_summary is ``""`` or
    ``None`` must not propagate ``None`` into the artifacts dict —
    consumers expect ``str`` and would crash on a JSON serialization
    round-trip with a None there."""
    parsed = _make_parsed(short_summary="")
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=2,
        cfa_result=_make_cfa_result(parsed=parsed),
    )
    assert payload["artifacts"]["short_summary"] == ""
    assert isinstance(payload["artifacts"]["short_summary"], str)


# ── back-compat: the existing cross_plan + project builders unchanged

def test_cross_plan_builder_still_uses_cross_plan_round_key() -> None:
    """Regression guard: adding CFA_ROUND_KEY must not collapse the
    cross_plan key. They are distinct namespaces in
    ``meta.phase_handoff.round_extras_key`` and post-mortem tooling
    relies on that distinction to route between the two pause kinds."""
    from pipeline.cross_project.handoff_payloads import (
        CROSS_PLAN_ROUND_KEY,
        build_cross_plan_handoff_payload,
    )
    payload = build_cross_plan_handoff_payload(
        round_n=1, max_rounds=2, plan_review_dict=None, plan_output="x",
    )
    assert payload["round_extras_key"] == CROSS_PLAN_ROUND_KEY
    assert CROSS_PLAN_ROUND_KEY != CFA_ROUND_KEY


# ── builder is pure (no I/O, no session mutation) ─────────────────────


def test_cfa_handoff_builder_is_pure() -> None:
    """Builder must not touch the filesystem, not mutate the cfa_result,
    not import the runtime path. A1 ships only the data shape; the
    persistence (apply_cross_phase_handoff_pause) and the gate
    decision are A2/A3 surfaces."""
    cfa_result = _make_cfa_result()
    original_source = cfa_result.source
    original_raw = cfa_result.raw_output

    build_cfa_handoff_payload(
        round_n=1, max_rounds=2, cfa_result=cfa_result,
    )

    assert cfa_result.source == original_source
    assert cfa_result.raw_output == original_raw


# ── builder rejects invalid round_n early ─────────────────────────────


def test_cfa_handoff_round_n_must_be_positive() -> None:
    """round_n is a 1-based attempt counter. Zero or negative values
    would silently produce an id like ``cfa:cross_final_acceptance:0``
    that resume tooling cannot interpret. The builder does NOT enforce
    this today (mirror cross_plan_builder behaviour) — this test pins
    that intentional non-enforcement so future tightening is a
    deliberate change, not a surprise."""
    # No exception raised; the builder produces a payload with the
    # caller-provided round_n verbatim. Pins the behaviour explicitly.
    payload = build_cfa_handoff_payload(
        round_n=0, max_rounds=2, cfa_result=_make_cfa_result(),
    )
    assert payload["round"] == 0
    assert payload["id"].endswith(":0")

    # Negative — same non-enforcement, documented.
    payload_neg = build_cfa_handoff_payload(
        round_n=-1, max_rounds=2, cfa_result=_make_cfa_result(),
    )
    assert payload_neg["round"] == -1


# ── max_rounds passed through ─────────────────────────────────────────


@pytest.mark.parametrize("max_rounds", [1, 2, 3, 5])
def test_cfa_handoff_max_rounds_passthrough(max_rounds: int) -> None:
    """``loop_max_rounds`` is what the UI / CLI shows as ``round N of
    M``. A1 passes it through; A2's gate helper decides what value to
    pass based on the config."""
    payload = build_cfa_handoff_payload(
        round_n=1, max_rounds=max_rounds, cfa_result=_make_cfa_result(),
    )
    assert payload["loop_max_rounds"] == max_rounds


# ══════════════════════════════════════════════════════════════════════
# Phase A2 — ``evaluate_cfa_gate`` fresh-pause + resume-decide
# ══════════════════════════════════════════════════════════════════════


def _make_real_cfa_result(
    *,
    approved: bool,
    source: str = "agent",
    short_summary: str = "test",
):
    """Build a real :class:`CrossFinalAcceptanceResult` (not a
    SimpleNamespace stub) — required by the gate's persistence path,
    which calls ``result_to_phase_log_entry`` on the result."""
    from pipeline.cross_project.final_acceptance import (
        CrossFinalAcceptanceResult,
    )
    from pipeline.release_parser import ContractStatus, ParsedRelease

    parsed = ParsedRelease(
        verdict="APPROVED" if approved else "REJECTED",
        ship_ready=approved,
        short_summary=short_summary,
        release_blockers=(),
        verification_gaps=(),
        contract_status=ContractStatus(
            task_contract="satisfied" if approved else "violated",
            interfaces="compatible",
            persistence="safe",
            tests="sufficient",
        ),
    )
    return CrossFinalAcceptanceResult(
        parsed=parsed,
        source=source,
        raw_output="",
        rendered="",
        duration_s=0.0,
        prompt_text="test prompt",  # so token capture path runs
    )


# ── fresh-pause path ──────────────────────────────────────────────────


def test_gate_fresh_reject_persists_pause_and_returns_paused(tmp_path):
    """Fresh CFA REJECTED → gate persists the pause via
    ``apply_cross_phase_handoff_pause`` and returns ``paused``. The
    cross runner must exit without finalizing (no ``run.end``)."""
    from pipeline.cross_project.cfa_gate import evaluate_cfa_gate

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session: dict = {"phases": {}}
    cross_ckpt: dict = {"sub_status": {}}

    # Monkeypatch run_cross_final_acceptance to return REJECTED.
    rejected = _make_real_cfa_result(
        approved=False, source="agent",
        short_summary="Bundle rejected by reviewer.",
    )

    import pipeline.cross_project.cfa_gate as gate_mod
    original_runner = gate_mod.__dict__.get("run_cross_final_acceptance")
    # The gate imports lazily; monkeypatch the source module instead.
    import pipeline.cross_project.final_acceptance as fa_mod
    original = fa_mod.run_cross_final_acceptance
    fa_mod.run_cross_final_acceptance = lambda _ctx, **_kw: rejected
    try:
        outcome = evaluate_cfa_gate(
            cfa_ctx=None,  # not used by the monkeypatched runner
            codex=None,
            dry_run=False,
            run_dir=run_dir,
            session=session,
            cross_ckpt=cross_ckpt,
            cross_phase_usage={},
            resume_from=None,
            output_dir=True,
            terminal=False,  # silent: no warn line
        )
    finally:
        fa_mod.run_cross_final_acceptance = original
        del original_runner  # silence unused

    assert outcome.outcome == "paused"
    assert outcome.cfa_result is rejected
    # Pause persisted state.
    assert session["status"] == "awaiting_phase_handoff"
    assert session["phase_handoff"]["id"].startswith("cfa:")
    # Checkpoint markers stamped.
    assert cross_ckpt["phase_handoff_pending"] is True
    assert cross_ckpt["phase_handoff_kind"] == "cfa"
    assert cross_ckpt["phase_handoff_id"].startswith("cfa:")
    paused_state = cross_ckpt["cfa_paused_state"]
    assert paused_state["verdict"] == "REJECTED"
    assert paused_state["source"] == "agent"
    assert paused_state["round"] == 1
    # Phase log entry persisted (the resume path reads from here).
    assert session["phases"]["cross_final_acceptance"]["verdict"] == "REJECTED"


def test_gate_fresh_approved_returns_terminal_no_pause(tmp_path):
    """Fresh CFA APPROVED → no pause, no checkpoint markers, gate
    returns ``approved_terminal`` so the caller proceeds to the
    existing finalize path."""
    from pipeline.cross_project.cfa_gate import evaluate_cfa_gate

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session: dict = {"phases": {}}
    cross_ckpt: dict = {"sub_status": {}}

    approved = _make_real_cfa_result(approved=True)

    import pipeline.cross_project.final_acceptance as fa_mod
    original = fa_mod.run_cross_final_acceptance
    fa_mod.run_cross_final_acceptance = lambda _ctx, **_kw: approved
    try:
        outcome = evaluate_cfa_gate(
            cfa_ctx=None,
            codex=None,
            dry_run=False,
            run_dir=run_dir,
            session=session,
            cross_ckpt=cross_ckpt,
            cross_phase_usage={},
            resume_from=None,
            output_dir=True,
            terminal=False,
        )
    finally:
        fa_mod.run_cross_final_acceptance = original

    assert outcome.outcome == "approved_terminal"
    assert outcome.cfa_result is approved
    assert session.get("status") != "awaiting_phase_handoff"
    assert "phase_handoff_kind" not in cross_ckpt
    assert "cfa_paused_state" not in cross_ckpt


# ── resume-decide path ────────────────────────────────────────────────


def _write_decision_artifact(
    run_dir, handoff_id: str, *, action: str, feedback: str = "",
    note: str | None = None,
):
    """Stamp a decision artifact in the layout
    ``load_handoff_decision`` reads from."""
    import json as _json

    from sdk import safe_handoff_id

    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "handoff_id": handoff_id,
        "phase": "cross_final_acceptance",
        "action": action,
        "feedback": feedback,
        "note": note,
        "decided_at": "2026-01-01T00:00:00Z",
    }
    (decisions_dir / f"{safe_handoff_id(handoff_id)}.json").write_text(
        _json.dumps(payload), encoding="utf-8",
    )


def _seed_paused_session(run_dir, *, source: str = "agent") -> dict:
    """Construct a session as it would look right after a CFA pause —
    phase_log entry persisted, checkpoint marker set, status
    awaiting."""
    cfa_entry_source = source
    return {
        "status": "awaiting_phase_handoff",
        "phases": {
            "cross_final_acceptance": {
                "verdict": "REJECTED",
                "ship_ready": False,
                "short_summary": "Original reviewer rejected.",
                "source": cfa_entry_source,
                "release_blockers": [],
                "rendered": "rendered md",
                "duration_s": 0.5,
                "raw_output": "raw",
                "contract_status": {
                    "task_contract": "violated",
                    "interfaces": "compatible",
                    "persistence": "safe",
                    "tests": "sufficient",
                },
            },
        },
        "phase_handoff": {
            "id": "cfa:cross_final_acceptance:1",
            "phase": "cross_final_acceptance",
        },
    }


def test_gate_resume_halt_writes_terminal_and_clears_markers(tmp_path):
    """Resume with halt → ``finalize_cross_terminal`` writes
    ``status="halted"`` + ``halt_reason="phase_handoff_halt"`` +
    evidence; the gate clears every CFA-specific checkpoint marker so
    a subsequent resume cannot re-enter the gate on stale state."""
    from pipeline.cross_project.cfa_gate import evaluate_cfa_gate

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    handoff_id = "cfa:cross_final_acceptance:1"
    _write_decision_artifact(run_dir, handoff_id, action="halt")

    session = _seed_paused_session(run_dir)
    cross_ckpt = {
        "phase_handoff_pending": True,
        "phase_handoff_id": handoff_id,
        "phase_handoff_kind": "cfa",
        "cfa_paused_state": {
            "verdict": "REJECTED",
            "source": "agent",
            "round": 1,
            "summary": "...",
            "blockers_count": 0,
        },
        "sub_status": {},
    }

    outcome = evaluate_cfa_gate(
        cfa_ctx=None, codex=None, dry_run=False,
        run_dir=run_dir,
        session=session,
        cross_ckpt=cross_ckpt,
        cross_phase_usage=None,
        resume_from=run_dir.name,
        output_dir=True,
        terminal=False,
    )

    assert outcome.outcome == "halted"
    assert outcome.cfa_result is None
    assert outcome.override_marker is None
    # Terminal state written.
    assert session["status"] == "halted"
    assert session["halt_reason"] == "phase_handoff_halt"
    # Checkpoint markers cleared.
    assert cross_ckpt["phase_handoff_pending"] is False
    assert "phase_handoff_id" not in cross_ckpt
    assert "phase_handoff_kind" not in cross_ckpt
    assert "cfa_paused_state" not in cross_ckpt


def test_gate_resume_continue_synthesises_approved_result(tmp_path):
    """Resume with continue → gate returns a synthesised APPROVED CFA
    result so the existing finalize path flows through the APPROVED
    branch. The override marker is built and returned via
    ``CfaGateOutcome.override_marker`` for the caller to re-apply
    after refreshing the phase_log entry. Checkpoint markers are
    cleared."""
    from pipeline.cross_project.cfa_gate import evaluate_cfa_gate

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    handoff_id = "cfa:cross_final_acceptance:1"
    _write_decision_artifact(
        run_dir, handoff_id, action="continue",
        note="Operator confirmed risk is acceptable for this release.",
    )

    session = _seed_paused_session(run_dir)
    cross_ckpt = {
        "phase_handoff_pending": True,
        "phase_handoff_id": handoff_id,
        "phase_handoff_kind": "cfa",
        "cfa_paused_state": {
            "verdict": "REJECTED",
            "source": "agent",
            "round": 1,
            "summary": "...",
            "blockers_count": 0,
        },
        "sub_status": {},
    }

    outcome = evaluate_cfa_gate(
        cfa_ctx=None, codex=None, dry_run=False,
        run_dir=run_dir,
        session=session,
        cross_ckpt=cross_ckpt,
        cross_phase_usage=None,
        resume_from=run_dir.name,
        output_dir=True,
        terminal=False,
    )

    assert outcome.outcome == "override_continue"
    assert outcome.cfa_result is not None
    # Synthesised: verdict=APPROVED, ship_ready=True so the finalizer
    # flows through the APPROVED branch of _decide_status.
    assert outcome.cfa_result.parsed.verdict == "APPROVED"
    assert outcome.cfa_result.parsed.approved is True
    # The synthesised result's ``source`` MUST be the distinct
    # ``operator_override`` sentinel — NOT the original ``source`` —
    # so the finalizer's ``_decide_status`` does not force-fail it
    # (it force-fails ``source == "parse_error"`` regardless of
    # verdict). Pinned by the parse_error regression test below.
    assert outcome.cfa_result.source == "operator_override"
    # Override marker carries the audit fields (original verdict +
    # source preserved here, not on the synthesised result).
    marker = outcome.override_marker
    assert marker is not None
    assert marker["action"] == "continue"
    assert marker["preserved_verdict"] == "REJECTED"
    assert marker["preserved_source"] == "agent"
    assert (
        "Operator confirmed risk" in marker["note"]
    )
    # Override marker also stamped on session phase entry.
    cfa_entry = session["phases"]["cross_final_acceptance"]
    assert cfa_entry["override"]["action"] == "continue"
    # Checkpoint markers cleared.
    assert cross_ckpt["phase_handoff_pending"] is False
    assert "phase_handoff_kind" not in cross_ckpt
    assert "cfa_paused_state" not in cross_ckpt


def test_parse_error_override_synthesises_non_failing_source(tmp_path):
    """Codex P1 regression — a continue-override on a ``parse_error``
    pause must finalize as ``done``, not silently fail. The finalizer
    force-fails ``cfa.source == "parse_error"`` independently of the
    verdict, so the synthesised override result must NOT carry that
    source. Pins ``source == "operator_override"`` end to end and that
    the finalizer's ``_decide_status`` then returns ``done``."""
    from pipeline.cross_project.cfa_gate import (
        _synthesise_approved_for_override,
    )
    from pipeline.cross_project.finalization import (
        CrossFinalizationContext,
        _decide_status,
    )

    # Original reviewer result with source="parse_error".
    original = _make_real_cfa_result(
        approved=False, source="parse_error",
        short_summary="Reviewer returned non-JSON.",
    )
    synthetic = _synthesise_approved_for_override(
        original, "operator forced ship past unparseable reviewer output",
    )
    assert synthetic.source == "operator_override"
    assert synthetic.parsed.approved is True

    # Feed the synthetic into the finalizer's decision tree — it must
    # return done (not failed). Pre-fix this returned
    # ("failed", "cross_final_acceptance_parse_error", ...).
    ctx = CrossFinalizationContext(
        run_dir=tmp_path / "run",
        output_dir=False,
        session={"phases": {}},
        projects={"api": tmp_path / "api"},
        max_rounds=2,
        cfa_result=synthetic,
        contract_results={},
        contract_check_failed=False,
        contract_check_failure_reason=None,
        cross_phase_usage={},
    )
    status, halt_reason, _failure_reason, _skipped = _decide_status(ctx)
    assert status == "done", (
        f"override on parse_error must finalize done; got {status!r} "
        f"(halt_reason={halt_reason!r})"
    )


def test_load_prior_cfa_result_reconstructs_blockers_and_gaps():
    """Codex P1 secondary — ``_load_prior_cfa_result`` must reconstruct
    BOTH release_blockers and verification_gaps from the persisted
    phase_log entry (the entry carries both via
    ``result_to_phase_log_entry``). Pre-fix gaps were dropped to an
    empty tuple."""
    from pipeline.cross_project.cfa_gate import _load_prior_cfa_result

    session = {
        "phases": {
            "cross_final_acceptance": {
                "verdict": "REJECTED",
                "ship_ready": False,
                "short_summary": "rejected",
                "source": "agent",
                "release_blockers": [
                    {
                        "id": "B1", "severity": "release",
                        "title": "blocker", "body": "b",
                        "required_fix": "fix", "why_blocks_release": "why",
                    },
                ],
                "verification_gaps": [
                    {
                        "risk": "untested path",
                        "missing_evidence": "no e2e",
                        "required_check": "run e2e",
                    },
                ],
                "contract_status": {
                    "task_contract": "violated", "interfaces": "broken",
                    "persistence": "safe", "tests": "weak",
                },
            },
        },
    }
    result = _load_prior_cfa_result(session)
    assert result is not None
    assert len(result.parsed.release_blockers) == 1
    assert result.parsed.release_blockers[0].id == "B1"
    assert len(result.parsed.verification_gaps) == 1
    assert result.parsed.verification_gaps[0].risk == "untested path"
    assert result.parsed.contract_status.interfaces == "broken"


def test_load_prior_cfa_result_reads_output_key():
    """Bugfix — ``result_to_phase_log_entry`` persists the rendered
    markdown under the ``"output"`` key, NOT ``"rendered"``.
    ``_load_prior_cfa_result`` must read ``"output"`` so a resumed
    override result carries the original verdict markdown (otherwise
    the resume preview prints an empty "Cross-final-acceptance
    verdict:" block — the live bug)."""
    from pipeline.cross_project.cfa_gate import _load_prior_cfa_result

    session = {
        "phases": {
            "cross_final_acceptance": {
                "verdict": "REJECTED",
                "ship_ready": False,
                "short_summary": "rejected",
                "source": "agent",
                # The real persisted key is "output", not "rendered".
                "output": "# System release gate\n\n**Verdict:** REJECTED\n",
                "release_blockers": [],
                "verification_gaps": [],
                "contract_status": {
                    "task_contract": "violated", "interfaces": "broken",
                    "persistence": "safe", "tests": "weak",
                },
            },
        },
    }
    result = _load_prior_cfa_result(session)
    assert result is not None
    assert "System release gate" in result.rendered, (
        "rendered must be reconstructed from the 'output' key, not empty"
    )


def test_gate_resume_retry_feedback_raises_until_a2c(tmp_path):
    """Until Phase A2c extends the CFA reviewer API with a feedback
    parameter, the retry_feedback action must raise rather than
    silently treating it as continue or halt — either would corrupt
    the operator's intent."""
    from pipeline.cross_project.cfa_gate import evaluate_cfa_gate

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    handoff_id = "cfa:cross_final_acceptance:1"
    _write_decision_artifact(
        run_dir, handoff_id, action="retry_feedback",
        feedback="Try harder; the blocker is bogus.",
    )

    session = _seed_paused_session(run_dir)
    cross_ckpt = {
        "phase_handoff_pending": True,
        "phase_handoff_id": handoff_id,
        "phase_handoff_kind": "cfa",
        "cfa_paused_state": {
            "verdict": "REJECTED", "source": "agent", "round": 1,
            "summary": "...", "blockers_count": 0,
        },
        "sub_status": {},
    }

    with pytest.raises(NotImplementedError) as exc:
        evaluate_cfa_gate(
            cfa_ctx=None, codex=None, dry_run=False,
            run_dir=run_dir, session=session, cross_ckpt=cross_ckpt,
            cross_phase_usage=None, resume_from=run_dir.name,
            output_dir=True, terminal=False,
        )
    assert "A2c" in str(exc.value)


def test_gate_only_routes_cfa_kind_into_resume_decide(tmp_path):
    """Phase A invariant 1 — kind-aware resume routing. The gate's
    resume-decide branch fires ONLY when ``phase_handoff_kind ==
    "cfa"``. A pending handoff with kind="plan" (or kind="project")
    must fall through to the fresh path so the planning_loop / project
    proxy resume routes own those kinds — never the CFA gate."""
    from pipeline.cross_project.cfa_gate import evaluate_cfa_gate

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Set up a checkpoint with phase_handoff_pending=True but
    # kind="plan" (a cross_plan rejection pause). The gate must NOT
    # try to load a decision artifact for this — it falls through to
    # the fresh CFA run.
    session: dict = {"phases": {}}
    cross_ckpt: dict = {
        "phase_handoff_pending": True,
        "phase_handoff_id": "cross_plan:cross_plan_round:1",
        "phase_handoff_kind": "plan",
        "sub_status": {},
    }

    approved = _make_real_cfa_result(approved=True)
    import pipeline.cross_project.final_acceptance as fa_mod
    original = fa_mod.run_cross_final_acceptance
    runner_called = []
    def _runner(_ctx, **_kw):
        runner_called.append(True)
        return approved
    fa_mod.run_cross_final_acceptance = _runner
    try:
        outcome = evaluate_cfa_gate(
            cfa_ctx=None, codex=None, dry_run=False,
            run_dir=run_dir, session=session, cross_ckpt=cross_ckpt,
            cross_phase_usage={}, resume_from=run_dir.name,
            output_dir=True, terminal=False,
        )
    finally:
        fa_mod.run_cross_final_acceptance = original

    # Fresh path fired (runner called); resume-decide did NOT.
    assert runner_called == [True]
    assert outcome.outcome == "approved_terminal"


def test_dispatch_preserves_completed_child_sessions_on_resume(tmp_path):
    """Phase A invariant 2 — when ``run_project_dispatch`` is re-entered
    on a CFA-pause resume, it must preserve previously-completed child
    sessions (``sub_status == "done"``) under
    ``session["phases"]["projects"][alias]``. Without preservation,
    Phase B's cross delivery would lose access to the worktree path,
    final_acceptance verdict, and metrics for committed aliases."""
    from unittest.mock import MagicMock

    from pipeline.cross_project.project_dispatch import (
        ProjectDispatchContext,
        run_project_dispatch,
    )

    api_dir = tmp_path / "api"
    web_dir = tmp_path / "web"
    api_dir.mkdir()
    web_dir.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Seed session AS IF the cross run paused on CFA mid-flight:
    # ``api`` finished (sub_status=done, worktree + verdict persisted)
    # but ``web`` did not (sub_status absent). The dispatch resume
    # must preserve api's entry while clearing web's (so web can be
    # re-dispatched).
    completed_api_entry = {
        "status": "done",
        "worktree": {"path": str(tmp_path / "wt_api_checkout")},
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED", "ship_ready": True,
            },
        },
    }
    session = {
        "phases": {
            "projects": {
                "api": dict(completed_api_entry),
                # Stale web entry from a prior failed attempt — must
                # be cleared so re-dispatch is fresh.
                "web": {"status": "failed", "error": "stale"},
            },
        },
    }
    cross_ckpt = {
        "sub_status": {"api": "done"},  # web omitted = not done
    }
    (run_dir / "api").mkdir()
    (run_dir / "api" / "meta.json").write_text(
        json.dumps(completed_api_entry),
        encoding="utf-8",
    )

    # ``child_profile=None`` short-circuits past the dispatch loop, so
    # the test isolates the preservation logic without driving real
    # children. The function returns paused=False; the assertions
    # below verify the session state BEFORE the short-circuit returns.
    ctx = ProjectDispatchContext(
        task="resume test",
        projects={"api": api_dir, "web": web_dir},
        task_plan=None,
        resume_from=None,
        dry_run=False,
        max_rounds=2,
        code_model="stub",
        phase_config=None,
        child_profile=None,  # short-circuits the dispatch loop
        requested_profile_name="advanced",
        has_global_plan=True,
        provider=MagicMock(),
        hypothesis_enabled=False,
        followup_session_seeds_per_alias=None,
        run_dir=run_dir,
        output_dir=False,
        plan_output="",
        plan_review_dict=None,
        cross_ckpt=cross_ckpt,
        session=session,
        cross_phase_usage={},
        ports=MagicMock(),
        terminal=False,
    )
    # The ports object is opaque; only the ``success`` callable is
    # invoked on the short-circuit branch.
    ctx.ports.success = MagicMock()

    result = run_project_dispatch(ctx)

    assert result.paused is False
    # api preserved verbatim.
    assert session["phases"]["projects"]["api"] == completed_api_entry
    # web cleared (sub_status != "done").
    assert "web" not in session["phases"]["projects"]


def test_gate_no_resume_from_skips_resume_decide(tmp_path):
    """Even with a pending CFA handoff on the checkpoint, the gate
    must not enter the resume-decide branch when ``resume_from`` is
    None — that's a corrupt-checkpoint case (resume-implied state
    without a resume call). Fresh path fires."""
    from pipeline.cross_project.cfa_gate import evaluate_cfa_gate

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    session: dict = {"phases": {}}
    cross_ckpt: dict = {
        "phase_handoff_pending": True,
        "phase_handoff_id": "cfa:cross_final_acceptance:1",
        "phase_handoff_kind": "cfa",
        "sub_status": {},
    }

    approved = _make_real_cfa_result(approved=True)
    import pipeline.cross_project.final_acceptance as fa_mod
    original = fa_mod.run_cross_final_acceptance
    fa_mod.run_cross_final_acceptance = lambda _ctx, **_kw: approved
    try:
        outcome = evaluate_cfa_gate(
            cfa_ctx=None, codex=None, dry_run=False,
            run_dir=run_dir, session=session, cross_ckpt=cross_ckpt,
            cross_phase_usage={},
            resume_from=None,  # NOT a resume
            output_dir=True, terminal=False,
        )
    finally:
        fa_mod.run_cross_final_acceptance = original

    assert outcome.outcome == "approved_terminal"
