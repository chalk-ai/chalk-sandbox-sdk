"""End-to-end coverage for sandbox creation from a deployed function."""

from __future__ import annotations

import chalkcompute

from e2etests._ci_resources import build_scoped_name
from e2etests.conftest import _delete_function, _deploy_script, needs_staging


@needs_staging
class TestFunctionSandbox:
    function_name = build_scoped_name("ex-function-sandbox")

    @classmethod
    def setup_class(cls) -> None:
        _deploy_script(
            "function-sandbox",
            "deploy.py",
            function_name=cls.function_name,
        )

    @classmethod
    def teardown_class(cls) -> None:
        _delete_function(cls.function_name)

    def test_deployed_function_creates_sandbox_with_pod_identity(self) -> None:
        sandbox_id = chalkcompute.RemoteFunction.from_name(self.function_name).remote()

        assert isinstance(sandbox_id, str)
        assert sandbox_id
