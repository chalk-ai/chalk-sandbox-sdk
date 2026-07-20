"""Deploy a tiny model and call it via ``get_model_version(...).remote()``.

This exercises the change end-to-end against a real environment (staging):

    get_model_version(name=...) -> DeployedModelVersion -> .remote(...)
        -> RemoteCallService.CallFunction  (scaling group's public ingress, direct)
        -> proxied to the scaling group's "handler" route
        -> first output column returned

The model computes ``a - b`` (see handler.py), so argument order is observable.

--------------------------------------------------------------------------------
Setup
--------------------------------------------------------------------------------
Run from the chalk repo's venv so ``.remote()`` exercises the LOCAL edited
``chalk`` package (the change under test), and add chalkcompute for the deploy
step (only the deploy needs it; the call is pure chalkpy):

    cd /Users/avi/chalk-code/chalk
    uv pip install chalkcompute            # into .venv, for the deploy step
    export CHALK_CLIENT_ID=...             # staging service-token creds
    export CHALK_CLIENT_SECRET=...
    export CHALK_ENVIRONMENT_ID=...        # staging environment id

You do NOT need ``chalk apply`` — that deploys a feature/resolver project;
scaling-group model deployment happens through the client API calls below.

--------------------------------------------------------------------------------
Usage
--------------------------------------------------------------------------------
    # idempotent: deploys only if not already callable, then calls.
    # Safe to re-run — it will NOT re-register (which would bump the latest
    # version and strand the scaling group on the old one).
    .venv/bin/python examples/deployed_model/deploy_and_call.py

    # skip deploy entirely; just call:
    .venv/bin/python examples/deployed_model/deploy_and_call.py --call-only

    # force a fresh register + deploy:
    .venv/bin/python examples/deployed_model/deploy_and_call.py --force

    # tear it down when done:
    .venv/bin/python examples/deployed_model/deploy_and_call.py --delete
"""

from __future__ import annotations

import argparse
import os
import time

import pyarrow as pa

from chalk.client import ChalkClient
from chalk.ml import ModelType
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest

MODEL_NAME = "avi-remote-subtract"
SG_NAME = "avi-remote-subtract-sg"
HANDLER_PATH = os.path.join(os.path.dirname(__file__), "handler.py")
READY_TIMEOUT_S = 300
READY_POLL_S = 5


def _client() -> ChalkClient:
    # Point explicitly at staging. Without api_server + creds, ChalkClient falls
    # back to ~/.chalk.yml, whose token is resolved by *current directory* — from
    # the chalk repo that is the localhost:4002 dev token, which yields a 502.
    return ChalkClient(
        api_server=os.environ.get("CHALK_API_SERVER", "https://api.staging.chalk.dev"),
        client_id=os.environ.get("CHALK_CLIENT_ID"),
        client_secret=os.environ.get("CHALK_CLIENT_SECRET"),
        environment=os.environ.get("CHALK_ENVIRONMENT_ID", "tmnmc9beyujew"),
    )


def ensure_deployed(client: ChalkClient) -> None:
    """Deploy only if needed; otherwise reuse the existing deployment.

    Idempotent so repeated test runs don't churn. Three cases:
      * model callable already        -> reuse, no deploy
      * model exists but not callable -> redeploy the scaling group to the
                                         *current* latest version, WITHOUT
                                         re-registering (re-registering bumps the
                                         latest version and strands the scaling
                                         group on the old one — the usual cause of
                                         "no deployed scaling group with a web URL")
      * model absent                  -> register a new version + deploy
    """
    existing = _get_model_or_none(client)
    if existing is not None and existing.version is not None:
        try:
            existing.remote(0.0, 0.0)  # liveness probe (0 - 0 == 0)
            print(f"Reusing existing deployment of {MODEL_NAME!r} v{existing.version} (callable).")
            return
        except Exception as e:  # noqa: BLE001 - any failure => (re)deploy the SG
            print(f"{MODEL_NAME!r} v{existing.version} exists but is not callable ({e}); redeploying scaling group.")
            _deploy_scaling_group(client, existing.version)
            return

    print(f"{MODEL_NAME!r} not found; registering + deploying.")
    version = _register(client)
    _deploy_scaling_group(client, version)


def _get_model_or_none(client: ChalkClient):
    try:
        return client.get_model_version(name=MODEL_NAME)
    except Exception:  # noqa: BLE001 - not-found (and transport errors) => treat as absent
        return None


def _register(client: ChalkClient) -> int:
    # Imported lazily: only registration needs chalkcompute (to build the image).
    from chalkcompute import Image

    image = (
        Image.debian_slim("3.11")
        .pip_install(["chalk-remote-call-python", "pyarrow"])
        .add_local_file(HANDLER_PATH, "/app/handler.py", strategy="copy")
        .env({"PYTHONPATH": "/app"})
        .workdir("/app")
        .entrypoint(["chalk-remote-call", "--handler", "handler.handler", "--port", "8080"])
    )

    print(f"Registering model version for {MODEL_NAME!r} ...")
    version = client.register_model_version(
        name=MODEL_NAME,
        # Placeholder metadata: this is a code-only handler (no ML artifact), but
        # the deploy path calls download_model_artifact, which rejects an
        # unspecified model_type. A valid type lets it return empty and the model
        # deploys image-only. The handler ignores it.
        model_type=ModelType.SKLEARN,
        input_schema={"a": pa.float64(), "b": pa.float64()},
        output_schema={"result": pa.float64()},
        # input_features drives DeployedModel keyword-argument validation; keep
        # it in the same order as input_schema so positional and keyword calls
        # agree.
        input_features=["a", "b"],
        output_features=["result"],
        model_image=image,
    )
    print(f"  -> version {version.model_version}")
    return version.model_version


def _deploy_scaling_group(client: ChalkClient, version: int) -> None:
    print(f"Deploying version {version} to scaling group {SG_NAME!r} ...")
    client.deploy_model_version_to_scaling_group(
        name=SG_NAME,
        model_name=MODEL_NAME,
        model_version=version,
        handler="handler.handler",
        scaling=AutoScalingSpec(min_replicas=1, max_replicas=1, target_cpu_utilization_percentage=70),
        resources=ScalingGroupResourceRequest(cpu="500m", memory="512Mi"),
    )
    _wait_ready(client)


def _wait_ready(client: ChalkClient) -> None:
    print("Waiting for a ready replica ...")
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        for sg in client.list_scaling_groups().scalingGroups:
            if sg.name == SG_NAME and (sg.readyReplicas or 0) > 0:
                print(f"  -> ready ({sg.readyReplicas} replica(s))")
                return
        time.sleep(READY_POLL_S)
    raise TimeoutError(f"Scaling group {SG_NAME!r} not ready after {READY_TIMEOUT_S}s")


def call(client: ChalkClient) -> None:
    model = client.get_model_version(name=MODEL_NAME)
    print(f"get_model_version -> {type(model).__name__}  version={model.version}  input_features={model.input_features}")

    checks = [
        ("remote(5.0, 3.0)", lambda: model.remote(5.0, 3.0), 2.0),
        ("remote(3.0, 5.0)", lambda: model.remote(3.0, 5.0), -2.0),
        ("remote(a=5.0, b=3.0)", lambda: model.remote(a=5.0, b=3.0), 2.0),
        ("remote(b=3.0, a=5.0)  # reordered kwargs", lambda: model.remote(b=3.0, a=5.0), 2.0),
    ]
    ok = True
    for label, fn, expected in checks:
        got = fn()
        status = "OK " if got == expected else "FAIL"
        ok = ok and got == expected
        print(f"  [{status}] {label} -> {got}  (expected {expected})")

    # A couple of expected-error paths (client-side validation, no network):
    for label, fn in [
        ("remote(1.0)  # wrong arity", lambda: model.remote(1.0)),
        ("remote(z=1.0)  # unknown kwarg", lambda: model.remote(z=1.0)),
    ]:
        try:
            fn()
            print(f"  [FAIL] {label} -> did not raise")
            ok = False
        except ValueError as e:
            print(f"  [OK ] {label} -> ValueError: {e}")

    print("ALL PASS" if ok else "SOME CHECKS FAILED")


def delete(client: ChalkClient) -> None:
    try:
        client.delete_scaling_group(name=SG_NAME)
        print(f"Deleted scaling group {SG_NAME!r}")
    except Exception as e:  # noqa: BLE001 - best-effort teardown
        print(f"Could not delete scaling group: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call-only", action="store_true", help="Skip deploy entirely; just call.")
    parser.add_argument(
        "--force", action="store_true", help="Register a new version + deploy even if one already exists."
    )
    parser.add_argument("--delete", action="store_true", help="Delete the scaling group and exit.")
    args = parser.parse_args()

    client = _client()
    if args.delete:
        delete(client)
        return
    if args.force:
        _deploy_scaling_group(client, _register(client))
    elif not args.call_only:
        ensure_deployed(client)
    call(client)


if __name__ == "__main__":
    main()
