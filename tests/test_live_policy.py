from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from chalkcompute._gen.chalk.modeldeployment.v1 import curated_models_pb as runtime_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._gen.chalk.weightstream.v1 import service_pb as weight_pb
from chalkcompute._live_policy import (
    LivePolicy,
    LivePolicyDeploymentError,
    LivePolicyError,
    _accelerator,
)
from chalkcompute._scaling_group import ScalingGroup
from chalkcompute._weights import WeightStream
from chalkcompute._weights_format import TensorEntry, WeightManifest, pointer_json
from tests._fakes import MemoryWeightVolume, StubbedConnectClient


class PolicyClient(StubbedConnectClient):
    def __init__(self):
        self.requests = []
        self.existing = None
        self.fail_create = False
        self.fail_runtime = False
        self.image = "registry/live-vllm@sha256:" + "a" * 64
        self.retain_id = True
        self.indexed = []

    def call(self, method, request, **kwargs):
        self.requests.append(request)
        if method.name == "create_weight_stream":
            return weight_pb.CreateWeightStreamResponse(
                weight_stream=weight_pb.WeightStream(
                    id="stream", volume_name="weights-test", base_model="model"
                )
            )
        if method.name == "list_weight_stream_versions":
            return weight_pb.ListWeightStreamVersionsResponse(versions=self.indexed)
        if method.name == "record_weight_stream_version":
            self.indexed.append(request.version)
            return weight_pb.RecordWeightStreamVersionResponse(version=request.version)
        if method.name == "resolve_serving_runtime_image":
            if self.fail_runtime:
                raise ConnectError(Code.UNIMPLEMENTED, "runtime unsupported")
            return runtime_pb.ResolveServingRuntimeImageResponse(
                image_ref=self.image,
                engine=request.engine,
                accelerator=request.accelerator,
            )
        if method.name == "get_scaling_group":
            if self.existing is None:
                raise ConnectError(Code.NOT_FOUND, "missing")
            return sg_pb.GetScalingGroupResponse(scaling_group=self.existing)
        if method.name == "create_scaling_group":
            group = sg_pb.ScalingGroupResponse(
                id="group",
                name=request.spec.container_spec.name,
                spec=request.spec,
                status="Available",
                ready_replicas=1,
                available_replicas=1,
                web_url="https://example.com",
                weight_stream_id=request.weight_stream_id if self.retain_id else None,
            )
            self.existing = group
            if self.fail_create:
                raise ConnectError(Code.UNAVAILABLE, "response lost")
            return sg_pb.CreateScalingGroupResponse(scaling_group=group)
        if method.name == "list_scaling_group_replicas":
            return sg_pb.ListScalingGroupReplicasResponse(
                replicas=[
                    sg_pb.ScalingGroupReplica(id="zero", applied_weight_version=0),
                    sg_pb.ScalingGroupReplica(id="unknown"),
                ]
            )
        if method.name == "list_scaling_group_revisions":
            return sg_pb.ListScalingGroupRevisionsResponse()
        if method.name == "delete_scaling_group":
            self.existing = None
            return sg_pb.DeleteScalingGroupResponse()
        return SimpleNamespace()


@pytest.fixture
def setup_policy(monkeypatch):
    volume = MemoryWeightVolume()
    manifest = WeightManifest(
        0,
        None,
        "keyframe",
        "model",
        "a" * 64,
        (
            TensorEntry(
                "w", "float32", (1,), "dense", "model.safetensors", 0, 4, 1, "f" * 32
            ),
        ),
    )
    volume._commit(
        {"CURRENT": pointer_json(0), "v000000/manifest.json": manifest.to_json()}
    )
    stream = WeightStream("test", "model", volume=cast(Any, volume))
    client = PolicyClient()
    stream.register(client=cast(Any, client))
    monkeypatch.setattr(
        "chalkcompute._scaling_group.referenced_volume_mount",
        lambda client, mount: mount,
    )
    return stream, client


def test_deploy_uses_real_immutable_image_and_stream_request(setup_policy):
    stream, client = setup_policy
    policy = LivePolicy.deploy("policy", stream, gpu="nvidia-a100-80gb:1", replicas=2)
    request = next(
        r for r in client.requests if isinstance(r, sg_pb.CreateScalingGroupRequest)
    )
    assert request.weight_stream_id == "stream"
    container = request.spec.container_spec
    assert container.image == client.image
    assert container.volumes[0].ref_name == "main"
    assert container.volumes[0].mount_path == "/chalk/weights"
    assert "--served-model-name" in container.entrypoint
    assert request.spec.scaling_spec.min_replicas == 2
    assert policy.scaling_group.info.weight_stream_id == "stream"
    replicas = policy.replicas()
    assert [r.applied_weight_version for r in replicas] == [0, None]
    policy.delete()
    assert client.existing is None
    assert stream.current() == 0


@pytest.mark.parametrize(
    "image", ["registry/live:latest", "registry/live@sha256:123", ""]
)
def test_mutable_or_missing_runtime_image_never_claims_group(setup_policy, image):
    stream, client = setup_policy
    client.image = image
    with pytest.raises(LivePolicyError, match="immutable"):
        LivePolicy.deploy("policy", stream, gpu="nvidia-a100-80gb")
    assert not any(
        isinstance(r, sg_pb.CreateScalingGroupRequest) for r in client.requests
    )


def test_unsupported_runtime_never_claims_group(setup_policy):
    stream, client = setup_policy
    client.fail_runtime = True
    with pytest.raises(ConnectError):
        LivePolicy.deploy("policy", stream, gpu="amd-mi355x:8")
    assert not any(
        isinstance(r, sg_pb.CreateScalingGroupRequest) for r in client.requests
    )


def test_ambiguous_create_error_preserves_recoverable_handle(setup_policy):
    stream, client = setup_policy
    client.fail_create = True
    with pytest.raises(LivePolicyDeploymentError) as caught:
        LivePolicy.deploy("policy", stream, gpu="nvidia-a100-80gb")
    assert caught.value.prior_group_id is None
    assert caught.value.created_new_group is None
    assert client.existing is not None
    assert not any(
        isinstance(r, sg_pb.DeleteScalingGroupRequest) for r in client.requests
    )
    caught.value.policy.delete()
    assert client.existing is None


def test_preexisting_group_failure_never_automatically_deletes(setup_policy):
    stream, client = setup_policy
    client.existing = sg_pb.ScalingGroupResponse(
        id="prior-group", weight_stream_id="stream"
    )
    client.fail_create = True
    with pytest.raises(LivePolicyDeploymentError) as caught:
        LivePolicy.deploy("policy", stream, gpu="nvidia-a100-80gb")
    assert caught.value.prior_group_id == "prior-group"
    assert caught.value.created_new_group is False
    assert not any(
        isinstance(r, sg_pb.DeleteScalingGroupRequest) for r in client.requests
    )


def test_missing_response_linkage_is_rejected(setup_policy):
    stream, client = setup_policy
    client.retain_id = False
    with pytest.raises(LivePolicyDeploymentError) as caught:
        LivePolicy.deploy("policy", stream, gpu="nvidia-a100-80gb")
    assert "association" in str(caught.value.__cause__)


def response_policy(monkeypatch, setup_policy, body, version="0"):
    stream, client = setup_policy
    policy = LivePolicy(stream, cast(Any, SimpleNamespace()), cast(Any, client))
    monkeypatch.setattr(
        policy.scaling_group,
        "call",
        lambda *args, **kwargs: SimpleNamespace(
            status=200,
            content=json.dumps(body).encode(),
            headers={"x-chalk-policy-version": version},
        ),
        raising=False,
    )
    return policy


def test_completion_choice_indices_map_to_input_prompts(monkeypatch, setup_policy):
    policy = response_policy(
        monkeypatch,
        setup_policy,
        {"choices": [{"index": 1, "text": "second"}, {"index": 0, "text": "first"}]},
    )
    output = policy.generate(["one", "two"], n=2)
    assert [
        (r.prompt_index, r.choice_index, r.text, r.policy_version) for r in output
    ] == [
        (0, 0, "first", 0),
        (0, 1, "second", 0),
        (1, 0, "first", 0),
        (1, 1, "second", 0),
    ]


@pytest.mark.parametrize(
    "version", [None, "-1", "+1", "01", "1,2", str(2**64), "", "9" * 100]
)
def test_policy_version_header_is_strict_uint64(monkeypatch, setup_policy, version):
    policy = response_policy(
        monkeypatch, setup_policy, {"choices": [{"index": 0, "text": "text"}]}, version
    )
    with pytest.raises(LivePolicyError, match="version header"):
        policy.generate("prompt")


def test_chat_preserves_tool_calls_and_structured_content(monkeypatch, setup_policy):
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call",
                "type": "function",
                "function": {"name": "foo", "arguments": "{}"},
            }
        ],
    }
    policy = response_policy(
        monkeypatch,
        setup_policy,
        {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}]},
        str(2**64 - 1),
    )
    output = policy.chat(
        [{"role": "user", "content": "call foo"}], tools=[{"type": "function"}]
    )
    assert output[0].message == message
    assert output[0].policy_version == 2**64 - 1
    assert output[0].finish_reason == "tool_calls"


@pytest.mark.parametrize(
    "choices",
    [
        [{"text": "missing"}],
        [{"index": True, "text": "bad"}],
        [{"index": 0, "text": "one"}, {"index": 0, "text": "two"}],
        [{"index": 1, "text": "gap"}],
    ],
)
def test_invalid_choice_indices_rejected(monkeypatch, setup_policy, choices):
    policy = response_policy(monkeypatch, setup_policy, {"choices": choices})
    with pytest.raises(LivePolicyError, match="indices"):
        policy.generate("prompt")


@pytest.mark.parametrize(
    "gpu, accelerator",
    [
        ("4:nvidia-a100-80gb", runtime_pb.ServingAccelerator.CUDA),
        ("amd-mi355x:8", runtime_pb.ServingAccelerator.ROCM),
    ],
)
def test_canonical_gpu_count_forms(gpu, accelerator):
    assert _accelerator(gpu) == accelerator


def test_scaling_group_rejects_empty_stream_id():
    with pytest.raises(ValueError, match="weight_stream_id"):
        ScalingGroup(image="image", weight_stream_id="")


@pytest.mark.parametrize("args", ["--dtype bfloat16", b"--dtype bfloat16"])
def test_engine_args_requires_sequence_before_claim(setup_policy, args):
    stream, client = setup_policy
    with pytest.raises(TypeError, match="sequence"):
        LivePolicy.deploy("policy", stream, gpu="nvidia-a100-80gb", engine_args=args)
    assert not any(
        isinstance(r, sg_pb.CreateScalingGroupRequest) for r in client.requests
    )


def test_preflight_rejects_empty_prior_identity(setup_policy):
    stream, client = setup_policy
    client.existing = sg_pb.ScalingGroupResponse(weight_stream_id="stream")
    with pytest.raises(LivePolicyError, match="prior group identity"):
        LivePolicy.deploy("policy", stream, gpu="nvidia-a100-80gb")
    assert not any(
        isinstance(r, sg_pb.CreateScalingGroupRequest) for r in client.requests
    )
