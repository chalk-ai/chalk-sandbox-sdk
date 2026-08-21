"""google-protobuf-style aliases on generated messages, required by chalkpy
(`chalk/client/model_image.py` calls `SerializeToString`/`ParseFromString`/
`HasField`/`.add_file` on our `ImageSpec`/`BuildStep`)."""

from __future__ import annotations

from typing import Any

import protobuf

from chalkcompute._gen.chalk.sandbox.v1 import service_pb as _pb


def _serialize_to_string(self: _pb.ImageSpec) -> bytes:
    return self.to_binary()


def _parse_from_string(self: _pb.ImageSpec, data: bytes) -> None:
    protobuf.merge_from_binary(self, data)


def _has_field(self: _pb.BuildStep, name: Any) -> bool:
    return self.has_field(name)


def _add_file(self: _pb.BuildStep) -> Any:
    if self.step is not None and self.step.field == "add_file":
        return self.step.value
    return _pb.AddFileStep()


setattr(_pb.ImageSpec, "SerializeToString", _serialize_to_string)
setattr(_pb.ImageSpec, "ParseFromString", _parse_from_string)
setattr(_pb.BuildStep, "HasField", _has_field)
setattr(_pb.BuildStep, "add_file", property(_add_file))
