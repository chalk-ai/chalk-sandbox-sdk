"""End-to-end coverage for scheduled remote function registration."""

from __future__ import annotations

import uuid

import chalkcompute
from protobuf import Oneof

from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect as catalog_connect,
)
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as catalog_pb
from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import (
    _delete_function,
    _delete_test_owned_source_volume,
    _deploy_script,
    _test_owned_source_volume_name,
    needs_staging,
)


@needs_staging
def test_remote_function_registers_with_schedule() -> None:
    """Deploy the scheduled heartbeat example, verify the schedule is persisted
    in the catalog, and exercise the no-input invoke path."""
    function_name = build_scoped_name("ex-heartbeat")
    if function_name == "ex-heartbeat":
        function_name = f"ex-heartbeat-local-{uuid.uuid4().hex[:12]}"
    fn: chalkcompute.RemoteFunction | None = None
    source_volume_name: str | None = None
    try:
        _deploy_script(
            "scheduled-function", "heartbeat.py", function_name=function_name
        )

        fn = chalkcompute.RemoteFunction.from_name(function_name)
        source_volume_name = _test_owned_source_volume_name(fn, function_name)
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
    finally:
        _delete_function(function_name)
        if fn is not None and source_volume_name is not None:
            _delete_test_owned_source_volume(fn, source_volume_name)
