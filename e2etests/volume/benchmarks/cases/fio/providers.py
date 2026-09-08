"""Provider registration and optional benchmark dependencies."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable

from .chalk import run_chalk
from .workload import FioConfig, FioResult

ProviderRunner = Callable[..., FioResult]


@dataclass(frozen=True)
class Provider:
    runner: ProviderRunner
    dependency_group: str = ""
    supports_placement: bool = False

    def run(self, config: FioConfig, **options: Any) -> FioResult:
        return self.runner(config, **options)


PROVIDERS = {
    "chalk": Provider(run_chalk),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=PROVIDERS)
    args = parser.parse_args()
    print(PROVIDERS[args.provider].dependency_group)


if __name__ == "__main__":
    main()
