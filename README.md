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
    api_key=cc.Secret.from_chalk_env("OPENAI_API_KEY"),
)
```

The scorer deploys as `trace-quality`, the model's name in kebab case; pass
`name=` to choose another. `inputs` names the dataset columns the judge reads
(default `("output",)`);
`prompt_fn` replaces the default prompt; `parse_fn` replaces structured
outputs for an endpoint without them. `completion_kwargs` (for example
`{"temperature": 0, "max_tokens": 400}`) go on every request as given. Any
OpenAI-compatible endpoint works via
`base_url`, including Chalk's AI router. Every function deployed from the
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
