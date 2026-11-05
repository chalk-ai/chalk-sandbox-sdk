"""Scoped no-replay scaling-group client and bounded exact-name lookup."""

from __future__ import annotations

import time
import json


def make_client(guard=None):
    from chalkcompute._connect import ConnectClient
    from chalkcompute._http import SYNC_HTTP_CLIENT
    from chalkcompute._rpc import RPC_READ_MAX_BYTES
    from connectrpc.protocol import ProtocolType

    class Client(ConnectClient):
        def stub(self, stub_cls):
            cls = stub_cls
            if cls.__name__ != "ScalingGroupManagerServiceClientSync":
                return super().stub(cls)
            self._ensure_auth()
            if cls not in self._typed_clients:
                self._typed_clients[cls] = cls(
                    self._api_server,
                    protocol=ProtocolType.GRPC,
                    interceptors=(),
                    http_client=SYNC_HTTP_CLIENT,
                    read_max_bytes=RPC_READ_MAX_BYTES,
                )
            return self._typed_clients[cls]

        def call(self, method, request, **kwargs):
            name = getattr(method, "__name__", "")
            if name != "create_scaling_group":
                kwargs["timeout"] = min(kwargs.get("timeout", 10), 10)
                return super().call(method, request, **kwargs)
            if guard is None:
                raise RuntimeError("Read-only client refuses compute create")
            guard.check_budget()
            record = guard.record
            if record.get("data_transfer_verified") is not True:
                raise RuntimeError(
                    "CPU data transfer must be verified before GPU claim"
                )
            if any(
                v not in record.get("released_groups", [])
                or v not in record.get("profile_native_release_proofs", {})
                for v in record.get("group_ids", {}).values()
            ):
                raise RuntimeError(
                    "One-A100 cap: previous group release is unconfirmed"
                )
            if record.get("submission_inflight", {}).get(name):
                raise RuntimeError(
                    "A create is already ambiguous/in flight; replay refused"
                )
            pin_placement(request)
            kwargs["timeout"] = min(30, record["gpu_deadline_epoch"] - time.time())
            if kwargs["timeout"] <= 0:
                raise TimeoutError("GPU lease expired")
            record.setdefault("submission_inflight", {})[name] = True
            record.setdefault("submission_deadlines_epoch", {})[name] = (
                time.time() + kwargs["timeout"]
            )
            guard.save()
            persisted = json.loads((guard.out / "launcher.json").read_bytes())
            if (
                persisted.get("submission_inflight", {}).get(name) is not True
                or persisted.get("submission_deadlines_epoch", {}).get(name)
                != record["submission_deadlines_epoch"][name]
            ):
                raise RuntimeError("GPU create ownership was not durably persisted")
            response = super().call(method, request, **kwargs)
            if response.scaling_group:
                record.setdefault("group_ids", {})[response.scaling_group.name] = (
                    response.scaling_group.id
                )
            record["submission_inflight"][name] = False
            guard.save()
            guard.check_budget()
            return response

    return Client()


def lookup_group(name, client=None):
    import chalkcompute as cc
    from chalkcompute._scaling_group import (
        _get_scaling_group,
        _parse_scaling_group_response,
        _scaling_group_response_is_authenticated,
    )
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    client = client or make_client()
    try:
        response = _get_scaling_group(client, name=name)
    except ConnectError as error:
        if error.code == Code.NOT_FOUND:
            return None
        raise
    if response.scaling_group is None:
        raise RuntimeError("Missing group lookup response")
    group = cc.ScalingGroup._make_empty()
    group._client = client
    group._scaling_group_info = _parse_scaling_group_response(response.scaling_group)
    group._scaling_group_id = group._scaling_group_info.id
    group._name = name
    group._authenticated = _scaling_group_response_is_authenticated(
        response.scaling_group
    )
    return group


def pin_placement(request):
    """ScalingGroup lacks public runtime/pool options; pin its scoped wire request."""
    from protobuf import Oneof
    from chalkcompute._gen.chalk.container.v1 import service_pb as cp
    from profile_host_guard import POOL

    if request is None or request.spec is None or request.spec.container_spec is None:
        raise ValueError("Missing concrete serving container specification")
    spec = request.spec.container_spec
    if spec.compute_class != cp.ComputeClass.HOST:
        raise ValueError("Profile serving compute must be HOST")
    spec.runtime = cp.Runtime.GVISOR
    spec.placement = Oneof("host_placement", cp.HostPlacementOptions(host_pool_id=POOL))
