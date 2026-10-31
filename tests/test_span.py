import pytest

from chalkcompute import Span


def _span(**attributes: str) -> Span:
    return Span(
        span_id="span", trace_id="trace", operation_name="llm", attributes=attributes
    )


TOKEN_ATTRIBUTES = [
    (
        "input_tokens",
        (
            "chalk.ai.input_tokens",
            "llm.token_count.prompt",
            "gen_ai.usage.input_tokens",
        ),
    ),
    (
        "output_tokens",
        (
            "chalk.ai.output_tokens",
            "llm.token_count.completion",
            "gen_ai.usage.output_tokens",
        ),
    ),
    (
        "total_tokens",
        ("chalk.ai.total_tokens", "llm.token_count.total", "gen_ai.usage.total_tokens"),
    ),
    (
        "cache_read_tokens",
        (
            "chalk.ai.cache_read_tokens",
            "llm.token_count.prompt_details.cache_read",
            "gen_ai.usage.cache_read.input_tokens",
        ),
    ),
    (
        "cache_write_tokens",
        (
            "chalk.ai.cache_write_tokens",
            "llm.token_count.prompt_details.cache_write",
            "gen_ai.usage.cache_creation.input_tokens",
        ),
    ),
    (
        "reasoning_tokens",
        (
            "llm.token_count.completion_details.reasoning",
            "gen_ai.usage.reasoning.output_tokens",
        ),
    ),
]


@pytest.mark.parametrize(("property_name", "keys"), TOKEN_ATTRIBUTES)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("42", 42),
        ("42.0", 42),
        ("4.2e2", 420),
        ("9007199254740993", 9007199254740993),
    ],
)
def test_token_attributes(
    property_name: str, keys: tuple[str, ...], raw: str, expected: int
) -> None:
    for key in keys:
        assert getattr(_span(**{key: raw}), property_name) == expected


@pytest.mark.parametrize(("property_name", "keys"), TOKEN_ATTRIBUTES)
@pytest.mark.parametrize(
    "raw",
    ["", "bad", "NaN", "Infinity", "-Infinity", "-1", "1.5", "9007199254740993.5"],
)
def test_invalid_token_counts_fall_back(
    property_name: str, keys: tuple[str, ...], raw: str
) -> None:
    assert getattr(_span(**{keys[0]: raw}), property_name) is None
    assert getattr(_span(**{keys[0]: raw, keys[1]: "7"}), property_name) == 7


@pytest.mark.parametrize(("property_name", "keys"), TOKEN_ATTRIBUTES)
def test_token_precedence_preserves_zero(
    property_name: str, keys: tuple[str, ...]
) -> None:
    assert getattr(_span(), property_name) is None
    for index, key in enumerate(keys):
        attributes = {fallback: "99" for fallback in keys[index + 1 :]}
        attributes[key] = "0"
        assert getattr(_span(**attributes), property_name) == 0


def test_total_tokens_requires_both_counts_and_does_not_double_count_details() -> None:
    span = _span(
        **{
            "llm.token_count.prompt": "100",
            "gen_ai.usage.output_tokens": "50",
            "llm.token_count.prompt_details.cache_read": "20",
            "llm.token_count.prompt_details.cache_write": "10",
            "llm.token_count.completion_details.reasoning": "5",
        }
    )
    assert span.total_tokens == 150
    assert _span(**{"llm.token_count.prompt": "100"}).total_tokens is None
    assert _span(**{"llm.token_count.completion": "50"}).total_tokens is None
    assert (
        _span(
            **{"llm.token_count.prompt": "0", "llm.token_count.completion": "0"}
        ).total_tokens
        == 0
    )
    assert (
        _span(**{**span.attributes, "llm.token_count.total": "200"}).total_tokens == 200
    )
    assert _span(**{**span.attributes, "llm.token_count.total": "0"}).total_tokens == 0
    assert (
        _span(**{**span.attributes, "llm.token_count.total": "invalid"}).total_tokens
        == 150
    )


@pytest.mark.parametrize(
    "key", ["chalk.ai.cost_usd", "llm.cost.total", "llm.cost", "gen_ai.usage.cost"]
)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0.0),
        ("0.125", 0.125),
        ("1e-3", 0.001),
        ("-1", None),
        ("NaN", None),
        ("inf", None),
        ("-inf", None),
        ("1e999", None),
        ("bad", None),
    ],
)
def test_cost_attributes(key: str, raw: str, expected: float | None) -> None:
    assert _span(**{key: raw}).cost == expected


def test_cost_precedence_and_invalid_fallbacks() -> None:
    assert _span().cost is None
    keys = ("chalk.ai.cost_usd", "llm.cost.total", "llm.cost", "gen_ai.usage.cost")
    for index, key in enumerate(keys):
        attributes = {earlier: "NaN" for earlier in keys[:index]}
        attributes.update({later: "99" for later in keys[index + 1 :]})
        attributes[key] = "0"
        assert _span(**attributes).cost == 0.0


@pytest.mark.parametrize(
    ("property_name", "keys"),
    [
        (
            "model",
            (
                "chalk.ai.model",
                "gen_ai.response.model",
                "llm.model_name",
                "gen_ai.request.model",
            ),
        ),
        ("provider", ("llm.provider", "gen_ai.provider.name", "gen_ai.system")),
    ],
)
def test_model_and_provider(property_name: str, keys: tuple[str, ...]) -> None:
    assert getattr(_span(), property_name) is None
    for index, key in enumerate(keys):
        attributes = {earlier: "" for earlier in keys[:index]}
        attributes.update({later: "fallback" for later in keys[index + 1 :]})
        attributes[key] = "expected"
        assert getattr(_span(**attributes), property_name) == "expected"


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999", "bad", ""])
def test_numeric_accessors_tolerate_invalid_values(raw: str) -> None:
    span = _span(value=raw)
    assert span.attribute_float("value") is None
    assert span.attribute_int("value") is None


def test_integer_accessor_preserves_precision_and_existing_fractional_behavior() -> (
    None
):
    assert _span(value="9007199254740993").attribute_int("value") == 9007199254740993
    assert _span(value="42.0").attribute_int("value") == 42
    assert _span(value="4.2").attribute_int("value") == 4
    assert _span(value="-4.2").attribute_int("value") == -4
