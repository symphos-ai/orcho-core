"""Evidence bundles — the REA-4 unblock surface.

Three concerns separated:

- `collect_evidence` reads run artifacts and returns a typed
  `EvidenceBundle` (validation result and `markdown` rendering
  pre-baked).
- `render_evidence_md` returns a markdown string from a bundle, in
  case the caller already has one and wants to re-render.
- `write_evidence_bundle` persists the bundle to a target directory
  and returns the list of written paths. **Side-effecting.**
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline import evidence as _evidence
from pipeline.evidence import (
    collect_evidence as _collect_evidence,
    render_evidence_md as _render_md,
)
from pipeline.evidence.schema import EvidenceSchemaError, validate_bundle
from sdk.errors import EvidenceInvalid
from sdk.runs import _CWD_DEFAULT, find_run
from sdk.types import EvidenceBundle


def collect_evidence(
    run_id: str | None = None,
    *,
    workspace: Path | str | None = None,
    runs_dir: Path | str | None = None,
    cwd: Path | str | None | object = _CWD_DEFAULT,
) -> EvidenceBundle:
    """Compose the evidence bundle for a run.

    Resolves the run via `find_run`; reads artifacts and runs the
    schema validator. The returned bundle carries `valid` /
    `validation_errors`; readers that want the strict contract should
    raise on `valid is False` themselves. The pipeline-level
    `EvidenceSchemaError` is normalised into `EvidenceInvalid` only
    when bundle composition itself fails (file missing / unreadable);
    schema-only soft failures stay in the bundle.
    """
    ref = find_run(run_id, workspace=workspace, runs_dir=runs_dir, cwd=cwd)
    try:
        body = _collect_evidence(ref.run_dir)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise EvidenceInvalid(
            f"Failed to compose evidence for {ref.run_id}: {exc}"
        ) from exc

    valid = True
    errors: tuple[str, ...] = ()
    try:
        validate_bundle(body)
    except EvidenceSchemaError as exc:
        valid = False
        errors = (str(exc),)

    markdown = _render_md(body)
    return EvidenceBundle(
        run_ref=ref,
        body=body,
        markdown=markdown,
        valid=valid,
        validation_errors=errors,
    )


def render_evidence_md(bundle: EvidenceBundle, *, debug: bool = False) -> str:
    """Re-render the markdown view of a bundle."""
    return _render_md(bundle.body, debug=debug)


def write_evidence_bundle(
    bundle: EvidenceBundle, out_dir: Path | str
) -> list[Path]:
    """Persist `evidence.json` + `evidence.md` under `out_dir/<run_id>/`.

    Side-effecting. Returns the list of written paths. Embedders that
    just need the markdown can call `render_evidence_md` and write it
    themselves.
    """
    target = Path(out_dir) / bundle.run_ref.run_id
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "evidence.json"
    md_path = target / "evidence.md"
    json_path.write_text(
        json.dumps(bundle.body, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(bundle.markdown, encoding="utf-8")
    return [json_path, md_path]


__all__ = [
    "collect_evidence",
    "render_evidence_md",
    "write_evidence_bundle",
]
# Anchor the import so static checkers see the explicit pipeline.evidence
# dependency even when only the re-exports are touched.
_ = _evidence
