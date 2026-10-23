"""Local preparation and the shared function/class ensure protocol."""

from __future__ import annotations

import time
import uuid
import posixpath
from collections.abc import Sequence

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute._connect import ConnectClient, ConnectError, wait_for_image_build
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect,
    service_pb,
)
from chalkcompute._image import Image
from chalkcompute._volume import VolumeClient, VolumeMount


def _paths_overlap(left: str, right: str) -> bool:
    left, right = posixpath.normpath(left), posixpath.normpath(right)
    return (
        left == right
        or left.startswith(right.rstrip("/") + "/")
        or right.startswith(left.rstrip("/") + "/")
    )


def function_source_root(
    image: Image | None,
    referenced: Sequence[VolumeMount],
    *,
    package: str | None = None,
) -> str:
    """Select a source directory without covering mounted application content."""
    occupied = {posixpath.normpath(mount.mount_path) for mount in referenced}
    if "/" in occupied:
        raise ValueError("A root volume mount leaves no location for function sources")
    generated_paths = {"/app"}
    if package is not None:
        generated_paths.add(f"/app/{package}")
    if not occupied & generated_paths:
        return "/app"
    occupied.update(image.lazy_local_files_grouped_for_volume_mounts() if image else ())
    root = "/__chalk_source"
    suffix = 0
    while any(_paths_overlap(root, path) for path in occupied):
        suffix += 1
        root = f"/__chalk_source_{suffix}"
    return root


def resolve_function_volumes(
    client: ConnectClient,
    *,
    image: Image,
    volume_name: str,
    referenced: Sequence[VolumeMount],
    poll_interval: float,
    timeout: float,
) -> list[VolumeMount]:
    """Resolve function source and merge it with referenced volumes."""
    source_paths = set(image.lazy_local_files_grouped_for_volume_mounts())
    occupied_paths = {mount.mount_path for mount in referenced}
    overlap = source_paths & occupied_paths
    if overlap:
        path = min(overlap)
        raise ValueError(f"Source mount overlaps a user volume at {path!r}")
    if not source_paths:
        return list(referenced)
    sources = VolumeClient.from_connect(client)._resolve_lazy_files(
        image,
        volume_name=volume_name,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    return [*referenced, *sources]


def ensure_external_function(
    client: ConnectClient,
    *,
    function_name: str,
    image: Image,
    version_spec: service_pb.ExternalFunctionVersionSpec,
    local_secrets: dict[str, str],
    force_new_version: bool,
    poll_interval: float,
    timeout: float,
    progress_parent: str | None = None,
) -> service_pb.EnsuredExternalFunction:
    stub = client.stub(service_connect.ExternalFunctionCatalogServiceClientSync)
    deadline = time.monotonic() + timeout
    request = service_pb.EnsureExternalFunctionRequest(
        request_id=str(uuid.uuid4()),
        function_name=function_name,
        spec=service_pb.DesiredFunctionSpec(
            version_spec=version_spec,
            image=image.to_proto(),
            local_secrets=local_secrets,
        ),
        force_new_version=force_new_version,
    )
    while time.monotonic() < deadline:
        try:
            response = client.call(
                stub.ensure_external_function,
                request,
                # A cold request can verify a large upload and publish it. A
                # fixed short RPC timeout would repeatedly discard that work.
                timeout=max(0.1, deadline - time.monotonic()),
            )
        except ConnectRpcError as exc:
            if exc.code not in (Code.UNAVAILABLE, Code.DEADLINE_EXCEEDED):
                raise
            time.sleep(poll_interval)
            continue
        if response.result is None:
            raise ConnectError("Ensure response omitted its result")
        if response.result.field == "complete":
            return response.result.value
        if response.result.field == "pending_image_build":
            try:
                wait_for_image_build(
                    client,
                    response.result.value.build_id,
                    poll_interval=poll_interval,
                    timeout=max(0.0, deadline - time.monotonic()),
                    progress_parent=progress_parent,
                )
            except ConnectRpcError as exc:
                if exc.code != Code.NOT_FOUND:
                    raise
                # Workflow retention is independent of image retention. Ensure
                # can resolve the finished image or submit a replacement build.
        else:
            raise ConnectError("Ensure response omitted its result")
    raise ConnectError(f"Function deployment timed out after {timeout}s")
