"""Bind deployed Chalk models into the local chalkdf ``CatalogRuntime``.

Two entry points:

* :func:`bind_model` — register one model imperatively (call from inside the
  user's function body).
* :func:`apply_models_from_env` — decode the ``_CHALK_BIND_MODELS`` env var
  produced by :func:`encode_models_env` and call :func:`bind_model` for each
  entry. The chalkcompute handler shim calls this on module import, so models
  declared via ``@chalkcompute.function(models=...)`` are bound automatically
  before the user function runs.

Allows ``F.catalog_call("model.<name>", ...)`` to resolve inside a
``@chalkcompute.function`` pod, where the engine runs with a bare local
``CatalogRuntime`` that has no model bindings by default.

Usage::

    import chalkcompute
    import chalk.functions as F
    import pyarrow as pa

    EMBEDDING_TYPE = pa.list_(pa.float32())

    @chalkcompute.function(...)
    def my_pipeline() -> None:
        chalkcompute.bind_model("qwen3-prod-test", output_type=EMBEDDING_TYPE)
        df = ...
        df.with_columns(
            F.catalog_call(
                "model.qwen3-prod-test", text, output_type=EMBEDDING_TYPE,
            ).alias("embedding")
        )
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, TypeAlias

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof

from chalkcompute._connect import (
    ConnectClient,
    grpc_target_from_url,
    service_auth_context_from_env,
)
from chalkcompute._gen.chalk.modeldeployment.v1 import service_connect as deploy_connect
from chalkcompute._gen.chalk.modeldeployment.v1 import service_pb as deploy_pb
from chalkcompute._gen.chalk.models.v1 import model_version_pb
from chalkcompute._gen.chalk.scalinggroup.v1 import service_connect as sg_connect
from chalkcompute._gen.chalk.scalinggroup.v1 import service_pb as sg_pb
from chalkcompute._gen.chalk.server.v1 import model_registry_connect, model_registry_pb

if TYPE_CHECKING:
    import pyarrow as pa

logger = logging.getLogger(__name__)

MODELS_ENV_VAR = "_CHALK_BIND_MODELS"
"""Env var name read by :func:`apply_models_from_env`.

Value is a base64-encoded Arrow IPC schema where each field name is a model
name and each field type is the model's output ``pa.DataType``. Bindings with
selectors carry field metadata and a ``v2:`` prefix so older SDK runtimes fail
instead of silently ignoring their selectors.
"""

_MODELS_V2_PREFIX = "v2:"
_DEPLOYMENT_METADATA = b"chalkcompute.deployment_name"
_VERSION_METADATA = b"chalkcompute.version"


def _validate_model_selectors(deployment_name: str | None, version: int | None) -> None:
    if deployment_name is not None:
        if not isinstance(deployment_name, str):
            raise TypeError("deployment_name must be a non-empty str")
        if not deployment_name:
            raise ValueError("deployment_name must be a non-empty str")
    if version is not None:
        if isinstance(version, bool) or not isinstance(version, int):
            raise TypeError("version must be a positive int")
        if not 0 < version < 2**63:
            raise ValueError("version must be a positive int less than 2**63")


@dataclass(frozen=True)
class ModelBindingSpec:
    """A model binding for ``@function(models=...)`` or ``@cls(models=...)``.

    The mapping key is the model name and its catalog name is ``model.<key>``.
    ``deployment_name`` selects an exact deployment of that model. ``version``
    defaults to the latest published version, including with a deployment
    selector; pass an older version explicitly when that deployment serves it.
    The deployed image must contain an SDK supporting this binding format.
    """

    output_type: pa.DataType
    deployment_name: str | None = None
    version: int | None = None

    def __post_init__(self) -> None:
        import pyarrow as pa

        if not isinstance(self.output_type, pa.DataType):
            raise TypeError("model output_type must be a pa.DataType")
        _validate_model_selectors(self.deployment_name, self.version)


_ModelBindingValue: TypeAlias = "pa.DataType | ModelBindingSpec"


class ModelError(Exception):
    """Base exception for model-binding errors."""


class ModelNotFoundError(ModelError):
    """Raised when the named model is not present in the Chalk model registry."""


class ModelNotDeployedError(ModelError):
    """Raised when the selection has no active deployment with a web URL."""


def bind_model(
    name: str,
    *,
    output_type: "pa.DataType",
    version: int | None = None,
    deployment_name: str | None = None,
    qualified_name: str | None = None,
    method_name: str | None = None,
    client: ConnectClient | None = None,
    chalk_client: Any | None = None,
) -> None:
    """Register a deployed Chalk model in the local chalkdf catalog.

    After this call, ``F.catalog_call(qualified_name, ..., output_type=output_type)``
    resolves to a gRPC call against the model's scaling group.

    Parameters
    ----------
    name
        Either a **scaling group name** (the engine's resolution convention for
        ``F.catalog_call("model.<X>", ...)``) or a **model name** from the Chalk
        model registry. Lookup tries scaling-group first and falls back to
        treating ``name`` as a model name. With ``deployment_name``, ``name``
        is always treated as a model name and direct scaling-group lookup is
        skipped.
    output_type
        PyArrow type of the model's output column. Required for plan-time type
        resolution in chalkdf.
    version
        Optional explicit model version, used only when ``name`` resolves via
        model-based path. Rejected if ``name`` resolves directly as a scaling
        group name. Defaults to the model's latest published version, including
        when ``deployment_name`` is set. Selection checks the deployment's
        current revision at binding time; later redeployments can change it.
    deployment_name
        Optional exact deployment name for the model. Without it, the requested
        model version must have exactly one active deployment with a nonempty
        ``web_url``. To select a deployment serving an older published version,
        pass that version explicitly. A URL is not a connectivity check.
    qualified_name
        Optional override for the FQN registered in the local catalog. Defaults
        to ``f"model.{name}"``, matching what you'd pass to
        ``F.catalog_call``.
    method_name
        Optional override for the method-name string the catalog passes through
        to the supplier. Defaults to ``name``. Empirically non-load-bearing for
        ``chalk-remote-call``-backed scaling groups (the actual gRPC route is
        always ``"handler"``), but exposed for forward compatibility.
    client
        Optional pre-configured ``ConnectClient``. Useful for tests; defaults to
        a fresh client driven by the standard ``CHALK_*`` env vars.

    Raises
    ------
    ModelNotFoundError
        Neither a scaling group nor a model with this name was found.
    ModelNotDeployedError
        A model with this name exists but has no scaling group with a
        ``web_url``.
    ModelError
        Ambiguous selection, inconsistent responses, or any other RPC failure.
    """
    _validate_model_selectors(deployment_name, version)
    # `chalkdf` must be imported first; it bootstraps `libchalk` onto sys.path.
    from chalkdf import Catalog
    from libchalk.runtime.remote_call import (  # ty: ignore[unresolved-import]
        GrpcChannelCredentials,
        RemoteCallClientSupplier,
    )

    fqn = qualified_name or f"model.{name}"
    runtime = Catalog.local_catalog().runtime
    if runtime.contains(fqn):
        logger.debug("Model %r already bound to %r; skipping", name, fqn)
        return

    client = client or ConnectClient(chalk_client=chalk_client)
    web_url = _resolve_web_url(
        client, name, version=version, deployment_name=deployment_name
    )

    target, use_tls = grpc_target_from_url(web_url)
    creds = (
        GrpcChannelCredentials.SSL() if use_tls else GrpcChannelCredentials.Insecure()
    )
    metadata = _auth_metadata(client)

    supplier = RemoteCallClientSupplier.from_grpc_host(target, creds, metadata)
    runtime.register_function(
        fqn,
        supplier,
        method_name or name,
        output_type,
    )
    logger.info(
        "Bound %r to catalog as %r → %s",
        name,
        fqn,
        target,
    )


@dataclass(frozen=True)
class _ModelDeployment:
    id: str
    name: str
    web_url: str
    deleted: bool
    version: int | None


def _model_deployment(
    record: deploy_pb.ModelScalingGroup | None,
    legacy: sg_pb.ScalingGroupResponse | None,
    revision: deploy_pb.ModelScalingGroupRevision | None,
) -> _ModelDeployment:
    """Normalize one aligned row, including servers with partial typed records."""
    group = record if record is not None else legacy
    if group is None:
        raise ModelError("ListModelScalingGroups returned an empty deployment row")
    if record is not None and legacy is not None:
        if record.id != legacy.id or (
            record.revision_id
            and legacy.revision_id
            and record.revision_id != legacy.revision_id
        ):
            raise ModelError("ListModelScalingGroups returned misaligned deployments")
    selected_revision = group.revision_id or (
        legacy.revision_id if legacy is not None else ""
    )
    if revision is not None:
        if (
            revision.model_scaling_group_id != group.id
            or (selected_revision and revision.id != selected_revision)
            or not revision.id
            or revision.model_version <= 0
        ):
            raise ModelError(
                "ListModelScalingGroups returned a misaligned current revision"
            )
    # Typed identity records predate their optional web_url. An explicitly
    # empty typed URL is authoritative; an absent one can use the legacy field.
    web_url = (
        record.web_url
        if record is not None and record.has_field("web_url")
        else legacy.web_url
        if legacy is not None
        else ""
    )
    return _ModelDeployment(
        id=group.id,
        name=group.name,
        web_url=web_url,
        deleted=group.deleted_at is not None
        or (legacy is not None and legacy.deleted_at is not None),
        version=revision.model_version if revision is not None else None,
    )


def _model_deployments(
    client: ConnectClient, name: str, version: int
) -> Iterator[_ModelDeployment]:
    """List the selected model version; the server owns rename-safe matching."""
    stub = client.stub(deploy_connect.ModelDeploymentServiceClientSync)
    selector = deploy_pb.ModelVersionSelector(
        model_name=name,
        identifier=model_version_pb.ModelVersionIdentifier(
            identifier=Oneof("version", version)
        ),
    )
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        try:
            page = client.call(
                stub.list_model_scaling_groups,
                deploy_pb.ListModelScalingGroupsRequest(
                    model_version=selector,
                    cursor=cursor,
                    limit=100,
                    include_deleted=False,
                ),
            )
        except ConnectRpcError as exc:
            raise ModelError(
                f"ListModelScalingGroups({name!r}, v{version}) failed: {exc}"
            ) from exc
        records, legacy, revisions = (
            page.model_scaling_groups,
            page.scaling_groups,
            page.current_revisions,
        )
        count = len(records) if records else len(legacy)
        if (records and legacy and len(records) != len(legacy)) or (
            revisions and len(revisions) != count
        ):
            raise ModelError("ListModelScalingGroups returned misaligned lists")
        for index in range(count):
            deployment = _model_deployment(
                records[index] if records else None,
                legacy[index] if legacy else None,
                revisions[index] if revisions else None,
            )
            if deployment.version is not None and deployment.version != version:
                raise ModelError(
                    f"Deployment {deployment.name!r} serves v{deployment.version}, "
                    f"but ListModelScalingGroups requested {name!r} v{version}"
                )
            yield deployment
        cursor = page.next_cursor or None
        if cursor is None:
            return
        if cursor in seen_cursors:
            raise ModelError("ListModelScalingGroups returned a repeated cursor")
        seen_cursors.add(cursor)


def _resolve_web_url(
    client: ConnectClient,
    name: str,
    *,
    version: int | None,
    deployment_name: str | None = None,
) -> str:
    """Preserve direct group lookup; share model/version selection for all bindings."""
    if deployment_name is None:
        sg_stub = client.stub(sg_connect.ScalingGroupManagerServiceClientSync)
        try:
            sg_response = client.call(
                sg_stub.get_scaling_group,
                sg_pb.GetScalingGroupRequest(name=name),
            )
        except ConnectRpcError as exc:
            if exc.code != Code.NOT_FOUND:
                raise ModelError(f"GetScalingGroup({name!r}) failed: {exc}") from exc
        else:
            if version is not None:
                raise ModelError(
                    f"version={version!r} is ambiguous: {name!r} resolved directly "
                    f"as a scaling group name. Pass the model name (not the scaling "
                    f"group name) when pinning a version."
                )
            sg = sg_response.scaling_group
            url = None if sg is None else sg.web_url or None
            if not url:
                raise ModelNotDeployedError(
                    f"Scaling group {name!r} has no webUrl "
                    f"(status={None if sg is None else sg.status!r})"
                )
            return url

    # Fallback: treat name as a model name in the registry.
    if version is None:
        registry_stub = client.stub(
            model_registry_connect.ModelRegistryServiceClientSync
        )
        try:
            model_response = client.call(
                registry_stub.get_model,
                model_registry_pb.GetModelRequest(
                    model_identifier=Oneof("model_name", name)
                ),
            )
        except ConnectRpcError as exc:
            if exc.code != Code.NOT_FOUND:
                raise ModelError(f"GetModel({name!r}) failed: {exc}") from exc
            message = (
                f"Model {name!r} was not found in this environment"
                if deployment_name is not None
                else f"{name!r} is neither a scaling group name nor a model name "
                "in this environment"
            )
            raise ModelNotFoundError(message) from exc
        model = model_response.model
        latest = None if model is None else model.latest_model_version
        version = None if latest is None else latest.version
        if not version:
            raise ModelNotDeployedError(
                f"Model {name!r} has no published versions in the registry"
            )

    deployments: list[_ModelDeployment] = []
    by_identity: dict[tuple[str, str], _ModelDeployment] = {}
    for deployment in _model_deployments(client, name, version):
        key = (
            ("id", deployment.id)
            if deployment.id
            else ("name", deployment.name)
            if deployment.name
            else None
        )
        if key is not None:
            previous = by_identity.get(key)
            if previous is not None:
                if previous != deployment:
                    raise ModelError(
                        f"ListModelScalingGroups returned conflicting rows for {key[1]!r}"
                    )
                continue
            by_identity[key] = deployment
        if (
            not deployment.deleted
            and deployment.web_url
            and (deployment_name is None or deployment.name == deployment_name)
        ):
            deployments.append(deployment)
    if not deployments:
        selection = (
            f"deployment {deployment_name!r} of " if deployment_name is not None else ""
        )
        raise ModelNotDeployedError(
            f"No active {selection}model {name!r} v{version} has a webUrl"
        )
    if len(deployments) > 1:
        candidates = ", ".join(
            sorted(
                deployment.name or deployment.id or "<unnamed>"
                for deployment in deployments
            )
        )
        raise ModelError(
            f"Model {name!r} v{version} has multiple deployments with a webUrl: "
            f"{candidates}. Pass deployment_name to select one."
        )
    return deployments[0].web_url


def encode_models_env(models: Mapping[str, _ModelBindingValue]) -> str:
    """Serialize model names mapped to output types or :class:`ModelBindingSpec`.

    Used by the ``@chalkcompute.function`` decorator to ship the binding spec
    into the deployed pod via the :data:`MODELS_ENV_VAR` environment variable.
    Round-trips with :func:`apply_models_from_env`.
    """
    import pyarrow as pa

    if not models:
        return ""
    fields = []
    has_selectors = False
    for name, value in models.items():
        if not isinstance(name, str) or not name:
            raise TypeError(f"model name must be a non-empty str, got {name!r}")
        if isinstance(value, ModelBindingSpec):
            binding = value
        elif isinstance(value, pa.DataType):
            binding = ModelBindingSpec(value)
        else:
            raise TypeError(
                f"model binding for {name!r} must be a pa.DataType or ModelBindingSpec, "
                f"got {type(value).__name__}"
            )
        metadata: dict[bytes, bytes] = {}
        if binding.deployment_name is not None:
            metadata[_DEPLOYMENT_METADATA] = binding.deployment_name.encode("utf-8")
        if binding.version is not None:
            metadata[_VERSION_METADATA] = str(binding.version).encode("ascii")
        has_selectors |= bool(metadata)
        fields.append(pa.field(name, binding.output_type, metadata=metadata or None))
    schema_bytes = pa.schema(fields).serialize().to_pybytes()
    encoded = base64.b64encode(schema_bytes).decode("ascii")
    return _MODELS_V2_PREFIX + encoded if has_selectors else encoded


def apply_models_from_env(env: Mapping[str, str] | None = None) -> list[str]:
    """Bind every model listed in :data:`MODELS_ENV_VAR`.

    Called automatically by the chalkcompute handler shim on module import,
    before the user function runs. Safe to call repeatedly — :func:`bind_model`
    is idempotent.

    Parameters
    ----------
    env
        Optional override for :data:`os.environ` (primarily for tests).

    Returns
    -------
    list[str]
        Qualified names that were bound. Empty if the env var is unset.
    """
    import pyarrow as pa

    source = env if env is not None else os.environ
    spec = source.get(MODELS_ENV_VAR)
    if not spec:
        return []

    has_selectors = spec.startswith(_MODELS_V2_PREFIX)
    if has_selectors:
        spec = spec[len(_MODELS_V2_PREFIX) :]
    elif ":" in spec:
        raise ModelError("Unsupported model binding payload version")
    try:
        schema_bytes = base64.b64decode(spec, validate=True)
        schema = pa.ipc.read_schema(pa.BufferReader(schema_bytes))
        bindings: list[tuple[str, ModelBindingSpec]] = []
        for field in schema:
            metadata = field.metadata or {}
            deployment = metadata.get(_DEPLOYMENT_METADATA)
            version = metadata.get(_VERSION_METADATA)
            if not has_selectors and (deployment is not None or version is not None):
                raise ValueError("model binding selectors require a v2: payload")
            if not field.name:
                raise ValueError("model name must be non-empty")
            bindings.append(
                (
                    field.name,
                    ModelBindingSpec(
                        output_type=field.type,
                        deployment_name=deployment.decode("utf-8")
                        if deployment is not None
                        else None,
                        version=int(version.decode("ascii"))
                        if version is not None
                        else None,
                    ),
                )
            )
    except (ValueError, TypeError, OSError, binascii.Error, pa.ArrowException) as exc:
        raise ModelError(f"Invalid model binding payload: {exc}") from exc
    bound: list[str] = []
    for name, binding in bindings:
        bind_model(
            name,
            output_type=binding.output_type,
            deployment_name=binding.deployment_name,
            version=binding.version,
        )
        bound.append(f"model.{name}")
    return bound


def _auth_metadata(client: ConnectClient) -> dict[str, str]:
    """Build gRPC metadata for a model scaling-group call.

    Mirrors ``RemoteFunction._remote_call_metadata`` in ``_function.py``:
    in-pod we forward the service-auth env context; otherwise we exchange an
    OAuth token via ``ConnectClient`` and attach it as a Bearer header.
    """
    uses_web_identity = (
        getattr(client, "_uses_web_identity_token", lambda: False)() is True
    )
    service_auth = None if uses_web_identity else service_auth_context_from_env()
    if service_auth is not None:
        return {"x-chalk-env-id": service_auth.environment_id}

    client._ensure_auth()
    return {
        "authorization": f"Bearer {client._access_token}",
        "x-chalk-env-id": client._env_id or "",
    }
