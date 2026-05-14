# ChalkDF Projection Example

Deploys a container via `chalk container run` that builds a `chalkdf.DataFrame`
from a `pyarrow.Table` and runs a `.select(...).run()` projection.

## Files

| File | Purpose |
|------|---------|
| `image.py` | Declares the container image: `python:3.14-slim-trixie` + `chalkdf`, `pyarrow`, `chalkpy` |
| `run.py` | The script that executes inside the container |
| `deploy.py` | Orchestrates the full lifecycle: stop old container, deploy, wait, fetch logs |

## What it does

The container script creates a 3-row PyArrow table, wraps it in a
`chalkdf.DataFrame`, then projects two columns:

```python
table = pa.table({
    "user_id": [1, 2, 3],
    "distance_km": [0.5, 1.2, 3.7],
    "active": [True, False, True],
})
df = DataFrame(table)
projected = df.select("user_id", "distance_km").run()
```

## Prerequisites

- `chalk` CLI installed and on PATH
- A `.env` file at the repo root with `CHALK_CLIENT_ID`, `CHALK_CLIENT_SECRET`,
  `CHALK_ENVIRONMENT_ID`, and `CHALK_API_SERVER`

## Usage

```bash
python3 deploy.py
```

This will:
1. Stop any previous `chalkdf-projection-example` container
2. Run `chalk container run` with `python:3.14-slim-trixie`, installing
   `chalkdf`, `pyarrow`, and `chalkpy` at startup
3. Wait 60s for pip install and script execution
4. Fetch logs via `chalk logs --query pod_name:chalk-container-chalkdf-projection-example`

## Expected output

```
Input DataFrame:
DataFrame(materialized 3 rows x 3 columns)
+---------+-------------+--------+
| user_id | distance_km | active |
+---------+-------------+--------+
| 1       | 0.5         | True   |
| 2       | 1.2         | False  |
| 3       | 3.7         | True   |
+---------+-------------+--------+

Projected DataFrame (user_id, distance_km):
DataFrame(materialized 3 rows x 2 columns)
+---------+-------------+
| user_id | distance_km |
+---------+-------------+
| 1       | 0.5         |
| 2       | 1.2         |
| 3       | 3.7         |
+---------+-------------+
```
