"""The function-sandbox example must carry the CI image into its deployed pod."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import chalkcompute
import pytest


@pytest.mark.parametrize("image_override", [None, "registry.local/sandbox:test"])
def test_function_sandbox_example_image_override(
    monkeypatch: pytest.MonkeyPatch, image_override: str | None
) -> None:
    if image_override is None:
        monkeypatch.delenv("CHALK_SANDBOX_E2E_IMAGE", raising=False)
    else:
        monkeypatch.setenv("CHALK_SANDBOX_E2E_IMAGE", image_override)

    deployed: dict[str, Any] = {}
    selected: dict[str, Any] = {}

    class FakeImage:
        @classmethod
        def debian_slim(cls, version: str) -> FakeImage:
            assert version == "3.12"
            return cls()

        def pip_install(self, packages: list[str]) -> FakeImage:
            assert packages == ["chalkcompute>=2.11.1"]
            return self

    class FakeSandbox:
        def __init__(self, *, image: str | FakeImage) -> None:
            selected["image"] = image
            self.id: str | None = None

        def run(self) -> None:
            self.id = "sandbox-id"

        def exec(self, *command: str) -> SimpleNamespace:
            assert command == ("echo", "sandbox-exec-ok")
            return SimpleNamespace(exit_code=0, stdout_text="sandbox-exec-ok\n")

        def terminate(self) -> None:
            selected["terminated"] = True

    def fake_function(**kwargs: Any) -> Any:
        deployed.update(kwargs)
        return lambda function: function

    monkeypatch.setattr(chalkcompute, "function", fake_function)
    monkeypatch.setattr(chalkcompute, "Image", FakeImage)
    monkeypatch.setattr(chalkcompute, "Sandbox", FakeSandbox)

    example = (
        Path(__file__).resolve().parents[1] / "examples/function-sandbox/deploy.py"
    )
    create_sandbox = runpy.run_path(str(example))["create_sandbox"]

    assert deployed["env"] == (
        {"CHALK_SANDBOX_E2E_IMAGE": image_override} if image_override else None
    )
    assert create_sandbox() == "sandbox-exec-ok\n"
    assert selected["terminated"] is True
    if image_override:
        assert selected["image"] == image_override
    else:
        assert isinstance(selected["image"], FakeImage)
