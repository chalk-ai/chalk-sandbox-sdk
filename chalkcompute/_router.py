"""Declarative model routes deployed through Chalk's authenticated router API."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from connectrpc.code import Code
from connectrpc.errors import ConnectError as ConnectRpcError
from protobuf import Oneof
from protobuf.wkt import FieldMask

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.router.v1 import model_route_connect, model_route_pb as pb
from chalkcompute._gen.chalk.router.v1 import (
    provider_connection_connect,
    provider_connection_pb as connection_pb,
)
from chalkcompute._tracing import _instrumented

if TYPE_CHECKING:
    from chalkcompute._classifier import Classifier
    from chalkcompute._function import DeployedFunction

_T = TypeVar("_T")
_Mode = Literal["off", "shadow", "live"]
_Classification = Literal["task", "difficulty"]


class RouterError(Exception):
    """A router administration operation failed."""


class RouterNotFoundError(RouterError):
    """No active router or provider connection matches the requested name."""


def _nonblank(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be non-empty with no surrounding whitespace")
    return value


def _model_name(name: str) -> str:
    name = _nonblank(name, "name")
    model = name if name.startswith("chalk/") else f"chalk/{name}"
    _nonblank(model.removeprefix("chalk/"), "name after chalk/")
    return model


@dataclass(frozen=True)
class Target:
    """A saved connection's immutable ID and an upstream model ID.

    Prefer ``Connection.from_name("my-openai").model("gpt-4.1-mini")`` when
    referring to a provider connection by its human-readable name. Provider
    prefixes and ``chalk/*`` aliases are not connection IDs.
    """

    connection_id: str
    model: str

    def __post_init__(self) -> None:
        _nonblank(self.connection_id, "connection_id")
        _nonblank(self.model, "model")
        if (
            "/" in self.connection_id
            or self.connection_id == "chalk"
            or self.model.startswith("chalk/")
            or len(self._reference.encode()) > 768
        ):
            raise ValueError(
                "target must identify a concrete connection and upstream model"
            )

    @property
    def _reference(self) -> str:
        return f"{self.connection_id}/{self.model}"

    def _to_proto(self) -> pb.RouteTarget:
        return pb.RouteTarget(
            connection_id=self.connection_id, upstream_model_id=self.model
        )


def _fallbacks(target: Target, alternatives: Sequence[Target]) -> pb.RouteFallbacks:
    if len(alternatives) > 16:
        raise ValueError("at most 16 fallback alternatives are allowed")
    seen = {target._reference}
    models: list[str] = []
    for alternative in alternatives:
        if alternative._reference in seen:
            raise ValueError("a fallback chain must not repeat a target")
        seen.add(alternative._reference)
        models.append(alternative._reference)
    return pb.RouteFallbacks(models=models)


@dataclass(frozen=True)
class Route:
    """A destination, optional classification criteria, and fallback override.

    ``fallbacks=None`` inherits the route's fallback entry for this target;
    ``fallbacks=[]`` explicitly disables fallbacks for this outcome.
    """

    target: Target
    description: str = ""
    fallbacks: Sequence[Target] | None = None

    def _to_proto(self) -> pb.JudgeOutcomeConfig:
        if len(self.description.encode()) > 2048:
            raise ValueError("outcome description must be at most 2048 bytes")
        return pb.JudgeOutcomeConfig(
            model=self.target._reference,
            description=self.description,
            fallbacks=None
            if self.fallbacks is None
            else _fallbacks(self.target, self.fallbacks),
        )


class RouterJudge:
    """Request classification using ``heuristic()``, ``completion()`` or ``decisions()``.

    Live mode selects an outcome's target; shadow mode records the classification
    while serving the router's default target. A failed judge uses the default.
    Model-based judges may incur additional provider charges, including in shadow mode.
    """

    def __init__(self, proto: pb.RouteJudge) -> None:
        self._proto = proto

    @classmethod
    def _build(
        cls,
        backend: pb.RouteJudgeBackend,
        classification: _Classification,
        outcomes: Mapping[str, Target | Route],
        mode: _Mode,
    ) -> RouterJudge:
        modes = {
            "off": pb.JudgeMode.OFF,
            "shadow": pb.JudgeMode.SHADOW,
            "live": pb.JudgeMode.LIVE,
        }
        classifications = {
            "task": pb.Classification.TASK,
            "difficulty": pb.Classification.DIFFICULTY,
        }
        if mode not in modes:
            raise ValueError("mode must be off, shadow, or live")
        if classification not in classifications:
            raise ValueError("classification must be task or difficulty")
        if not 1 <= len(outcomes) <= 32:
            raise ValueError("a judge needs 1–32 outcomes")
        if classification == "difficulty" and (
            len(outcomes) < 2 or set(outcomes) != {str(i) for i in range(len(outcomes))}
        ):
            raise ValueError(
                "difficulty outcomes must be consecutive strings starting at '0'"
            )
        configured: dict[str, pb.JudgeOutcomeConfig] = {}
        for label, outcome in outcomes.items():
            _nonblank(label, "outcome label")
            if len(label.encode()) > 128:
                raise ValueError("outcome label must be at most 128 bytes")
            configured[label] = (
                Route(outcome) if isinstance(outcome, Target) else outcome
            )._to_proto()
        return cls(
            pb.RouteJudge(
                mode=modes[mode],
                classification=classifications[classification],
                backend=backend,
                outcomes=configured,
            )
        )

    @classmethod
    def heuristic(
        cls,
        *,
        easy: Target | Route,
        hard: Target | Route,
        mode: _Mode = "live",
        use_default_keywords: bool | None = None,
        added_hard_keywords: Sequence[str] = (),
        removed_hard_keywords: Sequence[str] = (),
        code_blocks_are_hard: bool | None = None,
        max_easy_input_bytes: int | None = None,
    ) -> RouterJudge:
        """Classify difficulty locally in the router, without an extra LLM call.

        Omitted settings retain server defaults. A zero byte threshold disables
        the input-length signal; false disables the corresponding boolean signal.
        """
        keywords = [*added_hard_keywords, *removed_hard_keywords]
        if len(keywords) > 256 or any(
            not k.strip() or len(k.encode()) > 256 for k in keywords
        ):
            raise ValueError(
                "provide at most 256 non-blank keywords, each at most 256 bytes"
            )
        if {k.lower() for k in added_hard_keywords} & {
            k.lower() for k in removed_hard_keywords
        }:
            raise ValueError("a heuristic keyword cannot be both added and removed")
        if (
            max_easy_input_bytes is not None
            and not 0 <= max_easy_input_bytes <= 2**32 - 1
        ):
            raise ValueError("max_easy_input_bytes must fit an unsigned 32-bit integer")
        backend = pb.HeuristicJudgeBackend(
            use_default_keywords=use_default_keywords,
            added_hard_keywords=list(added_hard_keywords),
            removed_hard_keywords=list(removed_hard_keywords),
            code_blocks_are_hard=code_blocks_are_hard,
            max_easy_input_bytes=max_easy_input_bytes,
        )
        return cls._build(
            pb.RouteJudgeBackend(kind=Oneof("heuristic", backend)),
            "difficulty",
            {"0": easy, "1": hard},
            mode,
        )

    @classmethod
    def completion(
        cls,
        target: Target,
        *,
        classification: _Classification,
        outcomes: Mapping[str, Target | Route],
        mode: _Mode = "live",
        timeout_ms: int | None = None,
    ) -> RouterJudge:
        """Classify with a model supporting Chat Completions. Omitted timeout uses the server default."""
        _validate_judge_timeout(timeout_ms)
        backend = pb.CompletionJudgeBackend(
            target=target._to_proto(), timeout_ms=timeout_ms or 0
        )
        return cls._build(
            pb.RouteJudgeBackend(kind=Oneof("completion", backend)),
            classification,
            outcomes,
            mode,
        )

    @classmethod
    def decisions(
        cls,
        target: Target,
        *,
        classification: _Classification,
        outcomes: Mapping[str, Target | Route],
        mode: _Mode = "live",
        timeout_ms: int | None = None,
    ) -> RouterJudge:
        """Classify with a model supporting the Decisions API."""
        _validate_judge_timeout(timeout_ms)
        backend = pb.DecisionsJudgeBackend(
            target=target._to_proto(), timeout_ms=timeout_ms or 0
        )
        return cls._build(
            pb.RouteJudgeBackend(kind=Oneof("decisions", backend)),
            classification,
            outcomes,
            mode,
        )


def _validate_judge_timeout(timeout_ms: int | None) -> None:
    if timeout_ms is not None and not 1 <= timeout_ms <= 30000:
        raise ValueError("judge timeout_ms must be between 1 and 30000")


@dataclass(frozen=True)
class Connection:
    """A saved provider connection; credentials are never returned.

    Use ``Connection.from_name(name)`` to reuse a connection or
    ``Connection.create(...)`` to create one. Both discover Chalk
    credentials automatically, with an optional client for explicit control.
    """

    id: str
    name: str
    provider: str
    routing_enabled: bool

    @classmethod
    def from_name(cls, name: str, *, client: RouterClient | None = None) -> Connection:
        """Look up a saved connection by exact name using ambient Chalk credentials."""
        with _router_client(client) as resolved:
            return resolved.connection(name)

    @classmethod
    def create(
        cls,
        name: str,
        *,
        provider: str,
        api_key: str,
        base_url: str | None = None,
        prefix: str | None = None,
        client: RouterClient | None = None,
    ) -> Connection:
        """Create a persistent provider connection, storing its key encrypted in Chalk.

        This creates rather than updates; use ``from_name`` to reuse a saved
        connection. Upstream models are exposed only through explicit routes.
        """
        with _router_client(client) as resolved:
            return resolved.create_connection(
                name,
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                prefix=prefix,
            )

    def model(self, upstream_model: str) -> Target:
        """Select a concrete upstream model on this connection."""
        return Target(self.id, upstream_model)


@dataclass(frozen=True)
class RouterInfo:
    """Saved route metadata. Diagnostic describes the default connection, not a live probe."""

    id: str
    model: str
    published: bool
    diagnostic: str


def _info(route: pb.ModelRoute | None) -> RouterInfo:
    if route is None or not route.id:
        raise RouterError("router API returned no route")
    return RouterInfo(
        route.id, route.public_model_id, route.published, route.diagnostic.name.lower()
    )


class RouterClient:
    """Router administration using the SDK's normal Chalk credentials and environment.

    Definitions and deployed routes do not own provider credentials. Connections
    can be reused by many routes. Closing a client does not delete any resources.
    """

    def __init__(
        self,
        *,
        chalk_client: Any | None = None,
        connect_client: ConnectClient | None = None,
    ) -> None:
        if chalk_client is not None and connect_client is not None:
            raise ValueError("pass only one of chalk_client or connect_client")
        self._connect = connect_client or ConnectClient(chalk_client=chalk_client)
        self._owns_connect = connect_client is None

    def close(self) -> None:
        if self._owns_connect:
            self._connect.close()

    def __enter__(self) -> RouterClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _call(self, method: Callable[..., _T], request: object) -> _T:
        try:
            return self._connect.call(method, request)
        except ConnectRpcError as exc:
            error = RouterNotFoundError if exc.code == Code.NOT_FOUND else RouterError
            raise error(str(exc)) from exc

    def _routes(self) -> list[pb.ModelRoute]:
        stub = self._connect.stub(model_route_connect.ModelRouteServiceClientSync)
        routes: list[pb.ModelRoute] = []
        cursor: str | None = None
        while True:
            response = self._call(
                stub.list_model_routes, pb.ListModelRoutesRequest(cursor=cursor)
            )
            routes.extend(response.routes)
            cursor = response.next_cursor
            if not cursor:
                return routes

    def list_routes(self) -> list[RouterInfo]:
        """List active routes, including unpublished routes, across all pages."""
        return [_info(route) for route in self._routes()]

    def get(self, name: str) -> RouterInfo:
        """Look up an active route by name (with or without ``chalk/``)."""
        return _info(self._get_route(name))

    def _get_route(self, name: str) -> pb.ModelRoute:
        model = _model_name(name)
        for route in self._routes():
            if route.public_model_id == model:
                return route
        raise RouterNotFoundError(f"Router {model!r} not found")

    def list_connections(self) -> list[Connection]:
        """List active saved connections across all pages, without reading credentials."""
        stub = self._connect.stub(
            provider_connection_connect.ProviderConnectionServiceClientSync
        )
        connections: list[Connection] = []
        cursor: str | None = None
        while True:
            response = self._call(
                stub.list_provider_connections,
                connection_pb.ListProviderConnectionsRequest(cursor=cursor),
            )
            connections.extend(
                Connection(c.id, c.name, c.provider_kind, c.routing_enabled)
                for c in response.connections
            )
            cursor = response.next_cursor
            if not cursor:
                return connections

    def connection(self, name: str) -> Connection:
        """Resolve a saved connection by exact name; ambiguous names are rejected."""
        _nonblank(name, "connection name")
        matches = [c for c in self.list_connections() if c.name == name]
        if not matches:
            raise RouterNotFoundError(f"Provider connection {name!r} not found")
        if len(matches) > 1:
            raise RouterError(
                f"Multiple provider connections named {name!r}; use Target with an explicit connection ID"
            )
        return matches[0]

    def create_connection(
        self,
        name: str,
        *,
        provider: str,
        api_key: str,
        base_url: str | None = None,
        prefix: str | None = None,
    ) -> Connection:
        """Create a persistent, enabled provider connection for explicitly configured routes.

        This is a create operation, not an upsert: use ``connection(name)`` to
        reuse it. The key is sent to Chalk for encrypted storage. The connection
        uses explicit exposure so upstream models are not automatically listed.
        Provider-specific key and endpoint rules are validated by the server.
        """
        _nonblank(name, "connection name")
        _nonblank(provider, "provider")
        stub = self._connect.stub(
            provider_connection_connect.ProviderConnectionServiceClientSync
        )
        response = self._call(
            stub.create_provider_connection,
            connection_pb.CreateProviderConnectionRequest(
                name=name,
                provider_kind=provider,
                api_key=api_key,
                base_url=base_url,
                prefix=prefix,
                exposure=connection_pb.ExposurePolicy.EXPLICIT,
                routing_enabled=True,
            ),
        )
        c = response.connection
        if c is None or not c.id:
            raise RouterError("router API returned no provider connection")
        return Connection(c.id, c.name, c.provider_kind, c.routing_enabled)

    @_instrumented("chalkcompute.router.deploy")
    def deploy(self, router: Router) -> RouterInfo:
        """Create or replace the active route with this definition's model name.

        The server atomically replaces a single route's configuration. There is
        no compare-and-swap API: concurrent updates are last-writer-wins, and a
        concurrent first creation can fail with a name conflict. Retry deployment
        after resolving an uncertain response; lookup will find any saved route.
        """
        stub = self._connect.stub(model_route_connect.ModelRouteServiceClientSync)
        grid = router._spec.routing
        if grid is not None and (
            grid.classifier is not None or grid.routes or grid.fallback_defaults
        ):
            capabilities = self._call(
                stub.get_route_judge_defaults, pb.GetRouteJudgeDefaultsRequest()
            )
            if capabilities.routing_schema_version < 1:
                raise RouterError(
                    "This router server does not support classifier/routes configuration; upgrade it before deploying."
                )
            classifier = router._classifier
            if classifier is not None and classifier._function is not None:
                # Check server support before deploying a potentially expensive function.
                self._call(
                    stub.validate_route_judge_backend,
                    pb.ValidateRouteJudgeBackendRequest(backend=classifier._backend),
                )
                function = classifier._function
                self._connect._ensure_auth()
                function._client._ensure_auth()
                if self._connect._env_id != function._client._env_id:
                    raise RouterError(
                        "router and classifier function must use the same environment"
                    )
                function.deploy()
                function.wait_ready()
                version = function.version_info
                if version is None or not version.function_id:
                    raise RouterError("deployed classifier has no function ID")
                assert (
                    grid.classifier is not None and grid.classifier.backend is not None
                )
                backend = grid.classifier.backend.kind
                assert backend is not None and isinstance(
                    backend.value, pb.FunctionClassifierBackend
                )
                backend.value.function_id = version.function_id
        existing = next(
            (r for r in self._routes() if r.public_model_id == router.model), None
        )
        spec = router._spec
        if existing is None:
            response = self._call(stub.create_model_route, spec)
        else:
            response = self._call(
                stub.update_model_route,
                pb.UpdateModelRouteRequest(
                    id=existing.id,
                    update=pb.UpdateModelRouteOperation(
                        connection_id=spec.connection_id,
                        upstream_model_id=spec.upstream_model_id,
                        published=spec.published,
                        routing=spec.routing,
                    ),
                    # Including an absent routing value deliberately clears an old judge/fallback grid.
                    update_mask=FieldMask(
                        paths=[
                            "connection_id",
                            "upstream_model_id",
                            "published",
                            "routing",
                        ]
                    ),
                ),
            )
        return _info(response.route)

    def delete(self, name: str) -> list[str]:
        """Archive a route by name and return server warnings. Provider connections remain.

        Archived names remain reserved by the server. This operation can be
        refused while API keys or other resources still reference the route.
        """
        info = self.get(name)
        stub = self._connect.stub(model_route_connect.ModelRouteServiceClientSync)
        response = self._call(
            stub.delete_model_route, pb.DeleteModelRouteRequest(id=info.id)
        )
        return list(response.warnings)


@contextmanager
def _router_client(client: RouterClient | None) -> Generator[RouterClient, None, None]:
    if client is not None:
        yield client
    else:
        with RouterClient() as owned:
            yield owned


class Router:
    """Describe a model route locally, then publish it with ``deploy()``.

    ``name`` becomes ``chalk/<name>``. Repeated deployment updates that alias in
    the selected environment. ``default`` serves requests when no classifier is set,
    classification is off/shadow, or classification fails. ``fallbacks`` is the ordered
    recovery chain for the default target; other outcomes can supply their own.

    Construction performs no network I/O and snapshots the configuration.
    Deployment saves persistent router configuration; it does not provision a
    router server or probe provider availability. Inference uses the saved alias
    through Chalk's OpenAI/Anthropic-compatible router endpoint.
    Credentials are discovered automatically. Pass ``client=`` only when an
    explicit RouterClient is needed, just as with Sandbox.
    """

    def __init__(
        self,
        name: str,
        *,
        default: Target,
        fallbacks: Sequence[Target] = (),
        judge: RouterJudge | None = None,
        classifier: Classifier | DeployedFunction | None = None,
        routes: Mapping[str, Target | Route] | None = None,
        published: bool = True,
        client: RouterClient | None = None,
    ) -> None:
        from chalkcompute._classifier import Classifier

        if judge is not None and (classifier is not None or routes is not None):
            raise ValueError("use classifier/routes or judge, not both")
        if classifier is not None and not isinstance(classifier, Classifier):
            classifier = Classifier.function(classifier)
        if (classifier is None) != (routes is None):
            raise ValueError("classifier and routes must be supplied together")
        self._classifier = classifier
        chain = _fallbacks(default, fallbacks)
        routing = None
        if fallbacks or judge is not None:
            routing = pb.RouteGrid(
                default_target=default._to_proto(),
                fallbacks={default._reference: chain} if fallbacks else {},
                judge=pb.RouteJudge.from_binary(judge._proto.to_binary())
                if judge is not None
                else None,
            )
        if classifier is not None:
            checked = RouterJudge._build(
                classifier._backend,
                classifier._classification,
                routes or {},
                classifier._mode,
            )._proto
            if (
                classifier._backend.kind
                and classifier._backend.kind.field == "heuristic"
                and len(checked.outcomes) != 2
            ):
                raise ValueError("heuristic requires routes '0' and '1'")
            routing = pb.RouteGrid(
                default_target=default._to_proto(),
                classifier=pb.ClassifierConfig(
                    mode=checked.mode,
                    classification=checked.classification,
                    backend=checked.backend,
                ),
                routes={
                    label: pb.RoutingRule(
                        description=rule.description,
                        target=_target_from_reference(rule.model)._to_proto(),
                        fallbacks=None
                        if rule.fallbacks is None
                        else pb.TargetFallbacks(
                            targets=[
                                _target_from_reference(ref)._to_proto()
                                for ref in rule.fallbacks.models
                            ]
                        ),
                    )
                    for label, rule in checked.outcomes.items()
                },
                fallback_defaults=[
                    pb.ModelFallbacks(
                        target=default._to_proto(),
                        fallbacks=[target._to_proto() for target in fallbacks],
                    )
                ]
                if fallbacks
                else [],
            )
        self._spec = pb.CreateModelRouteRequest(
            public_model_id=_model_name(name),
            connection_id=default.connection_id,
            upstream_model_id=default.model,
            published=published,
            routing=routing,
        )
        self._info: RouterInfo | None = None
        self._client = client

    @classmethod
    def from_name(cls, name: str, *, client: RouterClient | None = None) -> Router:
        """Load a saved route's configuration and metadata using ambient credentials."""
        with _router_client(client) as resolved:
            route = resolved._get_route(name)
        router = cls.__new__(cls)
        router._spec = pb.CreateModelRouteRequest(
            public_model_id=route.public_model_id,
            connection_id=route.connection_id,
            upstream_model_id=route.upstream_model_id,
            published=route.published,
            routing=route.routing,
        )
        router._classifier = None
        router._info = _info(route)
        router._client = client
        return router

    @property
    def model(self) -> str:
        """The public model ID to send in inference requests, e.g. ``chalk/assistant``."""
        return self._spec.public_model_id

    @property
    def id(self) -> str | None:
        """The saved route ID, or None before a successful deployment."""
        return self._info.id if self._info is not None else None

    @property
    def info(self) -> RouterInfo | None:
        """Last retrieved metadata; call ``refresh()`` for a fresh read."""
        return self._info

    def deploy(self, *, client: RouterClient | None = None) -> Router:
        """Create or update this alias and return self, like ``Sandbox.run()``."""
        resolved_client = client if client is not None else self._client
        with _router_client(resolved_client) as resolved:
            self._info = resolved.deploy(self)
        self._client = resolved_client
        return self

    def refresh(self) -> RouterInfo:
        """Fetch current metadata for this alias without changing its local definition."""
        with _router_client(self._client) as resolved:
            self._info = resolved.get(self.model)
        return self._info

    def delete(self) -> list[str]:
        """Archive this route and return server warnings. Its connections remain."""
        with _router_client(self._client) as resolved:
            warnings = resolved.delete(self.model)
        self._info = None
        return warnings


def _target_from_reference(reference: str) -> Target:
    connection, separator, model = reference.partition("/")
    if not separator:
        raise RouterError("server returned an invalid concrete target")
    return Target(connection, model)
