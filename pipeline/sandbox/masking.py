"""
pipeline/sandbox/masking.py — output token masking for live agent streams.

The :class:`TokenMasker` is wired into :func:`agents.stream._stream_run`
ahead of ``output.log`` writes and stdout echo. Returned stdout (the
string the runtime parses for JSONL events / session ids) stays raw
— masking is for *displayed and persisted* output, not for the
machine parsers downstream.

Design notes:

* The masker compiles regex at construction. Bad patterns surface
  here (operator config) rather than during agent dispatch.
* Replacement is the fixed string ``***MASKED***``. Length is not
  preserved — preserving length would leak the secret length, which
  is a meaningful side channel on short keys.
* The masker is stateless across chunks. PTY streaming hands us
  data line-by-line, so secrets that span a chunk boundary are
  rare; ADR 0034 documents this as accepted residual risk. A
  future enhancement could buffer one trailing partial line.
"""
from __future__ import annotations

import re

MASK_REPLACEMENT = "***MASKED***"


class MaskingPatternError(ValueError):
    """Raised when a profile-supplied regex fails to compile.

    Distinct from ``re.error`` so resolver code can catch this
    category specifically and re-raise with the offending pattern
    in the message.
    """


class TokenMasker:
    """Apply a fused regex over text chunks, replacing matches.

    A single compiled pattern (alternation of all input regexes) is
    used so each chunk is scanned once. Patterns are compiled with
    no flags — authors are responsible for case sensitivity if it
    matters for their issuer.
    """

    __slots__ = ("_pattern", "_active")

    def __init__(self, patterns: tuple[str, ...]) -> None:
        self._active = bool(patterns)
        if not self._active:
            self._pattern: re.Pattern[str] | None = None
            return
        compiled: list[str] = []
        for raw in patterns:
            try:
                # Compile each individually first so a bad pattern
                # surfaces with its own text in the error.
                re.compile(raw)
            except re.error as exc:
                raise MaskingPatternError(
                    f"masking pattern is not a valid regex: {raw!r}: {exc}"
                ) from exc
            compiled.append(f"(?:{raw})")
        try:
            self._pattern = re.compile("|".join(compiled))
        except re.error as exc:  # pragma: no cover — caught above
            raise MaskingPatternError(
                f"masking patterns failed to combine: {exc}"
            ) from exc

    @property
    def active(self) -> bool:
        """``True`` when at least one pattern is compiled in.

        Hot-path callers can short-circuit on this property to skip
        the regex call on every chunk in the common case where the
        operator disabled masking.
        """
        return self._active

    def mask(self, text: str) -> str:
        """Return ``text`` with every matching token replaced.

        On inactive maskers this returns ``text`` unchanged with no
        copy. Empty ``text`` is returned as-is. Exceptions from the
        regex engine bubble up — the masker must not silently fail
        to mask, that would leak secrets that the operator believed
        were protected.
        """
        if not self._active or not text or self._pattern is None:
            return text
        return self._pattern.sub(MASK_REPLACEMENT, text)


__all__ = [
    "MASK_REPLACEMENT",
    "MaskingPatternError",
    "TokenMasker",
]
