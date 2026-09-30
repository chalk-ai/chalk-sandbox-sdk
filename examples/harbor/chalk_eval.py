#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute", "pyarrow"]
# ///
"""Run a Harbor task set as a Chalk evaluation: one dataset row per Harbor task.

    ./chalk_eval.py [task-name ...]

The task function runs one Harbor trial per row. The trial's environment is itself a
Chalk sandbox (via chalk_harbor), which the function creates with its platform identity,
so nothing here needs credentials beyond the caller's own. The scorer turns the trial's
verifier reward into the evaluation score and keeps timings and failures as metadata.

Rows run concurrently up to ``concurrency``; each trial's sandbox is its own workload, so
the function replica only orchestrates.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import chalkcompute
from chalkcompute import EvaluationScorerResult, Image

HERE = Path(__file__).resolve().parent
AGENT = "oracle"

IMAGE = (
    Image.debian_slim("3.13")
    .pip_install(["harbor", "chalkcompute", "dockerfile-parse"])
    .add_local_dir(str(HERE / "chalk_harbor"), "/opt/harbor/chalk_harbor")
    .add_local_dir(str(HERE / "smoke"), "/opt/harbor/tasks")
)


@chalkcompute.function(
    name="harbor-trial",
    image=IMAGE,
    chalk_identity=True,
    concurrency=8,
    cpu="1",
    memory="2Gi",
)
def harbor_trial(task_name: str) -> str:
    import os
    import subprocess
    import uuid

    job = f"eval-{uuid.uuid4().hex[:8]}"
    command = [
        "harbor", "run", "-p", "/opt/harbor/tasks", "-i", task_name, "-a", AGENT,
        "-e", "chalk_harbor:ChalkSandboxEnvironment", "-o", "/tmp/harbor-jobs",
        "--job-name", job, "--yes",
    ]  # fmt: skip
    env = {**os.environ, "PYTHONPATH": "/opt/harbor"}
    started = time.time()
    proc = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=1800, check=False
    )
    trials = sorted(Path("/tmp/harbor-jobs", job).glob("*/result.json"))
    if not trials:
        return json.dumps(
            {
                "task": task_name,
                "error": (proc.stderr or proc.stdout)[-2000:],
                "exit_code": proc.returncode,
            }
        )
    result = json.loads(trials[0].read_text())
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    exception = result.get("exception_info") or {}
    return json.dumps(
        {
            "task": task_name,
            "trial": result.get("trial_name"),
            "reward": rewards.get("reward"),
            "rewards": rewards,
            "exception": exception.get("exception_type"),
            "exception_message": (exception.get("exception_message") or "")[:1000],
            "wall_seconds": round(time.time() - started, 1),
        }
    )


@chalkcompute.function(name="harbor-reward")
def harbor_reward(output: str) -> EvaluationScorerResult:
    trial = json.loads(output)
    reward = trial.get("reward")
    return EvaluationScorerResult(
        score=float(reward) if reward is not None else 0.0, metadata=trial
    )


def _wait(run_id: str, timeout: float) -> chalkcompute.EvaluationRun:
    # A run only advances when it is read, and finalizing inside that read can outlast one
    # RPC's deadline; retry the read rather than treating a timeout as a failed run.
    deadline = time.monotonic() + timeout
    while True:
        try:
            run = chalkcompute.EvaluationRun.from_id(run_id)
            if run.is_terminal:
                return run
        except chalkcompute.EvaluationError as exc:
            print(f"  (poll retry: {exc})", flush=True)
        if time.monotonic() > deadline:
            raise TimeoutError(f"evaluation run {run_id} not finished after {timeout}s")
        time.sleep(10)


def main(argv: list[str]) -> int:
    tasks = argv or sorted(
        p.name for p in (HERE / "smoke").iterdir() if (p / "task.toml").exists()
    )
    dataset = chalkcompute.DatasetClient().upload("harbor-smoke", {"task_name": tasks})
    evaluation = chalkcompute.EvaluationClient().create(
        f"harbor-smoke-{int(time.time())}",
        dataset=dataset,
        task=harbor_trial,
        scorers=[harbor_reward],
    )
    run = evaluation.run()
    print(
        f"evaluation {evaluation.id} run {run.id}: {len(tasks)} harbor tasks",
        flush=True,
    )
    run = _wait(run.id, timeout=3600)
    print("status:", run.status)
    if run.result_dataset is None:
        return 1
    table = chalkcompute.DatasetClient().read(run.result_dataset)
    value = next(c for c in table.column_names if c.endswith("_value"))
    for row in table.to_pylist():
        print(f"  {row['task_name']:<16} reward={row[value]}")
    return 0 if str(run.status).endswith("SUCCEEDED") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
