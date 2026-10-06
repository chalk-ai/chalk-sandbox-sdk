from __future__ import annotations


import pyarrow as pa
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute import _model as model_module
from chalkcompute._gen.chalk.modeldeployment.v1 import service_pb as deploy_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._gen.chalk.server.v1 import model_registry_pb
from chalkcompute._model import (
    ModelError,
    ModelNotDeployedError,
    ModelNotFoundError,
    bind_model,
)
from tests._fakes import FakeMethod, StubbedConnectClient


class _FakeClient(StubbedConnectClient):
    """Stand-in for chalkcompute._connect.ConnectClient.

    Stores canned protobuf responses or Connect errors keyed by method name.
    """

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, object]] = []
        self._access_token = "fake-token"
        self._env_id = "fake-env"

    def call(self, method: FakeMethod, request: object, **_kwargs: object) -> object:
        self.calls.append((method.name, request))
        try:
            outcome = self._responses[method.name]
        except KeyError as exc:
            raise AssertionError(f"no canned response for {method.name}") from exc
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def _ensure_auth(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_catalog():
    from chalkdf import Catalog
    from chalkdf.libchalk.runtime.remote_call import reinitialize_global_catalog_runtime

    Catalog.reset_local()
    reinitialize_global_catalog_runtime()
    yield
    Catalog.reset_local()
    reinitialize_global_catalog_runtime()


@pytest.fixture
def _no_service_auth(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(model_module, "service_auth_context_from_env", lambda: None)


def _sg_response(
    scaling_url: str = "https://qwen3-prod-test.example.chalk.dev",
) -> sg_pb.GetScalingGroupResponse:
    return sg_pb.GetScalingGroupResponse(
        scaling_group=sg_pb.ScalingGroupResponse(
            name="qwen3-prod-test",
            web_url=scaling_url,
            status="Available",
        )
    )


def _ok_responses(
    scaling_url: str = "https://qwen3-prod-test.example.chalk.dev",
) -> dict[str, object]:
    """SG-first path: GetScalingGroup returns 200 with the URL."""
    return {
        "get_scaling_group": _sg_response(scaling_url),
    }


def _model_fallback_responses(
    scaling_url: str = "https://qwen3-prod-test.example.chalk.dev",
) -> dict[str, object]:
    """Fallback path: SG lookup 404s; resolution goes through model registry."""
    return {
        "get_scaling_group": ConnectRpcError(Code.NOT_FOUND, "not found"),
        "get_model": model_registry_pb.GetModelResponse(
            model=model_registry_pb.Model(
                latest_model_version=model_registry_pb.ModelVersion(version=7)
            )
        ),
        "list_model_scaling_groups": deploy_pb.ListModelScalingGroupsResponse(
            scaling_groups=[sg_pb.ScalingGroupResponse(web_url=scaling_url)]
        ),
    }


def test_bind_model_sg_first_path(_no_service_auth):
    from chalkdf import Catalog

    client = _FakeClient(_ok_responses())

    bind_model("qwen3-prod-test", output_type=pa.list_(pa.float32()), client=client)

    assert Catalog.local_catalog().runtime.contains("model.qwen3-prod-test")
    # Single RPC: GetScalingGroup (no model-registry fallback needed).
    assert [method for method, _request in client.calls] == ["get_scaling_group"]


def test_bind_model_falls_back_to_model_registry(_no_service_auth):
    from chalkdf import Catalog

    client = _FakeClient(_model_fallback_responses())

    bind_model("qwen3-embedding-0-6b", output_type=pa.string(), client=client)

    assert Catalog.local_catalog().runtime.contains("model.qwen3-embedding-0-6b")
    # SG-first → 404 → GetModel → ListModelScalingGroups.
    assert [method for method, _request in client.calls] == [
        "get_scaling_group",
        "get_model",
        "list_model_scaling_groups",
    ]
    list_call = client.calls[2][1]
    assert isinstance(list_call, deploy_pb.ListModelScalingGroupsRequest)
    assert list_call.model_version is not None
    assert list_call.model_version.identifier is not None
    assert list_call.model_version.identifier.identifier is not None
    assert list_call.model_version.identifier.identifier.value == 7


def test_bind_model_is_idempotent(_no_service_auth):
    client = _FakeClient(_ok_responses())

    bind_model("qwen3-prod-test", output_type=pa.string(), client=client)
    bind_model("qwen3-prod-test", output_type=pa.string(), client=client)

    # Second call short-circuits — no extra RPCs.
    assert len(client.calls) == 1


def test_explicit_version_via_model_fallback(_no_service_auth):
    responses = _model_fallback_responses()
    # With explicit version we should skip GetModel.
    responses.pop("get_model")
    client = _FakeClient(responses)

    bind_model(
        "qwen3-embedding-0-6b",
        output_type=pa.string(),
        version=3,
        client=client,
    )

    assert [method for method, _request in client.calls] == [
        "get_scaling_group",
        "list_model_scaling_groups",
    ]
    list_call = client.calls[1][1]
    assert isinstance(list_call, deploy_pb.ListModelScalingGroupsRequest)
    assert list_call.model_version is not None
    assert list_call.model_version.identifier is not None
    assert list_call.model_version.identifier.identifier is not None
    assert list_call.model_version.identifier.identifier.value == 3


def test_explicit_version_with_sg_name_raises(_no_service_auth):
    client = _FakeClient(_ok_responses())

    with pytest.raises(ModelError, match="ambiguous"):
        bind_model(
            "qwen3-prod-test",
            output_type=pa.string(),
            version=3,
            client=client,
        )


def test_qualified_name_override(_no_service_auth):
    from chalkdf import Catalog

    client = _FakeClient(_ok_responses())

    bind_model(
        "qwen3-prod-test",
        output_type=pa.string(),
        qualified_name="my-models.qwen3",
        client=client,
    )

    assert Catalog.local_catalog().runtime.contains("my-models.qwen3")
    assert not Catalog.local_catalog().runtime.contains("model.qwen3-prod-test")


def test_missing_name_raises_not_found(_no_service_auth):
    # Both SG and model registry return 404.
    client = _FakeClient(
        {
            "get_scaling_group": ConnectRpcError(Code.NOT_FOUND, "not found"),
            "get_model": ConnectRpcError(Code.NOT_FOUND, "not found"),
        }
    )

    with pytest.raises(ModelNotFoundError):
        bind_model("missing", output_type=pa.string(), client=client)


def test_model_without_scaling_group_raises(_no_service_auth):
    responses = _model_fallback_responses()
    responses["list_model_scaling_groups"] = deploy_pb.ListModelScalingGroupsResponse()
    client = _FakeClient(responses)

    with pytest.raises(ModelNotDeployedError):
        bind_model("qwen3-embedding-0-6b", output_type=pa.string(), client=client)


def test_sg_with_missing_weburl_raises(_no_service_auth):
    client = _FakeClient(
        {
            "get_scaling_group": sg_pb.GetScalingGroupResponse(
                scaling_group=sg_pb.ScalingGroupResponse(
                    name="qwen3-prod-test", status="Pending"
                )
            ),
        }
    )

    with pytest.raises(ModelNotDeployedError):
        bind_model("qwen3-prod-test", output_type=pa.string(), client=client)


def test_encode_models_env_round_trips_via_apply(
    _no_service_auth, monkeypatch: pytest.MonkeyPatch
):
    from chalkcompute._model import MODELS_ENV_VAR, encode_models_env

    encoded = encode_models_env(
        {"qwen3-prod-test": pa.list_(pa.float32()), "other-model": pa.string()}
    )
    assert encoded  # non-empty

    # Stub out `bind_model` so `apply_models_from_env` doesn't try to do RPCs;
    # we only care that the decoded spec is forwarded correctly.
    calls: list[tuple[str, pa.DataType]] = []

    def _fake_bind(name: str, *, output_type: pa.DataType, **_: object) -> None:
        calls.append((name, output_type))

    monkeypatch.setattr("chalkcompute._model.bind_model", _fake_bind)

    from chalkcompute._model import apply_models_from_env

    bound = apply_models_from_env({MODELS_ENV_VAR: encoded})
    assert bound == ["model.qwen3-prod-test", "model.other-model"]
    assert calls == [
        ("qwen3-prod-test", pa.list_(pa.float32())),
        ("other-model", pa.string()),
    ]


def test_encode_models_env_empty_returns_empty_string():
    from chalkcompute._model import encode_models_env

    assert encode_models_env({}) == ""


def test_apply_models_from_env_noop_when_unset():
    from chalkcompute._model import apply_models_from_env

    assert apply_models_from_env({}) == []


def test_encode_models_env_rejects_non_datatype():
    from chalkcompute._model import encode_models_env

    with pytest.raises(TypeError):
        encode_models_env({"foo": "not a pa.DataType"})  # type: ignore[dict-item]


def test_generate_handler_shim_includes_bootstrap_when_bind_models():
    from chalkcompute._function import _generate_handler_shim

    shim = _generate_handler_shim("compute", bind_models=True)
    assert "apply_models_from_env" in shim
    assert shim.startswith("from chalkcompute._model import apply_models_from_env")


def test_generate_handler_shim_skips_bootstrap_by_default():
    from chalkcompute._function import _generate_handler_shim

    shim = _generate_handler_shim("compute")
    assert "apply_models_from_env" not in shim


def test_remote_function_models_arg_sets_env_var_and_flags_shim(
    monkeypatch: pytest.MonkeyPatch,
):
    import os

    # Avoid touching the real chalkcompute deploy path / API server.
    monkeypatch.setenv("CHALK_REMOTE_FUNCTION", "")
    monkeypatch.delenv("CHALK_REMOTE_FUNCTION", raising=False)
    monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.invalid")
    monkeypatch.setenv("CHALK_CLIENT_ID", "x")
    monkeypatch.setenv("CHALK_CLIENT_SECRET", "x")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "x")

    from chalkcompute import RemoteFunction
    from chalkcompute._model import MODELS_ENV_VAR

    def _user_func(text: str) -> str:
        return text

    rf = RemoteFunction(_user_func, models={"qwen3-prod-test": pa.list_(pa.float32())})
    assert MODELS_ENV_VAR in rf._env  # set at construction
    assert rf._env[MODELS_ENV_VAR]  # non-empty
    assert os.environ.get(MODELS_ENV_VAR) is None  # not leaked into process env


def test_remote_class_models_arg_sets_env_var(monkeypatch: pytest.MonkeyPatch):
    import os

    monkeypatch.setenv("CHALK_REMOTE_FUNCTION", "")
    monkeypatch.delenv("CHALK_REMOTE_FUNCTION", raising=False)
    monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.invalid")
    monkeypatch.setenv("CHALK_CLIENT_ID", "x")
    monkeypatch.setenv("CHALK_CLIENT_SECRET", "x")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "x")

    from chalkcompute import RemoteClass, method
    from chalkcompute._cls import _generate_class_handler_shim
    from chalkcompute._model import MODELS_ENV_VAR

    class Worker:
        @method
        def run(self, text: str) -> str:
            return text

    rc = RemoteClass(Worker, models={"qwen3-prod-test": pa.list_(pa.float32())})
    assert MODELS_ENV_VAR in rc._env
    assert rc._env[MODELS_ENV_VAR]
    assert os.environ.get(MODELS_ENV_VAR) is None

    shim = _generate_class_handler_shim(
        class_name="Worker",
        method_name="run",
        method_has_self=True,
        before_name=None,
        before_has_self=False,
        after_name=None,
        after_has_self=False,
        bind_models=True,
    )
    assert shim.startswith("from chalkcompute._model import apply_models_from_env")


def test_unexpected_rpc_error_raises_model_error(_no_service_auth):
    client = _FakeClient(
        {"get_scaling_group": ConnectRpcError(Code.INTERNAL, "internal")}
    )

    with pytest.raises(ModelError):
        bind_model("qwen3-prod-test", output_type=pa.string(), client=client)
