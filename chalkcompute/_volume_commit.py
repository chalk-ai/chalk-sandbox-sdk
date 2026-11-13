"""Private staging groundwork for a future verified commit protocol.

There is deliberately no submission API. Current commit status responses cannot
prove that a reused commit ID identifies the same request contents. This local
digest detects envelope corruption; it is not a server attestation or fence.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from protobuf import Oneof

from chalkcompute._gen.chalk.volume.v2 import volume_pb as pb

_MAX_INTENT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class _PreparedVolumeCommit:
    server: str
    environment_id: str
    intent_bytes: bytes

    def __post_init__(self) -> None:
        if not self.server or not self.environment_id:
            raise ValueError("Prepared commit requires server and environment identity")
        # Own immutable bytes even when callers supply a mutable buffer.
        object.__setattr__(self, "intent_bytes", bytes(self.intent_bytes))
        if not self.intent_bytes or len(self.intent_bytes) > _MAX_INTENT_BYTES:
            raise ValueError("Prepared intent exceeds the single-request size bound")
        intent = self.intent()
        if (
            intent.volume is None
            or not intent.volume.volume_id
            or not intent.volume.name
            or not intent.commit_id
            or not intent.ref
        ):
            raise ValueError(
                "Prepared intent requires stable volume, ref and commit identity"
            )
        if not intent.has_field("base_sequence_number"):
            raise ValueError("Prepared intent requires an explicit expected sequence")
        if intent.has_field("base_version_id"):
            raise ValueError("Prepared intent must not carry an inferred base version")
        if not isinstance(intent.deltas, Oneof) or intent.deltas.field != "path_deltas":
            raise ValueError("Prepared intent must contain inline path deltas")

    def intent(self) -> pb.CommitIntent:
        """Return a fresh message so callers cannot mutate the frozen request."""
        return pb.CommitIntent.from_binary(self.intent_bytes)

    def _body(self) -> dict[str, str | int]:
        return {
            "format": 1,
            "server": self.server,
            "environment_id": self.environment_id,
            "intent": base64.b64encode(self.intent_bytes).decode("ascii"),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_encode(self._body())).hexdigest()

    def to_bytes(self) -> bytes:
        return _encode({**self._body(), "sha256": self.digest})

    @classmethod
    def from_bytes(cls, data: bytes) -> _PreparedVolumeCommit:
        if len(data) > 2 * _MAX_INTENT_BYTES:
            raise ValueError("Prepared envelope exceeds size bound")
        body = json.loads(data)
        if not isinstance(body, dict) or set(body) != {
            "format",
            "server",
            "environment_id",
            "intent",
            "sha256",
        }:
            raise ValueError("Invalid prepared envelope fields")
        if type(body["format"]) is not int or body["format"] != 1:
            raise ValueError("Unsupported prepared envelope format")
        if any(
            not isinstance(body[key], str)
            for key in ("server", "environment_id", "intent", "sha256")
        ):
            raise ValueError("Invalid prepared envelope field types")
        prepared = cls(
            server=body["server"],
            environment_id=body["environment_id"],
            intent_bytes=base64.b64decode(body["intent"], validate=True),
        )
        if prepared.digest != body["sha256"]:
            raise ValueError("Prepared envelope checksum mismatch")
        return prepared


def _encode(body: dict[str, str | int]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
