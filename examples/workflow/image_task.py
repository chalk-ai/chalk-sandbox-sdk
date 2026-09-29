# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "chalkcompute",
#   "chalkpy[workflows]",
# ]
# ///
"""Run one task of a workflow on an image of its own.

    uv run examples/workflow/image_task.py

``cc.run_workflow`` deploys two scale-to-zero groups: ``wf-image-task`` runs the
workflow and ``count``, and ``wf-image-task-<image hash>`` runs ``mean`` on an
image with numpy installed. The workflow calls ``mean`` as a child workflow on
the second group's queue, so that group scales up only while a ``mean`` call is
open. Tasks with equal images share a group.
"""

from chalk.workflows import task, workflow

import chalkcompute as cc

numpy_image = cc.Image.debian_slim("3.12").pip_install(["numpy"])


@task
def count(n: int) -> list[int]:
    return list(range(n))


@task(image=numpy_image, retries=2)
def mean(values: list[int]) -> float:
    import numpy as np

    return float(np.mean(values))


@workflow
async def average(n: int) -> float:
    return await mean(await count(n))


# The workers import this file too, under its module name rather than
# "__main__", so this block runs only on your machine.
if __name__ == "__main__":
    print(cc.run_workflow(average, {"n": 10}))
