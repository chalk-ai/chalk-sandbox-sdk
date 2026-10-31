# Model routers

Define and deploy a model alias with `cc.Router(...).deploy()`. Authentication is
resolved automatically, as with `Sandbox.run()`. Supporting types live in
`chalkcompute.router`: `Connection`, `Target`, `Route`, `Classifier`, and
`RoutingRequest`.

This API requires the SDK built from this branch and a router server with the
additive classifier fields. Deployment checks server support before writing.

## Full example: route using a Chalk function

The function returns a label; the router owns the destinations and fallback
behavior. This example uses an existing saved provider connection named
`my-openai`. Use your connection's model IDs if these models are unavailable.

```python
import os

import chalkcompute as cc
from openai import OpenAI
from chalkcompute.router import Connection, Route, RoutingRequest


@cc.function
def assistant_route(request: RoutingRequest) -> str:
    if request.metadata.get("task") == "coding":
        return "coding"
    if len(request.text) > 6000:
        return "complex"
    return "simple"


def main() -> None:
    provider = Connection.from_name("my-openai")
    small = provider.model("gpt-4.1-mini")
    large = provider.model("gpt-4.1")

    router = cc.Router(
        "assistant",
        default=small,
        fallbacks=[large],
        classifier=assistant_route,
        routes={
            "simple": small,
            "coding": Route(large, description="Code generation and debugging"),
            "complex": Route(large, fallbacks=[]),
        },
    ).deploy()

    # Router administration uses Chalk login/environment credentials. Inference
    # uses a router API key and the inference endpoint supplied for your environment.
    inference = OpenAI(
        base_url=os.environ["CHALK_ROUTER_BASE_URL"],
        api_key=os.environ["CHALK_ROUTER_API_KEY"],
    )
    response = inference.chat.completions.create(
        model=router.model,  # chalk/assistant
        messages=[{"role": "user", "content": "Write a Python LRU cache."}],
        metadata={"task": "coding"},
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
```

Install `openai` alongside `chalkcompute` for the inference call. With a local SDK
checkout, use the documented [local SDK function setup](../local-sdk-remote-call-testing.md)
so the deployed function can import `RoutingRequest`.

The function decorator follows the normal function deployment lifecycle.
`Router.deploy()` waits for the classifier's deployment and readiness before
publishing the router. The function and router must use the same Chalk environment.
The function reference uses its stable ID and **follows its active deployment**;
this API does not promise immutable serving of historical function versions.

The router and function remain deployed after this script exits. Repeated runs
update the same router alias. Inference incurs provider charges and the classifier
uses compute resources. `router.delete()` archives the alias; it does not delete
the function or provider connection. Archived router names remain reserved.

## Request and result contract

A function classifier takes exactly one annotated `RoutingRequest` parameter and
returns `str`. Synchronous and async functions are supported; generators are not.

- `request.model`: the requested public model alias.
- `request.surface`: `chat_completions`, `messages`, or `responses`.
- `request.request`: the surface's JSON request, retaining metadata, tools, and
  multimodal content. Router-only fallback controls are excluded from chat input.
- `request.metadata`: metadata dictionary, or an empty dictionary when absent.
- `request.messages`: structured messages/input items, when present.
- `request.text`: textual content joined with newlines, without image data,
  metadata, or tool definitions. Use the structured request for precise decisions.

Requests needing stored Responses conversation history use the default target.
Unknown labels, exceptions, unavailable functions, and classifier timeouts also
use the default. The selected target then uses the normal provider fallback path.
Function verdicts are not cached and function calls are not retried by the router.

`Route(target, fallbacks=None)` inherits the fallback defaults for that target;
`fallbacks=[]` explicitly disables them. The router's `fallbacks=` argument sets
the chain for its `default` target. Other targets can supply their own chains in
`Route(..., fallbacks=[...])`.

## Classifier choices

Use the function directly for live classification, or configure it explicitly:

```python
from chalkcompute.router import Classifier

classifier = Classifier.function(assistant_route, mode="shadow")
```

Shadow mode records the classification while serving the default. Off mode skips
classification. Function and model classifiers default to a one-second deadline;
set `timeout_ms` only when your workload requires a different limit (1–30000 ms).

For model classification, use `Classifier.completion(target)` or
`Classifier.decisions(target)`, and put criteria on your `Route` descriptions.
These classifiers incur an additional model call, including in shadow mode.
`Classifier.heuristic()` runs locally and requires routes named `"0"` and `"1"`
for easy and hard requests:

```python
router = cc.Router(
    "assistant",
    default=small,
    classifier=Classifier.heuristic(),
    routes={"0": small, "1": large},
).deploy()
```

## Connections and optional clients

Connections are saved provider credentials. Create one once, then look it up by
name on later deployments:

```python
provider = Connection.create(
    "my-openai",
    provider="openai",
    api_key=os.environ["OPENAI_API_KEY"],
)
provider = Connection.from_name("my-openai")
router = cc.Router.from_name("assistant")
router.refresh()
```

For explicit administration credentials, inject an optional client:

```python
from chalkcompute.router import RouterClient

with RouterClient(chalk_client=your_chalk_client) as client:
    provider = Connection.from_name("my-openai", client=client)
    router = cc.Router("assistant", default=provider.model("gpt-4.1-mini"))
    router.deploy(client=client)
```

Injected clients remain caller-owned. Concurrent route deployments are
last-writer-wins; there is no compare-and-swap operation.

## Server rollout

Deploy the matching metadata API, then the router, before using the additive SDK
API. Hosted routers receive the metadata API address automatically through the
existing `CHALK_API_HOST`; no new environment variables or secrets are needed
from the SDK user.

Function classification uses the incoming Chalk credential or router API key.
The metadata API checks the caller's route access and the current published
function binding with the router, then invokes that function in the router's
environment using Chalk's existing signing infrastructure. A route grant permits
its configured classifier; it does not grant arbitrary function invocation.

Creating or editing a function-backed route requires `deploy.redeploy`. Inference
callers only need access to invoke the route. The function retains its own runtime
identity and permissions. Endpoint and credentials are never route fields, and
the public `CallExternalFunction` RPC's permission policy is unchanged.
