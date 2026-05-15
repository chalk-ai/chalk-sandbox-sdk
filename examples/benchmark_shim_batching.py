"""Small local benchmark for shim batching vs non-batching.

This measures the Python-side remote-call shim path only. It compares:

1. A single non-batched caller via ``process_batches``
2. Many callers processed one-by-one via ``process_batches``
3. The same callers coalesced into one batched invocation via
   ``process_batches_coalesced``

Usage:
    uv run python examples/benchmark_shim_batching.py
    uv run python examples/benchmark_shim_batching.py --mode async --callers 32
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import types
from dataclasses import dataclass

import pyarrow as pa
from chalk_remote_call.arrow_utils import decode_ipc_stream, encode_record_batch
from chalk_remote_call.servicer import process_batches, process_batches_coalesced

from chalkcompute._function import _generate_handler_shim


@dataclass(frozen=True)
class ScenarioStats:
    name: str
    callers: int
    rows: int
    median_s: float
    p95_s: float

    @property
    def callers_per_s(self) -> float:
        return self.callers / self.median_s

    @property
    def rows_per_s(self) -> float:
        return self.rows / self.median_s

    @property
    def amortized_ms_per_caller(self) -> float:
        return (self.median_s * 1000.0) / self.callers


def _install_user_module() -> None:
    mod = types.ModuleType("user_module")

    def add(x: int, y: int, z: int) -> int:
        return x + y + z

    async def add_async(x: int, y: int, z: int) -> int:
        return x + y + z

    mod.add = add
    mod.add_async = add_async
    sys.modules["user_module"] = mod


def _load_handler(source: str) -> types.FunctionType:
    namespace: dict[str, object] = {}
    exec(compile(source, "<bench-shim>", "exec"), namespace)
    return namespace["handler"]  # type: ignore[return-value]


def _encode_event(event: dict[str, pa.Array]) -> bytes:
    return encode_record_batch(
        pa.record_batch(list(event.values()), names=list(event.keys()))
    )


def _decode_results(payloads: list[bytes]) -> list[list[object]]:
    decoded: list[list[object]] = []
    for payload in payloads:
        for batch in decode_ipc_stream(payload):
            decoded.append(batch.column("result").to_pylist())
    return decoded


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        raise ValueError("samples must be non-empty")
    ordered = sorted(samples)
    index = int(round((len(ordered) - 1) * percentile))
    return ordered[index]


def _make_events(callers: int, rows_per_caller: int) -> list[dict[str, pa.Array]]:
    events: list[dict[str, pa.Array]] = []
    for caller_index in range(callers):
        base = caller_index * rows_per_caller
        xs = list(range(base, base + rows_per_caller))
        ys = list(range(rows_per_caller))
        zs = [caller_index] * rows_per_caller
        events.append(
            {
                "x": pa.array(xs),
                "y": pa.array(ys),
                "z": pa.array(zs),
            }
        )
    return events


def _measure(rounds: int, fn) -> list[float]:
    samples: list[float] = []
    for _ in range(rounds):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


def _run_scenario(
    *,
    name: str,
    callers: int,
    rows: int,
    warmup: int,
    rounds: int,
    fn,
) -> ScenarioStats:
    for _ in range(warmup):
        fn()
    samples = _measure(rounds, fn)
    return ScenarioStats(
        name=name,
        callers=callers,
        rows=rows,
        median_s=statistics.median(samples),
        p95_s=_percentile(samples, 0.95),
    )


def _verify_equivalent_outputs(
    single_handler,
    batched_handler,
    encoded_events: list[bytes],
) -> None:
    single_outputs = [
        _decode_results(process_batches(ipc_bytes, single_handler, None, {}))
        for ipc_bytes in encoded_events
    ]
    batched_outputs = _decode_results(
        process_batches_coalesced(
            encoded_events,
            batched_handler,
            None,
            [{} for _ in encoded_events],
        )
    )
    if single_outputs != [[values] for values in batched_outputs]:
        raise AssertionError(
            "batched and non-batched outputs diverged: "
            f"{single_outputs!r} != {batched_outputs!r}"
        )


def _benchmark_mode(
    *,
    mode: str,
    callers: int,
    rows_per_caller: int,
    warmup: int,
    rounds: int,
) -> list[ScenarioStats]:
    func_name = "add" if mode == "sync" else "add_async"
    single_handler = _load_handler(_generate_handler_shim(func_name))
    batched_handler = _load_handler(_generate_handler_shim(func_name, batched=True))

    events = _make_events(callers, rows_per_caller)
    encoded_events = [_encode_event(event) for event in events]

    _verify_equivalent_outputs(single_handler, batched_handler, encoded_events)

    single_call = lambda: process_batches(encoded_events[0], single_handler, None, {})
    serial_workload = lambda: [
        process_batches(ipc_bytes, single_handler, None, {}) for ipc_bytes in encoded_events
    ]
    batched_workload = lambda: process_batches_coalesced(
        encoded_events,
        batched_handler,
        None,
        [{} for _ in encoded_events],
    )

    return [
        _run_scenario(
            name="single non-batched call",
            callers=1,
            rows=rows_per_caller,
            warmup=warmup,
            rounds=rounds,
            fn=single_call,
        ),
        _run_scenario(
            name="serial non-batched workload",
            callers=callers,
            rows=callers * rows_per_caller,
            warmup=warmup,
            rounds=rounds,
            fn=serial_workload,
        ),
        _run_scenario(
            name="coalesced batched workload",
            callers=callers,
            rows=callers * rows_per_caller,
            warmup=warmup,
            rounds=rounds,
            fn=batched_workload,
        ),
    ]


def _print_results(mode: str, stats: list[ScenarioStats]) -> None:
    print()
    print(f"Mode: {mode}")
    print(
        f"{'Scenario':30} {'median ms':>10} {'p95 ms':>10} "
        f"{'callers/s':>12} {'rows/s':>12} {'ms/caller':>11}"
    )
    for item in stats:
        print(
            f"{item.name:30} "
            f"{item.median_s * 1000:10.3f} "
            f"{item.p95_s * 1000:10.3f} "
            f"{item.callers_per_s:12.1f} "
            f"{item.rows_per_s:12.1f} "
            f"{item.amortized_ms_per_caller:11.3f}"
        )

    serial = next(item for item in stats if item.name == "serial non-batched workload")
    batched = next(item for item in stats if item.name == "coalesced batched workload")
    print(
        "Batching summary: "
        f"{batched.callers_per_s / serial.callers_per_s:.2f}x caller throughput, "
        f"{batched.rows_per_s / serial.rows_per_s:.2f}x row throughput, "
        f"{serial.median_s / batched.median_s:.2f}x workload latency improvement"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("sync", "async", "both"),
        default="both",
        help="Which function style to benchmark.",
    )
    parser.add_argument(
        "--callers",
        type=int,
        default=16,
        help="Number of callers in the workload/coalesced batch.",
    )
    parser.add_argument(
        "--rows-per-caller",
        type=int,
        default=128,
        help="Number of rows in each caller event.",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=3,
        help="Warmup iterations before timing each scenario.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=15,
        help="Timed iterations per scenario.",
    )
    args = parser.parse_args()

    _install_user_module()

    modes = ("sync", "async") if args.mode == "both" else (args.mode,)
    print(
        "Benchmarking local shim overhead only "
        f"(callers={args.callers}, rows/caller={args.rows_per_caller}, "
        f"warmup={args.warmup}, rounds={args.rounds})"
    )
    for mode in modes:
        stats = _benchmark_mode(
            mode=mode,
            callers=args.callers,
            rows_per_caller=args.rows_per_caller,
            warmup=args.warmup,
            rounds=args.rounds,
        )
        _print_results(mode, stats)


if __name__ == "__main__":
    main()
