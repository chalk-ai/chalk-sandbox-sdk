"""Measure raw endpoint responses immediately after ScalingGroup.deploy().

Run against ft-qa with a pinned observability-matrix HTTP or gRPC image.
The same command works with baseline and candidate SDK checkouts via PYTHONPATH.
No requests warm up the endpoint before deploy returns. SDK request retries are
bypassed. By default the temporary group is deleted; --keep retains it so the
printed staging link can be inspected.

Example (credentials come from `chalk login --api-host ...`):
    CHALK_API_SERVER=https://api.staging.chalk.ai \
    CHALK_ENVIRONMENT_ID=tmnmc9beyujew \
    python scripts/repro_scaling_group_readiness.py \
      --image IMAGE_URI --protocol http --compute-class K8S \
      --name avi-readiness-before --output before.json

The matrix images serve GET /healthz and GET /probe?marker=..., or the generic
/obsm.Probe/Echo gRPC method. They echo OBSM_REV to identify the test workload.
Requires httpx and grpcio in addition to the SDK.
"""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import grpc
import httpx

from chalkcompute import ComputeClass, ReadinessProbe, ScalingGroup

ENVIRONMENT_ID = "tmnmc9beyujew"
PROJECT_ID = "tmnmau5shs3qe"
REVISION = "cha11576"
MARKER = "readiness-check"


def request_once(group, protocol, token):
    """Use fresh connections and expose every HTTP/gRPC transport failure."""
    if protocol == "http":
        response = httpx.get(
            group.web_url.rstrip("/") + "/probe",
            params={"marker": MARKER},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Chalk-Env-Id": ENVIRONMENT_ID,
            },
            timeout=5,
        )
        return {
            "status": response.status_code,
            "body": response.text[:300],
            "ok": response.status_code == 200
            and response.json().get("rev") == REVISION,
        }

    host = urlparse(group.web_url).hostname
    with grpc.secure_channel(
        f"{host}:443",
        grpc.ssl_channel_credentials(),
        options=[("grpc.enable_retries", 0)],
    ) as channel:
        reply = channel.unary_unary("/obsm.Probe/Echo")(
            MARKER.encode(),
            metadata=(
                ("authorization", f"Bearer {token}"),
                ("x-chalk-env-id", ENVIRONMENT_ID),
            ),
            timeout=5,
        ).decode()
    return {
        "status": "OK",
        "body": reply,
        "ok": reply == f"ok {MARKER} rev={REVISION}",
    }


def observe(group, protocol, token, returned_at, seconds):
    requests = []
    while time.monotonic() - returned_at < seconds:
        started_at = time.monotonic()
        try:
            result = request_once(group, protocol, token)
        except Exception as error:
            result = {
                "status": error.code().name
                if isinstance(error, grpc.RpcError)
                else type(error).__name__,
                "body": str(error)[:500],
                "ok": False,
            }
        result["started_after_deploy_seconds"] = started_at - returned_at
        result["duration_seconds"] = time.monotonic() - started_at
        requests.append(result)
        print(json.dumps(result), flush=True)
        time.sleep(0.5)
    return requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--protocol", choices=("http", "grpc"), required=True)
    parser.add_argument("--compute-class", choices=("K8S", "HOST"), required=True)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if os.environ.get("CHALK_ENVIRONMENT_ID") != ENVIRONMENT_ID:
        parser.error("This reproduction is restricted to ft-qa: tmnmc9beyujew")
    if os.environ.get("CHALK_API_SERVER") != "https://api.staging.chalk.ai":
        parser.error("Set CHALK_API_SERVER=https://api.staging.chalk.ai")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")

    group = ScalingGroup(
        name=args.name,
        image=args.image,
        entrypoint=["python", "/app/server.py"],
        protocol=args.protocol,
        compute_class=getattr(ComputeClass, args.compute_class),
        authenticated=True,
        cpu="500m",
        memory="1Gi",
        env={
            "OBSM_REV": REVISION,
            "OTEL_SERVICE_NAME": args.name,
            "CHALK_ENVIRONMENT_ID": ENVIRONMENT_ID,
        },
        readiness_probe=ReadinessProbe.http("/healthz")
        if args.protocol == "http"
        else None,
    )
    # Acquire auth before deployment so it cannot delay the first request.
    group._client._ensure_auth()
    token = group._client._access_token
    try:
        started_at = time.monotonic()
        group.deploy(ready_timeout=180)
        returned_at = time.monotonic()
        requests = observe(group, args.protocol, token, returned_at, args.seconds)
        report = {
            "name": args.name,
            "id": group.id,
            "revision_id": group.current_revision_id,
            "compute_class": args.compute_class,
            "protocol": args.protocol,
            "deploy_seconds": returned_at - started_at,
            "staging_url": f"https://staging.chalk.dev/projects/{PROJECT_ID}/environments/{ENVIRONMENT_ID}/scaling-groups/{group.id}",
            "endpoint": group.web_url,
            "failures": sum(not request["ok"] for request in requests),
            "requests": requests,
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "requests"}
            ),
            flush=True,
        )
    finally:
        if group.id is not None and not args.keep:
            group.delete()


if __name__ == "__main__":
    main()
