#!/usr/bin/env python3
"""Compatibility entrypoint for the project pipeline.

The project pipeline lives in :mod:`pipeline.project.*` per ADR 0042
(Phases A–J). This module exists solely to keep the ``orcho-run``
entry point + the four documented public names resolving for
external callers that have not migrated to the canonical homes.

Stable re-export surface (Phase J — closed):

* :func:`pipeline.project.app.run_pipeline` — the back-compat
  positional/keyword surface for the project pipeline. Signature
  byte-for-byte preserved; pinned by
  ``tests/unit/pipeline/test_project_run_request.py::TestSignatureLock``.
* :func:`pipeline.project.cli.main` — the ``orcho-run`` CLI entry.
* :class:`agents.protocols.SessionMode` — the session-mode enum.
* :class:`pipeline.project.bootstrap.RunIdCollisionError` — the
  fresh-dir guard exception.

Everything else moved during the Phase B–H extractions. Consumers
that still need helpers, dataclasses, or internal callables import
them from the canonical homes documented in ADR 0042's status table.
"""

from __future__ import annotations

from agents.protocols import SessionMode
from pipeline.project.app import run_pipeline
from pipeline.project.bootstrap import RunIdCollisionError
from pipeline.project.cli import main

__all__ = ["RunIdCollisionError", "SessionMode", "main", "run_pipeline"]


if __name__ == "__main__":
    main()
