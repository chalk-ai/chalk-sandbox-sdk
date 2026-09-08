from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError as RpcError
from protobuf import Oneof

import chalkcompute._function_deployment as deployment
import chalkcompute._function as function_module
from chalkcompute import _native
from chalkcompute._connect import ConnectError
from chalkcompute._function import RemoteFunction
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import service_pb as pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg
from chalkcompute._gen.chalk.sandbox.v1 import service_pb as sandbox
from chalkcompute._gen.chalk.volume.v2 import volume_pb as volume
from chalkcompute._image import Image
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


def run(client, prepared):
    return deployment.ensure_external_function(
        client,
        function_name="test",
        prepared=prepared,
        version_spec=pb.ExternalFunctionVersionSpec(),
        local_secrets={},
        force_new_version=False,
        poll_interval=0,
        timeout=5,
    )


def test_source_identity_uses_bytes_destination_and_mode_not_mtime_or_local_path(
    tmp_path,
):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"hello")
    b.write_bytes(b"hello")
    a.chmod(0o644)
    b.chmod(0o644)
    first = deployment.prepare_function_image(
        Image("python:3.11").add_local_file(str(a), "/app/a.py")
    )
    os.utime(a, (10, 10))
    same = deployment.prepare_function_image(
        Image("python:3.11").add_local_file(str(b), "/app/a.py")
    )
    touched = deployment.prepare_function_image(first.image)
    assert first.sources == same.sources == touched.sources
    b.chmod(0o755)
    executable = deployment.prepare_function_image(same.image)
    assert executable.sources != first.sources
    b.write_bytes(b"HELLO")
    assert deployment.prepare_function_image(same.image).sources != executable.sources
    assert (
        deployment.prepare_function_image(
            Image("python:3.11").add_local_file(str(a), "/app/b.py")
        ).sources
        != first.sources
    )


def test_native_tree_digest_cross_language_golden(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"hello")
    digest, size, _ = _native.source_file_digests([str(source)])[0]
    assert digest == "ea8f163db38682925e4491c5e58d4bb3506ef8c14eb78a86e908c5624a67200f"
    assert size == 5
    # Keep the Go sourceManifestDigest golden in sync with this test.
    result = _native.source_tree_digest([("a.py", digest, 5, 0o644)])
    assert result == "7585d7b30b541faa685013f553715a556864f0464c58eefc1bb5a56ac73a5c88"


def test_warm_deployment_is_one_ensure_call(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("warm deployment attempted preparation")

    monkeypatch.setattr(deployment, "_supply_files", unexpected)
    monkeypatch.setattr(deployment, "wait_for_image_build", unexpected)
    client = Client([complete()])
    result = run(client, deployment.prepare_function_image(Image("python:3.11")))
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
    assert run(
        client, deployment.prepare_function_image(Image("python:3.11"))
    ).reused_version
    assert client.requests[0].request_id == client.requests[1].request_id


def test_image_wait_propagates_expired_workflow():
    from chalkcompute._connect import wait_for_image_build

    class ExpiredClient(StubbedConnectClient):
        def call(self, method, request, **kwargs):
            raise RpcError(Code.NOT_FOUND, "workflow expired")

    with pytest.raises(RpcError) as error:
        wait_for_image_build(ExpiredClient(), "expired", poll_interval=0, timeout=1)
    assert error.value.code == Code.NOT_FOUND


def test_missing_inputs_and_image_wait_keep_request_id(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"hello")
    prepared = deployment.prepare_function_image(
        Image("python:3.11").add_local_file(str(source), "/app/a.py")
    )
    tree = prepared.sources[0].tree_digest
    digest = next(iter(prepared.files))
    missing = pb.MissingFunctionDeploymentInputs(
        file_digests=[digest], upload_volume=volume.VolumeRef(name="source")
    )
    responses = [
        RpcError(Code.UNAVAILABLE, "retry"),
        pb.EnsureExternalFunctionResponse(
            result=Oneof(
                "missing_inputs",
                pb.MissingFunctionDeploymentInputs(tree_digests=[tree]),
            )
        ),
        pb.EnsureExternalFunctionResponse(result=Oneof("missing_inputs", missing)),
        pb.EnsureExternalFunctionResponse(
            result=Oneof(
                "pending_image_build",
                sandbox.GetOrBuildCustomImageResponse(
                    build_id="build", status="building"
                ),
            )
        ),
        complete(),
    ]
    uploads, waits = [], []

    def supply(client, image, request):
        uploads.append(request.file_digests)
        return pb.FunctionDeploymentInputs(upload_volume=request.upload_volume)

    monkeypatch.setattr(deployment, "_supply_files", supply)
    monkeypatch.setattr(
        deployment, "wait_for_image_build", lambda *a, **kw: waits.append(a[1])
    )
    client = Client(responses)
    run(client, prepared)
    assert len({request.request_id for request in client.requests}) == 1
    assert client.requests[0].to_binary() == client.requests[1].to_binary()
    assert client.requests[2].supplied_inputs.manifests == [prepared.manifests[tree]]
    assert uploads == [[digest]]
    assert waits == ["build"]


def test_upload_rejects_source_changed_after_fingerprinting(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"hello")
    prepared = deployment.prepare_function_image(
        Image("python:3.11").add_local_file(str(source), "/app/a.py")
    )
    digest = next(iter(prepared.files))
    receipt = volume.CommitIntent(
        deltas=Oneof(
            "path_deltas",
            volume.PathDeltaList(
                upserts=[
                    volume.PathFileDelta(
                        path=digest,
                        node=volume.FileNode(
                            node=Oneof(
                                "file",
                                volume.RegularFileNode(
                                    content=volume.ContentRef(
                                        content=Oneof(
                                            "inline",
                                            volume.InlineFileContent(
                                                hash="changed", size=5, data=b"other"
                                            ),
                                        )
                                    )
                                ),
                            )
                        ),
                    )
                ]
            ),
        )
    )
    inner = SimpleNamespace(
        stage=lambda *a: object(), staged_intent=lambda *a: receipt.to_binary()
    )
    monkeypatch.setattr(
        deployment.VolumeClient,
        "from_connect",
        lambda client: SimpleNamespace(_inner=inner),
    )
    with pytest.raises(ConnectError, match="Source changed"):
        deployment._supply_files(
            Client([]),
            prepared,
            pb.MissingFunctionDeploymentInputs(
                file_digests=[digest], upload_volume=volume.VolumeRef(name="source")
            ),
        )


def test_generated_function_sources_are_stable_between_runs(monkeypatch):
    monkeypatch.setenv("CHALK_FUNCTION_STRIP_SOURCE", "0")
    monkeypatch.delenv("CHALK_FUNCTION_LOCAL_COPY", raising=False)

    def add(x: int) -> int:
        return x + 1

    fn = RemoteFunction(add)
    images, temporary = [], []
    try:
        for _ in range(2):
            images.append(deployment.prepare_function_image(fn._build_deploy_image()))
            temporary.extend(fn._temp_files)
        assert images[0].sources == images[1].sources
        assert images[0].image.to_proto() == images[1].image.to_proto()
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

    def prepare(image):
        prepared = deployment.prepare_function_image(image)
        prepared_sources.append(
            {file.dest: Path(file.src).read_text() for file in image.lazy_local_files}
        )
        return prepared

    monkeypatch.setattr(function_module, "prepare_function_image", prepare)
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
