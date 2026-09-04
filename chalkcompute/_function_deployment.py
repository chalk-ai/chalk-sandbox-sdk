"""Local preparation and the shared function/class ensure protocol."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof

from chalkcompute._connect import ConnectClient, ConnectError, wait_for_image_build
from chalkcompute._gen.chalk.externalfunctioncatalog.v1 import (
    service_connect,
    service_pb,
)
from chalkcompute._gen.chalk.volume.v2 import volume_pb
from chalkcompute._image import Image
from chalkcompute._volume import VolumeClient, _default_upload_config


@dataclass
class PreparedFunctionImage:
    image: Image
    sources: list[service_pb.FunctionSourceMount]
    manifests: dict[str, service_pb.FunctionSourceManifest]
    files: dict[str, tuple[str, int]]


def prepare_function_image(image: Image) -> PreparedFunctionImage:
    from chalkcompute import _native

    groups = image.lazy_local_files_grouped_for_volume_mounts()
    paths = sorted({file.src for files in groups.values() for file in files})
    metadata = dict(zip(paths, _native.source_file_digests(paths), strict=True))
    manifests: dict[str, service_pb.FunctionSourceManifest] = {}
    sources: list[service_pb.FunctionSourceMount] = []
    local_files: dict[str, tuple[str, int]] = {}
    for mount_path, files in sorted(groups.items()):
        entries: dict[str, service_pb.FunctionSourceFile] = {}
        for file in files:
            digest, size, mode = metadata[file.src]
            mode = mode if file.mode is None else file.mode
            path = os.path.relpath(file.dest, mount_path).replace(os.sep, "/")
            entry = service_pb.FunctionSourceFile(
                path=path, content_digest=digest, size=size, mode=mode
            )
            previous = entries.get(path)
            if previous is not None and previous.to_binary() != entry.to_binary():
                raise ValueError(f"Conflicting source files at {file.dest!r}")
            entries[path] = entry
            local_files[digest] = (file.src, size)
        ordered = [entries[path] for path in sorted(entries)]
        digest = _native.source_tree_digest(
            [(f.path, f.content_digest, f.size, f.mode) for f in ordered]
        )
        manifests[digest] = service_pb.FunctionSourceManifest(
            tree_digest=digest, files=ordered
        )
        sources.append(
            service_pb.FunctionSourceMount(tree_digest=digest, mount_path=mount_path)
        )
    return PreparedFunctionImage(image, sources, manifests, local_files)


def _content_identity(content: volume_pb.ContentRef) -> tuple[str, int]:
    match content.content:
        case Oneof(field="inline", value=value):
            return value.hash, value.size
        case Oneof(field="packed", value=value):
            return value.hash, value.size
        case Oneof(field="chunked", value=value):
            return value.hash, value.size
        case Oneof(field="empty"):
            return "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262", 0
    raise ValueError("Upload did not return file content")


def _supply_files(
    client: ConnectClient,
    prepared: PreparedFunctionImage,
    missing: service_pb.MissingFunctionDeploymentInputs,
) -> service_pb.FunctionDeploymentInputs:
    if missing.upload_volume is None:
        raise ConnectError("Ensure response omitted the source upload volume")
    source = [
        (digest, Path(prepared.files[digest][0])) for digest in missing.file_digests
    ]
    volume_client = VolumeClient.from_connect(client)
    staged = volume_client._inner.stage(
        missing.upload_volume.name, source, _default_upload_config()
    )
    raw = volume_client._inner.staged_intent(
        missing.upload_volume.name, staged, None, []
    )
    intent = volume_pb.CommitIntent.from_binary(bytes(raw))
    if intent.deltas is None or intent.deltas.field != "path_deltas":
        raise ConnectError("Source upload receipt exceeded the inline batch limit")
    deltas = intent.deltas.value
    for file in deltas.upserts:
        if (
            file.node is None
            or file.node.node is None
            or file.node.node.field != "file"
            or file.node.node.value.content is None
        ):
            raise ConnectError("Source upload returned a non-file node")
        digest, size = _content_identity(file.node.node.value.content)
        if digest != file.path or size != prepared.files[file.path][1]:
            raise ConnectError("Source changed while uploading; retry deployment")
    return service_pb.FunctionDeploymentInputs(
        files=deltas.upserts,
        uploaded_objects=intent.uploaded_object_references,
        upload_volume=missing.upload_volume,
    )


def ensure_external_function(
    client: ConnectClient,
    *,
    function_name: str,
    prepared: PreparedFunctionImage,
    version_spec: service_pb.ExternalFunctionVersionSpec,
    local_secrets: dict[str, str],
    force_new_version: bool,
    poll_interval: float,
    timeout: float,
) -> service_pb.EnsuredExternalFunction:
    stub = client.stub(service_connect.ExternalFunctionCatalogServiceClientSync)
    # Small manifests save a cold-path exchange. Large ones stay off the hot path.
    manifests = list(prepared.manifests.values())
    if sum(len(m.to_binary()) for m in manifests) > 64 * 1024:
        manifests = []
    request = service_pb.EnsureExternalFunctionRequest(
        request_id=str(uuid.uuid4()),
        function_name=function_name,
        spec=service_pb.DesiredFunctionSpec(
            version_spec=version_spec,
            image=prepared.image.to_proto(),
            sources=prepared.sources,
            local_secrets=local_secrets,
        ),
        force_new_version=force_new_version,
        supplied_inputs=service_pb.FunctionDeploymentInputs(manifests=manifests),
    )
    deadline = time.monotonic() + timeout
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
        request.supplied_inputs = service_pb.FunctionDeploymentInputs()
        if response.result.field == "missing_inputs":
            missing = response.result.value
            if missing.tree_digests:
                request.supplied_inputs = service_pb.FunctionDeploymentInputs(
                    manifests=[prepared.manifests[d] for d in missing.tree_digests]
                )
            elif missing.file_digests:
                request.supplied_inputs = _supply_files(client, prepared, missing)
            else:
                raise ConnectError("Ensure response requested no inputs")
        elif response.result.field == "pending_image_build":
            try:
                wait_for_image_build(
                    client,
                    response.result.value.build_id,
                    poll_interval=poll_interval,
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except ConnectRpcError as exc:
                if exc.code != Code.NOT_FOUND:
                    raise
                # Workflow retention is independent of image retention. Ensure
                # can resolve the finished image or submit a replacement build.
        else:
            raise ConnectError("Ensure response omitted its result")
    raise ConnectError(f"Function deployment timed out after {timeout}s")
