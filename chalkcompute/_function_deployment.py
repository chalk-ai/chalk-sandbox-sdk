"""Local preparation and the shared function/class ensure protocol."""

from __future__ import annotations

import time
import uuid
import posixpath
import os
import stat
import struct
from pathlib import Path

import blake3
from collections.abc import Sequence

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError

from chalkcompute._connect import ConnectClient, ConnectError, wait_for_image_build
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect,
    service_pb,
)
from chalkcompute._image import Image
from chalkcompute._volume import VolumeClient, VolumeMount, _default_upload_config
from chalkcompute._gen.chalk.volume.v2 import volume_pb


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
    """Validate explicit mounts without allocating function storage."""
    source_paths = set(image.lazy_local_files_grouped_for_volume_mounts())
    overlap = source_paths & {mount.mount_path for mount in referenced}
    if overlap:
        raise ValueError(f"Source mount overlaps a user volume at {min(overlap)!r}")
    return list(referenced)


def _source_manifests(
    image: Image,
) -> tuple[
    list[service_pb.FunctionSourceMount],
    dict[str, service_pb.FunctionSourceManifest],
    dict[str, Path],
]:
    mounts = []
    manifests = {}
    paths = {}
    for mount_path, files in image.lazy_local_files_grouped_for_volume_mounts().items():
        entries = []
        for file in files:
            path = Path(file.src)
            hasher = blake3.blake3()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            mode = (
                file.mode
                if file.mode is not None
                else stat.S_IMODE(path.stat().st_mode)
            )
            entries.append(
                service_pb.FunctionSourceFile(
                    path=os.path.relpath(file.dest, mount_path).replace(os.sep, "/"),
                    content_digest=digest,
                    size=path.stat().st_size,
                    mode=mode,
                )
            )
            paths[digest] = path
        entries.sort(key=lambda entry: entry.path)
        tree = blake3.blake3(b"chalk.function-source.v1\0")
        for entry in entries:
            encoded = entry.path.encode()
            tree.update(struct.pack(">I", len(encoded)))
            tree.update(encoded)
            tree.update(bytes.fromhex(entry.content_digest))
            tree.update(struct.pack(">QI", entry.size, entry.mode))
        digest = tree.hexdigest()
        manifests[digest] = service_pb.FunctionSourceManifest(
            tree_digest=digest, files=entries
        )
        mounts.append(
            service_pb.FunctionSourceMount(tree_digest=digest, mount_path=mount_path)
        )
    return mounts, manifests, paths


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
    sources, manifests, paths = _source_manifests(image)
    request = service_pb.EnsureExternalFunctionRequest(
        request_id=str(uuid.uuid4()),
        function_name=function_name,
        spec=service_pb.DesiredFunctionSpec(
            version_spec=version_spec,
            image=image.to_proto(),
            local_secrets=local_secrets,
            sources=sources,
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
        if response.result.field == "missing_inputs":
            missing = response.result.value
            supplied = service_pb.FunctionDeploymentInputs(
                manifests=[manifests[digest] for digest in missing.tree_digests],
            )
            if missing.file_digests:
                if missing.upload_volume is None:
                    raise ConnectError("Source upload response omitted its volume")
                batch = missing.file_digests[:256]
                source = [(digest, paths[digest]) for digest in batch]
                volume_client = VolumeClient.from_connect(client)
                prepared = volume_client._inner.prepare_files(source, "main")
                staged = volume_client._inner.stage_prepared_files(
                    missing.upload_volume.name, prepared, _default_upload_config()
                )
                intent = volume_pb.CommitIntent.from_binary(
                    bytes(
                        volume_client._inner.staged_intent(
                            missing.upload_volume.name,
                            staged,
                            "main",
                            [],
                            "source_code",
                        )
                    )
                )
                if intent.deltas is None or intent.deltas.field != "path_deltas":
                    raise ConnectError("Source upload did not produce file deltas")
                supplied.files = intent.deltas.value.upserts
                supplied.uploaded_objects = intent.uploaded_object_references
                supplied.upload_volume = missing.upload_volume
            request.supplied_inputs = supplied
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
