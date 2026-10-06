from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import chalkcompute
import pytest


@pytest.mark.parametrize(
    ("image_override", "command_override", "expected_output"),
    [
        (None, None, "sandbox-exec-ok\n"),
        (None, ["echo", "custom-command"], "custom-command\n"),
        ("alpine:3.20", ["echo", "sandbox-exec-ok"], "sandbox-exec-ok\n"),
        ("python:3.13-slim", ["python", "--version"], "Python 3.13.0\n"),
    ],
)
def test_function_sandbox_image_override(
    monkeypatch: pytest.MonkeyPatch,
    image_override: str | None,
    command_override: list[str] | None,
    expected_output: str,
) -> None:
    seen: dict[str, Any] = {}
    if image_override is None:
        monkeypatch.delenv("CHALK_SANDBOX_E2E_IMAGE", raising=False)
    else:
        monkeypatch.setenv("CHALK_SANDBOX_E2E_IMAGE", image_override)
    if command_override is None:
        monkeypatch.delenv("CHALK_SANDBOX_E2E_COMMAND", raising=False)
    else:
        monkeypatch.setenv("CHALK_SANDBOX_E2E_COMMAND", json.dumps(command_override))

    def capture_function(**kwargs: Any) -> Any:
        seen["function_env"] = kwargs["env"]
        return lambda fn: fn

    class FakeSandbox:
        id = "sandbox-1"

        def __init__(self, *, image: Any) -> None:
            seen["sandbox_image"] = image

        def run(self) -> FakeSandbox:
            return self

        def exec(self, *command: str) -> SimpleNamespace:
            seen.setdefault("commands", []).append(command)
            if command[0] == "python":
                output = "Python 3.13.0\n"
            else:
                output = f"{command[1]}\n"
            return SimpleNamespace(exit_code=0, stdout_text=output, stderr_text="")

        def terminate(self) -> None:
            seen["terminated"] = True

    monkeypatch.setattr(chalkcompute, "function", capture_function)
    monkeypatch.setattr(chalkcompute, "Sandbox", FakeSandbox)
    script = Path(__file__).resolve().parents[1] / "examples/function-sandbox/deploy.py"
    deployed_function = runpy.run_path(str(script))["create_sandbox"]

    expected_env: dict[str, str] = {}
    if image_override is not None:
        expected_env["CHALK_SANDBOX_E2E_IMAGE"] = image_override
    if command_override is not None:
        expected_env["CHALK_SANDBOX_E2E_COMMAND"] = json.dumps(command_override)
    assert seen["function_env"] == (expected_env or None)
    monkeypatch.delenv("CHALK_SANDBOX_E2E_IMAGE", raising=False)
    monkeypatch.delenv("CHALK_SANDBOX_E2E_COMMAND", raising=False)
    for name, value in expected_env.items():
        monkeypatch.setenv(name, value)
    assert deployed_function() == expected_output
    expected_command = (
        command_override
        if command_override is not None
        else ["echo", "sandbox-exec-ok"]
    )
    assert seen["commands"] == [tuple(expected_command)]
    assert (
        seen["sandbox_image"] == image_override
        if image_override is not None
        else isinstance(seen["sandbox_image"], chalkcompute.Image)
    )
    assert seen["terminated"] is True
