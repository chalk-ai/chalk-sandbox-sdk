"""End-to-end coverage for deployed-function retries and result retention."""

from __future__ import annotations

import time
import uuid

import chalkcompute
import pytest
from protobuf import Oneof

from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect as catalog_connect,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import _delete_function, _deploy_script, needs_staging


def _defer_when_ready(
    fn: chalkcompute.RemoteFunction, token: str
) -> chalkcompute.FunctionCallHandle:
    """Enqueue after the catalog has attached the function's scaling group."""
    deadline = time.monotonic() + 120
    while True:
        try:
            return fn.defer(token)
        except RuntimeError as exc:
            if "No deployed scaling group serves" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _deployed_config(fn: chalkcompute.RemoteFunction):
    """Fetch the exact configuration persisted for a deployed function."""
    assert fn.version_info is not None

    stub = fn._client.stub(catalog_connect.ExternalFunctionCatalogServiceClientSync)
    response = fn._client.call(
        stub.get_external_function_version,
        catalog_pb.GetExternalFunctionVersionRequest(
            identifier=Oneof("id", fn.version_info.id)
        ),
    )
    return response.external_function_version.config


@needs_staging
class TestFunctionRetries:
    function_name = build_scoped_name("ex-retry")

    @classmethod
    def setup_class(cls) -> None:
        _deploy_script("scalar", "deploy_retry.py", function_name=cls.function_name)

    @classmethod
    def teardown_class(cls) -> None:
        _delete_function(cls.function_name)

    def test_deployed_function_retries_failed_queue_entry(self) -> None:
        """The catalog stores the policy and retries a failed queue entry."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        config = _deployed_config(fn)
        assert config.retry_policy.max_retries == 1

        token = f"retry-{uuid.uuid4().hex}"
        assert _defer_when_ready(fn, token).get(timeout=120) == f"{token}:attempt-2"


@needs_staging
class TestFunctionResultTtl:
    function_name = build_scoped_name("ex-result-ttl")

    @classmethod
    def setup_class(cls) -> None:
        _deploy_script(
            "scalar", "deploy_result_ttl.py", function_name=cls.function_name
        )

    @classmethod
    def teardown_class(cls) -> None:
        _delete_function(cls.function_name)

    def test_completed_results_expire_after_configured_ttl(self) -> None:
        """A completed deferred result becomes unavailable after its TTL."""
        fn = chalkcompute.RemoteFunction.from_name(self.function_name)
        config = _deployed_config(fn)
        assert config.queue.result_ttl_seconds == 2

        token = f"ttl-{uuid.uuid4().hex}"
        handle = _defer_when_ready(fn, token)
        assert handle.get(timeout=60) == token

        # Give Redis expiry and the queue status transition time to complete.
        time.sleep(4)
        with pytest.raises(chalkcompute.FunctionError, match="results have expired"):
            handle.get(timeout=30)
