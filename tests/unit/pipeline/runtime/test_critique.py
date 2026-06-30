"""
Unit tests for critique_is_empty() — pure function, no I/O.
"""

import pytest

from pipeline.project.handoff import critique_is_empty

EMPTY_CASES = [
    pytest.param(None,                           id="none"),
    pytest.param("",                             id="empty_string"),
    pytest.param(
        '{"verdict":"APPROVED","short_summary":"No blocking issues.","findings":[]}',
        id="approved_json",
    ),
]

NONEMPTY_CASES = [
    pytest.param("lgtm",                         id="lgtm_lower"),
    pytest.param("LGTM",                         id="lgtm_upper"),
    pytest.param("LGTM — everything looks good",  id="lgtm_with_suffix"),
    pytest.param("No issues found.",             id="no_issues"),
    pytest.param("No substantive defects were found.", id="no_substantive_defects"),
    pytest.param("Line 42: missing null check",          id="line_reference"),
    pytest.param("There are several issues:\n1. ...",    id="multi_issue"),
    pytest.param("Logic error in the loop condition",    id="logic_error"),
    pytest.param("Missing error handling for edge case", id="missing_handler"),
    pytest.param("Looks plausible, but misses tests.\nVERDICT: REJECTED", id="rejected_verdict"),
]


class TestCritiqueIsEmpty:
    @pytest.mark.parametrize("critique", EMPTY_CASES)
    def test_empty(self, critique: str | None) -> None:
        assert critique_is_empty(critique) is True

    @pytest.mark.parametrize("critique", NONEMPTY_CASES)
    def test_not_empty(self, critique: str) -> None:
        assert critique_is_empty(critique) is False
