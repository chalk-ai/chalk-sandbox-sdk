"""Fenced model target descriptors for managed serving replicas."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from chalkcompute._weights_format import WeightManifest, WeightStreamError, version_dir
from chalkcompute.weights.layout import WeightSchema


class ManagedTargetError(ValueError):
    """The delivered managed target is incomplete, stale, or inconsistent."""


class ModelTargetNotSelected(ManagedTargetError):
    """An authenticated revision has not received its initial desired target."""


_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "revision_id",
        "target_generation",
        "stream_id",
        "logical_version",
        "volume_id",
        "volume_version_id",
        "manifest_sha256",
        "weight_schema_sha256",
        "lease_expires_at",
    }
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)
_UINT64_MAX = 2**64 - 1
_INT64_MAX = 2**63 - 1
_MAX_LEASE_SECONDS = 300


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ManagedTargetError(f"{name} must be a non-empty canonical string")
    return value


def _uint64(value: Any, name: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= _UINT64_MAX:
        bound = "positive" if positive else "nonnegative"
        raise ManagedTargetError(f"{name} must be a {bound} uint64 integer")
    return value


def _utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not _RFC3339_UTC.fullmatch(value):
        raise ManagedTargetError("lease_expires_at must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManagedTargetError(
            "lease_expires_at must be an RFC3339 UTC timestamp"
        ) from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ManagedTargetError("lease_expires_at must be in UTC")
    return parsed.astimezone(timezone.utc)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManagedTargetError(f"Duplicate target descriptor key: {key}")
        result[key] = value
    return result


_FINGERPRINT_FIELDS = (
    "deployment_id",
    "revision_id",
    "target_generation",
    "stream_id",
    "logical_version",
    "volume_id",
    "volume_version_id",
    "manifest_sha256",
    "weight_schema_sha256",
)


def target_fingerprint(target: Mapping[str, str | int]) -> str:
    """Canonical cross-language admission constraint, independent of lease renewal."""
    digest = hashlib.sha256(b"chalk.model-weight-target.v1\x00")
    for field in _FINGERPRINT_FIELDS:
        encoded = str(target[field]).encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class ManagedWeightTarget:
    schema_version: int
    deployment_id: str
    revision_id: str
    target_generation: int
    stream_id: str
    logical_version: int
    volume_id: str
    volume_version_id: int
    manifest_sha256: str
    weight_schema_sha256: str
    lease_expires_at: datetime

    @classmethod
    def from_json(cls, raw: bytes | str) -> ManagedWeightTarget:
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ManagedTargetError(f"Invalid target descriptor JSON: {exc}") from exc
        if not isinstance(value, dict) or set(value) != _FIELDS:
            missing = sorted(_FIELDS - set(value)) if isinstance(value, dict) else []
            extra = sorted(set(value) - _FIELDS) if isinstance(value, dict) else []
            raise ManagedTargetError(
                f"Target descriptor fields differ from schema (missing={missing}, extra={extra})"
            )
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ManagedTargetError("Unsupported target descriptor schema_version")
        generation = _uint64(
            value["target_generation"], "target_generation", positive=True
        )
        if generation > _INT64_MAX:
            raise ManagedTargetError(
                "target_generation exceeds the signed 64-bit limit"
            )
        manifest_sha256 = value["manifest_sha256"]
        schema_sha256 = value["weight_schema_sha256"]
        if not isinstance(manifest_sha256, str) or not _DIGEST.fullmatch(
            manifest_sha256
        ):
            raise ManagedTargetError(
                "manifest_sha256 must be a lowercase SHA-256 digest"
            )
        if not isinstance(schema_sha256, str) or not _DIGEST.fullmatch(schema_sha256):
            raise ManagedTargetError(
                "weight_schema_sha256 must be a lowercase SHA-256 digest"
            )
        return cls(
            schema_version=1,
            deployment_id=_nonempty_string(value["deployment_id"], "deployment_id"),
            revision_id=_nonempty_string(value["revision_id"], "revision_id"),
            target_generation=generation,
            stream_id=_nonempty_string(value["stream_id"], "stream_id"),
            logical_version=_uint64(value["logical_version"], "logical_version"),
            volume_id=_nonempty_string(value["volume_id"], "volume_id"),
            volume_version_id=_uint64(
                value["volume_version_id"], "volume_version_id", positive=True
            ),
            manifest_sha256=manifest_sha256,
            weight_schema_sha256=schema_sha256,
            lease_expires_at=_utc_timestamp(value["lease_expires_at"]),
        )

    def fingerprint(self) -> str:
        """Cross-language admission constraint; not an authentication credential."""
        return target_fingerprint(self.observation())

    def observation(self) -> dict[str, str | int]:
        """The exact fenced target identity reported after successful apply."""
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "revision_id": self.revision_id,
            "target_generation": self.target_generation,
            "stream_id": self.stream_id,
            "logical_version": self.logical_version,
            "volume_id": self.volume_id,
            "volume_version_id": self.volume_version_id,
            "manifest_sha256": self.manifest_sha256,
            "weight_schema_sha256": self.weight_schema_sha256,
        }

    def _immutable_tuple(self) -> tuple[object, ...]:
        return (
            self.deployment_id,
            self.revision_id,
            self.target_generation,
            self.stream_id,
            self.logical_version,
            self.volume_id,
            self.volume_version_id,
            self.manifest_sha256,
            self.weight_schema_sha256,
        )

    def _revision_identity(self) -> tuple[str, str, str, str]:
        return (
            self.deployment_id,
            self.revision_id,
            self.stream_id,
            self.volume_id,
        )


class TargetProvider(Protocol):
    def current(self) -> ManagedWeightTarget: ...

    def assert_current(self, expected: ManagedWeightTarget) -> ManagedWeightTarget: ...

    def manifest_digests(
        self, expected: ManagedWeightTarget, versions: tuple[int, ...]
    ) -> Mapping[int, str]: ...


class TargetFence:
    """Reject identity changes, stale generations and mutated publications."""

    def __init__(self, expected: Mapping[str, str | None]) -> None:
        self.expected = expected
        self._lock = threading.Lock()
        self._accepted: ManagedWeightTarget | None = None

    def accept(self, target: ManagedWeightTarget) -> ManagedWeightTarget:
        now = datetime.now(timezone.utc)
        if target.lease_expires_at <= now:
            raise ManagedTargetError("Managed target lease has expired")
        if (target.lease_expires_at - now).total_seconds() > _MAX_LEASE_SECONDS:
            raise ManagedTargetError(
                f"Managed target lease exceeds {_MAX_LEASE_SECONDS} seconds"
            )
        for field, expected in self.expected.items():
            actual = getattr(target, field)
            if expected is not None and actual != expected:
                raise ManagedTargetError(
                    f"Target {field} does not match this serving replica"
                )
        with self._lock:
            previous = self._accepted
            if previous is not None:
                if target.target_generation < previous.target_generation:
                    raise ManagedTargetError("Target generation moved backwards")
                if target.lease_expires_at < previous.lease_expires_at:
                    raise ManagedTargetError("Target lease expiry moved backwards")
                if target._revision_identity() != previous._revision_identity():
                    raise ManagedTargetError(
                        "Deployment, revision, stream, or canonical volume identity changed"
                    )
                if target.target_generation == previous.target_generation:
                    if target._immutable_tuple() != previous._immutable_tuple():
                        raise ManagedTargetError(
                            "Target contents drifted without a generation change"
                        )
            self._accepted = target
        return target


class ManagedTargetProvider:
    """Read a local/test descriptor written by atomic file replacement."""

    def __init__(
        self,
        path: Path,
        *,
        expected_deployment_id: str | None = None,
        expected_revision_id: str | None = None,
        expected_stream_id: str | None = None,
        expected_volume_id: str | None = None,
        manifest_digests: Callable[
            [ManagedWeightTarget, tuple[int, ...]], Mapping[int, str]
        ]
        | None = None,
    ) -> None:
        self.path = path
        self._manifest_digests = manifest_digests
        self.expected = {
            "deployment_id": expected_deployment_id,
            "revision_id": expected_revision_id,
            "stream_id": expected_stream_id,
            "volume_id": expected_volume_id,
        }
        self._fence = TargetFence(self.expected)

    def current(self) -> ManagedWeightTarget:
        try:
            target = ManagedWeightTarget.from_json(self.path.read_bytes())
        except OSError as exc:
            raise ManagedTargetError(
                f"Cannot read target descriptor {self.path}: {exc}"
            ) from exc
        return self._fence.accept(target)

    def assert_current(self, expected: ManagedWeightTarget) -> ManagedWeightTarget:
        current = self.current()
        if current._immutable_tuple() != expected._immutable_tuple():
            raise ManagedTargetError(
                "Target changed while weights were being prepared or applied"
            )
        return current

    def manifest_digests(
        self, expected: ManagedWeightTarget, versions: tuple[int, ...]
    ) -> Mapping[int, str]:
        self.assert_current(expected)
        if self._manifest_digests is None:
            raise ManagedTargetError(
                "Managed sync requires immutable manifest identities"
            )
        return self._manifest_digests(expected, versions)


def verify_target_manifest(
    root: Path, target: ManagedWeightTarget, provider: TargetProvider
) -> tuple[tuple[WeightManifest, ...], dict[int, str]]:
    """Authenticate every frozen replay ancestor against indexed publication identities."""
    from chalkcompute.weights.serving.resident import load_manifest, read_profile_file

    raw_by_version: dict[int, bytes] = {}
    untrusted: list[WeightManifest] = []
    cursor = target.logical_version
    for _ in range(128):
        if cursor in raw_by_version:
            raise ManagedTargetError("Target manifest has cyclic ancestry")
        try:
            raw = read_profile_file(root / version_dir(cursor), "manifest.json")
            manifest = WeightManifest.from_json(raw)
        except (OSError, ValueError, WeightStreamError) as exc:
            raise ManagedTargetError(
                "Cannot read bounded target manifest ancestry"
            ) from exc
        if manifest.version != cursor:
            raise ManagedTargetError(
                "Target manifest version differs from its directory"
            )
        raw_by_version[cursor] = raw
        untrusted.append(manifest)
        if manifest.kind == "keyframe":
            break
        parent = manifest.parent_version
        if parent is None or parent < 0 or parent >= cursor:
            raise ManagedTargetError("Target manifest has invalid keyframe ancestry")
        cursor = parent
    else:
        raise ManagedTargetError("Target manifest ancestry exceeds 128 versions")
    reader = getattr(provider, "manifest_digests", None)
    if reader is None:
        raise ManagedTargetError("Managed sync requires immutable manifest identities")
    digests = dict(reader(target, tuple(raw_by_version)))
    if digests.keys() != raw_by_version.keys() or any(
        type(version) is not int
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
        for version, digest in digests.items()
    ):
        raise ManagedTargetError(
            "Immutable manifest identity set differs from replay ancestry"
        )
    if digests[target.logical_version] != target.manifest_sha256:
        raise ManagedTargetError(
            "Target manifest identity differs from selected publication"
        )
    chain = []
    for candidate in untrusted:
        expected = digests[candidate.version]
        if hashlib.sha256(raw_by_version[candidate.version]).hexdigest() != expected:
            raise ManagedTargetError(
                "Manifest digest differs from immutable indexed publication"
            )
        # Re-reading with the same authoritative hash also rejects a mount race and
        # enforces the resident profile's duplicate-field and bounded-JSON rules.
        chain.append(
            load_manifest(
                root / version_dir(candidate.version), candidate.version, expected
            )
        )
    leaf = chain[0]
    if any(m.architecture_hash != leaf.architecture_hash for m in chain):
        raise ManagedTargetError("Target manifest ancestry changes architecture")
    schema = WeightSchema.from_manifest(chain[-1])
    schema_by_name = {tensor.name: tensor for tensor in schema.tensors}
    for ancestor in chain:
        for tensor in ancestor.tensors:
            spec = schema_by_name.get(tensor.name)
            if spec is None or spec.shape != tensor.shape or spec.dtype != tensor.dtype:
                raise ManagedTargetError(
                    "Target patch tensors differ from the keyframe weight schema"
                )
    if schema.sha256 != target.weight_schema_sha256:
        raise ManagedTargetError("Target weight schema digest does not match manifest")
    return tuple(reversed(chain)), digests
