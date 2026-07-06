"""Unit tests for core/io/verification_header.py.

Covers the operator-facing verification run-header block: the view builder
(contract -> primitive DTO, schedule -> gate matrix with orthogonal columns)
and the renderer (structured gate matrix + compact ``gates=N`` line, no
schedule jargon leak, restrained color).
"""

from core.io.ansi import strip_ansi
from core.io.verification_header import (
    GateRowView,
    VerificationHeaderView,
    build_verification_header_view,
    render_gate_matrix,
    render_verification_header,
)
from pipeline.plugins import PluginConfig
from pipeline.verification_contract import VerificationContract


def _strip(text: str) -> str:
    return strip_ansi(text)


def _contract(**verification) -> VerificationContract:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            work_mode="pro",
            verification_envs={"ci": {"image": "python:3.12"}},
            verification=verification,
        ),
    )
    assert contract is not None
    return contract


def _gate(view: VerificationHeaderView, name: str) -> GateRowView:
    """Return the single matrix row for command ``name`` (asserts existence)."""
    matches = [g for g in view.gates if g.gate == name]
    assert matches, f"no gate row for {name!r} in {view.gates!r}"
    assert len(matches) == 1, f"expected one row for {name!r}, got {matches!r}"
    return matches[0]


# ── build_verification_header_view ─────────────────────────────────────


def test_none_contract_returns_none() -> None:
    assert build_verification_header_view(None) is None


def test_simple_contract_one_env_two_commands() -> None:
    contract = _contract(
        commands={"lint": "ruff check .", "test": "pytest -q"},
    )
    view = build_verification_header_view(contract)

    assert view is not None
    assert view.mode == "pro"
    assert view.envs == ("ci",)
    # No schedule -> the gate matrix is empty (only scheduled gates are rows).
    assert view.gates == ()

    out = _strip(render_verification_header(view, compact=False))
    assert "mode" in out
    assert "envs" in out


def test_auto_derived_warn_policy_and_effect() -> None:
    # One scheduled gate defers its policy (derived), another declares warn.
    contract = _contract(
        commands={"lint": "ruff check .", "test": "pytest -q"},
        schedule=[
            {"after_phase": "implement", "commands": ["lint"]},
            {"before_delivery": True, "policy": "warn", "commands": ["test"]},
        ],
    )
    view = build_verification_header_view(contract)

    assert view is not None
    assert view.policy_source == "auto-derived from mode/plugin defaults"
    assert view.effect == "warn on missing/failed receipts"
    assert "receipts" in view.effect
    assert view.warned is True


def test_mode_defaults_when_work_mode_unset() -> None:
    contract = VerificationContract.from_plugin(
        PluginConfig(
            verification_envs={"ci": {"image": "python:3.12"}},
            verification={"commands": {"lint": "ruff check ."}},
        ),
    )
    assert contract is not None
    view = build_verification_header_view(contract)
    assert view is not None
    assert view.mode == "default"


def test_explicit_only_policies_named_in_source() -> None:
    contract = _contract(
        commands={"lint": "ruff check ."},
        schedule=[
            {"after_phase": "implement", "policy": "warn", "commands": ["lint"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    assert "declared in contract" in view.policy_source
    assert "warn" in view.policy_source
    assert view.warned is True


def test_no_schedule_reports_no_scheduled_gates() -> None:
    contract = _contract(commands={"lint": "ruff check ."})
    view = build_verification_header_view(contract)
    assert view is not None
    assert view.policy_source == "no scheduled gates"
    # No declared policy -> effect stays honest, not an inferred consequence.
    assert view.effect == "receipts policy auto-derived from mode/plugin defaults"
    assert "receipts" in view.effect
    assert view.warned is False


def test_derived_only_schedule_effect_mentions_receipts() -> None:
    contract = _contract(
        commands={"lint": "ruff check ."},
        schedule=[
            {"after_phase": "implement", "commands": ["lint"]},
        ],
    )
    view = build_verification_header_view(contract)

    assert view is not None
    assert view.policy_source == "auto-derived from mode/plugin defaults"
    assert view.effect == "receipts policy auto-derived from mode/plugin defaults"
    assert "receipts" in view.effect
    assert view.warned is False


# ── gate matrix builder ────────────────────────────────────────────────


def test_gate_row_carries_orthogonal_columns() -> None:
    contract = _contract(
        commands={"lint": {"run": "ruff check .", "cheap": True}},
        schedule=[
            {"after_phase": "implement", "policy": "warn", "commands": ["lint"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    row = _gate(view, "lint")
    # Command identity is kept distinct from each orthogonal property.
    assert row.gate == "lint"
    assert row.timing == "after_implement"
    assert row.run_mode == "auto"
    assert row.policy == "warn"
    assert row.kind == "cheap"


def test_manual_only_gate_visible_as_manual_operator() -> None:
    # An e2e gate listed directly on a manual_only entry must read as an
    # operator/manual gate, never as an ordinary auto gate.
    contract = _contract(
        commands={"e2e": {"run": "pytest -m e2e"}},
        schedule=[
            {"manual_only": True, "policy": "require", "commands": ["e2e"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    row = _gate(view, "e2e")
    assert row.timing == "operator"
    assert row.run_mode == "manual"
    assert row.policy == "require"


def test_manual_only_gate_via_gate_sets_with_empty_commands() -> None:
    # Review F1: the operator e2e gate is declared with an EMPTY entry.commands
    # and its commands come ONLY through entry.gate_sets. Without expanding
    # gate_sets the gate would vanish entirely.
    contract = _contract(
        commands={"e2e": {"run": "pytest -m e2e"}},
        gate_sets={
            "manuals": {"commands": ["e2e"], "default_cheap": False},
        },
        schedule=[
            {"manual_only": True, "gate_sets": ["manuals"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    row = _gate(view, "e2e")
    assert row.timing == "operator"
    assert row.run_mode == "manual"
    # default_cheap=False is non-cheap with no declared taxonomy -> unknown.
    assert row.kind == "unknown"


def test_gate_set_default_policy_fills_unknown_entry_policy() -> None:
    contract = _contract(
        commands={"e2e": {"run": "pytest -m e2e"}},
        gate_sets={
            "manuals": {"commands": ["e2e"], "default_policy": "require"},
        },
        schedule=[
            {"manual_only": True, "gate_sets": ["manuals"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    # entry.policy is None -> fall back to the gate set's default_policy.
    assert _gate(view, "e2e").policy == "require"


def test_gate_set_default_policy_drives_top_summary() -> None:
    # F1: a gate whose policy is declared on its gate_set (entry.policy is None)
    # must drive the top policy_source/effect/warned, not just the matrix row —
    # the summary must never contradict a row it sits above.
    contract = _contract(
        commands={"e2e": {"run": "pytest -m e2e"}},
        gate_sets={
            "manuals": {"commands": ["e2e"], "default_policy": "require"},
        },
        schedule=[
            {"manual_only": True, "gate_sets": ["manuals"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    # Matrix row already shows require...
    assert _gate(view, "e2e").policy == "require"
    # ...and the top summary agrees: declared (not auto-derived), warned.
    assert "declared in contract" in view.policy_source
    assert "require" in view.policy_source
    assert view.effect == "require receipts; missing/failed resolved at gate time"
    assert view.warned is True


def test_mixed_source_command_keeps_gate_set_defaults() -> None:
    # F1: a command listed BOTH directly (entry.commands) and via entry.gate_sets
    # for the same hook/phase must not let the bare direct row shadow the gate
    # set's declared defaults — policy/kind come from the gate set, not unknown.
    contract = _contract(
        commands={"e2e": {"run": "pytest -m e2e"}},
        gate_sets={
            "manuals": {
                "commands": ["e2e"],
                "default_policy": "require",
                "default_cheap": True,
            },
        },
        schedule=[
            {"manual_only": True, "commands": ["e2e"], "gate_sets": ["manuals"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    # Exactly one row (deduped) carrying the gate set's declared metadata.
    e2e_rows = [g for g in view.gates if g.gate == "e2e"]
    assert len(e2e_rows) == 1
    row = e2e_rows[0]
    assert row.timing == "operator"
    assert row.run_mode == "manual"
    assert row.policy == "require"
    assert row.kind == "cheap"
    # ...and the declared data drives the summary, not auto-derived.
    assert "declared in contract" in view.policy_source
    assert "auto-derived" not in view.policy_source
    assert view.effect == "require receipts; missing/failed resolved at gate time"
    assert view.warned is True


def test_multiple_gate_sets_pick_strictest_policy_and_or_cheap() -> None:
    # F1: one command backed by two gate_sets with differing defaults must take
    # the strictest declared policy and OR the cheap flag, mirroring
    # verification_selection._merge_defaults — never the first/laxer source.
    contract = _contract(
        commands={"e2e": {"run": "pytest -m e2e"}},
        gate_sets={
            "baseline": {
                "commands": ["e2e"],
                "default_policy": "suggest",
                "default_cheap": False,
            },
            "strict": {
                "commands": ["e2e"],
                "default_policy": "require",
                "default_cheap": True,
            },
        },
        schedule=[
            {"manual_only": True, "gate_sets": ["baseline", "strict"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    row = _gate(view, "e2e")
    assert row.policy == "require"  # strictest, not the first 'suggest'
    assert row.kind == "cheap"  # OR-ed across gate sets, not the first False
    # Summary reflects the strictest declared policy too.
    assert "require" in view.policy_source
    assert view.effect == "require receipts; missing/failed resolved at gate time"
    assert view.warned is True


def test_gate_set_default_policy_warn_warns_in_summary() -> None:
    # Same invariant for a warn gate set: the row's warn must surface as warned.
    contract = _contract(
        commands={"e2e": {"run": "pytest -m e2e"}},
        gate_sets={
            "manuals": {"commands": ["e2e"], "default_policy": "warn"},
        },
        schedule=[
            {"manual_only": True, "gate_sets": ["manuals"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    assert _gate(view, "e2e").policy == "warn"
    assert "declared in contract" in view.policy_source
    assert view.effect == "warn on missing/failed receipts"
    assert view.warned is True


def test_unavailable_properties_render_unknown() -> None:
    # No entry policy, no gate-set default, no declared cheap -> both honest.
    contract = _contract(
        commands={"lint": "ruff check ."},
        schedule=[
            {"after_phase": "implement", "commands": ["lint"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    row = _gate(view, "lint")
    assert row.policy == "unknown"
    assert row.kind == "unknown"


def _reference_contract() -> VerificationContract:
    """A gate-set/selection/schedule contract shaped like the repo's own plugin.

    baseline/broad are ``always``; run-state/verification/cli-sdk are path-gated;
    e2e is operator/manual — the mix that exercises the activation column.
    """
    return _contract(
        commands={
            "env-provenance": {"run": "prov", "cheap": True},
            "lint": {"run": "ruff check .", "cheap": True},
            "run-state-unit": {"run": "pytest run_state"},
            "verification-unit": {"run": "pytest verification"},
            "cli-sdk-unit": {"run": "pytest cli sdk"},
            "broad-non-e2e": {"run": "pytest -m 'not e2e'"},
            "e2e": {"run": "pytest -m e2e"},
        },
        gate_sets={
            "baseline": {
                "commands": ["env-provenance", "lint"],
                "default_policy": "warn",
            },
            "run-state": {"commands": ["run-state-unit"], "default_policy": "require"},
            "verification": {
                "commands": ["verification-unit"], "default_policy": "require",
            },
            "cli-sdk": {"commands": ["cli-sdk-unit"], "default_policy": "require"},
            "broad": {"commands": ["broad-non-e2e"], "default_policy": "require"},
            "e2e": {"commands": ["e2e"], "default_policy": "suggest"},
        },
        selection=[
            {"always": ["baseline", "broad"]},
            {"paths": ["pipeline/run_state/**"], "include": ["run-state"]},
            {"paths": ["pipeline/verification*.py"], "include": ["verification"]},
            {"paths": ["cli/**", "sdk/**"], "include": ["cli-sdk"]},
            {"operator": ["e2e"]},
        ],
        schedule=[
            {"after_phase": "implement", "gate_sets": ["baseline"], "policy": "warn"},
            {
                "after_phase": "implement",
                "gate_sets": ["run-state", "verification", "cli-sdk"],
                "policy": "require",
            },
            {"after_phase": "implement", "gate_sets": ["broad"], "policy": "require"},
            {"manual_only": True, "gate_sets": ["e2e"], "policy": "suggest"},
        ],
    )


def test_activation_condition_at_start_no_diff() -> None:
    # At run start (build_verification_header_view reads the ledger with
    # changed_files=None), each gate carries its declared activation condition:
    # always for baseline/broad, on_path (+globs) for the subsystem gates, and
    # operator for e2e. No path-gated gate is shown as an unconditional require.
    view = build_verification_header_view(_reference_contract())
    assert view is not None

    for name in ("env-provenance", "lint", "broad-non-e2e"):
        assert _gate(view, name).condition == "always", name
        assert _gate(view, name).condition_paths == ()

    verification_row = _gate(view, "verification-unit")
    assert verification_row.condition == "on_path"
    assert verification_row.condition_paths == ("pipeline/verification*.py",)
    assert _gate(view, "run-state-unit").condition == "on_path"
    assert _gate(view, "cli-sdk-unit").condition == "on_path"

    assert _gate(view, "e2e").condition == "operator"

    # Timing multiplicity is unchanged: the six auto gates all read
    # after_implement, e2e reads operator.
    timings = sorted((g.gate, g.timing) for g in view.gates)
    assert timings == [
        ("broad-non-e2e", "after_implement"),
        ("cli-sdk-unit", "after_implement"),
        ("e2e", "operator"),
        ("env-provenance", "after_implement"),
        ("lint", "after_implement"),
        ("run-state-unit", "after_implement"),
        ("verification-unit", "after_implement"),
    ]


def test_activation_rendered_as_on_path_manual_always() -> None:
    # The rendered matrix shows the activation column: on-path gates print their
    # globs, e2e prints manual, baseline/broad print always — and no path-gated
    # gate is rendered as an unconditional require cell.
    view = build_verification_header_view(_reference_contract())
    assert view is not None
    out = _strip(render_verification_header(view, compact=False))
    assert "activation" in out

    verification_line = next(
        ln for ln in out.splitlines() if "verification-unit" in ln
    )
    # The activation cell is the trailing column and shows the globs; the gate's
    # ``require`` now lives in its own restored ``policy`` column, not the
    # activation cell — so the line ENDS with the on-path activation.
    assert verification_line.rstrip().endswith("on-path: pipeline/verification*.py")

    lint_line = next(ln for ln in out.splitlines() if ln.strip().startswith("lint"))
    assert lint_line.rstrip().endswith("always")

    e2e_line = next(ln for ln in out.splitlines() if ln.strip().startswith("e2e "))
    assert e2e_line.rstrip().endswith("manual")


def test_gate_identity_separates_same_command_across_hooks() -> None:
    # The same command scheduled under two distinct hooks yields two rows; the
    # gate is never collapsed into one flat bucket that fuses identity+timing.
    contract = _contract(
        commands={"lint": "ruff check ."},
        schedule=[
            {"after_phase": "implement", "commands": ["lint"]},
            {"before_delivery": True, "policy": "warn", "commands": ["lint"]},
        ],
    )
    view = build_verification_header_view(contract)
    assert view is not None
    timings = sorted(g.timing for g in view.gates if g.gate == "lint")
    assert timings == ["after_implement", "delivery"]


# ── when axis: derived from has_final_phase ────────────────────────────


def test_when_require_is_timing_hook_warn_is_pre_final_with_final_phase() -> None:
    # A profile WITH a final delivery phase: a required gate reads its timing hook
    # (after_implement), a warn gate defers to pre-final, e2e is operator.
    view = build_verification_header_view(
        _reference_contract(), has_final_phase=True,
    )
    assert view is not None
    assert _gate(view, "verification-unit").when == "after_implement"
    assert _gate(view, "broad-non-e2e").when == "after_implement"
    assert _gate(view, "lint").when == "pre-final"
    assert _gate(view, "env-provenance").when == "pre-final"
    assert _gate(view, "e2e").when == "operator"


def test_when_warn_is_not_auto_run_without_final_phase() -> None:
    # A fast / small_task-style profile with NO final phase: the warn gates are
    # honestly 'not auto-run', while the required gates still read their hook.
    view = build_verification_header_view(
        _reference_contract(), has_final_phase=False,
    )
    assert view is not None
    assert _gate(view, "lint").when == "not auto-run"
    assert _gate(view, "env-provenance").when == "not auto-run"
    assert _gate(view, "verification-unit").when == "after_implement"
    assert _gate(view, "e2e").when == "operator"


def test_when_profile_dependent_when_has_final_phase_unknown() -> None:
    # Default (no has_final_phase): a warn gate is marked profile-dependent, not
    # guessed; required gates are unaffected.
    view = build_verification_header_view(_reference_contract())
    assert view is not None
    assert _gate(view, "lint").when == "profile-dependent"
    assert _gate(view, "verification-unit").when == "after_implement"


def test_when_rendered_in_matrix_distinguishes_require_from_warn() -> None:
    # The rendered matrix carries the when column so require->after_implement is
    # legibly distinct from warn->pre-final on the row itself.
    view = build_verification_header_view(
        _reference_contract(), has_final_phase=True,
    )
    assert view is not None
    out = _strip(render_verification_header(view, compact=False))
    assert "when" in out
    ver_line = next(ln for ln in out.splitlines() if "verification-unit" in ln)
    assert "after_implement" in ver_line
    lint_line = next(ln for ln in out.splitlines() if ln.strip().startswith("lint"))
    assert "pre-final" in lint_line


# ── render_gate_matrix: the shared, reusable formatter ─────────────────


def test_render_gate_matrix_column_contract_and_reuse() -> None:
    # render_gate_matrix is the single formatter the banner also uses: its header
    # names the new column order and the banner reuses exactly its rows.
    view = build_verification_header_view(
        _reference_contract(), has_final_phase=True,
    )
    assert view is not None
    matrix = render_gate_matrix(view.gates)
    # Header row + one row per gate.
    assert len(matrix) == len(view.gates) + 1
    header = strip_ansi(matrix[0]).split()
    assert header == ["gate", "when", "run", "policy", "kind", "activation"]
    # The banner renders through the same helper: every matrix data row appears
    # verbatim inside the banner output (indented under the ``gates`` label).
    banner = _strip(render_verification_header(view, compact=False))
    for data_row in matrix[1:]:
        assert data_row in banner


def test_render_gate_matrix_empty_is_empty_list() -> None:
    # Empty gates -> the shared helper returns [] and leaves the "no gates"
    # rendering to the caller (the banner shows ``gates  —``).
    assert render_gate_matrix(()) == []


# ── render_verification_header ─────────────────────────────────────────


def _warn_view() -> VerificationHeaderView:
    return VerificationHeaderView(
        mode="pro",
        envs=("mcp-local-core",),
        gates=(
            GateRowView(
                gate="lint",
                timing="after_implement",
                run_mode="auto",
                policy="warn",
                kind="cheap",
                condition="always",
                when="pre-final",
            ),
            GateRowView(
                gate="e2e",
                timing="operator",
                run_mode="manual",
                policy="require",
                kind="unknown",
                condition="operator",
                when="operator",
            ),
        ),
        policy_source="auto-derived from mode/plugin defaults",
        effect="warn on missing/failed receipts",
        warned=True,
    )


def test_structured_block_has_all_dimension_labels() -> None:
    out = _strip(render_verification_header(_warn_view(), compact=False))
    lines = out.splitlines()
    assert lines[0] == "Verification"
    for label in ("mode", "envs", "policy", "effect", "gates"):
        assert any(line.strip().startswith(label) for line in lines[1:]), label
    assert "auto-derived from mode/plugin defaults" in out
    assert "warn on missing/failed receipts" in out
    assert "receipts" in out


def test_structured_matrix_has_separate_columns_per_gate() -> None:
    out = _strip(render_verification_header(_warn_view(), compact=False))
    # The matrix header names the orthogonal columns: ``when`` supersedes the raw
    # timing display, ``policy`` is restored as its own column, and ``activation``
    # is the trailing column.
    assert "when" in out
    assert "run" in out
    assert "policy" in out
    assert "kind" in out
    assert "activation" in out
    assert "timing" not in out  # the raw timing column is gone
    # Each gate row keeps command identity beside its own property cells, on a
    # single line — not a flat comma bucket fusing identity with properties.
    e2e_line = next(ln for ln in out.splitlines() if "e2e" in ln)
    assert "operator" in e2e_line  # its when stage
    assert "unknown" in e2e_line   # its kind
    assert "require" in e2e_line   # its restored policy column
    # The operator gate's activation cell is the trailing column and reads
    # ``manual``.
    assert e2e_line.rstrip().endswith("manual")
    # The legacy flat command bucket and its label are gone.
    assert "lint, e2e" not in out
    assert "commands" not in out


def test_compact_form_is_single_line_with_gates_count() -> None:
    out = _strip(render_verification_header(_warn_view(), compact=True))
    assert "\n" not in out
    assert "·" in out
    assert "mode=pro" in out
    assert "policy=auto-derived from mode/plugin defaults" in out
    assert "effect=warn on missing/failed receipts" in out
    # Matrix is summarised, not expanded; no flat command bucket.
    assert "gates=2" in out
    assert "commands=" not in out


def test_render_has_no_schedule_jargon_or_legacy_shape() -> None:
    for compact in (True, False):
        out = _strip(render_verification_header(_warn_view(), compact=compact))
        assert "schedule" not in out
        assert "derived from" in out  # operator phrasing present...
        assert "schedule=" not in out  # ...but no raw schedule token
        assert "Verification contract: work_mode=" not in out


def test_effect_value_painted_only_when_warned() -> None:
    warned = render_verification_header(_warn_view(), compact=False, color=True)
    assert "\033[93m" in warned  # YELLOW on the warn effect / warn-policy cell

    calm = VerificationHeaderView(
        mode="pro",
        envs=("ci",),
        gates=(
            GateRowView(
                gate="lint",
                timing="after_implement",
                run_mode="auto",
                policy="suggest",
                kind="unknown",
            ),
        ),
        policy_source="no scheduled gates",
        effect="receipts policy auto-derived from mode/plugin defaults",
        warned=False,
    )
    calm_out = render_verification_header(calm, compact=False, color=True)
    assert "\033[93m" not in calm_out  # no warning color
    assert "\033[92m" not in calm_out  # and never all-green


def test_empty_matrix_renders_dash_not_flat_bucket() -> None:
    view = VerificationHeaderView(
        mode="pro",
        envs=("ci",),
        gates=(),
        policy_source="no scheduled gates",
        effect="receipts policy auto-derived from mode/plugin defaults",
        warned=False,
    )
    out = _strip(render_verification_header(view, compact=False))
    gates_line = next(ln for ln in out.splitlines() if ln.strip().startswith("gates"))
    assert "—" in gates_line
