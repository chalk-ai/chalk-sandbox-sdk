"""Durable control-plane operations for immutable full-checkpoint promotion."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
import time
from typing import TYPE_CHECKING

from chalkcompute._gen.chalk.weightstream.v1 import service_connect, service_pb as pb
from chalkcompute._weights_format import WeightStreamError

if TYPE_CHECKING:
    from chalkcompute._connect import ConnectClient
    from chalkcompute._weights import WeightStream


State = pb.WeightStreamPromotionState
_TERMINAL = {State.SUCCEEDED, State.FAILED, State.CANCELLED}
_PENDING = {State.QUEUED, State.MATERIALIZING, State.REGISTERING, State.CANCELLING}


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _version(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 2**64 - 1:
        raise ValueError("version must be a uint64 integer (including zero)")
    return value


@dataclass(frozen=True)
class WeightPromotionResult:
    model_name: str
    model_version_id: str
    model_version: int
    model_volume_name: str
    model_volume_version_id: int


class WeightPromotionFailed(WeightStreamError):
    """Terminal promotion failure; the operation retains cleanup ownership state."""

    def __init__(self, operation: WeightStreamPromotion) -> None:
        self.operation = operation
        outcome = operation.snapshot.outcome
        assert outcome is not None and outcome.field == "error"
        self.code = outcome.value.code
        super().__init__(
            outcome.value.message or f"Promotion failed with code {self.code}"
        )


class WeightPromotionCancelled(WeightStreamError):
    def __init__(self, operation: WeightStreamPromotion) -> None:
        self.operation = operation
        super().__init__(f"Weight promotion {operation.id} was cancelled")


class WeightStreamPromotion:
    """An existing durable operation, independent of client or wait lifetimes.

    Start requires an explicit retry key. If its response is lost, call start
    again with that same key and identical inputs to recover the original job.
    Timeout, transport errors, and process exit never cancel or restart work.
    """

    def __init__(
        self, client: ConnectClient, promotion: pb.WeightStreamPromotion
    ) -> None:
        self._client = client
        self._promotion: pb.WeightStreamPromotion | None = None
        self._accept(promotion)

    @classmethod
    def start(
        cls,
        stream: WeightStream,
        version: int,
        model_name: str,
        *,
        idempotency_key: str,
        aliases: Sequence[str] = (),
    ) -> WeightStreamPromotion:
        version = _version(version)
        _text(model_name, "model_name")
        _text(idempotency_key, "idempotency_key")
        if isinstance(aliases, (str, bytes)):
            raise ValueError("aliases must be a sequence of strings")
        aliases = list(aliases)
        for alias in aliases:
            _text(alias, "alias")
        if len(set(aliases)) != len(aliases):
            raise ValueError("aliases must not contain duplicates")
        metadata = stream._metadata
        if metadata is None:
            raise WeightStreamError("Register the weight stream before promotion")
        response = cls._call(
            metadata.client,
            "start_weight_stream_promotion",
            pb.StartWeightStreamPromotionRequest(
                weight_stream_id=metadata.id,
                version=version,
                model_name=model_name,
                aliases=aliases,
                idempotency_key=idempotency_key,
            ),
        )
        operation = cls(metadata.client, response.promotion)
        source = operation.snapshot
        assert source.source_version is not None
        if (
            source.weight_stream_id != metadata.id
            or source.source_version.version != version
            or source.model_name != model_name
            or set(source.aliases) != set(aliases)
        ):
            raise WeightStreamError(
                "Promotion response does not match requested inputs"
            )
        return operation

    @classmethod
    def get(
        cls, promotion_id: str, *, client: ConnectClient | None = None
    ) -> WeightStreamPromotion:
        _text(promotion_id, "promotion_id")
        if client is None:
            from chalkcompute._connect import ConnectClient

            client = ConnectClient()
        response = cls._call(
            client,
            "get_weight_stream_promotion",
            pb.GetWeightStreamPromotionRequest(promotion_id=promotion_id),
        )
        operation = cls(client, response.promotion)
        if operation.id != promotion_id:
            raise WeightStreamError(
                "Promotion response returned a different operation ID"
            )
        return operation

    @staticmethod
    def _call(client, name, request, *, timeout=30.0):
        stub = client.stub(service_connect.WeightStreamServiceClientSync)
        return client.call(getattr(stub, name), request, timeout=timeout)

    @property
    def snapshot(self) -> pb.WeightStreamPromotion:
        assert self._promotion is not None
        return pb.WeightStreamPromotion.from_binary(self._promotion.to_binary())

    @property
    def id(self) -> str:
        assert self._promotion is not None
        return self._promotion.id

    @property
    def state(self) -> str:
        assert self._promotion is not None
        return self._promotion.state.name

    @property
    def done(self) -> bool:
        assert self._promotion is not None
        return self._promotion.state in _TERMINAL

    @property
    def cleanup_pending(self) -> bool:
        assert self._promotion is not None
        return self._promotion.cleanup_pending

    @property
    def result(self) -> WeightPromotionResult | None:
        assert self._promotion is not None
        outcome = self._promotion.outcome
        if self._promotion.state != State.SUCCEEDED or outcome is None:
            return None
        assert outcome.field == "result"
        item = outcome.value
        return WeightPromotionResult(
            item.model_name,
            item.model_version_id,
            item.model_version,
            item.model_volume_name,
            item.model_volume_version_id,
        )

    def refresh(self, *, timeout: float = 30.0) -> WeightStreamPromotion:
        response = self._call(
            self._client,
            "get_weight_stream_promotion",
            pb.GetWeightStreamPromotionRequest(promotion_id=self.id),
            timeout=timeout,
        )
        self._accept(response.promotion)
        return self

    def cancel(self) -> WeightStreamPromotion:
        """Request cancellation; a winning registry commit returns SUCCEEDED.

        Terminal cleanup_pending may remain true until unregistered output cleanup
        finishes. Refresh this same operation to observe it; never delete a model
        or output based solely on requesting cancellation.
        """
        response = self._call(
            self._client,
            "cancel_weight_stream_promotion",
            pb.CancelWeightStreamPromotionRequest(promotion_id=self.id),
        )
        self._accept(response.promotion)
        return self

    def wait(
        self, *, timeout: float = 600.0, poll_interval: float = 1.0
    ) -> WeightPromotionResult:
        if (
            not math.isfinite(timeout)
            or timeout < 0
            or not math.isfinite(poll_interval)
            or poll_interval <= 0
        ):
            raise ValueError(
                "timeout must be finite/nonnegative and poll_interval finite/positive"
            )
        deadline = time.monotonic() + timeout
        while not self.done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Promotion {self.id} is still {self.state}; resume with the existing operation ID"
                )
            self.refresh(timeout=min(30.0, remaining))
            if not self.done:
                time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        assert self._promotion is not None
        if self._promotion.state == State.FAILED:
            raise WeightPromotionFailed(self)
        if self._promotion.state == State.CANCELLED:
            raise WeightPromotionCancelled(self)
        result = self.result
        assert result is not None
        return result

    def _accept(self, item: pb.WeightStreamPromotion | None) -> None:
        if (
            item is None
            or not item.id
            or not item.weight_stream_id
            or not item.source_volume_name
            or not item.model_name
            or item.source_version is None
        ):
            raise WeightStreamError(
                "Promotion response is missing durable operation/source identity"
            )
        if item.source_version.kind not in {
            pb.WeightStreamVersionKind.KEYFRAME,
            pb.WeightStreamVersionKind.PATCH,
        }:
            raise WeightStreamError(
                "Promotion source has an unspecified/unknown version kind"
            )
        if len(set(item.aliases)) != len(item.aliases) or any(
            not alias.strip() for alias in item.aliases
        ):
            raise WeightStreamError(
                "Promotion aliases must be a canonical requested set"
            )
        if item.source_version.weight_stream_id != item.weight_stream_id:
            raise WeightStreamError("Promotion source belongs to a different stream")
        if item.state not in _PENDING | _TERMINAL:
            raise WeightStreamError("Promotion response has an unknown state")
        outcome = item.outcome
        if item.state == State.SUCCEEDED:
            if outcome is None or outcome.field != "result":
                raise WeightStreamError(
                    "Successful promotion is missing its registry result"
                )
            result = outcome.value
            if (
                not result.model_version_id
                or result.model_version < 1
                or not result.model_volume_name
                or result.model_name != item.model_name
            ):
                raise WeightStreamError(
                    "Promotion result is missing registry/artifact identity"
                )
        elif item.state == State.FAILED:
            if outcome is None or outcome.field != "error" or outcome.value.code == 0:
                raise WeightStreamError("Failed promotion is missing its non-OK error")
        elif outcome is not None:
            raise WeightStreamError(
                "Pending/cancelled promotion must not contain an outcome"
            )
        if item.cleanup_pending and item.state not in {State.FAILED, State.CANCELLED}:
            raise WeightStreamError(
                "Only failed/cancelled promotions may require cleanup"
            )
        if (item.completed_at is not None) != (item.state in _TERMINAL):
            raise WeightStreamError(
                "Promotion completion timestamp does not match its state"
            )
        old = self._promotion
        if old is not None:

            def identity(p):
                return (
                    p.id,
                    p.weight_stream_id,
                    p.source_volume_name,
                    p.source_version.to_binary(),
                    p.model_name,
                    tuple(sorted(p.aliases)),
                )

            if identity(old) != identity(item):
                raise WeightStreamError("Promotion immutable identity changed")
            if old.state in _TERMINAL and (
                old.state != item.state
                or old.outcome != item.outcome
                or old.completed_at != item.completed_at
                or (not old.cleanup_pending and item.cleanup_pending)
            ):
                raise WeightStreamError("Terminal promotion outcome changed")
        self._promotion = pb.WeightStreamPromotion.from_binary(item.to_binary())
