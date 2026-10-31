"""Classifier definitions, separate from their serving destinations."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, get_type_hints
from protobuf import Oneof

from chalkcompute._gen.chalk.router.v1 import model_route_pb as pb
from chalkcompute._router import (
    RouterJudge,
    Target,
    _Mode,
    _Classification,
    _validate_judge_timeout,
)
from chalkcompute._routing_request import RoutingRequest


if TYPE_CHECKING:
    from chalkcompute._function import DeployedFunction


class Classifier:
    """Choose a route label using a heuristic, model, or deployed function."""

    def __init__(
        self,
        backend: pb.RouteJudgeBackend,
        *,
        mode: _Mode = "live",
        classification: _Classification = "task",
        function: Any = None,
    ) -> None:
        if mode not in ("off", "shadow", "live"):
            raise ValueError("mode must be off, shadow, or live")
        if classification not in ("task", "difficulty"):
            raise ValueError("classification must be task or difficulty")
        self._backend = pb.RouteJudgeBackend.from_binary(backend.to_binary())
        self._mode = mode
        self._classification = classification
        self._function = function

    @classmethod
    def function(
        cls,
        function: DeployedFunction,
        *,
        mode: _Mode = "live",
        timeout_ms: int | None = None,
    ) -> Classifier:
        """Use ``@cc.function def classify(request: RoutingRequest) -> str``.

        Deployment waits for the function before publishing the router. Subsequent
        deployments of that function take effect without redeploying the router.
        """
        from chalkcompute._function import RemoteFunction

        if not isinstance(function, RemoteFunction) or not function._has_local_func:
            raise TypeError(
                "provide a locally defined @chalkcompute.function classifier"
            )
        signature = inspect.signature(function._func)
        params = list(signature.parameters.values())
        annotations = get_type_hints(function._func)
        if (
            len(params) != 1
            or params[0].kind
            not in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            or annotations.get(params[0].name) is not RoutingRequest
            or annotations.get("return") is not str
            or function._is_generator
        ):
            raise TypeError("classifier must accept one RoutingRequest and return str")
        _validate_judge_timeout(timeout_ms)
        return cls(
            pb.RouteJudgeBackend(
                kind=Oneof(
                    "function",
                    pb.FunctionClassifierBackend(
                        input_name=params[0].name, timeout_ms=timeout_ms or 0
                    ),
                )
            ),
            mode=mode,
            function=function,
        )

    @classmethod
    def heuristic(
        cls,
        *,
        mode: _Mode = "live",
        use_default_keywords: bool | None = None,
        added_hard_keywords: tuple[str, ...] = (),
        removed_hard_keywords: tuple[str, ...] = (),
        code_blocks_are_hard: bool | None = None,
        max_easy_input_bytes: int | None = None,
    ) -> Classifier:
        # Reuse the single validator for heuristic configuration.
        target = Target("validation", "model")
        judge = RouterJudge.heuristic(
            easy=target,
            hard=target,
            mode=mode,
            use_default_keywords=use_default_keywords,
            added_hard_keywords=added_hard_keywords,
            removed_hard_keywords=removed_hard_keywords,
            code_blocks_are_hard=code_blocks_are_hard,
            max_easy_input_bytes=max_easy_input_bytes,
        )
        assert judge._proto.backend is not None
        return cls(judge._proto.backend, mode=mode, classification="difficulty")

    @classmethod
    def completion(
        cls,
        target: Target,
        *,
        classification: _Classification = "task",
        mode: _Mode = "live",
        timeout_ms: int | None = None,
    ) -> Classifier:
        _validate_judge_timeout(timeout_ms)
        return cls(
            pb.RouteJudgeBackend(
                kind=Oneof(
                    "completion",
                    pb.CompletionJudgeBackend(
                        target=target._to_proto(), timeout_ms=timeout_ms or 0
                    ),
                )
            ),
            mode=mode,
            classification=classification,
        )

    @classmethod
    def decisions(
        cls,
        target: Target,
        *,
        classification: _Classification = "task",
        mode: _Mode = "live",
        timeout_ms: int | None = None,
    ) -> Classifier:
        _validate_judge_timeout(timeout_ms)
        return cls(
            pb.RouteJudgeBackend(
                kind=Oneof(
                    "decisions",
                    pb.DecisionsJudgeBackend(
                        target=target._to_proto(), timeout_ms=timeout_ms or 0
                    ),
                )
            ),
            mode=mode,
            classification=classification,
        )
