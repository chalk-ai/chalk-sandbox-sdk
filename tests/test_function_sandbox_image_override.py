from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import chalkcompute
import pytest


@pytest.mark.parametrize(
    ("image_override", "expected_python"),
    [(None, None), ("python:3.13-slim", "3.13")],
)
def test_function_sandbox_image_override(
    monkeypatch: pytest.MonkeyPatch,
    image_override: str | None,
    expected_python: str | None,
) -> None:
    seen: dict[str, Any] = {}
    for key, value in (
        ("CHALK_SANDBOX_E2E_IMAGE", image_override),
        ("CHALK_SANDBOX_E2E_EXPECTED_PYTHON", expected_python),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

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
                version = "3.13" if seen["sandbox_image"] == image_override else "3.12"
                output = f"Python {version}.0\n"
            else:
                output = "sandbox-exec-ok\n"
            return SimpleNamespace(exit_code=0, stdout_text=output, stderr_text="")

        def terminate(self) -> None:
            seen["terminated"] = True

    monkeypatch.setattr(chalkcompute, "function", capture_function)
    monkeypatch.setattr(chalkcompute, "Sandbox", FakeSandbox)
    script = Path(__file__).resolve().parents[1] / "examples/function-sandbox/deploy.py"
    deployed_function = runpy.run_path(str(script))["create_sandbox"]

    assert seen["function_env"] == (
        {
            "CHALK_SANDBOX_E2E_IMAGE": image_override,
            "CHALK_SANDBOX_E2E_EXPECTED_PYTHON": expected_python,
        }
        if image_override is not None
        else None
    )
    assert deployed_function() == "sandbox-exec-ok\n"
    assert (
        seen["sandbox_image"] == image_override
        if image_override is not None
        else isinstance(seen["sandbox_image"], chalkcompute.Image)
    )
    assert seen["terminated"] is True
