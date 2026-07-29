"""Wiring test: RemoteFunction.deploy() enforces the routable-hostname length limit."""

import pytest

from chalkcompute._function import RemoteFunction


def test_deploy_rejects_overlong_derived_hostname():
    fn = RemoteFunction._make_empty()
    fn._client._env_id = "test-env"
    fn._explicit_name = "a" * 60  # label "test-env-" + 60 = 69 > 63

    # match= keeps this red if deploy() reaches the build and fails for another reason.
    with pytest.raises(ValueError, match="too long"):
        fn.deploy()


def test_deploy_allows_in_budget_name_through_the_check():
    fn = RemoteFunction._make_empty()
    fn._client._env_id = "test-env"
    fn._explicit_name = "short-fn"
    try:
        fn.deploy()
    except ValueError as e:
        if "too long" in str(e):
            pytest.fail(f"in-budget name wrongly rejected by hostname check: {e}")
    except Exception:
        pass  # non-ValueError (build/network) means the check passed
