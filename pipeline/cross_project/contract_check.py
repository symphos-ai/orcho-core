"""Cross-level contract-check flow for the cross-project pipeline.

The cross runner's contract-check gate runs after child project dispatch
and before the cross final-acceptance gate. This module owns that flow:

* per-alias review-target resolution (worktree checkout vs source);
* resume-cache reuse of a prior ``contract_check`` result (session /
  ``meta.json`` disk fallback);
* operator gate decision (``resolve_gate_decision``) with PAUSE / ABORT /
  SKIP semantics, plus policy-disabled / policy-never skips;
* the ``artifact_bundle`` single-call review and the per-alias review;
* parse-failure shaping (``ReviewSchemaError`` / ``ReviewParseError``);
* usage accumulation for the ``contract_check`` phase.

:func:`run_cross_contract_check` mutates the shared ``session`` /
``cross_ckpt`` / ``cross_phase_usage`` dicts in place (and writes the
checkpoint / session / terminal artifacts for the PAUSE / ABORT paths),
then returns a typed :class:`ContractCheckResult`. Early exits are NOT a
``return`` here — they surface as ``control`` (``"paused"`` / ``"aborted"``)
so the coordinator in ``app.py`` performs the matching early return with
the same ``session`` / ``run_dir`` / ``session_ts`` tuple.

This module is a leaf peer: it MUST NOT import from
:mod:`pipeline.cross_project.orchestrator`.
"""

import dataclasses
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.io.transcript import render_parse_failure, render_review_block
from pipeline.cross_project.checkpoint import (
    write_cross_checkpoint as _write_cross_checkpoint,
)
from pipeline.cross_project.gate_decisions import (
    GateDecision,
    resolve_gate_decision,
)
from pipeline.cross_project.gate_entries import skipped_contract_entry
from pipeline.cross_project.planning_loop import (
    approved_review_json as _approved_review_json,
)
from pipeline.cross_project.prompts import (
    contract_review_focus as _contract_review_focus_impl,
)
from pipeline.cross_project.rendering import paint
from pipeline.cross_project.terminal import (
    finalize_cross_terminal as _finalize_cross_terminal,
)
from pipeline.cross_project.usage import (
    _capture_invoke_usage,
    _print_usage_snapshot,
    accumulate_phase_usage as _accumulate_phase_usage,
)
from pipeline.engine import save_session as save_cross_session
from pipeline.runtime import CrossGateRunPolicy


@dataclasses.dataclass(frozen=True)
class ContractCheckContext:
    """Resolved inputs for the cross contract-check flow.

    ``session`` / ``cross_ckpt`` / ``cross_phase_usage`` are mutated in
    place; the frozen wrapper only pins the bindings, not the dicts.
    """

    task: str
    projects: Mapping[str, Path]
    session: dict
    cross_ckpt: dict
    run_dir: Path
    output_dir: Path | None
    dry_run: bool
    resume_from: str | None
    terminal: bool
    common_cwd: str
    plan_output: str
    codex: Any
    contract_policy: Any
    operator_decisions: Sequence[Any] | None
    no_interactive: bool
    cross_phase_usage: dict


@dataclasses.dataclass(frozen=True)
class ContractCheckResult:
    """Outcome of the cross contract-check flow.

    ``control`` carries the early-exit signal back to the coordinator:
    ``"continue"`` proceeds to the CFA gate; ``"paused"`` / ``"aborted"``
    mean the gate already wrote the terminal/pending state and the
    coordinator must early-return ``(session, run_dir, session_ts)``.
    """

    contract_results: dict
    contract_check_failed: bool
    contract_check_failure_reason: str | None
    control: str
    review_projects: dict
    review_common_cwd: str


def run_cross_contract_check(ctx: ContractCheckContext) -> ContractCheckResult:
    """Run the cross contract-check gate; see module docstring."""
    task = ctx.task
    projects = ctx.projects
    session = ctx.session
    cross_ckpt = ctx.cross_ckpt
    run_dir = ctx.run_dir
    output_dir = ctx.output_dir
    dry_run = ctx.dry_run
    resume_from = ctx.resume_from
    terminal = ctx.terminal
    common_cwd = ctx.common_cwd
    plan_output = ctx.plan_output
    codex = ctx.codex
    _contract_policy = ctx.contract_policy
    operator_decisions = ctx.operator_decisions
    no_interactive = ctx.no_interactive
    cross_phase_usage = ctx.cross_phase_usage

    from pipeline.cross_project.rendering import silent_renderers
    (
        banner, _success, _warn, _preview,
        _rcpp, print, C,  # noqa: A001 — print shadow
    ) = silent_renderers(terminal)

    _decision_overrides: dict[str, str] = {}
    for _override in (operator_decisions or ()):
        _decision_overrides[_override.target] = _override.decision
    _decision_feedback_by_target: dict[str, str] = {
        ov.target: ov.feedback
        for ov in (operator_decisions or ())
        if ov.feedback
    }

    contract_results: dict[str, dict] = {}
    contract_check_failed = False
    contract_check_failure_reason: str | None = None
    contract_check_skipped_by_operator = False

    # Variant 1 — point the cross gates (contract_check + CFA) at the
    # per-alias worktree checkouts, where the child sub-pipelines left
    # their uncommitted changes. The cross gates do a working-tree
    # ``review_uncommitted`` at their cwd; reviewing the SOURCE projects
    # would inspect pristine, stale code because the change surface is
    # the isolated run worktree. So the gate must look where the work
    # actually is. When worktree isolation degraded to in-place
    # (``worktree.path == source``), this naturally resolves back to the
    # source path — identical to the historical in-place behaviour where
    # the reviewer saw the uncommitted changes directly in the project.
    def _review_target(alias: str, source: Path) -> Path:
        child = session.get("phases", {}).get("projects", {}).get(alias)
        if isinstance(child, dict):
            wt = child.get("worktree")
            if isinstance(wt, dict):
                p = wt.get("path")
                if isinstance(p, str) and p:
                    return Path(p)
        return source

    review_projects: dict[str, Path] = {
        a: _review_target(a, Path(p)) for a, p in projects.items()
    }
    review_common_cwd = (
        os.path.commonpath([str(p) for p in review_projects.values()])
        if review_projects else common_cwd
    )

    # Resume-skip (CFA continue-resume bugfix). On a resumed run the
    # pre-CFA pipeline — including contract_check — already completed;
    # its results live in the persisted ``meta.json`` under
    # ``phases.contract_check`` (and are hydrated into ``session`` from
    # ``resumed_meta`` when the caller threads it — the CLI does).
    # Re-running the reviewer here would (a) waste an LLM call and
    # (b) print a fresh CONTRACT_CHECK block plus a contradictory new
    # verdict on a CFA-pause resume — the "ran the pipeline in a
    # circle" symptom the operator hit. Reuse the cached results,
    # reconstruct the local failure flags from them (so downstream
    # CFA/finalizer logic does not see "all clean" while the cached
    # entries say REJECTED), and emit only a grey one-liner — NOT a
    # fresh phase banner.
    if resume_from:
        _cc_cached = session.get("phases", {}).get("contract_check")
        if not (isinstance(_cc_cached, dict) and _cc_cached):
            # Disk fallback for direct/internal callers that did not
            # thread ``resumed_meta`` (mirrors the gate's
            # ``_load_prior_cfa_result`` disk fallback).
            try:
                import json as _json
                _disk_meta = _json.loads(
                    (run_dir / "meta.json").read_text(encoding="utf-8"),
                )
                _disk_cc = _disk_meta.get("phases", {}).get("contract_check")
                if isinstance(_disk_cc, dict) and _disk_cc:
                    session.setdefault("phases", {})["contract_check"] = _disk_cc
            except (OSError, ValueError):
                pass
    _contract_check_cached = (
        bool(resume_from)
        and isinstance(session.get("phases", {}).get("contract_check"), dict)
        and bool(session["phases"]["contract_check"])
    )
    if _contract_check_cached:
        contract_results = session["phases"]["contract_check"]
        for _entry in contract_results.values():
            if isinstance(_entry, dict) and _entry.get("approved") is False:
                contract_check_failed = True
                if contract_check_failure_reason is None:
                    contract_check_failure_reason = (
                        "contract_check (artifact_bundle) rejected"
                    )
        print(paint(
            "  CONTRACT_CHECK — skipped (resume, cached)", C.GREY,
        ))
    elif (
        not _contract_policy.enabled
        or _contract_policy.run is CrossGateRunPolicy.NEVER
    ):
        _reason = (
            "policy_disabled"
            if not _contract_policy.enabled
            else "policy_never"
        )
        for alias in projects:
            contract_results[alias] = skipped_contract_entry(
                alias=alias,
                reason=_reason,
                source="policy",
                on_skip=_contract_policy.on_skip,
            )
        session["phases"]["contract_check"] = contract_results
        banner(
            "CONTRACT_CHECK",
            f"CONTRACT CHECK — skipped by policy ({_reason})",
            C.YELLOW,
        )
    else:
        try:
            _gate_decision = resolve_gate_decision(
                gate_name="contract_check",
                policy=_contract_policy,
                cli_overrides=_decision_overrides,
                interactive_allowed=not no_interactive,
                stdin_is_tty=sys.stdin.isatty(),
                stdout_is_tty=sys.stdout.isatty(),
            )
        except KeyboardInterrupt:
            _gate_decision = GateDecision.ABORT

        if _gate_decision is GateDecision.PAUSE:
            _pending = {
                "name": "contract_check",
                "run_policy": _contract_policy.run.value,
                "choices": ["run", "skip"],
                "on_skip": _contract_policy.on_skip.value,
            }
            session["status"] = "awaiting_gate_decision"
            session["pending_gate"] = _pending
            cross_ckpt["pending_gate"] = _pending
            _write_cross_checkpoint(run_dir, cross_ckpt)
            save_cross_session(run_dir, session)
            banner(
                "CONTRACT_CHECK",
                "CONTRACT CHECK — paused for operator decision",
                C.YELLOW,
            )
            return ContractCheckResult(
                contract_results=contract_results,
                contract_check_failed=contract_check_failed,
                contract_check_failure_reason=contract_check_failure_reason,
                control="paused",
                review_projects=review_projects,
                review_common_cwd=review_common_cwd,
            )

        if _gate_decision is GateDecision.ABORT:
            session["failure_reason"] = (
                "contract_check aborted by operator"
            )
            _finalize_cross_terminal(
                run_dir=run_dir if output_dir else None,
                session=session,
                status="cancelled",
                halt_reason="cross_gate_aborted:contract_check",
                cross_ckpt=cross_ckpt,
            )
            banner(
                "CONTRACT_CHECK",
                "CONTRACT CHECK — aborted by operator",
                C.RED,
            )
            return ContractCheckResult(
                contract_results=contract_results,
                contract_check_failed=contract_check_failed,
                contract_check_failure_reason=contract_check_failure_reason,
                control="aborted",
                review_projects=review_projects,
                review_common_cwd=review_common_cwd,
            )

        if _gate_decision is GateDecision.SKIP:
            _feedback = _decision_feedback_by_target.get(
                "contract_check", "",
            )
            for alias in projects:
                contract_results[alias] = skipped_contract_entry(
                    alias=alias,
                    reason="operator_decision",
                    source="operator",
                    on_skip=_contract_policy.on_skip,
                    operator_feedback=_feedback,
                )
            session["phases"]["contract_check"] = contract_results
            contract_check_skipped_by_operator = True
            banner(
                "CONTRACT_CHECK",
                "CONTRACT CHECK — skipped by operator decision",
                C.YELLOW,
            )
        else:
            _run_contract_check = True  # noqa: F841

    _run_contract_check = (
        _contract_policy.enabled
        and _contract_policy.run is not CrossGateRunPolicy.NEVER
        and not contract_check_skipped_by_operator
        and not _contract_check_cached
    )
    if _run_contract_check:
        banner("CONTRACT_CHECK", "CONTRACT CHECK — Codex reviews cross-project consistency", C.YELLOW)

    from core.contracts.review_schema import ReviewSchemaError
    from pipeline.review_markdown import render_review_markdown
    from pipeline.review_parser import ReviewParseError, parse_review

    _use_artifact_bundle = (
        _run_contract_check
        and _contract_policy.mode == "artifact_bundle"
    )

    if _use_artifact_bundle:
        from core.io.git_helpers import worktree_diff_against_base
        from pipeline.cross_project.artifact_bundle import (
            build_bundle_markdown,
            build_bundle_review_prompt,
            mirror_review_to_aliases,
        )

        # ADR 0053 — capture each alias's worktree diff so the
        # cross-contract reviewer checks the actual changed code, not
        # just the plan + child verdicts. ``worktree_diff_against_base``
        # never raises (returns a sentinel on clean/unavailable); the
        # bundle builder skips those.
        _alias_diffs = {
            alias: worktree_diff_against_base(path)
            for alias, path in review_projects.items()
        }
        _bundle_markdown = build_bundle_markdown(
            task=task,
            projects=review_projects,
            session_phases=session["phases"],
            cross_plan_markdown=plan_output,
            alias_diffs=_alias_diffs,
        )
        _bundle_turn = build_bundle_review_prompt(
            _bundle_markdown,
            project_dir=str(review_common_cwd),
        )
        _bundle_prompt = _bundle_turn.text
        if dry_run:
            _bundle_raw = _approved_review_json(
                "contract_check dry run (artifact_bundle) skipped reviewer."
            )
        else:
            _cc_t0 = time.time()
            from core.io.stdout_render import defer_assistant_json
            from core.observability.prompt_trace import set_last_prompt_turn

            set_last_prompt_turn(_bundle_turn)
            with defer_assistant_json():
                _bundle_raw = codex.invoke(_bundle_prompt, str(review_common_cwd))
            _accumulate_phase_usage(
                cross_phase_usage, "contract_check",
                _capture_invoke_usage(
                    codex, time.time() - _cc_t0,
                    prompt=_bundle_prompt, output=_bundle_raw,
                    model=getattr(codex, "model", None),
                    terminal=terminal,
                ),
            )
        try:
            _bundle_parsed = parse_review(_bundle_raw)
            _bundle_rendered = render_review_markdown(
                _bundle_parsed, title="Contract Check — system",
            )
            _bundle_template = {
                "approved":      _bundle_parsed.approved,
                "verdict":       _bundle_parsed.verdict,
                "short_summary": _bundle_parsed.short_summary,
                "findings":      _bundle_parsed.findings_as_dicts(),
                "risks":         list(_bundle_parsed.risks),
                "checks":        list(_bundle_parsed.checks),
            }
            contract_results.update(
                mirror_review_to_aliases(
                    aliases=tuple(projects.keys()),
                    parsed_dict_template=_bundle_template,
                    raw_response=_bundle_raw,
                    rendered=_bundle_rendered,
                )
            )
            print(render_review_block(
                {
                    **_bundle_template,
                    "rendered": _bundle_rendered,
                    "raw_response": _bundle_raw,
                },
                title="Contract Check — system",
            ))
            if not _bundle_parsed.approved:
                contract_check_failed = True
                if contract_check_failure_reason is None:
                    contract_check_failure_reason = (
                        "contract_check (artifact_bundle) rejected"
                    )
        except (ReviewSchemaError, ReviewParseError) as e:
            _bundle_rendered = (
                f"contract_check parse error: {e}\n\n"
                f"Raw output:\n{_bundle_raw}"
            )
            _parse_entry_template = {
                "approved":      False,
                "verdict":       "REJECTED",
                "short_summary": (
                    "P1: contract_check (artifact_bundle) parse error."
                ),
                "findings":      [],
                "risks":         [],
                "checks":        [],
                "parse_error":   str(e),
            }
            contract_results.update(
                mirror_review_to_aliases(
                    aliases=tuple(projects.keys()),
                    parsed_dict_template=_parse_entry_template,
                    raw_response=_bundle_raw,
                    rendered=_bundle_rendered,
                )
            )
            print(render_parse_failure(
                title="Contract Check — system",
                error=str(e),
                raw_output=_bundle_raw,
            ))
            contract_check_failed = True
            if contract_check_failure_reason is None:
                contract_check_failure_reason = (
                    "contract_check (artifact_bundle) parse error"
                )
        session["phases"]["contract_check"] = contract_results

    for alias, _source_path in (
        projects.items()
        if _run_contract_check and not _use_artifact_bundle
        else ()
    ):
        # Review the alias's worktree checkout (the change surface),
        # falling back to source when isolation degraded to in-place.
        project_path = review_projects[alias]
        focus = _contract_review_focus_impl(task, projects)
        print(f"\n{paint(f'  Contract check for [{alias}]:', C.CYAN)}")
        if dry_run:
            _raw_dry = _approved_review_json(
                f"contract_check dry run skipped reviewer for [{alias}]."
            )
            _parsed_dry = parse_review(_raw_dry)
            _rendered_dry = render_review_markdown(
                _parsed_dry, title=f"Contract Check — {alias}",
            )
            contract_results[alias] = {
                "approved":      _parsed_dry.approved,
                "verdict":       _parsed_dry.verdict,
                "short_summary": _parsed_dry.short_summary,
                "findings":      _parsed_dry.findings_as_dicts(),
                "risks":         list(_parsed_dry.risks),
                "checks":        list(_parsed_dry.checks),
                "rendered":      _rendered_dry,
                "raw_response":  _raw_dry,
            }
            print(render_review_block(
                contract_results[alias],
                title=f"Contract Check — {alias}",
            ))
            continue

        from pipeline.prompts.builders import runtime_review_uncommitted_prompt
        _review_turn = runtime_review_uncommitted_prompt(focus, project_dir=str(project_path))
        _review_prompt = _review_turn.text
        _cc_t0 = time.time()
        from core.io.stdout_render import defer_assistant_json
        from core.observability.prompt_trace import set_last_prompt_turn

        set_last_prompt_turn(_review_turn)
        with defer_assistant_json():
            raw = codex.invoke(_review_prompt, str(project_path))
        _accumulate_phase_usage(
            cross_phase_usage, "contract_check",
            _capture_invoke_usage(
                codex, time.time() - _cc_t0,
                prompt=_review_prompt, output=raw,
                model=getattr(codex, "model", None),
                terminal=terminal,
            ),
        )
        try:
            parsed = parse_review(raw)
        except (ReviewSchemaError, ReviewParseError) as e:
            rendered = f"contract_check parse error: {e}\n\nRaw output:\n{raw}"
            contract_results[alias] = {
                "approved":      False,
                "verdict":       "REJECTED",
                "short_summary": f"P1: contract_check parse error for {alias}.",
                "findings":      [],
                "risks":         [],
                "checks":        [],
                "rendered":      rendered,
                "raw_response":  raw,
                "parse_error":   str(e),
            }
            print(render_parse_failure(
                title=f"Contract Check — {alias}",
                error=str(e),
                raw_output=raw,
            ))
            contract_check_failed = True
            if contract_check_failure_reason is None:
                contract_check_failure_reason = (
                    f"contract_check parse error for {alias}"
                )
            continue

        rendered = render_review_markdown(parsed, title=f"Contract Check — {alias}")
        contract_results[alias] = {
            "approved":      parsed.approved,
            "verdict":       parsed.verdict,
            "short_summary": parsed.short_summary,
            "findings":      parsed.findings_as_dicts(),
            "risks":         list(parsed.risks),
            "checks":        list(parsed.checks),
            "rendered":      rendered,
            "raw_response":  raw,
        }
        print(render_review_block(
            contract_results[alias],
            title=f"Contract Check — {alias}",
        ))
        if not parsed.approved:
            contract_check_failed = True
            if contract_check_failure_reason is None:
                contract_check_failure_reason = (
                    f"contract_check rejected for {alias}"
                )

    if _run_contract_check and not _use_artifact_bundle:
        session["phases"]["contract_check"] = contract_results
    if "contract_check" in cross_phase_usage:
        _print_usage_snapshot(
            "contract_check", cross_phase_usage["contract_check"],
            terminal=terminal,
        )

    return ContractCheckResult(
        contract_results=contract_results,
        contract_check_failed=contract_check_failed,
        contract_check_failure_reason=contract_check_failure_reason,
        control="continue",
        review_projects=review_projects,
        review_common_cwd=review_common_cwd,
    )


__all__ = [
    "ContractCheckContext",
    "ContractCheckResult",
    "run_cross_contract_check",
]
