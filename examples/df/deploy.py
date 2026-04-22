"""Deploy a chalkcompute function that uses chalkdf to compute a group-by sum.

Run this file to deploy the function:

    uv run python examples/df/deploy.py

Optional: pass --function-name to customize the deployed function name.
"""

import argparse
import logging

import chalkcompute

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy dataframe groupby-sum function")
    parser.add_argument(
        "--function-name",
        default="df-groupby-sum",
        help="Name for the deployed function",
    )
    args = parser.parse_args()

    @chalkcompute.function(
        name=args.function_name,
        image=chalkcompute.Image.debian_slim().pip_install(["chalkdf", "chalkpy[runtime]", "pyarrow"]),
    )
    def df_groupby_sum(n: int) -> int:
        from chalkdf import DataFrame

        _ = n  # unused — exists so the remote-call protocol has a row to dispatch on
        df = DataFrame.from_dict(
            {
                "category": ["a", "a", "b", "b", "b"],
                "value": [10, 20, 30, 40, 50],
            }
        )
        result = df.group_by("category").sum()
        table = result.to_arrow()
        # Sum of per-group totals: (10+20) + (30+40+50) = 150
        grand_total = sum(table.column("value").to_pylist())
        log.info("Grand total: %d", grand_total)
        return grand_total


if __name__ == "__main__":
    main()
