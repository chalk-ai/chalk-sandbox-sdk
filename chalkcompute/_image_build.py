"""Build images and prepare their runtime volume mounts."""

from __future__ import annotations

import base64
import hashlib
import posixpath
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
    """Normalize volume mount paths and reject conflicting destinations.

    Parameters
    ----------
    mounts
        Volume mounts to normalize and merge.

    Returns
    -------
    list of VolumeMount
        The normalized mounts in their original order.

    Raises
    ------
    ValueError
        If a mount path is relative or conflicting mounts share a destination.
    """
    result: dict[str, VolumeMount] = {}
    for mount in mounts:
        path = posixpath.normpath(mount.mount_path)
        if not path.startswith("/"):
            raise ValueError("Volume mount paths must be absolute")
        if path in result and result[path] == mount:
            continue
        if path in result:
            raise ValueError(f"Conflicting volume mount destination: {path}")
        result[path] = mount
    return list(result.values())


def default_source_volume_name(image: Image) -> str:
    """Derive a stable source volume name from an image recipe.

    Parameters
    ----------
    image
        Image whose build specification determines the name.

    Returns
    -------
    str
        A stable source volume name.
    """
    recipe = image._to_build_proto()
    recipe.env = dict(sorted(recipe.env.items()))
    digest = hashlib.sha256(recipe.to_binary()).digest()
    fingerprint = base64.b32encode(digest).decode().rstrip("=").lower()
    return f"cc-image-{fingerprint}"


def build_image_snapshot(
    image: Image,
    *,
    volumes: Iterable[tuple[str | Volume, str] | Volume | VolumeMount] | None = None,
    name: str | None = None,
    chalk_client: Any | None = None,
    poll_interval: float = 0.5,
    timeout: float = 600.0,
) -> Image:
    """Build a reusable image and capture committed volume versions.

    Parameters
    ----------
    image
        Image recipe to build.
    volumes
        Additional volume references to pin in the built image.
    name
        Optional source volume name for local volume files.
    chalk_client
        Optional client for authentication.
    poll_interval
        Polling interval in seconds for each build and source upload.
    timeout
        Timeout in seconds for each build and source upload.

    Returns
    -------
    Image
        A built image with pinned volume versions.
    """
    recipe, mounts = normalize_image_volumes(image, volumes)
    assert isinstance(recipe, Image)
    mounts = merge_mounts(mounts)
    lazy = image.lazy_local_files_grouped_for_volume_mounts()
    name = name or default_source_volume_name(image)
    with VolumeClient.from_env(chalk_client=chalk_client) as driver:
        uri = build_image(
            driver._connect,
            recipe,
            poll_interval=poll_interval,
            timeout=timeout,
        )
        pinned = [pinned_volume_mount(driver, mount) for mount in mounts]
        if lazy:
            pinned.extend(
                driver._resolve_lazy_files(
                    image,
                    volume_name=name,
                    shared=True,
                    poll_interval=poll_interval,
                    timeout=timeout,
                )
            )
    return Image._from_built(uri, pinned)


def normalize_image_volumes(
    image: Image | str | None,
    volumes: Iterable[tuple[str | Volume, str] | Volume | VolumeMount] | None,
) -> tuple[Image | str | None, list[VolumeMount]]:
    """Separate image content mounts from the container build specification.

    Parameters
    ----------
    image
        Image or image reference supplied to a compute resource.
    volumes
        Volume references supplied alongside the image.

    Returns
    -------
    tuple
        The image or image reference and normalized volume mounts.

    Raises
    ------
    ValueError
        If volume mounts conflict with pinned or local image content.
    """
    mounts = normalize_volume_refs(volumes)
    if not isinstance(image, Image):
        return image, mounts
    if image._volume_mounts:
        mounts = merge_mounts([*image._volume_mounts, *mounts])
    if mounts:
        source_paths = {
            posixpath.normpath(path)
            for path in image.lazy_local_files_grouped_for_volume_mounts()
        }
        for mount in mounts:
            path = posixpath.normpath(mount.mount_path)
            if path in source_paths:
                raise ValueError(f"Conflicting volume mount destination: {path}")
    if image._volume_mounts:
        plain = image._copy()
        plain._volume_mounts = ()
        return plain, mounts
    return image, mounts
