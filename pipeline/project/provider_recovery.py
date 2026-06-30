# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral recovery projection for terminal provider-access failures.

When a phase dies because the configured runtime cannot reach its provider
surface (:class:`core.io.retry.AgentAccessError`), the run is terminal: blind
retry will not help until the operator either restores provider access or
switches the phase to a different *configured* runtime/model pair. This module
builds the durable, provider-neutral recovery projection that
``pipeline/project/run.py`` persists into ``session['failure']`` and the
``run.end`` event.

Design contract (see ADR 0101):

* The projection is **provider-neutral** — no provider name is hard-coded.
* ``recovery_actions`` always carries the ``retry`` and ``halt`` options.
* ``replace`` options are appended one per candidate, and only when candidates
  exist. This durable evidence list is intentionally distinct from the SDK
  ``next_actions`` projection (a later subtask), which drops ``halt`` and
  attaches a ``run_id`` to each executable action.
* Replacement **candidates** are derived ONLY from configured
  ``(runtime, model)`` pairs (``AppConfig.phase_runtime_map`` +
  ``phase_model_map``), with the failed pair excluded and duplicates removed.
  An ``AgentRegistry``-style runtime set may be supplied solely to validate
  that a candidate runtime exists; it is never the *source* of pairs.
* Operator-visible text is taken from the sanitized provider-access channel,
  never from raw stderr / provider JSON.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

PROVIDER_ACCESS_FAILURE_KIND = "provider_access"
RECOMMENDED_ACTION = "switch_runtime_or_restore_access"


def configured_runtime_model_pairs(
    runtime_map: Mapping[str, str],
    model_map: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Deduplicated ``(runtime, model)`` pairs from configured phase maps.

    Iterates the configured phases (the only source of truth for which
    runtime/model combinations the operator has actually wired up) and returns
    each distinct pair once, preserving first-seen order. Phases with no
    configured runtime are skipped.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for phase, runtime in runtime_map.items():
        if not runtime:
            continue
        model = model_map.get(phase, "")
        pair = (runtime, model)
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def replacement_candidates(
    *,
    failed_runtime: str,
    failed_model: str,
    runtime_map: Mapping[str, str],
    model_map: Mapping[str, str],
    known_runtimes: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    """Provider-neutral replacement candidates from configured pairs.

    Candidates are the configured ``(runtime, model)`` pairs other than the
    failed pair. ``known_runtimes`` — when supplied — only validates that a
    candidate runtime still exists in the registry; it is never the source of
    the pairs. Returns an empty list when no configured alternative exists.
    """
    valid = set(known_runtimes) if known_runtimes is not None else None
    out: list[dict[str, str]] = []
    for runtime, model in configured_runtime_model_pairs(runtime_map, model_map):
        if runtime == failed_runtime and model == failed_model:
            continue
        if valid is not None and runtime not in valid:
            continue
        out.append({"runtime": runtime, "model": model})
    return out


def build_recovery_actions(
    candidates: Iterable[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Durable recovery options for a provider-access failure.

    ``retry`` and ``halt`` are provider-neutral and always present; one
    ``replace`` option is appended per candidate (in candidate order). This is
    the durable evidence list — ``halt`` stays here and in ``meta`` and is not
    projected into the executable SDK action surface.
    """
    actions: list[dict[str, str]] = [
        {"action": "retry"},
        {"action": "halt"},
    ]
    for candidate in candidates:
        actions.append({
            "action": "replace",
            "runtime": candidate["runtime"],
            "model": candidate["model"],
        })
    return actions


def build_provider_access_recovery(
    *,
    failed_phase: str,
    runtime: str,
    model: str,
    runtime_map: Mapping[str, str],
    model_map: Mapping[str, str],
    known_runtimes: Iterable[str] | None = None,
) -> dict[str, object]:
    """Build the durable, provider-neutral recovery projection.

    Pure: the inputs are the failed phase, its configured runtime/model, and
    the configured phase maps. The output is a plain dict suitable for merging
    into ``session['failure']`` and the ``run.end`` event payload.
    """
    candidates = replacement_candidates(
        failed_runtime=runtime,
        failed_model=model,
        runtime_map=runtime_map,
        model_map=model_map,
        known_runtimes=known_runtimes,
    )
    return {
        "failure_kind": PROVIDER_ACCESS_FAILURE_KIND,
        "recoverable": False,
        "recommended_action": RECOMMENDED_ACTION,
        "failed_phase": failed_phase,
        "runtime": runtime,
        "model": model,
        "recovery_actions": build_recovery_actions(candidates),
    }
