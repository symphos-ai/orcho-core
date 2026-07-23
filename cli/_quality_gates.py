# SPDX-License-Identifier: Apache-2.0
"""cli/_quality_gates.py — read-only ``orcho quality-gates`` command body.

Projects a project's *declared* verification contract into the same gate matrix
the run-header banner shows, rendered through the ONE shared formatter
:func:`core.io.verification_header.render_gate_matrix`. The command never starts
a run, executes a gate command, or writes a receipt — it only loads the plugin,
validates the declared contract, and reads the shared gate ledger.

Three modes, one projection (``pipeline.verification_ledger.build_gate_ledger``)
and one renderer:

* ``--profile <work_kind>`` — resolve the profile from the shipped catalogue and
  derive ``has_final_phase`` from its phases (∩ ``FINAL_PHASES``), so a warn/off
  gate reads ``pre-final`` under a profile that has a final phase and
  ``not auto-run`` under one that does not.
* ``--paths <globs/files>`` — feed the paths to the ledger as ``changed_files`` so
  each gate's on-path disposition resolves to ``active`` / ``dormant`` / ``manual``
  by identity (the ledger delegates the path-matching to the selection engine).
* no ``--profile`` — render the declared matrix with ``has_final_phase=None`` so
  warn/off gates are honestly marked ``profile-dependent`` rather than guessed;
  required gates still read their own timing hook.

The command routing (parser + ``set_defaults(func=...)``) stays in
``cli/orcho.py``; only the body lives here.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

    from pipeline.verification_ledger import GateLedgerRow


def cmd_quality_gates(args: argparse.Namespace) -> int:
    """Render the declared verification gate matrix. Strictly read-only.

    Loads the project's plugin, validates its declared verification contract,
    projects it through the shared gate ledger, and renders the matrix via the
    shared ``render_gate_matrix`` helper. Returns 0 on success (including the
    "no contract declared" case — absent-but-worked), 2 on an unknown profile or
    an invalid declared contract. Nothing is executed and nothing is written in
    any mode.
    """
    from pipeline.plugins import load_plugin
    from pipeline.verification_contract import (
        VerificationContract,
        VerificationContractError,
    )

    project = getattr(args, "project", None) or "."
    profile_name = getattr(args, "profile", None)
    paths = getattr(args, "paths", None)
    changed_files = tuple(paths) if paths else None

    plugin = load_plugin(project)
    try:
        contract = VerificationContract.from_plugin(plugin)
    except VerificationContractError as exc:
        print(f"quality-gates: invalid verification contract: {exc}", file=sys.stderr)
        return 2

    if contract is None:
        print(
            f"quality-gates: no verification contract declared for {project!r}.",
        )
        return 0

    has_final_phase, profile_error = _resolve_has_final_phase(profile_name)
    if profile_error is not None:
        print(profile_error, file=sys.stderr)
        return 2

    from pipeline.verification_ledger import build_gate_ledger

    ledger = build_gate_ledger(
        contract,
        has_final_phase=has_final_phase,
        changed_files=changed_files,
    )
    print(
        _render_matrix(
            ledger,
            contract=contract,
            has_final_phase=has_final_phase,
            profile_name=profile_name,
            resolved=changed_files is not None,
        ),
    )
    return 0


def _resolve_has_final_phase(
    profile_name: str | None,
) -> tuple[bool | None, str | None]:
    """Derive ``has_final_phase`` from a named profile, or ``(None, None)``.

    With no ``--profile`` the profile is deliberately NOT guessed → ``None`` so
    the ledger marks warn/off gates ``profile-dependent``. With a name, resolve it
    from the shipped catalogue and intersect its phases with the single
    ``FINAL_PHASES`` set (never a duplicated copy). An unknown name yields an
    operator-facing error string (second tuple slot) instead of a value.
    """
    if profile_name is None:
        return None, None

    from pipeline.verification_contract import FINAL_PHASES

    profiles = _load_profiles()
    profile = profiles.get(profile_name)
    if profile is None:
        known = ", ".join(sorted(profiles)) or "(none)"
        return None, (
            f"quality-gates: unknown profile {profile_name!r} "
            f"(known: {known})"
        )
    from pipeline.project.profile_setup import _profile_phase_names

    return bool(_profile_phase_names(profile) & set(FINAL_PHASES)), None


def _load_profiles() -> dict:
    """Load the shipped profile catalogue (same path the other CLI paths use)."""
    from core.infra.paths import CONFIG_DIR
    from pipeline.profiles.loader import load_profiles_v2_with_plugins

    return load_profiles_v2_with_plugins(CONFIG_DIR / "pipeline_profiles_v2.json")


def _render_matrix(
    ledger: tuple[GateLedgerRow, ...],
    *,
    contract: object,
    has_final_phase: bool | None,
    profile_name: str | None,
    resolved: bool,
) -> str:
    """Render the ledger as the shared gate matrix plus an optional resolution.

    The matrix itself is formatted ONLY by the shared
    :func:`core.io.verification_header.render_gate_matrix` (the banner's formatter,
    the T2 integration point) — the rows are copied field-for-field into
    ``GateRowView`` exactly as the banner does, never reformatted or recomputed
    here. When ``resolved`` is set (``--paths`` supplied), a trailing section
    lists each gate's on-path disposition (``active`` / ``dormant`` / ``manual``)
    read straight from the ledger's identity-based resolve.
    """
    from core.io.verification_header import (
        build_verification_header_view,
        render_gate_matrix,
    )

    view = build_verification_header_view(
        contract, has_final_phase=has_final_phase, ledger_rows=ledger,
    )
    views = view.gates if view is not None else ()

    if profile_name is not None:
        title = f"Verification gate matrix — profile={profile_name}"
    else:
        title = (
            "Verification gate matrix — declared "
            "(no profile; warn/off gates shown profile-dependent)"
        )

    lines = [title]
    matrix = render_gate_matrix(views)
    if not matrix:
        lines.append("  (no scheduled gates)")
        return "\n".join(lines)
    lines.extend(f"  {row}" for row in matrix)

    if resolved:
        lines.append("")
        lines.append("Selection for given paths:")
        width = max((len(row.gate) for row in ledger), default=0)
        for row in ledger:
            if row.selected:
                selection = "selected"
            elif row.selection_reason:
                selection = f"not_selected ({row.selection_reason})"
            else:
                selection = "unresolved"
            lines.append(f"  {row.gate:<{width}}  {selection}")

    return "\n".join(lines)
