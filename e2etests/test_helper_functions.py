"""End-to-end test for helper functions in other files.

One function combining all three helper source kinds:
  - Package auto-include: helper in a sibling module of the function's package
  - Image.add_local_python_source: a loose module and a second package

Deploys the way a customer would: by importing the function's module
(``import fraud.app`` fires the decorator's eager deploy).
"""

import sys
from pathlib import Path

import chalkcompute
from e2etests.conftest import needs_staging

_EXAMPLE_DIR = str(
    Path(__file__).resolve().parent.parent / "examples" / "helper-functions"
)


@needs_staging
class TestHelperFunctions:
    @classmethod
    def setup_class(cls):
        sys.path.insert(0, _EXAMPLE_DIR)
        try:
            import fraud.app  # noqa: F401  (deploys ex-fraud-fn at import)
        finally:
            sys.path.remove(_EXAMPLE_DIR)

    def test_fraud_fn(self):
        """score(clean(double(3))) == (3 * 2 + 1) * 10."""
        fn = chalkcompute.RemoteFunction.from_name("ex-fraud-fn")
        assert fn.remote(3) == 70
