from collections.abc import Iterator

import pytest

from chalkcompute import _connect


@pytest.fixture(autouse=True)
def _isolate_default_connect_client() -> Iterator[None]:
    """Keep the process-wide SDK client from carrying state between unit tests."""
    _connect._close_default_connect_client()
    yield
    _connect._close_default_connect_client()
