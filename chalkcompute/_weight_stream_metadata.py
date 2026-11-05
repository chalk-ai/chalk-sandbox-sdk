"""Optional control-plane observations for authoritative volume publications."""

from __future__ import annotations

import math
import time

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from protobuf import Oneof
from typing import TYPE_CHECKING

from chalkcompute._gen.chalk.weightstream.v1 import service_connect, service_pb
from chalkcompute._weights_format import WeightStreamError, version_dir

if TYPE_CHECKING:
    from chalkcompute._connect import ConnectClient
    from chalkcompute._volume import Volume
    from chalkcompute._weights import WeightStream


class WeightMetadataPending(WeightStreamError):
    """Publication committed; retry metadata reconciliation, never reuse its version."""

    def __init__(self, version: int, volume_version_id: int) -> None:
        self.version = version
        self.volume_version_id = volume_version_id
        super().__init__(
            f"Weight version {version} committed at immutable volume snapshot "
            f"{volume_version_id}, but metadata indexing is pending. "
            "The publication succeeded. Call stream.reconcile() to retry indexing; "
            "do not reuse this logical version."
        )


class _WeightStreamMetadata:
    def __init__(self, client: ConnectClient, stream_id: str) -> None:
        self.client = client
        self.id = stream_id
        self.last_reconcile_stats: dict[str, int | float] = {}
        self._totals: dict[str, int] = {}

    def _call(self, name, request):
        stub = self.client.stub(service_connect.WeightStreamServiceClientSync)
        return self.client.call(getattr(stub, name), request)

    def pin(self, version: int | None) -> None:
        if version is None:
            self._call(
                "clear_weight_stream_pin",
                service_pb.ClearWeightStreamPinRequest(weight_stream_id=self.id),
            )
        else:
            self._call(
                "set_weight_stream_pin",
                service_pb.SetWeightStreamPinRequest(
                    weight_stream_id=self.id, version=version
                ),
            )

    def record(self, stream: WeightStream, snapshot: Volume, version: int) -> None:
        from chalkcompute._weights_format import WeightManifest

        manifest = WeightManifest.from_json(
            snapshot.read_file(f"{version_dir(version)}/manifest.json")
        )
        total = self._totals.get(manifest.architecture_hash)
        if total is None:
            base = stream._chain(snapshot, version)[0]
            total = sum(math.prod(t.shape) for t in base.tensors)
            self._totals[manifest.architecture_hash] = total
        # Dense fallback does not retain the exact changed-element count. Index
        # the transmitted fraction, a provable upper bound accepted by the API.
        fraction = sum(t.nnz for t in manifest.tensors) / total if total else 0.0
        ranges = {(t.file, t.offset, t.length, t.checksum) for t in manifest.tensors}
        descriptor = service_pb.WeightStreamVersion(
            weight_stream_id=self.id,
            version=version,
            parent_version=manifest.parent_version,
            kind=(
                service_pb.WeightStreamVersionKind.KEYFRAME
                if manifest.kind == "keyframe"
                else service_pb.WeightStreamVersionKind.PATCH
            ),
            volume_version_id=snapshot.version.id,
            architecture_hash=manifest.architecture_hash,
            trainer_step=manifest.trainer_step,
            training_run_id=manifest.training_run_id,
            payload_bytes=sum(item[2] for item in ranges),
            changed_element_fraction=fraction,
        )
        self._call(
            "record_weight_stream_version",
            service_pb.RecordWeightStreamVersionRequest(version=descriptor),
        )

    def reconcile(
        self, stream: WeightStream, *, max_snapshots: int = 1000
    ) -> list[int]:
        if max_snapshots < 1:
            raise ValueError("max_snapshots must be positive")
        started = time.monotonic()
        indexed = set()
        cursor = None
        seen_cursors: set[str] = set()
        while True:
            response = self._call(
                "list_weight_stream_versions",
                service_pb.ListWeightStreamVersionsRequest(
                    weight_stream_id=self.id, cursor=cursor, limit=200
                ),
            )
            indexed.update(item.version for item in response.versions)
            if not response.next_cursor:
                break
            cursor = response.next_cursor
            if cursor in seen_cursors:
                raise WeightStreamError("Metadata index repeated a pagination cursor")
            seen_cursors.add(cursor)
            if len(indexed) > max_snapshots:
                raise WeightStreamError(
                    "Metadata history exceeds max_snapshots; retry with a larger explicit bound"
                )
        # Historical snapshots survive live-ref pruning. The earliest snapshot
        # with CURRENT=v is v's original atomic publication, so retries retain
        # exactly the same immutable snapshot even after PIN/other commits.
        publications: dict[int, int] = {}
        history = []
        for item in stream.volume.iter_versions(limit=min(max_snapshots, 100)):
            if len(history) >= max_snapshots:
                raise WeightStreamError(
                    "Reconciliation exceeded max_snapshots; retry with a larger "
                    "explicit bound. No original publication snapshot was substituted."
                )
            history.append(item)
        ids = {item.id for item in history}
        if any(
            item.parent_id not in (None, 0) and item.parent_id not in ids
            for item in history
        ):
            raise WeightStreamError(
                "Volume history is incomplete; cannot recover original publication snapshots"
            )
        self.last_reconcile_stats = {
            "history_snapshots": len(history),
            "current_reads": 0,
            "record_calls": 0,
        }
        previous_version = None
        seen_versions = set()
        for item in sorted(history, key=lambda item: item.id):
            snapshot = stream.volume.at_version(item.id)
            version = stream._read_current(snapshot)
            self.last_reconcile_stats["current_reads"] += 1
            if version != previous_version:
                if version in seen_versions:
                    raise WeightStreamError(
                        "Ambiguous publication history: CURRENT revisited a logical version"
                    )
                if version is not None:
                    seen_versions.add(version)
                previous_version = version
            if version is not None and version not in indexed:
                publications.setdefault(version, item.id)
        recorded = []
        for version, snapshot_id in sorted(publications.items(), reverse=True):
            try:
                self.last_reconcile_stats["record_calls"] += 1
                self.record(stream, stream.volume.at_version(snapshot_id), version)
            except Exception as error:
                raise WeightMetadataPending(version, snapshot_id) from error
            recorded.append(version)
        self.last_reconcile_stats["elapsed_seconds"] = time.monotonic() - started
        return recorded


def register(stream: WeightStream, client: ConnectClient) -> _WeightStreamMetadata:
    if not stream.base_model:
        current = stream.current()
        if current is not None:
            from chalkcompute._weights_format import WeightManifest

            manifest = WeightManifest.from_json(
                stream._snapshot().read_file(f"{version_dir(current)}/manifest.json")
            )
            stream.base_model = manifest.base_model
    if not stream.base_model:
        raise WeightStreamError("Set base_model or seed the volume before registration")
    stub = client.stub(service_connect.WeightStreamServiceClientSync)
    try:
        try:
            response = client.call(
                stub.create_weight_stream,
                service_pb.CreateWeightStreamRequest(
                    name=stream.name,
                    base_model=stream.base_model,
                    volume_name=stream.volume.name,
                ),
            )
        except ConnectError as error:
            if error.code != Code.ALREADY_EXISTS:
                raise
            response = client.call(
                stub.get_weight_stream,
                service_pb.GetWeightStreamRequest(
                    weight_stream_key=Oneof("weight_stream_name", stream.name)
                ),
            )
    except Exception as error:
        raise WeightStreamError(
            "Could not register weight stream metadata. Existing volume "
            "publications remain committed; retry register() when the service "
            "is available."
        ) from error
    if response.weight_stream is None or not response.weight_stream.id:
        raise WeightStreamError("CreateWeightStream returned no stream identity")
    if (
        response.weight_stream.volume_name != stream.volume.name
        or response.weight_stream.base_model != stream.base_model
    ):
        raise WeightStreamError(
            "Registered stream name belongs to a different volume or base model"
        )
    return _WeightStreamMetadata(client, response.weight_stream.id)
