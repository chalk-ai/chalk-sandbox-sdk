"""Named usage pools for shared router quotas."""

from __future__ import annotations

import builtins

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from protobuf import Oneof
from protobuf.wkt import FieldMask

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.router.v1 import (
    rate_limit_connect,
    rate_limit_pb,
    usage_budget_connect,
    usage_budget_pb,
    usage_pool_connect,
    usage_pool_pb as pb,
)
from chalkcompute._router_resource import (
    RouterResource,
    boolean,
    client_for,
    nonblank,
    positive,
    rate_dimension,
    validate_rate_limit,
)

_PERIODS = {
    "daily": usage_budget_pb.UsageBudgetPeriod.DAILY,
    "weekly": usage_budget_pb.UsageBudgetPeriod.WEEKLY,
    "monthly": usage_budget_pb.UsageBudgetPeriod.MONTHLY,
}


class UsagePool(RouterResource):
    """A named pool shared by router API keys.

    Constructing a pool is local. ``deploy()`` creates it using ambient Chalk
    auth. Chain ``with_rate_limit`` and ``with_usage_budget`` to queue policies.
    Later deploys append newly queued policies and preserve existing quotas.
    Use ``from_name`` to manage a pool created by another process. A new
    definition never silently adopts an existing name.
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = nonblank(name, "name")
        self.created_at: datetime | None = None
        self.archived_at: datetime | None = None
        self._pending_rate_limits: list[rate_limit_pb.ApiKeyRateLimit] = []
        self._pending_usage_budgets: list[usage_budget_pb.UsageBudgetSpec] = []

    def with_rate_limit(
        self,
        *,
        requests_per_minute: int | None = None,
        tokens_per_minute: int | None = None,
        concurrent_requests: int | None = None,
        burst: int | None = None,
        connection_id: str | None = None,
        models: Sequence[str] = (),
        enabled: bool = True,
    ) -> UsagePool:
        """Queue one additional rate limit locally; apply it on the next deploy."""
        self._require_active()
        dimension, value = rate_dimension(
            requests_per_minute, tokens_per_minute, concurrent_requests
        )
        validate_rate_limit(dimension, value, burst, connection_id, models, enabled)
        self._pending_rate_limits.append(
            rate_limit_pb.ApiKeyRateLimit(
                requests_per_minute=requests_per_minute,
                tokens_per_minute=tokens_per_minute,
                concurrent_requests=concurrent_requests,
                request_burst=burst if dimension == "requests_per_minute" else None,
                token_burst=burst if dimension == "tokens_per_minute" else None,
                connection_id=connection_id,
                models=list(models),
                enabled=enabled,
            )
        )
        return self

    def with_usage_budget(
        self,
        *,
        token_limit: int,
        period: Literal["daily", "weekly", "monthly"],
        enabled: bool = True,
    ) -> UsagePool:
        """Queue an additional calendar token budget locally for the next deploy."""
        self._require_active()
        if period not in _PERIODS:
            raise ValueError("period must be daily, weekly, or monthly")
        if any(
            budget.period == _PERIODS[period] for budget in self._pending_usage_budgets
        ):
            raise ValueError("a usage budget for this period is already queued")
        self._pending_usage_budgets.append(
            usage_budget_pb.UsageBudgetSpec(
                token_limit=positive(token_limit, "token_limit"),
                period=_PERIODS[period],
                enabled=boolean(enabled, "enabled"),
            )
        )
        return self

    def _load(self, pool: pb.UsagePool | None, environment_id: str | None) -> UsagePool:
        if pool is None or not pool.id:
            raise RuntimeError("usage pool API returned no pool")
        self._id = pool.id
        self._environment_id = environment_id
        self.name = pool.name
        self.created_at = pool.created_at.to_datetime() if pool.created_at else None
        self.archived_at = pool.archived_at.to_datetime() if pool.archived_at else None
        self._deleted = self.archived_at is not None
        return self

    def deploy(self, *, client: ConnectClient | None = None) -> UsagePool:
        """Create or rename the pool and apply newly queued policies. Return self."""
        self._require_active()
        nonblank(self.name, "name")
        with client_for(client, self._environment_id) as c:
            stub = c.stub(usage_pool_connect.UsagePoolServiceClientSync)
            if self._id is None:
                pool = c.call(
                    stub.create_usage_pool,
                    pb.CreateUsagePoolRequest(
                        name=self.name,
                        rate_limits=self._pending_rate_limits,
                        usage_budgets=self._pending_usage_budgets,
                    ),
                ).pool
                self._load(pool, c._env_id)
                self._pending_rate_limits.clear()
                self._pending_usage_budgets.clear()
            else:
                pool = c.call(
                    stub.update_usage_pool,
                    pb.UpdateUsagePoolRequest(
                        id=self.id,
                        update=pb.UpdateUsagePoolOperation(name=self.name),
                        update_mask=FieldMask(paths=["name"]),
                    ),
                ).pool
            self._load(pool, c._env_id)
            self._deploy_pending_policies(c)
            return self

    def _deploy_pending_policies(self, client: ConnectClient) -> None:
        # Add policies individually: replacing the pool's policy sets would erase
        # policies managed elsewhere. Remove each item only after its API succeeds.
        while self._pending_rate_limits:
            spec = self._pending_rate_limits[0]
            dimension, value = rate_dimension(
                spec.requests_per_minute,
                spec.tokens_per_minute,
                spec.concurrent_requests,
            )
            rate_response = client.call(
                client.stub(
                    rate_limit_connect.RateLimitServiceClientSync
                ).create_rate_limit,
                rate_limit_pb.CreateRateLimitRequest(
                    scope="usage_pool",
                    scope_id=self.id,
                    limit_type=dimension,
                    limit_value=value,
                    burst_value=spec.request_burst
                    if dimension == "requests_per_minute"
                    else spec.token_burst,
                    connection_id=spec.connection_id,
                    models=spec.models,
                    enabled=spec.enabled,
                ),
            )
            if rate_response.policy is None or not rate_response.policy.id:
                raise RuntimeError("rate limit API returned no policy")
            self._pending_rate_limits.pop(0)
        while self._pending_usage_budgets:
            budget = self._pending_usage_budgets[0]
            budget_response = client.call(
                client.stub(
                    usage_budget_connect.UsageBudgetServiceClientSync
                ).create_usage_budget,
                usage_budget_pb.CreateUsageBudgetRequest(
                    scope="usage_pool",
                    scope_id=self.id,
                    token_limit=budget.token_limit,
                    period=budget.period,
                    enabled=budget.enabled,
                ),
            )
            if budget_response.budget is None or not budget_response.budget.id:
                raise RuntimeError("usage budget API returned no budget")
            self._pending_usage_budgets.pop(0)

    @classmethod
    def from_name(cls, name: str, *, client: ConnectClient | None = None) -> UsagePool:
        """Load an active pool by its case-sensitive name."""
        with client_for(client) as c:
            pool = c.call(
                c.stub(usage_pool_connect.UsagePoolServiceClientSync).get_usage_pool,
                pb.GetUsagePoolRequest(lookup=Oneof("name", nonblank(name, "name"))),
            ).pool
            return cls(name)._load(pool, c._env_id)

    @classmethod
    def from_id(cls, id: str, *, client: ConnectClient | None = None) -> UsagePool:
        """Load a pool, including an archived pool, by ID."""
        with client_for(client) as c:
            pool = c.call(
                c.stub(usage_pool_connect.UsagePoolServiceClientSync).get_usage_pool,
                pb.GetUsagePoolRequest(lookup=Oneof("id", nonblank(id, "id"))),
            ).pool
            if pool is None:
                raise RuntimeError("usage pool API returned no pool")
            return cls(pool.name)._load(pool, c._env_id)

    @classmethod
    def list(
        cls,
        *,
        include_archived: bool = False,
        client: ConnectClient | None = None,
    ) -> builtins.list[UsagePool]:
        """List all pages of pools in the active environment."""
        boolean(include_archived, "include_archived")
        pools: list[UsagePool] = []
        cursor: str | None = None
        seen: set[str] = set()
        with client_for(client) as c:
            stub = c.stub(usage_pool_connect.UsagePoolServiceClientSync)
            while True:
                response = c.call(
                    stub.list_usage_pools,
                    pb.ListUsagePoolsRequest(
                        cursor=cursor,
                        include_archived=include_archived,
                    ),
                )
                pools.extend(cls(p.name)._load(p, c._env_id) for p in response.pools)
                cursor = response.next_cursor
                if not cursor:
                    return pools
                if cursor in seen:
                    raise RuntimeError("usage pool API repeated a pagination cursor")
                seen.add(cursor)

    def refresh(self, *, client: ConnectClient | None = None) -> UsagePool:
        """Reload pool metadata, preserving locally queued policies."""
        with client_for(client, self._environment_id) as c:
            pool = c.call(
                c.stub(usage_pool_connect.UsagePoolServiceClientSync).get_usage_pool,
                pb.GetUsagePoolRequest(lookup=Oneof("id", self.id)),
            ).pool
            return self._load(pool, c._env_id)

    def delete(self, *, client: ConnectClient | None = None) -> None:
        """Archive the pool and its policies. Active API-key assignments block deletion."""
        self._require_active()
        with client_for(client, self._environment_id) as c:
            c.call(
                c.stub(usage_pool_connect.UsagePoolServiceClientSync).delete_usage_pool,
                pb.DeleteUsagePoolRequest(id=self.id),
            )
            self._deleted = True
