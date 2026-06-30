# SPDX-License-Identifier: Apache-2.0
"""Builtin phase-handler entry points (one module per ``orcho.phases`` key).

Each module owns a single ``(state) -> state`` handler that the package
facade (``pipeline.phases.builtin``) re-exports so the entry-point paths
``pipeline.phases.builtin:_phase_*`` keep resolving. Handlers import the
helpers they need from their real homes (lifecycle, plan_artifact,
session_invoke, …) and never from the package facade, so there is no
import cycle through ``pipeline.phases.builtin.__init__``.
"""
