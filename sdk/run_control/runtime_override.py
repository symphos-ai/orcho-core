# SPDX-License-Identifier: Apache-2.0
"""Durable persistence of an operator runtime/model override (ADR 0101 / T2).

When a run dies on a terminal provider-access failure, the durable recovery
record (ADR 0101 / T1) offers the operator a set of provider-neutral *replace*
candidates — configured ``(runtime, model)`` pairs other than the failed one.
This module persists the operator's chosen replacement into the run's
``meta.json`` so a subsequent resume re-enters the pipeline and applies it to
exactly the failed phase.

Execution contract:

* The chosen pair is validated against the **configured** replacement
  candidates for the named phase — the same provider-neutral candidate set the
  recovery record was built from (``phase_runtime_map`` + ``phase_model_map``;
  ``AgentRegistry.names()`` only validates that the runtime exists). A pair
  that is not a configured candidate is rejected; there is no silent fallback.
* The record is written under ``meta['runtime_override']`` as
  ``{phase, runtime, model, decided_at, note}``. The write is **idempotent**
  on the operator-meaningful triple ``(phase, runtime, model)`` plus ``note``:
  re-persisting the same decision is a no-op that preserves the original
  ``decided_at``; a *different* decision is a **conflict** (raised, never a
  silent overwrite) — mirroring the phase-handoff / waiver idempotency model.
* :func:`RunService.resume` calls :func:`persist_runtime_override` **before**
  building the ``ProjectRunRequest``, so the durable override is fixed ahead of
  resume. The canonical resume tool is ``orcho_run_resume`` with args
  ``{run_id, runtime_override: {phase, runtime, model}}`` (``run_id`` is
  mandatory — it addresses the specific run).

MCP visibility: the override is *applied* from **durable meta** — any resume
transport that re-enters ``run_project_pipeline`` (the SDK ``RunService.resume``,
the CLI ``orcho-run --resume``, and the MCP ``orcho_run_resume`` subprocess)
reads the persisted record from ``meta.json`` and applies it, and the record
survives the resume because ``init_session_with_atexit`` carries it forward into
the fresh session before rewriting ``meta.json`` (alongside the ``phase_handoff``
carry-forward). *Persisting* the override, however, is the job of the transport
that receives the operator's choice. The SDK ``next_actions`` replace Action
delivers that choice through the MCP ``orcho_run_resume`` tool, whose strict
schema is updated synchronously (cross-repo companion in ``orcho-mcp``) to accept
``runtime_override={phase, runtime, model}`` and call this module's
:func:`persist_runtime_override` before the resume subprocess spawns. This module
stays the single validation + persistence authority for every transport.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "RuntimeOverrideConflict",
    "RuntimeOverrideError",
    "configured_replacement_candidates",
    "persist_runtime_override",
    "read_runtime_override",
]


class RuntimeOverrideError(ValueError):
    """The chosen runtime/model pair is not a configured replacement candidate."""


class RuntimeOverrideConflict(RuntimeOverrideError):
    """A different runtime override is already persisted for this run."""


def configured_replacement_candidates(phase: str) -> list[dict[str, str]]:
    """Provider-neutral replacement candidates for ``phase``.

    Reuses the T1 candidate derivation: configured ``(runtime, model)`` pairs
    (``AppConfig.phase_runtime_map`` + ``phase_model_map``) other than the
    phase's own configured pair, with the runtime validated against the
    registered runtimes. No provider name is hard-coded.
    """
    from core.infra import config
    from pipeline.project import provider_recovery

    app = config.AppConfig.load()
    runtime_map = app.phase_runtime_map
    model_map = app.phase_model_map

    from agents.registry import AgentRegistry

    known = AgentRegistry.default().names()
    return provider_recovery.replacement_candidates(
        failed_runtime=runtime_map.get(phase, "claude"),
        failed_model=model_map.get(phase, ""),
        runtime_map=runtime_map,
        model_map=model_map,
        known_runtimes=known,
    )


def read_runtime_override(meta: dict[str, Any]) -> dict[str, str] | None:
    """Return the persisted override triple ``{phase, runtime, model}`` or None.

    Pure, tolerant read: a missing / malformed record yields ``None`` so a
    plain resume (no persisted override) is a strict no-op for callers.
    """
    record = meta.get("runtime_override")
    if not isinstance(record, dict):
        return None
    phase = record.get("phase")
    runtime = record.get("runtime")
    model = record.get("model")
    if not (phase and runtime and model):
        return None
    return {"phase": str(phase), "runtime": str(runtime), "model": str(model)}


def _load_meta(run_dir: Path) -> dict[str, Any]:
    meta_file = run_dir / "meta.json"
    if not meta_file.is_file():
        return {}
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def persist_runtime_override(
    run_dir: Path,
    *,
    phase: str,
    runtime: str,
    model: str,
    note: str | None = None,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Validate and durably persist an operator runtime/model override.

    Validates ``(runtime, model)`` against the configured replacement
    candidates for ``phase`` (raising :class:`RuntimeOverrideError` for a
    non-candidate pair), then writes ``meta['runtime_override']`` idempotently:

    * no existing record → write a new ``{phase, runtime, model, decided_at,
      note}`` record and return it;
    * an existing record with the same ``(phase, runtime, model)`` and
      ``note`` → no-op, return the existing record verbatim (original
      ``decided_at`` preserved);
    * an existing record that diverges → raise
      :class:`RuntimeOverrideConflict`.

    Returns the persisted record. Never silently overwrites a divergent
    decision.
    """
    candidates = configured_replacement_candidates(phase)
    if {"runtime": runtime, "model": model} not in candidates:
        raise RuntimeOverrideError(
            f"runtime override for phase {phase!r}: pair "
            f"(runtime={runtime!r}, model={model!r}) is not a configured "
            f"replacement candidate. Configured candidates: {candidates!r}."
        )

    run_dir = Path(run_dir)
    meta = _load_meta(run_dir)
    existing = meta.get("runtime_override")
    if isinstance(existing, dict):
        same = (
            existing.get("phase") == phase
            and existing.get("runtime") == runtime
            and existing.get("model") == model
            and existing.get("note") == note
        )
        if same:
            return existing
        raise RuntimeOverrideConflict(
            f"runtime override conflict for {run_dir.name}: existing "
            f"{{phase={existing.get('phase')!r}, runtime={existing.get('runtime')!r}, "
            f"model={existing.get('model')!r}}} differs from requested "
            f"{{phase={phase!r}, runtime={runtime!r}, model={model!r}}}. "
            "Overrides are exact-payload idempotent; refusing to overwrite."
        )

    record: dict[str, Any] = {
        "phase": phase,
        "runtime": runtime,
        "model": model,
        "decided_at": decided_at or datetime.now().isoformat(),
        "note": note,
    }
    meta["runtime_override"] = record
    _write_meta(run_dir, meta)
    return record
