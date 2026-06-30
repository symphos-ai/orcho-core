"""
core/contracts/release_schema.py — JSON schema for the release gate.

The release gate (``final_acceptance`` phase, future
``cross_final_acceptance`` gate) emits exactly one JSON object validated
against the schema below. Distinct from
:mod:`core.contracts.review_schema` because the release tier asks a
different question ("can this ship?") and carries different signal
(``ship_ready`` / ``release_blockers`` / ``verification_gaps`` /
``contract_status``).

Coherence invariants (strict):

* ``APPROVED`` iff ``ship_ready == True`` AND ``release_blockers == []``
  AND ``verification_gaps == []``. No grey zone — an unaddressed
  verification gap blocks ship.
* ``REJECTED`` iff ``ship_ready == False`` AND at least one
  ``release_blockers`` OR ``verification_gaps`` entry is present.
* When verdict is ``APPROVED``, every ``contract_status`` value must be
  the positive enum (``satisfied`` / ``compatible`` / ``safe`` /
  ``sufficient``) or ``not_applicable``.
* Severity is one of ``P0|P1|P2`` only — P3 belongs to ``review_changes``,
  not the release gate.

Schema is dependency-free (no pydantic) — the core stays importable on
a bare stdlib install.
"""
from __future__ import annotations

from typing import Any

RELEASE_SUMMARY_MAX_CHARS = 280
RELEASE_VERDICTS = ("APPROVED", "REJECTED")
RELEASE_SEVERITIES = ("P0", "P1", "P2")

RELEASE_REQUIRED_KEYS = (
    "verdict",
    "ship_ready",
    "short_summary",
    "release_blockers",
    "verification_gaps",
    "contract_status",
)

RELEASE_BLOCKER_REQUIRED_KEYS = (
    "id", "severity", "title", "body", "required_fix", "why_blocks_release",
)
RELEASE_BLOCKER_OPTIONAL_KEYS = ("file", "line")

VERIFICATION_GAP_REQUIRED_KEYS = ("risk", "missing_evidence", "required_check")

# contract_status keys + their allowed value sets.
CONTRACT_STATUS_VALUES: dict[str, tuple[str, ...]] = {
    "task_contract": ("satisfied", "incomplete", "unclear"),
    "interfaces":    ("compatible", "broken", "not_applicable"),
    "persistence":   ("safe", "risky", "not_applicable"),
    "tests":         ("sufficient", "weak", "missing"),
}
CONTRACT_STATUS_KEYS = tuple(CONTRACT_STATUS_VALUES.keys())

# When verdict is APPROVED, every contract_status value must be one of
# these positive / inert enums.
_CONTRACT_STATUS_APPROVED_OK: dict[str, frozenset[str]] = {
    "task_contract": frozenset({"satisfied"}),
    "interfaces":    frozenset({"compatible", "not_applicable"}),
    "persistence":   frozenset({"safe", "not_applicable"}),
    "tests":         frozenset({"sufficient"}),
}


class ReleaseSchemaError(ValueError):
    """Raised when a release dict does not match the expected schema."""


def validate_release_dict(data: Any) -> dict[str, Any]:
    """Validate ``data`` against the release-gate schema. Returns the dict on success."""
    if not isinstance(data, dict):
        raise ReleaseSchemaError(
            f"release must be a JSON object, got {type(data).__name__}"
        )

    missing = [k for k in RELEASE_REQUIRED_KEYS if k not in data]
    if missing:
        raise ReleaseSchemaError(f"release missing required keys: {missing}")

    verdict = data["verdict"]
    if verdict not in RELEASE_VERDICTS:
        raise ReleaseSchemaError(
            f"verdict must be one of {RELEASE_VERDICTS}, got {verdict!r}"
        )

    ship_ready = data["ship_ready"]
    if not isinstance(ship_ready, bool):
        raise ReleaseSchemaError(
            f"ship_ready must be a boolean, got {type(ship_ready).__name__}"
        )

    short_summary = data["short_summary"]
    if not isinstance(short_summary, str) or not short_summary.strip():
        raise ReleaseSchemaError("short_summary must be a non-empty string")
    if len(short_summary) > RELEASE_SUMMARY_MAX_CHARS:
        data["short_summary"] = (
            short_summary[: RELEASE_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        )

    blockers = data["release_blockers"]
    if not isinstance(blockers, list):
        raise ReleaseSchemaError("release_blockers must be a list")

    gaps = data["verification_gaps"]
    if not isinstance(gaps, list):
        raise ReleaseSchemaError("verification_gaps must be a list")

    # Verdict / ship_ready / blockers / gaps coherence — strict.
    if verdict == "APPROVED":
        if ship_ready is not True:
            raise ReleaseSchemaError(
                "ship_ready must be True when verdict is APPROVED"
            )
        if blockers:
            raise ReleaseSchemaError(
                "release_blockers must be empty when verdict is APPROVED"
            )
        if gaps:
            raise ReleaseSchemaError(
                "verification_gaps must be empty when verdict is APPROVED "
                "(no grey zone — unaddressed gaps block ship)"
            )
    else:  # REJECTED
        if ship_ready is not False:
            raise ReleaseSchemaError(
                "ship_ready must be False when verdict is REJECTED"
            )
        if not blockers and not gaps:
            raise ReleaseSchemaError(
                "REJECTED verdict requires at least one release_blockers "
                "or verification_gaps entry"
            )

    for i, blocker in enumerate(blockers):
        _validate_blocker(blocker, i)

    for i, gap in enumerate(gaps):
        _validate_gap(gap, i)

    _validate_contract_status(data["contract_status"], verdict)

    return data


def _validate_blocker(b: Any, index: int) -> None:
    where = f"release_blockers[{index}]"
    if not isinstance(b, dict):
        raise ReleaseSchemaError(
            f"{where} must be an object, got {type(b).__name__}"
        )

    missing = [k for k in RELEASE_BLOCKER_REQUIRED_KEYS if k not in b]
    if missing:
        raise ReleaseSchemaError(f"{where} missing required keys: {missing}")

    for key in RELEASE_BLOCKER_REQUIRED_KEYS:
        value = b[key]
        if key == "severity":
            if value not in RELEASE_SEVERITIES:
                raise ReleaseSchemaError(
                    f"{where}.severity must be one of {RELEASE_SEVERITIES}, "
                    f"got {value!r}"
                )
            continue
        if not isinstance(value, str) or not value.strip():
            raise ReleaseSchemaError(
                f"{where}.{key} must be a non-empty string"
            )

    if "file" in b and b["file"] is not None and not isinstance(b["file"], str):
        raise ReleaseSchemaError(f"{where}.file must be a string or null")

    if "line" in b and b["line"] is not None:
        line = b["line"]
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            raise ReleaseSchemaError(
                f"{where}.line must be a positive integer or null"
            )


def _validate_gap(g: Any, index: int) -> None:
    where = f"verification_gaps[{index}]"
    if not isinstance(g, dict):
        raise ReleaseSchemaError(
            f"{where} must be an object, got {type(g).__name__}"
        )

    missing = [k for k in VERIFICATION_GAP_REQUIRED_KEYS if k not in g]
    if missing:
        raise ReleaseSchemaError(f"{where} missing required keys: {missing}")

    for key in VERIFICATION_GAP_REQUIRED_KEYS:
        value = g[key]
        if not isinstance(value, str) or not value.strip():
            raise ReleaseSchemaError(
                f"{where}.{key} must be a non-empty string"
            )


def _validate_contract_status(cs: Any, verdict: str) -> None:
    if not isinstance(cs, dict):
        raise ReleaseSchemaError(
            f"contract_status must be an object, got {type(cs).__name__}"
        )

    missing = [k for k in CONTRACT_STATUS_KEYS if k not in cs]
    if missing:
        raise ReleaseSchemaError(
            f"contract_status missing required keys: {missing}"
        )

    extra = sorted(set(cs.keys()) - set(CONTRACT_STATUS_KEYS))
    if extra:
        raise ReleaseSchemaError(
            f"contract_status has unknown keys: {extra}"
        )

    for key, allowed in CONTRACT_STATUS_VALUES.items():
        value = cs[key]
        if value not in allowed:
            raise ReleaseSchemaError(
                f"contract_status.{key} must be one of {allowed}, got {value!r}"
            )

    if verdict == "APPROVED":
        for key, ok_set in _CONTRACT_STATUS_APPROVED_OK.items():
            value = cs[key]
            if value not in ok_set:
                raise ReleaseSchemaError(
                    f"contract_status.{key}={value!r} is incompatible with "
                    f"verdict=APPROVED (allowed for APPROVED: {sorted(ok_set)})"
                )


RELEASE_SCHEMA_DOC = """
Emit exactly one JSON object with this shape:

{
  "verdict": "APPROVED" | "REJECTED",
  "ship_ready": true | false,
  "short_summary": "<one or two sentences, target 280 chars>",
  "release_blockers": [
    {
      "id": "<short stable id, e.g. 'R1'>",
      "severity": "P0" | "P1" | "P2",
      "title": "<short blocker title>",
      "body": "<concrete failure scenario and why it matters>",
      "required_fix": "<what must change before ship>",
      "file": "path/to/file.py",
      "line": 123,
      "why_blocks_release": "<release-specific framing of why this stops ship>"
    }
  ],
  "verification_gaps": [
    {
      "risk": "<what could go wrong>",
      "missing_evidence": "<test / check / proof not present>",
      "required_check": "<what would close the gap>"
    }
  ],
  "contract_status": {
    "task_contract": "satisfied" | "incomplete" | "unclear",
    "interfaces":    "compatible" | "broken"   | "not_applicable",
    "persistence":   "safe"       | "risky"    | "not_applicable",
    "tests":         "sufficient" | "weak"     | "missing"
  }
}

Rules:
- All six top-level keys are required; keep `short_summary` non-empty and <=280 chars.
- APPROVED requires `ship_ready=true`, no blockers/gaps, and shippable `contract_status`.
- REJECTED requires `ship_ready=false` and at least one blocker or verification gap.
- Blockers use P0/P1/P2 only; `id`, `title`, `body`, `required_fix`, `why_blocks_release` are non-empty.
- Optional blocker `file` is a string path; optional `line` is a positive integer.
- Verification gap fields (`risk`, `missing_evidence`, `required_check`) are non-empty.
- `contract_status` keys are required; values must come from the enum sets above.
""".strip()
