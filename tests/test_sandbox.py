from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest
from chalkcompute import Image, NetworkPolicy, ResourceDetector, Sandbox, Secret
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof
from protobuf.wkt import Timestamp
from chalkcompute._containerspec import ContainerSpec
from chalkcompute._gen.chalk.sandbox.v2 import process_service_pb as process_pb
from chalkcompute._gen.chalk.sandbox.v2 import service_pb as pb
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._sandbox import SandboxClient, SandboxCreationError
from chalkcompute._volume import VolumeMount


class _RecordingSandboxClient:
    _connect = object()

    def __init__(self) -> None:
        self.created_image: str | None = None
        self.create_kwargs: dict[str, object] | None = None

    def _instantiate(self, sandbox: Sandbox, image: str, **kwargs: object) -> None:
        self.created_image = image
        self.create_kwargs = {
            **kwargs,
            "cpu": sandbox._cpu,
            "memory": sandbox._memory,
            "volumes": sandbox._volumes,
        }
        sandbox._id = "sandbox-123"


def test_sandbox_run_builds_image_before_create() -> None:
    client = _RecordingSandboxClient()
    image = Image.debian_slim().run_commands("pip install claude")
    sandbox = Sandbox(
        image=image,
        cpu="2",
        memory="4Gi",
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.build_image", return_value="registry/image:built"
    ) as build:
        sandbox.run(poll_interval=0.25, timeout=30)

    assert client.created_image == "registry/image:built"
    assert client.create_kwargs is not None
    assert client.create_kwargs["cpu"] == "2"
    assert client.create_kwargs["memory"] == "4Gi"
    build.assert_called_once_with(
        client._connect,
        image,
        poll_interval=0.25,
        timeout=30,
    )


class _Stub:
    def __init__(self) -> None:
        self.request: pb.CreateSandboxRequest | None = None

    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        self.request = request
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.RUNNING,
                created_at=Timestamp(seconds=1),
            )
        )


class _ErrorAfterBuildStub:
    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.PENDING,
                created_at=Timestamp(seconds=1),
            )
        )

    def get_sandbox(self, request: pb.GetSandboxRequest, **_kwargs: object):
        assert request.id == "sandbox-123"
        return pb.GetSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.FAILED,
                created_at=Timestamp(seconds=1),
                status_message="pod failed readiness probe: connection refused",
                status_details="Traceback:\n  load_config()\nValueError: invalid",
            )
        )


class _SucceededDuringCreationStub:
    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.PENDING,
                created_at=Timestamp(seconds=1),
            )
        )

    def get_sandbox(self, request: pb.GetSandboxRequest, **_kwargs: object):
        assert request.id == "sandbox-123"
        return pb.GetSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.SUCCEEDED,
                created_at=Timestamp(seconds=1),
                status_message="Exit code 0",
            )
        )


def test_sandbox_client_create_sends_image_uri_not_image_spec() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        with patch(
            "chalkcompute._sandbox.build_image", return_value="registry/image:built"
        ) as build:
            client.create(
                Image.debian_slim().run_commands("echo hello"),
                wait=False,
            )
    finally:
        client.close()

    assert stub.request is not None
    assert stub.request.source.value.image == "registry/image:built"
    build.assert_called_once()


@pytest.mark.parametrize("use_client_create", [False, True])
def test_sandbox_mixed_env_serializes_secret_references(use_client_create) -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    env = {
        "GIT_AUTHOR_NAME": "Elliot Marx",
        "SUPER_SECRET_VALUE": Secret.from_chalk_env("TOKEN"),
    }
    secrets = [Secret.from_chalk_env("OTHER")]
    try:
        if use_client_create:
            client.create("ubuntu:latest", env=env, secrets=secrets, wait=False)
        else:
            Sandbox(
                image="ubuntu:latest", env=env, secrets=secrets, sandbox_client=client
            ).run(wait=False)
    finally:
        client.close()

    assert stub.request is not None
    spec = stub.request.source.value
    assert dict(spec.env_vars) == {"GIT_AUTHOR_NAME": "Elliot Marx"}
    assert [ref.source.value for ref in spec.secret_refs] == ["OTHER", "TOKEN"]
    assert dict(spec.secret_refs[1].aliases) == {"TOKEN": "SUPER_SECRET_VALUE"}


@pytest.mark.parametrize("use_client_create", [False, True])
@pytest.mark.parametrize("host_pool_id", [None, "cmg1a2b3c0000q8j9k0l1m2n3o"])
def test_sandbox_host_pool_placement(
    use_client_create: bool, host_pool_id: str | None
) -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        if use_client_create:
            client.create("ubuntu:latest", host_pool_id=host_pool_id, wait=False)
        else:
            Sandbox(
                image="ubuntu:latest", host_pool_id=host_pool_id, sandbox_client=client
            ).run(wait=False)
    finally:
        client.close()

    assert stub.request is not None
    placement = stub.request.source.value.placement
    if host_pool_id is None:
        assert placement is None
    else:
        assert placement is not None
        assert placement.field == "host_placement"
        assert placement.value.host_pool_id == host_pool_id


def test_sandbox_add_credentials_configures_env_and_network_policy(monkeypatch) -> None:
    def host_bindings(sandbox: Sandbox, host: str) -> dict[str, str | Secret]:
        assert sandbox._network_policy is not None
        return {
            placeholder: secret
            for rule in sandbox._network_policy.allowed_hosts[host]
            for transform in rule.transforms
            for placeholder, secret in transform.headers_secrets.items()
        }

    # A single host and single secret produce one placeholder-backed host binding.
    sandbox = Sandbox(image="ubuntu:latest").add_credentials(
        "api.openai.com", "OPENAI_API_KEY"
    )
    openai_placeholder = sandbox._env["OPENAI_API_KEY"]
    assert openai_placeholder.startswith("__chalk-secret-")
    assert host_bindings(sandbox, "api.openai.com") == {
        openai_placeholder: Secret.from_chalk_env("OPENAI_API_KEY")
    }

    # Multiple hosts and one secret reuse one placeholder across every host.
    sandbox = Sandbox(image="ubuntu:latest").add_credentials(
        ["api.openai.com", "*.openai.com"], "OPENAI_API_KEY"
    )
    openai_placeholder = sandbox._env["OPENAI_API_KEY"]
    for host in ("api.openai.com", "*.openai.com"):
        assert host_bindings(sandbox, host) == {
            openai_placeholder: Secret.from_chalk_env("OPENAI_API_KEY")
        }

    # A single host and multiple secrets produce distinct placeholders on one host.
    sandbox = Sandbox(image="ubuntu:latest").add_credentials(
        "api.example.com",
        "API_KEY",
        Secret.from_local_env("LOCAL_TOKEN"),
    )
    api_placeholder = sandbox._env["API_KEY"]
    local_placeholder = sandbox._env["LOCAL_TOKEN"]
    assert api_placeholder != local_placeholder
    assert host_bindings(sandbox, "api.example.com") == {
        api_placeholder: Secret.from_chalk_env("API_KEY"),
        local_placeholder: Secret.from_local_env("LOCAL_TOKEN"),
    }

    # Multiple hosts and multiple secrets install every binding on every host.
    sandbox = Sandbox(image="ubuntu:latest").add_credentials(
        ["sts.amazonaws.com", "*.amazonaws.com"],
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    )
    aws_bindings = {
        sandbox._env["AWS_ACCESS_KEY_ID"]: Secret.from_chalk_env("AWS_ACCESS_KEY_ID"),
        sandbox._env["AWS_SECRET_ACCESS_KEY"]: Secret.from_chalk_env(
            "AWS_SECRET_ACCESS_KEY"
        ),
    }
    assert host_bindings(sandbox, "sts.amazonaws.com") == aws_bindings
    assert host_bindings(sandbox, "*.amazonaws.com") == aws_bindings

    # Chaining different credentials onto one host preserves both bindings.
    sandbox = (
        Sandbox(image="ubuntu:latest")
        .add_credentials("api.example.com", "FIRST_KEY")
        .add_credentials("api.example.com", "SECOND_KEY")
    )
    assert host_bindings(sandbox, "api.example.com") == {
        sandbox._env["FIRST_KEY"]: Secret.from_chalk_env("FIRST_KEY"),
        sandbox._env["SECOND_KEY"]: Secret.from_chalk_env("SECOND_KEY"),
    }

    # Chaining one credential onto different hosts reuses its original placeholder.
    sandbox = (
        Sandbox(image="ubuntu:latest")
        .add_credentials("openai.com", "API_KEY")
        .add_credentials("*.openai.com", "API_KEY")
    )
    api_placeholder = sandbox._env["API_KEY"]
    assert host_bindings(sandbox, "openai.com") == {
        api_placeholder: Secret.from_chalk_env("API_KEY")
    }
    assert host_bindings(sandbox, "*.openai.com") == {
        api_placeholder: Secret.from_chalk_env("API_KEY")
    }

    # Existing unrelated env, secrets, and policy entries remain unchanged.
    existing_rule = NetworkPolicy.Rule(
        transforms=[NetworkPolicy.Transformer(headers={"x-existing": "yes"})]
    )
    existing_secret = Secret.from_chalk_env("UNRELATED_SECRET")
    network_policy = NetworkPolicy(
        denied_routes=["169.254.0.0/16"],
        allowed_hosts={"api.example.com": [existing_rule]},
    )
    sandbox = Sandbox(
        image="ubuntu:latest",
        env={"MODE": "test"},
        secrets=[existing_secret],
        network_policy=network_policy,
    ).add_credentials("api.example.com", "API_KEY")
    assert sandbox._env["MODE"] == "test"
    assert sandbox._secrets == [existing_secret]
    assert sandbox._network_policy is network_policy
    assert sandbox._network_policy.denied_routes == ["169.254.0.0/16"]
    assert existing_rule.transforms == [
        NetworkPolicy.Transformer(headers={"x-existing": "yes"})
    ]
    [rule] = sandbox._network_policy.allowed_hosts["api.example.com"]
    assert rule.transforms[0].headers == {"x-existing": "yes"}
    assert host_bindings(sandbox, "api.example.com") == {
        sandbox._env["API_KEY"]: Secret.from_chalk_env("API_KEY")
    }

    # Existing headers_secrets bindings are reused while new bindings are appended.
    existing_placeholder = "some-user-placeholder"
    existing_transform = NetworkPolicy.Transformer(
        headers_secrets={existing_placeholder: Secret.from_chalk_env("EXISTING_KEY")}
    )
    network_policy = NetworkPolicy(
        allowed_hosts={
            "api.example.com": [NetworkPolicy.Rule(transforms=[existing_transform])]
        }
    )
    sandbox = Sandbox(
        image="ubuntu:latest",
        env={"EXISTING_KEY": existing_placeholder},
        network_policy=network_policy,
    ).add_credentials("api.example.com", "EXISTING_KEY", "NEW_KEY")
    assert sandbox._network_policy is network_policy
    assert existing_transform.headers_secrets == {
        existing_placeholder: Secret.from_chalk_env("EXISTING_KEY")
    }
    assert host_bindings(sandbox, "api.example.com") == {
        existing_placeholder: Secret.from_chalk_env("EXISTING_KEY"),
        sandbox._env["NEW_KEY"]: Secret.from_chalk_env("NEW_KEY"),
    }

    # An existing literal env value blocks a credential with the same env name.
    with pytest.raises(
        ValueError, match="environment variable 'API_KEY' is already set"
    ):
        Sandbox(image="ubuntu:latest", env={"API_KEY": "literal"}).add_credentials(
            "api.example.com", "API_KEY"
        )

    # An existing standalone secret blocks a network credential with the same env name.
    with pytest.raises(ValueError, match="already populated by secrets"):
        Sandbox(
            image="ubuntu:latest",
            secrets=[Secret.from_chalk_env("API_KEY")],
        ).add_credentials("api.example.com", "API_KEY")

    # An env-mapped secret alias blocks a network credential using the same alias.
    with pytest.raises(ValueError, match="already populated by secrets"):
        Sandbox(
            image="ubuntu:latest",
            env={"API_KEY": Secret.from_chalk_env("SOURCE_KEY")},
        ).add_credentials("api.example.com", "API_KEY")

    # Re-adding an identical binding to the same host is a no-op.
    sandbox = Sandbox(image="ubuntu:latest").add_credentials(
        "api.example.com", "API_KEY"
    )
    assert sandbox._network_policy is not None
    rule_count = len(sandbox._network_policy.allowed_hosts["api.example.com"])
    sandbox.add_credentials("api.example.com", "API_KEY")
    assert len(sandbox._network_policy.allowed_hosts["api.example.com"]) == rule_count

    # Rebinding one placeholder to a different credential on the same host fails.
    sandbox = Sandbox(image="ubuntu:latest").add_credentials(
        "api.example.com", "API_KEY"
    )
    with pytest.raises(ValueError, match="already bound to a different secret"):
        sandbox.add_credentials(
            "api.example.com",
            Secret.from_chalk_env("OTHER_KEY", alias="API_KEY"),
        )

    # Duplicate hosts in one call are rejected explicitly.
    with pytest.raises(ValueError, match="hosts must not contain duplicates"):
        Sandbox(image="ubuntu:latest").add_credentials(
            ["api.example.com", "api.example.com"], "API_KEY"
        )

    # Duplicate credential env names in one call are rejected explicitly.
    with pytest.raises(ValueError, match="specified more than once"):
        Sandbox(image="ubuntu:latest").add_credentials(
            "api.example.com", "API_KEY", "API_KEY"
        )

    # Local credentials resolve on a copied policy while the original remains reusable.
    monkeypatch.setenv("LOCAL_TOKEN", "secret-value")
    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        sandbox_client=client,  # type: ignore[arg-type]
    ).add_credentials("api.example.com", Secret.from_local_env("LOCAL_TOKEN"))
    with patch("chalkcompute._secret._upsert_secret") as upsert:
        sandbox.run()
    upsert.assert_called_once_with(client._connect, "LOCAL_TOKEN", "secret-value", ANY)
    assert client.create_kwargs is not None
    resolved_policy = client.create_kwargs["network_policy"]
    assert isinstance(resolved_policy, NetworkPolicy)
    resolved_transform = resolved_policy.allowed_hosts["api.example.com"][0].transforms[
        0
    ]
    assert resolved_transform.headers_secrets == {
        sandbox._env["LOCAL_TOKEN"]: "LOCAL_TOKEN"
    }
    assert host_bindings(sandbox, "api.example.com") == {
        sandbox._env["LOCAL_TOKEN"]: Secret.from_local_env("LOCAL_TOKEN")
    }


def test_sandbox_add_credentials_binds_every_existing_rule() -> None:
    # The proxy applies only the first rule matching a request, so a binding
    # that lives on a later rule is never substituted.
    def rule_bindings(sandbox: Sandbox, host: str) -> list[set[str]]:
        assert sandbox._network_policy is not None
        return [
            {
                placeholder
                for transform in rule.transforms
                for placeholder in transform.headers_secrets
            }
            for rule in sandbox._network_policy.allowed_hosts[host]
        ]

    # Chaining credentials onto one host keeps a single rule carrying both.
    sandbox = (
        Sandbox(image="ubuntu:latest")
        .add_credentials("api.example.com", "FIRST_KEY")
        .add_credentials("api.example.com", "SECOND_KEY")
    )
    assert rule_bindings(sandbox, "api.example.com") == [
        {sandbox._env["FIRST_KEY"], sandbox._env["SECOND_KEY"]}
    ]

    # Rules with matches each get the binding, and no catch-all rule is added
    # that would open paths they deny.
    shared = NetworkPolicy.Rule(
        match=NetworkPolicy.Match(path=NetworkPolicy.Matcher(starts_with="/v1/"))
    )
    other = NetworkPolicy.Rule(
        match=NetworkPolicy.Match(path=NetworkPolicy.Matcher(exact="/health")),
        transforms=[NetworkPolicy.Transformer(headers={"x-existing": "yes"})],
    )
    sandbox = Sandbox(
        image="ubuntu:latest",
        network_policy=NetworkPolicy(
            allowed_hosts={
                "api.example.com": [shared, other],
                "other.example.com": [shared],
            }
        ),
    ).add_credentials("api.example.com", "API_KEY")
    placeholder = sandbox._env["API_KEY"]
    assert rule_bindings(sandbox, "api.example.com") == [{placeholder}, {placeholder}]
    rules = sandbox._network_policy.allowed_hosts["api.example.com"]
    assert [rule.match for rule in rules] == [shared.match, other.match]
    assert rules[1].transforms[0].headers == {"x-existing": "yes"}

    # A rule shared with another host is replaced, not mutated, so the
    # credential is not exposed to that host.
    assert shared.transforms == []
    assert rule_bindings(sandbox, "other.example.com") == [set()]


def test_sandbox_client_create_can_request_chalk_identity() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create("python:3.14-slim", chalk_identity=True, wait=False)
    finally:
        client.close()

    assert stub.request is not None
    assert stub.request.source.value.has_field("chalk_workload_identity")


def test_sandbox_client_create_omits_chalk_identity_by_default() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create("python:3.14-slim", wait=False)
    finally:
        client.close()

    assert stub.request is not None
    assert not stub.request.source.value.has_field("chalk_workload_identity")


def test_sandbox_client_create_sends_resource_detectors() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create(
            "python:3.14-slim",
            resource_detectors=[ResourceDetector.GITHUB_PULL_REQUESTS],
            wait=False,
        )
    finally:
        client.close()

    assert stub.request is not None
    detection = stub.request.source.value.resource_detection
    assert detection is not None
    assert list(detection.detectors) == [
        container_pb.ResourceDetector.GITHUB_PULL_REQUESTS
    ]


def test_resource_detector_implicitly_allows_its_hosts() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    policy = NetworkPolicy(allowed_hosts={"example.com": []})
    try:
        client.create(
            "python:3.14-slim",
            network_policy=policy,
            resource_detectors=[ResourceDetector.GITHUB_PULL_REQUESTS],
            wait=False,
        )
        client.create(
            "python:3.14-slim",
            resource_detectors=[ResourceDetector.GITHUB_PULL_REQUESTS],
            wait=False,
        )
    finally:
        client.close()

    # The caller's policy is not mutated.
    assert list(policy.allowed_hosts) == ["example.com"]
    assert stub.request is not None
    assert "api.github.com" in stub.request.source.value.network_policy.allowed_hosts


def test_sandbox_client_create_omits_resource_detection_by_default() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create("python:3.14-slim", wait=False)
    finally:
        client.close()

    assert stub.request is not None
    assert not stub.request.source.value.has_field("resource_detection")


def test_sandbox_rejects_invalid_resource_detectors() -> None:
    with pytest.raises(TypeError, match="resource_detectors must be a list"):
        Sandbox(
            image="python:3.14-slim",
            resource_detectors=["github_pull_requests"],  # type: ignore[list-item]
        )


def test_sandbox_rejects_non_bool_chalk_identity() -> None:
    with pytest.raises(TypeError, match="chalk_identity must be a bool"):
        Sandbox(
            image="python:3.14-slim",
            chalk_identity="yes",  # type: ignore[arg-type]
        )


def test_sandbox_client_create_defaults_entrypoint_to_sleep_infinity() -> None:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        sandbox = client.create("python:3.14-slim", wait=False)
    finally:
        client.close()

    assert stub.request is not None
    assert list(stub.request.source.value.entrypoint) == ["sleep", "infinity"]
    assert sandbox.image_uri == "python:3.14-slim"


def test_sandbox_client_create_forwards_container_fields() -> None:
    from chalkcompute import ComputeClass, ContainerSecurityPolicy, KernelPolicy
    from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb

    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create(
            "python:3.14-slim",
            gpu="nvidia-l4",
            port=8080,
            lifetime="30s",
            tags={"team": "compute"},
            compute_class=ComputeClass.HOST,
            security_policy=ContainerSecurityPolicy(
                kernel_policy=KernelPolicy.RESTRICTED
            ),
            wait=False,
        )
    finally:
        client.close()

    assert stub.request is not None
    spec = stub.request.source.value
    assert spec.resources is not None
    assert spec.resources.gpu == "nvidia-l4"
    assert spec.port == 8080
    assert dict(spec.tags) == {"team": "compute"}
    assert spec.compute_class == container_pb.ComputeClass.HOST
    assert spec.security_policy is not None
    assert spec.has_field("lifetime")


@pytest.fixture
def no_caller_tracing(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Record, instead of performing, the caller-side tracing setup ``tracing=`` triggers."""
    calls: list[None] = []
    monkeypatch.setattr(
        "chalkcompute._sandbox._request_tracing_intent", lambda: calls.append(None)
    )
    return calls


def _created_env(tracing: object, env: dict[str, str] | None = None) -> dict[str, str]:
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        client.create("python:3.14-slim", env=env, tracing=tracing, wait=False)
    finally:
        client.close()
    assert stub.request is not None
    return dict(stub.request.source.value.env_vars)


def test_sandbox_tracing_true_traces_every_root(no_caller_tracing) -> None:
    env = _created_env(True, env={"APP": "1"})
    assert env == {
        "APP": "1",
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": "1.0",
    }
    assert no_caller_tracing == [None]


def test_sandbox_tracing_policy_sets_root_sample_rate(no_caller_tracing) -> None:
    from chalkcompute import TracingPolicy

    env = _created_env(TracingPolicy(sample_rate=0.1))
    assert env["OTEL_TRACES_SAMPLER"] == "parentbased_traceidratio"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.1"
    assert no_caller_tracing == [None]


def test_sandbox_tracing_false_turns_tracing_off(no_caller_tracing) -> None:
    env = _created_env(False)
    assert env["OTEL_TRACES_SAMPLER"] == "always_off"
    assert "OTEL_TRACES_SAMPLER_ARG" not in env
    assert no_caller_tracing == []


def test_sandbox_tracing_default_keeps_platform_sampler(no_caller_tracing) -> None:
    env = _created_env(None, env={"APP": "1"})
    assert env == {"APP": "1"}
    assert no_caller_tracing == []


def test_sandbox_tracing_conflicts_with_explicit_sampler_env(no_caller_tracing) -> None:
    with pytest.raises(ValueError, match="OTEL_TRACES_SAMPLER"):
        Sandbox(
            image="ubuntu:latest",
            env={"OTEL_TRACES_SAMPLER": "always_on"},
            tracing=True,
        )


def test_sandbox_handle_rejects_tracing() -> None:
    with pytest.raises(TypeError, match="cannot be combined"):
        Sandbox(client=SandboxClient(), sandbox_id="sandbox-123", tracing=True)


def test_sandbox_exec_hands_trace_context_to_the_process(monkeypatch) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    monkeypatch.setattr(
        "chalkcompute._sandbox.trace_env",
        lambda: {"TRACEPARENT": traceparent, "BAGGAGE": "session.id=abc"},
    )
    client = SandboxClient()
    stub = _ProcessStub()
    client._process_stub = stub  # type: ignore[assignment]
    try:
        Sandbox(client=client, sandbox_id="sandbox-123").exec(
            "env", env={"APP": "1", "BAGGAGE": "mine=1"}
        )
    finally:
        client.close()

    assert stub.request is not None
    assert dict(stub.request.command.value.env_vars) == {
        "APP": "1",
        "TRACEPARENT": traceparent,
        # The caller's explicit value wins over the propagated one.
        "BAGGAGE": "mine=1",
    }


class _RuntimeStub:
    def __init__(self) -> None:
        self.spec = None

    def execute_python(self, requests, **_kwargs: object):
        from chalkcompute._gen.chalk.sandbox.v1 import runtime_service_pb as runtime_pb

        self.spec = next(requests).message.value
        return iter(
            [
                runtime_pb.ExecutePythonResponse(
                    message=Oneof(
                        "completed",
                        runtime_pb.PythonExecutionCompleted(
                            exit_code=0,
                            outcome=runtime_pb.PythonExecutionOutcome.SUCCEEDED,
                        ),
                    )
                )
            ]
        )


def test_sandbox_execute_python_hands_trace_context_to_the_process(
    monkeypatch,
) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    monkeypatch.setattr(
        "chalkcompute._sandbox.trace_env", lambda: {"TRACEPARENT": traceparent}
    )
    client = SandboxClient()
    stub = _RuntimeStub()
    client._runtime_stub = stub  # type: ignore[assignment]
    try:
        execution = Sandbox(client=client, sandbox_id="sandbox-123").execute_python(
            "print(1)", env={"APP": "1"}
        )
    finally:
        client.close()

    assert execution.succeeded
    assert stub.spec is not None
    assert dict(stub.spec.env) == {"APP": "1", "TRACEPARENT": traceparent}


def test_sandbox_exec_without_a_trace_adds_no_env(monkeypatch) -> None:
    monkeypatch.setattr("chalkcompute._sandbox.trace_env", lambda: {})
    client = SandboxClient()
    stub = _ProcessStub()
    client._process_stub = stub  # type: ignore[assignment]
    try:
        Sandbox(client=client, sandbox_id="sandbox-123").exec("env")
    finally:
        client.close()

    assert stub.request is not None
    assert dict(stub.request.command.value.env_vars) == {}


def test_sandbox_client_create_propagates_backend_error_details() -> None:
    client = SandboxClient()
    client._stub = _ErrorAfterBuildStub()  # type: ignore[assignment]
    try:
        with pytest.raises(SandboxCreationError) as exc_info:
            with patch("chalkcompute._sandbox.time.sleep"):
                client.create("python:3.14-slim", poll_interval=0, timeout=30)
    finally:
        client.close()

    error = exc_info.value
    assert str(error) == (
        "Sandbox sandbox-123 entered failed state during creation: "
        "pod failed readiness probe: connection refused\n"
        "Traceback:\n  load_config()\nValueError: invalid"
    )
    assert error.sandbox_id == "sandbox-123"
    assert error.status == "failed"
    assert error.status_message == "pod failed readiness probe: connection refused"
    assert error.status_details == "Traceback:\n  load_config()\nValueError: invalid"
    assert error.build_id is None


def test_sandbox_client_create_accepts_succeeded_during_readiness_poll() -> None:
    client = SandboxClient()
    client._stub = _SucceededDuringCreationStub()  # type: ignore[assignment]
    try:
        sandbox = client.create("python:3.14-slim", poll_interval=0, timeout=30)
    finally:
        client.close()

    assert sandbox.info is not None
    assert sandbox.info.status == "succeeded"
    assert sandbox.info.status_details is None


class _ProcessStub:
    def __init__(self) -> None:
        self.request: process_pb.OpenSessionRequest | None = None

    def open_session(self, requests, **_kwargs: object):
        self.request = next(requests)
        return iter(
            [
                process_pb.OpenSessionResponse(
                    event=Oneof(
                        "session_attached",
                        process_pb.SessionAttached(session_id="session-123"),
                    )
                ),
                process_pb.OpenSessionResponse(
                    event=Oneof(
                        "output_data",
                        process_pb.OutputData(
                            stream=process_pb.OutputData.Stream.STDOUT,
                            data=b"hello\\n",
                        ),
                    )
                ),
                process_pb.OpenSessionResponse(
                    event=Oneof("process_exited", process_pb.ProcessExited(exit_code=0))
                ),
            ]
        )


def test_sandbox_exec_uses_v2_process_service() -> None:
    client = SandboxClient()
    stub = _ProcessStub()
    client._process_stub = stub  # type: ignore[assignment]
    try:
        result = Sandbox(client=client, sandbox_id="sandbox-123").exec("echo", "hello")
    finally:
        client.close()

    assert result.session_id == "session-123"
    assert result.stdout == b"hello\\n"
    assert result.stderr == b""
    assert result.exit_code == 0
    assert stub.request is not None
    assert stub.request.command.value.sandbox_id == "sandbox-123"
    assert stub.request.command.value.command == "echo"
    assert list(stub.request.command.value.args) == ["hello"]


class _ReattachProcessStub:
    def __init__(self) -> None:
        self.calls = 0

    def open_session(self, requests, **_kwargs: object):
        next(requests)
        self.calls += 1
        if self.calls == 1:
            yield process_pb.OpenSessionResponse(
                seq=1,
                event=Oneof(
                    "session_attached",
                    process_pb.SessionAttached(session_id="session-123"),
                ),
            )
            yield process_pb.OpenSessionResponse(
                seq=2,
                event=Oneof(
                    "output_data",
                    process_pb.OutputData(
                        stream=process_pb.OutputData.Stream.STDOUT,
                        data=b"hello",
                    ),
                ),
            )
            raise ConnectRpcError(Code.UNAVAILABLE, "disconnected")

        yield process_pb.OpenSessionResponse(
            seq=2,
            event=Oneof(
                "output_data",
                process_pb.OutputData(
                    stream=process_pb.OutputData.Stream.STDOUT,
                    data=b"hello",
                ),
            ),
        )
        yield process_pb.OpenSessionResponse(
            seq=3,
            event=Oneof("process_exited", process_pb.ProcessExited(exit_code=0)),
        )


def test_sandbox_reattach() -> None:
    client = SandboxClient()
    client._process_stub = _ReattachProcessStub()  # type: ignore[assignment]
    try:
        with patch("chalkcompute._sandbox.time.sleep"):
            result = Sandbox(client=client, sandbox_id="sandbox-123").exec("echo")
    finally:
        client.close()

    assert result.session_id == "session-123"
    assert result.stdout == b"hello"
    assert result.exit_code == 0


class _TerminatedStub:
    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.TERMINATED,
                created_at=Timestamp(seconds=1),
                status_message="Exit code 0",
            )
        )


def test_terminated_during_creation_includes_status_message() -> None:
    from chalkcompute._sandbox import SandboxTerminatedError

    client = SandboxClient()
    client._stub = _TerminatedStub()  # type: ignore[assignment]
    try:
        with pytest.raises(SandboxTerminatedError, match="Exit code 0"):
            client.create("python:3.14-slim", wait=True)
    finally:
        client.close()


def test_sandbox_referenced_volume_is_existence_checked() -> None:
    from chalkcompute._volume import VolumeMount

    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        volumes=[("my-data", "/data")],
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.referenced_volume_mount",
        return_value=VolumeMount(name="my-data", mount_path="/data"),
    ) as resolve:
        sandbox.run()

    resolve.assert_called_once_with(
        client._connect, VolumeMount(name="my-data", mount_path="/data")
    )
    assert client.create_kwargs is not None
    assert client.create_kwargs["volumes"] == [
        VolumeMount(name="my-data", mount_path="/data")
    ]


def test_sandbox_accepts_version_pinned_volume_handle() -> None:
    from chalkcompute._volume import Volume, VolumeMount

    volume = Volume("my-data", client=MagicMock()).at_version(7)
    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        volumes=[volume],
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.referenced_volume_mount",
        side_effect=lambda _connect, mount: mount,
    ):
        sandbox.run()

    assert volume.version_id == 7
    assert client.create_kwargs is not None
    assert client.create_kwargs["volumes"] == [
        VolumeMount(
            name="my-data",
            mount_path="/volumes/my-data",
            version_id=7,
        )
    ]


def test_sandbox_accepts_writable_forked_volume_handle() -> None:
    from chalkcompute._volume import ForkInfo, Volume, VolumeMount

    volume_client = MagicMock()
    volume_client._create_ref.return_value = ForkInfo(name="sandbox-a", version_id=7)
    volume = Volume("my-data", client=volume_client).at_version(7).fork("sandbox-a")
    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        volumes=[volume],
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.referenced_volume_mount",
        side_effect=lambda _connect, mount: mount,
    ):
        sandbox.run()

    assert volume.version_id is None
    assert client.create_kwargs is not None
    assert client.create_kwargs["volumes"] == [
        VolumeMount(
            name="my-data",
            mount_path="/volumes/my-data",
            ref_name="sandbox-a",
        )
    ]


def test_version_pinned_mount_is_serialized_to_proto() -> None:
    mount = (
        ContainerSpec(
            image="ubuntu:latest",
            volumes=[VolumeMount(name="my-data", mount_path="/data", version_id=7)],
        )
        .to_proto()
        .volumes[0]
    )

    assert mount.version_id == 7
    assert mount.has_field("version_id")


@pytest.mark.parametrize(
    "version_id, ref, expected_ref",
    [(None, "main", ""), (None, "sandbox-a", "sandbox-a"), (7, "main", "")],
)
def test_sandbox_client_create_serializes_volume_handle_tuple(
    version_id: int | None, ref: str, expected_ref: str
) -> None:
    from chalkcompute import VersionedVolume

    volume = VersionedVolume("code", client=MagicMock(), version_id=version_id, ref=ref)
    client = SandboxClient()
    stub = _Stub()
    client._stub = stub  # type: ignore[assignment]
    try:
        with patch(
            "chalkcompute._sandbox.referenced_volume_mount",
            side_effect=lambda _connect, mount: mount,
        ) as resolve:
            client.create("ubuntu:latest", volumes=[(volume, "/code")], wait=False)
        resolve.assert_called_once_with(
            ANY,
            VolumeMount(
                "code", "/code", version_id=version_id, ref_name=expected_ref or None
            ),
        )
    finally:
        client.close()

    assert stub.request is not None
    mount = stub.request.source.value.volumes[0]
    assert (mount.name, mount.mount_path, mount.type) == (
        "code",
        "/code",
        "versioned_chalkfs",
    )
    assert mount.version_id == (version_id or 0)
    assert mount.has_field("version_id") == (version_id is not None)
    assert mount.ref_name == expected_ref
    assert mount.has_field("ref_name") == bool(expected_ref)


def test_fork_mount_is_serialized_to_proto() -> None:
    mount = (
        ContainerSpec(
            image="ubuntu:latest",
            volumes=[
                VolumeMount(name="my-data", mount_path="/data", ref_name="sandbox-a")
            ],
        )
        .to_proto()
        .volumes[0]
    )

    assert mount.ref_name == "sandbox-a"
    assert mount.has_field("ref_name")


def test_sandbox_missing_referenced_volume_fails_run() -> None:
    import pytest

    from chalkcompute._volume import VolumeError

    client = _RecordingSandboxClient()
    sandbox = Sandbox(
        image="ubuntu:latest",
        volumes=[("nope", "/data")],
        sandbox_client=client,  # type: ignore[arg-type]
    )

    with patch(
        "chalkcompute._sandbox.referenced_volume_mount",
        side_effect=VolumeError("Referenced volume 'nope' was not found."),
    ):
        with pytest.raises(VolumeError, match="nope"):
            sandbox.run()


@pytest.mark.parametrize("wait", [False, True])
def test_sandbox_uploads_lazy_local_files_without_exec_probe(tmp_path, wait) -> None:
    from chalkcompute._volume import VolumeClient, VolumeMount

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("print('hi')\n")

    client = SandboxClient()
    stub = MagicMock()
    stub.create_sandbox.return_value = pb.CreateSandboxResponse(
        sandbox=pb.SandboxInfo(id="sandbox-123", status=pb.SandboxStatus.PENDING)
    )
    stub.get_sandbox.return_value = pb.GetSandboxResponse(
        sandbox=pb.SandboxInfo(id="sandbox-123", status=pb.SandboxStatus.RUNNING)
    )
    client._stub = stub
    image = Image.debian_slim().add_local_dir(str(src), "/app")
    sandbox = Sandbox(
        image=image,
        sandbox_client=client,
    )

    volume_client = MagicMock(spec=VolumeClient)
    volume_client.upload_lazy_files.return_value = [
        VolumeMount(name="sandbox-files-abc", mount_path="/app")
    ]

    try:
        with (
            patch("chalkcompute._sandbox.build_image", return_value="img:built"),
            patch.object(VolumeClient, "from_connect", return_value=volume_client),
            patch.object(
                Sandbox,
                "exec",
                side_effect=AssertionError("Startup must not execute commands"),
            ) as execute,
        ):
            assert sandbox.run(wait=wait) is sandbox
    finally:
        client.close()

    execute.assert_not_called()
    volume_client.upload_lazy_files.assert_called_once_with(
        image, volume_prefix="sandbox-files"
    )
    stub.create_sandbox.assert_called_once()
    spec = stub.create_sandbox.call_args.args[0].source.value
    assert spec.image == "img:built"
    assert [(mount.name, mount.mount_path) for mount in spec.volumes] == [
        ("sandbox-files-abc", "/app")
    ]
    assert sandbox.info is not None
    if wait:
        stub.get_sandbox.assert_called_once()
        assert sandbox.info.status == "running"
    else:
        stub.get_sandbox.assert_not_called()
        assert sandbox.info.status == "pending"
