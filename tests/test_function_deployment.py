from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as RpcError
from protobuf import Oneof

import chalkcompute._function_deployment as deployment
import chalkcompute._function as function_module
from chalkcompute._function import RemoteFunction
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg
from chalkcompute._gen.chalk.sandbox.v1 import service_pb as sandbox
from chalkcompute._image import Image
from chalkcompute._volume import VolumeMount
from tests._fakes import StubbedConnectClient


class Client(StubbedConnectClient):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def call(self, method, request, **kwargs):
        assert method.name == "ensure_external_function"
        self.requests.append(
            pb.EnsureExternalFunctionRequest.from_binary(request.to_binary())
        )
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def complete():
    return pb.EnsureExternalFunctionResponse(
        result=Oneof(
            "complete",
            pb.EnsuredExternalFunction(
                external_function=pb.ExternalFunction(
                    id="fn",
                    name="test",
                    current_version=pb.ExternalFunctionVersion(
                        id="v1", function_name="test", scaling_group_name="test"
                    ),
                ),
                scaling_group=sg.ScalingGroupResponse(
                    status="Running", ready_replicas=1, available_replicas=1
                ),
                reused_version=True,
            ),
        )
    )


def run(client, image):
    return deployment.ensure_external_function(
        client,
        function_name="test",
        image=image,
        version_spec=pb.ExternalFunctionVersionSpec(),
        local_secrets={},
        force_new_version=False,
        poll_interval=0,
        timeout=5,
    )


def test_warm_deployment_is_one_ensure_call(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("warm deployment attempted preparation")

    monkeypatch.setattr(deployment, "wait_for_image_build", unexpected)
    client = Client([complete()])
    result = run(client, Image("python:3.11"))
    assert result.reused_version
    assert len(client.requests) == 1


def test_expired_build_returns_to_ensure(monkeypatch):
    def expired(*args, **kwargs):
        raise RpcError(Code.NOT_FOUND, "workflow expired")

    monkeypatch.setattr(deployment, "wait_for_image_build", expired)
    client = Client(
        [
            pb.EnsureExternalFunctionResponse(
                result=Oneof(
                    "pending_image_build",
                    sandbox.GetOrBuildCustomImageResponse(
                        build_id="expired", status="building"
                    ),
                )
            ),
            complete(),
        ]
    )
    assert run(client, Image("python:3.11")).reused_version
    assert client.requests[0].request_id == client.requests[1].request_id


def test_image_wait_propagates_expired_workflow():
    from chalkcompute._connect import wait_for_image_build

    class ExpiredClient(StubbedConnectClient):
        def call(self, method, request, **kwargs):
            raise RpcError(Code.NOT_FOUND, "workflow expired")

    with pytest.raises(RpcError) as error:
        wait_for_image_build(ExpiredClient(), "expired", poll_interval=0, timeout=1)
    assert error.value.code == Code.NOT_FOUND


def test_source_mount_rejects_user_volume_at_same_path(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("answer = 42\n")
    image = Image("python").add_local_file(str(source), "/app/source.py")

    with pytest.raises(ValueError, match="overlaps.*'/app'"):
        deployment.resolve_function_volumes(
            Client([]),
            image=image,
            volume_name="function",
            referenced=[VolumeMount("user", "/app")],
            poll_interval=0,
            timeout=5,
        )


def test_generated_function_sources_are_stable_between_runs(monkeypatch):
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")
    monkeypatch.delenv("CHALK_FUNCTION_LOCAL_COPY", raising=False)

    def add(x: int) -> int:
        return x + 1

    fn = RemoteFunction(add)
    images, sources, temporary = [], [], []
    try:
        for _ in range(2):
            image = fn._build_deploy_image()
            images.append(image)
            sources.append(
                {file.dest: Path(file.src).read_text() for file in image.lazy_local_files}
            )
            temporary.extend(fn._temp_files)
        assert sources[0] == sources[1]
        assert images[0].to_proto() == images[1].to_proto()
    finally:
        for file in temporary:
            os.unlink(file)


def test_ready_reused_function_does_not_poll(monkeypatch):
    def add(x: int) -> int:
        return x

    fn = RemoteFunction(add, name="test")
    client = Client([complete(), complete()])
    client._env_id = None
    fn._client = client
    monkeypatch.setattr(
        RemoteFunction, "_build_deploy_image", lambda _: Image("python:3.11")
    )
    monkeypatch.setattr(
        RemoteFunction,
        "_poll_until_ready",
        lambda *a, **kw: pytest.fail("polled a ready version"),
    )
    fn.deploy()
    fn.deploy()
    assert fn.version_info.id == "v1"
    assert len(client.requests) == 2
    assert client.requests[0].request_id != client.requests[1].request_id
    assert client.requests[0].spec == client.requests[1].spec


@pytest.mark.parametrize("name", [None, "remote-add", "renamed"])
@pytest.mark.parametrize("strip_source", ["0", "1"])
@pytest.mark.parametrize("background", [False, True])
def test_redeploy_preserves_callable_and_desired_spec(
    monkeypatch, tmp_path, name, strip_source, background
):
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", strip_source)
    monkeypatch.delenv("CHALK_FUNCTION_LOCAL_SDK_COPY", raising=False)
    source = tmp_path / "reuse_example.py"
    source.write_text("def add_values(x: int = 41) -> int:\n    return x + 1\n")
    spec = importlib.util.spec_from_file_location("reuse_example", source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    registered_name = name or "add_values"

    class EchoClient(Client):
        def call(self, method, request, **kwargs):
            response = super().call(method, request, **kwargs)
            version = response.result.value.external_function.current_version
            version.function_name = registered_name
            version.input_arrow_schema = request.spec.version_spec.input_arrow_schema
            version.output_arrow_schema = request.spec.version_spec.output_arrow_schema
            return response

    client = EchoClient([complete(), complete()])
    client._env_id = None
    monkeypatch.setattr(function_module, "ConnectClient", lambda **kwargs: client)
    prepared_sources = []

    def resolve(client, *, image, referenced, **kwargs):
        prepared_sources.append(
            {file.dest: Path(file.src).read_text() for file in image.lazy_local_files}
        )
        return [*referenced, VolumeMount("source", "/app", version_id=1)]

    monkeypatch.setattr(function_module, "resolve_function_volumes", resolve)
    monkeypatch.setattr(
        RemoteFunction,
        "_poll_until_ready",
        lambda *a, **kw: pytest.fail("polled a ready version"),
    )
    if background:
        fn = function_module.function(module.add_values, name=name)
        fn.version_info
    else:
        fn = RemoteFunction(module.add_values, name=name)
        fn.deploy()
    fn.deploy()

    assert len(client.requests) == 2
    assert client.requests[0].request_id != client.requests[1].request_id
    assert [r.function_name for r in client.requests] == [registered_name] * 2
    assert client.requests[0].spec == client.requests[1].spec
    assert prepared_sources[0] == prepared_sources[1]
    for path, text in prepared_sources[1].items():
        compile(text, path, "exec")
    assert fn._func_name == "add_values"
    assert fn._queue_function_name() == registered_name
    assert fn._bind_call_arguments((), {}) == (41,)
