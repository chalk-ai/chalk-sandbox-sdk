"""Version-stamped generation over a weight-stream-backed scaling group."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from protobuf import Oneof

from chalkcompute._containerspec import ComputeClass, ExecutionBackend
from chalkcompute._gen.chalk.modeldeployment.v1 import (
    curated_models_connect as runtime_connect,
)
from chalkcompute._gen.chalk.modeldeployment.v1 import curated_models_pb as runtime_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_connect as sg_connect
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._naming import sanitize_dns_label
from chalkcompute._scaling_group import (
    ReadinessProbe,
    ScalingGroup,
    _parse_scaling_group_response,
)
from chalkcompute._volume import VolumeMount
from chalkcompute._weights import WeightStream

if TYPE_CHECKING:
    from chalkcompute._connect import ConnectClient
    from chalkcompute._weights import WeightPublisher


class LivePolicyError(Exception):
    """Serving response, image, or stream association is invalid."""


class LivePolicyDeploymentError(LivePolicyError):
    """Deployment failed; policy retains the handle needed to inspect or delete it."""

    def __init__(self, policy: LivePolicy, prior_group_id: str | None) -> None:
        self.policy = policy
        self.prior_group_id = prior_group_id
        # CreateScalingGroup upserts by name and does not return a created flag.
        # A preflight miss cannot prove that a concurrent caller did not create it.
        self.created_new_group: bool | None = False if prior_group_id else None
        super().__init__(
            "Live policy deployment did not complete. Inspect the cause and "
            "error.policy.scaling_group; call error.policy.delete() to release "
            "an unwanted deployment."
        )


@dataclass(frozen=True)
class PolicyGeneration:
    text: str
    policy_version: int
    prompt_index: int
    choice_index: int
    finish_reason: str | None


@dataclass(frozen=True)
class PolicyChatGeneration:
    message: Mapping[str, Any]
    policy_version: int
    choice_index: int
    finish_reason: str | None


@dataclass(frozen=True)
class PolicyReplica:
    id: str
    name: str
    status: str
    applied_weight_version: int | None


class LivePolicy:
    """A live serving deployment associated with one immutable weight stream ID."""

    def __init__(
        self, stream: WeightStream, scaling_group: ScalingGroup, client: ConnectClient
    ) -> None:
        self.stream = stream
        self.scaling_group = scaling_group
        self._client = client

    @classmethod
    def deploy(
        cls,
        name: str,
        stream: WeightStream,
        *,
        gpu: str,
        engine: Literal["vllm", "sglang"] = "vllm",
        replicas: int = 1,
        compute_class: ComputeClass = ComputeClass.HOST,
        execution_target: ExecutionBackend = ExecutionBackend.SERVERLESS,
        cpu: str | None = None,
        memory: str | None = None,
        engine_args: Sequence[str] = (),
        ready_timeout: float = 600.0,
    ) -> LivePolicy:
        """Resolve an immutable runtime image and deploy a registered stream.

        The returned handle manages a scaling group; call delete() when finished.
        LivePolicyDeploymentError retains the same handle if deployment fails.
        The weight volume is borrowed and is not deleted with the deployment.
        """
        name = sanitize_dns_label(name)
        if type(replicas) is not int or replicas < 1:
            raise ValueError("replicas must be a positive integer")
        engines = {
            "vllm": runtime_pb.ServingEngine.VLLM,
            "sglang": runtime_pb.ServingEngine.SGLANG,
        }
        if engine not in engines:
            raise ValueError("engine must be vllm or sglang")
        if isinstance(engine_args, (str, bytes)):
            raise TypeError("engine_args must be a sequence of argument strings")
        accelerator = _accelerator(gpu)
        protected = {
            "--model",
            "--host",
            "--port",
            "--worker-extension-cls",
            "--api-key",
            "--config",
            "--served-model-name",
        }
        if any(not isinstance(arg, str) for arg in engine_args):
            raise TypeError("engine_args must contain strings")
        if any(
            any(
                option.startswith(arg.split("=", 1)[0].replace("_", "-"))
                for option in protected
            )
            for arg in engine_args
            if arg.startswith("--")
        ):
            raise ValueError(
                "engine_args cannot replace managed model, network, or serving options"
            )
        if stream.current() is None:
            raise ValueError(
                "Seed or publish the stream before deploying a live policy"
            )
        if stream.id is None:
            stream.register()
        assert (
            stream._metadata is not None
            and stream.id is not None
            and stream.base_model is not None
        )
        client = stream._metadata.client
        stub = client.stub(runtime_connect.ServingRuntimeServiceClientSync)
        resolved = client.call(
            stub.resolve_serving_runtime_image,
            runtime_pb.ResolveServingRuntimeImageRequest(
                engine=engines[engine], accelerator=accelerator
            ),
        )
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", resolved.image_ref):
            raise LivePolicyError(
                "Serving runtime resolver returned no immutable SHA256 image"
            )
        if resolved.engine != engines[engine] or resolved.accelerator != accelerator:
            raise LivePolicyError(
                "Serving runtime resolver returned mismatched selectors"
            )
        group_stub = client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
        prior_group_id = None
        try:
            existing = client.call(
                group_stub.get_scaling_group, sg_pb.GetScalingGroupRequest(name=name)
            )
            if existing.scaling_group is None or not existing.scaling_group.id:
                raise LivePolicyError(
                    "GetScalingGroup returned no prior group identity"
                )
            prior_group_id = existing.scaling_group.id
            if existing.scaling_group.weight_stream_id != stream.id:
                raise LivePolicyError(
                    "Existing scaling group belongs to another weight stream"
                )
        except ConnectError as error:
            if error.code != Code.NOT_FOUND:
                raise
        mount = "/chalk/weights"
        group = ScalingGroup(
            name=name,
            image=resolved.image_ref,
            weight_stream_id=stream.id,
            gpu=gpu,
            cpu=cpu,
            memory=memory,
            compute_class=compute_class,
            execution_target=execution_target,
            min_replicas=replicas,
            max_replicas=replicas,
            volumes=[VolumeMount(stream.volume.name, mount, ref_name="main")],
            env={
                "CHALK_WEIGHT_STREAM_DIR": mount,
                "CHALK_ENGINE": engine,
                "CHALK_LISTEN_ADDR": ":8000",
            },
            entrypoint=[
                "chalk-live-serve",
                "--engine",
                engine,
                "--weights",
                mount,
                "--",
                "--served-model-name",
                stream.base_model,
                *engine_args,
            ],
            readiness_probe=ReadinessProbe.http("/chalk/health"),
            port=8000,
            authenticated=True,
        )
        group._client = client
        policy = cls(stream, group, client)
        try:
            group.deploy(ready_timeout=ready_timeout)
            if group.info is None or group.info.weight_stream_id != stream.id:
                raise LivePolicyError(
                    "Scaling group did not retain the requested weight stream association"
                )
        except Exception as error:
            raise LivePolicyDeploymentError(policy, prior_group_id) from error
        return policy

    def _request(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        response = self.scaling_group.call(path, json=payload)
        if not 200 <= response.status < 300:
            raise LivePolicyError(f"Live policy returned HTTP {response.status}")
        header = response.headers.get("x-chalk-policy-version")
        if (
            header is None
            or len(header) > 20
            or not re.fullmatch(r"0|[1-9][0-9]*", header)
            or int(header) > 2**64 - 1
        ):
            raise LivePolicyError(
                "Live policy response has no valid policy version header"
            )
        try:
            body = json.loads(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LivePolicyError("Live policy returned invalid JSON") from error
        if not isinstance(body, dict):
            raise LivePolicyError("Live policy returned an invalid response object")
        return body, int(header)

    def generate(
        self, prompts: str | Sequence[str], **sampling: Any
    ) -> list[PolicyGeneration]:
        """Generate completions; every output carries the serving response's version."""
        if sampling.get("stream"):
            raise ValueError("LivePolicy.generate uses buffered responses")
        values = [prompts] if isinstance(prompts, str) else list(prompts)
        result = []
        for prompt_index, prompt in enumerate(values):
            body, version = self._request(
                "/v1/completions",
                {
                    **sampling,
                    "model": self.stream.base_model,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise LivePolicyError("Completion response has no choices")
            for index, choice in _choices(choices):
                if not isinstance(choice.get("text"), str):
                    raise LivePolicyError("Completion choice has no text")
                result.append(
                    PolicyGeneration(
                        choice["text"],
                        version,
                        prompt_index,
                        index,
                        choice.get("finish_reason"),
                    )
                )
        return result

    def chat(
        self, messages: Sequence[Mapping[str, Any]], **sampling: Any
    ) -> list[PolicyChatGeneration]:
        """Generate chat completions with the exact response policy version."""
        if sampling.get("stream"):
            raise ValueError("LivePolicy.chat uses buffered responses")
        body, version = self._request(
            "/v1/chat/completions",
            {
                **sampling,
                "model": self.stream.base_model,
                "messages": [dict(message) for message in messages],
                "stream": False,
            },
        )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LivePolicyError("Chat response has no choices")
        result = []
        for index, choice in _choices(choices):
            message = choice.get("message")
            if not isinstance(message, dict):
                raise LivePolicyError("Chat choice has no message")
            result.append(
                PolicyChatGeneration(
                    dict(message), version, index, choice.get("finish_reason")
                )
            )
        return result

    def replicas(self) -> list[PolicyReplica]:
        if self.scaling_group.id is None:
            raise LivePolicyError("Live policy has no deployed scaling group")
        stub = self._client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
        response = self._client.call(
            stub.list_scaling_group_replicas,
            sg_pb.ListScalingGroupReplicasRequest(
                scaling_group_key=Oneof("scaling_group_id", self.scaling_group.id)
            ),
        )
        return [
            PolicyReplica(
                item.id,
                item.name,
                item.status.name,
                item.applied_weight_version
                if item.has_field("applied_weight_version")
                else None,
            )
            for item in response.replicas
        ]

    def publisher(self, **kwargs: Any) -> WeightPublisher:
        return self.stream.publisher(**kwargs)

    def pin(self, version: int) -> None:
        self.stream.pin(version)

    def unpin(self) -> None:
        self.stream.unpin()

    def promote(
        self, version: int, model_name: str, *, aliases: Sequence[str] = ()
    ) -> Any:
        """Request checkpoint promotion; currently the service returns Unimplemented.

        Raw patches are never registered as a model artifact.
        """
        from chalkcompute._gen.chalk.weightstream.v1 import service_pb as weight_pb

        if self.stream._metadata is None:
            raise LivePolicyError("Register the weight stream before promotion")
        return self.stream._metadata._call(
            "promote_weight_stream_version",
            weight_pb.PromoteWeightStreamVersionRequest(
                weight_stream_id=self.stream._metadata.id,
                version=version,
                model_name=model_name,
                aliases=list(aliases),
            ),
        )

    def delete(self) -> None:
        """Delete the scaling group, preserving the externally owned weight volume."""
        if self.scaling_group.id is None:
            stub = self._client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
            try:
                response = self._client.call(
                    stub.get_scaling_group,
                    sg_pb.GetScalingGroupRequest(name=self.scaling_group._name),
                )
            except ConnectError as error:
                if error.code == Code.NOT_FOUND:
                    return
                raise
            if (
                response.scaling_group is None
                or response.scaling_group.weight_stream_id != self.stream.id
            ):
                raise LivePolicyError(
                    "Refusing to delete a group with a different weight stream association"
                )
            self.scaling_group._scaling_group_id = response.scaling_group.id
            self.scaling_group._scaling_group_info = _parse_scaling_group_response(
                response.scaling_group
            )
        self.scaling_group.delete()


def _choices(choices: list[Any]) -> list[tuple[int, dict[str, Any]]]:
    indexed = {}
    for choice in choices:
        if not isinstance(choice, dict):
            raise LivePolicyError("Generation choice is not an object")
        index = choice.get("index")
        if type(index) is not int or index < 0 or index in indexed:
            raise LivePolicyError(
                "Generation choices have missing, invalid, or duplicate indices"
            )
        indexed[index] = choice
    if set(indexed) != set(range(len(indexed))):
        raise LivePolicyError("Generation choice indices are incomplete")
    return sorted(indexed.items())


def _accelerator(gpu: str) -> runtime_pb.ServingAccelerator:
    # ContainerSpec forwards canonical GPU strings. SDK callers use both
    # documented count prefixes and the platform's count suffix form.
    parts = gpu.lower().split(":")
    if len(parts) == 2:
        count, value = parts if parts[0].isdigit() else reversed(parts)
        if not count.isdigit() or int(count) < 1:
            raise ValueError("GPU count must be positive")
    elif len(parts) == 1:
        value = parts[0]
    else:
        raise ValueError("Invalid GPU request")
    if value.startswith("nvidia-"):
        return runtime_pb.ServingAccelerator.CUDA
    if value.startswith("amd-"):
        return runtime_pb.ServingAccelerator.ROCM
    raise ValueError("GPU must identify a nvidia- or amd- accelerator")
