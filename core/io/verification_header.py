"""core/io/verification_header.py — verification run-header block renderer.

Operator-facing presentation of a *declared* verification contract for the
run header. The legacy one-liner
(``Verification contract: work_mode=…; envs=…; commands=…; schedule=derived,warn``)
packed five distinct dimensions into one comma/semicolon soup, and even the
labelled successor still collapsed every gate into one flat ``commands`` list
that fused command identity with its timing, run mode, policy, and cost. This
module instead renders a compact **gate matrix**: each row keeps the command
identity in its own column, separate from five orthogonal columns — the
**when** stage, run mode (auto/manual), receipt **policy**, **kind**/cost, and
the gate's **activation condition** — plus the unchanged mode / envs /
policy-source / effect lines. A compact single line for tight terminals
summarises the matrix as ``gates=N`` without expanding it.

Every one of those columns — including policy, kind, and the derived ``when`` —
is read straight from the shared gate ledger
(:mod:`pipeline.verification_ledger`); the header keeps no second projection of
policy/kind. The ``when`` column shows the stage a gate actually runs at
(``after_implement`` / ``delivery`` for a required gate, ``pre-final`` /
``not auto-run`` / ``profile-dependent`` for a warn/manual gate depending on the
profile, ``operator`` for a manual gate) so a warn gate never reads as if it ran
inline at its hook. The **activation** column shows *when the gate is selected* —
``always`` (baseline / broad), ``on-path: <globs>`` (a subsystem gate keyed on
touched paths), or ``manual`` (an operator/manual gate); it is a distinct axis
from both the receipt policy and the ``when`` stage. The receipt-enforcement
policy is also summarised once at the top (``policy`` / ``effect`` lines).

Design rules (mirrors :mod:`core.io.pipeline_block`):

* **Pure presentation.** Nothing here executes ``verification.commands``,
  builds or reads a ``ScheduledGatePlan``, runs the ``work_mode`` transform,
  reads or writes receipts, or blocks a transition. The gate ledger is consumed
  with ``changed_files=None`` (run start, no diff), so it resolves no plan and
  every disposition stays ``None`` — the header only reads already-validated,
  declared fields and the ledger's declared activation condition.
* **No hard pipeline dependency.** The :class:`VerificationContract` is read
  duck-typed (``work_mode`` / ``verification_envs`` / ``commands`` /
  ``schedule`` / ``gate_sets``); its type is only imported under
  ``TYPE_CHECKING`` and the ledger projection is imported *lazily* inside the
  builder, so ``core.io`` stays a sibling of the pipeline layer, not a
  module-level dependant. The ledger owns the timing/run-mode/policy/kind/when
  derivation and the activation condition; this module does not keep a second
  copy.
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
    from pipeline.verification_contract import VerificationContract

# The string used for any property not knowable at run-header time (an
# effective policy that would only resolve after the work_mode transform, or a
# cost with no declared ``cheap`` flag). We surface it rather than hide it or
# invent a value.
_UNKNOWN = "unknown"


# Declared schedule policies ordered weakest -> strongest. Used only to pick
# the single most-consequential declared policy for the operator-facing
# ``effect`` line; this is a presentation choice, not a gate computation.
_POLICY_STRENGTH: tuple[str, ...] = ("manual", "suggest", "warn", "require")

# Operator-facing effect phrasing per declared policy. Each phrase mentions
# "receipts" so the receipt-expectation dimension is always legible, and none
# of them assert a resolved gate action (the effective action depends on the
# work_mode transform we deliberately do NOT recompute here).
_EFFECT_TEXT: dict[str, str] = {
    "require": "require receipts; missing/failed resolved at gate time",
    "warn": "warn on missing/failed receipts",
    "suggest": "suggested; missing/failed receipts noted, not blocking",
    "manual": "manual receipts; missing/failed receipts do not block",
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

    Each row keeps the command identity (``gate``) separate from its orthogonal
    columns so nothing collapses into a flat bucket:

    * ``timing`` — the raw hook/timing, e.g. ``after_implement`` (after_phase),
      ``before_<phase>`` (before_phase), ``delivery`` (before_delivery),
      ``operator`` (manual_only), ``on_resume`` (on_resume). Taken from the gate
      ledger, not recomputed here. Retained on the row but no longer a matrix
      column — the ``when`` axis supersedes it in the display.
    * ``when`` — the operator-facing stage the gate actually runs at, derived by
      the ledger's ``effective_stage`` (``after_implement`` / ``delivery`` for a
      required gate, ``pre-final`` / ``not auto-run`` / ``profile-dependent`` for a
      warn/manual gate depending on the profile, ``operator`` for a manual/suggest
      gate). This is the matrix column that replaces the raw ``timing`` display so
      a warn gate never reads as if it runs inline at its hook.
    * ``run_mode`` — ``auto`` for before_phase/after_phase/before_delivery,
      ``manual`` for manual_only/on_resume. A manual_only gate is always legible
      as ``operator`` / ``manual``, never disguised as an ordinary auto gate.
    * ``policy`` — the effective declared receipt-enforcement policy for this
      gate (manual|suggest|warn|require), or ``unknown`` when it would only resolve
      after the work_mode transform we deliberately do not recompute here. This
      is a *distinct* dimension from activation and drives only the top-level
      ``policy`` / ``effect`` summary; it is no longer a per-gate matrix cell.
    * ``kind`` — declared cost: ``cheap`` when the command (or its gate set)
      declares ``cheap: true``, else ``unknown``. No env-proof/targeted/broad
      taxonomy is invented without a declared source.
    * ``condition`` / ``condition_paths`` — the gate's *activation condition*
      read from the shared ledger: ``always`` (an ``always`` selection rule),
      ``on_path`` (a subsystem ``paths`` rule — ``condition_paths`` carries the
      union of its globs), ``operator`` (a manual/operator gate), or
      ``task_kind``. Rendered as the matrix ``activation`` cell so a path-gated
      gate never reads as an unconditional ``require``. Both default so a
      directly-constructed row renders byte-identically.

    The gate identity is ``(gate, timing)`` derived from ``(command, hook,
    phase)``; rows are deduplicated on that identity by the ledger.
    """

    gate: str
    timing: str
    run_mode: str
    policy: str
    kind: str
    condition: str = "always"
    condition_paths: tuple[str, ...] = ()
    activation_binding: str = ""
    when: str = ""


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
    *,
    has_final_phase: bool | None = None,
) -> VerificationHeaderView | None:
    """Project a declared contract into a :class:`VerificationHeaderView`.

    Returns ``None`` when no contract is declared (the header then omits the
    Verification section entirely, byte-identical to the no-contract path).

    ``has_final_phase`` (keyword-only, defaulting to ``None`` so existing callers
    are unaffected) is threaded straight into the ledger's ``when`` derivation:
    ``True`` when the active profile has a final delivery phase, ``False`` when it
    provably does not (e.g. a fast / small_task profile), ``None`` when the profile
    is unknown. It is display-only — it never changes the row set, policy, or kind.

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
    gates = _build_gate_rows(contract, has_final_phase=has_final_phase)
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
# The row set, its ``(command, hook, phase)`` identity, and EVERY presentation
# axis — timing, run_mode, activation ``condition``, receipt ``policy``, declared
# ``kind`` (cost), and the derived ``when`` stage — come from the shared gate
# ledger (:mod:`pipeline.verification_ledger`). It is the SINGLE source; the
# header re-reads neither the raw ``contract.schedule`` nor its gate sets, and no
# longer computes policy/kind in a second pass. The ledger owns the dedup that
# keeps two schedule entries of one command under different ``(hook, phase)`` as
# distinct rows, and the ``has_final_phase`` that resolves each warn/manual gate's
# honest ``when``.


def _build_gate_rows(
    contract: VerificationContract,
    *,
    has_final_phase: bool | None = None,
) -> tuple[GateRowView, ...]:
    """Project the shared gate ledger into the deduplicated gate matrix.

    The row set, identity, and every column — timing, run_mode, activation
    ``condition``, receipt ``policy``, declared ``kind``, and the derived ``when``
    — come from :func:`pipeline.verification_ledger.build_gate_ledger` (consumed
    with ``changed_files=None`` — run start, no diff, so no plan is resolved and
    no disposition is computed). ``has_final_phase`` is passed through only to feed
    the ledger's ``when`` derivation. This function formats; it computes nothing.

    Commands not referenced by any schedule entry are omitted (only scheduled
    gates are rows). Total on empty fields: a contract with no schedule yields
    ``()``. The ledger is imported lazily so ``core.io`` keeps no module-level
    dependency on the pipeline layer.
    """
    from pipeline.verification_ledger import build_gate_ledger

    rows: list[GateRowView] = []
    for row in build_gate_ledger(contract, has_final_phase=has_final_phase):
        rows.append(
            GateRowView(
                gate=row.gate,
                timing=row.timing,
                run_mode=row.run_mode,
                policy=row.policy,
                kind=row.kind,
                condition=row.condition,
                condition_paths=row.condition_paths,
                activation_binding=row.activation_binding,
                when=row.when,
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
    orthogonal when / run mode / policy / kind columns, with the gate's activation
    condition (``always`` / ``on-path: <globs>`` / ``manual``) as the trailing
    column so a path-gated gate never reads as an unconditional ``require``. The
    ``when`` column shows the stage the gate actually runs at, so a required gate
    (``after_implement``) is legibly distinct from a warn gate (``pre-final``)::

        Verification
          mode      pro
          envs      mcp-local-core
          policy    declared in contract (require, warn)
          effect    require receipts; missing/failed resolved at gate time
          gates     gate               when             run     policy   kind     activation
                    lint               pre-final        auto    warn     cheap    always
                    verification-unit  after_implement  auto    require  unknown  on-path: pipeline/verification*.py
                    e2e                operator         manual  suggest  unknown  manual

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
# distinct from the orthogonal property columns. ``when`` (the stage the gate
# runs at) supersedes the raw ``timing`` display and leads the property columns;
# ``policy`` is a short column between ``run`` and ``kind``. ``activation`` is
# last (and so never width-padded) because its ``on-path: <globs>`` form can run
# long.
_GATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("gate", "gate"),
    ("when", "when"),
    ("run_mode", "run"),
    ("policy", "policy"),
    ("kind", "kind"),
    ("activation", "activation"),
)


def _activation_label(row: GateRowView) -> str:
    """Operator-facing activation cell for a gate row.

    ``always`` for an always-selected gate, ``manual`` for an operator/manual
    gate, ``on-path: <globs>`` (union of the subsystem globs) for a path-gated
    gate, and ``task-kind`` for a task-kind gate. This surfaces *when* the gate
    activates so a subsystem gate is never read as an unconditional require.
    """
    binding = row.activation_binding or row.condition
    if binding == "on_path":
        if row.condition_paths:
            return "on-path: " + ", ".join(row.condition_paths)
        return "on-path"
    if binding == "operator":
        return "manual"
    if binding == "task_kind":
        return "task-kind"
    if binding == "always":
        return "always"
    return binding


def _gate_cell(row: GateRowView, attr: str) -> str:
    """Value for one matrix column of a row (``activation`` is computed)."""
    if attr == "activation":
        return _activation_label(row)
    return getattr(row, attr)


def render_gate_matrix(
    gates: tuple[GateRowView, ...], *, color: bool | None = None,
) -> list[str]:
    """Render the aligned gate matrix as standalone lines (no ``gates`` label).

    The SINGLE matrix formatter, shared by the run-header banner and the
    ``orcho quality-gates`` command so neither reformats the matrix on its own.
    Takes ready :class:`GateRowView` rows (already carrying when / run_mode /
    policy / kind / activation from the ledger) and returns the column-aligned
    matrix lines by the :data:`_GATE_COLUMNS` contract: the first line is the
    (grey) column-name header, each following line is one gate. Columns are kept
    independent so command identity is never fused with its when/run/policy/kind;
    the trailing ``activation`` cell (``always`` / ``on-path: <globs>`` /
    ``manual``) is left unpadded so a long glob list does not distort the matrix.
    No colored data cell — the matrix stays restrained; policy colour lives on the
    banner's top ``effect`` line.

    Empty ``gates`` → ``[]`` (the caller decides how to render "no gates"; the
    banner shows ``gates  —``).
    """
    if not gates:
        return []

    header = [name for _, name in _GATE_COLUMNS]
    cells = [
        [_gate_cell(g, attr) for attr, _ in _GATE_COLUMNS] for g in gates
    ]
    widths = [
        max(len(header[i]), *(len(row[i]) for row in cells))
        for i in range(len(_GATE_COLUMNS))
    ]

    def _format(row: list[str]) -> str:
        parts = [
            value.ljust(widths[i]) if i < len(row) - 1 else value
            for i, value in enumerate(row)
        ]
        return "  ".join(parts)

    lines = [paint(_format(header), C.GREY, color=color)]
    lines.extend(_format(row) for row in cells)
    return lines


def _gate_matrix_lines(
    gates: tuple[GateRowView, ...], *, color: bool | None,
) -> list[str]:
    """Wrap :func:`render_gate_matrix` under the header's ``gates`` label.

    Empty matrix → a single ``gates  —`` line. Otherwise the ``gates`` label
    prefixes the shared matrix' column-name header and subsequent gate rows are
    indented to align beneath it. The alignment itself belongs to
    :func:`render_gate_matrix` (the reusable formatter); this only adds the
    banner-specific label/indent so the CLI can reuse the matrix without it.
    """
    label = paint(f"{'gates':<8}", C.GREY, color=color)
    matrix = render_gate_matrix(gates, color=color)
    if not matrix:
        return [f"  {label}  —"]

    lines = [f"  {label}  {matrix[0]}"]
    pad = " " * (8 + 4)  # align matrix rows under the header (2 + 8 label + 2)
    lines.extend(pad + row for row in matrix[1:])
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
    "render_gate_matrix",
    "render_verification_header",
]
