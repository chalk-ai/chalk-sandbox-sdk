from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

import chalkcompute as cc
from chalkcompute.router import Connection, Target, Route
from chalkcompute._router import RouterJudge as LegacyJudge
from chalkcompute.router import RoutingRequest
from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.router.v1 import model_route_pb as pb
from chalkcompute._gen.chalk.router.v1 import provider_connection_pb as cpb


@pytest.fixture
def backend():
    connect = MagicMock(spec=ConnectClient)
    stub = connect.stub.return_value

    def call(method, request):
        # Cross the actual protobuf wire boundary, including oneofs and presence.
        request = type(request).from_binary(request.to_binary())
        response = method(request)
        return type(response).from_binary(response.to_binary())

    connect.call.side_effect = call
    stub.list_model_routes.return_value = pb.ListModelRoutesResponse()
    stub.create_model_route.return_value = pb.CreateModelRouteResponse(
        route=pb.ModelRoute(
            id="route-1",
            public_model_id="chalk/assistant",
            published=True,
            diagnostic=pb.RouteDiagnostic.READY,
        )
    )
    stub.update_model_route.return_value = pb.UpdateModelRouteResponse(
        route=pb.ModelRoute(
            id="route-1", public_model_id="chalk/assistant", published=False
        )
    )
    return cc.RouterClient(connect_client=connect), stub, connect


SMALL = Target("connection-a", "small")
LARGE = Target("connection-b", "vendor/large")
BACKUP = Target("connection-c", "backup")


def test_definition_is_lazy_and_deploy_returns_same_handle(backend):
    client, stub, connect = backend
    router = cc.Router("assistant", default=SMALL)
    assert router.id is None
    assert router.info is None
    connect.stub.assert_not_called()
    assert router.deploy(client=client) is router
    assert router.id == "route-1"
    assert router.model == "chalk/assistant"
    assert router.info.diagnostic == "ready"
    request = stub.create_model_route.call_args.args[0]
    assert request.connection_id == SMALL.connection_id
    assert request.upstream_model_id == SMALL.model
    assert request.published
    assert request.routing is None


def test_repeat_deploy_finds_later_page_and_clears_previous_grid(backend):
    client, stub, _ = backend
    stub.list_model_routes.side_effect = [
        pb.ListModelRoutesResponse(
            routes=[pb.ModelRoute(id="other", public_model_id="chalk/other")],
            next_cursor="page-2",
        ),
        pb.ListModelRoutesResponse(
            routes=[
                pb.ModelRoute(
                    id="route-1",
                    public_model_id="chalk/assistant",
                    routing=pb.RouteGrid(),
                )
            ]
        ),
    ]
    router = cc.Router("chalk/assistant", default=LARGE, published=False).deploy(
        client=client
    )
    stub.create_model_route.assert_not_called()
    assert stub.list_model_routes.call_args_list[1].args[0].cursor == "page-2"
    request = stub.update_model_route.call_args.args[0]
    assert request.id == "route-1"
    assert set(request.update_mask.paths) == {
        "connection_id",
        "upstream_model_id",
        "published",
        "routing",
    }
    assert request.update.routing is None
    assert request.update.has_field("published") and request.update.published is False
    assert request.update.upstream_model_id == "vendor/large"
    assert router.info.published is False


def test_judge_and_fallback_presence_survive_serialization_and_snapshot(backend):
    client, stub, _ = backend
    fallbacks = [BACKUP]
    judge = LegacyJudge.heuristic(
        easy=SMALL,
        hard=Route(LARGE, fallbacks=[]),
        use_default_keywords=False,
        code_blocks_are_hard=False,
        max_easy_input_bytes=0,
    )
    router = cc.Router("assistant", default=SMALL, fallbacks=fallbacks, judge=judge)
    fallbacks.clear()
    router.deploy(client=client)
    grid = stub.create_model_route.call_args.args[0].routing
    assert grid.default_target.connection_id == SMALL.connection_id
    assert grid.fallbacks["connection-a/small"].models == ["connection-c/backup"]
    assert grid.judge.outcomes["0"].fallbacks is None
    assert grid.judge.outcomes["1"].fallbacks is not None
    assert grid.judge.outcomes["1"].fallbacks.models == []
    heuristic = grid.judge.backend.kind.value
    assert (
        heuristic.has_field("use_default_keywords")
        and heuristic.use_default_keywords is False
    )
    assert (
        heuristic.has_field("code_blocks_are_hard")
        and heuristic.code_blocks_are_hard is False
    )
    assert (
        heuristic.has_field("max_easy_input_bytes")
        and heuristic.max_easy_input_bytes == 0
    )


@pytest.mark.parametrize(
    "factory,kind",
    [
        (LegacyJudge.completion, "completion"),
        (LegacyJudge.decisions, "decisions"),
    ],
)
def test_model_based_judges(backend, factory, kind):
    client, stub, _ = backend
    judge = factory(
        SMALL,
        classification="task",
        mode="shadow",
        outcomes={
            "coding": Route(
                LARGE, description="Write or review code", fallbacks=[BACKUP]
            ),
            "other": SMALL,
        },
    )
    cc.Router("assistant", default=SMALL, judge=judge).deploy(client=client)
    saved = stub.create_model_route.call_args.args[0].routing.judge
    assert saved.mode == pb.JudgeMode.SHADOW
    assert saved.classification == pb.Classification.TASK
    assert saved.backend.kind.field == kind
    assert saved.backend.kind.value.target.connection_id == SMALL.connection_id
    assert saved.backend.kind.value.timeout_ms == 0  # server default
    assert saved.outcomes["coding"].description == "Write or review code"
    assert saved.outcomes["coding"].fallbacks.models == ["connection-c/backup"]


def test_plain_fallback_does_not_create_judge(backend):
    client, stub, _ = backend
    cc.Router("assistant", default=SMALL, fallbacks=[LARGE, BACKUP]).deploy(
        client=client
    )
    grid = stub.create_model_route.call_args.args[0].routing
    assert grid.judge is None
    assert grid.fallbacks["connection-a/small"].models == [
        "connection-b/vendor/large",
        "connection-c/backup",
    ]


def test_connections_page_and_resolve_by_name(backend):
    client, stub, _ = backend
    stub.list_provider_connections.side_effect = [
        cpb.ListProviderConnectionsResponse(next_cursor="next"),
        cpb.ListProviderConnectionsResponse(
            connections=[
                cpb.ProviderConnection(
                    id="immutable",
                    name="team-openai",
                    provider_kind="openai",
                    routing_enabled=True,
                )
            ]
        ),
    ]
    connection = client.connection("team-openai")
    assert connection.model("gpt-4.1-mini") == Target("immutable", "gpt-4.1-mini")
    assert stub.list_provider_connections.call_args.args[0].cursor == "next"


def test_connection_create_uses_explicit_exposure_and_does_not_return_key(backend):
    client, stub, _ = backend
    stub.create_provider_connection.return_value = cpb.CreateProviderConnectionResponse(
        connection=cpb.ProviderConnection(
            id="new", name="team", provider_kind="openai", routing_enabled=True
        )
    )
    result = client.create_connection(
        "team", provider="openai", api_key="test-only-value"
    )
    request = stub.create_provider_connection.call_args.args[0]
    assert request.exposure == cpb.ExposurePolicy.EXPLICIT
    assert request.routing_enabled
    assert request.api_key == "test-only-value"
    assert "test-only-value" not in repr(result)


def test_missing_and_ambiguous_connections(backend):
    client, stub, _ = backend
    stub.list_provider_connections.return_value = cpb.ListProviderConnectionsResponse()
    with pytest.raises(cc.RouterNotFoundError):
        client.connection("absent")
    stub.list_provider_connections.return_value = cpb.ListProviderConnectionsResponse(
        connections=[
            cpb.ProviderConnection(id="a", name="duplicate"),
            cpb.ProviderConnection(id="b", name="duplicate"),
        ]
    )
    with pytest.raises(cc.RouterError, match="Multiple"):
        client.connection("duplicate")


def test_failed_deploy_keeps_last_successful_metadata(backend):
    client, stub, _ = backend
    router = cc.Router("assistant", default=SMALL).deploy(client=client)
    original = router.info
    stub.list_model_routes.side_effect = ConnectError(Code.PERMISSION_DENIED, "denied")
    with pytest.raises(cc.RouterError, match="denied") as exc:
        router.deploy(client=client)
    assert isinstance(exc.value.__cause__, ConnectError)
    assert router.info is original


def test_delete_returns_warnings_and_does_not_delete_connection(backend):
    client, stub, connect = backend
    stub.list_model_routes.return_value = pb.ListModelRoutesResponse(
        routes=[pb.ModelRoute(id="r", public_model_id="chalk/assistant")]
    )
    stub.delete_model_route.return_value = pb.DeleteModelRouteResponse(
        id="r", warnings=["removed"]
    )
    with client:
        assert client.delete("assistant") == ["removed"]
    assert stub.delete_model_route.call_args.args[0].id == "r"
    stub.delete_provider_connection.assert_not_called()
    connect.close.assert_not_called()  # caller owns this injected client


def test_default_deploy_owns_and_closes_client(monkeypatch, backend):
    _, stub, connect = backend
    monkeypatch.setattr("chalkcompute._router.ConnectClient", lambda **kwargs: connect)
    cc.Router("assistant", default=SMALL).deploy()
    stub.create_model_route.assert_called_once()
    connect.close.assert_called_once()


def test_full_resource_workflow_without_constructing_client(monkeypatch, backend):
    _, stub, connect = backend
    monkeypatch.setattr("chalkcompute._router.ConnectClient", lambda **kwargs: connect)
    stub.list_provider_connections.return_value = cpb.ListProviderConnectionsResponse()
    stub.create_provider_connection.return_value = cpb.CreateProviderConnectionResponse(
        connection=cpb.ProviderConnection(
            id="provider", name="openai", provider_kind="openai", routing_enabled=True
        )
    )
    with pytest.raises(cc.RouterNotFoundError):
        Connection.from_name("openai")
    provider = Connection.create("openai", provider="openai", api_key="test-only-value")
    small = provider.model("small")
    large = provider.model("large")
    router = cc.Router(
        "assistant",
        default=small,
        judge=LegacyJudge.heuristic(easy=small, hard=large),
    ).deploy()
    request = stub.create_model_route.call_args.args[0]
    assert request.routing.default_target.connection_id == "provider"
    assert request.routing.judge.outcomes["1"].model == "provider/large"
    stub.list_model_routes.return_value = pb.ListModelRoutesResponse(
        routes=[
            pb.ModelRoute(
                id="route-1", public_model_id="chalk/assistant", published=True
            )
        ]
    )
    assert router.refresh().id == "route-1"
    stub.delete_model_route.return_value = pb.DeleteModelRouteResponse(id="route-1")
    assert router.delete() == []
    assert router.id is None
    assert connect.close.call_count == 5  # includes failed lookup


def test_explicit_constructor_client_is_retained_for_lifecycle(backend):
    client, stub, connect = backend
    router = cc.Router("assistant", default=SMALL, client=client)
    assert router.deploy() is router
    stub.list_model_routes.return_value = pb.ListModelRoutesResponse(
        routes=[pb.ModelRoute(id="route-1", public_model_id="chalk/assistant")]
    )
    router.refresh()
    stub.delete_model_route.return_value = pb.DeleteModelRouteResponse(id="route-1")
    router.delete()
    connect.close.assert_not_called()


def test_load_and_redeploy_existing_route_without_client(monkeypatch, backend):
    _, stub, connect = backend
    monkeypatch.setattr("chalkcompute._router.ConnectClient", lambda **kwargs: connect)
    definition = cc.Router(
        "assistant",
        default=SMALL,
        fallbacks=[LARGE],
        judge=LegacyJudge.heuristic(easy=SMALL, hard=LARGE),
    )
    stub.list_model_routes.return_value = pb.ListModelRoutesResponse(
        routes=[
            pb.ModelRoute(
                id="saved",
                public_model_id="chalk/assistant",
                connection_id=SMALL.connection_id,
                upstream_model_id=SMALL.model,
                published=True,
                routing=definition._spec.routing,
            )
        ]
    )
    loaded = cc.Router.from_name("assistant")
    assert loaded.id == "saved"
    assert loaded.model == "chalk/assistant"
    loaded.deploy()
    update = stub.update_model_route.call_args.args[0]
    assert update.id == "saved"
    assert update.update.routing == definition._spec.routing
    assert connect.close.call_count == 2


def test_resource_connection_can_use_explicit_client(backend):
    client, stub, connect = backend
    stub.list_provider_connections.return_value = cpb.ListProviderConnectionsResponse(
        connections=[
            cpb.ProviderConnection(id="saved", name="openai", provider_kind="openai")
        ]
    )
    assert Connection.from_name("openai", client=client).id == "saved"
    connect.close.assert_not_called()


@pytest.mark.parametrize(
    "make",
    [
        lambda: cc.Router(" ", default=SMALL),
        lambda: cc.Router("chalk/", default=SMALL),
        lambda: Target("chalk", "alias"),
        lambda: Target("a/b", "model"),
        lambda: Target("a", "chalk/alias"),
        lambda: Target("a", " model"),
        lambda: cc.Router("x", default=SMALL, fallbacks=[SMALL]),
        lambda: cc.Router("x", default=SMALL, fallbacks=[LARGE, LARGE]),
        lambda: cc.Router("x", default=SMALL, fallbacks=[LARGE] * 17),
        lambda: LegacyJudge.heuristic(
            easy=SMALL,
            hard=LARGE,
            added_hard_keywords=["reason"],
            removed_hard_keywords=["REASON"],
        ),
        lambda: LegacyJudge.heuristic(easy=SMALL, hard=LARGE, max_easy_input_bytes=-1),
        lambda: LegacyJudge.completion(
            SMALL, classification="difficulty", outcomes={"easy": SMALL, "hard": LARGE}
        ),
        lambda: LegacyJudge.completion(SMALL, classification="task", outcomes={}),
        lambda: LegacyJudge.completion(
            SMALL, classification="task", outcomes={"other": SMALL}, timeout_ms=30001
        ),
    ],
)
def test_invalid_definitions_fail_before_network(make):
    with pytest.raises(ValueError):
        make()


def test_modern_routing_separates_classifier_targets_and_fallback_presence(backend):
    from chalkcompute.router import Classifier, Route

    client, stub, _ = backend
    stub.get_route_judge_defaults.return_value = pb.GetRouteJudgeDefaultsResponse(
        routing_schema_version=1
    )
    router = cc.Router(
        "assistant",
        default=SMALL,
        fallbacks=[BACKUP],
        classifier=Classifier.completion(LARGE),
        routes={
            "simple": SMALL,
            "coding": Route(LARGE, description="Writing code", fallbacks=[]),
        },
    )
    router.deploy(client=client)
    routing = stub.create_model_route.call_args.args[0].routing
    assert routing.judge is None and not routing.fallbacks
    assert routing.classifier.backend.kind.field == "completion"
    assert routing.routes["simple"].fallbacks is None
    assert routing.routes["coding"].fallbacks.targets == []
    assert (
        routing.fallback_defaults[0].fallbacks[0].connection_id == BACKUP.connection_id
    )


def test_old_server_rejected_before_mutation(backend):
    from chalkcompute.router import Classifier

    client, stub, _ = backend
    stub.get_route_judge_defaults.return_value = pb.GetRouteJudgeDefaultsResponse()
    router = cc.Router(
        "assistant",
        default=SMALL,
        classifier=Classifier.completion(LARGE),
        routes={"simple": SMALL},
    )
    with pytest.raises(cc.RouterError, match="upgrade"):
        router.deploy(client=client)
    stub.create_model_route.assert_not_called()
    stub.update_model_route.assert_not_called()


def test_function_deploys_and_waits_before_router_publication(backend, monkeypatch):
    from chalkcompute._function import RemoteFunction

    monkeypatch.setattr(RemoteFunction, "_start_background_deploy", lambda self: None)
    from chalkcompute._function import FunctionVersionInfo

    client, stub, connect = backend
    events = []

    @cc.function
    def classifier(request: RoutingRequest) -> str:
        return "simple"

    connect._env_id = "test-environment"
    classifier._client = connect
    classifier._version_info = FunctionVersionInfo(
        id="version",
        function_name="classifier",
        version=1,
        scaling_group_name="classifier",
        function_id="12345678-1234-1234-1234-123456789abc",
    )
    monkeypatch.setattr(classifier, "deploy", lambda: events.append("deploy"))
    monkeypatch.setattr(classifier, "wait_ready", lambda: events.append("ready"))
    stub.get_route_judge_defaults.return_value = pb.GetRouteJudgeDefaultsResponse(
        routing_schema_version=1
    )
    stub.validate_route_judge_backend.return_value = (
        pb.ValidateRouteJudgeBackendResponse()
    )
    response = stub.create_model_route.return_value
    stub.create_model_route.side_effect = lambda request: (
        events.append("publish"),
        response,
    )[1]
    router = cc.Router(
        "assistant", default=SMALL, classifier=classifier, routes={"simple": SMALL}
    )
    assert events == []
    router.deploy(client=client)
    assert events == ["deploy", "ready", "publish"]
    backend_config = stub.create_model_route.call_args.args[
        0
    ].routing.classifier.backend.kind.value
    assert backend_config.function_id == classifier._version_info.function_id
    assert backend_config.input_name == "request"


def test_classifier_signature_and_routes_validate_locally(monkeypatch):
    from chalkcompute._function import RemoteFunction

    monkeypatch.setattr(RemoteFunction, "_start_background_deploy", lambda self: None)
    from chalkcompute.router import Classifier

    @cc.function
    def wrong(request: str) -> str:
        return request

    with pytest.raises(TypeError, match="RoutingRequest"):
        Classifier.function(wrong)
    with pytest.raises(ValueError, match="together"):
        cc.Router("assistant", default=SMALL, routes={"simple": SMALL})
    with pytest.raises(ValueError, match="consecutive"):
        cc.Router(
            "assistant",
            default=SMALL,
            classifier=Classifier.heuristic(),
            routes={"easy": SMALL, "hard": LARGE},
        )


def test_routing_request_arrow_preserves_multimodal_tools_and_metadata():
    import pyarrow as pa
    from chalkcompute._function import _build_schemas

    payload = {
        "metadata": {"task": "coding"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "explain"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc"},
                    },
                ],
            },
            {"role": "tool", "content": "tool result"},
        ],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }
    request = RoutingRequest(
        model="chalk/assistant", surface="chat_completions", request=payload
    )
    scalar = request.__chalk_serialize__()
    assert scalar.type == pa.large_string()
    restored = RoutingRequest.__chalk_deserialize__(scalar)
    assert restored.request == payload
    assert restored.metadata == {"task": "coding"}
    assert restored.text == "explain\ntool result"
    assert "base64" not in restored.text

    def classifier(request: RoutingRequest) -> str:
        return "coding"

    # Resolve the local annotation as Python modules do in a deployed function.
    classifier.__annotations__ = {"request": RoutingRequest, "return": str}
    inputs, outputs = _build_schemas(classifier)
    assert inputs.columns[0].arrow_type == RoutingRequest.__chalk_arrow_type__
    assert outputs.columns[0].name == "result"
