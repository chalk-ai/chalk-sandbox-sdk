from __future__ import annotations


import pyarrow as pa
import pytest

from chalkcompute import _model as model_module
from chalkcompute._model import (
    ModelError,
    ModelNotDeployedError,
    ModelNotFoundError,
    bind_model,
)


class _FakeResponse:
    def __init__(
        self, status_code: int, payload: dict | None = None, text: str | None = None
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text if text is not None else ""

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stand-in for chalkcompute._connect.ConnectClient.

    Stores a queue of canned RPC responses keyed by ``(service, method)``.
    """

    def __init__(self, responses: dict[tuple[str, str], _FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict]] = []
        self._access_token = "fake-token"
        self._env_id = "fake-env"

    def rpc(self, service: str, method: str, payload: dict) -> _FakeResponse:
        self.calls.append((service, method, payload))
        try:
            return self._responses[(service, method)]
        except KeyError:
            return _FakeResponse(
                status_code=500, text=f"no canned response for {service}/{method}"
            )

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
) -> _FakeResponse:
    return _FakeResponse(
        200,
        {
            "scalingGroup": {
                "name": "qwen3-prod-test",
                "webUrl": scaling_url,
                "status": "Available",
            }
        },
    )


def _ok_responses(
    scaling_url: str = "https://qwen3-prod-test.example.chalk.dev",
) -> dict[tuple[str, str], _FakeResponse]:
    """SG-first path: GetScalingGroup returns 200 with the URL."""
    return {
        (
            "chalk.scalinggroup.v1.ScalingGroupManagerService",
            "GetScalingGroup",
        ): _sg_response(scaling_url),
    }


def _model_fallback_responses(
    scaling_url: str = "https://qwen3-prod-test.example.chalk.dev",
) -> dict[tuple[str, str], _FakeResponse]:
    """Fallback path: SG lookup 404s; resolution goes through model registry."""
    return {
        (
            "chalk.scalinggroup.v1.ScalingGroupManagerService",
            "GetScalingGroup",
        ): _FakeResponse(404, text='{"code":"not_found"}'),
        ("chalk.server.v1.ModelRegistryService", "GetModel"): _FakeResponse(
            200, {"model": {"latestModelVersion": {"version": 7}}}
        ),
        (
            "chalk.modeldeployment.v1.ModelDeploymentService",
            "ListModelScalingGroups",
        ): _FakeResponse(200, {"scalingGroups": [{"webUrl": scaling_url}]}),
    }


def test_bind_model_sg_first_path(_no_service_auth):
    from chalkdf import Catalog

    client = _FakeClient(_ok_responses())

    bind_model("qwen3-prod-test", output_type=pa.list_(pa.float32()), client=client)

    assert Catalog.local_catalog().runtime.contains("model.qwen3-prod-test")
    # Single RPC: GetScalingGroup (no model-registry fallback needed).
    assert [c[1] for c in client.calls] == ["GetScalingGroup"]


def test_bind_model_falls_back_to_model_registry(_no_service_auth):
    from chalkdf import Catalog

    client = _FakeClient(_model_fallback_responses())

    bind_model("qwen3-embedding-0-6b", output_type=pa.string(), client=client)

    assert Catalog.local_catalog().runtime.contains("model.qwen3-embedding-0-6b")
    # SG-first → 404 → GetModel → ListModelScalingGroups.
    assert [c[1] for c in client.calls] == [
        "GetScalingGroup",
        "GetModel",
        "ListModelScalingGroups",
    ]
    list_call = client.calls[2][2]
    assert list_call["modelVersion"]["identifier"] == {"version": 7}


def test_bind_model_is_idempotent(_no_service_auth):
    client = _FakeClient(_ok_responses())

    bind_model("qwen3-prod-test", output_type=pa.string(), client=client)
    bind_model("qwen3-prod-test", output_type=pa.string(), client=client)

    # Second call short-circuits — no extra RPCs.
    assert len(client.calls) == 1


def test_explicit_version_via_model_fallback(_no_service_auth):
    responses = _model_fallback_responses()
    # With explicit version we should skip GetModel.
    responses.pop(("chalk.server.v1.ModelRegistryService", "GetModel"))
    client = _FakeClient(responses)

    bind_model(
        "qwen3-embedding-0-6b",
        output_type=pa.string(),
        version=3,
        client=client,
    )

    assert [c[1] for c in client.calls] == ["GetScalingGroup", "ListModelScalingGroups"]
    list_call = client.calls[1][2]
    assert list_call["modelVersion"]["identifier"] == {"version": 3}


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
            (
                "chalk.scalinggroup.v1.ScalingGroupManagerService",
                "GetScalingGroup",
            ): _FakeResponse(404, text='{"code":"not_found"}'),
            ("chalk.server.v1.ModelRegistryService", "GetModel"): _FakeResponse(
                404, text='{"code":"not_found"}'
            ),
        }
    )

    with pytest.raises(ModelNotFoundError):
        bind_model("missing", output_type=pa.string(), client=client)


def test_model_without_scaling_group_raises(_no_service_auth):
    responses = _model_fallback_responses()
    responses[
        ("chalk.modeldeployment.v1.ModelDeploymentService", "ListModelScalingGroups")
    ] = _FakeResponse(200, {"scalingGroups": []})
    client = _FakeClient(responses)

    with pytest.raises(ModelNotDeployedError):
        bind_model("qwen3-embedding-0-6b", output_type=pa.string(), client=client)


def test_sg_with_missing_weburl_raises(_no_service_auth):
    client = _FakeClient(
        {
            (
                "chalk.scalinggroup.v1.ScalingGroupManagerService",
                "GetScalingGroup",
            ): _FakeResponse(
                200, {"scalingGroup": {"name": "qwen3-prod-test", "status": "Pending"}}
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
    monkeypatch.setenv("_CHALK_REMOTE_FUNCTION", "")
    monkeypatch.delenv("_CHALK_REMOTE_FUNCTION", raising=False)
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


def test_unexpected_rpc_error_raises_model_error(_no_service_auth):
    client = _FakeClient(
        {
            (
                "chalk.scalinggroup.v1.ScalingGroupManagerService",
                "GetScalingGroup",
            ): _FakeResponse(500, text='{"code":"internal"}')
        }
    )

    with pytest.raises(ModelError):
        bind_model("qwen3-prod-test", output_type=pa.string(), client=client)
