# Usage budgets

Create token budgets for a router usage pool or API key using normal
Chalk credentials and the active environment. The server must have Model Gateway
and quota enforcement enabled. No new credentials or client construction are needed.

```python
import chalkcompute as cc

# Deploy creates this local pool first. Both resources persist after this program exits.
pool = cc.UsagePool("customer-support")
budget = cc.UsageBudget(
    usage_pool=pool,
    token_limit=1_000_000,
    period="monthly",
).deploy()
print(budget.id)

budget.token_limit = 2_000_000
budget.deploy()
budget.refresh()
print(budget.current_usage, budget.next_reset_at)

# In another process:
budget = cc.UsageBudget.from_id("your-budget-id")
budget.enabled = False
budget.deploy()
```

Provide exactly one of `usage_pool` or `api_key_id`. The latter is the router
key's record ID, not its secret token. Periods are `daily`, `weekly`, and
`monthly`; these are server-defined fixed UTC windows, not rolling durations.
Only one budget per owner and period is allowed. Creating a new definition does
not silently overwrite an existing budget. Use `from_id()` or
`UsageBudget.list(usage_pool=pool)` to manage existing budgets.
Listing returns a lazy generator: the request happens when iteration starts and
budget handles are constructed one at a time. The current server API returns all
matching budgets in one response, so this does not bound the underlying response
size. Prefer a pool/key filter for large environments; omitting it requests every
budget. Use `list(...)` to explicitly collect results. Close the generator if you
stop early (for example, with `contextlib.closing`); supplied clients remain
caller-owned. A local pool passed to
`list()` is looked up by name without creating anything.

`usage_pool` accepts either `cc.UsagePool("...")` or a deployed/loaded pool handle.
Construction stays local; deploying the budget deploys a new pool first with the
same authentication and environment. Already deployed pools are reused without
redeploying them. Use `UsagePool.from_name(...)` to adopt an existing pool; a new
pool definition does not silently adopt an existing name. An existing pool ID
string is also accepted as `usage_pool="your-pool-id"`.

For fluent configuration, put the budget directly on the pool:

```python
pool = (
    cc.UsagePool("analytics")
    .with_usage_budget(token_limit=1_000_000, period="monthly")
    .deploy()
)

# The standalone form also accepts an already deployed pool.
daily_budget = cc.UsageBudget(
    usage_pool=pool, token_limit=100_000, period="daily",
).deploy()
```

`with_usage_budget()` queues an addition for the next `pool.deploy()` and returns
the same pool. To edit an existing budget, use its handle instead of adding a
second budget for the same period.

`current_usage` is an admission counter, not an exact historical billing ledger.
It can be `None` when enforcement is disabled or unavailable. The next reset is
reported by the server. Disabling a budget is explicit; a zero token limit is
invalid. Ownership and period cannot be changed on an existing budget.

`deploy()` creates once, then updates the loaded handle's limit and enabled
flag using a field mask. Calls are bound to the handle's original environment.
Creates are not automatically retried after an ambiguous failure; reconcile
with `list()` before creating again. Updates remain last-writer-wins.

Every operation accepts an optional `client=cc.ConnectClient(...)`; supplied
clients remain caller-owned. `budget.delete()` removes only this budget. Other
periods and the owning usage pool or API key remain in place.
