from __future__ import annotations

import base64
import binascii
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
    ModelBindingSpec,
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
        if callable(outcome):
            return outcome(request)
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


def test_missing_model_with_deployment_selector_skips_group_lookup(_no_service_auth):
    client = _FakeClient({"get_model": ConnectRpcError(Code.NOT_FOUND, "not found")})

    with pytest.raises(ModelNotFoundError, match="Model 'missing' was not found"):
        bind_model(
            "missing",
            output_type=pa.string(),
            deployment_name="prod",
            client=client,
        )
    assert [method for method, _ in client.calls] == ["get_model"]


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


def _deployment_page(
    *names: str,
    shape: str = "typed",
    version: int = 7,
    next_cursor: str | None = None,
) -> deploy_pb.ListModelScalingGroupsResponse:
    records = []
    groups = []
    revisions = []
    for name in names:
        group_id = f"deployment-{name}"
        revision_id = f"revision-{name}"
        url = f"https://{name}.example.chalk.dev"
        groups.append(
            sg_pb.ScalingGroupResponse(
                id=group_id,
                name=name,
                revision_id=revision_id,
                web_url=url,
            )
        )
        records.append(
            deploy_pb.ModelScalingGroup(
                id=group_id,
                name=name,
                model_id="model-id",
                revision_id=revision_id,
                web_url=url,
            )
            if shape != "intermediate"
            else deploy_pb.ModelScalingGroup(
                id=group_id,
                name=name,
                model_id="model-id",
            )
        )
        revisions.append(
            deploy_pb.ModelScalingGroupRevision(
                id=revision_id,
                model_scaling_group_id=group_id,
                model_version=version,
                # A renamed model can retain its historical name in revision specs.
                spec=deploy_pb.ModelScalingGroupSpec(
                    model_version=deploy_pb.ModelVersionSelector(model_name="old-name"),
                ),
            )
        )
    return deploy_pb.ListModelScalingGroupsResponse(
        model_scaling_groups=records if shape != "legacy" else [],
        scaling_groups=groups if shape != "typed" else [],
        current_revisions=revisions if shape in ("typed", "both") else [],
        next_cursor=next_cursor,
    )


def _selection_client(page: object) -> _FakeClient:
    responses = _model_fallback_responses()
    responses["list_model_scaling_groups"] = page
    return _FakeClient(responses)


def _resolve(client: _FakeClient, *, version=7, deployment_name=None):
    return model_module._resolve_web_url(
        client,
        "embedding",
        version=version,
        deployment_name=deployment_name,
    )


@pytest.mark.parametrize("shape", ["typed", "both", "intermediate", "legacy"])
def test_unique_deployment_response_generations(shape):
    client = _selection_client(_deployment_page("prod", shape=shape))
    assert _resolve(client) == "https://prod.example.chalk.dev"
    request = client.calls[-1][1]
    assert request.model_version.model_name == "embedding"
    assert request.model_version.identifier.identifier.value == 7
    assert request.include_deleted is False


@pytest.mark.parametrize("shape", ["typed", "both", "intermediate", "legacy"])
def test_explicit_deployment_selects_exact_name(shape):
    client = _selection_client(_deployment_page("prod-canary", "prod", shape=shape))
    assert _resolve(client, deployment_name="prod") == "https://prod.example.chalk.dev"
    assert [method for method, _ in client.calls] == ["list_model_scaling_groups"]


def test_explicit_deployment_bind_preserves_catalog_name_and_idempotence(
    _no_service_auth,
):
    from chalkdf import Catalog

    client = _selection_client(_deployment_page("prod"))
    bind_model(
        "embedding", output_type=pa.string(), deployment_name="prod", client=client
    )
    bind_model(
        "embedding", output_type=pa.string(), deployment_name="another", client=client
    )
    assert Catalog.local_catalog().runtime.contains("model.embedding")
    assert not Catalog.local_catalog().runtime.contains("model.prod")
    assert [method for method, _ in client.calls] == [
        "get_model",
        "list_model_scaling_groups",
    ]
    assert client.calls[-1][1].model_version.identifier.identifier.value == 7


@pytest.mark.parametrize("deployment_name", [None, "prod"])
def test_unique_deployment_on_later_page(deployment_name):
    pages = {
        "": _deployment_page(next_cursor="second"),
        "second": _deployment_page("prod"),
    }
    client = _selection_client(lambda request: pages[request.cursor])
    assert (
        _resolve(client, deployment_name=deployment_name)
        == "https://prod.example.chalk.dev"
    )
    requests = [
        request
        for method, request in client.calls
        if method == "list_model_scaling_groups"
    ]
    assert [request.cursor for request in requests] == ["", "second"]
    assert all(
        request.model_version.identifier.identifier.value == 7 for request in requests
    )


def test_ambiguity_checks_later_pages_and_names_candidates():
    pages = {
        "": _deployment_page("z-prod", next_cursor="second"),
        "second": _deployment_page("a-canary"),
    }
    client = _selection_client(lambda request: pages[request.cursor])
    with pytest.raises(ModelError, match="a-canary, z-prod.*deployment_name"):
        _resolve(client)


def test_distinct_deployments_with_same_url_remain_ambiguous():
    page = _deployment_page("prod", "canary")
    page.model_scaling_groups[1].web_url = page.model_scaling_groups[0].web_url
    with pytest.raises(ModelError, match="multiple deployments"):
        _resolve(_selection_client(page))


def test_identical_deployment_repeated_across_pages_is_not_ambiguous():
    pages = {
        "": _deployment_page("prod", next_cursor="second"),
        "second": _deployment_page("prod"),
    }
    client = _selection_client(lambda request: pages[request.cursor])
    assert _resolve(client) == "https://prod.example.chalk.dev"


def test_conflicting_duplicate_deployment_fails():
    second = _deployment_page("prod")
    second.model_scaling_groups[0].web_url = "https://changed.example.chalk.dev"
    pages = {"": _deployment_page("prod", next_cursor="second"), "second": second}
    with pytest.raises(ModelError, match="conflicting rows"):
        _resolve(_selection_client(lambda request: pages[request.cursor]))


@pytest.mark.parametrize("missing", ["empty", "url", "deleted", "named"])
def test_no_eligible_deployment(missing):
    from protobuf.wkt import Timestamp

    page = _deployment_page() if missing == "empty" else _deployment_page("prod")
    selector = None
    if missing == "url":
        page.model_scaling_groups[0].web_url = ""
    elif missing == "deleted":
        page.model_scaling_groups[0].deleted_at = Timestamp(seconds=1)
    elif missing == "named":
        selector = "another"
    with pytest.raises(ModelNotDeployedError, match="embedding.*v7"):
        _resolve(_selection_client(page), deployment_name=selector)


def test_explicit_empty_typed_url_does_not_use_legacy_url():
    page = _deployment_page("prod", shape="both")
    page.model_scaling_groups[0].web_url = ""
    with pytest.raises(ModelNotDeployedError):
        _resolve(_selection_client(page))


def test_explicit_version_with_no_deployment_match_fails(_no_service_auth):
    client = _selection_client(_deployment_page())
    with pytest.raises(ModelNotDeployedError, match="deployment 'prod'.*embedding.*v3"):
        bind_model(
            "embedding",
            output_type=pa.string(),
            deployment_name="prod",
            version=3,
            client=client,
        )
    assert [method for method, _ in client.calls] == ["list_model_scaling_groups"]
    assert client.calls[-1][1].model_version.identifier.identifier.value == 3


def test_response_with_wrong_selected_version_fails():
    with pytest.raises(ModelError, match="serves v6.*v7"):
        _resolve(_selection_client(_deployment_page("prod", version=6)))


@pytest.mark.parametrize("cursors", [("first", "first"), ("first", "second", "first")])
def test_repeated_cursor_fails_without_looping(cursors):
    pages = {
        request_cursor: _deployment_page("prod", next_cursor=next_cursor)
        for request_cursor, next_cursor in zip(("", *cursors[:-1]), cursors)
    }
    client = _selection_client(lambda request: pages[request.cursor])
    with pytest.raises(ModelError, match="repeated cursor"):
        _resolve(client)
    assert sum(
        method == "list_model_scaling_groups" for method, _ in client.calls
    ) == len(cursors)


@pytest.mark.parametrize(
    "bad_field",
    [
        "deployment",
        "selected_revision",
        "revision_owner",
        "revision_id",
        "list_length",
        "revision_length",
    ],
)
def test_misaligned_response_fails(bad_field):
    page = _deployment_page("prod", shape="both")
    if bad_field == "deployment":
        page.scaling_groups[0].id = "another-deployment"
    elif bad_field == "selected_revision":
        page.scaling_groups[0].revision_id = "another-revision"
    elif bad_field == "revision_owner":
        page.current_revisions[0].model_scaling_group_id = "another-deployment"
    elif bad_field == "revision_id":
        page.current_revisions[0].id = "another-revision"
    elif bad_field == "list_length":
        page.scaling_groups.append(sg_pb.ScalingGroupResponse(id="extra"))
    elif bad_field == "revision_length":
        page.current_revisions.append(deploy_pb.ModelScalingGroupRevision(id="extra"))
    with pytest.raises(ModelError, match="misaligned"):
        _resolve(_selection_client(page))


@pytest.mark.parametrize("method", ["get_model", "list_model_scaling_groups"])
def test_model_path_rpc_errors_preserve_cause(method):
    responses = _model_fallback_responses()
    failure = ConnectRpcError(Code.PERMISSION_DENIED, "denied")
    responses[method] = failure
    with pytest.raises(ModelError) as error:
        _resolve(_FakeClient(responses), version=None)
    assert error.value.__cause__ is failure


def test_model_without_published_versions_fails():
    responses = _model_fallback_responses()
    responses["get_model"] = model_registry_pb.GetModelResponse(
        model=model_registry_pb.Model()
    )
    with pytest.raises(ModelNotDeployedError, match="no published versions"):
        _resolve(_FakeClient(responses), version=None)


@pytest.mark.parametrize(
    "kwargs, error_type",
    [
        ({"deployment_name": ""}, ValueError),
        ({"deployment_name": 3}, TypeError),
        ({"version": 0}, ValueError),
        ({"version": -1}, ValueError),
        ({"version": 2**63}, ValueError),
        ({"version": True}, TypeError),
        ({"version": 1.5}, TypeError),
    ],
)
def test_selector_validation_shared_by_bind_and_spec(kwargs, error_type):
    with pytest.raises(error_type):
        ModelBindingSpec(output_type=pa.string(), **kwargs)
    client = _FakeClient({})
    with pytest.raises(error_type):
        bind_model("embedding", output_type=pa.string(), client=client, **kwargs)
    assert client.calls == []


def test_legacy_payload_encoding_remains_identical():
    schema = pa.schema([pa.field("embedding", pa.list_(pa.float32()))])
    old_payload = base64.b64encode(schema.serialize().to_pybytes()).decode("ascii")
    assert (
        model_module.encode_models_env({"embedding": pa.list_(pa.float32())})
        == old_payload
    )
    assert (
        model_module.encode_models_env(
            {"embedding": ModelBindingSpec(pa.list_(pa.float32()))}
        )
        == old_payload
    )


@pytest.mark.parametrize(
    "dtype", [pa.string(), pa.list_(pa.float32()), pa.struct([("score", pa.float64())])]
)
def test_selector_payload_round_trip_and_old_reader_rejection(dtype, monkeypatch):
    payload = model_module.encode_models_env(
        {
            "embedding": ModelBindingSpec(dtype, deployment_name="prod", version=7),
            "plain": pa.string(),
        }
    )
    assert payload.startswith("v2:")
    calls = []
    monkeypatch.setattr(
        model_module, "bind_model", lambda name, **kwargs: calls.append((name, kwargs))
    )
    assert model_module.apply_models_from_env(
        {model_module.MODELS_ENV_VAR: payload}
    ) == ["model.embedding", "model.plain"]
    assert calls == [
        ("embedding", {"output_type": dtype, "deployment_name": "prod", "version": 7}),
        (
            "plain",
            {"output_type": pa.string(), "deployment_name": None, "version": None},
        ),
    ]
    # Execute the previous SDK's decode steps to prove it cannot ignore selectors.
    with pytest.raises((binascii.Error, OSError, pa.ArrowException)):
        pa.ipc.read_schema(pa.BufferReader(base64.b64decode(payload)))


@pytest.mark.parametrize("options", [{"deployment_name": "prod"}, {"version": 3}])
def test_each_selector_is_preserved_independently(options, monkeypatch):
    payload = model_module.encode_models_env(
        {"embedding": ModelBindingSpec(pa.string(), **options)}
    )
    calls = []
    monkeypatch.setattr(
        model_module, "bind_model", lambda name, **kwargs: calls.append(kwargs)
    )
    model_module.apply_models_from_env({model_module.MODELS_ENV_VAR: payload})
    assert (
        calls[0]
        == {"output_type": pa.string(), "deployment_name": None, "version": None}
        | options
    )


@pytest.mark.parametrize("payload", ["v3:AAAA", "v2:invalid!", "not-arrow"])
def test_invalid_payload_fails_before_binding(payload, monkeypatch):
    calls = []
    monkeypatch.setattr(
        model_module, "bind_model", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(ModelError):
        model_module.apply_models_from_env({model_module.MODELS_ENV_VAR: payload})
    assert calls == []


def test_all_bindings_validated_before_any_are_applied(monkeypatch):
    schema = pa.schema(
        [
            pa.field("valid", pa.string()),
            pa.field("invalid", pa.string(), metadata={b"chalkcompute.version": b"-1"}),
        ]
    )
    payload = "v2:" + base64.b64encode(schema.serialize().to_pybytes()).decode("ascii")
    calls = []
    monkeypatch.setattr(
        model_module, "bind_model", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(ModelError, match="positive int"):
        model_module.apply_models_from_env({model_module.MODELS_ENV_VAR: payload})
    assert calls == []


def test_unversioned_selector_metadata_is_rejected():
    payload = model_module.encode_models_env(
        {"embedding": ModelBindingSpec(pa.string(), deployment_name="prod")}
    )
    with pytest.raises(ModelError, match="require a v2: payload"):
        model_module.apply_models_from_env({model_module.MODELS_ENV_VAR: payload[3:]})


@pytest.mark.parametrize("entry_point", ["function", "cls"])
def test_decorator_selectors_reach_shared_bind_path(
    entry_point, monkeypatch, no_deploy
):
    import os
    import chalkcompute as cc

    monkeypatch.delenv("CHALK_REMOTE_FUNCTION", raising=False)
    monkeypatch.delenv(model_module.MODELS_ENV_VAR, raising=False)
    monkeypatch.setenv("CHALK_API_SERVER", "https://api.example.invalid")
    monkeypatch.setenv("CHALK_CLIENT_ID", "x")
    monkeypatch.setenv("CHALK_CLIENT_SECRET", "x")
    monkeypatch.setenv("CHALK_ENVIRONMENT_ID", "x")
    monkeypatch.setattr(cc.RemoteClass, "deploy", lambda self: self)
    bindings = {
        "embedding": cc.ModelBindingSpec(
            pa.list_(pa.float32()), deployment_name="prod", version=7
        )
    }
    if entry_point == "function":

        @cc.function(models=bindings)
        def pipeline(text: str) -> str:
            return text

        remote = pipeline
    else:

        @cc.cls(models=bindings)
        class Worker:
            @cc.method
            def run(self, text: str) -> str:
                return text

        remote = Worker
    payload = remote._env[model_module.MODELS_ENV_VAR]
    calls = []
    monkeypatch.setattr(
        model_module, "bind_model", lambda name, **kwargs: calls.append((name, kwargs))
    )
    assert model_module.apply_models_from_env(
        {model_module.MODELS_ENV_VAR: payload}
    ) == ["model.embedding"]
    assert calls == [
        (
            "embedding",
            {
                "output_type": pa.list_(pa.float32()),
                "deployment_name": "prod",
                "version": 7,
            },
        )
    ]
    assert os.environ.get(model_module.MODELS_ENV_VAR) is None
