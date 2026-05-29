"""Shared helpers for volume-mount type preservation and pre-send guarding.

A deploy can emit multiple create/update requests for the same scaling group
(create-then-update, or an object reconstructed via ``from_name`` and
redeployed). Each request site re-derives the mount ``type`` from a transient
driver, so different requests could stamp DIFFERENT types onto the same volume
name (one ``chalkfs``, one ``versioned_chalkfs``). On the backend this makes the
ChalkScalingGroup CR flip-flop and re-roll endlessly.

These helpers add two protections that all deploy paths share:

#4 Preservation
    Before sending a create/update for an object that may already exist, read
    the server's recorded mount ``type`` for each volume name and reuse it in
    the outgoing request, so an existing volume's type can never change.

#5 Pre-send guard
    Immediately before issuing any request that carries volume mounts, validate
    that (a) each volume name maps to exactly one type within the request and
    (b) the request does not change the type of a volume that already exists
    server-side. On violation, raise an actionable error and fail fast.

These functions operate purely on the JSON dicts that the deploy paths already
build (mounts shaped like ``{"name", "mountPath", "type"}``) so they slot in
without touching the volume-typing model itself.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


class _RpcClient(Protocol):
    """Minimal structural type for the Connect client used by deploy paths."""

    def rpc(
        self,
        service: str,
        method: str,
        body: dict[str, Any],
        timeout: float = ...,
    ) -> Any: ...


def fetch_scaling_group_volume_types(
    client: _RpcClient,
    service: str,
    name: str,
) -> dict[str, str]:
    """Return ``{volume_name: mount_type}`` recorded on an existing scaling group.

    Reads ``GetScalingGroup`` and extracts the mount types from
    ``scalingGroup.spec.containerSpec.volumes`` (each entry has ``name`` and
    ``type``). A not-found / first-deploy scaling group yields an empty mapping
    so callers can proceed normally — this never raises for a missing group.

    Any non-200 response (including NOT_FOUND) or a response missing the
    expected nesting is treated as "nothing to preserve". We intentionally do
    not surface read errors here: preservation is best-effort and must not break
    a first deploy. The pre-send guard still protects against same-request
    conflicts regardless of what this returns.
    """
    try:
        resp = client.rpc(service, "GetScalingGroup", {"name": name})
    except Exception:
        # A transient read failure must not block a deploy; the guard still
        # enforces same-request consistency.
        return {}
    if getattr(resp, "status_code", None) != 200:
        return {}
    try:
        data = resp.json()
    except Exception:
        return {}
    sg = data.get("scalingGroup") or {}
    container_spec = (sg.get("spec") or {}).get("containerSpec") or {}
    return _volume_types_from_mounts(container_spec.get("volumes") or [])


def fetch_container_volume_types(
    client: _RpcClient,
    service: str,
    name: str,
) -> dict[str, str]:
    """Return ``{volume_name: mount_type}`` recorded on an existing container.

    Reads ``GetContainer`` by name and extracts mount types from
    ``container.spec.volumes``. As with scaling groups, a not-found container or
    any unexpected shape yields an empty mapping; this is best-effort and never
    raises for a missing container.
    """
    try:
        resp = client.rpc(service, "GetContainer", {"name": name})
    except Exception:
        return {}
    if getattr(resp, "status_code", None) != 200:
        return {}
    try:
        data = resp.json()
    except Exception:
        return {}
    container = data.get("container") or {}
    spec = container.get("spec") or {}
    return _volume_types_from_mounts(spec.get("volumes") or [])


def _volume_types_from_mounts(mounts: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Collapse a list of mount dicts into ``{name: type}``.

    Server state is assumed self-consistent (one type per name); if a name
    repeats, the last entry wins, matching how the backend would render it.
    """
    types: dict[str, str] = {}
    for mount in mounts:
        name = mount.get("name")
        mount_type = mount.get("type")
        if isinstance(name, str) and isinstance(mount_type, str) and mount_type:
            types[name] = mount_type
    return types


def preserve_existing_volume_types(
    mounts: list[dict[str, Any]],
    existing_types: Mapping[str, str],
) -> None:
    """Rewrite outgoing mount types in place to match server-recorded types.

    For any mount whose ``name`` already exists server-side, overwrite its
    ``type`` with the server's recorded value. Mounts for new volume names are
    left untouched. This makes redeploys idempotent with respect to mount type
    and prevents a transient local driver from flipping an existing volume's
    type.
    """
    for mount in mounts:
        name = mount.get("name")
        if isinstance(name, str) and name in existing_types:
            mount["type"] = existing_types[name]


def guard_volume_mounts(
    mounts: Sequence[Mapping[str, Any]],
    *,
    existing_types: Mapping[str, str],
    error_cls: type[Exception],
    owner: str,
) -> None:
    """Validate mount-type consistency immediately before a request is sent.

    Enforces two invariants and raises ``error_cls`` (e.g. ``FunctionError``,
    ``ContainerError``, ``ClassError``) on violation:

    (a) Each volume ``name`` maps to exactly one ``type`` within this request.
        Two mounts of the same name with different types would make the backend
        CR ambiguous, so we reject the request rather than send it.
    (b) The request does not change the ``type`` of a volume that already
        exists server-side (``existing_types``). Changing an existing volume's
        type causes the ChalkScalingGroup CR to flip-flop and re-roll.

    ``owner`` is a human-readable description of the deploy target (e.g.
    ``"function 'embed'"``) included in the error message.
    """
    # (a) one type per name within the request.
    seen: dict[str, str] = {}
    for mount in mounts:
        name = mount.get("name")
        mount_type = mount.get("type")
        if not isinstance(name, str) or not isinstance(mount_type, str):
            continue
        prior = seen.get(name)
        if prior is not None and prior != mount_type:
            raise error_cls(
                f"Conflicting mount types for volume {name!r} in {owner}: "
                f"the request mounts it as both {prior!r} and {mount_type!r}. "
                f"A volume must use a single mount type within one deploy."
            )
        seen[name] = mount_type

    # (b) no change to an existing volume's type.
    for name, mount_type in seen.items():
        existing = existing_types.get(name)
        if existing is not None and existing != mount_type:
            raise error_cls(
                f"Cannot change mount type of existing volume {name!r} in "
                f"{owner}: it is recorded server-side as {existing!r} but the "
                f"deploy would set it to {mount_type!r}. A volume's mount type "
                f"is fixed once created."
            )
