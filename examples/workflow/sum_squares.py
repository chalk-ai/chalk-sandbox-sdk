# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "chalkcompute",
#   "chalkpy[workflows]",
# ]
# ///
"""Run a durable workflow on autoscaled compute with one ``uv run``.

    uv run examples/workflow/sum_squares.py

``cc.run_workflow`` bundles this file into a scaling group named
``wf-sum-squares``, starts ``sum_squares`` on that group's own task queue, and
prints the result. The environment's workflow orchestrator drives the run. The
group sits at zero replicas until a workflow is open on its queue, scales up to
serve it, and returns to zero once no workflow has been open for the group's
shutdown delay. So the only lasting resource is the scaling group, and it costs
nothing while idle. Delete it from the dashboard, or with
``cc.ScalingGroup.from_name("wf-sum-squares").delete()``.

Each ``@task`` call runs as its own retried activity on the worker, so a replica
lost mid-run costs one task, not the workflow.

Requires a Chalk environment with a workflow orchestrator, and credentials from
``chalk login`` (or ``CHALK_CLIENT_ID`` / ``CHALK_CLIENT_SECRET``).
"""

from chalk.workflows import task, workflow

import chalkcompute as cc


@task
def square(x: int) -> int:
    return x * x


@workflow
async def sum_squares(n: int) -> int:
    total = 0
    for i in range(n):
        total += await square(i)
    return total


# The worker imports this file too, under its module name rather than
# "__main__", so this block runs only on your machine.
if __name__ == "__main__":
    print(cc.run_workflow(sum_squares, {"n": 10}))
