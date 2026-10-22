from __future__ import annotations

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.server.v1 import environment_pb, team_pb
from tests._fakes import StubbedConnectClient


class _Client(StubbedConnectClient):
    function_dashboard_url = ConnectClient.function_dashboard_url
    _default_dashboard_url = ConnectClient._default_dashboard_url

    def __init__(self, response):
        self._env_id = "env-1"
        self._api_server = "https://api.staging.chalk.dev"
        self._response = response

    def call(self, method, request, **kwargs):
        assert method.name == "get_env"
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _env_response(**fields):
    return team_pb.GetEnvResponse(environment=environment_pb.Environment(**fields))


def test_function_dashboard_url_derives_host_from_api_server():
    client = _Client(_env_response(id="env-1", project_id="proj"))
    assert client.function_dashboard_url("my fn") == (
        "https://staging.chalk.dev/projects/proj/environments/env-1"
        "/external-functions/my%20fn"
    )


def test_function_dashboard_url_prefers_environment_dashboard_url():
    client = _Client(
        _env_response(id="env-1", project_id="proj", dashboard_url="https://d.example/")
    )
    assert client.function_dashboard_url("f").startswith("https://d.example/projects/")


def test_function_dashboard_url_is_none_when_lookup_fails():
    client = _Client(ConnectRpcError(Code.PERMISSION_DENIED, "no"))
    assert client.function_dashboard_url("f") is None


def test_function_dashboard_url_prod_host():
    client = _Client(_env_response(id="env-1", project_id="proj"))
    client._api_server = "https://api.chalk.ai"
    assert client.function_dashboard_url("f").startswith("https://chalk.ai/projects/")
