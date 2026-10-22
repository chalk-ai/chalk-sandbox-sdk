"""End-to-end coverage for scheduled remote function registration."""

from __future__ import annotations

import chalkcompute
from protobuf import Oneof

from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect as catalog_connect,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import _delete_function, _deploy_script, needs_staging


@needs_staging
def test_remote_function_registers_with_schedule() -> None:
    function_name = build_scoped_name("ex-heartbeat")
    try:
        _assert_scheduled_function(function_name)
    finally:
        _delete_function(function_name)


def _assert_scheduled_function(function_name: str) -> None:
    """Deploy the scheduled heartbeat example, verify the schedule is persisted
    in the catalog, and exercise the no-input invoke path."""
    _deploy_script("scheduled-function", "heartbeat.py", function_name=function_name)

    fn = chalkcompute.RemoteFunction.from_name(function_name)
    assert fn.version_info is not None

    stub = fn._client.stub(catalog_connect.ExternalFunctionCatalogServiceClientSync)
    response = fn._client.call(
        stub.get_external_function_version,
        catalog_pb.GetExternalFunctionVersionRequest(
            identifier=Oneof("id", fn.version_info.id)
        ),
    )
    version = response.external_function_version
    assert version is not None
    assert version.config is not None
    assert version.config.schedule == "10m"

    assert fn.remote() is None
