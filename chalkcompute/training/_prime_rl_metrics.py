"""Reports prime-rl's per-step metrics to the Chalk training dashboard.

`chalkcompute.prime_rl` runs this file by path under a venv that has chalkpy but not
chalkcompute, so it imports nothing from the package. It follows the per-step lines prime-rl
logs:

    orchestrator: Step 7 |   3.4s | Reward 0.4774 | Trainable 128/128 | ... | Error 0.0% | ...
    trainer:      Step 7 |   6.7s | Loss 0.0048 | Entropy 0.6810 | Mismatch KL 0.0008 | Grad. Norm 0.7327 | LR 3.00e-06 | ...

"Mismatch KL" is the trainer/inference divergence, not a KL penalty, so it is not reported as `kl`.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
STEP = re.compile(r"\bStep (\d+) \|")
NUMBER = r"(-?[\d.]+(?:e[+-]?\d+)?)"
METRICS = {
    "reward": re.compile(rf"\| Reward {NUMBER}"),
    "loss": re.compile(rf"\| Loss {NUMBER}"),
    "entropy": re.compile(rf"\| Entropy {NUMBER}"),
    "grad_norm": re.compile(rf"\| Grad\. Norm {NUMBER}"),
    "learning_rate": re.compile(rf"\| LR {NUMBER}"),
}


def main(output_dir: Path, rl_log: Path, done: Path) -> None:
    offsets: dict[Path, int] = {}
    sent: set[tuple[str, int]] = set()
    while True:
        finished = done.exists()  # checked first so the last pass sees every line
        for path in [rl_log, *output_dir.glob("logs/**/*.log")]:
            for line in new_lines(path, offsets):
                line = ANSI.sub("", line)
                if not (match := STEP.search(line)):
                    continue
                step = int(match.group(1))
                # The trainer's lines appear both in rl.log and in its own log; send each once.
                values = {
                    name: float(found.group(1))
                    for name, pattern in METRICS.items()
                    if (name, step) not in sent and (found := pattern.search(line))
                }
                if values:
                    sent.update((name, step) for name in values)
                    report(step, values)
        if finished:
            return
        time.sleep(2)


def new_lines(path: Path, offsets: dict[Path, int]) -> list[str]:
    if not path.is_file():
        return []
    with path.open("rb") as f:
        f.seek(offsets.get(path, 0))
        data = f.read()
    complete = data.rfind(b"\n") + 1
    offsets[path] = offsets.get(path, 0) + complete
    return data[:complete].decode(errors="replace").splitlines()


def report(step: int, values: dict[str, float]) -> None:
    # Resolved in the forwarder's venv, which pins the chalkpy release that has it.
    from chalk.ml.chalk_train import log_metrics  # ty: ignore[unresolved-import]

    try:
        log_metrics(values, tags={"step": str(step), "split": "train"})
        print(
            f"[metrics] step {step} " + " ".join(f"{k}={v}" for k, v in values.items()),
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
        print(f"[metrics] could not report step {step}: {exc}", flush=True)


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:4]))
