"""pipeline/entry_points.py — Phase 7c: shared entry_points discovery.

orcho-core ships built-in registry entries (TestsGate /
LinearPhaseStepExecutor / 6 session adapters / 6 v2 profiles) directly
via dataclass-typed factories.
Phase 7 opens the same registries to third-party plugins via
``importlib.metadata`` entry_points groups:

* ``orcho.execution_modes`` — ``PhaseStepExecutor`` instances
* ``orcho.quality_gates``    — ``QualityGateHandler`` instances
* ``orcho.session_adapters`` — ``SessionAdapter`` instances
* ``orcho.profiles``         — ``Profile`` instances

This module factors the discovery + load + override semantics into
one place so every registry uses the same plugin-author contract.

**Plugin entry contract:**

Each entry's value is either:

  1. A bare instance — ``my_module:MY_INSTANCE``
  2. A zero-arg callable returning an instance —
     ``my_module:make_my_instance``
  3. A class with a no-arg constructor — ``my_module:MyClass``

The ``discover_entry_points`` helper duck-types between the three:
classes and function-shaped factories are invoked with no args; other
objects are used as-is. Callable instances count as bare instances, not
factories, so plugin authors can ship handlers that implement
``__call__`` without the loader accidentally executing them at discovery
time.

**Override semantics:**

Re-registration is allowed. A plugin entry named ``"tests"`` shadows
the built-in ``TestsGate``; the supported customer-overlay
mechanism. The order is built-ins first, then plugin entries — so
plugin overrides win deterministically.

**Failure isolation:**

One broken plugin entry must not block discovery for the rest. Each
``ep.load()`` failure is caught, logged with a clear diagnostic
identifying the bad plugin, and the loop continues. The orchestrator
sees the partial registry — same shape as if the broken plugin
weren't installed.
"""
from __future__ import annotations

import functools
import inspect
from typing import Any

__all__ = ["discover_entry_points", "register_entry_points"]


def discover_entry_points(
    group: str,
    *,
    skip: set[str] | None = None,
) -> dict[str, Any]:
    """Load all entries in the named entry_points ``group``.

    Returns ``{name: instance}`` where ``instance`` is whatever the
    entry resolves to after ``ep.load()`` + zero-arg invocation if
    callable. Names in ``skip`` are filtered out before loading
    (used by ``AgentRegistry`` to skip Gemini when the CLI is
    absent).

    Failures inside individual ``ep.load()`` calls are caught + logged;
    the loop continues. Lazy import of ``importlib.metadata`` keeps
    unrelated tests from paying for the metadata scan.

    Args:
        group: entry_points group name (e.g. ``orcho.quality_gates``).
        skip: set of entry names to ignore (rarely needed; default
            ``None`` = empty set).

    Returns:
        Dict mapping entry name → loaded instance. Empty when no
        plugins ship the group OR all entries failed to load.
    """
    skip = skip or set()
    from importlib.metadata import entry_points
    out: dict[str, Any] = {}
    for ep in entry_points(group=group):
        if ep.name in skip:
            continue
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001 — broad catch is intentional
            print(
                f"  ! orcho.entry_points: failed to load "
                f"{group!r}/{ep.name!r}: {exc}"
            )
            continue
        # Duck-type the accepted shapes:
        #   1. bare instance  → use as-is
        #   2. class / function-shaped zero-arg factory → invoke
        #
        # Avoid plain ``callable(obj)`` here: handler instances may implement
        # ``__call__`` as their own runtime protocol and still be the intended
        # bare-instance shape. A callable object factory can be exposed through
        # a small named function if a plugin needs that pattern later.
        if _should_invoke_loaded_entry(obj):
            try:
                obj = obj()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"  ! orcho.entry_points: invoking "
                    f"{group!r}/{ep.name!r} factory raised: {exc}"
                )
                continue
        out[ep.name] = obj
    return out


def _should_invoke_loaded_entry(obj: Any) -> bool:
    """True for class / function-shaped factories, false for instances."""
    return (
        inspect.isclass(obj)
        or inspect.isfunction(obj)
        or inspect.ismethod(obj)
        or inspect.isbuiltin(obj)
        or isinstance(obj, functools.partial)
    )


def register_entry_points(
    registry: Any,
    group: str,
    *,
    register_method: str = "register",
    skip: set[str] | None = None,
) -> int:
    """Discover and register all plugin entries in ``group`` onto
    ``registry``. Returns the count of successfully registered
    entries.

    The default ``register_method='register'`` matches every Phase 7c
    registry shape (``QualityGateRegistry`` /
    ``ExecutionModeRegistry`` / ``SessionAdapterRegistry``).
    ``AgentRegistry.register`` follows the same pattern.

    Plugin overrides land last. Built-ins should be registered before
    this function is called so plugin entries with conflicting names
    win deterministically — the standard
    ``register(name, instance)`` semantics replace existing
    registrations.

    Args:
        registry: target registry exposing ``.register(name, value)``.
        group: entry_points group name to scan.
        register_method: name of the bound method on ``registry`` to
            invoke (defaults to ``"register"``).
        skip: optional set of entry names to ignore.

    Returns:
        Number of plugin entries successfully registered. ``0`` when
        no plugins ship the group or all entries failed to load.
    """
    bind = getattr(registry, register_method, None)
    if not callable(bind):
        raise AttributeError(
            f"register_entry_points: registry {type(registry).__name__} "
            f"has no callable {register_method!r} method"
        )
    count = 0
    for name, obj in discover_entry_points(group, skip=skip).items():
        try:
            bind(name, obj)
            count += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"  ! orcho.entry_points: registering "
                f"{group!r}/{name!r} on {type(registry).__name__} "
                f"raised: {exc}"
            )
    return count
