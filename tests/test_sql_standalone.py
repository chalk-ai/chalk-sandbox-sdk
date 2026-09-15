"""Run with only chalkcompute base dependencies and pytest installed."""

import os
import subprocess
import sys
from pathlib import Path


def test_standalone_execution_without_chalkpy_or_dataframes():
    # Block optional imports even when the developer's environment has them.
    script = r"""
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"chalk", "chalkdf", "pyarrow", "pandas"}:
            raise AssertionError("Optional dependency imported: " + fullname)
sys.meta_path.insert(0, BlockOptional())
from chalkcompute import SQLClient
from chalkcompute._gen.chalk.protosql.v1 import sql_service_pb as pb, sql_service_connect
from chalkcompute._gen.chalk.server.v1 import auth_pb
from chalkcompute import _connect
from protobuf import Oneof
import os
for name in ("CHALK_WEB_IDENTITY_TOKEN_FILE", "CHALK_ENVIRONMENT", "CHALK_ACTIVE_ENVIRONMENT"):
    os.environ.pop(name, None)
os.environ["CHALK_CLIENT_ID"] = "test-client"
os.environ["CHALK_CLIENT_SECRET"] = "test-secret"
os.environ["CHALK_API_SERVER"] = "https://test.invalid"
os.environ["CHALK_ENVIRONMENT_ID"] = "env-test"
class Auth:
    def __init__(self, *args, **kwargs): pass
    def get_token(self, request, **kwargs):
        assert request.client_id == "test-client"
        assert request.client_secret == "test-secret"
        return auth_pb.GetTokenResponse(access_token="test-token", primary_environment="env-test")
    def close(self): pass
class Stub:
    def execute_sql_query(self, request, **kwargs):
        assert kwargs["headers"]["authorization"] == "Bearer test-token"
        assert kwargs["headers"]["x-chalk-env-id"] == "env-test"
        pb.ExecuteSqlQueryRequest.from_binary(request.to_binary())
        return pb.ExecuteSqlQueryResponse(query_id="q", payload=Oneof("parquet", b"PAR1"))
_connect.auth_connect.AuthServiceClientSync = Auth
_connect.service_auth_context_from_env = lambda: None
connect = _connect.ConnectClient()
connect._typed_clients[sql_service_connect.SqlServiceClientSync] = Stub()
client = SQLClient(connect_client=connect)
assert client.query("select 1").query_id == "q"
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
