"""Public classifier usage checked by ``just ty`` without deploying functions."""

from typing import TYPE_CHECKING, assert_type

import chalkcompute as cc
from chalkcompute.router import Classifier, RoutingRequest, Target


if TYPE_CHECKING:

    @cc.function
    def classify(request: RoutingRequest) -> str:
        return "selected"

    target = Target("connection", "model")
    assert_type(Classifier.function(classify), Classifier)
    assert_type(
        cc.Router(
            "decorated",
            default=target,
            classifier=classify,
            routes={"selected": target},
        ),
        cc.Router,
    )

    def concrete_handle(function: cc.RemoteFunction) -> None:
        deployed: cc.DeployedFunction = function
        assert_type(Classifier.function(function), Classifier)
        assert_type(Classifier.function(deployed), Classifier)
        assert_type(
            cc.Router(
                "concrete",
                default=target,
                classifier=function,
                routes={"selected": target},
            ),
            cc.Router,
        )
