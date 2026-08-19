"""Test adapter for legacy canned JSON responses and generated RPC callers."""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass
from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError


def _camel_case(value: str) -> str:
    return "".join(part.capitalize() for part in value.split("_"))


@dataclass(frozen=True)
class _Method:
    service: str
    rpc_name: str
    response_type: type[Any]
    __name__: str


class _Stub:
    def __init__(self, stub_cls: type[Any]) -> None:
        module = importlib.import_module(stub_cls.__module__.replace("_connect", "_pb"))
        service_name = re.sub(r"ClientSync$", "", stub_cls.__name__)
        self._module = module
        self._service_name = service_name

    def __getattr__(self, name: str) -> _Method:
        rpc_name = _camel_case(name)
        response_type = getattr(self._module, f"{rpc_name}Response")
        request_type = getattr(self._module, f"{rpc_name}Request")
        package = request_type.desc().type_name.rsplit(".", 1)[0]
        return _Method(
            service=f"{package}.{self._service_name}",
            rpc_name=rpc_name,
            response_type=response_type,
            __name__=name,
        )


class TypedConnectFakeMixin:
    """Route generated calls through an existing fake's ``rpc`` method."""

    def stub(self, stub_cls: type[Any]) -> _Stub:
        return _Stub(stub_cls)

    def call(
        self,
        method: _Method,
        request: Any,
        *,
        timeout: float = 30,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        payload = json.loads(request.to_json())
        kwargs: dict[str, Any] = {}
        if timeout != 30:
            kwargs["timeout"] = timeout
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        try:
            response = self.rpc(method.service, method.rpc_name, payload, **kwargs)
        except TypeError:
            response = self.rpc(method.service, method.rpc_name, payload)
        if response.status_code != 200:
            code = {
                400: Code.INVALID_ARGUMENT,
                401: Code.UNAUTHENTICATED,
                403: Code.PERMISSION_DENIED,
                404: Code.NOT_FOUND,
                409: Code.ALREADY_EXISTS,
                429: Code.RESOURCE_EXHAUSTED,
                500: Code.INTERNAL,
                503: Code.UNAVAILABLE,
            }.get(response.status_code, Code.UNKNOWN)
            raise ConnectError(code, response.text or f"HTTP {response.status_code}")
        return method.response_type.from_json(json.dumps(response.json()))
