import importlib.util
import subprocess
import sys

import pytest

from chalkcompute._connect import _parse_request, grpc_target_from_url


class TestGrpcTargetFromUrl:
    def test_https_defaults_to_443(self):
        target, use_tls = grpc_target_from_url("https://api.chalk.ai")

        assert target == "api.chalk.ai:443"
        assert use_tls is True

    def test_https_preserves_explicit_port(self):
        target, use_tls = grpc_target_from_url("https://api.chalk.ai:7443/path")

        assert target == "api.chalk.ai:7443"
        assert use_tls is True

    def test_http_uses_insecure_fallback_port(self):
        target, use_tls = grpc_target_from_url(
            "http://function.example.com/run",
            insecure_port=8080,
        )

        assert target == "function.example.com:8080"
        assert use_tls is False

    def test_http_preserves_explicit_port(self):
        target, use_tls = grpc_target_from_url(
            "http://function.example.com:9000/run",
            insecure_port=8080,
        )

        assert target == "function.example.com:9000"
        assert use_tls is False


class TestConnectRequestParsing:
    def test_parses_json_dict_into_generated_proto(self):
        request = _parse_request(
            "chalk.container.v1.ContainerService",
            "RunContainer",
            {
                "container": {
                    "spec": {
                        "name": "network-test",
                        "image": "ubuntu:latest",
                        "networkPolicy": {
                            "allowedRoutes": [{"route": "10.0.0.0/8"}]
                        },
                    }
                }
            },
        )

        assert request.container.spec.name == "network-test"
        assert request.container.spec.network_policy.allowed_routes[0].route == (
            "10.0.0.0/8"
        )

    def test_dynamic_connect_protos_do_not_conflict_with_chalkpy(self):
        if importlib.util.find_spec("chalk") is None:
            pytest.skip("chalkpy not installed")

        code = """
from chalkcompute._connect import _parse_request

_parse_request(
    "chalk.externalfunctioncatalog.v1.ExternalFunctionCatalogService",
    "GetExternalFunctionVersion",
    {"key": {"functionName": "orchestrate"}},
)
import chalkcompute._gen.chalk.auth.v1.permissions_pb2
import chalk_remote_call._gen.chalk.runtime.v1.remote_python_call_pb2
import chalk.client.client_grpc
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
