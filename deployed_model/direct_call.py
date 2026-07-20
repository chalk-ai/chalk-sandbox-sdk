"""Staging smoke test for direct scaling-group calls.

Deploys a trivial ``a - b`` model, then calls it via ``get_model_version(...).remote(...)``,
which issues RemoteCallService.CallFunction to the scaling group's public ingress directly
(no api-server proxy).

Setup (from the chalk repo venv, with chalkcompute installed for the deploy + call):

    uv pip install chalkcompute
    export CHALK_CLIENT_ID=...  CHALK_CLIENT_SECRET=...  CHALK_ENVIRONMENT_ID=...

Usage:

    .venv/bin/python examples/deployed_model/direct_call.py            # deploy if needed, then call
    .venv/bin/python examples/deployed_model/direct_call.py --call-only
    .venv/bin/python examples/deployed_model/direct_call.py --delete
"""

from __future__ import annotations

import argparse
import os
import time

import pyarrow as pa
from chalk.client import ChalkClient
from chalk.ml import ModelType
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest

MODEL_NAME = "avi-direct-subtract"
SG_NAME = "avi-direct-subtract-sg"
HANDLER_PATH = os.path.join(os.path.dirname(__file__), "handler.py")
READY_TIMEOUT_S = 300
READY_POLL_S = 5


def _client() -> ChalkClient:
    return ChalkClient(
        api_server=os.environ.get("CHALK_API_SERVER", "https://api.staging.chalk.dev"),
        client_id=os.environ.get("CHALK_CLIENT_ID"),
        client_secret=os.environ.get("CHALK_CLIENT_SECRET"),
        environment=os.environ.get("CHALK_ENVIRONMENT_ID", "tmnmc9beyujew"),
    )


def _register(client: ChalkClient) -> int:
    from chalkcompute import Image

    image = (
        Image.debian_slim("3.11")
        .pip_install(["chalk-remote-call-python", "pyarrow"])
        .add_local_file(HANDLER_PATH, "/app/handler.py", strategy="copy")
        .env({"PYTHONPATH": "/app"})
        .workdir("/app")
        .entrypoint(
            ["chalk-remote-call", "--handler", "handler.handler", "--port", "8080"]
        )
    )
    version = client.register_model_version(
        name=MODEL_NAME,
        model_type=ModelType.SKLEARN,  # placeholder; handler is code-only
        input_schema={"a": pa.float64(), "b": pa.float64()},
        output_schema={"result": pa.float64()},
        input_features=["a", "b"],
        output_features=["result"],
        model_image=image,
    )
    return version.model_version


def _deploy(client: ChalkClient, version: int) -> None:
    print(f"Deploying {MODEL_NAME!r} v{version} to {SG_NAME!r} ...")
    client.deploy_model_version_to_scaling_group(
        name=SG_NAME,
        model_name=MODEL_NAME,
        model_version=version,
        handler="handler.handler",
        scaling=AutoScalingSpec(
            min_replicas=1, max_replicas=1, target_cpu_utilization_percentage=70
        ),
        resources=ScalingGroupResourceRequest(cpu="500m", memory="512Mi"),
    )
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        for sg in client.list_scaling_groups().scalingGroups:
            if sg.name == SG_NAME and (sg.readyReplicas or 0) > 0:
                print(f"  ready ({sg.readyReplicas} replica(s))")
                return
        time.sleep(READY_POLL_S)
    raise TimeoutError(f"Scaling group {SG_NAME!r} not ready after {READY_TIMEOUT_S}s")


def call(client: ChalkClient) -> None:
    model = client.get_model_version(MODEL_NAME)  # latest; DeployedModelVersion
    print(
        f"get_model_version -> {type(model).__name__} v{model.version} inputs={model.input_features}"
    )

    ok = True

    # Correct bindings. The handler computes a - b, so a swapped or misbound
    # argument changes the result: these verify kwargs bind by name, not order.
    good = [
        ("remote(5.0, 3.0)", lambda: model.remote(5.0, 3.0), 2.0),
        ("remote(3.0, 5.0)", lambda: model.remote(3.0, 5.0), -2.0),
        ("remote(0.0, 0.0)", lambda: model.remote(0.0, 0.0), 0.0),
        ("remote(a=5.0, b=3.0)", lambda: model.remote(a=5.0, b=3.0), 2.0),
        (
            "remote(b=3.0, a=5.0)  # kwarg order must not matter",
            lambda: model.remote(b=3.0, a=5.0),
            2.0,
        ),
        (
            "remote(5.0, b=3.0)  # mixed positional + kwarg",
            lambda: model.remote(5.0, b=3.0),
            2.0,
        ),
    ]
    for label, fn, expected in good:
        got = fn()
        passed = got == expected
        ok = ok and passed
        print(f"  [{'OK ' if passed else 'FAIL'}] {label} -> {got} (expected {expected})")

    # Incorrect bindings: bind_inputs must reject these client-side with
    # ValueError, before any network call.
    bad = [
        ("remote()  # no inputs", lambda: model.remote()),
        ("remote(1.0)  # missing b", lambda: model.remote(1.0)),
        ("remote(a=1.0)  # missing b (kwargs only)", lambda: model.remote(a=1.0)),
        ("remote(b=1.0)  # missing a (kwargs only)", lambda: model.remote(b=1.0)),
        ("remote(1.0, 2.0, 3.0)  # too many positional", lambda: model.remote(1.0, 2.0, 3.0)),
        ("remote(z=1.0)  # unknown kwarg", lambda: model.remote(z=1.0)),
        (
            "remote(a=1.0, b=2.0, z=3.0)  # unknown kwarg alongside valid ones",
            lambda: model.remote(a=1.0, b=2.0, z=3.0),
        ),
        (
            "remote(1.0, a=2.0)  # 'a' both positionally and by keyword",
            lambda: model.remote(1.0, a=2.0),
        ),
        (
            "remote(1.0, 2.0, b=3.0)  # 'b' both positionally and by keyword",
            lambda: model.remote(1.0, 2.0, b=3.0),
        ),
    ]
    for label, fn in bad:
        try:
            fn()
            print(f"  [FAIL] {label} -> did not raise")
            ok = False
        except ValueError as e:
            print(f"  [OK ] {label} -> ValueError: {e}")
        except Exception as e:
            print(f"  [FAIL] {label} -> {type(e).__name__} (expected ValueError): {e}")
            ok = False

    # Wrong types pass client-side binding (bind_inputs does not type-check)
    # and must fail in the handler; any raised error is acceptable, silence is not.
    for label, fn in [
        ('remote("5", "3")  # strings, not floats', lambda: model.remote("5", "3")),
        ('remote(a="5", b=3.0)  # mixed-type kwargs', lambda: model.remote(a="5", b=3.0)),
    ]:
        try:
            got = fn()
            print(f"  [FAIL] {label} -> did not raise (got {got!r})")
            ok = False
        except Exception as e:
            print(f"  [OK ] {label} -> {type(e).__name__}: {e}")

    print("ALL PASS" if ok else "SOME CHECKS FAILED")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--call-only", action="store_true", help="Skip deploy; just call."
    )
    parser.add_argument(
        "--delete", action="store_true", help="Delete the scaling group and exit."
    )
    args = parser.parse_args()

    client = _client()
    if args.delete:
        client.delete_scaling_group(name=SG_NAME)
        print(f"Deleted {SG_NAME!r}")
        return
    if not args.call_only:
        _deploy(client, _register(client))
    call(client)


if __name__ == "__main__":
    main()
