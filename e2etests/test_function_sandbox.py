"""End-to-end coverage for sandbox creation from a deployed function."""

from __future__ import annotations

import chalkcompute

from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import (
    _delete_function,
    _delete_test_owned_source_volume,
    _deploy_test_function_source,
    needs_function_sandbox_identity,
    needs_staging,
)


@needs_staging
@needs_function_sandbox_identity
class TestFunctionSandbox:
    function_name = build_scoped_name("ex-function-sandbox")
    source: tuple[chalkcompute.RemoteFunction, str | None] | None = None

    @classmethod
    def setup_class(cls) -> None:
        cls.source = _deploy_test_function_source(
            "function-sandbox", "deploy.py", cls.function_name
        )

    @classmethod
    def teardown_class(cls) -> None:
        _delete_function(cls.function_name)
        if cls.source is not None and cls.source[1] is not None:
            _delete_test_owned_source_volume(cls.source[0], cls.source[1])

    def test_deployed_function_executes_in_sandbox_with_pod_identity(self) -> None:
        output = chalkcompute.RemoteFunction.from_name(self.function_name).remote()

        assert output == "sandbox-exec-ok\n"
