"""pipeline.evidence — Run evidence model (REA roadmap).

Layered build-out matching the run-evidence audit roadmap (internal
planning record):

* REA-0: ``write_placeholder`` — the locked ``evidence.json`` stub
  written when a run cannot finalize a full bundle. Schema version
  ``"0-placeholder"``.
* REA-1: typed plan contract — surfaces in ``schema.REQUIRED_PLAN_KEYS``.
* REA-2: event spine — consumed by the collector to compose
  per-phase / gate / command / artifact rollups.
* REA-3 (this milestone): ``collect_evidence`` + ``write_bundle`` +
  ``render_evidence_md``. Schema version ``"1"``. CLI
  ``orcho evidence <run_id>`` regenerates the bundle on demand.

ADR 0020 anchors this module in core, not in any downstream embedder.
"""
from __future__ import annotations

from pipeline.evidence.bundle import (
    EVIDENCE_FILE_NAME,
    EVIDENCE_MD_FILE_NAME,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION_PLACEHOLDER,
    write_bundle,
    write_bundle_or_placeholder,
    write_placeholder,
)
from pipeline.evidence.collector import collect_evidence
from pipeline.evidence.render_md import render_evidence_md
from pipeline.evidence.schema import (
    EvidenceSchemaError,
    validate_bundle,
)

__all__ = [
    "EVIDENCE_FILE_NAME",
    "EVIDENCE_MD_FILE_NAME",
    "EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_SCHEMA_VERSION_PLACEHOLDER",
    "EvidenceSchemaError",
    "collect_evidence",
    "render_evidence_md",
    "validate_bundle",
    "write_bundle",
    "write_bundle_or_placeholder",
    "write_placeholder",
]
