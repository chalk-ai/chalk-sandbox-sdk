# CHA-9498 manual test — deleting a scaling group deletes its function

Tests the reverse cascade end-to-end against your local stack
(`localhost:3000` → local `go-api-server` → env `avi-chalk-ai`).

## Why not the plain SDK?

Your local env has **no container registry configured**, so the normal SDK path
(`@chalkcompute.function`, `Image.pip_install`, `add_local_file`) fails at the
image-build step (`GetOrBuildCustomImage ... container registry is not configured`).
That's an environment limitation, unrelated to the fix.

`create_test_resources.py` sidesteps it: it calls the same Connect RPCs the SDK
uses (`CreateExternalFunctionVersion`, `CreateScalingGroup`) but with a **prebuilt
public image** (`traefik/whoami`), so nothing is built. The resulting DB records
are exactly what the delete cascade acts on — the containers don't need to be
real functions.

## Step 0 — create the fixtures

```bash
cd /Users/avi/chalk-code/chalk-sandbox-sdk/examples/cha9498-test
uv run create_test_resources.py
```

Creates three independent resources (auth + host come from `~/.chalk.yml`):

| Name | What it is | Scenario |
|---|---|---|
| `cha9498-reverse-cascade` | external function + backing SG (`function_name` metadata) | 1 — delete SG → function disappears (the fix) |
| `cha9498-forward` | external function + backing SG | 2 — delete function → SG disappears (no regression) |
| `cha9498-standalone-sg` | standalone SG, no function | 3 — delete SG → no function touched, no warning |

They deploy `traefik/whoami` to your cluster and may sit in `Pending`/crashloop —
that's fine, the delete test only needs the records to exist.

Confirm in the dashboard (env `avi-chalk-ai`): the **Scaling groups** page shows
all three; the **External functions** page shows the two functions.

## Step 1 — Scenario 3: standalone SG, no side effects

1. Scaling groups page → **Delete** `cha9498-standalone-sg`.
2. ✅ The modal does **not** say "It also deletes the associated function …".
3. Confirm → ✅ it's gone; both functions and their SGs remain.

## Step 2 — Scenario 1: deleting a function-backed SG deletes the function (THE FIX)

1. Scaling groups page → **Delete** `cha9498-reverse-cascade`.
2. ✅ The modal reads: "It also deletes the associated function `cha9498-reverse-cascade`."
3. Confirm → ✅ External functions page: `cha9498-reverse-cascade` is **gone**.
4. ✅ `cha9498-forward` (function + SG) is untouched — no collateral.

## Step 3 — Scenario 2: deleting a function still tears down its SG (no regression)

Delete the `cha9498-forward` function from the External functions page (or the
`chalk` CLI / SDK). Then:
1. ✅ `cha9498-forward` is gone from the External functions page.
2. ✅ Its backing scaling group is gone from the Scaling groups page.

## Optional — ground-truth check in the metadata DB

```sql
select function_name, version, deleted_at from external_function_versions
  where function_name like 'cha9498%' order by function_name, version;
select name, status, deleted_at from scaling_groups where name like 'cha9498%';
```

Soft-deleted rows are retained with `deleted_at` set; deleted scaling groups show `status = 'Deleted'`.

## Cleanup

Delete anything left over from the dashboard, or re-run scenarios. To remove a
standalone SG from the CLI: `chalk scaling-group delete --name=cha9498-standalone-sg`.
Deleting a function-backed SG (Scenario 1/2) already removes both sides.
