"""Durable promotion public API checked by just ty without remote calls."""

from typing import TYPE_CHECKING, assert_type
import chalkcompute as cc

if TYPE_CHECKING:

    def promote(stream: cc.WeightStream, policy: cc.LivePolicy) -> None:
        operation = stream.promote(0, "policy", idempotency_key="run123:v0:policy")
        assert_type(operation, cc.WeightStreamPromotion)
        assert_type(operation.result, cc.WeightPromotionResult | None)
        assert_type(operation.wait(), cc.WeightPromotionResult)
        assert_type(operation.cancel(), cc.WeightStreamPromotion)
        assert_type(operation.refresh(), cc.WeightStreamPromotion)
        assert_type(
            cc.WeightStreamPromotion.get(operation.id), cc.WeightStreamPromotion
        )
        assert_type(
            policy.promote(0, "policy", idempotency_key="run123:v0:policy"),
            cc.WeightStreamPromotion,
        )
