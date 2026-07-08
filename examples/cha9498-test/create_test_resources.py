#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute"]
# ///
#
# Creates the three fixtures for the CHA-9498 delete-cascade test WITHOUT
# building an image. Your local env has no container registry configured, so the
# normal SDK path (@chalkcompute.function / Image.pip_install) can't build. We
# sidestep that by calling the same Connect RPCs the SDK calls, but with a
# prebuilt public image (traefik/whoami) — no GetOrBuildCustomImage involved.
#
# The records are what the delete cascade acts on; the containers don't need to
# be real functions. CreateExternalFunctionVersion makes the server tag the
# scaling group with function_name metadata, which is exactly what the delete
# modal warning and the reverse cascade key off.
#
# Auth + API host come from ~/.chalk.yml (localhost:4002, env avi-chalk-ai) via
# chalkcompute's own ConnectClient.
#
#   uv run create_test_resources.py

from google.protobuf.json_format import MessageToDict

from chalkcompute._connect import ConnectClient
from chalkcompute._function import _CATALOG_SVC, _SG_SVC, _build_schemas

IMAGE = "traefik/whoami"  # tiny public HTTP server, listens on :80
PORT = 80


def _spec(name: str) -> dict:
    return {
        "containerSpec": {"name": name, "image": IMAGE, "port": PORT},
        "scalingSpec": {"minReplicas": 1, "maxReplicas": 1},
    }


def _sig(name: str) -> str:  # only used to synthesize valid arrow schemas
    return name


def create_function_backed(client: ConnectClient, function_name: str) -> None:
    in_schema, out_schema = _build_schemas(_sig)
    body = {
        "functionName": function_name,
        "inputArrowSchema": MessageToDict(in_schema),
        "outputArrowSchema": MessageToDict(out_schema),
        # container name == function name → the backing scaling group's name
        "spec": _spec(function_name),
    }
    resp = client.rpc(_CATALOG_SVC, "CreateExternalFunctionVersion", body, timeout=120)
    if resp.status_code != 200:
        raise SystemExit(
            f"CreateExternalFunctionVersion({function_name}) failed: {resp.text}"
        )
    print(f"created function-backed scaling group for '{function_name}': {resp.text}")


def create_standalone_sg(client: ConnectClient, name: str) -> None:
    resp = client.rpc(_SG_SVC, "CreateScalingGroup", {"spec": _spec(name)}, timeout=120)
    if resp.status_code != 200:
        raise SystemExit(f"CreateScalingGroup({name}) failed: {resp.text}")
    print(f"created standalone scaling group '{name}': {resp.text}")


if __name__ == "__main__":
    client = ConnectClient()
    # Scenario 1: delete this SG in the UI -> function should disappear.
    create_function_backed(client, "cha9498-reverse-cascade")
    # Scenario 2: delete this FUNCTION -> its SG should disappear.
    create_function_backed(client, "cha9498-forward")
    # Scenario 3: delete this standalone SG -> no function touched, no warning.
    create_standalone_sg(client, "cha9498-standalone-sg")
    print("\nDone. Check the Scaling groups and External functions pages.")
