"""Deterministic, test-only control-loop harness.

This package drives real ``run_pipeline`` mock runs (and the real
non-interactive delivery-defer settle path) to a set of named lifecycle
states, then captures the *real* persisted ``meta.json`` each run leaves on
disk. T2 consumes these drivers to prove the core lifecycle and the SDK
read-model (``run_diagnosis`` / ``delivery_decision_state`` / recovery
lineage) agree on every state.

Nothing here touches production classification or settle logic: the drivers
are local helpers layered over the public entry points (``run_pipeline`` and
``agents.runtimes.MockAgentProvider``) plus locally-defined provider builders
and external-surface patches.
"""
