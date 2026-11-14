"""Authenticated desired-state reads for a fixed model deployment revision."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from connectrpc.protocol import ProtocolType
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from chalkcompute._gen.chalk.modeldeployment.v1 import (
    weight_runtime_connect,
    weight_runtime_pb,
)
from chalkcompute._http import SYNC_HTTP_CLIENT
from chalkcompute._web_identity import read_web_identity_token
from chalkcompute.weights.serving.managed_target import (
    ManagedTargetError,
    ModelTargetNotSelected,
    ManagedWeightTarget,
    TargetFence,
)

_POLL_SECONDS = 2.0
_LEASE_SECONDS = 15.0
_RPC_TIMEOUT_MS = 3000


class ModelDeploymentTargetProvider:
    """Poll one deployment using only its explicitly configured workload token.

    JWT claims decoded locally only bound cache lifetime. The API authenticates
    the token and authorizes the deployment-scoped read over TLS. This provider
    verifies publication identity; it does not attest mounted snapshot provenance
    or the weights already resident in the engine.
    """

    def __init__(
        self,
        *,
        api_server: str,
        token_file: Path,
        expected_environment_id: str,
        expected_deployment_id: str,
        expected_revision_id: str,
        expected_stream_id: str,
        expected_volume_id: str,
    ) -> None:
        url = urlsplit(api_server)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("Model target API must be an HTTPS origin")
        identities = {
            "deployment_id": expected_deployment_id,
            "revision_id": expected_revision_id,
            "stream_id": expected_stream_id,
            "volume_id": expected_volume_id,
        }
        for identity in [expected_environment_id, *identities.values()]:
            if not identity or identity.strip() != identity:
                raise ValueError("Model target expected identities must be nonempty")
        self._environment_id = expected_environment_id
        self._deployment_id = expected_deployment_id
        self._revision_id = expected_revision_id
        self._stream_id = expected_stream_id
        self._token_file = token_file
        self._fence = TargetFence(identities)
        self._lock = threading.Lock()
        self._target: ManagedWeightTarget | None = None
        self._next_poll = 0.0
        self._deadline = 0.0
        # Do not install automatic retry interceptors: a refresh has one fixed RPC deadline.
        self._client = weight_runtime_connect.ModelWeightRuntimeServiceClientSync(
            api_server.rstrip("/"),
            protocol=ProtocolType.GRPC,
            http_client=SYNC_HTTP_CLIENT,
            read_max_bytes=1 << 20,
        )

    def current(self) -> ManagedWeightTarget:
        with self._lock:
            started = time.monotonic()
            if self._target is not None and started < min(
                self._next_poll, self._deadline
            ):
                return self._fence.accept(self._target)
            # An unsuccessful refresh invalidates cached readiness immediately. A
            # later successful authenticated read may establish a new lease.
            self._target = None
            wall_started = time.time()
            try:
                token = read_web_identity_token(str(self._token_file))
                if token.environment_id != self._environment_id:
                    raise ManagedTargetError(
                        "Workload token environment differs from replica"
                    )
                if token.expires_at is None or token.expires_at <= wall_started:
                    raise ManagedTargetError(
                        "Workload token must have a live expiration"
                    )
                expiry = min(wall_started + _LEASE_SECONDS, token.expires_at)
                response = self._client.get_model_weight_runtime_target(
                    weight_runtime_pb.GetModelWeightRuntimeTargetRequest(
                        expected_revision_id=self._revision_id
                    ),
                    timeout_ms=_RPC_TIMEOUT_MS,
                    headers={
                        "authorization": f"Bearer {token.value}",
                        "x-chalk-env-id": self._environment_id,
                        "x-chalk-server": "go-api",
                    },
                )
                target = self._decode(response, expiry)
                deadline = started + target.lease_expires_at.timestamp() - wall_started
                if time.monotonic() >= deadline:
                    raise ManagedTargetError("Target refresh exhausted its lease")
                target = self._fence.accept(target)
            except ConnectError as error:
                if (
                    error.code == Code.UNAVAILABLE
                    and error.message
                    == "deployment weight target has not been selected"
                ):
                    raise ModelTargetNotSelected(
                        "Deployment weight target has not been selected"
                    ) from None
                raise ManagedTargetError(
                    "Authenticated model target refresh failed"
                ) from None
            except ManagedTargetError:
                raise
            except Exception:
                # Upstream errors may contain credential-bearing request metadata.
                raise ManagedTargetError(
                    "Authenticated model target refresh failed"
                ) from None
            self._deadline = deadline
            self._next_poll = started + _POLL_SECONDS
            self._target = target
            return target

    def _decode(
        self,
        response: weight_runtime_pb.GetModelWeightRuntimeTargetResponse,
        expiry: float,
    ) -> ManagedWeightTarget:
        if response.lease_expires_at is None:
            raise ManagedTargetError("Model target response has no lease")
        expiry = min(
            expiry,
            response.lease_expires_at.seconds + response.lease_expires_at.nanos / 1e9,
        )
        target = response.target
        if target is None or target.version is None or target.volume is None:
            raise ManagedTargetError("Deployment has no complete desired weight target")
        version = target.version
        return ManagedWeightTarget.from_json(
            json.dumps(
                {
                    "schema_version": 1,
                    "deployment_id": target.model_scaling_group_id,
                    "revision_id": target.model_scaling_group_revision_id,
                    "target_generation": target.generation,
                    "stream_id": version.weight_stream_id,
                    "logical_version": version.version,
                    "volume_id": target.volume.volume_id,
                    "volume_version_id": version.volume_version_id,
                    "manifest_sha256": version.manifest_sha256,
                    "weight_schema_sha256": version.weight_schema_sha256,
                    "lease_expires_at": datetime.fromtimestamp(
                        expiry, timezone.utc
                    ).isoformat(),
                }
            )
        )

    def assert_current(self, expected: ManagedWeightTarget) -> ManagedWeightTarget:
        current = self.current()
        if current._immutable_tuple() != expected._immutable_tuple():
            raise ManagedTargetError(
                "Target changed while weights were prepared or applied"
            )
        return current

    def manifest_digests(
        self, expected: ManagedWeightTarget, versions: tuple[int, ...]
    ) -> dict[int, str]:
        """Authenticate the bounded replay set through the deployment-scoped identity."""
        if (
            not versions
            or len(versions) > 128
            or len(set(versions)) != len(versions)
            or expected.logical_version not in versions
            or any(
                type(v) is not int or v < 0 or v > expected.logical_version
                for v in versions
            )
        ):
            raise ManagedTargetError("Invalid bounded manifest identity request")
        with self._lock:
            started = time.monotonic()
            wall_started = time.time()
            try:
                token = read_web_identity_token(str(self._token_file))
                if (
                    token.environment_id != self._environment_id
                    or token.expires_at is None
                    or token.expires_at <= wall_started
                ):
                    raise ManagedTargetError(
                        "Manifest identity requires a live workload credential"
                    )
                response = self._client.get_model_weight_runtime_target(
                    weight_runtime_pb.GetModelWeightRuntimeTargetRequest(
                        expected_revision_id=self._revision_id,
                        manifest_versions=list(versions),
                    ),
                    timeout_ms=_RPC_TIMEOUT_MS,
                    headers={
                        "authorization": f"Bearer {token.value}",
                        "x-chalk-env-id": self._environment_id,
                        "x-chalk-server": "go-api",
                    },
                )
                target = self._decode(
                    response, min(wall_started + _LEASE_SECONDS, token.expires_at)
                )
                deadline = started + target.lease_expires_at.timestamp() - wall_started
                if (
                    time.monotonic() >= deadline
                    or target._immutable_tuple() != expected._immutable_tuple()
                ):
                    raise ManagedTargetError(
                        "Target changed during manifest identity verification"
                    )
                result: dict[int, str] = {}
                for identity in response.manifest_identities:
                    if (
                        identity.version not in versions
                        or identity.version in result
                        or not re.fullmatch(r"[0-9a-f]{64}", identity.manifest_sha256)
                    ):
                        raise ManagedTargetError(
                            "Invalid immutable manifest identity response"
                        )
                    result[identity.version] = identity.manifest_sha256
                if (
                    result.keys() != set(versions)
                    or result[expected.logical_version] != expected.manifest_sha256
                ):
                    raise ManagedTargetError(
                        "Immutable manifest identity response is incomplete or inconsistent"
                    )
                target = self._fence.accept(target)
                self._target = target
                self._deadline = deadline
                self._next_poll = started + _POLL_SECONDS
                return result
            except ManagedTargetError:
                self._target = None
                raise
            except Exception:
                self._target = None
                raise ManagedTargetError(
                    "Authenticated manifest identity refresh failed"
                ) from None
