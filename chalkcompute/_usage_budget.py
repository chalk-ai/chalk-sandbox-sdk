"""Fixed-window token budgets for router API keys and usage pools."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from protobuf.wkt import FieldMask

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.router.v1 import (
    usage_budget_connect,
    usage_budget_pb as pb,
)
from chalkcompute._router_resource import (
    RouterResource,
    boolean,
    client_for,
    nonblank,
    positive,
    resolve_owner,
    scope_for,
)

from chalkcompute._usage_pool import UsagePool

Period = Literal["daily", "weekly", "monthly"]
_PERIODS = {
    "daily": pb.UsageBudgetPeriod.DAILY,
    "weekly": pb.UsageBudgetPeriod.WEEKLY,
    "monthly": pb.UsageBudgetPeriod.MONTHLY,
}


class UsageBudget(RouterResource):
    """A token budget scoped to exactly one usage pool or router API key.

    Construct locally, then ``deploy()`` with ambient Chalk authentication.
    ``usage_pool`` accepts a local pool definition, a deployed handle, or a pool
    ID. Deploy creates an undeployed pool first using the same client. Loaded
    pools are reused without deploying any queued changes on them.
    ``token_limit`` and ``enabled`` can be changed and redeployed. Ownership and
    period are immutable; use ``from_id`` to edit a previously created budget.
    """

    def __init__(
        self,
        *,
        token_limit: int,
        period: Period,
        usage_pool: UsagePool | str | None = None,
        api_key_id: str | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__()
        self._scope, self._owner = scope_for(usage_pool, api_key_id)
        if period not in _PERIODS:
            raise ValueError("period must be daily, weekly, or monthly")
        self._period = period
        self.token_limit = positive(token_limit, "token_limit")
        self.enabled = boolean(enabled, "enabled")
        self.created_at: datetime | None = None
        self.current_usage: int | None = None
        self.next_reset_at: datetime | None = None

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def scope_id(self) -> str:
        return self._owner if isinstance(self._owner, str) else self._owner.id

    @property
    def period(self) -> Period:
        return self._period

    @classmethod
    def _from_proto(cls, budget: pb.UsageBudget | None, env: str | None) -> UsageBudget:
        if budget is None or not budget.id:
            raise RuntimeError("usage budget API returned no budget")
        period: Period
        if budget.period == pb.UsageBudgetPeriod.DAILY:
            period = "daily"
        elif budget.period == pb.UsageBudgetPeriod.WEEKLY:
            period = "weekly"
        elif budget.period == pb.UsageBudgetPeriod.MONTHLY:
            period = "monthly"
        else:
            raise RuntimeError("usage budget API returned an unsupported period")
        if budget.scope not in ("usage_pool", "api_key"):
            raise RuntimeError("usage budget API returned an unsupported scope")
        resource = cls(
            token_limit=budget.token_limit,
            period=period,
            usage_pool=budget.scope_id if budget.scope == "usage_pool" else None,
            api_key_id=budget.scope_id if budget.scope == "api_key" else None,
            enabled=budget.enabled,
        )
        resource._id = budget.id
        resource._environment_id = env
        resource.created_at = (
            budget.created_at.to_datetime() if budget.created_at else None
        )
        resource.current_usage = budget.current_usage
        resource.next_reset_at = (
            budget.next_reset_at.to_datetime() if budget.next_reset_at else None
        )
        return resource

    def _load(self, budget: pb.UsageBudget | None, env: str | None) -> UsageBudget:
        loaded = self._from_proto(budget, env)
        if not isinstance(self._owner, str) and self._owner._id == loaded._owner:
            loaded._owner = self._owner
        self.__dict__.update(loaded.__dict__)
        return self

    def deploy(self, *, client: ConnectClient | None = None) -> UsageBudget:
        """Create a budget, or update this handle's token limit and enabled flag."""
        self._require_active()
        positive(self.token_limit, "token_limit")
        boolean(self.enabled, "enabled")
        with client_for(client, self._environment_id) as c:
            scope_id = resolve_owner(self._owner, c, create=self._id is None)
            stub = c.stub(usage_budget_connect.UsageBudgetServiceClientSync)
            if self._id is None:
                budget = c.call(
                    stub.create_usage_budget,
                    pb.CreateUsageBudgetRequest(
                        scope=self.scope,
                        scope_id=scope_id,
                        token_limit=self.token_limit,
                        period=_PERIODS[self.period],
                        enabled=self.enabled,
                    ),
                ).budget
            else:
                budget = c.call(
                    stub.update_usage_budget,
                    pb.UpdateUsageBudgetRequest(
                        id=self.id,
                        update=pb.UpdateUsageBudgetOperation(
                            token_limit=self.token_limit, enabled=self.enabled
                        ),
                        update_mask=FieldMask(paths=["token_limit", "enabled"]),
                    ),
                ).budget
            return self._load(budget, c._env_id)

    @classmethod
    def from_id(cls, id: str, *, client: ConnectClient | None = None) -> UsageBudget:
        with client_for(client) as c:
            budget = c.call(
                c.stub(
                    usage_budget_connect.UsageBudgetServiceClientSync
                ).get_usage_budget,
                pb.GetUsageBudgetRequest(id=nonblank(id, "id")),
            ).budget
            return cls._from_proto(budget, c._env_id)

    @classmethod
    def list(
        cls,
        *,
        usage_pool: UsagePool | str | None = None,
        api_key_id: str | None = None,
        client: ConnectClient | None = None,
    ) -> list[UsageBudget]:
        """List budgets for a pool or key; local pools are looked up by name."""
        owner = (
            None
            if usage_pool is None and api_key_id is None
            else scope_for(usage_pool, api_key_id)
        )
        with client_for(client) as c:
            scope, scope_id = (
                (None, None)
                if owner is None
                else (
                    owner[0],
                    resolve_owner(owner[1], c, create=False),
                )
            )
            response = c.call(
                c.stub(
                    usage_budget_connect.UsageBudgetServiceClientSync
                ).list_usage_budgets,
                pb.ListUsageBudgetsRequest(scope=scope, scope_id=scope_id),
            )
            return [cls._from_proto(budget, c._env_id) for budget in response.budgets]

    def refresh(self, *, client: ConnectClient | None = None) -> UsageBudget:
        """Reload configuration and the current admission counter, when available."""
        self._require_active()
        with client_for(client, self._environment_id) as c:
            budget = c.call(
                c.stub(
                    usage_budget_connect.UsageBudgetServiceClientSync
                ).get_usage_budget,
                pb.GetUsageBudgetRequest(id=self.id),
            ).budget
            return self._load(budget, c._env_id)

    def delete(self, *, client: ConnectClient | None = None) -> None:
        """Delete this budget without deleting its owner or other periods' budgets."""
        self._require_active()
        with client_for(client, self._environment_id) as c:
            c.call(
                c.stub(
                    usage_budget_connect.UsageBudgetServiceClientSync
                ).delete_usage_budget,
                pb.DeleteUsageBudgetRequest(id=self.id),
            )
            self._deleted = True
