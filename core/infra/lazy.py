"""Small lazy-value helpers for side-effect-free construction paths."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

T = TypeVar("T")
_UNSET = object()


class LazyValue[T]:
    """Cache a value from ``factory`` only when first requested."""

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._value: object = _UNSET

    def get(self) -> T:
        if self._value is _UNSET:
            self._value = self._factory()
        return cast(T, self._value)

    def set(self, value: T) -> None:
        self._value = value


def lazy_cli_binary(runtime: str, resolver: Callable[[], str]) -> LazyValue[str]:
    """Return a lazy CLI binary resolver with runtime-specific errors."""

    def resolve() -> str:
        try:
            return resolver()
        except RuntimeError as exc:
            env_key = f"{runtime.upper()}_BIN"
            raise RuntimeError(
                f"{runtime} runtime cannot start because its CLI binary is "
                f"unavailable. Install the {runtime} CLI, set "
                f"{env_key}=/path/to/{runtime}, or use --mock/--dry-run for "
                f"CI paths that should not invoke agents. Original error: {exc}"
            ) from exc

    return LazyValue(resolve)
