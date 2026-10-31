"""Server-enforced rate limits for router API keys and usage pools."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from protobuf.wkt import FieldMask

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.router.v1 import rate_limit_connect, rate_limit_pb as pb
from chalkcompute._router_resource import (
    RouterResource, boolean, client_for, nonblank, positive, scope_for,
)


class RateLimit(RouterResource):
    """One gateway rate-limit dimension for a usage pool or router API key.

    Specify exactly one of ``requests_per_minute``, ``tokens_per_minute``, or
    ``concurrent_requests``. Separate objects can enforce multiple dimensions.
    This is a persistent gateway policy, separate from the outbound function
    ``RateLimitPolicy``. Construct locally, then call ``deploy()``.
    """

    def __init__(
        self,
        *,
        usage_pool_id: str | None = None,
        api_key_id: str | None = None,
        requests_per_minute: int | None = None,
        tokens_per_minute: int | None = None,
        concurrent_requests: int | None = None,
        burst: int | None = None,
        connection_id: str | None = None,
        models: Sequence[str] = (),
        enabled: bool = True,
    ) -> None:
        super().__init__()
        self._scope, self._scope_id = scope_for(usage_pool_id, api_key_id)
        dimensions = {
            "requests_per_minute": requests_per_minute,
            "tokens_per_minute": tokens_per_minute,
            "concurrent_requests": concurrent_requests,
        }
        selected = [(name, value) for name, value in dimensions.items() if value is not None]
        if len(selected) != 1:
            raise ValueError("provide exactly one rate-limit dimension")
        self._limit_type, self.limit_value = selected[0]
        self.burst = burst
        self.connection_id = connection_id
        if isinstance(models, (str, bytes)):
            raise TypeError("models must be a sequence of model names, not a string")
        self.models = tuple(models)
        self.enabled = enabled
        self.created_at: datetime | None = None
        self._validate()

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def limit_type(self) -> str:
        return self._limit_type

    def _validate(self) -> None:
        positive(self.limit_value, "limit_value")
        boolean(self.enabled, "enabled")
        if self.burst is not None:
            positive(self.burst, "burst")
            if self.limit_type == "concurrent_requests" and self.burst != self.limit_value:
                raise ValueError("concurrent request burst must be absent or equal to the limit")
        if self.connection_id is not None:
            nonblank(self.connection_id, "connection_id")
        if isinstance(self.models, (str, bytes)):
            raise TypeError("models must be a sequence of model names, not a string")
        if self.models and self.connection_id is None:
            raise ValueError("models requires connection_id")
        for model in self.models:
            nonblank(model, "model")

    @classmethod
    def _from_proto(cls, policy: pb.RateLimitPolicy | None, env: str | None) -> RateLimit:
        if policy is None or not policy.id:
            raise RuntimeError("rate limit API returned no policy")
        if policy.scope not in ("usage_pool", "api_key"):
            raise RuntimeError("rate limit API returned an unsupported scope")
        if policy.limit_type not in ("requests_per_minute", "tokens_per_minute", "concurrent_requests"):
            raise RuntimeError("rate limit API returned an unsupported dimension")
        resource = cls(
            usage_pool_id=policy.scope_id if policy.scope == "usage_pool" else None,
            api_key_id=policy.scope_id if policy.scope == "api_key" else None,
            requests_per_minute=policy.limit_value if policy.limit_type == "requests_per_minute" else None,
            tokens_per_minute=policy.limit_value if policy.limit_type == "tokens_per_minute" else None,
            concurrent_requests=policy.limit_value if policy.limit_type == "concurrent_requests" else None,
            burst=policy.burst_value, connection_id=policy.connection_id,
            models=policy.models, enabled=policy.enabled,
        )
        resource._id = policy.id
        resource._environment_id = env
        resource.created_at = policy.created_at.to_datetime() if policy.created_at else None
        return resource

    def _load(self, policy: pb.RateLimitPolicy | None, env: str | None) -> RateLimit:
        loaded = self._from_proto(policy, env)
        self.__dict__.update(loaded.__dict__)
        return self

    def deploy(self, *, client: ConnectClient | None = None) -> RateLimit:
        """Create a policy or save changes to this handle's limit and filters."""
        self._require_active()
        self._validate()
        with client_for(client, self._environment_id) as c:
            stub = c.stub(rate_limit_connect.RateLimitServiceClientSync)
            if self._id is None:
                policy = c.call(stub.create_rate_limit, pb.CreateRateLimitRequest(
                    scope=self.scope, scope_id=self.scope_id, limit_type=self.limit_type,
                    limit_value=self.limit_value, burst_value=self.burst,
                    connection_id=self.connection_id, models=list(self.models), enabled=self.enabled,
                )).policy
            else:
                policy = c.call(stub.update_rate_limit, pb.UpdateRateLimitRequest(
                    id=self.id,
                    update=pb.UpdateRateLimitOperation(
                        limit_value=self.limit_value, burst_value=self.burst,
                        connection_id=self.connection_id, models=list(self.models), enabled=self.enabled,
                    ),
                    update_mask=FieldMask(paths=[
                        "limit_value", "burst_value", "connection_id", "models", "enabled",
                    ]),
                )).policy
            return self._load(policy, c._env_id)

    @classmethod
    def from_id(cls, id: str, *, client: ConnectClient | None = None) -> RateLimit:
        with client_for(client) as c:
            policy = c.call(
                c.stub(rate_limit_connect.RateLimitServiceClientSync).get_rate_limit,
                pb.GetRateLimitRequest(id=nonblank(id, "id")),
            ).policy
            return cls._from_proto(policy, c._env_id)

    @classmethod
    def list(
        cls, *, usage_pool_id: str | None = None, api_key_id: str | None = None,
        client: ConnectClient | None = None,
    ) -> list[RateLimit]:
        """Read every page, optionally selecting one pool or key's policies."""
        owner = None if usage_pool_id is None and api_key_id is None else scope_for(usage_pool_id, api_key_id)
        policies: list[RateLimit] = []
        cursor: str | None = None
        seen: set[str] = set()
        with client_for(client) as c:
            stub = c.stub(rate_limit_connect.RateLimitServiceClientSync)
            while True:
                response = c.call(stub.list_rate_limits, pb.ListRateLimitsRequest(cursor=cursor))
                policies.extend(
                    cls._from_proto(policy, c._env_id) for policy in response.policies
                    if owner is None or (policy.scope, policy.scope_id) == owner
                )
                cursor = response.next_cursor
                if not cursor:
                    return policies
                if cursor in seen:
                    raise RuntimeError("rate limit API repeated a pagination cursor")
                seen.add(cursor)

    def refresh(self, *, client: ConnectClient | None = None) -> RateLimit:
        self._require_active()
        with client_for(client, self._environment_id) as c:
            policy = c.call(
                c.stub(rate_limit_connect.RateLimitServiceClientSync).get_rate_limit,
                pb.GetRateLimitRequest(id=self.id),
            ).policy
            return self._load(policy, c._env_id)

    def delete(self, *, client: ConnectClient | None = None) -> None:
        """Delete this policy without deleting its owner or other policies."""
        self._require_active()
        with client_for(client, self._environment_id) as c:
            c.call(
                c.stub(rate_limit_connect.RateLimitServiceClientSync).delete_rate_limit,
                pb.DeleteRateLimitRequest(id=self.id),
            )
            self._deleted = True
