"""Server-enforced rate limits for router API keys and usage pools."""

from __future__ import annotations

import builtins

from collections.abc import Sequence
from datetime import datetime

from protobuf.wkt import FieldMask

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.router.v1 import rate_limit_connect, rate_limit_pb as pb
from chalkcompute._router_resource import (
    RouterResource,
    client_for,
    nonblank,
    rate_dimension,
    resolve_owner,
    scope_for,
    validate_rate_limit,
)

from chalkcompute._usage_pool import UsagePool


class RateLimit(RouterResource):
    """One gateway rate-limit dimension for a usage pool or router API key.

    Specify exactly one of ``requests_per_minute``, ``tokens_per_minute``, or
    ``concurrent_requests``. Separate objects can enforce multiple dimensions.
    This is a persistent gateway policy, separate from the outbound function
    ``RateLimitPolicy``. Construct locally, then call ``deploy()``.
    ``usage_pool`` accepts a local definition, deployed handle, or pool ID. Deploy
    creates an undeployed pool first using the same client. An already deployed
    pool is reused without deploying any queued changes on it.
    """

    def __init__(
        self,
        *,
        usage_pool: UsagePool | str | None = None,
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
        self._scope, self._owner = scope_for(usage_pool, api_key_id)
        self._limit_type, self.limit_value = rate_dimension(
            requests_per_minute,
            tokens_per_minute,
            concurrent_requests,
        )
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
        return self._owner if isinstance(self._owner, str) else self._owner.id

    @property
    def limit_type(self) -> str:
        return self._limit_type

    def _validate(self) -> None:
        validate_rate_limit(
            self.limit_type,
            self.limit_value,
            self.burst,
            self.connection_id,
            self.models,
            self.enabled,
        )

    @classmethod
    def _from_proto(
        cls, policy: pb.RateLimitPolicy | None, env: str | None
    ) -> RateLimit:
        if policy is None or not policy.id:
            raise RuntimeError("rate limit API returned no policy")
        if policy.scope not in ("usage_pool", "api_key"):
            raise RuntimeError("rate limit API returned an unsupported scope")
        if policy.limit_type not in (
            "requests_per_minute",
            "tokens_per_minute",
            "concurrent_requests",
        ):
            raise RuntimeError("rate limit API returned an unsupported dimension")
        resource = cls(
            usage_pool=policy.scope_id if policy.scope == "usage_pool" else None,
            api_key_id=policy.scope_id if policy.scope == "api_key" else None,
            requests_per_minute=policy.limit_value
            if policy.limit_type == "requests_per_minute"
            else None,
            tokens_per_minute=policy.limit_value
            if policy.limit_type == "tokens_per_minute"
            else None,
            concurrent_requests=policy.limit_value
            if policy.limit_type == "concurrent_requests"
            else None,
            burst=policy.burst_value,
            connection_id=policy.connection_id,
            models=policy.models,
            enabled=policy.enabled,
        )
        resource._id = policy.id
        resource._environment_id = env
        resource.created_at = (
            policy.created_at.to_datetime() if policy.created_at else None
        )
        return resource

    def _load(self, policy: pb.RateLimitPolicy | None, env: str | None) -> RateLimit:
        loaded = self._from_proto(policy, env)
        if not isinstance(self._owner, str) and self._owner._id == loaded._owner:
            loaded._owner = self._owner
        self.__dict__.update(loaded.__dict__)
        return self

    def deploy(self, *, client: ConnectClient | None = None) -> RateLimit:
        """Create a policy or save changes to this handle's limit and filters."""
        self._require_active()
        self._validate()
        with client_for(client, self._environment_id) as c:
            scope_id = resolve_owner(self._owner, c, create=self._id is None)
            stub = c.stub(rate_limit_connect.RateLimitServiceClientSync)
            if self._id is None:
                policy = c.call(
                    stub.create_rate_limit,
                    pb.CreateRateLimitRequest(
                        scope=self.scope,
                        scope_id=scope_id,
                        limit_type=self.limit_type,
                        limit_value=self.limit_value,
                        burst_value=self.burst,
                        connection_id=self.connection_id,
                        models=list(self.models),
                        enabled=self.enabled,
                    ),
                ).policy
            else:
                policy = c.call(
                    stub.update_rate_limit,
                    pb.UpdateRateLimitRequest(
                        id=self.id,
                        update=pb.UpdateRateLimitOperation(
                            limit_value=self.limit_value,
                            burst_value=self.burst,
                            connection_id=self.connection_id,
                            models=list(self.models),
                            enabled=self.enabled,
                        ),
                        update_mask=FieldMask(
                            paths=[
                                "limit_value",
                                "burst_value",
                                "connection_id",
                                "models",
                                "enabled",
                            ]
                        ),
                    ),
                ).policy
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
        cls,
        *,
        usage_pool: UsagePool | str | None = None,
        api_key_id: str | None = None,
        client: ConnectClient | None = None,
    ) -> builtins.list[RateLimit]:
        """Read every page, optionally selecting one pool or key's policies."""
        owner = (
            None
            if usage_pool is None and api_key_id is None
            else scope_for(usage_pool, api_key_id)
        )
        policies: list[RateLimit] = []
        cursor: str | None = None
        seen: set[str] = set()
        with client_for(client) as c:
            resolved = (
                None
                if owner is None
                else (owner[0], resolve_owner(owner[1], c, create=False))
            )
            stub = c.stub(rate_limit_connect.RateLimitServiceClientSync)
            while True:
                response = c.call(
                    stub.list_rate_limits, pb.ListRateLimitsRequest(cursor=cursor)
                )
                policies.extend(
                    cls._from_proto(policy, c._env_id)
                    for policy in response.policies
                    if resolved is None or (policy.scope, policy.scope_id) == resolved
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
