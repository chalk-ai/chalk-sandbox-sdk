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

## Logging and progress

The SDK leaves Python logging configuration to your application. Importing
`chalkcompute` does not install handlers, change log levels, or configure the
root logger. Deployment status is visible by default in terminals, notebooks, and
captured scripts, independently of Python logging. With no logging configuration,
Python's default stderr handler reports warnings and errors. Progress and the
SDK's console diagnostics use stderr, leaving stdout available for script results.

| Context | Default output |
| --- | --- |
| Interactive terminal | Colored deployment status lines on stderr |
| Notebook, CI, redirected stderr, or `TERM=dumb` | Plain deployment status lines on stderr |
| Application with logging configured | Deployment status remains visible; diagnostic records also go to the application's handlers |

`CHALK_SANDBOX_TUI=false` (or `0`) disables progress, including in a terminal.
`CHALK_SANDBOX_TUI=true` (or `1`) explicitly enables status lines, including plain
text in notebooks. Unset it or use `auto` for the default behavior above. Terminal
detection controls color, not visibility. Progress uses
a named group with a vertical guide for its steps and a closing success/failure
line. It prints only when a step changes. If concurrent deployments interleave,
the group heading repeats with `(continued)` to identify the following steps.
It never rewrites previous lines, replaces standard streams, or hides warnings
and errors. No renderer dependency is required.

Color is automatic for interactive terminals. `NO_COLOR`, `TERM=dumb`, notebooks,
CI, and redirected streams disable it. If a program controls a pseudo terminal,
set `NO_COLOR=1` for plain status, or `CHALK_SANDBOX_TUI=false` to disable status.
Disabling progress does not disable diagnostics or customer output. To route SDK
information only through your application's logging, disable progress and configure
the `chalkcompute` logger as shown below.

```text
Function "fraud"
  │ › Registering function
  │ ✓ Function registered
  │ › Waiting for readiness
  │ › Pending ready=0 available=0
  │ › Pending ready=0 available=0 — waiting for startup probe
  └ ✓ Ready
```

For additional console diagnostics in a script or notebook:

```python
import chalkcompute as cc

cc.configure_logging()          # INFO: lifecycle events and readiness changes
# cc.configure_logging("DEBUG") # Every readiness poll and transport retry
# cc.configure_logging("WARNING") # Warning/error diagnostics only; progress remains
```

The helper writes unwrapped text to stderr, with colored level labels on
interactive terminals and plain text elsewhere. It never interprets markup.
Pass `color=False` to disable color, or `stream=` to use another text stream. It is
safe to rerun in notebook cells: each call replaces only the helper's previous
handler. It configures only the `chalkcompute` logger and disables propagation to
the root logger to avoid duplicate log records. Configuring logging does not change
progress. INFO diagnostics include lifecycle events also shown in progress; set
`CHALK_SANDBOX_TUI=false` if you prefer just the diagnostic format.

Local customer `print()` output and application logging are untouched. Container
and sandbox `.exec()` output remains captured in result objects; streaming APIs
return output events for the caller to consume. Remote function stdout/stderr is
not automatically forwarded to the caller by this SDK.

An unobserved background deployment failure always writes its explanation to
stderr and exits unsuccessfully, even when logging or progress is disabled. This
fatal report is independent of diagnostic log levels and handlers.

If your application already configures logging, use that configuration instead
of the helper. All Python SDK loggers are under `chalkcompute`, so they work with
your existing text, JSON, file, or telemetry handlers:

```python
import logging

# In an application's logging setup:
logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("chalkcompute").setLevel(logging.INFO)
```

At INFO, functions, classes, containers, and scaling groups report readiness only
when the state, replica counts, or status message changes. Repeated polls and
routine transport recovery are DEBUG diagnostics. For example, a slow startup
produces these status records rather than a line every half second:

```text
INFO chalkcompute._function: Scaling group fraud: Pending ready=0 available=0
INFO chalkcompute._function: Scaling group fraud: Pending ready=0 available=0 — waiting for externally routed startup probe
INFO chalkcompute._function: Scaling group fraud: Available ready=1 available=1
```

Background deployment failures are ERROR records, including the Python definition
location and actionable exception details. Definition-only deploy scripts still
exit unsuccessfully when a background deployment fails. Remote command stdout and
stderr remain available in result objects and stream events for callers to handle.

## Environment variables and secrets

Deployment `env` mappings accept strings and `Secret` references together:

```python
import chalkcompute as cc

sandbox = cc.Sandbox(
    image="ubuntu:latest",
    env={
        "GIT_AUTHOR_NAME": "Elliot Marx",
        "SUPER_SECRET_VALUE": cc.Secret.from_chalk_env("MY_CHALK_SECRET"),
    },
)
```

The dictionary key sets the injected environment variable name.
This works across
`SandboxClient.create`, `Container`, `ScalingGroup`, `@cc.function`, `@cc.cls`,
and `@cc.training`.

Use `Secret.from_local_env("LOCAL_VAR")` for a value read at run/deploy time, or
`Secret.from_chalk_integration("postgres", keys=["PASSWORD"])` to select one
integration secret. Bulk integrations and `Secret.from_local_env_file(...)`
belong in `secrets=[...]`, which can be used alongside `env`.
Training supports Chalk secret references only; local secret resolution is
not supported for training.

`Image.env(...)` and per-exec env mappings accept literal strings only. Set
secrets on the sandbox or other deployment so its processes inherit them.

To make credentials available only to requests for specific hosts, add them to
a sandbox before running it. Strings name existing Chalk secrets; use an
explicit `Secret` for local credentials or aliases:

```python
sandbox = (
    cc.Sandbox(image="ubuntu:latest")
    .add_credentials("api.openai.com", "OPENAI_API_KEY")
    .add_credentials(
        ["api.slack.com", "*.slack.com"],
        cc.Secret.from_local_env("SLACK_BOT_TOKEN"),
    )
)

with cc.SandboxClient.from_env() as client:
    client.run(sandbox)
```

The corresponding environment variables contain placeholders. The network
policy substitutes them when they appear in headers on requests to the listed
hosts.

## Declarative images

Build custom container images in Python instead of writing Dockerfiles.
The image spec is serialized as protobuf and transmitted to the sandbox service,
which builds and caches the image before starting the container.

```python
from chalkcompute import Image, SandboxClient

# Build a data-science image declaratively
img = (
    Image.debian_slim()
    .pip_install(["pandas", "numpy", "scikit-learn"])
    .apt_install(["git", "curl"])
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

    # Install Debian/Ubuntu system packages
    .apt_install(["git", "tmux"])

    # Run shell commands (each becomes a Docker RUN layer)
    .run_commands(
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

## Function names

Omit `name=` when defining functions. The registered name defaults to the Python
function's name, including underscores:

```python
import chalkcompute as cc

@cc.function
def add_numbers(a: int, b: int) -> int:
    return a + b
```

Other scripts can attach with `cc.RemoteFunction.from_name("add_numbers")`.
Cross-process lookup and repeated deployment already work with the default name.
The `@cc.cls` decorator likewise defaults to the Python class's name.

Use `name=` only when you intentionally need a different registered name, such as
preserving an existing deployment's name after renaming the Python function.
Changing or removing an existing override changes which function you deploy; it
does not rename the existing deployment. Function names containing underscores
and hyphens are distinct, but can collide when converted to scaling-group names.

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

@cc.function
def answer(input: str) -> str:
    return call_support_model(input)

@cc.function
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
    task=cc.RemoteFunction.from_name("answer"),
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

@cc.function
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

### Creating sandboxes

Use `client.create(...)` to construct and immediately run a sandbox:

```python
sandbox = client.create(
    image="ubuntu:latest",
    cpu="2",
    memory="4Gi",
    env={"DEBIAN_FRONTEND": "noninteractive"},
    chalk_identity=True,
)
```

To configure a sandbox before running it, save it to a variable and pass it to
`client.run(...)` when it is ready:

```python
sandbox = Sandbox(image="ubuntu:latest", cpu="2", memory="4Gi")
if use_shared_cache:
    sandbox.mount_volume("build-cache", "/cache")

client.run(sandbox)
```

Set `chalk_identity=True` to give the sandbox a platform-managed Chalk
identity. The sandbox receives `CHALK_WEB_IDENTITY_TOKEN_FILE` and the Chalk
API/environment settings it needs to authenticate without caller credentials
being copied into the workload.

### Managing sandboxes

```python
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

### Git inside a sandbox

Omitting `image` uses `Image.sandbox()`: Python 3.14 on Debian slim with Git,
CA certificates and OpenSSH, working in `/workspace`.
The image builds lazily through the normal image builder. Both `Sandbox().run()` and `client.create()`
use this default. Explicit images retain their existing behavior; custom images
need `git` for Git helpers.

```python
from chalkcompute import SandboxClient

with SandboxClient.from_env() as client:
    sandbox = client.create()
    try:
        # Uses the remote's default branch with depth=1.
        sandbox.git.clone("https://github.com/octocat/Hello-World.git", "repo")
        print(sandbox.git.status("repo").current_branch)
        print(sandbox.git.branches("repo"))
    finally:
        sandbox.terminate()
```

Git helpers also support `init`, `branches`, `create_branch`, `checkout_branch`,
`delete_branch`, `fetch`, `pull`, `push`, `reset`, `restore`, `remote_add`,
`remote_get`, `remotes`, `get_config`, `set_config`, and `configure_user`.
Pass `branch`, `commit_id`, or `depth` to `clone` to select a branch, detached
commit, or history depth (`1` by default; `None` for full history). Pulls fast-forward
only; branch deletion requires `force=True` for unmerged work. Config changes apply only to that repository.
HTTPS credentials can be supplied as `username` and `password` to clone/fetch/
pull/push; they are scoped to the command without changing the remote URL or
saving credentials. SSH remotes use the sandbox's existing SSH configuration.
Git failures raise `GitError`.

Git paths identify the repository root relative to the image working directory.
Helpers accept `timeout_secs`; multi-command operations apply it to each command.
See the [sandbox Git guide](docs/sandbox-git.md) for workflows and a method reference.
See [the sandbox capability guide](docs/sandbox-capabilities.md) for defaults.

### Add tmux to a sandbox image

Use `apt_install` like `pip_install` to add system packages to a Debian/Ubuntu
image. It requires a root build user and installs at build time, so each sandbox
starts with the tools ready to use. The builder returns a new image and supports
version pins such as
`"package=version"`.

```python
from chalkcompute import Image, SandboxClient

image = Image.sandbox().apt_install(["tmux"])
with SandboxClient.from_env() as client:
    sandbox = client.create(image=image)
    try:
        # Start a detached shell; it survives this exec call completing.
        sandbox.exec("tmux", "new-session", "-d", "-s", "work")
        sandbox.exec("tmux", "send-keys", "-t", "work", "echo hello", "Enter")
        print(sandbox.exec("tmux", "list-sessions").stdout_text)
        print(sandbox.exec("tmux", "capture-pane", "-p", "-t", "work").stdout_text)
        sandbox.exec("tmux", "kill-session", "-t", "work")
    finally:
        sandbox.terminate()
```

tmux sessions live inside the sandbox and end when it terminates. These commands
use detached sessions through ordinary exec; interactive `tmux attach` requires
a terminal with PTY support. For images using another package manager, install
tmux with `run_commands` (for example, `apk add --no-cache tmux` on Alpine).

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

@cc.function
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

The function's identity needs sandbox creation permissions. The caller needs
sandbox access in its own Chalk environment. Arrow transports one nonnullable
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

### Background deployment errors

If a script only defines decorated functions, failed background deployments are
reported at exit with a nonzero status. Known API and function deployment errors
show the function's source location and error messages without SDK or transport
stack frames. RPC failures include the status code (such as `ALREADY_EXISTS`) in
the failure heading. Server-provided recovery instructions, including CLI
commands and dashboard links, are preserved in the output.
Unexpected exceptions always include their full traceback.

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

### Catalog discovery of typed functions

The `Sandbox` Arrow field carries `metadata["chalk.python.type"] =
"chalkcompute.Sandbox"`, derived from its `__chalk_type_id__` declaration. A
catalog client can identify the return type without importing Python or treating
an arbitrary lookalike struct as a Sandbox. Check the actual struct shape,
nonnullable output, and `chalk.function.is_generator` metadata as well when a
workflow needs exactly one Sandbox.

Function registration also publishes the docstring in output-schema metadata
`chalk.function.description` and JSON-serializable parameter defaults in each
input field's `chalk.default` metadata. Defaults that cannot be encoded as JSON
are omitted. These values are catalog-visible; keep secrets in Chalk-managed
secret references rather than function defaults. Clients can show descriptions,
collect required arguments, and reconstruct declared defaults without invoking
the function. Record and invoke the same immutable version selected by the user.
