"""Named usage pools for shared router quotas."""

from __future__ import annotations

from datetime import datetime

from protobuf import Oneof
from protobuf.wkt import FieldMask

from chalkcompute._connect import ConnectClient
from chalkcompute._gen.chalk.router.v1 import usage_pool_connect, usage_pool_pb as pb
from chalkcompute._router_resource import RouterResource, boolean, client_for, nonblank


class UsagePool(RouterResource):
    """A named pool shared by router API keys.

    Constructing a pool is local. ``deploy()`` creates it using ambient Chalk
    auth; later deploys on the same handle rename it without changing its quotas.
    Use ``from_name`` to manage a pool created by another process. A new
    definition never silently adopts an existing name.
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = nonblank(name, "name")
        self.created_at: datetime | None = None
        self.archived_at: datetime | None = None

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
        """Create the pool, or save its name without replacing budgets or limits."""
        self._require_active()
        nonblank(self.name, "name")
        with client_for(client, self._environment_id) as c:
            stub = c.stub(usage_pool_connect.UsagePoolServiceClientSync)
            if self._id is None:
                pool = c.call(stub.create_usage_pool, pb.CreateUsagePoolRequest(name=self.name)).pool
            else:
                pool = c.call(
                    stub.update_usage_pool,
                    pb.UpdateUsagePoolRequest(
                        id=self.id,
                        update=pb.UpdateUsagePoolOperation(name=self.name),
                        update_mask=FieldMask(paths=["name"]),
                    ),
                ).pool
            return self._load(pool, c._env_id)

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
        cls, *, include_archived: bool = False, client: ConnectClient | None = None,
    ) -> list[UsagePool]:
        """List all pages of pools in the active environment."""
        boolean(include_archived, "include_archived")
        pools: list[UsagePool] = []
        cursor: str | None = None
        seen: set[str] = set()
        with client_for(client) as c:
            stub = c.stub(usage_pool_connect.UsagePoolServiceClientSync)
            while True:
                response = c.call(stub.list_usage_pools, pb.ListUsagePoolsRequest(
                    cursor=cursor, include_archived=include_archived,
                ))
                pools.extend(cls(p.name)._load(p, c._env_id) for p in response.pools)
                cursor = response.next_cursor
                if not cursor:
                    return pools
                if cursor in seen:
                    raise RuntimeError("usage pool API repeated a pagination cursor")
                seen.add(cursor)

    def refresh(self, *, client: ConnectClient | None = None) -> UsagePool:
        """Replace local state with the current server state."""
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
