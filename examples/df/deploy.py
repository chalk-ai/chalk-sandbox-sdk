"""Deploy a chalkcompute function that uses chalkdf to compute a group-by sum.

Run this file to deploy the function:

    uv run python examples/df/deploy.py
"""

import logging
import sys
from pathlib import Path

import chalkcompute

# Add examples dir to path so we can import test_utils
sys.path.insert(0, str(Path(__file__).parent.parent))
from test_utils import make_function_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


@chalkcompute.function(
    name=make_function_name("df-groupby-sum"),
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
