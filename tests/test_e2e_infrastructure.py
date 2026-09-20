from __future__ import annotations

from unittest.mock import Mock

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from chalkcompute import ContainerError
from chalkcompute._container import ExecResult
from e2etests import _infrastructure as infra


def _transport_error(code: Code) -> ContainerError:
    error = ContainerError("Container session RPC failed: upstream request timeout")
    error.__cause__ = ConnectError(code, "upstream request timeout")
    return error


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(infra.time, "sleep", lambda delay: None)


@pytest.mark.parametrize("code", [Code.UNAVAILABLE, Code.DEADLINE_EXCEEDED])
def test_read_only_probe_recovers_from_transport_error(code):
    expected = ExecResult(stdout=b"hello\n", stderr=b"", exit_code=0)
    container = Mock()
    container.exec.side_effect = [_transport_error(code), expected]
    assert infra.exec_read_only(container, "echo", "hello", timeout_secs=5) is expected
    assert container.exec.call_count == 2
    container.exec.assert_called_with("echo", "hello", timeout_secs=5)


def test_read_only_probe_recovers_from_kubelet_tls_failure():
    failure = ExecResult(
        stdout=b"",
        stderr=b'exec startup failed: Internal error occurred: error sending request: Post "https://kubelet/exec": remote error: tls: internal error',
        exit_code=1,
    )
    success = ExecResult(stdout=b"hello\n", stderr=b"", exit_code=0)
    container = Mock()
    container.exec.side_effect = [failure, success]
    assert infra.exec_read_only(container, "echo", "hello") is success
    assert container.exec.call_count == 2


@pytest.mark.parametrize(
    "result",
    [
        ExecResult(stdout=b"403", stderr=b"", exit_code=0),
        ExecResult(stdout=b"", stderr=b"curl: connection refused", exit_code=7),
        ExecResult(stdout=b"", stderr=b"curl: operation timed out", exit_code=28),
        ExecResult(
            stdout=b"", stderr=b"remote error: tls: internal error", exit_code=1
        ),
        ExecResult(
            stdout=b"", stderr=b"exec startup failed: permission denied", exit_code=1
        ),
        ExecResult(
            stdout=b"partial",
            stderr=b"exec startup failed: remote error: tls: internal error",
            exit_code=1,
        ),
    ],
)
def test_real_command_and_policy_failures_are_not_retried(result):
    container = Mock()
    container.exec.return_value = result
    assert infra.exec_read_only(container, "curl", "http://fixture") is result
    assert container.exec.call_count == 1


@pytest.mark.parametrize("code", [Code.PERMISSION_DENIED, Code.NOT_FOUND, None])
def test_nontransient_container_errors_are_not_retried(code):
    error = _transport_error(code) if code is not None else ContainerError("failed")
    container = Mock()
    container.exec.side_effect = error
    with pytest.raises(ContainerError) as raised:
        infra.exec_read_only(container, "echo", "hello")
    assert raised.value is error
    assert container.exec.call_count == 1


def test_persistent_transport_error_still_fails():
    error = _transport_error(Code.UNAVAILABLE)
    container = Mock()
    container.exec.side_effect = error
    with pytest.raises(ContainerError) as raised:
        infra.exec_read_only(container, "echo", "hello")
    assert raised.value is error
    assert container.exec.call_count == 3


def test_persistent_startup_failure_is_returned_for_the_test_to_assert():
    result = ExecResult(
        stdout=b"",
        stderr=b"exec startup failed: remote error: tls: internal error",
        exit_code=1,
    )
    container = Mock()
    container.exec.return_value = result
    assert infra.exec_read_only(container, "echo", "hello") is result
    assert container.exec.call_count == 3


def test_safe_response_retry_is_bounded_and_preserves_assertion_failures():
    call = Mock(side_effect=[503, 200])
    assert (
        infra.retry_safe_operation(call, retry_response=lambda status: status == 503)
        == 200
    )
    persistent = Mock(return_value=503)
    assert (
        infra.retry_safe_operation(
            persistent, retry_response=lambda status: status == 503
        )
        == 503
    )
    assert persistent.call_count == 3
    assertion = Mock(side_effect=AssertionError("wrong result"))
    with pytest.raises(AssertionError, match="wrong result"):
        infra.retry_safe_operation(assertion, retry_response=lambda _: True)
    assert assertion.call_count == 1
