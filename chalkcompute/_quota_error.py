"""Environment quota rejections from the Chalk API."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager

from connectrpc.client import ResponseMetadata
from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError, ErrorDetail


class QuotaExceededError(ConnectRpcError):
    """A request would exceed a quota enforced by the Chalk environment.

    ``resource`` identifies the quota. ``limit``, ``in_use``, and ``requested``
    are optional amounts in the resource's base units (for example, millicores).
    """

    def __init__(
        self,
        message: str,
        *,
        resource: str | None = None,
        limit: int | None = None,
        in_use: int | None = None,
        requested: int | None = None,
        details: Sequence[ErrorDetail] = (),
    ) -> None:
        super().__init__(Code.RESOURCE_EXHAUSTED, message, details)
        self.resource = resource
        self.limit = limit
        self.in_use = in_use
        self.requested = requested


def _metadata_value(metadata: ResponseMetadata, key: str) -> str | None:
    return metadata.trailers.get(key) or metadata.headers.get(key)


def _quota_amount(metadata: ResponseMetadata, key: str) -> int | None:
    value = _metadata_value(metadata, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


@contextmanager
def _translate_quota_error(
    error_type: type[QuotaExceededError],
) -> Generator[None, None, None]:
    with ResponseMetadata() as metadata:
        try:
            yield
        except ConnectRpcError as exc:
            if exc.code != Code.RESOURCE_EXHAUSTED:
                raise
            resource = _metadata_value(metadata, "chalk-quota-exceeded")
            if not resource:
                raise
            raise error_type(
                exc.message,
                resource=resource,
                limit=_quota_amount(metadata, "chalk-quota-limit"),
                in_use=_quota_amount(metadata, "chalk-quota-in-use"),
                requested=_quota_amount(metadata, "chalk-quota-requested"),
                details=exc.details,
            ) from exc
