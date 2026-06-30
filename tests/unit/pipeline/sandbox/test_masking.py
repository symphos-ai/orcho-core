"""Token-masker behavior — built-in patterns and operator extensions."""
from __future__ import annotations

import pytest

from pipeline.sandbox.defaults import BUILTIN_TOKEN_PATTERNS
from pipeline.sandbox.masking import (
    MASK_REPLACEMENT,
    MaskingPatternError,
    TokenMasker,
)


class TestTokenMasker:
    def test_empty_patterns_inactive(self) -> None:
        m = TokenMasker(())
        assert m.active is False
        assert m.mask("anything sk-ant-xxx") == "anything sk-ant-xxx"

    def test_anthropic_token_masked(self) -> None:
        m = TokenMasker(BUILTIN_TOKEN_PATTERNS)
        out = m.mask("found sk-ant-api01-abcdefghijklmnopqrstuvwxyz0123 in log")
        assert "sk-ant-" not in out
        assert MASK_REPLACEMENT in out

    def test_openai_token_masked(self) -> None:
        m = TokenMasker(BUILTIN_TOKEN_PATTERNS)
        out = m.mask("openai key sk-abcdefghijklmnopqrstuvwxyz")
        assert "sk-abcdef" not in out
        assert MASK_REPLACEMENT in out

    def test_gemini_token_masked(self) -> None:
        m = TokenMasker(BUILTIN_TOKEN_PATTERNS)
        # AIza + exactly 35 url-safe chars
        token = "AIza" + "0123456789ABCDEFGHIJKLMNOPQRSTUVWXY"
        out = m.mask(f"x {token} y")
        assert token not in out
        assert MASK_REPLACEMENT in out

    def test_safe_text_passes_through(self) -> None:
        m = TokenMasker(BUILTIN_TOKEN_PATTERNS)
        text = "no secrets here, just a plain log line about commit 6a2a233"
        assert m.mask(text) == text

    def test_multiple_tokens_one_call(self) -> None:
        m = TokenMasker(BUILTIN_TOKEN_PATTERNS)
        text = (
            "first sk-ant-api01-abcdefghijklmnopqrstuvwxyz0123 "
            "second sk-abcdefghijklmnopqrstuvwxyz"
        )
        out = m.mask(text)
        assert out.count(MASK_REPLACEMENT) == 2

    def test_bad_regex_raises_at_construction(self) -> None:
        with pytest.raises(MaskingPatternError, match="not a valid regex"):
            TokenMasker((r"sk-[unclosed",))

    def test_custom_pattern_overlaying_builtin(self) -> None:
        m = TokenMasker(BUILTIN_TOKEN_PATTERNS + (r"PROJTOK-[A-Z0-9]+",))
        out = m.mask("our token PROJTOK-ABC123 alongside sk-ant-api01-abcdefghijklmnopqrstuvwxyz")
        assert "PROJTOK-ABC123" not in out
        assert out.count(MASK_REPLACEMENT) == 2

    def test_empty_input(self) -> None:
        m = TokenMasker(BUILTIN_TOKEN_PATTERNS)
        assert m.mask("") == ""
