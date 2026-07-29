"""Unit tests for chalkcompute._naming — DNS label sanitization and validation."""

import pytest

from chalkcompute._naming import (
    MAX_DNS_LABEL,
    sanitize_dns_label,
    validate_dns_label,
    validate_env,
)


# ---------------------------------------------------------------------------
# validate_dns_label
# ---------------------------------------------------------------------------


class TestValidateDnsLabel:
    def test_simple_valid(self):
        validate_dns_label("my-container")

    def test_single_char(self):
        validate_dns_label("a")

    def test_all_lowercase_alphanum(self):
        validate_dns_label("abc123")

    def test_rejects_uppercase(self):
        with pytest.raises(ValueError):
            validate_dns_label("MyContainer")

    def test_rejects_underscores(self):
        with pytest.raises(ValueError):
            validate_dns_label("my_container")

    def test_rejects_leading_digit(self):
        with pytest.raises(ValueError):
            validate_dns_label("1container")

    def test_rejects_leading_hyphen(self):
        with pytest.raises(ValueError):
            validate_dns_label("-container")

    def test_rejects_trailing_hyphen(self):
        with pytest.raises(ValueError):
            validate_dns_label("container-")

    def test_rejects_dots(self):
        with pytest.raises(ValueError):
            validate_dns_label("my.container")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_dns_label("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError):
            validate_dns_label("a" * 64)

    def test_accepts_max_length(self):
        validate_dns_label("a" * 63)


# ---------------------------------------------------------------------------
# sanitize_dns_label
# ---------------------------------------------------------------------------


class TestSanitizeDnsLabel:
    def test_lowercase(self):
        assert sanitize_dns_label("MyFunc") == "myfunc"

    def test_underscores_to_hyphens(self):
        assert sanitize_dns_label("my_func") == "my-func"

    def test_dots_to_hyphens(self):
        assert sanitize_dns_label("my.func") == "my-func"

    def test_strips_invalid_chars(self):
        assert sanitize_dns_label("fn!abc") == "fnabc"

    def test_collapses_hyphens(self):
        assert sanitize_dns_label("fn---abc") == "fn-abc"

    def test_strips_leading_hyphens(self):
        assert sanitize_dns_label("---abc") == "abc"

    def test_strips_trailing_hyphens(self):
        assert sanitize_dns_label("abc---") == "abc"

    def test_truncates_to_63(self):
        result = sanitize_dns_label("a" * 100)
        assert len(result) == 63

    def test_dunder_function(self):
        # __image -> --image -> -image -> image
        assert sanitize_dns_label("__image") == "image"

    def test_dunder_only_raises(self):
        # ____ -> ---- -> - -> "" -> ValueError
        with pytest.raises(ValueError):
            sanitize_dns_label("____")

    def test_digits_only_raises(self):
        # "123" starts with a digit after sanitization
        with pytest.raises(ValueError):
            sanitize_dns_label("123")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            sanitize_dns_label("")

    def test_typical_python_function(self):
        assert sanitize_dns_label("get_user_data") == "get-user-data"

    def test_camel_case_class(self):
        assert sanitize_dns_label("MyDataProcessor") == "mydataprocessor"

    def test_already_valid(self):
        assert sanitize_dns_label("my-func") == "my-func"

    def test_mixed_separators(self):
        assert sanitize_dns_label("My_Class.method_name") == "my-class-method-name"

    def test_fn_prefix_pattern(self):
        # Simulates the auto-generated pattern: fn-{func_name}-{uuid}
        result = sanitize_dns_label("fn-my_func-abc12345")
        assert result == "fn-my-func-abc12345"

    def test_cls_prefix_pattern(self):
        # Simulates: cls-MyClass-my_method-abc12345
        result = sanitize_dns_label("cls-MyClass-my_method-abc12345")
        assert result == "cls-myclass-my-method-abc12345"

    def test_custom_max_length(self):
        result = sanitize_dns_label("abcdefghij", max_length=5)
        assert result == "abcde"

    def test_truncation_strips_trailing_hyphen(self):
        # "ab-cd" truncated to 3 would be "ab-", should strip to "ab"
        result = sanitize_dns_label("ab-cd", max_length=3)
        assert result == "ab"


# ---------------------------------------------------------------------------
# validate_dns_label with an environment_id (routable hostname length)
# ---------------------------------------------------------------------------


class TestValidateDnsLabelWithEnvironment:
    def test_short_name_accepted(self):
        validate_dns_label("my-func", "test-env")

    def test_no_environment_skips_length_check(self):
        # Without an env id, only the bare-name shape is validated.
        name = "a" + "b" * 54  # valid bare name
        validate_dns_label(name)

    def test_bare_name_valid_but_derived_label_overflows(self):
        env = "test-env"
        name = "a" + "b" * 54
        validate_dns_label(name)
        assert len(f"{env}-{name}") >= MAX_DNS_LABEL
        with pytest.raises(ValueError):
            validate_dns_label(name, env)

    def test_boundary_at_max(self):
        env = "env"
        name = "a" * (MAX_DNS_LABEL - len(env) - 2)
        assert len(f"{env}-{name}") == MAX_DNS_LABEL - 1
        validate_dns_label(name, env)

    def test_one_over_boundary_rejected(self):
        env = "env"
        name = "a" * (MAX_DNS_LABEL - len(env) - 1)
        with pytest.raises(ValueError):
            validate_dns_label(name, env)

    def test_error_message_is_actionable(self):
        with pytest.raises(ValueError) as exc_info:
            validate_dns_label("x" * 60, "test-env")
        message = str(exc_info.value)
        assert "test-env" in message
        assert str(MAX_DNS_LABEL) in message
        assert "Shorten" in message


# ---------------------------------------------------------------------------
# validate_env
# ---------------------------------------------------------------------------


class TestValidateEnv:
    def test_none_is_accepted(self):
        validate_env(None)

    def test_empty_is_accepted(self):
        validate_env({})

    def test_valid_string_mapping(self):
        validate_env({"FOO": "bar", "PORT": "8080"})

    def test_none_value_rejected_with_helpful_message(self):
        # The os.getenv("X")-of-an-unset-var case that produced the opaque
        # server-side "invalid value for string field value: null".
        with pytest.raises(TypeError) as exc_info:
            validate_env({"OPENAI_API_KEY": None})
        message = str(exc_info.value)
        assert "OPENAI_API_KEY" in message
        assert "os.getenv" in message

    def test_non_string_value_rejected(self):
        with pytest.raises(TypeError, match="PORT"):
            validate_env({"PORT": 8080})

    def test_reports_all_bad_values(self):
        with pytest.raises(TypeError) as exc_info:
            validate_env({"A": None, "B": 1, "C": "ok"})
        message = str(exc_info.value)
        assert "A" in message and "B" in message

    def test_non_string_key_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_env({1: "v"})

    def test_empty_key_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_env({"": "v"})
