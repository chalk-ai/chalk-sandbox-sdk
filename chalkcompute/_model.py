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
import logging
import os
from typing import TYPE_CHECKING, Any, Mapping

from chalkcompute._connect import (
    ConnectClient,
    grpc_target_from_url,
    service_auth_context_from_env,
)

if TYPE_CHECKING:
    import pyarrow as pa

logger = logging.getLogger(__name__)

_REGISTRY_SVC = "chalk.server.v1.ModelRegistryService"
_DEPLOYMENT_SVC = "chalk.modeldeployment.v1.ModelDeploymentService"
_SG_SVC = "chalk.scalinggroup.v1.ScalingGroupManagerService"

MODELS_ENV_VAR = "_CHALK_BIND_MODELS"
"""Env var name read by :func:`apply_models_from_env`.

Value is a base64-encoded Arrow IPC schema where each field name is a model
name and each field type is the model's output ``pa.DataType``.
"""


class ModelError(Exception):
    """Base exception for model-binding errors."""


class ModelNotFoundError(ModelError):
    """Raised when the named model is not present in the Chalk model registry."""


class ModelNotDeployedError(ModelError):
    """Raised when the model exists but has no scaling group with a reachable URL."""


def bind_model(
    name: str,
    *,
    output_type: "pa.DataType",
    version: int | None = None,
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
        treating ``name`` as a model name and resolving its latest deployed
        scaling group.
    output_type
        PyArrow type of the model's output column. Required for plan-time type
        resolution in chalkdf.
    version
        Optional explicit model version, used only when ``name`` resolves via
        the model-registry fallback. Ignored if ``name`` is already a scaling
        group name. Defaults to the model's ``latest_model_version``.
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
        Any other RPC failure.
    """
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
    web_url = _resolve_web_url(client, name, version=version)

    target, use_tls = grpc_target_from_url(web_url)
    creds = GrpcChannelCredentials.SSL() if use_tls else GrpcChannelCredentials.Insecure()
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


def _resolve_web_url(client: ConnectClient, name: str, *, version: int | None) -> str:
    """Find a scaling group ``web_url`` for ``name``.

    Two-step resolution:

    1. Try ``GetScalingGroup({name})`` — matches the engine convention that
       ``F.catalog_call("model.<X>", ...)`` resolves ``<X>`` as a scaling group
       name. Cheap (one RPC, no enumeration) and the path the engine itself
       uses.
    2. If that 404s, fall back to treating ``name`` as a model name: look up
       the model's ``latest_model_version`` (or the explicit ``version``) and
       resolve its scaling group via ``ListModelScalingGroups``.

    Raises one of :class:`ModelNotFoundError`, :class:`ModelNotDeployedError`,
    or :class:`ModelError`.
    """
    sg_resp = client.rpc(_SG_SVC, "GetScalingGroup", {"name": name})
    if sg_resp.status_code == 200:
        if version is not None:
            raise ModelError(
                f"version={version!r} is ambiguous: {name!r} resolved directly "
                f"as a scaling group name. Pass the model name (not the scaling "
                f"group name) when pinning a version."
            )
        sg = (sg_resp.json() or {}).get("scalingGroup") or {}
        url = sg.get("webUrl")
        if not url:
            raise ModelNotDeployedError(
                f"Scaling group {name!r} has no webUrl (status={sg.get('status')!r})"
            )
        return url
    if sg_resp.status_code != 404:
        raise ModelError(f"GetScalingGroup({name!r}) failed: {sg_resp.text}")

    # Fallback: treat name as a model name in the registry.
    if version is None:
        model_resp = client.rpc(_REGISTRY_SVC, "GetModel", {"modelName": name})
        if model_resp.status_code == 404:
            raise ModelNotFoundError(
                f"{name!r} is neither a scaling group name nor a model name in "
                f"this environment"
            )
        if model_resp.status_code != 200:
            raise ModelError(f"GetModel({name!r}) failed: {model_resp.text}")
        latest = (model_resp.json().get("model") or {}).get("latestModelVersion") or {}
        version = latest.get("version")
        if version is None:
            raise ModelNotDeployedError(
                f"Model {name!r} has no published versions in the registry"
            )

    list_resp = client.rpc(
        _DEPLOYMENT_SVC,
        "ListModelScalingGroups",
        {
            "modelVersion": {
                "modelName": name,
                "identifier": {"version": int(version)},
            }
        },
    )
    if list_resp.status_code != 200:
        raise ModelError(
            f"ListModelScalingGroups({name!r} v{version}) failed: {list_resp.text}"
        )
    groups = list_resp.json().get("scalingGroups") or []
    url = next((g.get("webUrl") for g in groups if g.get("webUrl")), None)
    if url is None:
        raise ModelNotDeployedError(
            f"Model {name!r} v{version} has no scaling group with a webUrl"
        )
    return url


def encode_models_env(models: Mapping[str, "pa.DataType"]) -> str:
    """Serialize a ``{name: output_type}`` mapping into a base64 string.

    Used by the ``@chalkcompute.function`` decorator to ship the binding spec
    into the deployed pod via the :data:`MODELS_ENV_VAR` environment variable.
    Round-trips with :func:`apply_models_from_env`.
    """
    import pyarrow as pa

    if not models:
        return ""
    fields = []
    for name, dtype in models.items():
        if not isinstance(name, str) or not name:
            raise TypeError(f"model name must be a non-empty str, got {name!r}")
        if not isinstance(dtype, pa.DataType):
            raise TypeError(
                f"model output_type for {name!r} must be a pa.DataType, got {type(dtype).__name__}"
            )
        fields.append(pa.field(name, dtype))
    schema_bytes = pa.schema(fields).serialize().to_pybytes()
    return base64.b64encode(schema_bytes).decode("ascii")


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

    schema_bytes = base64.b64decode(spec)
    schema = pa.ipc.read_schema(pa.BufferReader(schema_bytes))
    bound: list[str] = []
    for field in schema:
        bind_model(field.name, output_type=field.type)
        bound.append(f"model.{field.name}")
    return bound


def _auth_metadata(client: ConnectClient) -> dict[str, str]:
    """Build gRPC metadata for a model scaling-group call.

    Mirrors ``RemoteFunction._remote_call_metadata`` in ``_function.py``:
    in-pod we forward the service-auth env context; otherwise we exchange an
    OAuth token via ``ConnectClient`` and attach it as a Bearer header.
    """
    service_auth = service_auth_context_from_env()
    if service_auth is not None:
        return {"x-chalk-env-id": service_auth.environment_id}

    client._ensure_auth()
    return {
        "authorization": f"Bearer {client._access_token}",
        "x-chalk-env-id": client._env_id or "",
    }
