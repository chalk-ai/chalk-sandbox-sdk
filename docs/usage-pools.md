# Usage pools

Usage pools group router API keys so their limits and token budgets can be shared.
Use your usual Chalk credentials and active environment; no client construction
or router-specific secret is required. The environment needs Model Gateway enabled.

```python
import chalkcompute as cc

# Persistent configuration: retained after this program exits.
pool = cc.UsagePool("customer-support").deploy()
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
Budgets and rate limits are separate resources scoped to this ID.

Constructing `UsagePool` is local. The first `deploy()` creates a new pool;
an existing name raises an API error instead of overwriting configuration.
Subsequent deploys on the same handle update its name. Handles are bound to the
Chalk environment in which they were created or loaded. `refresh()` reloads
server state, and `from_id(id)` also permits inspection of archived pools.

After a lost create response, reconcile with `from_name()` before retrying.
Create requests are not automatically retried. Updates remain last-writer-wins.

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
