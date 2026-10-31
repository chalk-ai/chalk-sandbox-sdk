# Router rate limits

Create persistent server-enforced policies using normal Chalk credentials and
the active environment. Model Gateway and quota enforcement must be enabled.
These policies apply to router API keys or usage pools; they are separate from
`cc.RateLimitPolicy`, which limits outbound function calls.

```python
import chalkcompute as cc

# The first policy deploy creates this local pool. The second reuses it.
pool = cc.UsagePool("customer-support")
requests = cc.RateLimit(
    usage_pool=pool,
    requests_per_minute=120,
    burst=20,
).deploy()
concurrency = cc.RateLimit(
    usage_pool=pool,
    concurrent_requests=8,
).deploy()
print(requests.id, concurrency.id)

# Change and save an existing policy.
requests.limit_value = 240
requests.deploy()

# In another process:
requests = cc.RateLimit.from_id("your-policy-id")
requests.enabled = False
requests.deploy()
```

Choose exactly one dimension per policy: `requests_per_minute`,
`tokens_per_minute`, or `concurrent_requests`. The constructor's selected value
is exposed as `limit_value` on the handle. To enforce multiple dimensions,
create multiple policies. Ownership and dimension are fixed on that handle.

Choose exactly one owner: `usage_pool` or `api_key_id`. An API-key ID is the
key's record ID, not its secret token. `connection_id` optionally restricts a
policy to a provider connection. `models` filters upstream model IDs within that
connection and requires `connection_id`.

`usage_pool` accepts `cc.UsagePool("...")`, a deployed/loaded pool handle, or an
existing pool ID string. Construction stays local; deploying a policy deploys a
new pool first using the same client and environment. Already deployed pools
are referenced by ID without redeployment. Use `UsagePool.from_name(...)` to
adopt an existing pool; a new definition does not silently adopt an existing name.
A local definition passed to `RateLimit.list(usage_pool=...)` is looked up by name
without creating anything.

For fluent configuration, add limits directly to the pool:

```python
pool = (
    cc.UsagePool("analytics")
    .with_rate_limit(requests_per_minute=120, burst=20)
    .with_rate_limit(tokens_per_minute=50_000)
    .with_usage_budget(token_limit=1_000_000, period="monthly")
    .deploy()
)
```

Each `with_rate_limit()` call queues an additional policy locally and returns the
same pool. The next `pool.deploy()` applies it, preserving existing policies.
Use a policy handle to edit an existing limit instead of adding another.

For request/token rates, `burst` is the bucket capacity; when omitted, the server
uses the rate value. Concurrency has no separate burst capacity. After loading a
policy, setting `burst = None` restores the server default. Clearing
`connection_id` and setting `models = ()` removes the filters. Updates use explicit
field masks, preserving false flags and allowing filters to be cleared.

A new definition creates a new policy, even if an identical policy exists.
Repeated `deploy()` on the same handle updates its ID. Save IDs for later use;
`RateLimit.list(usage_pool=pool)` reads all pages and filters policies
for that owner. Omit the filter to list all policies. After an ambiguous create
failure, reconcile through `list()` before trying again: creates are not retried
automatically. Updates remain last-writer-wins.

`refresh()` reloads server state. `delete()` removes only that policy. Resource
handles enforce their original Chalk environment. Every operation accepts an
optional `client=cc.ConnectClient(...)`; injected clients remain caller-owned.
