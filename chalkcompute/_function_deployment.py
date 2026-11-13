"""Local preparation and the shared function/class ensure protocol."""

from __future__ import annotations

import posixpath
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute._connect import ConnectClient, ConnectError, wait_for_image_build
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect,
    service_pb,
)
from chalkcompute._image import Image
from chalkcompute._volume import (
    VolumeClient,
    VolumeMount,
    VolumeError,
    _prepare_source_files,
)


if TYPE_CHECKING:
    from chalkcompute._native import PreparedFiles


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
    *,
    image: Image,
    referenced: Sequence[VolumeMount],
) -> list[VolumeMount]:
    """Validate explicit mounts without allocating function storage."""
    source_paths = set(image.lazy_local_files_grouped_for_volume_mounts())
    overlap = source_paths & {mount.mount_path for mount in referenced}
    if overlap:
        raise ValueError(f"Source mount overlaps a user volume at {min(overlap)!r}")
    return list(referenced)


def _source_trees(
    image: Image,
) -> tuple[list[service_pb.FunctionSourceMount], dict[str, PreparedFiles]]:
    """Identify each source root by the commit ID of its file tree."""
    mounts = []
    prepared_by_digest = {}
    for mount_path, prepared in _prepare_source_files(image):
        prepared_by_digest[prepared.commit_id] = prepared
        mounts.append(
            service_pb.FunctionSourceMount(
                tree_digest=prepared.commit_id, mount_path=mount_path
            )
        )
    return mounts, prepared_by_digest


def _source_manifests(
    mounts: list[service_pb.FunctionSourceMount],
    prepared_by_digest: dict[str, PreparedFiles],
) -> tuple[
    list[service_pb.FunctionSourceMount],
    dict[str, service_pb.FunctionSourceManifest],
    dict[str, PreparedFiles],
]:
    """Describe the same sources for servers that only accept manifests."""
    manifest_mounts = []
    manifests = {}
    prepared_by_content = {}
    for mount in mounts:
        prepared = prepared_by_digest[mount.tree_digest]
        data = prepared.source_manifest()
        manifest = service_pb.FunctionSourceManifest(
            tree_digest=data["tree_digest"],
            files=[service_pb.FunctionSourceFile(**file) for file in data["files"]],
        )
        manifests[manifest.tree_digest] = manifest
        for file in manifest.files:
            prepared_by_content[file.content_digest] = prepared
        manifest_mounts.append(
            service_pb.FunctionSourceMount(
                tree_digest=manifest.tree_digest, mount_path=mount.mount_path
            )
        )
    return manifest_mounts, manifests, prepared_by_content


def _manifest_inputs(
    volume_client: VolumeClient,
    missing: service_pb.MissingFunctionDeploymentInputs,
    manifests: dict[str, service_pb.FunctionSourceManifest],
    prepared_by_content: dict[str, PreparedFiles],
) -> service_pb.FunctionDeploymentInputs:
    """Supply the manifests and digest-staged files an old server asked for."""
    supplied = service_pb.FunctionDeploymentInputs(
        manifests=[manifests[digest] for digest in missing.tree_digests],
    )
    if missing.file_digests:
        if missing.upload_volume is None:
            raise ConnectError("Source upload response omitted its volume")
        groups: dict[PreparedFiles, list[str]] = {}
        for digest in missing.file_digests:
            groups.setdefault(prepared_by_content[digest], []).append(digest)
        intent = volume_client._stage_source_inputs(missing.upload_volume.name, groups)
        if intent.deltas is None or intent.deltas.field != "path_deltas":
            raise ConnectError("Source upload did not produce file deltas")
        supplied.files = intent.deltas.value.upserts
        supplied.uploaded_objects = intent.uploaded_object_references
        supplied.upload_volume = missing.upload_volume
    return supplied


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
    sources, prepared_by_digest = _source_trees(image)
    manifests: dict[str, service_pb.FunctionSourceManifest] | None = None
    prepared_by_content: dict[str, PreparedFiles] = {}
    spec = service_pb.DesiredFunctionSpec(
        version_spec=version_spec,
        image=image.to_proto(),
        local_secrets=local_secrets,
        sources=sources,
    )
    request = service_pb.EnsureExternalFunctionRequest(
        request_id=str(uuid.uuid4()),
        function_name=function_name,
        spec=spec,
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
        if response.result.field == "missing_inputs":
            missing = response.result.value
            if manifests is None and missing.upload_volume is None:
                # Old servers need source_manifest and select_by_digest, so keep the chalk_volume_client pin before chalk-private#52666 until this fallback is removed.
                manifest_sources, manifests, prepared_by_content = _source_manifests(
                    sources, prepared_by_digest
                )
                spec.sources = manifest_sources
                # The server rejects a changed spec under the same request ID.
                request.request_id = str(uuid.uuid4())
                request.supplied_inputs = None
                continue
            volume_client = VolumeClient.from_connect(client)
            try:
                if manifests is not None:
                    request.supplied_inputs = _manifest_inputs(
                        volume_client, missing, manifests, prepared_by_content
                    )
                    continue
                upload_volume = missing.upload_volume
                if upload_volume is None:
                    raise ConnectError("Source upload response omitted its volume")
                # The server allocates the volume, and the tree is committed to it
                # under its digest. The next ensure call finds that commit.
                for digest in missing.tree_digests:
                    volume_client._resolve_prepared(
                        upload_volume.name,
                        prepared_by_digest[digest],
                        timeout=max(0.1, deadline - time.monotonic()),
                        poll_interval=poll_interval,
                    )
            except VolumeError as exc:
                if exc.code not in (
                    "unavailable",
                    "deadline_exceeded",
                    "resource_exhausted",
                ):
                    raise
                time.sleep(poll_interval)
            continue
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
