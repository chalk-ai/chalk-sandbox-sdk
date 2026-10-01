"""Tests for ``chalkcompute.scorers.regex_match``."""

from __future__ import annotations

import re

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


def test_scores_one_when_the_pattern_appears() -> None:
    scorer = scorers.regex_match(r"\d{4}")

    assert scorer._func(output="ordered in 2026 already").score == 1.0
    assert scorer._func(output="no digits here").score == 0.0


def test_records_the_match_and_its_named_groups() -> None:
    scorer = scorers.regex_match(r"(?P<code>[A-Z]{2})-(?P<number>\d+)")

    graded = scorer._func(output="ticket AB-1234 filed")

    assert graded.metadata == {
        "match": "AB-1234",
        "groups": {"code": "AB", "number": "1234"},
    }
    assert scorer._func(output="nothing").metadata == {"match": None, "groups": {}}


def test_full_match_requires_the_whole_column() -> None:
    anywhere = scorers.regex_match(r"yes")
    whole = scorers.regex_match(r"yes", full_match=True)

    assert anywhere._func(output="yes, really").score == 1.0
    assert whole._func(output="yes, really").score == 0.0
    assert whole._func(output="yes").score == 1.0


def test_flags_are_passed_through() -> None:
    scorer = scorers.regex_match(r"paris", flags=re.IGNORECASE)

    assert scorer._func(output="PARIS").score == 1.0


def test_an_empty_column_does_not_fail_the_row() -> None:
    scorer = scorers.regex_match(r"\d")

    assert scorer._func(output=None).score == 0.0
    assert scorer._func(output="").score == 0.0


def test_an_unusable_pattern_is_rejected_at_declaration() -> None:
    with pytest.raises(ValueError, match="not a usable regular expression"):
        scorers.regex_match(r"(unclosed")


def test_reads_exactly_one_column() -> None:
    assert scorers.regex_match(r"x")._explicit_name == "regex-match"
    with pytest.raises(ValueError, match="exactly one column"):
        scorers.regex_match(r"x", inputs=("output", "expected"))
