from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from chalkcompute import (
    OIDCToken,
    WorkloadIdentityTokenError,
    WorkloadIdentityTokenSource,
)
from chalkcompute._gen.chalk.server.v1.auth_pb import GetWorkloadIdentityTokenResponse


def _response(
    token: str,
    *,
    expires_at: datetime | None = None,
    issuer: str = "https://chalk.example",
    subject: str = "environment:prod",
) -> GetWorkloadIdentityTokenResponse:
    timestamp = None
    if expires_at is not None:
        timestamp = Timestamp()
        timestamp.FromDatetime(expires_at)
    return GetWorkloadIdentityTokenResponse(
        token=token,
        expires_at=timestamp,
        issuer=issuer,
        subject=subject,
    )


class _FakeConnectClient:
    def __init__(self, responses: list[GetWorkloadIdentityTokenResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, float]] = []

    def _get_workload_identity_token_response(
        self,
        audience: str,
        *,
        timeout: float = 30,
    ) -> GetWorkloadIdentityTokenResponse:
        self.requests.append((audience, timeout))
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_returns_token_and_server_metadata() -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    connect = _FakeConnectClient([_response("jwt", expires_at=expires_at)])
    source = WorkloadIdentityTokenSource("api.example.com", connect_client=connect)  # type: ignore[arg-type]

    token = source.get_token(timeout=12)

    assert token == OIDCToken(
        value="jwt",
        expires_at=expires_at,
        issuer="https://chalk.example",
        subject="environment:prod",
    )
    assert connect.requests == [("api.example.com", 12)]


def test_reuses_token_with_enough_remaining_lifetime() -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    connect = _FakeConnectClient([_response("jwt", expires_at=expires_at)])
    source = WorkloadIdentityTokenSource("api.example.com", connect_client=connect)  # type: ignore[arg-type]

    first = source.get_token(min_ttl=timedelta(seconds=60))
    second = source.get_token(min_ttl=timedelta(seconds=60))

    assert first is second
    assert len(connect.requests) == 1


def test_refreshes_token_when_cached_lifetime_is_too_short() -> None:
    connect = _FakeConnectClient(
        [
            _response("first", expires_at=datetime.now(UTC) + timedelta(minutes=2)),
            _response("second", expires_at=datetime.now(UTC) + timedelta(minutes=10)),
        ]
    )
    source = WorkloadIdentityTokenSource("api.example.com", connect_client=connect)  # type: ignore[arg-type]

    source.get_token()
    refreshed = source.get_token(min_ttl=timedelta(minutes=5))

    assert refreshed.value == "second"
    assert len(connect.requests) == 2


def test_uncached_token_without_expiry_is_returned_when_min_ttl_is_zero() -> None:
    connect = _FakeConnectClient(
        [_response("jwt", expires_at=None), _response("jwt-2", expires_at=None)]
    )
    source = WorkloadIdentityTokenSource("api.example.com", connect_client=connect)  # type: ignore[arg-type]

    first = source.get_token()
    second = source.get_token()

    assert first.value == "jwt"
    assert second.value == "jwt-2"
    assert len(connect.requests) == 2


def test_rejects_token_without_expiry_when_min_ttl_requested() -> None:
    connect = _FakeConnectClient([_response("jwt", expires_at=None)])
    source = WorkloadIdentityTokenSource("api.example.com", connect_client=connect)  # type: ignore[arg-type]

    with pytest.raises(WorkloadIdentityTokenError, match="cannot guarantee min_ttl"):
        source.get_token(min_ttl=timedelta(seconds=1))


def test_rejects_token_that_does_not_meet_min_ttl() -> None:
    connect = _FakeConnectClient(
        [_response("jwt", expires_at=datetime.now(UTC) + timedelta(seconds=2))]
    )
    source = WorkloadIdentityTokenSource("api.example.com", connect_client=connect)  # type: ignore[arg-type]

    with pytest.raises(WorkloadIdentityTokenError, match="does not meet min_ttl"):
        source.get_token(min_ttl=timedelta(seconds=60))


@pytest.mark.parametrize("audience", ["", "   "])
def test_rejects_empty_audience(audience: str) -> None:
    with pytest.raises(ValueError, match="audience cannot be empty"):
        WorkloadIdentityTokenSource(audience)


def test_rejects_invalid_min_ttl() -> None:
    source = WorkloadIdentityTokenSource("api.example.com")

    with pytest.raises(ValueError, match="min_ttl cannot be negative"):
        source.get_token(min_ttl=timedelta(seconds=-1))


def test_close_does_not_close_an_injected_client() -> None:
    connect = _FakeConnectClient([])
    source = WorkloadIdentityTokenSource("api.example.com", connect_client=connect)  # type: ignore[arg-type]

    source.close()

    assert not hasattr(connect, "closed")


def test_close_closes_an_internally_created_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = _FakeConnectClient([_response("jwt")])
    monkeypatch.setattr(
        "chalkcompute._workload_identity.ConnectClient", lambda: connect
    )
    source = WorkloadIdentityTokenSource("api.example.com")

    source.get_token()
    source.close()

    assert connect.closed
