"""Tests for ``chalkcompute.scorers.json_match``."""

from __future__ import annotations

import pytest

from chalkcompute import scorers

pytestmark = pytest.mark.usefixtures("no_deploy")


def _score(scorer, output, expected):
    return scorer._func(output=output, expected=expected)


def test_equal_documents_score_one_whatever_the_key_order() -> None:
    scorer = scorers.json_match()

    graded = _score(scorer, '{"b": 2, "a": 1}', '{"a": 1, "b": 2}')

    assert graded.score == 1.0
    assert graded.metadata == {"mismatched": []}


def test_list_order_still_counts() -> None:
    scorer = scorers.json_match()

    assert _score(scorer, "[1, 2]", "[1, 2]").score == 1.0
    assert _score(scorer, "[2, 1]", "[1, 2]").score == 0.0


def test_names_the_keys_that_differed() -> None:
    graded = _score(
        scorers.json_match(), '{"a": 1, "b": 9}', '{"a": 1, "b": 2, "c": 3}'
    )

    assert graded.score == 0.0
    assert graded.metadata == {"mismatched": ["b", "c"]}


def test_paths_score_the_share_that_agree() -> None:
    scorer = scorers.json_match(paths=["user.id", "user.name", "items.0.sku"])
    output = '{"user": {"id": 7, "name": "wrong"}, "items": [{"sku": "A1"}]}'
    expected = '{"user": {"id": 7, "name": "right"}, "items": [{"sku": "A1"}]}'

    graded = _score(scorer, output, expected)

    assert graded.score == pytest.approx(2 / 3)
    assert graded.metadata == {"mismatched": ["user.name"]}


def test_paths_ignore_everything_they_do_not_name() -> None:
    scorer = scorers.json_match(paths=["id"])

    assert (
        _score(scorer, '{"id": 1, "noise": "a"}', '{"id": 1, "noise": "b"}').score
        == 1.0
    )


def test_a_path_missing_on_one_side_counts_as_a_mismatch() -> None:
    scorer = scorers.json_match(paths=["id"])

    assert _score(scorer, "{}", '{"id": 1}').score == 0.0
    assert _score(scorer, "{}", "{}").score == 1.0
    assert _score(scorer, '{"items": []}', '{"items": [1]}').score == 1.0


def test_a_column_that_does_not_parse_scores_zero() -> None:
    scorer = scorers.json_match()

    assert _score(scorer, "{not json", "{}").metadata["error"].startswith("output:")
    assert _score(scorer, "{}", "{not json").metadata["error"].startswith("expected:")
    assert _score(scorer, None, "{}").score == 0.0


def test_rejects_an_empty_path_list() -> None:
    assert scorers.json_match()._explicit_name == "json-match"
    with pytest.raises(ValueError, match="at least one path"):
        scorers.json_match(paths=[])


# Python holds True == 1, so a boolean and a number would pass for each other.
@pytest.mark.parametrize(
    ("output", "expected", "path"),
    [
        ('{"ok": true}', '{"ok": 1}', "ok"),
        ('{"ok": false}', '{"ok": 0}', "ok"),
        ('{"a": {"ok": true}}', '{"a": {"ok": 1}}', "a.ok"),
        ('{"a": [true]}', '{"a": [1]}', "a.0"),
    ],
)
def test_a_boolean_is_not_a_number(output, expected, path) -> None:
    assert _score(scorers.json_match(), output, expected).score == 0.0
    assert _score(scorers.json_match(paths=[path]), output, expected).score == 0.0
    assert _score(scorers.json_match(), output, output).score == 1.0
    assert _score(scorers.json_match(paths=[path]), output, output).score == 1.0


def test_integers_and_floats_are_the_same_json_number() -> None:
    assert _score(scorers.json_match(), '{"n": 1}', '{"n": 1.0}').score == 1.0


def test_nan_and_infinity_are_not_accepted_as_json() -> None:
    graded = _score(scorers.json_match(), '{"x": NaN}', '{"x": NaN}')

    assert graded.score == 0.0
    assert "not valid JSON" in graded.metadata["error"]


# A bare string iterates into one path per character, and character paths are
# missing on both sides, which would read as agreement.
def test_rejects_a_single_path_passed_as_a_string() -> None:
    with pytest.raises(TypeError, match="must be a sequence"):
        scorers.json_match(paths="user.id")
