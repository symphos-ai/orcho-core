# SPDX-License-Identifier: Apache-2.0
"""``compliance_check`` phase handler — default no-op stub."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.runtime import PipelineState


def _phase_compliance_check(state: PipelineState) -> PipelineState:
    """Default no-op compliance handler.

    The ``enterprise`` profile lists ``compliance_check`` in core so the
    pipeline shape is announceable for compliance-conscious users, but
    core ships only a stub. Plugin authors override this via
    ``registry.register("compliance_check", ...)`` with their internal
    SaaS / policy engine.
    """
    state.phase_log["compliance_check"] = {
        "skipped": "default no-op stub; register a custom handler "
                   "for real compliance.",
    }
    return state
