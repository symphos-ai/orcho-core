"""core/io/verification_header.py — verification run-header block renderer.

Operator-facing presentation of a *declared* verification contract for the
run header. The legacy one-liner
(``Verification contract: work_mode=…; envs=…; commands=…; schedule=derived,warn``)
packed five distinct dimensions into one comma/semicolon soup, and even the
labelled successor still collapsed every gate into one flat ``commands`` list
that fused command identity with its timing, run mode, policy, and cost. This
module instead renders a compact **gate matrix**: each row keeps the command
identity in its own column, separate from four orthogonal columns —
timing/hook, run mode (auto/manual), effective policy, and kind/cost — plus the
unchanged mode / envs / policy-source / effect lines. A compact single line for
tight terminals summarises the matrix as ``gates=N`` without expanding it.

Design rules (mirrors :mod:`core.io.pipeline_block`):

* **Pure presentation.** Nothing here executes ``verification.commands``,
  builds or reads a ``ScheduledGatePlan``, runs the ``work_mode`` transform,
  reads or writes receipts, or blocks a transition. It only reads
  already-validated, declared fields.
* **No hard pipeline dependency.** The :class:`VerificationContract` is read
  duck-typed (``work_mode`` / ``verification_envs`` / ``commands`` /
  ``schedule`` / ``gate_sets``); the type is only imported under
  ``TYPE_CHECKING`` so ``core.io`` stays a sibling of the pipeline layer, not a
  dependant.
* **Operator language, not schedule jargon.** A ``None`` schedule policy means
  "not set; derive later" — it is surfaced as ``auto-derived from mode/plugin
  defaults``, never as the raw ``schedule: derived`` token. Any property not
  knowable at header time (effective policy after the work_mode transform, or a
  cost with no declared ``cheap`` flag) is shown honestly as ``unknown``.
* **Restrained color.** Labels are dim/neutral; the effect value and any
  ``warn`` / ``require`` policy cell are yellow only for those actual policies.
  No all-green block.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.io.ansi import C, paint

if TYPE_CHECKING:
    from pipeline.verification_contract import (
        GateSet,
        ScheduleEntry,
        VerificationContract,
    )

# The string used for any property not knowable at run-header time (an
# effective policy that would only resolve after the work_mode transform, or a
# cost with no declared ``cheap`` flag). We surface it rather than hide it or
# invent a value.
_UNKNOWN = "unknown"


# Declared schedule policies ordered weakest -> strongest. Used only to pick
# the single most-consequential declared policy for the operator-facing
# ``effect`` line; this is a presentation choice, not a gate computation.
_POLICY_STRENGTH: tuple[str, ...] = ("off", "suggest", "warn", "require")

# Operator-facing effect phrasing per declared policy. Each phrase mentions
# "receipts" so the receipt-expectation dimension is always legible, and none
# of them assert a resolved gate action (the effective action depends on the
# work_mode transform we deliberately do NOT recompute here).
_EFFECT_TEXT: dict[str, str] = {
    "require": "require receipts; missing/failed resolved at gate time",
    "warn": "warn on missing/failed receipts",
    "suggest": "suggested; missing/failed receipts noted, not blocking",
    "off": "receipts not enforced",
}

_AUTO_DERIVED = "auto-derived from mode/plugin defaults"
_AUTO_DERIVED_RECEIPTS = "receipts policy auto-derived from mode/plugin defaults"

# Below this terminal width the labelled block is cramped, so the compact
# single-line form reads better. Chosen to comfortably fit the longest
# structured line at typical command lengths without wrapping.
_COMPACT_WIDTH_THRESHOLD = 60


@dataclass(frozen=True)
class GateRowView:
    """One presentation-ready row of the gate matrix.

    Each row keeps the command identity (``gate``) separate from four orthogonal
    columns so nothing collapses into a flat bucket:

    * ``timing`` — the hook/timing, e.g. ``after_implement`` (after_phase),
      ``before_<phase>`` (before_phase), ``delivery`` (before_delivery),
      ``operator`` (manual_only), ``on_resume`` (on_resume).
    * ``run_mode`` — ``auto`` for before_phase/after_phase/before_delivery,
      ``manual`` for manual_only/on_resume. A manual_only gate is always legible
      as ``operator`` / ``manual``, never disguised as an ordinary auto gate.
    * ``policy`` — the effective declared policy for this gate
      (off|suggest|warn|require), or ``unknown`` when it would only resolve
      after the work_mode transform we deliberately do not recompute here.
    * ``kind`` — declared cost: ``cheap`` when the command (or its gate set)
      declares ``cheap: true``, else ``unknown``. No env-proof/targeted/broad
      taxonomy is invented without a declared source.

    The gate identity is ``(gate, timing)`` derived from ``(command, hook,
    phase)``; rows are deduplicated on that identity by the builder.
    """

    gate: str
    timing: str
    run_mode: str
    policy: str
    kind: str


@dataclass(frozen=True)
class VerificationHeaderView:
    """Primitive, presentation-ready view of a declared verification contract.

    Built by :func:`build_verification_header_view` from the already-validated
    contract; holds only strings/tuples so :mod:`core.io` never reaches back
    into the pipeline layer. ``gates`` is the per-gate matrix (command identity
    kept separate from its orthogonal timing/run_mode/policy/kind columns).
    ``effect`` always mentions "receipts" so the receipt-expectation dimension
    stays legible. ``warned`` flags whether the strongest declared policy is
    ``warn`` / ``require`` (drives the restrained yellow on the effect value
    only).
    """

    mode: str
    envs: tuple[str, ...]
    gates: tuple[GateRowView, ...]
    policy_source: str
    effect: str
    warned: bool = False


def build_verification_header_view(
    contract: VerificationContract | None,
) -> VerificationHeaderView | None:
    """Project a declared contract into a :class:`VerificationHeaderView`.

    Returns ``None`` when no contract is declared (the header then omits the
    Verification section entirely, byte-identical to the no-contract path).

    Pure and total: reads only declared fields (``work_mode``,
    ``verification_envs``, ``commands``, ``gate_sets``, and each ``schedule``
    entry's hook/phase/policy/commands/gate_sets), never executes a command,
    never builds or reads a gate plan, never runs the work_mode transform,
    never touches receipts, and never raises on empty fields.
    """
    if contract is None:
        return None

    mode = contract.work_mode or "default"
    envs = tuple(sorted(contract.verification_envs))
    gates = _build_gate_rows(contract)
    policy_source, effect, warned = _summarize_policy(gates)

    return VerificationHeaderView(
        mode=mode,
        envs=envs,
        gates=gates,
        policy_source=policy_source,
        effect=effect,
        warned=warned,
    )


def _summarize_policy(
    gates: tuple[GateRowView, ...],
) -> tuple[str, str, bool]:
    """Derive the ``policy_source`` / ``effect`` / ``warned`` summary.

    The summary is computed from the *same* effective per-gate policies the
    matrix shows, so the top lines never contradict a row: a gate whose policy
    is declared on its gate set (``gate_set.default_policy``) counts as declared
    here too, not as auto-derived. A row policy of ``unknown`` (``None`` at both
    the entry and the gate-set level) is the only "derive later" case — surfaced
    as ``auto-derived``, never inferred into a behaviour. No work_mode transform
    and no ``build_scheduled_gate_plan`` are involved.
    """
    if not gates:
        return "no scheduled gates", _AUTO_DERIVED_RECEIPTS, False

    # ``unknown`` rows defer their policy to the mode/plugin defaults; the rest
    # carry a declared policy (from the entry or its gate set).
    has_derived = any(g.policy == _UNKNOWN for g in gates)
    declared = sorted({g.policy for g in gates if g.policy in _POLICY_STRENGTH})

    if has_derived:
        policy_source = _AUTO_DERIVED
    elif declared:
        policy_source = "declared in contract (" + ", ".join(declared) + ")"
    else:  # gates exist but every policy is unknown -> covered by has_derived
        policy_source = _AUTO_DERIVED

    strongest = _strongest_policy(declared)
    if strongest is not None:
        effect = _EFFECT_TEXT[strongest]
        warned = strongest in ("warn", "require")
    else:
        # Only derived policies — the effective consequence is not known at
        # header time, so stay honest rather than inferring behaviour.
        effect = _AUTO_DERIVED_RECEIPTS
        warned = False
    return policy_source, effect, warned


def _strongest_policy(declared: list[str]) -> str | None:
    """Most-consequential declared policy, or ``None`` when none are declared."""
    ranked = [p for p in _POLICY_STRENGTH if p in declared]
    return ranked[-1] if ranked else None


# ── gate matrix builder ────────────────────────────────────────────────
#
# A gate's identity is ``(command, hook, phase)``. Each schedule entry can name
# its commands two ways, and BOTH must be expanded or whole gates vanish:
#   (a) ``entry.commands`` — commands listed directly on the entry, and
#   (b) ``entry.gate_sets`` — each name resolved via
#       ``contract.gate_sets[name].commands`` (names are validated at contract
#       normalisation, so the lookup is always present).
# A manual_only/operator gate is very often declared as
# ``{"manual_only": true, "gate_sets": ["manuals"]}`` with an EMPTY
# ``entry.commands``; without the gate_sets expansion such gates would be lost.

# hook -> run mode. Phase-anchored and delivery hooks fire automatically; the
# operator/resume hooks are run by a human. manual_only therefore reads as
# ``manual`` (and ``operator`` timing), never as an ordinary auto gate.
_RUN_MODE: dict[str, str] = {
    "before_phase": "auto",
    "after_phase": "auto",
    "before_delivery": "auto",
    "manual_only": "manual",
    "on_resume": "manual",
}


def _timing(hook: str, phase: str) -> str:
    """Operator-facing timing label for a hook (+ phase where it carries one)."""
    if hook in ("before_phase", "after_phase") and phase:
        return f"{hook.split('_')[0]}_{phase}"
    if hook == "before_delivery":
        return "delivery"
    if hook == "manual_only":
        return "operator"
    return hook


def _gate_policy(entry: ScheduleEntry, backing: list[GateSet]) -> str:
    """Effective declared policy: entry, else strictest backing default, else unknown.

    ``backing`` is every gate set that contributes this command under the entry
    (a command listed both directly and via a gate set, or via several gate sets,
    is backed by all of them). When the entry omits a policy, the strictest
    declared ``default_policy`` across the backing sets wins — mirroring
    :func:`pipeline.verification_selection._merge_defaults` (max strictness by
    :data:`_POLICY_STRENGTH`), so a stricter gate set is never hidden behind a
    laxer one. The work_mode transform is intentionally NOT applied — when no
    policy is declared at any level the consequence is not known at header time,
    so we stay honest with ``unknown`` rather than inferring behaviour.
    """
    if entry.policy is not None:
        return entry.policy
    declared = [
        gate_set.default_policy
        for gate_set in backing
        if gate_set.default_policy is not None
    ]
    if declared:
        return max(declared, key=_POLICY_STRENGTH.index)
    return _UNKNOWN


def _gate_kind(
    contract: VerificationContract, command: str, backing: list[GateSet],
) -> str:
    """Declared cost for a command: ``cheap`` when any declared source says so.

    Mirrors :func:`pipeline.verification_selection._merge_defaults`' OR-ed cheap:
    the row is ``cheap`` when the per-command ``cheap`` is true OR any backing
    gate set declares ``default_cheap`` true. Anything else (all sources false or
    undeclared) is ``unknown`` — we do not invent a cost taxonomy without a
    declared source.
    """
    spec = contract.commands.get(command, {})
    cheap = spec.get("cheap") is True or any(
        gate_set.default_cheap for gate_set in backing
    )
    return "cheap" if cheap else _UNKNOWN


def _build_gate_rows(
    contract: VerificationContract,
) -> tuple[GateRowView, ...]:
    """Project ``contract.schedule`` into the deduplicated gate matrix.

    Commands not referenced by any schedule entry (neither in ``entry.commands``
    nor via ``entry.gate_sets``) are omitted — the matrix is the *scheduled*
    gates; unscheduled commands carry no timing/run_mode and are surfaced
    elsewhere. Total on empty fields: a contract with no schedule yields ``()``.
    """
    rows: list[GateRowView] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in contract.schedule:
        # Per command in this entry, collect EVERY backing gate set (in
        # declaration order) so a command listed both directly and via a gate
        # set keeps the gate set's declared defaults — the bare direct source no
        # longer shadows the richer gate-set metadata. ``order`` preserves
        # first-seen position (entry.commands first, then gate-set commands).
        backing: dict[str, list[GateSet]] = {}
        order: list[str] = []
        for cmd in entry.commands:
            if cmd not in backing:
                backing[cmd] = []
                order.append(cmd)
        for name in entry.gate_sets:
            gate_set = contract.gate_sets.get(name)
            if gate_set is None:  # defensive; names are validated upstream
                continue
            for cmd in gate_set.commands:
                if cmd not in backing:
                    backing[cmd] = []
                    order.append(cmd)
                backing[cmd].append(gate_set)

        for command in order:
            identity = (command, entry.hook, entry.phase)
            if identity in seen:
                continue
            seen.add(identity)
            sources = backing[command]
            rows.append(
                GateRowView(
                    gate=command,
                    timing=_timing(entry.hook, entry.phase),
                    run_mode=_RUN_MODE.get(entry.hook, _UNKNOWN),
                    policy=_gate_policy(entry, sources),
                    kind=_gate_kind(contract, command, sources),
                ),
            )
    return tuple(rows)


def render_verification_header(
    view: VerificationHeaderView,
    *,
    compact: bool | None = None,
    color: bool | None = None,
) -> str:
    """Render the verification view as a header block.

    Structured form (default; chosen automatically when the terminal is wide
    enough) — a gate matrix keeping each command's identity separate from its
    orthogonal timing / run mode / policy / kind columns::

        Verification
          mode      pro
          envs      mcp-local-core
          policy    auto-derived from mode/plugin defaults
          effect    warn on missing/failed receipts
          gates     gate           timing           run   policy   kind
                    lint           after_implement  auto  warn     cheap
                    e2e            operator         manual require  unknown

    Compact form (``compact=True``, or auto on a narrow terminal): one line
    with semantic labels and ``·`` separators, the matrix summarised as
    ``gates=N``. ``compact=None`` auto-decides from
    :func:`shutil.get_terminal_size`. ``color`` follows
    :func:`core.io.ansi.paint`.
    """
    if compact is None:
        compact = _terminal_width() < _COMPACT_WIDTH_THRESHOLD

    envs = ", ".join(view.envs) or "—"
    effect_color = (C.YELLOW,) if view.warned else ()

    if compact:
        bits = [
            f"mode={view.mode}",
            f"envs={envs}",
            f"policy={view.policy_source}",
            "effect=" + paint(view.effect, *effect_color, color=color),
            f"gates={len(view.gates)}",
        ]
        label = paint("Verification", C.CYAN, C.BOLD, color=color)
        sep = _dim(" · ", color)
        return f"{label}  " + sep.join(bits)

    rows = [
        ("mode", view.mode, ()),
        ("envs", envs, ()),
        ("policy", view.policy_source, ()),
        ("effect", view.effect, effect_color),
    ]
    lines = [paint("Verification", C.CYAN, C.BOLD, color=color)]
    for key, value, styles in rows:
        label = paint(f"{key:<8}", C.GREY, color=color)
        painted = paint(value, *styles, color=color) if styles else value
        lines.append(f"  {label}  {painted}")
    lines.extend(_gate_matrix_lines(view.gates, color=color))
    return "\n".join(lines)


# Column order for the structured gate matrix; command identity stays first and
# distinct from the four orthogonal property columns.
_GATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("gate", "gate"),
    ("timing", "timing"),
    ("run_mode", "run"),
    ("policy", "policy"),
    ("kind", "kind"),
)


def _gate_matrix_lines(
    gates: tuple[GateRowView, ...], *, color: bool | None,
) -> list[str]:
    """Render the aligned gate-matrix section under the ``gates`` label.

    Empty matrix → a single ``gates  —`` line. Otherwise a header row of column
    names plus one row per gate, columns kept independent so command identity is
    never fused with its timing/run_mode/policy/kind. A ``warn`` / ``require``
    policy cell is the only colored value (restrained yellow); no all-green.
    """
    label = paint(f"{'gates':<8}", C.GREY, color=color)
    if not gates:
        return [f"  {label}  —"]

    header = [name for _, name in _GATE_COLUMNS]
    cells = [
        [getattr(g, attr) for attr, _ in _GATE_COLUMNS] for g in gates
    ]
    widths = [
        max(len(header[i]), *(len(row[i]) for row in cells))
        for i in range(len(_GATE_COLUMNS))
    ]

    def _format(row: list[str], *, painted: bool) -> str:
        parts: list[str] = []
        for i, value in enumerate(row):
            text = value.ljust(widths[i]) if i < len(row) - 1 else value
            if painted and _GATE_COLUMNS[i][0] == "policy" and value in (
                "warn", "require",
            ):
                text = paint(text, C.YELLOW, color=color)
            parts.append(text)
        return "  ".join(parts)

    header_line = paint(_format(header, painted=False), C.GREY, color=color)
    lines = [f"  {label}  {header_line}"]
    pad = " " * (8 + 4)  # align matrix rows under the header (2 + 8 label + 2)
    lines.extend(pad + _format(row, painted=True) for row in cells)
    return lines


def _dim(text: str, color: bool | None) -> str:
    return paint(text, C.GREY, color=color)


def _terminal_width() -> int:
    try:
        return max(shutil.get_terminal_size((80, 24)).columns, 40)
    except OSError:
        return 80


__all__ = [
    "GateRowView",
    "VerificationHeaderView",
    "build_verification_header_view",
    "render_verification_header",
]
