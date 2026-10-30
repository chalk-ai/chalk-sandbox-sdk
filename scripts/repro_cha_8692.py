#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["chalkcompute", "python-dotenv", "httpx", "grpcio"]
# [tool.uv.sources]
# chalkcompute = { path = "../" }
# ///
#
# Reproduction for https://linear.app/chalk-ai/issue/CHA-8692
# "Make `.env(...)` work correctly on images"
#
# Run with staging credentials in `.env`:
#   set -a && source .env && set +a
#   uv run python scripts/repro_cha_8692.py
#
# What this script reproduces against staging
# -------------------------------------------
#
# (1) Container(image=Image.debian_slim().env({...}))
#       PASSES today. ENV directives are written into the generated
#       Dockerfile (see go-api-server/sandbox/custom_image_builder_argo.go
#       generateDockerfile -> "ENV %s=%q") and the K8s pod inherits
#       them from the image manifest. ContainerService overlays its
#       own pod-level env on top, but does not strip the image env.
#
# (2) SandboxClient.create(image=Image.debian_slim().env({...}))
#       Reproduces the failure described in CHA-8692. The image build
#       succeeds and the pod is scheduled, but the sandbox never
#       reaches the "ready" state because:
#
#         buildSandboxContainerSpec (lifecycle.go) does NOT set an
#         Entrypoint. With a custom-built Image whose base is
#         debian_slim, the resulting pod inherits debian's default
#         CMD instead of running the chalk-sandbox-entrypoint binary
#         that phones home over TCP. The pod exits and the sandbox
#         is stuck in "connecting".
#
#       The original bug report ("the pod did not have the expected
#       env vars") is consistent with this failure mode: the user
#       never gets a chance to observe their env inside the pod
#       because the pod never establishes a session, so any
#       debugging looks like "env wasn't applied".
#
# Fix sketch
# ----------
#   - Inject the chalk-sandbox-entrypoint into custom-built images
#     during Dockerfile generation (multi-stage COPY from the
#     entrypoint base image), OR
#   - Override the K8s pod Entrypoint in buildSandboxContainerSpec
#     when the image source was an ImageSpec (after ensuring the
#     binary is available at a known path), OR
#   - Reject ImageSpec on Sandbox until the entrypoint is plumbed,
#     and document that Sandbox needs a pre-built sandbox image.

from __future__ import annotations

import os
import sys
import time
import uuid

import grpc
import httpx
from dotenv import load_dotenv
from google.protobuf import json_format

from chalkcompute import Container, Image, SandboxClient
from chalkcompute._gen.chalk.sandbox.v1 import service_pb2 as pb

load_dotenv()


def _client() -> SandboxClient:
    api = os.environ["CHALK_API_SERVER"]
    resp = httpx.post(
        f"{api}/v1/oauth/token",
        json={
            "client_id": os.environ["CHALK_CLIENT_ID"],
            "client_secret": os.environ["CHALK_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    host = api.replace("https://", "").replace("http://", "")
    return SandboxClient(
        f"{host}:443",
        credentials=grpc.ssl_channel_credentials(),
        metadata=[
            ("authorization", f"Bearer {token}"),
            ("x-chalk-env-id", os.environ["CHALK_ENVIRONMENT_ID"]),
        ],
    )


def case_container_image_env() -> bool:
    print("\n--- (1) Container(image=Image.env({...})) ---")
    img = Image.debian_slim().env({"TEST_VAR": "hello", "ANOTHER": "42"})
    c = Container(image=img, name=f"cha8692-c-{uuid.uuid4().hex[:8]}")
    try:
        c.run()
        r = c.exec("sh", "-c", "echo $TEST_VAR/$ANOTHER")
        print(f"  exit={r.exit_code} stdout={r.stdout_text!r}")
        return r.exit_code == 0 and r.stdout_text.strip() == "hello/42"
    finally:
        c.stop()


def case_sandbox_image_env(deadline_secs: float = 180.0) -> bool:
    print("\n--- (2) SandboxClient.create(image=Image.env({...})) ---")
    img = Image.debian_slim().env({"TEST_VAR": "hello"})
    client = _client()
    try:
        req = pb.CreateSandboxRequest()
        req.image_spec.CopyFrom(img.to_proto())
        req.name = f"cha8692-s-{uuid.uuid4().hex[:8]}"
        resp = client._stub.CreateSandbox(req, metadata=client._metadata)
        sid = resp.sandbox.id
        print(f"  created {sid} state={resp.sandbox.state} build_id={resp.sandbox.build_id!r}")

        deadline = time.monotonic() + deadline_secs
        last_state = ""
        while time.monotonic() < deadline:
            time.sleep(2)
            g = client._stub.GetSandbox(
                pb.GetSandboxRequest(sandbox_id=sid),
                metadata=client._metadata,
            )
            state = g.sandbox.state
            if state != last_state:
                print(f"  state -> {state}")
                last_state = state
            if state == "ready":
                r = client.get(sid).exec("sh", "-c", "echo $TEST_VAR")
                print(f"  exec exit={r.exit_code} stdout={r.stdout_text!r}")
                client._stub.TerminateSandbox(
                    pb.TerminateSandboxRequest(sandbox_id=sid),
                    metadata=client._metadata,
                )
                return r.exit_code == 0 and r.stdout_text.strip() == "hello"
            if state in ("error", "terminated"):
                print(f"  final: {json_format.MessageToJson(g)}")
                return False

        print(f"  TIMEOUT after {deadline_secs}s in state={last_state!r}")
        client._stub.TerminateSandbox(
            pb.TerminateSandboxRequest(sandbox_id=sid),
            metadata=client._metadata,
        )
        return False
    finally:
        client.close()


def main() -> int:
    results = {
        "container_image_env": case_container_image_env(),
        "sandbox_image_env": case_sandbox_image_env(),
    }
    print("\n=== summary ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL (reproduces CHA-8692)'}")
    # Repro of the bug = sandbox case fails. Container case is the control.
    return 0 if results["container_image_env"] and not results["sandbox_image_env"] else 2


if __name__ == "__main__":
    sys.exit(main())
