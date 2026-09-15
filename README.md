# Chalk Sandbox SDK

Python SDK for the Chalk Sandbox gRPC service. Create sandboxes, execute commands, and stream output over bidirectional gRPC streams.

Contributor note: for testing deployed functions against local `chalkcompute`
or local `chalk-remote-call-python` changes, see
`local-sdk-remote-call-testing.md`.

## Install

```
pip install grpcio protobuf
```

## Quick start

```python
from chalkcompute import SandboxClient

with SandboxClient.from_env() as client:
    # Create a sandbox from a pre-built image
    sandbox = client.create(image="ubuntu:latest")

    # Run a command
    result = sandbox.exec("echo", "hello world")
    print(result.stdout_text)  # "hello world"
    print(result.exit_code)    # 0

    # Clean up
    sandbox.terminate()
```

## Authentication inside deployed functions

New deployments use the **invoking caller's identity** by default.
`SandboxClient.from_env()`, `Sandbox(...).run()`, and other chalkcompute clients
act with that caller's permissions in the selected environment. A read-only
caller cannot create a sandbox by invoking a provisioner. No token exchange code
belongs in the function.

The platform issues a bounded delegation, not the caller's original credentials.
Working tokens last at most five minutes and refresh automatically. The delegation
expires no later than the caller's credential or 24 hours after invocation;
nested calls and sandboxes with `chalk_identity=True` cannot extend it. Queued
calls carry this delegation and fail before user code if it expires or is revoked.
Permission additions do not expand an existing delegation. Revocations and changed
feature restrictions invalidate it when checked by the API.

Credentials and native transport caches follow each invocation, including concurrent
calls on a shared worker. Clients can be reused across callers. Caller mode never
falls back to a worker token, local login, or ambient web-identity file when its
credentials are missing. ChalkPy's internal `chalk_client_from_env` bridge cannot
safely rotate per-invocation credentials and is rejected in caller mode; use
chalkcompute clients for these operations. Explicitly supplied client credentials
remain an explicit choice of identity.

Scheduled jobs have no interactive caller. Configure an explicit service account:

```python
import chalkcompute as cc

@cc.function(
    schedule="1h",
    env={"CHALK_FUNCTION_AUTH_MODE": "service"},
    secrets=[cc.Secret("CHALK_CLIENT_ID"), cc.Secret("CHALK_CLIENT_SECRET")],
)
def scheduled_job() -> None:
    # Runs with the account stored in these Chalk-managed secrets.
    ...
```

Service mode deliberately grants every invocation the configured account's access;
limit its permissions and who can invoke the function. It receives no automatic
worker exchange token. A configured `CHALK_WEB_IDENTITY_TOKEN_FILE` is also supported.
Class startup/shutdown hooks have no caller scope; use service mode if they need
Chalk API access.

**Rollout:** deploy the matching API and function-queue changes before releasing
this SDK or redeploying functions. Older deployments keep their legacy auth until
redeployed. Upgrade callers and workers together for direct and queued calls;
Herdr's `chalk function call` path delegates in the API. Engine-side `catalog_call`
and internal scheduler calls do not yet mint caller delegations, so their targets
must explicitly use service mode. The `CHALK_FUNCTION_AUTH_MODE` container marker
selects `caller` (the SDK default) or `service`; unknown values are rejected.

## Declarative images

Build custom container images with a fluent API instead of writing Dockerfiles.
The image spec is serialized as protobuf and transmitted to the sandbox service,
which builds and caches the image before starting the container.

```python
from chalkcompute import Image, SandboxClient

# Build a data-science image declaratively
img = (
    Image.debian_slim()
    .pip_install(["pandas", "numpy", "scikit-learn"])
    .run_commands(
        "apt-get update && apt-get install -y git curl",
    )
    .workdir("/home/user/app")
    .env({"PYTHONDONTWRITEBYTECODE": "1"})
)

with SandboxClient.from_env() as client:
    sandbox = client.create(image=img)
    result = sandbox.exec("python", "-c", "import pandas; print(pandas.__version__)")
    print(result.stdout_text)
    sandbox.terminate()
```

### Base images

```python
# Arbitrary base image
img = Image.base("node:25-trixie-slim")

# Convenience: python + debian slim
img = Image.debian_slim()  # python:3.14-slim-trixie

# From an existing Dockerfile (contents are inlined, so you can chain more steps)
img = Image.from_dockerfile("Dockerfile").pip_install(["extra-dep"])
```

### Build steps

```python
img = (
    Image.debian_slim()
    # Install Python packages
    .pip_install(["requests", "flask"])

    # Install from a requirements.txt (read locally, inlined into the spec)
    .pip_install_from_requirements("requirements.txt")

    # Run shell commands (each becomes a Docker RUN layer)
    .run_commands(
        "apt-get update && apt-get install -y git",
        "mkdir -p /app/data",
    )

    # Add local files into the image
    .add_local_file("config.yaml", "/app/config.yaml")
    .add_local_file("entrypoint.sh", "/app/entrypoint.sh", mode=0o755)
    .add_local_dir("src", "/app/src")

    # Raw Dockerfile instructions
    .dockerfile_commands(["EXPOSE 8080", "HEALTHCHECK CMD curl -f http://localhost:8080/"])

    # Image-level configuration
    .workdir("/app")
    .env({"FLASK_APP": "app:create_app"})
    .entrypoint(["/app/entrypoint.sh"])
    .cmd(["serve"])
)
```

### Immutable composition

Each builder method returns a new `Image`, so intermediate images can be shared:

```python
base = Image.debian_slim().pip_install(["requests"])

# Two different images that share the same base
api_image = base.pip_install(["flask"]).workdir("/api")
worker_image = base.pip_install(["celery"]).workdir("/worker")

api_sandbox = client.create(image=api_image)
worker_sandbox = client.create(image=worker_image)

api_sandbox.terminate()
worker_sandbox.terminate()
```

## Connecting

```python
from chalkcompute import SandboxClient

# Insecure (local dev)
client = SandboxClient("localhost:50051")

# With TLS
client = SandboxClient("sandbox.example.com:443", use_tls=True)

# As a context manager
with SandboxClient("localhost:50051") as client:
    ...
```

### Rotating workload identity

The SDK can use a directly usable Chalk JWT from a rotating token file instead
of a client ID and secret:

```shell
export CHALK_WEB_IDENTITY_TOKEN_FILE=/var/run/secrets/chalk/identity-token
export CHALK_API_SERVER=https://api.chalk.ai
```

Each SDK client caches the token for the shorter of one hour or half of the
token's remaining lifetime from its `exp` claim, then re-reads the file on its
next authenticated operation. Tokens without `exp` use the one-hour limit.
Changing the configured file path bypasses the cache. The JWT's
`environment_id` claim selects the environment unless `CHALK_ENVIRONMENT` or
`CHALK_ENVIRONMENT_ID` is set explicitly. Queued function calls additionally
require `CHALK_GRPC_ENGINE`, because identity JWTs do not contain engine-routing
data.

### Workload identity federation

Use the authenticated Connect client to mint a short-lived OIDC token for a
third-party workload identity provider. For example, with Snowflake configured
to trust Chalk's issuer and JWKS:

```python
from chalkcompute import ConnectClient

token = ConnectClient().get_workload_identity_token("snowflakecomputing.com")
```

The token is scoped to the active Chalk environment. Its audience is the value
passed to `get_workload_identity_token`, and its signing key is published by the
Chalk API server at `/.well-known/jwks.json`.

## Chalk SQL

Run SQL against your Chalk environment without installing ChalkPy or defining
features locally. Authentication uses the same environment variables,
`~/.chalk.yml`, or Compute workload identity as the rest of this SDK.

```python
from chalkcompute import SQLClient

with SQLClient() as sql:
    result = sql.query("SELECT 1 AS value")
    print(result.query_id)
    table = result.to_arrow()
    print(sql.explain("SELECT 1 AS value"))

    job = sql.submit("SELECT * FROM chalk.datasets.support_examples",
                     resource_group="batch")
    print(job.query_id)  # Save this ID to resume with sql.get_job(id).
    result = job.wait(timeout=600)
    paths = result.download("./sql-results")
```

`query` waits for synchronous execution. Use `persist=True` to store results
remotely and download them later; the default returns inline Parquet, subject
to the RPC response-size limit. `submit` runs in the background and persists
its output. Neither operation registers a named dataset.

`job.refresh()` updates `job.status`; `job.wait()` returns a `SQLResult` or
raises `SQLError`. A waiting timeout raises `TimeoutError` and leaves the
remote job running. The polling service groups queued and running work into
one state and reports canceled jobs as failures. SQL execution is not
transparently retried after a transport failure because the query may already
have run.

`download(directory)` streams every Parquet shard to disk, refreshes older
signed URLs, and refuses to overwrite existing files. Keep the client open
while using its jobs and results. Use a fresh directory when retrying a
partially completed multi-file download.

Downloading needs no dataframe library. Install `chalkcompute[sql]` for
`to_arrow()`, and additionally install `pandas` for `to_pandas()`. Both
conversions load the full result into memory. These extras do not require
ChalkPy.

## Evaluations

Create a reusable evaluation by pinning a completed dataset revision to a
deployed task function and one or more deployed scorer functions. A dataset
name resolves to its latest revision when the evaluation is created, and the
resolved revision is then pinned. Function parameters bind to dataset columns
by name; scorers may additionally declare `output` and `trace` parameters.

```python
import chalkcompute as cc

dataset = cc.DatasetClient().upload(
    "support_goldens",
    "support_goldens.csv",
)

suite = cc.EvaluationSuite.create("Release")

@cc.function(name="support-answer")
def answer(input: str) -> str:
    return call_support_model(input)

@cc.function(name="response-quality")
def response_quality(input: str, output: str) -> cc.EvaluationScorerResult:
    score, details = score_response(input=input, output=output)
    return cc.EvaluationScorerResult(
        score=score,
        metadata={"details": details},
    )

evaluation = cc.Evaluation.create(
    "Customer Support Chatbot",
    dataset=dataset,
    task=answer,
    scorers=[response_quality],
    suite_id=suite.id,
)

run = evaluation.run(metadata={"git_sha": "abc123"}).wait()
print(run.status, run.result_dataset)
```

`DatasetClient.upload` accepts CSV or Parquet paths, multiple same-schema
files, PyArrow tables and record batches, column/row mappings, and dataframes
convertible to Arrow. It uploads ordinary tabular data and does not require
ChalkPy feature definitions. Uploading to an existing name creates a new
dataset revision.

`@cc.function` starts deployment in the background, allowing consecutive
definitions to build concurrently. `Evaluation.create` waits on those handles
before reading their immutable function version IDs. Existing functions can
instead be attached by reference:

```python
evaluation = cc.Evaluation.create(
    "Customer Support Chatbot",
    dataset=dataset,
    task=cc.RemoteFunction.from_name("support-answer"),
    scorers=[cc.RemoteFunction.from_version_id("fn_brand_alignment_v2")],
)
```

`RemoteFunction.from_name` resolves the currently selected version at lookup
time; evaluation creation then pins that version. `from_id` remains a
compatibility alias for `from_version_id`. An imperative `RemoteFunction` must
be explicitly deployed before it can be used in an evaluation.

### Built-in scorers

`cc.scorers` deploys common scorers without a function body. Each one takes an
`inputs` pair naming the candidate column and the column it is compared
against, scores between 0 and 1, and records the raw quantity as row metadata.

```python
match = cc.scorers.exact_match(case_insensitive=True)

evaluation = cc.Evaluation.create(
    "Capitals",
    dataset=dataset,
    task=answer,
    scorers=[match],
)
```

- `exact_match` — the two columns hold the same text, after optional trimming,
  whitespace collapsing, and case folding.
- `levenshtein` — edit distance, normalized to `1 - distance / len(longer)`.
- `regex_match` — a pattern appears in (or covers) one column, with the match
  and its named groups kept as metadata.
- `contains` — expected substrings, one or a JSON array, scored as all, any,
  or the fraction present.
- `numeric_close` — two columns as numbers, within an absolute or relative
  tolerance, or graded by how far apart they are.
- `string_similarity` — `jaro_winkler`, `jaccard`, `token_set`, `token_sort`,
  `partial`, or `sequence`, matching the `chalk.functions` of those names.
- `json_valid` — one column parses as JSON.
- `json_match` — two columns hold the same JSON, or agree on the dotted
  `paths` you name.
- `embedding_similarity` — cosine similarity of the two columns' embeddings,
  for an answer that is right but worded differently.

### LLM judges

`cc.scorers.llm_judge` deploys a scorer from a pydantic model (v1 or v2)
instead of a function body. The model's `score` field becomes the score; every other field
is recorded as row metadata. The reply is requested through the OpenAI client
with structured outputs and validated against the model, so a malformed grade
fails the row.

```python
from pydantic import BaseModel, Field

class TraceQuality(BaseModel):
    score: float = Field(ge=0, le=1, description="Overall quality.")
    directness: int = Field(ge=0, le=2, description="Shortest reasonable path to the goal.")
    task_correctness: int = Field(ge=0, le=2, description="Was the task actually completed?")
    reason: str

trace_quality = cc.scorers.llm_judge(
    TraceQuality,
    model="gpt-5",
    instructions="You are grading a browser agent's login attempt.",
)
```

The scorer deploys as `trace-quality`, the model's name in kebab case; pass
`name=` to choose another. `inputs` names the dataset columns the judge reads
(default `("output",)`);
`prompt_fn` replaces the default prompt; `parse_fn` replaces structured
outputs for an endpoint without them. `completion_kwargs` (for example
`{"temperature": 0, "max_tokens": 400}`) go on every request as given.

The judge above names no provider key: with neither `api_key` nor `base_url`
it calls [Chalk's AI router](https://docs.chalk.ai/docs/ai-router) as the
environment itself, and the router holds the provider credentials. To call a
provider directly instead, pass `api_key=cc.Secret.from_chalk_env("OPENAI_API_KEY")`,
and `base_url` for any other OpenAI-compatible endpoint. Every function deployed from the
same module imports that module, so give sibling functions an image with
`pydantic` as well. A judge is generated
at import time, so it cannot be deployed in strip mode.

## Deployment revisions and rollback

Scaling groups and functions have stable parent IDs with immutable deployment
revisions beneath them. Scaling groups append a revision on each `deploy()`.
Functions and class methods reuse an existing version when the source, image
recipe, configuration, and managed secret revisions are unchanged—even across
separate runs of your script. The unchanged path makes one ensure request without
building an image or uploading source files. Only missing source content is
uploaded on a change; unchanged images and source snapshots are shared within
the environment.

Use `deploy(force_new_version=True)` to create a fresh function version while
still reusing image/source preparation. External secret providers and integration
secrets conservatively disable function-version reuse because their values can
rotate outside Chalk. Referenced data volumes retain their existing semantics.
This requires a server with `EnsureExternalFunction` support.

```python
group = cc.ScalingGroup(name="api", image="registry.example/api:v1").deploy()
group.deploy()  # updates the same group and creates another revision
for revision in group.revisions():
    print(revision.id, revision.status, revision.is_current)
group.rollback("sgr_previous")

@cc.function(name="rank")
def rank(query: str) -> str:
    return query

rank.deploy()
rank.deploy()  # reuses the unchanged version
rank.deploy(force_new_version=True)  # explicitly creates a new version
for version in rank.versions():
    print(version.id, version.created_at, version.is_current)
rank.rollback("efv_previous")
```

Use `ScalingGroup.from_id(...)` or `RemoteFunction.from_function_id(...)` to
attach to a stable parent. `RemoteFunction.from_version_id(...)` attaches
through an immutable version and still exposes its parent lifecycle. `refresh()`
follows the parent's currently selected revision, and `delete()` deletes the
stable parent and all of its revisions.

Scorers may return a numeric scalar, one `EvaluationScorerResult`, or a
`list[EvaluationScorerResult]`. Returning a list lets one scorer emit multiple
scores from shared computation; an empty list emits no scores for that row.
Each result carries a normalized score and optional row-level JSON-serializable
metadata. The return annotation declares the Arrow schema, and the class-level
Arrow hooks handle nested serialization, so the generic function runtime does
not need scorer-specific behavior.

## Sandbox lifecycle

```python
# Create with resource limits
sandbox = client.create(
    image="ubuntu:latest",
    cpu="2",
    memory="4Gi",
    env={"DEBIAN_FRONTEND": "noninteractive"},
    chalk_identity=True,
)

# List all sandboxes
for info in client.list():
    print(f"{info.id} {info.status} {info.name}")

# Get a handle to an existing sandbox by ID
existing_sandbox = client.get(id="550e8400-e29b-41d4-a716-446655440000")

# Fetch info from server
info = existing_sandbox.refresh()  # force re-fetch
print(info.status)

# Terminate, optionally with a grace period
sandbox.terminate()
existing_sandbox.terminate(grace_period_seconds=30)
```

Set `chalk_identity=True` to give the sandbox a platform-managed Chalk
identity. The sandbox receives `CHALK_WEB_IDENTITY_TOKEN_FILE` and the Chalk
API/environment settings it needs to authenticate without caller credentials
being copied into the workload.

## Executing commands

### Run and wait

```python
result = sandbox.exec("ls", "-la", "/tmp")
for line in result.stdout:
    print(line)
for line in result.stderr:
    print(f"ERR: {line}")
print(f"exit code: {result.exit_code}")

# Or get the full text at once
print(result.stdout_text)
print(result.stderr_text)
```

### Stream output in real time

```python
for event in sandbox.exec_stream("make", "build", workdir="/app"):
    if event.stdout:
        print(event.stdout, end="")
    if event.stderr:
        print(event.stderr, end="", file=sys.stderr)
    if event.is_exited:
        print(f"\nDone: exit code {event.exit_code}")
```

### Interactive processes (stdin + signals)

```python
process = sandbox.exec_start("bash")

process.write_stdin("echo hello\n")
process.write_stdin("exit\n")
process.close_stdin()

for event in process.output():
    if event.stdout:
        print(event.stdout, end="")
```

Send signals to running processes:

```python
import signal

process = sandbox.exec_start("sleep", "300")
process.send_signal(signal.SIGTERM)
result = process.wait()
```

### Options

All exec methods accept the same keyword arguments:

```python
result = sandbox.exec(
    "python", "train.py",
    workdir="/app",                     # working directory
    timeout_secs=3600,                  # kill after 1 hour
    env={"CUDA_VISIBLE_DEVICES": "0"},  # environment variables
)
```

## Examples

### Clone a GitHub repo into a sandbox

```python
from chalkcompute import SandboxClient

client = SandboxClient.from_env()
sandbox = client.create(image="ubuntu:latest")

# Install git
sandbox.exec("apt-get", "update")
sandbox.exec("apt-get", "install", "-y", "git")

# Clone
result = sandbox.exec(
    "git", "clone", "https://github.com/chalk-ai/chalk.git", "/workspace/chalk"
)
if result.exit_code != 0:
    print(f"Clone failed: {result.stderr_text}")
else:
    # List what we got
    result = sandbox.exec("ls", "-la", "/workspace/chalk")
    for line in result.stdout:
        print(line)

sandbox.terminate()
client.close()
```

### Spawn an OpenCode agent in a sandbox

[OpenCode](https://github.com/opencode-ai/opencode) is a terminal-based AI coding agent. You can run it inside a sandbox to give it an isolated environment to work in.

```python
from chalkcompute import SandboxClient

client = SandboxClient.from_env()
sandbox = client.create(
    image="ubuntu:latest",
    cpu="2",
    memory="4Gi",
    env={
        "ANTHROPIC_API_KEY": "sk-ant-...",
    },
)

# Install dependencies
sandbox.exec("apt-get", "update")
sandbox.exec("apt-get", "install", "-y", "git", "curl", "build-essential")

# Install Go (opencode is a Go binary)
sandbox.exec("bash", "-c", "curl -fsSL https://go.dev/dl/go1.26.3.linux-amd64.tar.gz | tar -C /usr/local -xz")
sandbox.exec("bash", "-c", "echo 'export PATH=$PATH:/usr/local/go/bin:/root/go/bin' >> /root/.bashrc")

# Install opencode
sandbox.exec("bash", "-c", "export PATH=$PATH:/usr/local/go/bin:/root/go/bin && go install github.com/opencode-ai/opencode@latest")

# Clone a repo to work on
sandbox.exec("git", "clone", "https://github.com/your-org/your-repo.git", "/workspace/repo")

# Run opencode non-interactively with a prompt
result = sandbox.exec(
    "bash", "-c",
    "export PATH=$PATH:/usr/local/go/bin:/root/go/bin && cd /workspace/repo && opencode -p 'fix the failing tests in pkg/auth'",
    timeout_secs=600,
)
print(result.stdout_text)

# Or run it interactively and feed it commands
process = sandbox.exec_start(
    "bash", "-c",
    "export PATH=$PATH:/usr/local/go/bin:/root/go/bin && cd /workspace/repo && opencode",
)

# Stream its output
for event in process.output():
    if event.stdout:
        print(event.stdout, end="")
    if event.stderr:
        print(event.stderr, end="", file=sys.stderr)
    if event.is_exited:
        break

sandbox.terminate()
client.close()
```

### Long-running build with real-time output

```python
from chalkcompute import SandboxClient

client = SandboxClient.from_env()
sandbox = client.create(image="node:25-trixie-slim")

sandbox.exec("git", "clone", "https://github.com/your-org/frontend.git", "/app")
sandbox.exec("npm", "install", workdir="/app")

# Stream the build output as it happens
for event in sandbox.exec_stream("npm", "run", "build", workdir="/app"):
    if event.stdout:
        print(event.stdout, end="")
    if event.stderr:
        print(event.stderr, end="", file=sys.stderr)
    if event.is_exited and event.exit_code != 0:
        print(f"Build failed with exit code {event.exit_code}")

sandbox.terminate()
client.close()
```

### Functions defined in notebooks

When `@chalkcompute.function()` runs in an IPython/Jupyter notebook (including
Chalk notebooks), the SDK captures the function and its transitive source
dependencies from executed cells through its definition. It uses the same Rust
dependency analyzer as Chalk's notebook graph and run planner. Imports used only
inside a helper or function body are included too.

The snapshot is captured before background deployment starts. Rerun the defining
cell after changing an upstream import, helper, or constant to deploy the updated
code. The worker does not depend on the notebook kernel staying alive.

Only required top-level imports, definitions, and assignments are included;
unrelated plotting, display, and invocation statements are omitted. Required
initialization expressions execute again when the worker imports the module.
Pass computed notebook results as arguments or use explicit datasets/volumes
when rerunning their initialization is inappropriate. Kernel-injected state,
missing source, and dependencies produced by top-level control flow produce a
build error with instructions for making the function portable. Mutations of
existing objects and dynamic `exec`/wildcard imports cannot be inferred reliably
from the source graph; move that setup into an explicit function or module.

Packages still belong in the function's `image=Image...pip_install(...)` settings;
copying an import does not install its package. Functions imported from ordinary
Python files continue to use file/package deployment.

## Return sandbox handles from functions

A provisioning function can return a `Sandbox` directly. Create the sandbox and
finish its setup before returning; do not terminate it in a context manager.

```python
import chalkcompute as cc

@cc.function(name="provision-agent")
def provision_agent() -> cc.Sandbox:
    sandbox = cc.Sandbox(
        image="node:22-bookworm",
        compute_class=cc.ComputeClass.HOST,
        lifetime="3600s",
        entrypoint=["sleep", "infinity"],
        # secrets=[cc.Secret("ANTHROPIC_API_KEY")],
    ).run()
    sandbox.exec("mkdir", "-p", "/workspace")
    return sandbox

# After deployment, the annotated function restores a Sandbox handle.
sandbox = provision_agent.remote()
print(sandbox.exec("pwd").stdout_text)
sandbox.terminate()
```

The invoking caller needs sandbox creation and access permissions in its Chalk
environment. The provisioner uses that same identity by default. Arrow transports one nonnullable
struct field, `sandbox_id: large_utf8`, using the existing custom-object contract
(`__chalk_arrow_type__`, `__chalk_serialize__`, `__chalk_deserialize__`). It never
includes the creator's credentials, environment variables, secret references,
client connection, or sandbox specification. Decoding performs no network I/O;
the first operation authenticates using the receiving process's identity. A
handle does not extend the server-enforced lifetime or recreate an expired sandbox.
Unstarted sandboxes cannot be serialized. Optional and list annotations work,
including empty lists.

Herdr's Chalk plugin can consume the same Arrow result through `chalk function
call --output-file`. It checks access and finite lifetime through the Chalk CLI
and discovers existing PTY sessions using the run marker. Working directory and
cleanup policy belong in Herdr's `provisionFunction` configuration; they are not
part of a general-purpose `Sandbox` handle.

### Remote execution errors

Remote handler failures raise `chalkcompute.RemoteExecutionError`, a subclass of
`FunctionError`. Its `exception_type`, `message`, and `remote_traceback` describe
the original Python exception. `internal_traceback` retains runtime diagnostics;
`call_id` and `trace_id` are available when supplied by the call path.

IPython and Chalk notebooks show the local call site followed by the remote
traceback, preserving exception chains. Notebook source locations refer to the
snapshot deployed with the function, so editing a cell cannot change the source
shown for an earlier deployment. Plain Python also gets a readable multiline
error. User execution failures are not automatically retried.

This presentation requires a function deployed with remote-call runtime 1.9.2
or later. Upgrade the SDK in the calling environment before redeploying: function
images pin the caller's installed runtime version. Existing deployments keep
their existing error format until redeployed.

The SDK honors `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` and
`OTEL_EXPORTER_OTLP_PROTOCOL` (`grpc` or `http/protobuf`). SDK-owned trace export
failures produce one warning per outage; exporter retry diagnostics remain
available at debug level.
