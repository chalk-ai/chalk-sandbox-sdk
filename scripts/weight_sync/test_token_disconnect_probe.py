import json

import pytest

from token_disconnect_probe import generated_text_event


def event(text):
    return (
        "data: "
        + json.dumps({"choices": [{"text": text, "finish_reason": None}]})
        + "\n\n"
    ).encode()


def test_skips_comments_metadata_and_empty_text():
    lines = (
        b": heartbeat\n\nevent: completion\n\n" + event("") + event(" one")
    ).splitlines(keepends=True)
    result = generated_text_event(lines)
    assert result["generated_text"] == " one"
    assert result["parsed_event_count"] == 2
    assert result["generated_token_count_lower_bound"] == 1
    assert result["exact_generated_token_count"] is None


@pytest.mark.parametrize("stream", [b"data: [DONE]\n\n", event(""), b": comment\n\n"])
def test_requires_nonempty_generated_text(stream):
    with pytest.raises(ValueError, match="before nonempty generated text"):
        generated_text_event(stream.splitlines(keepends=True))


def test_stops_before_consuming_native_eof():
    def stream():
        yield from event("1").splitlines(keepends=True)
        raise AssertionError("Probe consumed beyond first generated text")

    assert generated_text_event(stream())["generated_characters"] == 1
