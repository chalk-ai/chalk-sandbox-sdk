from __future__ import annotations

import json
import traceback

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from chalkcompute import FunctionError, RemoteExecutionError
from chalkcompute._function_error import (
    decode_remote_error,
    remote_error_from_exception,
)
from chalkcompute._remote_call_client import RemoteCallStatusError


def payload() -> str:
    return json.dumps(
        {
            "chalk_remote_error": 1,
            "exception": {
                "kind": "ValueError",
                "message": "bad input\\n is literal",
                "stacktrace": 'Traceback (most recent call last):\n  File "user.py", line 7, in score\n    raise ValueError("bad input")\nValueError: bad input\n',
                "internal_stacktrace": "full runtime diagnostics",
            },
        }
    )


@pytest.mark.parametrize("queued", [False, True])
def test_decodes_without_double_unescaping(queued: bool) -> None:
    wire = payload()
    if queued:
        wire = json.dumps({"exc_type": "RemoteFunctionError", "message": wire})
    error = decode_remote_error(wire, "score", call_id="call-123")
    assert isinstance(error, RemoteExecutionError)
    assert isinstance(error, FunctionError)
    assert error.message == "bad input\\n is literal"
    assert error.call_id == "call-123"
    assert error.internal_traceback == "full runtime diagnostics"
    assert '\n  File "user.py"' in str(error)


@pytest.mark.parametrize("call_id", [None, "call-123"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_decodes_proxy_error_with_optional_call_id(call_id, wrapped) -> None:
    message = "CallFunction RPC failed"
    wire = json.dumps({"exc_type": "ProxyError", "message": message})
    if wrapped:
        wire = json.dumps({"exc_type": "RemoteFunctionError", "message": wire})

    error = decode_remote_error(wire, "score", call_id=call_id)

    assert isinstance(error, RemoteExecutionError)
    assert error.function_name == "score"
    assert error.exception_type == "ProxyError"
    assert error.message == message
    assert error.call_id == call_id
    suffix = f" (call {call_id})" if call_id else ""
    assert str(error) == f"ProxyError: {message}{suffix}"


def test_proxy_envelope_preserves_versioned_runtime_error() -> None:
    wire = json.dumps({"exc_type": "ProxyError", "message": payload()})

    error = decode_remote_error(wire, "score", call_id="call-123")

    assert isinstance(error, RemoteExecutionError)
    assert error.exception_type == "ValueError"
    assert error.call_id == "call-123"
    assert error.internal_traceback == "full runtime diagnostics"


def test_versioned_proxy_error_preserves_trace_id() -> None:
    wire = json.loads(payload())
    wire["exception"]["kind"] = "ProxyError"
    wire["exception"]["stacktrace"] = ""
    wire["trace_id"] = "trace-123"

    error = decode_remote_error(wire, "score", call_id="call-123")

    assert isinstance(error, RemoteExecutionError)
    assert error.call_id == "call-123"
    assert error.trace_id == "trace-123"
    assert "Trace ID: trace-123" in str(error)


@pytest.mark.parametrize(
    "wire",
    [
        '{"exc_type": "ProxyError", "message": null}',
        '{"exc_type": "ProxyError", "message": 123}',
        '{"exc_type": "RuntimeError", "message": "legacy failure"}',
    ],
)
def test_unsupported_legacy_errors_keep_fallback(wire) -> None:
    assert decode_remote_error(wire, "score") is None


@pytest.mark.parametrize(
    "wire",
    [
        "ProxyError: bad input",
        "{}",
        "[]",
        '{"chalk_remote_error": 2}',
        '{"chalk_remote_error": 1, "exception": {}}',
    ],
)
def test_unknown_errors_are_not_reclassified(wire: str) -> None:
    assert decode_remote_error(wire, "score") is None


def test_native_and_connect_decode_identically() -> None:
    for error in [
        RemoteCallStatusError(2, payload()),
        ConnectError(Code.UNKNOWN, payload()),
    ]:
        decoded = remote_error_from_exception(error, "score")
        assert decoded is not None
        assert decoded.exception_type == "ValueError"
    assert remote_error_from_exception(RuntimeError(payload()), "score") is None


def test_ipython_uses_renderer_and_keeps_debug_traceback(monkeypatch) -> None:
    import IPython
    from IPython.core.interactiveshell import InteractiveShell

    error = decode_remote_error(payload(), "score")
    assert error is not None
    shell = InteractiveShell()
    monkeypatch.setattr(IPython, "get_ipython", lambda: shell)
    shell.user_ns["remote_error"] = error
    rendered: list[list[str]] = []
    shell._showtraceback = lambda etype, value, stb: rendered.append(stb)
    try:
        result = shell.run_cell("raise remote_error")
        assert result.error_in_exec is error
        output = "\n".join(rendered[-1])
        assert "raise remote_error" in output
        assert "Cell In[1], line 1" in output
        assert "/IPython/core/" not in output
        assert 'File "user.py", line 7, in score' in output
        assert "Remote traceback" in output
        assert "full runtime diagnostics" not in output
        assert traceback.extract_tb(error.__traceback__)
    finally:
        shell.history_manager.end_session()


def test_error_can_be_pickled_with_all_diagnostics() -> None:
    import pickle

    error = decode_remote_error(payload(), "score", call_id="call-123")
    restored = pickle.loads(pickle.dumps(error))
    assert isinstance(restored, RemoteExecutionError)
    assert restored.call_id == "call-123"
    assert restored.internal_traceback == error.internal_traceback
