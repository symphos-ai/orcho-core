"""
pipeline/engine/scope_expansion.py — deterministic out-of-plan scope classifier.

Focused, **pure** home (no git / filesystem / LLM I/O) for classifying files
that a run changed *outside* its declared plan scope into one of three durable
statuses — ``notice`` / ``risk`` / ``blocker`` — each carrying per-file
evidence. It sits beside the other engine value-object modules
(``companion_scope.py`` / ``delivery_scope.py``) and owns the **new**
responsibility the architecture-fitness gate forbids piling onto the already
large ``final_acceptance`` / ``finalization`` bodies: turning observed durable
signals about an out-of-plan file into a typed, JSON-safe *status fact*. The
*verdict / route* for that status (continue / alert / phase-handoff / halt) is
not decided here — it is the OperatingMode sanction projection's job (ADR 0112
§5, :mod:`pipeline.runtime.scope_expansion_sanction`); this module stays a pure
classifier with no status→verdict coupling.

The split mirrors the neighbouring scope modules:

**(A) Typed results.** :class:`ScopeExpansionStatus`, :class:`ScopeExpansionItem`
(one classified file), :class:`ScopeExpansionAssessment` (the whole set with
``notices`` / ``risks`` / ``blockers`` projections and ``has_blocker``), and the
neutral per-file signal record :class:`FileScopeSignals`.

**(B) Pure derivation.** :func:`derive_in_plan_patterns` is a compatibility
projection over :mod:`pipeline.engine.declared_write_scope`, which owns durable
scope normalisation, provenance, and matching semantics.

**(C) Pure signal building.** :func:`categorize_file` maps a path to a stable
category; :func:`build_scope_expansion_signals` assembles one
:class:`FileScopeSignals` per out-of-plan file from named durable artefacts.
Every absent signal is treated *conservatively* — an unobserved gate is
``verified=False``, a missing explanation is ``has_explanation=False`` — so a
gap never silently upgrades a file toward ``notice``.

**(D) Pure classification + render.** :func:`classify_file_signals` applies the
deterministic status matrix, :func:`build_scope_expansion_assessment` maps it
across every signal, and :func:`render_scope_expansion_lines` renders a compact
human-readable summary from the JSON-safe ``to_dict()`` projection.

``verified`` is **per-file / per-category** (a category is bound to its relevant
gate), never one coarse global ``gates_green`` flag: a ``notice`` without a
green gate for its own category is downgraded to at least ``risk``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pipeline.engine.declared_write_scope import (
    path_matches_declared_scope,
    resolve_declared_write_scope,
)

# ── category constants ───────────────────────────────────────────────────────

CATEGORY_BUILD = "build"
CATEGORY_FIXTURE = "fixture_snapshot"
CATEGORY_SCHEMA = "generated_schema"
CATEGORY_IMPORT_WIRING = "import_wiring"
CATEGORY_PROJECT_CONFIG = "project_config"
CATEGORY_TEST = "test"
CATEGORY_PUBLIC_WIRE = "public_wire"
CATEGORY_PERSISTENCE = "persistence"
CATEGORY_SECURITY = "security"
CATEGORY_OTHER = "other"

# Benign categories may reach ``notice`` when verified + explained. The
# sensitive categories below never do — they are blocker-leaning by default.
_BENIGN_CATEGORIES = frozenset(
    {
        CATEGORY_BUILD,
        CATEGORY_FIXTURE,
        CATEGORY_SCHEMA,
        CATEGORY_IMPORT_WIRING,
        CATEGORY_PROJECT_CONFIG,
        CATEGORY_TEST,
    }
)
_SDK_RECONCILABLE_CATEGORIES = frozenset({CATEGORY_SCHEMA, CATEGORY_PUBLIC_WIRE})

# A diff at or above this many changed lines (added + removed) is "large".
LARGE_DIFF_LINES = 200

_GREEN = "green"

# ── test-module detection (language-agnostic) ────────────────────────────────
# A test edit never changes the product surface, so it is never a genuine-safety
# (persistence / security / public-wire) scope expansion — even when the name
# carries a sensitive token (``test_run_state.py``, ``auth.service.test.ts``,
# ``secret_store_test.go``). Detected two ways, both on the ORIGINAL-case path so
# CamelCase conventions (``FooTest.java``) survive lowercasing.
#
# 1. Naming conventions across ecosystems (basename-only, so test *data* keeps
#    its content category):
_TEST_NAME_RE = re.compile(
    r"""^(?:
        test_.+\.py                                          # pytest / unittest
      | .+_test\.(?:py|go|rb|exs?|cc|cpp|cxx|cs|c)           # go / ruby / elixir / c(++) / python-alt
      | test_.+\.(?:cc|cpp|cxx|c)                            # c(++) prefix style
      | .+[._](?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)       # jest / vitest / mocha / jasmine
      | .+_spec\.rb                                          # rspec
      | .+(?:Test|Tests|Spec|IT)\.(?:java|kt|kts|scala|cs|swift|php)  # jvm / .net / swift / php
    )$""",
    re.VERBOSE,
)
# 2. Location: a source file under a test directory (catches ecosystems with no
#    name convention, e.g. Rust ``tests/foo.rs``). Excludes data/fixture/golden
#    dirs so goldens and test data keep their content category.
_TEST_DIR_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec)/")
_TEST_DATA_DIR_RE = re.compile(r"(?:^|/)(?:fixtures?|golden|goldens|snapshots?|testdata|data)/")
_TEST_SOURCE_EXTS = (
    ".py", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rb", ".java", ".kt", ".kts", ".scala", ".cs", ".swift", ".php",
    ".ex", ".exs", ".cc", ".cpp", ".cxx", ".c", ".h", ".hpp", ".m", ".mm",
)


def _is_test_module(path: str, lowered: str) -> bool:
    """True when ``path`` is a test source file in any common ecosystem (pure).

    Uses the original-case basename for naming conventions (CamelCase
    ``FooTest.java``) and the lowered path for the directory heuristic. Never
    matches test *data* / fixtures / goldens — those keep their content
    category.
    """
    if _TEST_NAME_RE.match(_basename(path)):
        return True
    return (
        _TEST_DIR_RE.search(lowered) is not None
        and _TEST_DATA_DIR_RE.search(lowered) is None
        and lowered.endswith(_TEST_SOURCE_EXTS)
    )


# ── (A) typed results ────────────────────────────────────────────────────────


class ScopeExpansionStatus(StrEnum):
    """Typed durable status of one out-of-plan file — a pure *fact*, not a verdict.

    Seam (ADR 0112 §5): this status records *what happened* to an out-of-plan
    file; it does not decide *what to do about it*. The route a status takes
    (continue / alert / phase-handoff / halt) is the consumer's concern,
    computed by the OperatingMode sanction projection
    (:func:`pipeline.runtime.scope_expansion_sanction.decide`) — never here.
    In particular ``BLOCKER`` is the strongest *fact*, and ``has_blocker`` is a
    fact, not a verdict: this classifier owns no status→verdict coupling and
    names no release outcome.

    - ``NOTICE`` — a benign, verified, explained companion change (e.g. a build
      lockfile or regenerated snapshot).
    - ``RISK`` — an out-of-plan change that lacks a green relevant gate, an
      explanation, or otherwise cannot be cleared to ``notice`` but is not a
      hard blocker.
    - ``BLOCKER`` — the strongest out-of-plan fact: an unaligned public
      wire/schema change, persistence/state, security/secret, a destructive
      delete, a large diff, or a change repeated across corrections.
    """

    NOTICE = "scope_expansion_notice"
    RISK = "scope_expansion_risk"
    BLOCKER = "scope_expansion_blocker"


@dataclass(frozen=True, slots=True)
class FileScopeSignals:
    """Neutral, pure per-file signal record — the input to classification.

    Every field is a plain, durably-derivable fact about one out-of-plan file.
    A signal that could not be observed is set conservatively (``verified`` and
    the explanatory/safety flags default to ``False``) so a missing artefact
    never upgrades a file toward ``notice``.
    """

    path: str
    category: str
    verified: bool = False
    has_explanation: bool = False
    large_diff: bool = False
    destructive_delete: bool = False
    repeated_across_corrections: bool = False
    paired_alignment: bool = False
    is_public_wire: bool = False
    is_persistence: bool = False
    is_security: bool = False
    sdk_already_public: bool = False
    sdk_no_new_exports: bool = False
    sdk_restores_invariant: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Durable, JSON-safe view of the raw signals."""
        return {
            "path": self.path,
            "category": self.category,
            "verified": self.verified,
            "has_explanation": self.has_explanation,
            "large_diff": self.large_diff,
            "destructive_delete": self.destructive_delete,
            "repeated_across_corrections": self.repeated_across_corrections,
            "paired_alignment": self.paired_alignment,
            "is_public_wire": self.is_public_wire,
            "is_persistence": self.is_persistence,
            "is_security": self.is_security,
            "sdk_already_public": self.sdk_already_public,
            "sdk_no_new_exports": self.sdk_no_new_exports,
            "sdk_restores_invariant": self.sdk_restores_invariant,
        }


@dataclass(frozen=True, slots=True)
class ScopeExpansionItem:
    """One classified out-of-plan file with its status and evidence."""

    path: str
    category: str
    status: ScopeExpansionStatus
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Durable, JSON-safe view (enum → value)."""
        return {
            "path": self.path,
            "category": self.category,
            "status": self.status.value,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class ScopeExpansionAssessment:
    """The classified set of out-of-plan files with status projections."""

    items: tuple[ScopeExpansionItem, ...] = ()

    @property
    def notices(self) -> tuple[ScopeExpansionItem, ...]:
        return tuple(i for i in self.items if i.status is ScopeExpansionStatus.NOTICE)

    @property
    def risks(self) -> tuple[ScopeExpansionItem, ...]:
        return tuple(i for i in self.items if i.status is ScopeExpansionStatus.RISK)

    @property
    def blockers(self) -> tuple[ScopeExpansionItem, ...]:
        return tuple(i for i in self.items if i.status is ScopeExpansionStatus.BLOCKER)

    @property
    def has_blocker(self) -> bool:
        return any(i.status is ScopeExpansionStatus.BLOCKER for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        """Durable, JSON-safe view: items plus summary counts and ``has_blocker``."""
        return {
            "items": [item.to_dict() for item in self.items],
            "has_blocker": self.has_blocker,
            "counts": {
                "notice": len(self.notices),
                "risk": len(self.risks),
                "blocker": len(self.blockers),
            },
        }


# ── (B) pure derivation of the in-plan pattern set ───────────────────────────


def derive_in_plan_patterns(
    plan: Any,
    project_allowed_modifications: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Fold the durable plan scope into a sorted set of in-plan glob patterns.

    Unions ``ParsedPlan.owned_files`` / ``allowed_modifications`` (plan and per
    subtask) with the project-level ``PluginConfig.allowed_modifications`` list,
    extracting the bare glob from each ``"glob — reason"`` entry. Pure — no I/O.
    """
    return resolve_declared_write_scope(
        plan,
        project_allowed_modifications,
    ).patterns


def _path_matches(rel: str, patterns: Sequence[str]) -> bool:
    """Compatibility-local name for the canonical declared-scope matcher."""
    return path_matches_declared_scope(rel, patterns)


# ── (C) pure categorisation + signal building ────────────────────────────────


_BUILD_BASENAMES = frozenset(
    {
        "directory.build.props",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "poetry.lock",
        "cargo.toml",
        "cargo.lock",
        "makefile",
    }
)
_BUILD_SUFFIXES = (".csproj", ".sln", ".gradle")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _stem(path: str) -> str:
    name = _basename(path)
    return name.rsplit(".", 1)[0] if "." in name else name


def categorize_file(path: str) -> str:
    """Deterministically map a path to a scope-expansion category (pure).

    Precedence runs from most-sensitive to least so a security/persistence/wire
    signal is never masked by an incidental build/fixture match.
    """
    if not isinstance(path, str) or not path:
        return CATEGORY_OTHER
    p = path.lower()
    name = _basename(p)

    # test source module (any language) — checked before the sensitive families
    # so a sensitive token in a test's name/path cannot mis-escalate it. See
    # ``_is_test_module``. Fixtures, goldens, and test *data* are excluded there
    # and keep their content categories via the branches below.
    if _is_test_module(path, p):
        return CATEGORY_TEST
    # security — secret / auth / credential material (highest priority).
    if any(tok in p for tok in ("secret", "auth", "credential")):
        return CATEGORY_SECURITY
    # persistence — storage, *state*.py, migrations.
    if (
        p.startswith("storage/")
        or "/storage/" in p
        or "migration" in p
        or (name.endswith(".py") and "state" in name)
    ):
        return CATEGORY_PERSISTENCE
    # generated schema — the SDK schema snapshot and *schema*.json snapshots.
    if name == "sdk_schema.json" or (name.endswith(".json") and "schema" in name):
        return CATEGORY_SCHEMA
    # public wire — sdk/* sources, proto, wire schemas.
    if p.startswith("sdk/") or "/sdk/" in p or p.endswith(".proto") or "wire" in name:
        return CATEGORY_PUBLIC_WIRE
    # fixture / snapshot — golden corpora.
    if "tests/fixtures" in p or "snapshot" in p or "golden" in p:
        return CATEGORY_FIXTURE
    # build — project build descriptors.
    if name in _BUILD_BASENAMES or name.endswith(_BUILD_SUFFIXES):
        return CATEGORY_BUILD
    # import wiring — package __init__ files.
    if name == "__init__.py":
        return CATEGORY_IMPORT_WIRING
    # project-local config.
    if name.endswith((".cfg", ".ini", ".toml", ".yaml", ".yml")) or "config" in name:
        return CATEGORY_PROJECT_CONFIG
    return CATEGORY_OTHER


def _stat_flags(stat: Mapping[str, Any] | None) -> tuple[bool, bool]:
    """Derive ``(large_diff, destructive_delete)`` from a per-file diff stat."""
    if not isinstance(stat, Mapping):
        return False, False
    added = int(stat.get("added", 0) or 0)
    removed = int(stat.get("removed", 0) or 0)
    large = bool(stat.get("large")) or (added + removed) >= LARGE_DIFF_LINES
    destructive = bool(
        stat.get("is_deletion") or stat.get("deleted") or stat.get("destructive")
    )
    return large, destructive


def _has_paired_alignment(path: str, changed_file_set: Sequence[str]) -> bool:
    """True when a related test/doc/schema file is also in the change set (pure).

    A public-wire change is "aligned" when the same change set carries a related
    test, doc, or schema file sharing the file stem — the durable signal that a
    wire change shipped with its matching update rather than alone.
    """
    stem = _stem(path)
    if not stem:
        return False
    for other in changed_file_set:
        if not isinstance(other, str) or not other or other == path:
            continue
        low = other.lower()
        base = _basename(low)
        is_test = base.startswith("test_") or "test" in base or "/tests/" in low or low.startswith("tests/")
        is_doc = low.startswith("docs/") or low.endswith(".md")
        is_schema = "schema" in base or low.endswith(".proto")
        if (is_test or is_doc or is_schema) and stem.lower() in base:
            return True
    return False


def build_scope_expansion_signals(
    *,
    changed_files: Sequence[str],
    in_plan_patterns: Sequence[str],
    diff_stats_by_file: Mapping[str, Mapping[str, Any]] | None = None,
    changed_file_set: Sequence[str] | None = None,
    gate_status_by_category: Mapping[str, str] | None = None,
    explained_files: Sequence[str] | None = None,
    repeated_paths: Sequence[str] | None = None,
    sdk_flags_by_file: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[FileScopeSignals, ...]:
    """Build per-file signals for every out-of-plan changed file (pure).

    A file matching ``in_plan_patterns`` is in scope and skipped. For each
    out-of-plan file: ``verified`` is the per-category gate being green
    (per-file/per-category binding, not a global flag); ``large_diff`` /
    ``destructive_delete`` come from ``diff_stats_by_file``; ``has_explanation``
    / ``repeated_across_corrections`` from membership in the explained / repeated
    sets; ``paired_alignment`` from related files in ``changed_file_set``; the
    sensitivity flags from the category; the ``sdk_*`` flags from
    ``sdk_flags_by_file``. Any absent signal stays conservatively ``False``.
    """
    diff_stats = diff_stats_by_file or {}
    gate_status = gate_status_by_category or {}
    explained = set(explained_files or ())
    repeated = set(repeated_paths or ())
    sdk_flags = sdk_flags_by_file or {}
    full_set = tuple(changed_file_set) if changed_file_set is not None else tuple(changed_files)

    signals: list[FileScopeSignals] = []
    for path in changed_files:
        if not isinstance(path, str) or not path:
            continue
        if _path_matches(path, in_plan_patterns):
            continue
        category = categorize_file(path)
        large_diff, destructive_delete = _stat_flags(diff_stats.get(path))
        sdk = sdk_flags.get(path) or {}
        signals.append(
            FileScopeSignals(
                path=path,
                category=category,
                verified=gate_status.get(category) == _GREEN,
                has_explanation=path in explained,
                large_diff=large_diff,
                destructive_delete=destructive_delete,
                repeated_across_corrections=path in repeated,
                paired_alignment=_has_paired_alignment(path, full_set),
                is_public_wire=category == CATEGORY_PUBLIC_WIRE,
                is_persistence=category == CATEGORY_PERSISTENCE,
                is_security=category == CATEGORY_SECURITY,
                sdk_already_public=bool(sdk.get("already_public")),
                sdk_no_new_exports=bool(sdk.get("no_new_exports")),
                sdk_restores_invariant=bool(sdk.get("restores_invariant")),
            )
        )
    return tuple(signals)


# ── (D) pure classification + render ─────────────────────────────────────────


def _evidence_for(sig: FileScopeSignals) -> tuple[str, ...]:
    """Build the deterministic, ordered evidence tuple from the set signals."""
    parts: list[str] = []
    parts.append("verified" if sig.verified else "unverified")
    parts.append("explained" if sig.has_explanation else "no-explanation")
    if sig.large_diff:
        parts.append("large-diff")
    if sig.destructive_delete:
        parts.append("destructive-delete")
    if sig.repeated_across_corrections:
        parts.append("repeated-across-corrections")
    if sig.is_public_wire:
        parts.append("paired-alignment" if sig.paired_alignment else "no-paired-alignment")
    if sig.is_persistence:
        parts.append("persistence")
    if sig.is_security:
        parts.append("security")
    if _is_sdk_reconciliation(sig):
        parts.append("sdk-reconciliation: already-public, no-new-exports, restores-invariant")
    return tuple(parts)


def _is_sdk_reconciliation(sig: FileScopeSignals) -> bool:
    """True when all three SDK-reconciliation invariant flags are observed."""
    return (
        sig.sdk_already_public
        and sig.sdk_no_new_exports
        and sig.sdk_restores_invariant
    )


def classify_file_signals(sig: FileScopeSignals) -> ScopeExpansionItem:
    """Apply the deterministic status matrix to one file's signals (pure).

    Order:
    1. An SDK schema/wire reconciliation of an already-public, no-new-export
       change that restores an existing invariant — and is not itself large /
       destructive / repeated — is ``notice`` (verified) or ``risk`` (not), NOT
       a blocker.
    2. Hard blocker conditions: persistence, security, an unaligned public wire,
       a destructive delete, a large diff, or a change repeated across
       corrections.
    3. ``notice``: a benign category that is verified, explained, and neither
       large nor destructive.
    4. Otherwise ``risk`` — the conservative floor (covers benign-but-unverified,
       benign-but-unexplained, and every other out-of-plan change).
    """
    evidence = _evidence_for(sig)

    def item(status: ScopeExpansionStatus) -> ScopeExpansionItem:
        return ScopeExpansionItem(
            path=sig.path, category=sig.category, status=status, evidence=evidence
        )

    # 1. SDK reconciliation of an already-public invariant restoration.
    if (
        sig.category in _SDK_RECONCILABLE_CATEGORIES
        and _is_sdk_reconciliation(sig)
        and not sig.large_diff
        and not sig.destructive_delete
        and not sig.repeated_across_corrections
    ):
        return item(ScopeExpansionStatus.NOTICE if sig.verified else ScopeExpansionStatus.RISK)

    # 2. Hard blocker conditions.
    if (
        sig.is_persistence
        or sig.is_security
        or sig.destructive_delete
        or sig.large_diff
        or sig.repeated_across_corrections
        or (sig.is_public_wire and not sig.paired_alignment)
    ):
        return item(ScopeExpansionStatus.BLOCKER)

    # 3. Benign verified + explained → notice.
    if (
        sig.category in _BENIGN_CATEGORIES
        and sig.has_explanation
        and sig.verified
        and not sig.large_diff
        and not sig.destructive_delete
    ):
        return item(ScopeExpansionStatus.NOTICE)

    # 4. Conservative floor: anything not cleared to notice is risk.
    return item(ScopeExpansionStatus.RISK)


def build_scope_expansion_assessment(
    signals: Sequence[FileScopeSignals],
) -> ScopeExpansionAssessment:
    """Classify every signal into a :class:`ScopeExpansionAssessment` (pure)."""
    return ScopeExpansionAssessment(
        items=tuple(classify_file_signals(sig) for sig in signals)
    )


_RENDER_HEADERS: tuple[tuple[str, str], ...] = (
    (ScopeExpansionStatus.NOTICE.value, "Scope expanded:"),
    (ScopeExpansionStatus.RISK.value, "Scope expansion risk:"),
    (ScopeExpansionStatus.BLOCKER.value, "Scope expansion blocker:"),
)


def render_scope_expansion_lines(assessment_dict: Mapping[str, Any]) -> tuple[str, ...]:
    """Render a compact, stable summary from a :meth:`ScopeExpansionAssessment.to_dict`.

    Emits a header line per non-empty status group followed by one indented
    ``<path> — <category>; <evidence>`` line per item. Pure — reads only the
    JSON-safe dict, never re-classifies.
    """
    items = assessment_dict.get("items") or ()
    by_status: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        if isinstance(item, Mapping):
            by_status.setdefault(str(item.get("status")), []).append(item)

    lines: list[str] = []
    for status_value, header in _RENDER_HEADERS:
        group = by_status.get(status_value)
        if not group:
            continue
        lines.append(header)
        for item in group:
            path = item.get("path", "")
            category = item.get("category", "")
            evidence = item.get("evidence") or ()
            evidence_text = "; ".join(str(e) for e in evidence)
            suffix = f"; {evidence_text}" if evidence_text else ""
            lines.append(f"  {path} — {category}{suffix}")
    return tuple(lines)


__all__ = [
    "CATEGORY_BUILD",
    "CATEGORY_FIXTURE",
    "CATEGORY_IMPORT_WIRING",
    "CATEGORY_OTHER",
    "CATEGORY_PERSISTENCE",
    "CATEGORY_PROJECT_CONFIG",
    "CATEGORY_PUBLIC_WIRE",
    "CATEGORY_SCHEMA",
    "CATEGORY_SECURITY",
    "FileScopeSignals",
    "ScopeExpansionAssessment",
    "ScopeExpansionItem",
    "ScopeExpansionStatus",
    "build_scope_expansion_assessment",
    "build_scope_expansion_signals",
    "categorize_file",
    "classify_file_signals",
    "derive_in_plan_patterns",
    "render_scope_expansion_lines",
]
