#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute>=2.11.1"]
# ///

"""Run Claude in a network-restricted Chalk Sandbox and inspect its API calls.

Prerequisites:
- Standard CHALK_* credentials in the local environment.
- A Chalk environment secret named ANTHROPIC_API_KEY.

The proxy prints redacted headers but complete request and response bodies. Those
bodies can contain prompts, source code, model output, or other sensitive data.
Use this only in a development environment whose terminal output is safe to keep.
The temporary scaling group and sandbox are deleted on normal return, exceptions,
and interrupts handled by Python. The sandbox also expires after 20 minutes if the
local process is killed before its cleanup handlers run.
"""

import json
import secrets
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from chalkcompute import Image, NetworkPolicy, Sandbox, SandboxClient, ScalingGroup


HERE = Path(__file__).parent
PROXY_PORT = 18081
PROMPT = "Say hello in one short sentence."


@contextmanager
def deployed(scaling_group):
    try:
        scaling_group.deploy()
        assert scaling_group.web_url is not None

        for _ in range(60):
            health = scaling_group.call("/healthz", method="GET")
            if health.status == 200:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"proxy health check returned HTTP {health.status}")

        yield scaling_group
    finally:
        scaling_group.delete()


@contextmanager
def terminate_on_exit(sandbox):
    try:
        deadline = time.monotonic() + 600
        while True:
            assert sandbox.info is not None
            print(f"  sandbox poll: status={sandbox.info.status}", flush=True)
            if sandbox.info.status == "running":
                break
            if sandbox.info.status != "pending":
                raise RuntimeError(
                    f"sandbox entered {sandbox.info.status} state: "
                    f"{sandbox.info.status_message or 'no status message'}"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError("sandbox did not start within 600 seconds")
            time.sleep(1)
            sandbox.refresh()

        yield sandbox
    finally:
        sandbox.terminate()


@contextmanager
def printing_proxy_logs(proxy, token):
    stop = threading.Event()

    def print_logs():
        seen = 0
        final = False
        while True:
            response = proxy.call(
                "/logs",
                method="GET",
                headers={"x-claude-intercept-token": token},
            )
            lines = response.content.decode().splitlines()
            for line in lines[seen:]:
                print(
                    "\n--- intercepted Anthropic traffic ---\n"
                    + json.dumps(json.loads(line), indent=2),
                    flush=True,
                )
            seen = len(lines)
            if final:
                return
            final = stop.wait(0.5)

    thread = threading.Thread(target=print_logs, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


proxy_token = secrets.token_urlsafe(32)

proxy_image = (
    Image.debian_slim("3.13")
    .add_local_file(
        str(HERE / "logging_proxy.py"),
        "/app/logging_proxy.py",
        strategy="copy",
    )
    .entrypoint(["python", "/app/logging_proxy.py"])
)

proxy = ScalingGroup(
    image=proxy_image,
    name=f"claude-intercept-{uuid.uuid4().hex[:8]}",
    port=PROXY_PORT,
    env={"CLAUDE_INTERCEPT_TOKEN": proxy_token},
)
with ExitStack() as stack:
    proxy = stack.enter_context(deployed(proxy))

    workload_image = (
        Image.base("node:22-bookworm-slim")
        .run_commands("npm install -g @anthropic-ai/claude-code")
        .add_local_file(
            str(HERE / "claude-settings.json"),
            "/root/.claude/settings.json",
            strategy="copy",
        )
        .add_local_file(
            str(HERE / "claude-state.json"),
            "/root/.claude.json",
            strategy="copy",
        )
        .env({"IS_SANDBOX": "1"})
        .workdir("/work")
    )

    policy = NetworkPolicy(
        allowed_hosts={
            "*.anthropic.com": [
                NetworkPolicy.Rule(
                    transforms=[
                        NetworkPolicy.Transformer(
                            headers={"x-claude-intercept-token": proxy_token},
                        )
                    ],
                    forward_url=proxy.web_url + "/",
                )
            ]
        }
    )

    sandbox = Sandbox(
        image=workload_image,
        network_policy=policy,
        lifetime="1200s",
        env={
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_TELEMETRY": "1",
        },
    ).add_credentials("*.anthropic.com", "ANTHROPIC_API_KEY")

    client = stack.enter_context(SandboxClient.from_env())
    client.run(sandbox, wait=False)
    stack.enter_context(terminate_on_exit(sandbox))
    stack.enter_context(printing_proxy_logs(proxy, proxy_token))

    for event in sandbox.exec_stream(
        "claude", "-p", PROMPT, workdir="/work", timeout_secs=900
    ):
        if event.stdout:
            print(event.stdout, end="")
        if event.stderr:
            print(event.stderr, end="", file=sys.stderr)
