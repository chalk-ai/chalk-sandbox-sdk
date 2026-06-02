#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "chalkcompute",
#     "claude-agent-sdk>=0.0.20",
#     "python-dotenv",
# ]
# ///

# # External agent loop driving a Chalk container
#
# A minimal demo of using the **Claude Agent SDK** as an *external* harness
# around a `chalkcompute.Container`. The Python process here is the agent
# loop: it owns the conversation, exposes two custom tools (`start_container`
# and `exec_in_container`), and the container runs as a side-effect of tool
# calls the model decides to make.
#
# Why bother? It separates *what the agent decides to do* from *what
# compute it runs on*. The model never sees credentials, k8s, or Docker —
# it sees two tools. The harness wires the tools to a remote container in
# Chalk's sandbox cluster and feeds results back into the conversation.
#
# ## Overview
#
# 1. Define two SDK MCP tools that wrap `Container.run()` / `Container.exec()`.
#    A module-level dict holds the live `Container` objects keyed by name,
#    so the model can refer to its container by a friendly name across turns.
# 2. Build a Claude Agent SDK MCP server from those tools.
# 3. Open a `ClaudeSDKClient` configured to use only those two tools.
# 4. Send one prompt — *"make a Debian container and run ls"* — then stream
#    messages until the model finishes. Print every text block and tool call
#    so the agent loop is visible.
# 5. On exit, stop every container the agent started.
#
# Run it (after `just env`):
#
#     uv run python examples/claude/external-harness/external_harness.py
#
# You'll see the model call `start_container`, then `exec_in_container`,
# then summarize the output of `ls`.

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from chalkcompute import Container, Image
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
from dotenv import load_dotenv

# Load .env from the repo root so CHALK_* credentials are picked up.
load_dotenv(Path(__file__).parents[3] / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("external-harness")


# ## Container registry
#
# The agent doesn't have a way to hand a Python object back to us between
# tool calls — it can only pass JSON. So we keep a dict of live containers
# keyed by the name the model picked. Each tool looks up the container by
# name. When the agent loop exits we stop everything that's still in here.

_CONTAINERS: dict[str, Container] = {}


# ## Tool 1 — `start_container`
#
# Spawns a container from a tiny set of declarative knobs: a base image
# string (e.g. `python:3.14-slim-trixie`, `debian:stable-slim`) and an optional
# list of pip packages. We deliberately keep the surface narrow — the agent
# doesn't need every `Image` builder method to demo the harness pattern.
#
# The tool returns the chosen name so the model can use it in subsequent
# `exec_in_container` calls.


@tool(
    "start_container",
    (
        "Start a fresh Chalk container from a Docker base image. "
        "Returns the container name to use in exec_in_container."
    ),
    {
        "name": str,
        "base_image": str,
        "pip_packages": list,
    },
)
async def start_container(args: dict[str, Any]) -> dict[str, Any]:
    name: str = args["name"]
    base_image: str = args["base_image"]
    pip_packages: list[str] = args.get("pip_packages") or []

    if name in _CONTAINERS:
        return {
            "content": [
                {"type": "text", "text": f"Container '{name}' is already running."}
            ],
            "isError": True,
        }

    log.info(
        "Starting container name=%s base_image=%s pip=%s",
        name,
        base_image,
        pip_packages,
    )

    image = Image.base(base_image)
    if pip_packages:
        image = image.pip_install(pip_packages)

    # Suffix the k8s-visible name so concurrent demo runs don't collide.
    container = Container(
        image=image,
        name=f"{name}-{uuid.uuid4().hex[:6]}",
    )
    container.run()
    _CONTAINERS[name] = container

    return {
        "content": [
            {
                "type": "text",
                "text": f"Started container '{name}' from base_image='{base_image}'.",
            }
        ]
    }


# ## Tool 2 — `exec_in_container`
#
# Runs a command inside a previously started container and returns the
# captured stdout, stderr, and exit code as plain text. We cap output at
# ~8KB so a runaway command doesn't blow the model's context window.

_MAX_OUTPUT_CHARS = 8000


@tool(
    "exec_in_container",
    (
        "Execute a shell command inside a running container started with "
        "start_container. Returns stdout, stderr, and exit code."
    ),
    {
        "name": str,
        "command": list,
    },
)
async def exec_in_container(args: dict[str, Any]) -> dict[str, Any]:
    name: str = args["name"]
    command: list[str] = args["command"]

    container = _CONTAINERS.get(name)
    if container is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"No container named '{name}'. Call start_container first.",
                }
            ],
            "isError": True,
        }

    log.info("exec name=%s command=%s", name, command)
    result = container.exec(*command)

    stdout = result.stdout_text[:_MAX_OUTPUT_CHARS]
    stderr = result.stderr_text[:_MAX_OUTPUT_CHARS]
    rendered = (
        f"exit_code: {result.exit_code}\n"
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )
    return {"content": [{"type": "text", "text": rendered}]}


# ## Pretty-print the agent loop
#
# `ClaudeSDKClient.receive_response()` yields `AssistantMessage`,
# `UserMessage` (containing tool results), and a final `ResultMessage`. We
# render each so the conversation is legible from the terminal — every
# text block, tool call, and tool result is one line.


def _render_message(msg: Any) -> None:
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(f"  assistant: {block.text}")
            elif isinstance(block, ToolUseBlock):
                print(f"  tool_call:  {block.name}({block.input})")
    elif isinstance(msg, UserMessage):
        # Tool results come back wrapped as a UserMessage.
        for block in getattr(msg, "content", []) or []:
            content = getattr(block, "content", None)
            if isinstance(content, list):
                for piece in content:
                    text = piece.get("text") if isinstance(piece, dict) else None
                    if text:
                        first_line = text.splitlines()[0] if text else ""
                        print(f"  tool_result: {first_line}")
    elif isinstance(msg, ResultMessage):
        print(f"  [done] {msg.duration_ms}ms")


# ## Wiring it together
#
# Bundle the two tools into an SDK MCP server, configure `ClaudeAgentOptions`
# to allow only those tools (no shell, no filesystem — the model's only
# levers are the ones we wrote), then send a single prompt.
#
# We use `system_prompt="..."` and `permission_mode="bypassPermissions"`
# so the demo runs end-to-end non-interactively. In a production harness
# you would likely keep the default `acceptEdits` mode and supply a
# `permission_callback` to gate destructive actions.


async def run_demo() -> None:
    server = create_sdk_mcp_server(
        name="chalk_container",
        version="0.1.0",
        tools=[start_container, exec_in_container],
    )

    options = ClaudeAgentOptions(
        mcp_servers={"chalk": server},
        allowed_tools=[
            "mcp__chalk__start_container",
            "mcp__chalk__exec_in_container",
        ],
        system_prompt=(
            "You are driving a Chalk container harness. "
            "When asked to run a command, use start_container to spawn a "
            "container if one is not already running, then exec_in_container "
            "to run the requested command. Report the command's stdout back "
            "to the user."
        ),
        permission_mode="bypassPermissions",
        max_turns=8,
    )

    prompt = (
        "Start a Debian container called 'demo' from the base image "
        "'debian:stable-slim', then run 'ls /' inside it and tell me what "
        "directories are at the root."
    )

    print(f"\n>>> user: {prompt}\n")
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                _render_message(message)
    finally:
        for name, container in list(_CONTAINERS.items()):
            log.info("Stopping container %s", name)
            try:
                container.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("stop(%s) failed: %s", name, e)
            _CONTAINERS.pop(name, None)


if __name__ == "__main__":
    try:
        asyncio.run(run_demo())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)
