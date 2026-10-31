# Usage budgets

Create token budgets for an existing router usage pool or API key using normal
Chalk credentials and the active environment. The server must have Model Gateway
and quota enforcement enabled. No new credentials or client construction are needed.

```python
import chalkcompute as cc

# Use an existing pool ID. This budget persists after the program exits.
budget = cc.UsageBudget(
    usage_pool_id="your-pool-id",
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

Provide exactly one of `usage_pool_id` or `api_key_id`. The latter is the router
key's record ID, not its secret token. Periods are `daily`, `weekly`, and
`monthly`; these are server-defined fixed UTC windows, not rolling durations.
Only one budget per owner and period is allowed. Creating a new definition does
not silently overwrite an existing budget. Use `from_id()` or
`UsageBudget.list(usage_pool_id="your-pool-id")` to manage existing budgets.
Omit the filter to list all budgets in the environment.

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
