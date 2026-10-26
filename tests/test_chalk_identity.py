from __future__ import annotations

from typing import Any

import chalkcompute as cc
import pytest
from chalkcompute import ChalkIdentity, Image, OIDCTokenFile, Sandbox
from chalkcompute._chalk_identity import (
    normalize_chalk_identity,
    split_oidc_token_files,
    validate_chalk_identity,
)
from chalkcompute._containerspec import ContainerSpec
from chalkcompute._gen.chalk.container.v1 import service_pb as container_pb
from chalkcompute._gen.chalk.sandbox.v2 import service_pb as pb
from chalkcompute._naming import validate_env
from chalkcompute._sandbox import SandboxClient, SandboxCreationError
from protobuf.wkt import Timestamp

SANDBOX_IDENTITY = ChalkIdentity(mode="sandbox")
SNOWFLAKE = OIDCTokenFile(audience="snowflakecomputing.com")


class _EchoStub:
    """Behaves like a server that supports OIDC token files: the spec round trips."""

    def __init__(self) -> None:
        self.request: pb.CreateSandboxRequest | None = None
        self.terminated: list[str] = []

    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        self.request = request
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-123",
                status=pb.SandboxStatus.RUNNING,
                created_at=Timestamp(seconds=1),
                spec=request.source.value,
            )
        )

    def terminate_sandbox(self, request: pb.TerminateSandboxRequest, **_kwargs: object):
        self.terminated.append(request.id)
        return pb.TerminateSandboxResponse()


class _OldServerStub(_EchoStub):
    """Behaves like a server that predates OIDC token files and drops the field."""

    def create_sandbox(self, request: pb.CreateSandboxRequest, **_kwargs: object):
        self.request = request
        spec = container_pb.ChalkContainerSpec(
            image="python:3.14-slim",
            chalk_workload_identity=container_pb.ChalkWorkloadIdentity(
                mode=container_pb.IdentityMode.WORKLOAD_PRINCIPAL
            ),
        )
        return pb.CreateSandboxResponse(
            sandbox=pb.SandboxInfo(
                id="sandbox-old",
                status=pb.SandboxStatus.RUNNING,
                created_at=Timestamp(seconds=1),
                spec=spec,
            )
        )


def _create(stub: _EchoStub, **kwargs: Any) -> Sandbox:
    client = SandboxClient()
    client._stub = stub  # type: ignore[assignment]
    try:
        return client.create("python:3.14-slim", wait=False, **kwargs)
    finally:
        client.close()


def _requested_identity(stub: _EchoStub) -> container_pb.ChalkWorkloadIdentity:
    assert stub.request is not None
    identity = stub.request.source.value.chalk_workload_identity
    assert identity is not None
    return identity


# --- descriptors ---------------------------------------------------------


def test_oidc_token_file_is_an_immutable_descriptor() -> None:
    assert SNOWFLAKE.audience == "snowflakecomputing.com"
    with pytest.raises(AttributeError):
        SNOWFLAKE.audience = "other"  # type: ignore[misc]
    assert SNOWFLAKE == OIDCTokenFile(audience="snowflakecomputing.com")


@pytest.mark.parametrize("audience", ["", "   ", None, 3])
def test_oidc_token_file_rejects_invalid_audience(audience: object) -> None:
    with pytest.raises(ValueError, match="audience"):
        OIDCTokenFile(audience=audience)  # type: ignore[arg-type]


def test_chalk_identity_modes() -> None:
    assert ChalkIdentity().mode == "creator"
    assert ChalkIdentity(mode="sandbox").mode == "sandbox"
    with pytest.raises(ValueError, match="mode must be one of"):
        ChalkIdentity(mode="admin")  # type: ignore[arg-type]


def test_normalize_chalk_identity() -> None:
    assert normalize_chalk_identity(False) is None
    assert normalize_chalk_identity(None) is None
    assert normalize_chalk_identity(True) == ChalkIdentity(mode="creator")
    assert normalize_chalk_identity(SANDBOX_IDENTITY) is SANDBOX_IDENTITY
    with pytest.raises(TypeError, match="bool or ChalkIdentity"):
        normalize_chalk_identity("yes")  # type: ignore[arg-type]


def test_validate_chalk_identity_gates_sandbox_mode_by_surface() -> None:
    validate_chalk_identity(SANDBOX_IDENTITY, allow_sandbox=True)
    validate_chalk_identity(True, allow_sandbox=False)
    validate_chalk_identity(ChalkIdentity(), allow_sandbox=False)
    with pytest.raises(ValueError, match="only supported for sandboxes"):
        validate_chalk_identity(SANDBOX_IDENTITY, allow_sandbox=False)


def test_split_oidc_token_files_preserves_order_and_leaves_the_rest() -> None:
    remaining, files = split_oidc_token_files(
        {"A": "1", "TOKEN_B": SNOWFLAKE, "C": "3", "TOKEN_D": OIDCTokenFile("d")}
    )
    assert remaining == {"A": "1", "C": "3"}
    assert list(files) == ["TOKEN_B", "TOKEN_D"]
    assert split_oidc_token_files(None) == ({}, {})


def test_descriptors_are_exported() -> None:
    assert cc.OIDCTokenFile is OIDCTokenFile
    assert cc.ChalkIdentity is ChalkIdentity


# --- every other env surface rejects the descriptor -----------------------


def test_validate_env_rejects_oidc_token_file_with_a_clear_error() -> None:
    with pytest.raises(TypeError, match=r"OIDCTokenFile values are not supported") as e:
        validate_env({"TOKEN": SNOWFLAKE})
    assert "TOKEN" in str(e.value)
    assert "Sandbox" in str(e.value)


def test_image_env_rejects_oidc_token_file() -> None:
    with pytest.raises(TypeError, match="OIDCTokenFile"):
        Image.debian_slim().env({"TOKEN": SNOWFLAKE})  # type: ignore[dict-item]


def test_container_spec_rejects_oidc_token_file_in_env() -> None:
    with pytest.raises(TypeError, match="OIDCTokenFile"):
        ContainerSpec(image="python:3.14-slim", env={"TOKEN": SNOWFLAKE})  # type: ignore[dict-item]


def test_sandbox_exec_rejects_oidc_token_file_in_per_exec_env() -> None:
    sandbox = Sandbox(image="python:3.14-slim", chalk_identity=SANDBOX_IDENTITY)
    sandbox._client = object()  # type: ignore[assignment]
    sandbox._id = "sandbox-123"
    with pytest.raises(TypeError, match="OIDCTokenFile"):
        sandbox.exec("true", env={"TOKEN": SNOWFLAKE})  # type: ignore[dict-item]


def test_scaling_group_rejects_oidc_token_file_in_env() -> None:
    with pytest.raises(TypeError, match="OIDCTokenFile"):
        cc.ScalingGroup(image="nginx:alpine", env={"TOKEN": SNOWFLAKE})  # type: ignore[dict-item]


def test_function_rejects_oidc_token_file_in_env() -> None:
    with pytest.raises(TypeError, match="OIDCTokenFile"):

        @cc.function(image=Image.debian_slim(), env={"TOKEN": SNOWFLAKE})  # type: ignore[dict-item]
        def f() -> None: ...


# --- sandbox principal identity is sandbox-only ---------------------------


def test_scaling_group_rejects_sandbox_identity() -> None:
    with pytest.raises(ValueError, match="only supported for sandboxes"):
        cc.ScalingGroup(image="nginx:alpine", chalk_identity=SANDBOX_IDENTITY)


def test_function_rejects_sandbox_identity() -> None:
    with pytest.raises(ValueError, match="only supported for sandboxes"):

        @cc.function(image=Image.debian_slim(), chalk_identity=SANDBOX_IDENTITY)
        def f() -> None: ...


def test_class_rejects_sandbox_identity() -> None:
    with pytest.raises(ValueError, match="only supported for sandboxes"):

        @cc.cls(image=Image.debian_slim(), chalk_identity=SANDBOX_IDENTITY)
        class C:
            @cc.method
            def run(self) -> None: ...


def test_container_spec_rejects_sandbox_identity_by_default() -> None:
    with pytest.raises(ValueError, match="only supported for sandboxes"):
        ContainerSpec(image="python:3.14-slim", chalk_identity=SANDBOX_IDENTITY)


def test_creator_identity_forms_are_equivalent_on_other_surfaces() -> None:
    for identity in (True, ChalkIdentity(), ChalkIdentity(mode="creator")):
        spec = ContainerSpec(image="python:3.14-slim", chalk_identity=identity)
        assert spec.to_dict()["chalkWorkloadIdentity"] == {
            "mode": "IDENTITY_MODE_CREATOR_IDENTITY"
        }
        proto = spec.to_proto().chalk_workload_identity
        assert proto is not None
        assert proto.mode == container_pb.IdentityMode.CREATOR_IDENTITY
        assert list(proto.oidc_token_files) == []
    assert "chalkWorkloadIdentity" not in ContainerSpec(image="x").to_dict()


# --- sandboxes ------------------------------------------------------------


def test_sandbox_sandbox_identity_without_files_requests_principal_mode() -> None:
    stub = _EchoStub()
    _create(stub, chalk_identity=SANDBOX_IDENTITY)
    identity = _requested_identity(stub)
    assert identity.mode == container_pb.IdentityMode.WORKLOAD_PRINCIPAL
    assert list(identity.oidc_token_files) == []


def test_sandbox_sends_declared_oidc_token_files_and_keeps_them_out_of_env() -> None:
    stub = _EchoStub()
    sandbox = _create(
        stub,
        chalk_identity=SANDBOX_IDENTITY,
        env={
            "SNOWFLAKE_AUTHENTICATOR": "WORKLOAD_IDENTITY",
            "SNOWFLAKE_TOKEN_FILE_PATH": SNOWFLAKE,
            "OTHER_TOKEN": OIDCTokenFile(audience="other.example.com"),
        },
    )
    identity = _requested_identity(stub)
    assert identity.mode == container_pb.IdentityMode.WORKLOAD_PRINCIPAL
    assert [(f.audience, f.env_var) for f in identity.oidc_token_files] == [
        ("snowflakecomputing.com", "SNOWFLAKE_TOKEN_FILE_PATH"),
        ("other.example.com", "OTHER_TOKEN"),
    ]

    # The path is injected by the platform; the SDK never sends a value for it.
    assert stub.request is not None
    env = dict(stub.request.source.value.env_vars)
    assert env["SNOWFLAKE_AUTHENTICATOR"] == "WORKLOAD_IDENTITY"
    assert "SNOWFLAKE_TOKEN_FILE_PATH" not in env
    assert "OTHER_TOKEN" not in env
    assert sandbox.id == "sandbox-123"


def test_sandbox_to_dict_declares_principal_mode_and_files() -> None:
    spec = ContainerSpec(
        image="python:3.14-slim",
        chalk_identity=SANDBOX_IDENTITY,
        oidc_token_files={"TOKEN": SNOWFLAKE},
        allow_sandbox_identity=True,
    )
    assert spec.to_dict()["chalkWorkloadIdentity"] == {
        "mode": "IDENTITY_MODE_WORKLOAD_PRINCIPAL",
        "oidcTokenFiles": [{"audience": "snowflakecomputing.com", "envVar": "TOKEN"}],
    }


@pytest.mark.parametrize("identity", [False, True, ChalkIdentity()])
def test_sandbox_rejects_oidc_token_file_without_sandbox_identity(
    identity: bool | ChalkIdentity,
) -> None:
    with pytest.raises(ValueError, match='ChalkIdentity\\(mode="sandbox"\\)'):
        Sandbox(
            image="python:3.14-slim",
            chalk_identity=identity,
            env={"TOKEN": SNOWFLAKE},
        )


@pytest.mark.parametrize(
    ("env_var", "message"),
    [
        ("CHALK_API_SERVER", "reserved"),
        ("CHALK_WEB_IDENTITY_TOKEN_FILE", "reserved"),
        ("CHALK_ENVIRONMENT", "reserved"),
        ("1BAD", "not a valid environment variable name"),
        ("BAD-NAME", "not a valid environment variable name"),
    ],
)
def test_sandbox_rejects_invalid_oidc_token_file_env_var(
    env_var: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Sandbox(
            image="python:3.14-slim",
            chalk_identity=SANDBOX_IDENTITY,
            env={env_var: SNOWFLAKE},
        )


def test_sandbox_accepts_up_to_eight_oidc_token_files() -> None:
    Sandbox(
        image="python:3.14-slim",
        chalk_identity=SANDBOX_IDENTITY,
        env={f"TOKEN_{i}": OIDCTokenFile(audience=f"aud-{i}") for i in range(8)},
    )


def test_sandbox_rejects_more_than_eight_oidc_token_files() -> None:
    with pytest.raises(ValueError, match="at most 8 OIDCTokenFile values"):
        Sandbox(
            image="python:3.14-slim",
            chalk_identity=SANDBOX_IDENTITY,
            env={f"TOKEN_{i}": OIDCTokenFile(audience=f"aud-{i}") for i in range(9)},
        )


@pytest.mark.parametrize("compute_class", [None, cc.ComputeClass.HOST])
def test_sandbox_accepts_oidc_token_files_on_host_compute(
    compute_class: cc.ComputeClass | None,
) -> None:
    Sandbox(
        image="python:3.14-slim",
        chalk_identity=SANDBOX_IDENTITY,
        compute_class=compute_class,
        env={"TOKEN": SNOWFLAKE},
    )


def test_sandbox_rejects_oidc_token_files_on_k8s_compute() -> None:
    with pytest.raises(ValueError, match="requires host compute"):
        Sandbox(
            image="python:3.14-slim",
            chalk_identity=SANDBOX_IDENTITY,
            compute_class=cc.ComputeClass.K8S,
            env={"TOKEN": SNOWFLAKE},
        )


def test_sandbox_identity_without_files_is_allowed_on_k8s_compute() -> None:
    Sandbox(
        image="python:3.14-slim",
        chalk_identity=SANDBOX_IDENTITY,
        compute_class=cc.ComputeClass.K8S,
    )


def test_sandbox_rejects_non_identity_chalk_identity() -> None:
    with pytest.raises(TypeError, match="bool or ChalkIdentity"):
        Sandbox(image="python:3.14-slim", chalk_identity="sandbox")  # type: ignore[arg-type]


def test_sandbox_create_fails_and_cleans_up_when_server_drops_token_files() -> None:
    stub = _OldServerStub()
    with pytest.raises(SandboxCreationError, match="does not support OIDCTokenFile"):
        _create(
            stub,
            chalk_identity=SANDBOX_IDENTITY,
            env={"TOKEN": SNOWFLAKE},
        )
    assert stub.terminated == ["sandbox-old"]


def test_sandbox_without_token_files_is_unaffected_by_the_server_check() -> None:
    stub = _OldServerStub()
    sandbox = _create(stub, chalk_identity=SANDBOX_IDENTITY)
    assert sandbox.id == "sandbox-old"
    assert stub.terminated == []
