"""Tests for ``chalkcompute.scorers.vlm_judge``, the judge that is shown pictures."""

from __future__ import annotations

import base64
import inspect
import json
import sys
import typing

import pytest
from pydantic import BaseModel

from chalkcompute import scorers
from chalkcompute._evaluation import EvaluationScorerResult
from chalkcompute._trace import Trace
from tests.test_scorers import _FakeCompletions, _install_fake_openai

pytestmark = pytest.mark.usefixtures("no_deploy")

PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)
URL = "https://example.com/shot.png"
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class Grade(BaseModel):
    score: float
    reason: str


REPLY = {"score": 1.0, "reason": "ok"}


@pytest.fixture
def completion(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the model call: records the messages, answers with REPLY."""
    calls: list[list[dict]] = []

    def fake_complete(spec, messages):
        calls.append(messages)
        return dict(REPLY)

    monkeypatch.setattr(scorers, "_complete", fake_complete)
    return calls


def _judge(**overrides):
    kwargs: dict = dict(model="gpt-5", instructions="Be strict.")
    kwargs.update(overrides)
    return scorers.vlm_judge(kwargs.pop("response_model", Grade), **kwargs)


def _content(calls: list[list[dict]]) -> list[dict]:
    return calls[0][-1]["content"]


def _pictures(content: list[dict]) -> list[dict]:
    return [part["image_url"] for part in content if part["type"] == "image_url"]


def test_a_url_column_is_shown_as_a_picture(completion) -> None:
    result = _judge()._func(output=URL)

    assert result == EvaluationScorerResult(score=1.0, metadata={"reason": "ok"})
    system, user = completion[0]
    assert system == {"role": "system", "content": "Be strict."}
    assert user["role"] == "user"
    intro, heading, picture = user["content"]
    assert intro["type"] == "text" and '"reason"' in intro["text"]
    assert "## output" not in intro["text"]
    assert heading == {"type": "text", "text": "## output"}
    assert picture == {"type": "image_url", "image_url": {"url": URL}}


def test_a_data_url_is_sent_as_given(completion) -> None:
    _judge()._func(output=DATA_URL)

    assert _pictures(_content(completion)) == [{"url": DATA_URL}]


def test_image_bytes_are_inlined_as_a_data_url(completion) -> None:
    _judge()._func(output=PNG)

    assert _pictures(_content(completion)) == [{"url": DATA_URL}]


def test_a_path_is_read_in_the_scorer(completion, tmp_path) -> None:
    path = tmp_path / "shot.png"
    path.write_bytes(PNG)

    _judge()._func(output=str(path))

    assert _pictures(_content(completion)) == [{"url": DATA_URL}]


@pytest.mark.parametrize(
    ("data", "media_type"),
    [
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0" + bytes(8), "image/jpeg"),
        (b"GIF89a" + bytes(8), "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"%PDF-1.7", None),
        (b"", None),
    ],
)
def test_image_bytes_are_recognized_by_their_signature(data, media_type) -> None:
    assert scorers._image_media_type(data) == media_type


def test_bytes_that_are_not_an_image_fail_the_row(completion) -> None:
    with pytest.raises(ValueError, match="PNG, JPEG, GIF, or WebP"):
        _judge()._func(output=b"%PDF-1.7 not a picture")
    assert completion == []


@pytest.mark.parametrize(
    "value", [None, "", "   ", "/no/such/file.png", "a caption, not an image", []]
)
def test_a_missing_or_unreadable_image_fails_the_row(completion, value) -> None:
    with pytest.raises(ValueError, match="vlm_judge: column 'output'"):
        _judge()._func(output=value)
    assert completion == []


def test_a_json_array_shows_every_picture_in_order(completion) -> None:
    _judge()._func(output=json.dumps([URL, DATA_URL]))

    assert _pictures(_content(completion)) == [{"url": URL}, {"url": DATA_URL}]


def test_detail_goes_on_every_picture(completion) -> None:
    _judge(detail="low")._func(output=json.dumps([URL, DATA_URL]))

    assert _pictures(_content(completion)) == [
        {"url": URL, "detail": "low"},
        {"url": DATA_URL, "detail": "low"},
    ]


def test_detail_must_be_a_fidelity_the_provider_knows() -> None:
    with pytest.raises(ValueError, match="detail"):
        _judge(detail="medium")


def test_text_inputs_are_read_before_the_pictures(completion) -> None:
    judge = _judge(inputs=("input",), images=("shot",))

    judge._func(input="a red square", shot=URL)

    intro, heading, picture = _content(completion)
    assert intro["text"].endswith("## input\n\na red square")
    assert heading["text"] == "## shot"
    assert picture["image_url"] == {"url": URL}
    assert list(inspect.signature(judge._func).parameters) == ["input", "shot"]
    hints = typing.get_type_hints(judge._func)
    assert hints["input"] is str and hints["shot"] is str


def test_trace_is_read_as_text_not_shown_as_a_picture() -> None:
    hints = typing.get_type_hints(_judge(inputs=("trace",))._func)
    assert hints["trace"] == Trace | None

    with pytest.raises(ValueError, match="transcript"):
        _judge(images=("trace",))


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        (dict(images=()), ValueError),
        (dict(images="output"), TypeError),
        (dict(inputs="input"), TypeError),
        (dict(images=("not a column",)), ValueError),
        (dict(inputs=("output",), images=("output",)), ValueError),
    ],
)
def test_rejects_unusable_columns(kwargs, error) -> None:
    with pytest.raises(error):
        _judge(**kwargs)


def test_generated_function_is_a_vision_judge_defined_in_the_caller() -> None:
    judge = _judge()

    assert judge._explicit_name == "grade"
    assert _judge(name="shot-quality")._explicit_name == "shot-quality"
    fn = judge._func
    assert fn.__module__ == __name__
    assert getattr(sys.modules[__name__], "_chalk_vlm_judge_grade") is judge
    declared = json.loads(fn.__chalk_scorer_response_schema__)
    assert list(declared["properties"]) == ["reason"]
    assert typing.get_type_hints(fn)["return"] is EvaluationScorerResult


def test_prompt_fn_replaces_the_text_and_keeps_the_pictures(completion) -> None:
    def prompt_fn(response_model, output):
        return f"grade {response_model.__name__}: {output}"

    _judge(prompt_fn=prompt_fn, instructions="")._func(output=URL)

    (user,) = completion[0]
    assert user["content"][0] == {"type": "text", "text": f"grade Grade: {URL}"}
    assert _pictures(user["content"]) == [{"url": URL}]


def test_complete_sends_the_content_parts_as_they_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _FakeCompletions(parsed=Grade(**REPLY))
    _install_fake_openai(monkeypatch, completions)
    spec = _judge()._func.__globals__["__chalk_spec__"]
    content = [
        {"type": "text", "text": "grade"},
        {"type": "image_url", "image_url": {"url": URL}},
    ]

    graded = scorers._complete(spec.judge, [{"role": "user", "content": content}])

    assert graded == Grade(**REPLY)
    assert completions.requests == [
        {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": content}],
            "response_format": Grade,
        }
    ]
