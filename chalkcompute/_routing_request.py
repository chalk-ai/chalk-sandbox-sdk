"""The lossless JSON input contract for router classification functions."""

from __future__ import annotations

import json
from protobuf import Oneof
from typing import Any

from chalkcompute._gen.chalk.arrow.v1 import arrow_pb


class RoutingRequest:
    """A request being classified, before the serving model is selected.

    ``request`` retains the surface's JSON fields, including tool calls and image
    parts. ``text`` collects textual content; it does not stringify image data,
    metadata, or tool definitions. Stored Responses conversation history is not
    available, so those requests use the router's default without classification.
    """

    __chalk_arrow_type__ = arrow_pb.ArrowType(
        arrow_type_enum=Oneof("large_utf8", arrow_pb.EmptyMessage())
    )
    __chalk_type_id__ = "chalkcompute.router.RoutingRequest"

    def __init__(self, *, model: str, surface: str, request: dict[str, Any]) -> None:
        self.model = model
        self.surface = surface
        self.request = json.loads(json.dumps(request))

    @property
    def metadata(self) -> dict[str, Any]:
        value = self.request.get("metadata")
        return value if isinstance(value, dict) else {}

    @property
    def messages(self) -> list[dict[str, Any]]:
        value = self.request.get("messages", self.request.get("input", []))
        return (
            [item for item in value if isinstance(item, dict)]
            if isinstance(value, list)
            else []
        )

    @property
    def text(self) -> str:
        def texts(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [text for item in value for text in texts(item)]
            if isinstance(value, dict):
                return [
                    text
                    for key in ("text", "content", "arguments", "output")
                    if key in value
                    for text in texts(value[key])
                ]
            return []

        return "\n".join(
            texts(self.request.get("system", self.request.get("instructions", [])))
            + texts(self.request.get("messages", self.request.get("input", [])))
        )

    def __chalk_serialize__(self) -> Any:
        import pyarrow as pa

        return pa.scalar(
            json.dumps(
                {"model": self.model, "surface": self.surface, "request": self.request}
            ),
            type=pa.large_string(),
        )

    @classmethod
    def __chalk_deserialize__(cls, value: Any) -> RoutingRequest:
        data = json.loads(value.as_py())
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("model"), str)
            or not isinstance(data.get("surface"), str)
            or not isinstance(data.get("request"), dict)
        ):
            raise ValueError("invalid RoutingRequest envelope")
        return cls(
            model=data["model"], surface=data["surface"], request=data["request"]
        )
