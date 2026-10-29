"""Opt-in native SDK tests against the authenticated Redis-backed FNQ harness.

Run with FNQ_CANCEL_E2E_ENDPOINT, FNQ_CANCEL_E2E_TOKEN, and
FNQ_CANCEL_E2E_ENVIRONMENT from chalk-remote-call-python's local test server.
The target must have a Stream-backed ``handler`` queue without a consumer.
"""

import asyncio
import os

import pytest

from chalkcompute import CallCancelledError, RemoteCallTerminationStatus
from chalkcompute._connect import ConnectClient
from chalkcompute._function import RemoteFunction


class _LocalDataplane(ConnectClient):
    def __init__(self, endpoint: str, token: str, environment: str) -> None:
        super().__init__()
        self.endpoint = endpoint
        self._access_token = token
        self._env_id = environment

    def _ensure_auth(self) -> None:
        pass

    def dataplane_api_server_target(self) -> tuple[str, bool]:
        return self.endpoint, False


@pytest.fixture
def function():
    endpoint = os.environ.get("FNQ_CANCEL_E2E_ENDPOINT")
    if not endpoint:
        pytest.skip("FNQ_CANCEL_E2E_ENDPOINT is not set")

    def handler(x: int) -> int:
        return x

    function = RemoteFunction(handler)
    # Bind a deployed function to the isolated harness's queue and identity;
    # enqueue, cancel, and poll still use the real native transport and server.
    function._web_url = f"http://{endpoint}"
    function._client = _LocalDataplane(
        endpoint,
        os.environ["FNQ_CANCEL_E2E_TOKEN"],
        os.environ["FNQ_CANCEL_E2E_ENVIRONMENT"],
    )
    yield function
    function._drop_remote_call_clients()


@pytest.mark.parametrize("terminate", [False, True])
def test_native_function_handle_cancellation(function, terminate):
    handle = function.defer(21)
    result = handle.cancel(terminate_containers=terminate)
    assert result.is_cancelled
    assert result.termination_status == (
        RemoteCallTerminationStatus.TERMINATED
        if terminate
        else RemoteCallTerminationStatus.NOT_REQUESTED
    )
    assert handle.cancel(terminate_containers=terminate) == result
    with pytest.raises(
        RuntimeError, match="termination policy is already fixed"
    ) as conflict:
        handle.cancel(terminate_containers=not terminate)
    assert conflict.value.args[0] == 9  # gRPC FAILED_PRECONDITION
    with pytest.raises(CallCancelledError):
        handle.get(timeout=5)
    with pytest.raises(CallCancelledError):
        next(handle.stream(timeout=5))

    client = function._ensure_remote_call_client(queue=True)
    poll = client.poll_next(handle.call_id)
    assert poll.is_cancelled
    assert not poll.chunks
    with pytest.raises(CallCancelledError):
        client.get_result_chunks(handle.call_id)


def test_native_async_function_handle_cancellation(function):
    function._is_async = True
    handle = function.defer(21)

    async def cancel_and_get():
        assert (await handle.cancel()).is_cancelled
        with pytest.raises(CallCancelledError):
            await handle.get(timeout=5)
        with pytest.raises(CallCancelledError):
            await anext(handle.stream(timeout=5))

    asyncio.run(cancel_and_get())
