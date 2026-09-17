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

    Parameters
    ----------
    image_uri : str
        Nonempty image reference.
    volumes : tuple of VolumeMount, optional
        Mounts with a version ID and no mutable ref selector.

    Raises
    ------
    ValueError
        If the image reference is empty or mounts are unpinned or conflicting.
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
        """Serialize without credentials or local filesystem references.

        Returns
        -------
        str
            JSON containing the schema version, image URI, and volume pins.
        """
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
        """Load a versioned spec without contacting any service.

        Parameters
        ----------
        value : str
            JSON produced by :meth:`to_json`.

        Returns
        -------
        ResolvedImage
            Image reference and pinned volume mounts.

        Raises
        ------
        ValueError
            If the JSON, schema version, or resolved spec is invalid.
        """
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

    Parameters
    ----------
    image : Image
        Image definition to build.
    volumes : iterable, optional
        Volume handles, mounts, or ``(name_or_volume, mount_path)`` pairs.
        Each selector is captured as an immutable version.
    name : str, optional
        Stable source volume name. Required for volume-backed local files.
        Mount directories other than ``/app`` receive a name suffix.
    chalk_client : Any, optional
        Chalk client supplying authentication. Defaults to environment auth.
    poll_interval : float, optional
        Seconds between build or source-resolution polls. Defaults to 0.5.
    timeout : float, optional
        Timeout in seconds for each build and source-resolution operation.
        Defaults to 600.0.

    Returns
    -------
    ResolvedImage
        Built image URI with referenced mounts followed by source mounts,
        all pinned to immutable versions.

    Raises
    ------
    ValueError
        If mounts conflict or local files require a missing source name.
    ConnectError
        If the image build fails or times out.
    VolumeError
        If volume lookup or source resolution fails.

    Notes
    -----
    Local files use stable ``source_code`` volumes. Resolving changed content
    with the same name publishes a new version. Retain backing volumes while
    resolved specs remain in use.
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
