"""Cross-level commit delivery (ADR cross-delivery + CFA pause, Phase B).

The mono pipeline early-returns from
:meth:`pipeline.project.run._run_commit_delivery` for cross children
(``parent_run_id`` / ``project_alias`` set) so the per-alias diffs stay
inside their retained run worktrees. This module is the cross-level
replacement: after the cross release gate (CFA) approves — or the
operator overrides a REJECTED verdict and chooses ``continue`` — it
loops the aliases and transports each child's worktree diff into the
operator's project checkout using the proven mono primitives
(:func:`pipeline.engine.commit_delivery.resolve_commit_delivery` +
:func:`~pipeline.engine.commit_delivery.apply_commit_delivery`). Cross
only orchestrates; it does not reimplement transport, the
``target_dirty`` pause/retry loop, or the audit-artifact writer.

Per-alias inputs (see ADR §"Phase B — Cross-level delivery"):

* target project checkout — ``projects[alias]`` (top-level
  ``session["projects"]`` is a plain ``{alias: path_str}`` map);
* source worktree checkout — child session
  ``session["phases"]["projects"][alias]["worktree"]["path"]``;
* alias run_dir — the existing ``<cross_run_dir>/<alias>/`` child
  artifact dir (audit json lands beside the child's ``diff.patch``);
* baseline_ref — child ``pre_run_dirty.seed_tree_sha`` → child
  ``worktree.base_ref`` → ``HEAD`` (mono-compatible fallback).

Multi-alias policy (user-confirmed): per-alias failures are recorded
and the loop continues — one dirty / failed project never blocks
delivery to the clean ones. There is no global rollback. A single
``halted`` (operator stop) ends the loop immediately. Outcomes
aggregate to one overall verdict the finalizer maps to a terminal
status — no new top-level status is introduced.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.observability import events as _events
from core.observability.logging import warn
from pipeline.engine.commit_delivery import (
    apply_commit_delivery,
    resolve_commit_delivery,
)
from pipeline.run_state.release_verdict import is_approved

# Type of the ``decision -> str | None`` closure handed to
# ``resolve_commit_delivery`` to author the outward commit message.
CommitMessageGenerator = Any

# Per-alias delivery status buckets. Mirrors
# :data:`pipeline.engine.commit_delivery.CommitDeliveryStatus` plus the
# cross-only ``skipped_already_delivered`` idempotent-resume marker.
_SUCCESS_LIKE = frozenset(
    {"committed", "applied_uncommitted", "no_diff", "skipped",
     "skipped_already_delivered"},
)
_FAILURE_LIKE = frozenset(
    {"target_dirty", "commit_failed", "apply_failed", "not_applicable",
     "disabled"},
)

CrossDeliveryOverall = Literal["ok", "partial", "failed", "halted", "disabled"]


@dataclass(frozen=True, slots=True)
class AliasDeliveryRecord:
    """One alias's delivery outcome.

    ``status`` is the mono :class:`CommitDeliveryStatus` (or the
    cross-only ``skipped_already_delivered``). ``release_override``
    is populated only when the operator overrode a non-APPROVED child
    verdict to ship the bundle — it preserves the original reviewer
    verdict so evidence never pretends the child approved.
    """

    alias: str
    status: str
    commit_sha: str | None = None
    error: str | None = None
    release_override: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None

    def is_success(self) -> bool:
        return self.status in _SUCCESS_LIKE

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"alias": self.alias, "status": self.status}
        if self.commit_sha:
            out["commit_sha"] = self.commit_sha
        if self.error:
            out["error"] = self.error
        if self.release_override is not None:
            out["release_override"] = self.release_override
        if self.decision is not None:
            out["decision"] = self.decision
        return out


@dataclass(frozen=True, slots=True)
class CrossDeliveryResult:
    """Aggregate outcome the finalizer maps to a terminal status.

    ``overall``:

    * ``ok`` — every alias success-like → finalize ``done``;
    * ``partial`` — mix of success/failure → ``failed`` +
      ``cross_delivery_partial``;
    * ``failed`` — every alias failure-like → ``failed`` +
      ``cross_delivery_failed``;
    * ``halted`` — operator stopped the loop → ``halted``;
    * ``disabled`` — delivery turned off by config
      (``app_cfg.commit.enabled is False``) → ``done`` (success-like).

    ``disabled_by_config`` lets the classifier check a contract, not a
    guess: a ``disabled`` overall is success only when the operator
    explicitly turned delivery off — never when the path simply did
    not run while enabled (that is the P0 this module fixes).
    """

    overall: CrossDeliveryOverall
    per_alias: tuple[AliasDeliveryRecord, ...] = ()
    disabled_by_config: bool = False

    def to_evidence(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "disabled_by_config": self.disabled_by_config,
            "per_alias": {r.alias: r.to_dict() for r in self.per_alias},
        }


def _child_session(session: dict[str, Any], alias: str) -> dict[str, Any] | None:
    children = session.get("phases", {}).get("projects", {})
    if isinstance(children, dict):
        child = children.get(alias)
        if isinstance(child, dict):
            return child
    return None


def _worktree_path(child: dict[str, Any]) -> Path | None:
    worktree = child.get("worktree")
    if isinstance(worktree, dict):
        wp = worktree.get("path")
        if isinstance(wp, str) and wp:
            return Path(wp)
    return None


def _baseline_ref(child: dict[str, Any]) -> str:
    """Mono-compatible baseline resolution.

    seed_tree_sha (top-level on the child session) → worktree base_ref
    → ``HEAD``. Matches
    :meth:`pipeline.project.run.PipelineState._commit_delivery_baseline`
    so the cross path never drifts from the mono baseline rule.
    """
    pre_run = child.get("pre_run_dirty")
    if isinstance(pre_run, dict):
        seed = pre_run.get("seed_tree_sha")
        if isinstance(seed, str) and seed.strip():
            return seed.strip()
    worktree = child.get("worktree")
    if isinstance(worktree, dict):
        base = worktree.get("base_ref")
        if isinstance(base, str) and base.strip():
            return base.strip()
    return "HEAD"


def _child_verdict(child: dict[str, Any]) -> str:
    fa = child.get("phases", {}).get("final_acceptance")
    if isinstance(fa, dict):
        return str(fa.get("verdict") or "")
    return ""


def _override_session(
    child: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build an alias-scoped session whose ``final_acceptance`` reads
    APPROVED so :func:`resolve_commit_delivery` proceeds, and the
    override marker preserving the original child verdict.

    Mono's ``resolve_commit_delivery`` blocks delivery when the child
    ``phases.final_acceptance.verdict != APPROVED`` (its correctness
    gate). On a CFA override-continue the operator has explicitly
    chosen to ship the bundle, so the per-alias gate must also pass.
    Per ADR invariant 6 option (a): pass a *synthetic* alias session
    rather than adding an override param to the mono API — the
    original verdict survives in the returned marker.
    """
    original_verdict = _child_verdict(child) or "UNKNOWN"
    synthetic = dict(child)
    phases = dict(child.get("phases", {})) if isinstance(
        child.get("phases"), dict,
    ) else {}
    fa = dict(phases.get("final_acceptance", {})) if isinstance(
        phases.get("final_acceptance"), dict,
    ) else {}
    fa["verdict"] = "APPROVED"
    phases["final_acceptance"] = fa
    synthetic["phases"] = phases
    marker = {
        "original_verdict": original_verdict,
        "effective_verdict": "APPROVED_FOR_DELIVERY",
        "source": "operator_override",
    }
    return synthetic, marker


def _build_commit_message_generator(
    release_agent: Any,
    content_language: str | None,
) -> CommitMessageGenerator | None:
    """Mirror mono's ``commit_message_generator`` (:mod:`pipeline.project.run`)
    for cross delivery so the outward commit message is authored in
    ``content_language`` (English default), not the operator's
    ``task_language``.

    Without this, cross ``resolve_commit_delivery`` receives no generator and
    falls back to ``_message_from_release_summary`` — the CFA release summary
    the release agent wrote in the operator's conversation language, which then
    leaks into the pushed commit / PR title (the cross counterpart of the mono
    #48 language split).

    Null-safe: no release agent → ``None`` generator → ``resolve_commit_delivery``
    keeps its existing release-summary fallback unchanged. Any generation
    failure also degrades to that fallback, exactly like mono.
    """
    if release_agent is None:
        return None

    from pipeline.commit_message_parser import (
        CommitMessageParseError,
        CommitMessageSchemaError,
        parse_commit_message,
    )
    from pipeline.engine.commit_delivery import render_commit_message_prompt

    def commit_message_generator(decision: Any) -> str | None:
        prompt = render_commit_message_prompt(
            decision,
            body_language=content_language,
        )
        try:
            raw = release_agent.invoke(
                prompt,
                str(decision.source_path),
                mutates_artifacts=False,
                continue_session=False,
            )
            return parse_commit_message(raw).render()
        except (CommitMessageParseError, CommitMessageSchemaError):
            return None
        except Exception as exc:  # noqa: BLE001
            warn(f"cross commit message generation failed; using summary: {exc}")
            return None

    return commit_message_generator


def run_cross_delivery(
    *,
    session: dict[str, Any],
    projects: dict[str, Path],
    app_cfg: Any,
    cross_run_dir: Path,
    terminal: bool,
    override: bool = False,
    cross_ckpt: dict[str, Any] | None = None,
    release_agent: Any = None,
) -> CrossDeliveryResult:
    """Deliver each alias's worktree diff into its project checkout.

    Runs after the cross release gate approves (or the operator
    overrides). Loops aliases, reusing the mono delivery primitives
    per alias. Continues past per-alias failures, stops on operator
    ``halt``, and aggregates to one :class:`CrossDeliveryResult`.

    Args:
        session: cross-level session. Child sessions are read from
            ``session["phases"]["projects"][alias]``; per-alias
            evidence is written to ``session["phases"]["cross_delivery"]``.
        projects: ``{alias: project_checkout_path}`` — the delivery
            targets (operator's main checkouts).
        app_cfg: loaded ``AppConfig``; ``app_cfg.commit`` is the mono
            commit-delivery config mapping.
        cross_run_dir: the cross run dir. Per-alias audit json lands
            in ``<cross_run_dir>/<alias>/commit_decisions/``.
        terminal: ``True`` when the cross parent owns a TTY — forwarded
            so ``apply_commit_delivery`` can run its interactive
            ``target_dirty`` retry prompt. Headless cross records
            ``target_dirty`` and continues.
        override: ``True`` on a CFA override-continue — lifts each
            non-APPROVED child verdict to APPROVED for delivery while
            preserving the original verdict in ``release_override``.
        cross_ckpt: cross checkpoint dict. When present, a
            ``delivery_status`` map enables idempotent resume: aliases
            already delivered on a prior attempt are skipped.
        release_agent: the cross release (CFA) agent, used to author the
            outward commit message in ``app_cfg.content_language`` — parity
            with the mono ``commit_message_generator``. ``None`` → the
            existing release-summary fallback (operator-language) is used.

    Returns:
        :class:`CrossDeliveryResult` — the aggregate the finalizer
        consumes to decide the terminal status.
    """
    commit_cfg = dict(getattr(app_cfg, "commit", {}) or {})
    # ADR 0119 — cross delivery flows through the same single commit site, so it
    # obeys the same default-branch invariant: a missing ``commit.branch_policy``
    # resolves (via ``normalize_branch_policy`` at the commit site) to the
    # ``worktree_branch`` default, never to ``bypass``. ``bypass`` — committing
    # the alias diff straight onto each project checkout's current HEAD,
    # including its default branch — is reachable only when the operator sets it
    # explicitly in config; cross must not inject it as a hidden default and
    # silently weaken the invariant. The config passes through unchanged.
    if commit_cfg.get("enabled") is False:
        # Operator turned delivery off on purpose — success-like.
        result = CrossDeliveryResult(overall="disabled", disabled_by_config=True)
        session.setdefault("phases", {})["cross_delivery"] = result.to_evidence()
        return result

    no_interactive = not terminal
    commit_message_generator = _build_commit_message_generator(
        release_agent,
        getattr(app_cfg, "content_language", None),
    )
    delivery_status: dict[str, Any] = {}
    if isinstance(cross_ckpt, dict):
        existing = cross_ckpt.get("delivery_status")
        if isinstance(existing, dict):
            delivery_status = existing

    _events.emit(
        "cross.delivery.started",
        project_count=len(projects),
    )

    records: list[AliasDeliveryRecord] = []
    halted = False
    for alias, project_path in projects.items():
        prior = delivery_status.get(alias)
        if isinstance(prior, dict) and prior.get("status") in _SUCCESS_LIKE:
            # Idempotent resume — already delivered on a prior attempt.
            records.append(
                AliasDeliveryRecord(
                    alias=alias,
                    status="skipped_already_delivered",
                    commit_sha=prior.get("commit_sha"),
                ),
            )
            continue

        record = _deliver_one_alias(
            alias=alias,
            project_path=Path(project_path),
            session=session,
            commit_cfg=commit_cfg,
            cross_run_dir=cross_run_dir,
            no_interactive=no_interactive,
            override=override,
            commit_message_generator=commit_message_generator,
        )
        records.append(record)

        if isinstance(cross_ckpt, dict):
            delivery_status[alias] = {
                "status": record.status,
                "commit_sha": record.commit_sha,
            }
            cross_ckpt["delivery_status"] = delivery_status
            from pipeline.cross_project.checkpoint import write_cross_checkpoint

            write_cross_checkpoint(cross_run_dir, cross_ckpt)

        if record.is_success():
            _events.emit(
                "cross.delivery.alias_committed",
                alias=alias,
                status=record.status,
                commit_sha=record.commit_sha,
            )
        elif record.status == "halted":
            _events.emit(
                "cross.delivery.alias_failed",
                alias=alias,
                status="halted",
                error=record.error or "operator halted delivery",
            )
            halted = True
            break
        else:
            _events.emit(
                "cross.delivery.alias_failed",
                alias=alias,
                status=record.status,
                error=record.error or "",
            )

    overall = _aggregate(records, halted=halted)
    _events.emit("cross.delivery.completed", overall=overall)

    result = CrossDeliveryResult(
        overall=overall,
        per_alias=tuple(records),
        disabled_by_config=False,
    )
    session.setdefault("phases", {})["cross_delivery"] = result.to_evidence()
    return result


def _deliver_one_alias(
    *,
    alias: str,
    project_path: Path,
    session: dict[str, Any],
    commit_cfg: dict[str, Any],
    cross_run_dir: Path,
    no_interactive: bool,
    override: bool,
    commit_message_generator: CommitMessageGenerator | None = None,
) -> AliasDeliveryRecord:
    child = _child_session(session, alias)
    worktree = _worktree_path(child) if child is not None else None
    if worktree is None:
        # No isolated worktree to transport from — either the alias has
        # no stored child session (e.g. a plan-only cross run whose
        # children were never persisted under ``phases.projects``) or
        # the child ran a profile without worktree isolation. There is
        # nothing for the cross level to deliver — a legitimate no-op,
        # NOT the "APPROVED ships nothing while changes exist" P0 (in
        # that case the child HAS a worktree carrying the fix). Classify
        # success-like so it never demotes an otherwise-clean run.
        return AliasDeliveryRecord(alias=alias, status="no_diff")

    release_override: dict[str, Any] | None = None
    delivery_session: dict[str, Any] = child
    # Read the "not approved" decision through the single release-verdict
    # source (ADR 0115 slice 4) instead of an open-coded non-approved literal
    # — parity with the mono delivery guards. ``_child_verdict`` still
    # extracts the raw verdict string; only the not-approved decision routes
    # through ``is_approved``. The ADR 0090 forced-REJECTED and ADR 0108
    # provenance backstops are unchanged: this only routes the existing
    # override gate, never relaxes it.
    if not is_approved(_child_verdict(child)) and override:
        delivery_session, release_override = _override_session(child)

    alias_run_dir = cross_run_dir / alias
    alias_run_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{cross_run_dir.name}__{alias}"

    decision = resolve_commit_delivery(
        project_dir=project_path,
        source_worktree=worktree,
        run_dir=alias_run_dir,
        run_id=run_id,
        session=delivery_session,
        commit_config=commit_cfg,
        no_interactive=no_interactive,
        baseline_ref=_baseline_ref(child),
        commit_message_generator=commit_message_generator,
    )
    if decision.status == "pending":
        decision = apply_commit_delivery(
            decision,
            run_dir=alias_run_dir,
            commit_config=commit_cfg,
            no_interactive=no_interactive,
        )

    return AliasDeliveryRecord(
        alias=alias,
        status=decision.status,
        commit_sha=decision.commit_sha,
        error=decision.error,
        release_override=release_override,
        decision=decision.to_dict(),
    )


def _aggregate(
    records: list[AliasDeliveryRecord], *, halted: bool,
) -> CrossDeliveryOverall:
    if halted:
        return "halted"
    if not records:
        # Nothing to deliver while enabled is itself the P0 failure mode.
        return "failed"
    successes = sum(1 for r in records if r.is_success())
    failures = len(records) - successes
    if failures == 0:
        return "ok"
    if successes == 0:
        return "failed"
    return "partial"


__all__ = [
    "AliasDeliveryRecord",
    "CrossDeliveryResult",
    "run_cross_delivery",
]
