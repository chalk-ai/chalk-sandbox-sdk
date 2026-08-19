"""End-to-end tests for the Chalk MCP eval_python tool."""

from __future__ import annotations

import json
from typing import Any

from pyqwest import FullResponse, SyncClient

from e2etests.conftest import CHALK_API_SERVER, needs_staging


def _read_jsonrpc_response(response: FullResponse) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text().splitlines():
            if line.startswith("data: "):
                return json.loads(line.removeprefix("data: "))
        raise AssertionError(f"expected event-stream data line, got: {response.text()}")
    return response.json()


@needs_staging
def test_mcp_eval_python(auth_headers: dict[str, str]) -> None:
    endpoint = f"{CHALK_API_SERVER.rstrip('/')}/v1/mcp/sse"
    mcp_headers = {
        **auth_headers,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    client = SyncClient()
    init_response = client.post(
        endpoint,
        content={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "mcp-eval-python-e2e",
                    "version": "1.0.0",
                },
            },
        },
        headers=mcp_headers,
    )
    assert init_response.status == 200, init_response.text()
    session_id = init_response.headers.get("mcp-session-id")
    assert session_id

    def assert_eval_python_output(
        code: str, expected_stdout: str, expected_stderr: str
    ) -> None:
        call_response = client.post(
            endpoint,
            content={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "eval_python",
                    "arguments": {"code": code},
                },
            },
            headers={
                **mcp_headers,
                "Mcp-Session-Id": session_id,
            },
        )
        assert call_response.status == 200, call_response.text()

        rpc = _read_jsonrpc_response(call_response)
        result = rpc.get("result")
        assert isinstance(result, dict), rpc
        assert result.get("isError") is not True, result

        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            content = result.get("content")
            assert isinstance(content, list), result
            assert len(content) == 1, result
            text_content = content[0]
            assert isinstance(text_content, dict), text_content
            text = text_content.get("text")
            assert isinstance(text, str), text_content
            structured = json.loads(text)

        assert structured["stdout"] == expected_stdout
        assert structured["stderr"] == expected_stderr
        assert structured["exit_code"] == 0
        assert structured["timed_out"] is False

    assert_eval_python_output("print(sum(range(10)))", "45\n", "")
    assert_eval_python_output(
        "import sys\nprint('visible stderr', file=sys.stderr)",
        "",
        "visible stderr\n",
    )
    assert_eval_python_output("sum(range(10))", "", "")
