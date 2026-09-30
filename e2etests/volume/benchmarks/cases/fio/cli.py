"""Run the shared FIO workload on a selected volume provider."""

from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .providers import PROVIDERS
from .workload import (
    FioConfig,
    build_pyperf_results,
)
from e2etests.volume.benchmarks.harness import (
    positive_int,
    run_benchmark,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    defaults = FioConfig()
    parser.add_argument("--provider", choices=PROVIDERS, default="chalk")
    parser.add_argument("--volume")
    parser.add_argument("--keep-volume", action="store_true")
    parser.add_argument("--size", default=defaults.size)
    parser.add_argument("--jobs", type=positive_int, default=defaults.jobs)
    parser.add_argument(
        "--small-files", type=positive_int, default=defaults.small_files
    )
    parser.add_argument("--small-file-size", default=defaults.small_file_size)
    parser.add_argument("--direct", choices=("0", "1"), default=defaults.direct)
    parser.add_argument("--cpu", default=defaults.cpu)
    parser.add_argument("--memory", default=defaults.memory)
    parser.add_argument(
        "--timeout-secs", type=positive_int, default=defaults.timeout_secs
    )
    parser.add_argument("--provider-environment")
    parser.add_argument("--provider-region")
    parser.add_argument("--provider-cloud")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--local",
        action="store_true",
        help="Write JSON and a table locally without archiving to Chalk",
    )
    args = parser.parse_args(argv)
    if not PROVIDERS[args.provider].supports_placement and any(
        (args.provider_environment, args.provider_region, args.provider_cloud)
    ):
        parser.error(
            f"{args.provider} placement must be configured through its SDK environment"
        )
    if args.output is not None and args.output.suffix != ".json":
        parser.error("--output must have a .json extension")
    return args


def main(args: argparse.Namespace | None = None) -> dict[str, Any]:
    if args is None:
        args = parse_args()
    config = FioConfig(
        **{field.name: getattr(args, field.name) for field in fields(FioConfig)}
    )
    result = PROVIDERS[args.provider].run(
        config,
        volume_name=args.volume,
        keep=args.keep_volume,
        environment=args.provider_environment,
        region=args.provider_region,
        cloud=args.provider_cloud,
    )
    print(result.log, flush=True)
    return build_pyperf_results(result, config)


def cli(argv: list[str] | None = None) -> None:
    from .report import render_table

    args = parse_args(argv)
    local = args.local or args.provider != "chalk"
    output = args.output
    if local and output is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        output = Path("benchmark-results") / f"fio-{args.provider}-{timestamp}.json"

    def benchmark() -> dict[str, Any]:
        results = main(args)
        table = render_table(results)
        print(table, flush=True)
        if local and output is not None:
            output.with_suffix(".md").write_text(table + "\n")
        return results

    run_benchmark("volume-fio", benchmark, output=output, archive=not local)
    if output is not None:
        print(f"Results: {output}", flush=True)
