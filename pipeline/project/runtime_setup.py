"""Runtime resolution for the project pipeline.

The typed project entry surface (``pipeline/project/app.py``) must turn
the requested provider + per-phase model/runtime config into the concrete
objects the run executes against: the active :class:`AgentProvider`, the
resolved per-phase model names, the synthesized
:class:`agents.registry.PhaseAgentConfig`, and the subtask
:class:`agents.registry.AgentRegistry`. This module owns that work and
returns a typed :class:`RuntimeSetup` so the coordinator wires the run off
one structured object instead of a train of locals.

Construction stays side-effect free with respect to external agent
binaries: ``_synthesize_phase_config`` / ``_agent_registry_from_provider``
build provider-resolved slots, but resolving an external CLI binary is
deferred to first real invocation by the runtime adapters themselves.

It also owns the small pure helpers external consumers reach for —
``_resolve_session_mode`` (AUTO → concrete mode) and
``_validate_plan_file_paths`` (plan path existence split) — and
``apply_session_seeds``, which merges parent-meta + checkpoint
``agent.session_id`` seeds onto the phase config. The coordinator calls
``apply_session_seeds`` after the checkpoint store is built.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents.protocols import SessionMode
from agents.registry import PhaseAgentConfig
from core.infra import config
from pipeline.project.profile_dispatch import (
    _FOLLOWUP_ROLE_TO_AGENT_ATTR,
    apply_followup_session_seeds as _apply_followup_session_seeds,
    resolve_phase_models as _resolve_phase_models,
)

if TYPE_CHECKING:
    from agents.runtimes._strategy import AgentProvider


@dataclasses.dataclass(frozen=True)
class RuntimeSetup:
    """Resolved runtime state for one run.

    ``phase_config`` is the synthesized config (the caller's value when one
    was supplied, otherwise built per-slot from AppConfig + fallbacks).
    ``provider`` is the active provider (caller-supplied or a fresh
    ``RealAgentProvider``). ``agent_registry`` bridges that provider into
    the subtask runtime registry.
    """

    provider: AgentProvider
    plan_model: str
    implement_model: str
    repair_model: str
    repair_escalation_model: str
    review_model: str
    chain_same_model_only: bool
    phase_config: PhaseAgentConfig
    agent_registry: Any


def setup_runtime(
    *,
    phase_config: PhaseAgentConfig | None,
    provider: AgentProvider | None,
    model: str,
    runtime_override: dict[str, str] | None = None,
) -> RuntimeSetup:
    """Resolve provider, per-phase models, phase config, and agent registry.

    Side-effect free apart from a ``vtrace`` line. The returned
    ``phase_config`` is synthesized via ``provider.resolve(...)`` for each
    slot when the caller did not supply one; a supplied config is returned
    unchanged (idempotent), so hoisting the call is safe.

    ``runtime_override`` (ADR 0101 / T2) is the persisted operator
    runtime/model override read from the run's ``meta.json`` on resume. When
    present (``{phase, runtime, model}``) it substitutes the runtime+model for
    exactly the named phase's synthesized slot — reusing the same per-phase
    ``provider.resolve`` construction path, not a parallel one — and never
    leaks to other phases. It is honored only on the synthesize path (no
    caller-supplied ``phase_config``); a plain resume with no persisted record
    passes ``None`` and behaviour is unchanged.
    """
    plan_model, implement_model, repair_model, repair_escalation_model, review_model = (
        _resolve_phase_models(phase_config, fallback_code_model=model)
    )

    from agents.runtimes import RealAgentProvider
    from core.observability.trace import vtrace
    _provider: AgentProvider = provider if provider is not None else RealAgentProvider()
    vtrace("provider", type(_provider).__name__,
           extra=f"plan={plan_model} build={implement_model} review={review_model}")

    chain_same_model_only = _read_chain_same_model_only()

    phase_config = _synthesize_phase_config(
        phase_config, _provider=_provider,
        plan_model=plan_model, implement_model=implement_model,
        repair_model=repair_model, repair_escalation_model=repair_escalation_model,
        review_model=review_model,
        runtime_override=runtime_override,
    )
    agent_registry = _agent_registry_from_provider(_provider, phase_config)

    return RuntimeSetup(
        provider=_provider,
        plan_model=plan_model,
        implement_model=implement_model,
        repair_model=repair_model,
        repair_escalation_model=repair_escalation_model,
        review_model=review_model,
        chain_same_model_only=chain_same_model_only,
        phase_config=phase_config,
        agent_registry=agent_registry,
    )


def project_effective_work_mode(
    *,
    profile: Any,
    cli_mode: str | None = None,
    contract_work_mode: str = "",
) -> str:
    """Project the effective verification ``work_mode`` for a run.

    Pure deterministic resolution of the run's strictness posture
    (``work_mode``) from three inputs, in precedence order:

      1. an explicit per-run CLI override (``orcho run --mode``);
      2. an explicit project/contract-declared ``work_mode``;
      3. otherwise the resolved profile's ``default_mode`` (the T2/T4
         semantic default — e.g. ``feature`` → ``fast``,
         ``complex_feature`` → ``pro``).

    Returns ``""`` only when no explicit override is set and the profile
    carries no ``default_mode`` (plugins / custom). The result is always a
    member of :data:`pipeline.verification_contract.WORK_MODES`. No I/O —
    the CLI override is read by the caller and passed in as ``cli_mode``.
    """
    from pipeline.verification_contract import WORK_MODES

    explicit = (cli_mode or "").strip() or (contract_work_mode or "").strip()
    if explicit:
        effective = explicit
    else:
        default_mode = getattr(profile, "default_mode", None)
        effective = (
            "" if default_mode is None
            else str(getattr(default_mode, "value", default_mode))
        )
    if effective not in WORK_MODES:
        raise ValueError(
            f"projected work_mode {effective!r} is not one of {WORK_MODES!r}"
        )
    return effective


def apply_default_mode_projection(
    contract: Any,
    *,
    profile: Any,
    cli_mode: str | None = None,
) -> Any:
    """Return ``contract`` with ``work_mode`` set to the effective mode.

    Focused projection point: applied at run assembly once the resolved
    profile and the (read-only) verification contract are both known. The
    rule mirrors :func:`project_effective_work_mode` — an explicit work_mode
    (CLI ``--mode`` or a project/contract-declared value) is preserved, and
    only an *unset* work_mode is filled from ``profile.default_mode``.

    ``None`` contract (no verification declared) is returned unchanged: with
    no contract there is no gate plan for the mode to shape. When the
    effective mode already equals the contract's current ``work_mode`` the
    same object is returned (no needless ``replace``). The contract is a
    frozen dataclass, so a change produces a new instance via
    :func:`dataclasses.replace`; the new ``work_mode`` then flows into the
    assembled ``SelectionContext`` and run meta unchanged.
    """
    if contract is None:
        return None
    current = getattr(contract, "work_mode", "") or ""
    effective = project_effective_work_mode(
        profile=profile,
        cli_mode=cli_mode,
        contract_work_mode=current,
    )
    if effective == current:
        return contract
    return dataclasses.replace(contract, work_mode=effective)


def apply_session_seeds(
    phase_config: PhaseAgentConfig,
    followup_session_seeds: dict[str, str] | None,
    ckpt: Any,
) -> int:
    """Seed ``agent.session_id`` from parent-meta + checkpoint sources.

    E1: unified seed source for ``agent.session_id`` rehydrate. Two
    channels feed it:

      * cross-run followup → ``followup_session_seeds`` (parent meta).
      * intra-run resume   → ``ckpt.get_agent_sessions()`` (own DB).

    Checkpoint takes precedence — by the time a resume reaches here, the
    on-disk record reflects what *this* run last did, which is strictly
    fresher than the parent-meta snapshot taken at child spawn time.
    ``apply_followup_session_seeds`` still owns the ``agent.session_id =
    sid`` + ``_followup_resume_pending = True`` wiring; we only widen its
    input set.

    Wire shape adapter: checkpoint stores by ``role_attr`` (the
    ``PhaseAgentConfig`` slot name the agent is bound to —
    identity-resolved at invoke time so CHAIN-mode dispatch saves under
    the *real* slot). ``apply_followup_session_seeds`` reads by ``role``
    (``plan`` / ``validate_plan`` / ...). Convert here via the inverse of
    ``_FOLLOWUP_ROLE_TO_AGENT_ATTR`` so the persisted rows reach the seeder.
    """
    _persisted_agent_sessions: dict[str, str] = {}
    if ckpt is not None:
        try:
            _persisted_agent_sessions = ckpt.get_agent_sessions()
        except Exception:  # noqa: BLE001
            _persisted_agent_sessions = {}
    _attr_to_role = {
        attr: role
        for role, attr in _FOLLOWUP_ROLE_TO_AGENT_ATTR.items()
    }
    _persisted_session_seeds_by_role: dict[str, str] = {
        _attr_to_role[attr]: sid
        for attr, sid in _persisted_agent_sessions.items()
        if attr in _attr_to_role
    }
    _merged_session_seeds: dict[str, str] = {
        **(followup_session_seeds or {}),
        **_persisted_session_seeds_by_role,
    }
    return _apply_followup_session_seeds(
        phase_config,
        _merged_session_seeds,
    )


def _resolve_session_mode(
    requested: SessionMode,
    *,
    repair_round: int,
    implement_model: str,
    repair_model: str,
    chain_same_model_only: bool,
) -> SessionMode:
    """AUTO → concrete SessionMode given the per-phase models.

    Rules (mirrors the matrix in plan §9):

      * review_changes phase always runs STATELESS — handled by callers, not here.
      * Same runtime + same model on both sides of the implement → repair_changes edge: CHAIN.
      * Same runtime, different models: HYBRID.
      * Round 2+ repair_changes with escalation defaults to HYBRID (different model).
      * Anything explicit (STATELESS / CHAIN / HYBRID) is passed through.
    """
    if requested != SessionMode.AUTO:
        return requested
    if repair_round <= 0:
        return SessionMode.STATELESS
    same_model = implement_model == repair_model
    if same_model or not chain_same_model_only:
        return SessionMode.CHAIN
    return SessionMode.HYBRID


def _validate_plan_file_paths(
    plan: Any,
    cwd: str,
) -> tuple[list[str], list[str]]:
    """Split ``plan.file_paths`` into (existing, missing) relative to ``cwd``.

    Files that don't yet exist aren't necessarily an error — a plan may
    create new files on purpose. Callers use this to log a warning and feed
    a "watch out, these paths don't exist" hint into the implement prompt.
    """
    if not plan.file_paths:
        return [], []
    existing: list[str] = []
    missing: list[str] = []
    cwd_path = Path(cwd)
    for rel in plan.file_paths:
        if (cwd_path / rel).exists():
            existing.append(rel)
        else:
            missing.append(rel)
    return existing, missing


def _read_chain_same_model_only() -> bool:
    """Read ``session.chain_same_model_only`` once. Defaults True so we
    never silently chain across model boundaries.
    """
    try:
        return bool(config.AppConfig.load().session.get("chain_same_model_only", True))
    except Exception:
        return True


def _synthesize_phase_config(
    phase_config: PhaseAgentConfig | None,
    *,
    _provider: object,
    plan_model: str,
    implement_model: str,
    repair_model: str,
    repair_escalation_model: str,
    review_model: str,
    runtime_override: dict[str, str] | None = None,
) -> PhaseAgentConfig:
    """Return ``phase_config`` as-is when supplied; otherwise build one
    via ``_provider.resolve(runtime, model, effort=...)`` for every slot.

    Runtime/model/effort for each phase come from AppConfig
    (``phase_runtime_map`` / ``phase_model_map`` / ``phase_effort_map``).
    No phase is hardwired to Claude or Codex here — a runtime id
    registered under ``orcho.agent_runtimes`` and pinned via
    ``phase_runtime_map`` routes through the same construction path.

    ADR 0101 / T2: when ``runtime_override`` (``{phase, runtime, model}``) is
    present, the named phase's slot is built from the override pair instead of
    the configured one — through the same ``_provider.resolve`` path, isolated
    to that one phase. Any other phase is untouched.
    """
    if phase_config is not None:
        return phase_config
    app = config.AppConfig.load()
    phase_models   = app.phase_model_map
    phase_runtimes = app.phase_runtime_map
    phase_efforts  = app.phase_effort_map

    fallback_models = {
        "plan":              plan_model,
        "validate_plan":     review_model,
        "implement":         implement_model,
        "review_changes":    review_model,
        "repair_changes":    repair_model,
        "repair_escalation": repair_escalation_model,
        "final_acceptance":  review_model,
    }

    override_phase = (runtime_override or {}).get("phase")

    def _slot(phase: str):
        # ``or`` (not ``.get(..., default)``) so an empty-string value
        # from local config still falls back to the per-phase default
        # instead of slipping through as "".
        runtime = phase_runtimes.get(phase) or "claude"
        model = phase_models.get(phase) or fallback_models[phase]
        # Per-phase operator override (resume only): substitute runtime+model
        # for exactly this phase, leaving every other slot at its configured
        # value. Empty halves fall back to the configured value.
        if runtime_override is not None and override_phase == phase:
            runtime = runtime_override.get("runtime") or runtime
            model = runtime_override.get("model") or model
        return _provider.resolve(
            runtime,
            model,
            effort=phase_efforts.get(phase),
        )

    return PhaseAgentConfig(
        plan_agent              = _slot("plan"),
        validate_plan_agent     = _slot("validate_plan"),
        implement_agent         = _slot("implement"),
        review_changes_agent    = _slot("review_changes"),
        repair_changes_agent    = _slot("repair_changes"),
        repair_escalation_agent = _slot("repair_escalation"),
        final_acceptance_agent  = _slot("final_acceptance"),
    )


def _agent_registry_from_provider(
    provider: object,
    phase_config: PhaseAgentConfig,
):
    """Build the subtask runtime registry from the active provider.

    ``subtask_dag`` resolves each ``SubTask`` through ``AgentRegistry``.
    Project runs already own the provider and phase runtime map, so bridge that
    provider into the state registry instead of requiring a second runtime
    discovery path.
    """
    from agents.registry import AgentRegistry

    app = config.AppConfig.load()
    runtime_names = {
        str(runtime).strip()
        for runtime in app.phase_runtime_map.values()
        if str(runtime).strip()
    }
    runtime_names.update({"claude", "codex", "gemini"})
    for attr in (
        "plan_agent",
        "validate_plan_agent",
        "implement_agent",
        "review_changes_agent",
        "repair_changes_agent",
        "repair_escalation_agent",
        "final_acceptance_agent",
    ):
        runtime = getattr(getattr(phase_config, attr, None), "runtime", None)
        if runtime:
            runtime_names.add(str(runtime).strip())

    registry = AgentRegistry()
    for runtime in sorted(runtime_names):
        registry.register(
            runtime,
            lambda model, effort=None, _runtime=runtime: provider.resolve(
                _runtime, model, effort=effort,
            ),
        )
    return registry


__all__ = [
    "RuntimeSetup",
    "setup_runtime",
    "project_effective_work_mode",
    "apply_default_mode_projection",
    "apply_session_seeds",
    "_resolve_session_mode",
    "_validate_plan_file_paths",
    "_read_chain_same_model_only",
    "_synthesize_phase_config",
    "_agent_registry_from_provider",
]
