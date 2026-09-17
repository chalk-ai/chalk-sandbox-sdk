"""Portable image references with immutable content mounts."""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Any, Iterable

from chalkcompute._connect import build_image
from chalkcompute._image import Image
from chalkcompute._volume import (
    Volume,
    VolumeClient,
    VolumeMount,
    normalize_volume_refs,
    pinned_volume_mount,
)


def merge_mounts(mounts: Iterable[VolumeMount]) -> list[VolumeMount]:
    """Reject overlapping destinations and collapse identical mounts."""
    result: dict[str, VolumeMount] = {}
    for mount in mounts:
        path = posixpath.normpath(mount.mount_path)
        if not path.startswith("/"):
            raise ValueError("Volume mount paths must be absolute")
        if path in result and result[path] == mount:
            continue
        if any(
            path == old
            or path.startswith(old.rstrip("/") + "/")
            or old.startswith(path.rstrip("/") + "/")
            for old in result
        ):
            raise ValueError(f"Conflicting volume mount destination: {path}")
        result[path] = mount
    return list(result.values())


@dataclass(frozen=True)
class ResolvedImage:
    """An image URI and pinned volumes, retained independently of workers.

    Pins select immutable content versions. They do not specify runtime
    read-only enforcement. Deleting the backing volumes invalidates the spec.
    """

    image_uri: str
    volumes: tuple[VolumeMount, ...] = ()

    def __post_init__(self) -> None:
        if not self.image_uri:
            raise ValueError("image_uri must be a nonempty string")
        for mount in self.volumes:
            if mount.version_id is None or mount.ref_name is not None:
                raise ValueError("Resolved image mounts must be pinned by version_id")
        object.__setattr__(self, "volumes", tuple(merge_mounts(self.volumes)))

    def to_json(self) -> str:
        """Serialize without credentials or local filesystem references."""
        return json.dumps(
            {
                "schema_version": 1,
                "image_uri": self.image_uri,
                "volumes": [
                    {
                        "name": m.name,
                        "mount_path": m.mount_path,
                        "version_id": m.version_id,
                    }
                    for m in self.volumes
                ],
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> ResolvedImage:
        """Load a versioned spec without contacting any service."""
        try:
            data = json.loads(value)
            if data["schema_version"] != 1:
                raise ValueError("Unsupported resolved image schema version")
            mounts = [VolumeMount(**mount) for mount in data["volumes"]]
            return cls(data["image_uri"], tuple(mounts))
        except (TypeError, KeyError) as exc:
            raise ValueError("Invalid resolved image spec") from exc


def resolve_image(
    image: Image,
    *,
    volumes: Iterable[tuple[str | Volume, str] | Volume | VolumeMount] | None = None,
    name: str | None = None,
    chalk_client: Any | None = None,
    poll_interval: float = 0.5,
    timeout: float = 600.0,
) -> ResolvedImage:
    """Build the image once and pin every volume to an immutable version.

    Volume-backed local files land in a stable source_code volume named from
    ``name`` so re-resolving appends a version instead of creating a volume.
    """
    mounts = normalize_volume_refs(volumes)
    lazy = image.lazy_local_files_grouped_for_volume_mounts()
    merge_mounts([*mounts, *(VolumeMount("source", path) for path in lazy)])
    if lazy and name is None:
        raise ValueError(
            "resolve_image(name=...) is required for volume-backed local files"
        )
    with VolumeClient.from_env(chalk_client=chalk_client) as driver:
        uri = build_image(
            driver._connect,
            image,
            poll_interval=poll_interval,
            timeout=timeout,
        )
        pinned = [pinned_volume_mount(driver, mount) for mount in mounts]
        if lazy:
            assert name is not None
            pinned.extend(
                driver._resolve_lazy_files(
                    image,
                    volume_name=name,
                    poll_interval=poll_interval,
                    timeout=timeout,
                )
            )
    return ResolvedImage(uri, tuple(pinned))


def normalize_resolved_image(
    image: Image | str | None | ResolvedImage,
    volumes: Iterable[tuple[str | Volume, str] | Volume | VolumeMount] | None,
) -> tuple[Image | str | None, list[VolumeMount]]:
    """Expand resolved images before ordinary spec validation."""
    mounts = normalize_volume_refs(volumes)
    if isinstance(image, ResolvedImage):
        return Image.base(image.image_uri), merge_mounts([*image.volumes, *mounts])
    return image, mounts
