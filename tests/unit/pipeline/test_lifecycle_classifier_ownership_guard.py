# SPDX-License-Identifier: Apache-2.0
"""ADR 0114 P0 — single-ownership guard for run-lifecycle decision tables.

Load-bearing architecture-fitness test. The consolidation work behind ADR 0114
P0 collapsed every duplicated copy of the run-lifecycle *decision tables* onto
exactly one home each in ``orcho-core``:

* the terminal-status vocabularies (``TERMINAL_SUCCESS_STATUSES`` /
  ``RESUMABLE_TERMINAL_STATUSES`` / ``FAILURE_TERMINAL_STATUSES`` /
  ``TERMINAL_CROSS_STATUSES``) live only in
  ``pipeline/run_state/status_vocab.py``;
* the commit-delivery ``status -> halt_reason`` map
  (``COMMIT_DELIVERY_HALT_REASONS``) lives only in
  ``pipeline/engine/commit_delivery.py``;
* the decidable-handoff classifier (the ``(status, active)`` predicate
  ``_is_decidable_handoff_status``) lives only in ``sdk/phase_handoff.py``; its
  decision vocabulary (``PAUSE_STATUS`` / ``INTERRUPTED_STATUS``) lives only in
  ``pipeline/run_state/status_vocab.py``. No site may re-derive it as a local
  ``{"awaiting_phase_handoff", "interrupted"}`` value-copy set.

Green tests are necessary but not sufficient: a future change could silently
re-introduce a parallel ``frozenset({"done", "success", "completed"})`` or a
hand-written halt-reason ladder somewhere else, and behaviour would still pass
its own unit tests while the single-source invariant rots. This guard fails
that drift statically (AST scan of the sources, no import side-effects):

1. **Core single ownership.** No file under ``pipeline/`` / ``sdk/`` / ``cli/``
   may carry a canonical terminal-status set literal or the halt-reason map
   outside its declared home module.
2. **No re-introduction in MCP / CLI outside a named allowlist.** ``orcho-mcp``
   adapts the core wire shape; it must not re-own the decision tables except in
   an explicit, documented allowlist of adapter modules. The CLI scan is part
   of (1) and is mandatory. The MCP scan is robust to the repo being absent
   (an explicit ``skip`` rather than a false pass).

The allowlist is deliberately *named and documented* (not a blanket
suppression) so a legitimate wire-rename / shape-adapter is permitted while a
fresh duplicate trips the guard. ``test_guard_trips_on_injected_duplicate``
proves the scanner is not a no-op by feeding it a synthetic rogue module.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

# orcho-core repo root: <root>/tests/unit/pipeline/<this file>.
_CORE_ROOT = Path(__file__).resolve().parents[3]


# ── Canonical decision-table signatures ───────────────────────────────────────
#
# Each signature is the *value set* a duplicate would reproduce. We match on the
# exact set of string members so a broader vocabulary (e.g.
# ``pipeline/project/handoff_advice_evidence.py``'s 6-member success synonym
# set) does NOT trip the guard — only a byte-equivalent re-declaration does.
#
# RESUMABLE_TERMINAL_STATUSES and FAILURE_TERMINAL_STATUSES share the same
# members ({halted, failed, interrupted}); that single signature covers both.
_STATUS_SET_SIGNATURES: dict[str, frozenset[str]] = {
    "TERMINAL_SUCCESS_STATUSES": frozenset({"done", "success", "completed"}),
    "RESUMABLE_OR_FAILURE_TERMINAL_STATUSES": frozenset(
        {"halted", "failed", "interrupted"}
    ),
    "TERMINAL_CROSS_STATUSES": frozenset({"done", "failed", "halted", "cancelled"}),
    # The decidable-handoff vocabulary as a value-copy set. The canonical owner
    # is the ``(status, active)`` predicate ``_is_decidable_handoff_status`` in
    # ``sdk/phase_handoff.py``, which compares against the named ``PAUSE_STATUS``
    # / ``INTERRUPTED_STATUS`` constants rather than building this set. A local
    # ``{"awaiting_phase_handoff", "interrupted"}`` membership set is exactly the
    # status-only re-derivation that drifts from the torn-handoff form (where the
    # *active* payload, not the status alone, decides) — so it must not reappear.
    "DECIDABLE_HANDOFF_STATUSES": frozenset(
        {"awaiting_phase_handoff", "interrupted"}
    ),
}

# The COMMIT_DELIVERY_HALT_REASONS map, identified by the exact set of its
# string *values* (a dict literal mapping commit-delivery statuses to halt
# reasons). Presentation tables that are keyed *by* halt_reason with non-string
# (e.g. tuple) values — like pipeline/project/finalization.py's label map — do
# not match, because their value set is not a set of these strings.
_HALT_REASON_VALUE_SIGNATURE: frozenset[str] = frozenset(
    {
        "commit_decision_halt",
        "commit_decision_fix",
        "commit_delivery_target_dirty",
        "commit_delivery_failed",
        "commit_delivery_verification_blocked",
    }
)


# ── Allowlists (explicit, documented) ─────────────────────────────────────────

# The two canonical homes in orcho-core. A signature is permitted here and
# nowhere else under pipeline/ / sdk/ / cli/.
_CORE_TABLE_HOMES: frozenset[str] = frozenset(
    {
        "pipeline/run_state/status_vocab.py",  # terminal-status frozensets
        "pipeline/engine/commit_delivery.py",  # COMMIT_DELIVERY_HALT_REASONS
    }
)

# Named orcho-mcp adapter modules permitted to hold a copy of a core-owned
# table. Each entry is a wire-shape adapter or a KNOWN, tracked P1 follow-up
# (migrating it onto orcho-core's canonical sets is explicitly out of scope for
# this consolidation). Adding a NEW entry here is the sanctioned, reviewable way
# to legitimise a shape-adapter; a duplicate that appears WITHOUT a matching
# entry trips the guard.
_MCP_ADAPTER_ALLOWLIST: frozenset[str] = frozenset(
    {
        # P1 (tracked, not yet migrated): re-derives the terminal-success set
        # for its run-status projection wire shaping.
        "src/orcho_mcp/services/run_projection.py",
        # P1 (tracked, not yet migrated): re-derives the resumable-condition set
        # for run diagnosis.
        "src/orcho_mcp/inspection/diagnosis.py",
    }
)


# ── AST scanner (shared by the real scans and the injection self-test) ────────


def _string_set_from_node(node: ast.AST) -> frozenset[str] | None:
    """Return the frozenset of string members if ``node`` is a set /
    ``frozenset(...)`` / ``set(...)`` of *only* string literals, else None."""
    if isinstance(node, ast.Set):
        elts = node.elts
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set"}
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Set | ast.List | ast.Tuple)
    ):
        elts = node.args[0].elts
    else:
        return None
    members: list[str] = []
    for elt in elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            members.append(elt.value)
        else:
            return None
    return frozenset(members)


def _string_value_set_from_dict(node: ast.AST) -> frozenset[str] | None:
    """Return the frozenset of string values if ``node`` is a dict literal whose
    values are *all* string literals, else None."""
    if not isinstance(node, ast.Dict):
        return None
    values: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.append(value.value)
        else:
            return None
    return frozenset(values)


def _scan_file(path: Path) -> list[tuple[str, int]]:
    """Return ``(signature_name, lineno)`` hits for canonical decision tables.

    Deduplicated by ``(name, lineno)``: ``ast.walk`` visits a
    ``frozenset({...})`` as both the ``Call`` and its inner ``Set``, which would
    otherwise double-count the same literal.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: dict[tuple[str, int], None] = {}
    for node in ast.walk(tree):
        members = _string_set_from_node(node)
        if members is not None:
            for name, signature in _STATUS_SET_SIGNATURES.items():
                if members == signature:
                    hits[(name, getattr(node, "lineno", 0))] = None
        values = _string_value_set_from_dict(node)
        if values is not None and values == _HALT_REASON_VALUE_SIGNATURE:
            hits[("COMMIT_DELIVERY_HALT_REASONS", getattr(node, "lineno", 0))] = None
    return list(hits)


def _iter_py(root: Path, *subdirs: str) -> list[Path]:
    """All ``*.py`` under ``root/<subdir>`` (recursive), skipping bytecode."""
    out: list[Path] = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            out.append(path)
    return out


def _violations(
    files: list[Path], root: Path, allowed_rel_paths: frozenset[str]
) -> list[str]:
    """``"<rel>:<lineno>: <signature>"`` for every hit outside the allowlist."""
    found: list[str] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if rel in allowed_rel_paths:
            continue
        for name, lineno in _scan_file(path):
            found.append(f"{rel}:{lineno}: {name}")
    return found


def _find_orcho_mcp_root() -> Path | None:
    """Locate the orcho-mcp repo, or None if unavailable in this environment.

    Resolution order: ``ORCHO_MCP_SRC`` env override, then the first ancestor of
    the orcho-core root that has an ``orcho-mcp/src/orcho_mcp`` package. Returns
    None (→ skip) rather than guessing when the sibling repo is not checked out.
    """
    env = os.environ.get("ORCHO_MCP_SRC")
    if env:
        candidate = Path(env)
        if (candidate / "src" / "orcho_mcp").is_dir():
            return candidate
    for parent in [_CORE_ROOT, *_CORE_ROOT.parents]:
        candidate = parent / "orcho-mcp"
        if (candidate / "src" / "orcho_mcp").is_dir():
            return candidate
    return None


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_core_lifecycle_tables_have_single_owner() -> None:
    """No file under ``pipeline/`` / ``sdk/`` / ``cli/`` (the CLI scan is
    mandatory) re-declares a canonical terminal-status set or the halt-reason
    map outside its declared home module."""
    files = _iter_py(_CORE_ROOT, "pipeline", "sdk", "cli")
    violations = _violations(files, _CORE_ROOT, _CORE_TABLE_HOMES)
    assert not violations, (
        "Run-lifecycle decision tables must have exactly one home in "
        "orcho-core (status_vocab.py for the status sets, commit_delivery.py "
        "for COMMIT_DELIVERY_HALT_REASONS). A duplicate re-appeared:\n  "
        + "\n  ".join(violations)
        + "\nImport the canonical name instead of re-declaring the literal."
    )


def test_mcp_holds_no_unallowlisted_core_owned_tables() -> None:
    """``orcho-mcp`` must not re-own a core-owned decision table outside the
    named adapter allowlist. Skips cleanly when the sibling repo is absent."""
    mcp_root = _find_orcho_mcp_root()
    if mcp_root is None:
        pytest.skip(
            "orcho-mcp repo not available in this environment "
            "(set ORCHO_MCP_SRC or check it out beside orcho-core)"
        )
    files = _iter_py(mcp_root, "src")
    violations = _violations(files, mcp_root, _MCP_ADAPTER_ALLOWLIST)
    assert not violations, (
        "orcho-mcp re-owns a core-owned lifecycle decision table outside the "
        "named adapter allowlist. Either consume the orcho-core canonical set "
        "via the SDK, or — if this is a sanctioned wire-shape adapter — add the "
        "module to _MCP_ADAPTER_ALLOWLIST with a documented reason:\n  "
        + "\n  ".join(violations)
    )


def test_guard_trips_on_injected_duplicate(tmp_path: Path) -> None:
    """Prove the scanner is not a no-op: a synthetic module that re-declares
    both a terminal-status set and the halt-reason map is flagged, and the
    allowlist suppresses it (so a legitimate adapter stays green)."""
    rogue = tmp_path / "rogue_module.py"
    rogue.write_text(
        "TERMINAL = frozenset({'done', 'success', 'completed'})\n"
        "DECIDABLE = {'awaiting_phase_handoff', 'interrupted'}\n"
        "HALT = {\n"
        "    'halted': 'commit_decision_halt',\n"
        "    'fix_requested': 'commit_decision_fix',\n"
        "    'target_dirty': 'commit_delivery_target_dirty',\n"
        "    'commit_failed': 'commit_delivery_failed',\n"
        "    'apply_failed': 'commit_delivery_failed',\n"
        "    'verification_blocked': 'commit_delivery_verification_blocked',\n"
        "}\n",
        encoding="utf-8",
    )

    flagged = _violations([rogue], tmp_path, allowed_rel_paths=frozenset())
    names = {entry.split(": ", 1)[1] for entry in flagged}
    assert "TERMINAL_SUCCESS_STATUSES" in names, flagged
    assert "DECIDABLE_HANDOFF_STATUSES" in names, flagged
    assert "COMMIT_DELIVERY_HALT_REASONS" in names, flagged

    # The named-allowlist mechanism suppresses a sanctioned home/adapter,
    # confirming the guard is not fragile on legitimate shape-adaptation.
    assert (
        _violations([rogue], tmp_path, frozenset({"rogue_module.py"})) == []
    )


def test_canonical_homes_actually_define_the_tables() -> None:
    """Anchor the allowlist to reality: each declared home must genuinely
    contain the signature it is allowed to hold, so the allowlist cannot drift
    into excusing a module that no longer owns the table."""
    status_home = _CORE_ROOT / "pipeline/run_state/status_vocab.py"
    delivery_home = _CORE_ROOT / "pipeline/engine/commit_delivery.py"

    status_hits = {name for name, _ in _scan_file(status_home)}
    assert "TERMINAL_SUCCESS_STATUSES" in status_hits
    assert "TERMINAL_CROSS_STATUSES" in status_hits
    assert "RESUMABLE_OR_FAILURE_TERMINAL_STATUSES" in status_hits
    # ``DECIDABLE_HANDOFF_STATUSES`` is a *negative* signature: it has NO home
    # as a value-copy set. status_vocab.py owns the decision vocabulary only as
    # the separate ``PAUSE_STATUS`` / ``INTERRUPTED_STATUS`` string constants,
    # and the predicate (sdk/phase_handoff.py) compares against them rather than
    # materialising a membership set. So the set literal must appear nowhere,
    # including here.
    assert "DECIDABLE_HANDOFF_STATUSES" not in status_hits

    delivery_hits = {name for name, _ in _scan_file(delivery_home)}
    assert "COMMIT_DELIVERY_HALT_REASONS" in delivery_hits
