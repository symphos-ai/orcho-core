"""Deterministic scope-expansion sanction projection — isolated unit tests.

Covers the ADR 0112 §5 sanction matrix exposed by
:func:`pipeline.runtime.scope_expansion_sanction.decide`:

- the per-mode routing table ``fast`` / ``pro`` / ``governed`` ×
  ``notice`` / ``risk`` / ``blocker`` for a benign change (sanction + alert),
- genuine-safety → ``HALT_WAIVER`` in every mode (never auto-sanctioned),
- an active ``continue_with_waiver`` → ``AUTO_CONTINUE`` in every mode,
  including over a genuine-safety class (the single operator escape hatch),
- the knob being a *policy/projection* carrier whose outcome is computed by
  ``decide`` (it can express the whole matrix), not a baked outcome enum,
- the exhaustive import-time projection-table guard over ``OperatingMode``,
- input validation (fail-fast on bad mode / non-bool flags / unknown status),
- the bare-string status mirror staying byte-aligned with the engine enum, and
- the side-effect-free / engine-decoupled import boundary.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys

import pytest

from pipeline.engine.scope_expansion import ScopeExpansionStatus
from pipeline.runtime import scope_expansion_sanction as ses
from pipeline.runtime.roles import ScopeExpansionSanction
from pipeline.runtime.run_shape import OperatingMode, ScopeExpansionSanctionPolicy
from pipeline.runtime.scope_expansion_sanction import (
    STATUS_BLOCKER,
    STATUS_NOTICE,
    STATUS_RISK,
    ScopeExpansionDisposition,
    decide,
    project_scope_expansion_sanction,
)

# ── §5 per-mode routing matrix (benign change, no waiver) ────────────────────

# (operating_mode, status) → (expected sanction, expected alert), pinned
# literally so a drift in the implementation is caught here, not inferred.
_MATRIX: dict[
    tuple[str, str], tuple[ScopeExpansionSanction, bool]
] = {
    # fast: auto-sanction any benign status; surfaced as notice; no pause.
    ("fast", STATUS_NOTICE): (ScopeExpansionSanction.AUTO_CONTINUE, False),
    ("fast", STATUS_RISK): (ScopeExpansionSanction.AUTO_CONTINUE, False),
    ("fast", STATUS_BLOCKER): (ScopeExpansionSanction.AUTO_CONTINUE, False),
    # pro: notice auto; risk auto + alert; blocker → phase-handoff.
    ("pro", STATUS_NOTICE): (ScopeExpansionSanction.AUTO_CONTINUE, False),
    ("pro", STATUS_RISK): (ScopeExpansionSanction.AUTO_ALERT, True),
    ("pro", STATUS_BLOCKER): (ScopeExpansionSanction.HANDOFF, True),
    # governed: any participant-add / scope expansion → handoff + alert.
    ("governed", STATUS_NOTICE): (ScopeExpansionSanction.HANDOFF, True),
    ("governed", STATUS_RISK): (ScopeExpansionSanction.HANDOFF, True),
    ("governed", STATUS_BLOCKER): (ScopeExpansionSanction.HANDOFF, True),
}


@pytest.mark.parametrize(("mode_value", "status"), list(_MATRIX))
def test_decide_matrix_benign(mode_value: str, status: str) -> None:
    expected_sanction, expected_alert = _MATRIX[(mode_value, status)]
    result = decide(
        status=status,
        category_is_genuine_safety=False,
        operating_mode=OperatingMode(mode_value),
        has_active_waiver=False,
    )
    assert isinstance(result, ScopeExpansionDisposition)
    assert result.sanction is expected_sanction
    assert result.alert is expected_alert
    assert result.reason  # always a non-empty rationale


def test_decide_accepts_engine_status_enum_member() -> None:
    # ``decide`` accepts a ScopeExpansionStatus member directly (a StrEnum),
    # not only the bare value string, and routes it identically.
    via_enum = decide(
        status=ScopeExpansionStatus.BLOCKER,
        category_is_genuine_safety=False,
        operating_mode=OperatingMode.PRO,
        has_active_waiver=False,
    )
    via_str = decide(
        status=STATUS_BLOCKER,
        category_is_genuine_safety=False,
        operating_mode=OperatingMode.PRO,
        has_active_waiver=False,
    )
    assert via_enum.sanction is via_str.sanction is ScopeExpansionSanction.HANDOFF


# ── Genuine-safety stays hard in every mode ──────────────────────────────────


@pytest.mark.parametrize("mode_value", ["fast", "pro", "governed"])
@pytest.mark.parametrize("status", [STATUS_NOTICE, STATUS_RISK, STATUS_BLOCKER])
def test_genuine_safety_is_halt_waiver_in_every_mode(
    mode_value: str, status: str
) -> None:
    # security / persistence / destructive_delete → default halt + waiver, with
    # an alert, regardless of mode and regardless of the benign status. Never
    # silently auto-sanctioned, not even under fast.
    result = decide(
        status=status,
        category_is_genuine_safety=True,
        operating_mode=OperatingMode(mode_value),
        has_active_waiver=False,
    )
    assert result.sanction is ScopeExpansionSanction.HALT_WAIVER
    assert result.alert is True


# ── continue_with_waiver disarms the gate in every mode ──────────────────────


@pytest.mark.parametrize("mode_value", ["fast", "pro", "governed"])
@pytest.mark.parametrize("status", [STATUS_NOTICE, STATUS_RISK, STATUS_BLOCKER])
@pytest.mark.parametrize("genuine_safety", [False, True])
def test_active_waiver_auto_continues_in_every_mode(
    mode_value: str, status: str, genuine_safety: bool
) -> None:
    # The single operator escape hatch (ADR 0072/0073): an active
    # continue_with_waiver fully disarms the gate everywhere, even over a
    # genuine-safety class and even in governed.
    result = decide(
        status=status,
        category_is_genuine_safety=genuine_safety,
        operating_mode=OperatingMode(mode_value),
        has_active_waiver=True,
    )
    assert result.sanction is ScopeExpansionSanction.AUTO_CONTINUE
    assert result.alert is False
    assert "waiver" in result.reason.lower()


# ── The knob is a policy/projection, not a baked outcome ─────────────────────


def test_knob_is_a_policy_carrier_not_an_outcome_enum() -> None:
    # project_scope_expansion_sanction yields a posture carrier, NOT a
    # ScopeExpansionSanction. The same carrier (mode) drives every matrix cell
    # via decide — proving it can express status/category/waiver-dependent
    # outcomes rather than fixing one.
    for mode in OperatingMode:
        policy = project_scope_expansion_sanction(mode)
        assert isinstance(policy, ScopeExpansionSanctionPolicy)
        assert not isinstance(policy, ScopeExpansionSanction)
        assert policy.operating_mode is mode

    pro = project_scope_expansion_sanction(OperatingMode.PRO)
    # One carrier, three distinct outcomes depending on the input signals →
    # it is a policy, not a single stored verdict.
    outcomes = {
        decide(
            status=status,
            category_is_genuine_safety=False,
            operating_mode=pro.operating_mode,
            has_active_waiver=False,
        ).sanction
        for status in (STATUS_NOTICE, STATUS_RISK, STATUS_BLOCKER)
    }
    assert outcomes == {
        ScopeExpansionSanction.AUTO_CONTINUE,
        ScopeExpansionSanction.AUTO_ALERT,
        ScopeExpansionSanction.HANDOFF,
    }
    # And the same carrier still yields HALT_WAIVER for a genuine-safety class.
    assert (
        decide(
            status=STATUS_NOTICE,
            category_is_genuine_safety=True,
            operating_mode=pro.operating_mode,
            has_active_waiver=False,
        ).sanction
        is ScopeExpansionSanction.HALT_WAIVER
    )


def test_projection_is_pure_and_idempotent() -> None:
    for mode in OperatingMode:
        first = project_scope_expansion_sanction(mode)
        second = project_scope_expansion_sanction(mode)
        assert first is second  # cached table entry, holds no state


# ── Import-time exhaustiveness guard over OperatingMode ──────────────────────


def test_projection_table_covers_every_operating_mode() -> None:
    # The pinned table mirrors the closed enum exactly; every mode resolves.
    assert set(ses._SANCTION_POLICY_BY_MODE) == set(OperatingMode)
    for mode in OperatingMode:
        assert project_scope_expansion_sanction(mode).operating_mode is mode


def test_guard_condition_fires_on_an_incomplete_table() -> None:
    # The import-time guard is ``if set(OperatingMode) - set(table): raise``.
    # An incomplete table makes that difference non-empty → it would raise.
    incomplete = {OperatingMode.FAST: ses._SANCTION_POLICY_BY_MODE[OperatingMode.FAST]}
    missing = set(OperatingMode) - set(incomplete)
    assert missing  # non-empty → the loud import-time guard would trigger
    assert OperatingMode.PRO in missing and OperatingMode.GOVERNED in missing


# ── Status mirror stays aligned with the engine enum (no silent drift) ───────


def test_status_constants_match_engine_enum_values() -> None:
    # The bare-string mirror in the runtime module must equal the engine's
    # durable ScopeExpansionStatus values, so decoupling from the engine import
    # never silently diverges the vocabulary.
    assert ScopeExpansionStatus.NOTICE.value == STATUS_NOTICE
    assert ScopeExpansionStatus.RISK.value == STATUS_RISK
    assert ScopeExpansionStatus.BLOCKER.value == STATUS_BLOCKER
    assert {s.value for s in ScopeExpansionStatus} == ses._KNOWN_STATUSES


# ── Input validation (fail-fast) ─────────────────────────────────────────────


def test_decide_rejects_non_operating_mode() -> None:
    with pytest.raises(TypeError):
        decide(
            status=STATUS_NOTICE,
            category_is_genuine_safety=False,
            operating_mode="fast",  # type: ignore[arg-type]
            has_active_waiver=False,
        )


def test_decide_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        decide(
            status="scope_expansion_unknown",
            category_is_genuine_safety=False,
            operating_mode=OperatingMode.PRO,
            has_active_waiver=False,
        )


@pytest.mark.parametrize("bad_field", ["category_is_genuine_safety", "has_active_waiver"])
def test_decide_rejects_non_bool_flags(bad_field: str) -> None:
    kwargs = dict(
        status=STATUS_NOTICE,
        category_is_genuine_safety=False,
        operating_mode=OperatingMode.PRO,
        has_active_waiver=False,
    )
    kwargs[bad_field] = "yes"  # type: ignore[assignment]
    with pytest.raises(TypeError):
        decide(**kwargs)  # type: ignore[arg-type]


# ── Engine-decoupled module (no wrong-direction import) ──────────────────────


def test_module_does_not_import_the_engine_classifier() -> None:
    # The inert runtime sanction module must not import the engine package
    # (which would pull agents/core and risk an import cycle). Asserted on the
    # module's actual import statements (via AST, so docstring mentions of the
    # classifier do not trip it) — transitive pulls via pipeline.runtime cannot
    # mask a real engine import here.
    tree = ast.parse(inspect.getsource(ses))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any(m.startswith("pipeline.engine") for m in imported_modules), (
        f"runtime sanction module imports the engine: {sorted(imported_modules)}"
    )


# ── Side-effect-free import boundary (clean subprocess) ──────────────────────

# Mirror of the run_shape / semantic_mode_defaults guard: trace open() and
# Path.read_text BEFORE importing the module; the guard fails if the shipped
# profile JSON is touched or the profile loader leaks into sys.modules.
_GUARD_SOURCE = r"""
import builtins
import pathlib
import sys

_FORBIDDEN = ("pipeline_profiles_v2.json", "pipeline_profiles")


def _check(path):
    text = str(path)
    if any(token in text for token in _FORBIDDEN):
        raise AssertionError("forbidden profile-config path opened: " + text)


_real_open = builtins.open


def _traced_open(file, *args, **kwargs):
    _check(file)
    return _real_open(file, *args, **kwargs)


builtins.open = _traced_open

_real_read_text = pathlib.Path.read_text


def _traced_read_text(self, *args, **kwargs):
    _check(self)
    return _real_read_text(self, *args, **kwargs)


pathlib.Path.read_text = _traced_read_text

import pipeline.runtime.scope_expansion_sanction as ses  # noqa: F401

assert "pipeline.profiles.loader" not in sys.modules, "loader leaked into import"

from pipeline.runtime.run_shape import OperatingMode

# Projection + decision stay inert and callable.
assert ses.project_scope_expansion_sanction(OperatingMode.FAST).operating_mode \
    is OperatingMode.FAST
assert ses.decide(
    status=ses.STATUS_BLOCKER,
    category_is_genuine_safety=False,
    operating_mode=OperatingMode.GOVERNED,
    has_active_waiver=False,
).sanction.value == "handoff"

print("OK")
"""


def test_import_is_side_effect_free_in_clean_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _GUARD_SOURCE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"guard subprocess failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout, (
        f"guard subprocess did not print OK\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
