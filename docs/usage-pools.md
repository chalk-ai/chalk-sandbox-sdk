# Usage pools

Usage pools group router API keys so their limits and token budgets can be shared.
Use your usual Chalk credentials and active environment; no client construction
or router-specific secret is required. The environment needs Model Gateway enabled.

```python
import chalkcompute as cc

# Persistent configuration: retained after this program exits.
pool = (
    cc.UsagePool("customer-support")
    .with_rate_limit(requests_per_minute=120, burst=20)
    .with_usage_budget(token_limit=1_000_000, period="monthly")
    .deploy()
)
print(pool.id)

# In another process, look up the active, case-sensitive name.
pool = cc.UsagePool.from_name("customer-support")
pool.name = "customer-success"
pool.deploy()  # Renames only; preserves attached budgets and rate limits.

for existing in cc.UsagePool.list():
    print(existing.id, existing.name)
```

Assign router API keys to the resulting pool ID through existing gateway key
management. Creating an empty pool does not move any keys or enforce a quota.
Budgets and rate limits are separate resources owned by the pool. The fluent
methods return the same pool and queue configuration locally. Call them repeatedly
for multiple rate-limit dimensions or budget periods. Policies require quota
enforcement to be enabled in the environment.

```python
pool.with_rate_limit(tokens_per_minute=50_000).deploy()
```

On a new pool, deployment submits all configured policies with the pool creation.
On a deployed or loaded pool, deployment adds only newly queued policies, preserving
existing ones. Successfully applied entries are removed from the queue, so deploying
the same handle again does not duplicate them. New policy additions to an existing
pool use individual API calls and can partially succeed. A pool can have only one
budget per period; adding another for the same period raises an API error. Use the
budget's handle to edit its limit instead. `refresh()` preserves queued additions.

The separate `UsageBudget` and `RateLimit` resources accept `usage_pool=pool` for
either a local definition or a deployed handle. See [usage budgets](usage-budgets.md)
and [rate limits](router-rate-limits.md) for standalone policy management.
Deploying a standalone policy creates its undeployed pool first with the same
client and environment. An already deployed pool is referenced by ID without
redeploying it; any new queued changes on that pool still require `pool.deploy()`.
An existing pool ID can also be passed as `usage_pool="pool-id"`.

Constructing `UsagePool` is local. The first `deploy()` creates a new pool;
an existing name raises an API error instead of overwriting configuration.
Subsequent deploys on the same handle update its name and apply queued additions. Handles are bound to the
Chalk environment in which they were created or loaded. `refresh()` reloads
server state, and `from_id(id)` also permits inspection of archived pools.

After a lost create response, reconcile with `from_name()` before retrying.
Create requests are not automatically retried. For an ambiguous policy creation
response, inspect the pool's policies before retrying; rate-limit creation has no
idempotency key. Updates remain last-writer-wins.

Optionally reuse a client or supply an existing ChalkClient for authentication.
Injected clients remain caller-owned:

```python
client = cc.ConnectClient(chalk_client=your_chalk_client)
try:
    pool = cc.UsagePool.from_name("customer-success", client=client)
finally:
    client.close()
```

`pool.delete()` archives the pool and its policies. Active API keys assigned to
it can prevent deletion; reassign or revoke those keys first. Closing a client
does not delete the pool. `UsagePool.list(include_archived=True)` includes
archived pools, which cannot be redeployed.
